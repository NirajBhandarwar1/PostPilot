"""One short health line: are the free model quotas about to run out, and did the scout work?

Free tiers run out silently (Cloudflare: 10,000 neurons per UTC day, Groq: ~200K tokens per day), and
nobody noticed until a call failed. llm.py adds every answered call here; the ideas email and `status`
read it back. Pure functions over the db meta table; `now` is a parameter so tests need no real clock.
Nothing here may raise into a real model call or an email send."""

import logging
from datetime import UTC, datetime

from . import db
from .config import env

log = logging.getLogger("postpilot.health")

LOW = 0.8      # "running low" from here; the line gets a warning sign too

# Cloudflare bills Workers AI in "neurons", per model: neurons per 1M tokens (in, out).
NEURON_RATES: dict[str, tuple[int, int]] = {
    "@cf/openai/gpt-oss-120b": (31818, 68182),
    "@cf/nvidia/nemotron-3-120b-a12b": (45455, 136364),
}
DEFAULT_RATE = NEURON_RATES["@cf/openai/gpt-oss-120b"]      # a model not in the table: a guess, flagged as one

# provider name -> (tracked service, display name, unit, env var for the daily budget, default budget)
# "qwen" is Qwen on Groq, so it shares Groq's quota.
SERVICES = {"cloudflare": ("cloudflare", "Cloudflare", "neurons", "CLOUDFLARE_DAILY_NEURONS", 10000),
            "groq": ("groq", "Groq", "tokens", "GROQ_DAILY_TOKENS", 200000),
            "qwen": ("groq", "Groq", "tokens", "GROQ_DAILY_TOKENS", 200000)}
ORDER = ("cloudflare", "groq")


def _day(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%d")      # both free tiers reset at 00:00 UTC


def _key(service: str, now: datetime | None = None) -> str:
    return f"quota:{service}:{_day(now)}"


def _approx_key(service: str, now: datetime | None = None) -> str:
    return f"quota_approx:{service}:{_day(now)}"


def budget(service: str) -> int:
    for svc, _name, _unit, var, default in SERVICES.values():
        if svc == service:
            try:
                return max(int(env(var, str(default)) or default), 1)
            except ValueError:
                return default
    return 1


def neurons(model: str, prompt_tokens: int, completion_tokens: int) -> tuple[int, bool]:
    """Estimated neurons for one call, and whether the model's rate was a guess."""
    known = model in NEURON_RATES
    r_in, r_out = NEURON_RATES.get(model, DEFAULT_RATE)
    return round(prompt_tokens * r_in / 1_000_000 + completion_tokens * r_out / 1_000_000), not known


def record_usage(provider: str, model: str, prompt_tokens: int, completion_tokens: int,
                 now: datetime | None = None) -> None:
    """Add one answered call to today's counter. Providers we do not track are ignored. Never raises."""
    try:
        spec = SERVICES.get(provider)
        if not spec:
            return
        service = spec[0]
        if service == "cloudflare":
            used, guess = neurons(model, prompt_tokens, completion_tokens)
            if guess:
                db.set_meta(_approx_key(service, now), "1")
        else:
            used = prompt_tokens + completion_tokens
        if used > 0:
            db.add_meta_int(_key(service, now), used)
            _warn_once(service, now)
    except Exception as e:
        log.warning("could not track quota use: %s", str(e)[:200])


def _warn_once(service: str, now: datetime | None = None) -> None:
    """WARNING the first time today's use crosses 80%, and again at 100%. The level is remembered in meta
    so a busy day does not log the same warning on every call."""
    total = int(db.get_meta(_key(service, now), "0") or 0)
    cap = budget(service)
    level = 100 if total >= cap else 80 if total >= LOW * cap else 0
    if not level:
        return
    wkey = f"quota_warn:{service}:{_day(now)}"
    if int(db.get_meta(wkey, "0") or 0) >= level:
        return
    db.set_meta(wkey, str(level))
    name = next(s[1] for s in SERVICES.values() if s[0] == service)
    log.warning("%s free quota %s for today (%s)", name, "used up" if level == 100 else "running low (80%+)",
                "; ".join(line for line in quota_lines(now) if line.startswith(name)))


def mark_used_up(provider: str, now: datetime | None = None) -> None:
    """The provider said today's free allocation is gone: show 100% whatever our own count says. Never raises."""
    try:
        spec = SERVICES.get(provider)
        if not spec:
            return
        service = spec[0]
        used = int(db.get_meta(_key(service, now), "0") or 0)
        db.set_meta(_key(service, now), str(max(used, budget(service))))
        _warn_once(service, now)
    except Exception as e:
        log.warning("could not mark quota used up: %s", str(e)[:200])


def _usage(now: datetime | None = None) -> list[tuple[str, str, int, int, bool]]:
    """(display name, unit, used today, budget, approximate) for each tracked service with any use today."""
    out = []
    for service in ORDER:
        spec = next(s for s in SERVICES.values() if s[0] == service)
        used = int(db.get_meta(_key(service, now), "0") or 0)
        if used <= 0:
            continue
        approx = service == "cloudflare" and db.get_meta(_approx_key(service, now)) == "1"
        out.append((spec[1], spec[2], used, budget(service), approx))
    return out


def _pct(used: int, cap: int) -> int:
    return int(used * 100 / cap)        # rounds down, so "100%" only appears when it is really used up


def quota_lines(now: datetime | None = None) -> list[str]:
    """One line per provider used today, e.g. "Cloudflare 62% of today's free quota (~6,200/10,000 neurons)"."""
    try:
        lines = []
        for name, unit, used, cap, approx in _usage(now):
            tilde = "~" if name == "Cloudflare" else ""        # neurons are always an estimate
            detail = f"({tilde}{used:,}/{cap:,} {unit}{', approx.' if approx else ''})"
            if used >= cap:
                lines.append(f"{name} used up for today {detail}")
                continue
            line = f"{name} {_pct(used, cap)}% of today's free quota {detail}"
            lines.append(line + (", running low" if used >= LOW * cap else ""))
        return lines
    except Exception as e:
        log.warning("could not read quota: %s", str(e)[:200])
        return []


def model_health(days: int = 1) -> str:
    """'models ok 41/44': model calls that gave usable output today (UTC), or '' with no calls."""
    try:
        rates = db.llm_pass_rates(days)
        good = sum(g for g, _ in rates.values())
        total = sum(t for _, t in rates.values())
        return f"models ok {good}/{total}" if total else ""
    except Exception as e:
        log.warning("could not read model health: %s", str(e)[:200])
        return ""


def health_line(scout: dict | None = None, now: datetime | None = None) -> str:
    """ONE short line: 'Health: scout ok, 3 ideas (1 dropped) · Cloudflare 62% of today's quota · Groq 14% · models ok 41/44'.

    `scout` is the batch stats dict (pitched, dropped, ...). A provider at 80% or more puts a warning first."""
    try:
        parts = []
        if scout is not None:
            ideas = int(scout.get("pitched") or 0)
            dropped = int(scout.get("dropped") or 0)
            parts.append(f"scout ok, {ideas} idea{'s' if ideas != 1 else ''}" + (f" ({dropped} dropped)" if dropped else ""))
        warn = False
        try:
            usage = _usage(now)
        except Exception as e:          # a broken quota read must not hide the rest of the line
            log.warning("could not read quota: %s", str(e)[:200])
            usage = []
        for i, (name, _unit, used, cap, _approx) in enumerate(usage):
            warn = warn or used >= LOW * cap
            if used >= cap:
                parts.append(f"{name} used up")
            else:
                parts.append(f"{name} {_pct(used, cap)}%" + (" of today's quota" if i == 0 else ""))
        if mh := model_health():
            parts.append(mh)
        while len(parts) > 1 and len("Health: " + " · ".join(parts)) > 150:      # the line is a glance, not a report
            parts.pop()
        if not parts:
            return ""
        return ("⚠ " if warn else "") + "Health: " + " · ".join(parts)
    except Exception as e:
        log.warning("could not build the health line: %s", str(e)[:200])
        return ""


def log_health(scout: dict | None = None) -> None:
    """Log the health line after a scout: WARNING when a quota is at 80%+ or the scout found no ideas, else INFO."""
    line = health_line(scout)
    if not line:
        return
    empty = scout is not None and not int(scout.get("pitched") or 0)
    (log.warning if line.startswith("⚠") or empty else log.info)("%s", line)
