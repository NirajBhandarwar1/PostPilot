"""Open-model client. Any OpenAI-compatible endpoint works: Groq, OpenRouter, Ollama (local).

Providers are tried in the order of LLM_PROVIDERS; if one fails (rate limit, outage, bad JSON
twice) the next one gets the same request. Output is validated against a Pydantic model.
The same file counts today's free-tier quota and builds the one-line health summary.
"""

import contextlib
import copy
import dataclasses
import json
import logging
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import BaseModel, ValidationError

from . import db
from .config import env

log = logging.getLogger("postpilot.llm")


@dataclass
class Provider:
    name: str
    base_url: str
    model: str
    api_key: str
    strict_schema: bool      # supports response_format json_schema strict
    extra: dict              # provider-specific body fields
    tokens_field: str = "max_tokens"   # OpenAI's reasoning models want max_completion_tokens
    token_scale: float = 1.0           # reasoning models think before answering: allow more
    paid: bool = False                 # counts against the daily spend cap (R3)
    tpm: int = 0                       # free-tier tokens per minute (input + reserved output); 0 = unknown


# Token use per provider/model since the last reset_usage(), so each batch reports its own cost.
USAGE: dict[str, dict[str, int]] = {}


def reset_usage() -> None:
    USAGE.clear()


# ---------- daily spend cap for paid providers (R3) ----------

def _spend_key() -> str:
    return "paid_tokens:" + time.strftime("%Y-%m-%d")


def paid_tokens_today() -> int:
    from . import db
    return int(db.get_meta(_spend_key(), "0") or 0)


def _add_paid_tokens(n: int) -> None:
    from . import db
    db.add_meta_int(_spend_key(), n)       # one statement: the loop and the CLI can't lose updates


def daily_cap() -> int:
    return int(env("OPENAI_DAILY_TOKENS", "100000") or 100000)


_cap_warned: set[str] = set()


class BudgetExceeded(RuntimeError):
    """The current token budget (e.g. one eval run) is used up; stop cleanly."""


class NoCredit(RuntimeError):
    """The provider says the account has no credit left. Waiting won't help: skip it for a while."""


# Cloudflare's free tier says the same when its 10,000 daily neurons are gone (it resets 00:00 UTC).
# OpenAI answers HTTP 429 both for "too fast" (retry) and for "no credit left" (never retry).
NO_CREDIT = re.compile(r"insufficient_quota|credit_balance|no credits remaining|exceeded your current quota|"
                       r"billing_hard_limit|payment required|used up your daily free allocation", re.I)
NO_CREDIT_PAUSE_S = 30 * 60        # then try the provider again, in case you added credit
_no_credit_until: dict[str, float] = {}


_BUDGET: dict | None = None


@contextlib.contextmanager
def token_budget(limit: int):
    """Cap paid tokens for everything inside the block (eval runs, D24). Paid calls past the
    limit raise BudgetExceeded instead of silently falling back to free models."""
    global _BUDGET
    _BUDGET = {"limit": limit, "used": 0}
    try:
        yield _BUDGET
    finally:
        _BUDGET = None


def _over_cap(p: "Provider") -> bool:
    if not p.paid or paid_tokens_today() < daily_cap():
        return False
    day = time.strftime("%Y-%m-%d")
    if day not in _cap_warned:
        _cap_warned.add(day)
        log.warning("daily paid-token cap reached (%d tokens); using free providers until tomorrow",
                    daily_cap())
    return True


# Each task can have its own model list (fallback order), e.g. BRIEF_PROVIDERS=groq,cloudflare.
# Unset, a task uses WRITER_PROVIDERS. An entry may pin a model: "cloudflare:@cf/openai/gpt-oss-120b".
TASK_ENV = {"scout": "SCOUT_PROVIDERS", "ideas": "IDEAS_PROVIDERS", "options": "OPTIONS_PROVIDERS",
            "brief": "BRIEF_PROVIDERS", "post": "POST_PROVIDERS"}


def _int_env(name: str, default: int) -> int:
    try:
        return int(env(name, str(default)) or default)
    except ValueError:
        return default


def providers(role: str = "default") -> list[Provider]:
    """Providers for a job, in fallback order.

    role "default" reads LLM_PROVIDERS; role "writer" reads WRITER_PROVIDERS (falling back to
    LLM_PROVIDERS); the task roles in TASK_ENV read their own list, falling back to the writer list."""
    groq_tpm = _int_env("GROQ_TPM", 8000)
    presets = {
        "openai": Provider(
            "openai", "https://api.openai.com/v1",
            env("OPENAI_MODEL", "gpt-6-sol"), env("OPENAI_API_KEY"),
            strict_schema=True, extra={"reasoning_effort": env("OPENAI_REASONING_EFFORT", "medium")},
            tokens_field="max_completion_tokens", token_scale=3.0, paid=True),
        # Qwen3.8 on Groq: the best free idea writer in the 2026-10-22 bake-off.
        "qwen": Provider(
            "qwen", "https://api.groq.com/openai/v1",
            env("QWEN_MODEL", "qwen/qwen3.8-27b"), env("GROQ_API_KEY"),
            strict_schema=True, extra={}, tpm=groq_tpm),
        "groq": Provider(
            "groq", "https://api.groq.com/openai/v1",
            env("GROQ_MODEL", "openai/gpt-oss-120b"), env("GROQ_API_KEY"),
            strict_schema=True, extra={"reasoning_effort": env("REASONING_EFFORT", "medium")}, tpm=groq_tpm),
        "openrouter": Provider(
            "openrouter", "https://openrouter.ai/api/v1",
            env("OPENROUTER_MODEL", "openai/gpt-oss-120b:free"), env("OPENROUTER_API_KEY"),
            strict_schema=True, extra={}),
        "cloudflare": Provider(
            "cloudflare",
            f"https://api.cloudflare.com/client/v4/accounts/{env('CLOUDFLARE_ACCOUNT_ID')}/ai/v1",
            env("CLOUDFLARE_MODEL", "@cf/openai/gpt-oss-120b"), env("CLOUDFLARE_API_TOKEN"),
            # Its models think before answering; at 1x the JSON was cut off (2026-10-30 bake-off).
            strict_schema=True, extra={}, token_scale=2.5),
        "ollama": Provider(
            "ollama", env("OLLAMA_URL", "http://localhost:11434") + "/v1",
            env("OLLAMA_MODEL"), "ollama",  # only used once OLLAMA_MODEL is set
            strict_schema=True, extra={}),
        # Any other OpenAI-compatible host (DeepSeek, Together, Fireworks, a cloud GPU, ...)
        "custom": Provider(
            "custom", env("CUSTOM_BASE_URL"), env("CUSTOM_MODEL"), env("CUSTOM_API_KEY"),
            strict_schema=env("CUSTOM_STRICT", "false").lower() == "true", extra={}),
    }
    if role == "all":
        return list(presets.values())
    default_order = env("LLM_PROVIDERS", "groq,ollama")
    writer_order = env("WRITER_PROVIDERS", default_order)
    if role in TASK_ENV:                     # a task's own list, else the writer list
        order_text = env(TASK_ENV[role], writer_order)
    else:
        order_text = writer_order if role == "writer" else default_order
    out = []
    for item in (x.strip() for x in order_text.split(",")):
        name, _, model = item.partition(":")        # "cloudflare:@cf/nvidia/nemotron-3-120b-a12b"
        p = presets.get(name)
        if p and p.base_url and (model or p.model) and p.api_key:
            out.append(dataclasses.replace(p, model=model) if model else p)
    return out


# Tasks whose answer is mostly copying and tidying: low reasoning effort saves tokens per minute.
# Only used when the caller passed no effort and the provider takes reasoning_effort.
TASK_EFFORT = {"options": "low", "brief": "low"}

MIN_PASS_SAMPLES = 5            # fewer results than this and a model keeps its configured place
MIN_PASS_RATE = 0.5             # a model below this (with enough samples) goes to the end of the list


def ordered(plist: list[Provider], role: str) -> list[Provider]:
    """Keep the configured order, but move a model that keeps failing to the end (PROVIDER_ORDER=config turns it off).

    A pass rate only says "returned valid JSON", not "wrote well", so it can demote but never promote: the user's
    order encodes quality judgements (e.g. Gemma first) that a pass rate cannot see. Stats are per provider+model,
    so one broken model does not drag down a healthy model on the same provider. Paid providers never move."""
    if env("PROVIDER_ORDER", "results").lower() == "config" or len(plist) < 2:
        return plist
    try:
        rates = db.llm_pass_rates(7)

        def failing(p: Provider) -> bool:
            good, total = rates.get((p.name, p.model, role), (0, 0))
            return not p.paid and total >= MIN_PASS_SAMPLES and good / total < MIN_PASS_RATE

        # Stable partition: relative order is kept among the healthy and among the demoted.
        return [p for p in plist if not failing(p)] + [p for p in plist if failing(p)]
    except Exception as e:
        log.warning("provider ordering skipped: %s", str(e)[:200])
        return plist


def available() -> bool:
    return bool(providers())


# ---------- schema helpers ----------

def strict_schema(model: type[BaseModel]) -> dict:
    """Pydantic schema -> the strict form most providers want: refs inlined, every field
    required, no extra properties, no titles."""
    raw = model.model_json_schema()
    defs = raw.pop("$defs", {})

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(copy.deepcopy(defs[node["$ref"].split("/")[-1]]))
            # Drop schema "title" labels, but keep a real field that happens to be named "title".
            node = {k: (walk(v) if k != "properties" else {pk: walk(pv) for pk, pv in v.items()})
                    for k, v in node.items() if not (k == "title" and isinstance(v, str))}
            if node.get("type") == "object" and "properties" in node:
                node["required"] = list(node["properties"])
                node["additionalProperties"] = False
            return node
        if isinstance(node, list):
            return [walk(x) for x in node]
        return node

    return walk(raw)


def _extract_json(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()   # reasoning models
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, flags=re.S)
    if m:
        return m.group(1)
    start, end = text.find("{"), text.rfind("}")
    return text[start:end + 1] if start != -1 else text


# ---------- the call ----------

TPM_MARGIN = 300          # headroom for the estimate being a little off
MIN_OUTPUT_TOKENS = 2500  # below this room the answer would be cut off: do not even try


def _estimate_tokens(*texts: str) -> int:
    """Rough input size: ~3.5 chars per token, plus 10% so we err on the big side."""
    return int(sum(len(t) for t in texts) / 3.5 * 1.1) + 1


def _used(usage: dict) -> int:
    return (usage.get("prompt_tokens") or 0) + (usage.get("completion_tokens") or 0)


def _reason(e: Exception) -> str:
    """A short class for a failed attempt, for the router stats."""
    msg = str(e)
    if "cut off" in msg:
        return "cut_off"
    if "HTTP 413" in msg or HOPELESS.search(msg):
        return "too_big"
    m = re.search(r"HTTP (\d{3})", msg)
    return f"http_{m.group(1)}" if m else "error"

HOPELESS = re.compile(r"request too large|context[_ ]length|maximum context|too many tokens|"
                      r"reduce the length|prompt is too long|payload too large", re.I)


def probe(name: str) -> str:
    """One tiny strict-JSON request to a provider: "ok", or why it failed. `postpilot doctor`
    runs it on every backup in WRITER_PROVIDERS (eng review D21)."""
    p = next((x for x in providers("all") if x.name == name), None)
    if not p or not (p.base_url and p.model and p.api_key):
        return "not configured"
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"],
              "additionalProperties": False}
    body = {"model": p.model, p.tokens_field: 400,
            "messages": [{"role": "user", "content": 'Reply with the JSON {"ok": true}.'}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "Probe", "strict": True,
                                                                      "schema": schema}}}
    try:
        resp = _post(p, body, timeout=120)
        text = resp["choices"][0]["message"].get("content") or ""
        return "ok" if json.loads(_extract_json(text)).get("ok") is True else f"unexpected reply: {text[:80]}"
    except Exception as e:
        return f"failed: {str(e)[:160]}"


# ---------- per-model live probe: a model is only recommended after it answered a real call ----------

PROBE_OK_S = 12 * 3600       # a model that answered stays verified this long
PROBE_FAIL_S = 3600          # a failure (404, no credit, 429, 5xx, timeout) is re-tested after this
PROBE_TIMEOUT_S = 45


def probe_model(provider: str, model: str, now: float | None = None) -> tuple[bool, str]:
    """Send one tiny real chat request to exactly this provider + model: (True, "ok") or (False, short reason).

    Being listed as free in a catalog proves nothing (a listed model can 404, need credit, or be
    rate-limited), so only an answered call counts as verified. Anything uncertain (429, 5xx,
    timeout, no credit) is "not verified". Results are cached in the meta table. Never raises."""
    now = time.time() if now is None else now
    key = f"probe:{provider}:{model}"
    try:
        hit = json.loads(db.get_meta(key, "") or "null")
        if isinstance(hit, dict):
            ttl = PROBE_OK_S if hit.get("ok") else PROBE_FAIL_S
            if 0 <= now - float(hit["at"]) <= ttl:
                return bool(hit["ok"]), str(hit.get("why", ""))
    except Exception:
        pass                                    # a broken cache entry just means "probe again"
    ok, why = _probe_call(provider, model)
    try:
        db.set_meta(key, json.dumps({"at": now, "ok": ok, "why": why}))
    except Exception as e:
        log.warning("could not cache probe result for %s:%s: %s", provider, model, e)
    return ok, why


def _probe_call(provider: str, model: str) -> tuple[bool, str]:
    try:
        preset = next((x for x in providers("all") if x.name == provider), None)
        if not preset or not (preset.base_url and preset.api_key):
            return False, f"no API key configured for {provider}"
        p = dataclasses.replace(preset, model=model)
        body = {"model": model, p.tokens_field: 64,
                "messages": [{"role": "user", "content": "Reply with the single word: ok"}]}
        req = urllib.request.Request(
            p.base_url.rstrip("/") + "/chat/completions", data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {p.api_key}",
                     "User-Agent": "postpilot/0.4"})
        # One attempt, no retry/sleep loop like _post: a probe must be quick and a busy model is "unknown".
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT_S) as r:
            resp = json.loads(r.read().decode("utf-8"))
        msg = (resp.get("choices") or [{}])[0].get("message") or {}
        # Reasoning models may spend all 64 tokens thinking: a reasoning field still proves the model ran.
        if (msg.get("content") or msg.get("reasoning") or msg.get("reasoning_content") or "").strip():
            return True, "ok"
        return False, "empty reply"
    except urllib.error.HTTPError as e:
        detail = ""
        with contextlib.suppress(Exception):
            detail = e.read().decode("utf-8", "replace")
        if NO_CREDIT.search(detail):
            return False, f"HTTP {e.code}, no credit"
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:80]}"


def _post(p: Provider, body: dict, timeout: int = 600, patient: bool = True) -> dict:
    """One chat request. A busy provider is retried for a while only when `patient`; with another
    provider waiting behind it, one short retry is enough (a real run lost 4 minutes to retries)."""
    req = urllib.request.Request(
        p.base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {p.api_key}",
                 "User-Agent": "postpilot/0.4"},
    )
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            # A request that is too big will fail no matter how long we wait: move on at once
            # instead of retrying for minutes (eng review D7). Busy/rate-limit errors still retry.
            if e.code in (402, 429) and NO_CREDIT.search(detail):
                raise NoCredit(f"{p.name} has no credit left (add credit, then it is used again): {detail[:200]}") from None
            if e.code == 413 or HOPELESS.search(detail):
                raise RuntimeError(f"{p.name} HTTP {e.code} (not retried): {detail}") from None
            if e.code in (429, 500, 502, 503) and attempt < (3 if patient else 1):
                wait = float(e.headers.get("retry-after") or 0) or 20 * (attempt + 1)
                log.warning("%s HTTP %s, retrying in %.0fs: %s", p.name, e.code, wait, detail[:150])
                time.sleep(min(wait, 120 if patient else 30))
                continue
            raise RuntimeError(f"{p.name} HTTP {e.code}: {detail}") from None
    raise RuntimeError(f"{p.name}: gave up after retries")


def chat_json(system: str, user: str, schema: type[BaseModel], max_tokens: int = 4000,
              effort: str | None = None, json_schema: dict | None = None, role: str = "default"):
    """Ask for JSON matching `schema`. Returns a validated instance."""
    plist = ordered(providers(role), role)
    if not plist:
        raise RuntimeError("No model provider configured. Set GROQ_API_KEY (or another provider) in .env.")
    js = json_schema or strict_schema(schema)   # custom schema, e.g. with an enum of model ids
    errors = []
    effort = effort or TASK_EFFORT.get(role)
    # Inside a token budget (the eval) only paid writers run: a silent fall-through to a free
    # model would score the wrong model.
    if _BUDGET is not None and any(p.paid for p in plist):
        plist = [p for p in plist if p.paid]
    for p in plist:
        if _over_cap(p):
            errors.append(f"{p.name}: daily cap reached")
            continue
        if time.time() < _no_credit_until.get(p.name, 0):       # known empty: go straight to the next one
            errors.append(f"{p.name}: no credit")
            continue
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        for attempt in range(2):
            if p.paid and _BUDGET is not None and _BUDGET["used"] >= _BUDGET["limit"]:
                raise BudgetExceeded(f"token budget of {_BUDGET['limit']:,} reached")
            sent = int(max_tokens * p.token_scale)
            if p.tpm > 0:
                # The provider counts input + reserved output against its per-minute limit, so shrink the
                # reservation to fit; never raise it. No room for a usable answer: skip with no network call.
                room = p.tpm - TPM_MARGIN - _estimate_tokens(*(m["content"] for m in messages), json.dumps(js))
                if room < min(sent, MIN_OUTPUT_TOKENS):
                    log.warning("%s: request too big for its per-minute limit (%d tpm, ~%d room)", p.name, p.tpm, room)
                    errors.append(f"{p.name}: request too big for its per-minute limit")
                    db.record_llm_call(p.name, p.model, role, False, "too_big")
                    break
                sent = min(sent, room)
            body = {"model": p.model, "messages": messages, p.tokens_field: sent, **p.extra}
            if effort and "reasoning_effort" in body:
                body["reasoning_effort"] = effort
            if p.strict_schema:
                body["response_format"] = {"type": "json_schema", "json_schema": {
                    "name": schema.__name__, "strict": True, "schema": js}}
            else:
                body["response_format"] = {"type": "json_object"}
                body["messages"] = [*messages[:-1], {"role": "user", "content": messages[-1]["content"] + "\n\nReply with only a JSON object matching this schema:\n" + json.dumps(js)}]
            t0 = time.time()
            usage: dict = {}
            try:
                resp = _post(p, body, **({} if p is plist[-1] else {"patient": False}))
                # Count tokens for every answered call, valid or not: a bad answer still costs.
                usage = resp.get("usage", {}) or {}
                tally = USAGE.setdefault(f"{p.name}:{p.model}", {"calls": 0, "in": 0, "out": 0})
                tally["calls"] += 1
                tally["in"] += usage.get("prompt_tokens") or 0
                tally["out"] += usage.get("completion_tokens") or 0
                record_usage(p.name, p.model, usage.get("prompt_tokens") or 0,    # free-tier quota (never raises)
                                    usage.get("completion_tokens") or 0)
                if p.paid:
                    spent = (usage.get("prompt_tokens") or 0) + (usage.get("completion_tokens") or 0)
                    _add_paid_tokens(spent)
                    if _BUDGET is not None:
                        _BUDGET["used"] += spent
                choice = resp["choices"][0]
                if choice.get("finish_reason") == "length":
                    raise RuntimeError("output cut off (max_tokens)")
                text = choice["message"].get("content") or ""
                result = schema.model_validate_json(_extract_json(text))
                log.info("%s/%s ok (%s in, %s out tokens)", p.name, p.model,
                         usage.get("prompt_tokens"), usage.get("completion_tokens"))
                db.record_llm_call(p.name, p.model, role, True, "", time.time() - t0, _used(usage))
                return result
            except (ValidationError, json.JSONDecodeError) as e:
                log.warning("%s returned invalid JSON (attempt %d): %s", p.name, attempt + 1, str(e)[:300])
                errors.append(f"{p.name}: invalid JSON")
                db.record_llm_call(p.name, p.model, role, False, "invalid_json", time.time() - t0, _used(usage))
                messages = [*messages, {"role": "assistant", "content": text[:2000]}, {"role": "user", "content": f"That did not match the schema: {str(e)[:500]}. " "Send the corrected JSON only."}]
            except NoCredit as e:
                _no_credit_until[p.name] = time.time() + NO_CREDIT_PAUSE_S
                mark_used_up(p.name)         # the provider's word beats our own count
                log.warning("%s: no credit left, using the next provider for the next %d min. %s",
                            p.name, NO_CREDIT_PAUSE_S // 60, str(e)[:200])
                errors.append(f"{p.name}: no credit")
                db.record_llm_call(p.name, p.model, role, False, "no_credit", time.time() - t0)
                break
            except Exception as e:
                log.warning("%s failed: %s", p.name, str(e)[:300])
                errors.append(f"{p.name}: {str(e)[:200]}")
                db.record_llm_call(p.name, p.model, role, False, _reason(e), time.time() - t0, _used(usage))
                break  # move on to the next provider
    raise RuntimeError("All model providers failed: " + " | ".join(errors))


# ---------- free-quota tracking and the health line (was health.py) ----------
# Free tiers run out silently (Cloudflare: 10,000 neurons per UTC day, Groq: ~200K tokens per day).
# chat_json adds every answered call here; `status` and the scout log read it back. Pure functions over
# the db meta table; `now` is a parameter so tests need no real clock. Nothing here may raise into a real
# model call or an email send.

health_log = logging.getLogger("postpilot.health")      # the log name people already grep for

QUOTA_LOW = 0.8      # "running low" from here; the line gets a warning sign too

# Cloudflare bills Workers AI in "neurons", per model: neurons per 1M tokens (in, out).
NEURON_RATES: dict[str, tuple[int, int]] = {
    "@cf/openai/gpt-oss-120b": (31818, 68182),
    "@cf/nvidia/nemotron-3-120b-a12b": (45455, 136364),
}
DEFAULT_RATE = NEURON_RATES["@cf/openai/gpt-oss-120b"]      # a model not in the table: a guess, flagged as one

# provider name -> (tracked service, display name, unit, env var for the daily budget, default budget)
# "qwen" is Qwen on Groq, so it shares Groq's quota.
QUOTA_SERVICES = {"cloudflare": ("cloudflare", "Cloudflare", "neurons", "CLOUDFLARE_DAILY_NEURONS", 10000),
            "groq": ("groq", "Groq", "tokens", "GROQ_DAILY_TOKENS", 200000),
            "qwen": ("groq", "Groq", "tokens", "GROQ_DAILY_TOKENS", 200000)}
QUOTA_ORDER = ("cloudflare", "groq")


def _quota_day(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%d")      # both free tiers reset at 00:00 UTC


def _quota_key(service: str, now: datetime | None = None) -> str:
    return f"quota:{service}:{_quota_day(now)}"


def _quota_approx_key(service: str, now: datetime | None = None) -> str:
    return f"quota_approx:{service}:{_quota_day(now)}"


def quota_budget(service: str) -> int:
    for svc, _name, _unit, var, default in QUOTA_SERVICES.values():
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
        spec = QUOTA_SERVICES.get(provider)
        if not spec:
            return
        service = spec[0]
        if service == "cloudflare":
            used, guess = neurons(model, prompt_tokens, completion_tokens)
            if guess:
                db.set_meta(_quota_approx_key(service, now), "1")
        else:
            used = prompt_tokens + completion_tokens
        if used > 0:
            db.add_meta_int(_quota_key(service, now), used)
            _warn_once(service, now)
    except Exception as e:
        health_log.warning("could not track quota use: %s", str(e)[:200])


def _warn_once(service: str, now: datetime | None = None) -> None:
    """WARNING the first time today's use crosses 80%, and again at 100%. The level is remembered in meta
    so a busy day does not log the same warning on every call."""
    total = int(db.get_meta(_quota_key(service, now), "0") or 0)
    cap = quota_budget(service)
    level = 100 if total >= cap else 80 if total >= QUOTA_LOW * cap else 0
    if not level:
        return
    wkey = f"quota_warn:{service}:{_quota_day(now)}"
    if int(db.get_meta(wkey, "0") or 0) >= level:
        return
    db.set_meta(wkey, str(level))
    name = next(s[1] for s in QUOTA_SERVICES.values() if s[0] == service)
    health_log.warning("%s free quota %s for today (%s)", name, "used up" if level == 100 else "running low (80%+)",
                "; ".join(line for line in quota_lines(now) if line.startswith(name)))


def mark_used_up(provider: str, now: datetime | None = None) -> None:
    """The provider said today's free allocation is gone: show 100% whatever our own count says. Never raises."""
    try:
        spec = QUOTA_SERVICES.get(provider)
        if not spec:
            return
        service = spec[0]
        used = int(db.get_meta(_quota_key(service, now), "0") or 0)
        db.set_meta(_quota_key(service, now), str(max(used, quota_budget(service))))
        _warn_once(service, now)
    except Exception as e:
        health_log.warning("could not mark quota used up: %s", str(e)[:200])


def _quota_usage(now: datetime | None = None) -> list[tuple[str, str, int, int, bool]]:
    """(display name, unit, used today, budget, approximate) for each tracked service with any use today."""
    out = []
    for service in QUOTA_ORDER:
        spec = next(s for s in QUOTA_SERVICES.values() if s[0] == service)
        used = int(db.get_meta(_quota_key(service, now), "0") or 0)
        if used <= 0:
            continue
        approx = service == "cloudflare" and db.get_meta(_quota_approx_key(service, now)) == "1"
        out.append((spec[1], spec[2], used, quota_budget(service), approx))
    return out


def _quota_pct(used: int, cap: int) -> int:
    return int(used * 100 / cap)        # rounds down, so "100%" only appears when it is really used up


def quota_lines(now: datetime | None = None) -> list[str]:
    """One line per provider used today, e.g. "Cloudflare 62% of today's free quota (~6,200/10,000 neurons)"."""
    try:
        lines = []
        for name, unit, used, cap, approx in _quota_usage(now):
            tilde = "~" if name == "Cloudflare" else ""        # neurons are always an estimate
            detail = f"({tilde}{used:,}/{cap:,} {unit}{', approx.' if approx else ''})"
            if used >= cap:
                lines.append(f"{name} used up for today {detail}")
                continue
            line = f"{name} {_quota_pct(used, cap)}% of today's free quota {detail}"
            lines.append(line + (", running low" if used >= QUOTA_LOW * cap else ""))
        return lines
    except Exception as e:
        health_log.warning("could not read quota: %s", str(e)[:200])
        return []


def model_health(days: int = 1) -> str:
    """'models ok 41/44': model calls that gave usable output today (UTC), or '' with no calls."""
    try:
        rates = db.llm_pass_rates(days)
        good = sum(g for g, _ in rates.values())
        total = sum(t for _, t in rates.values())
        return f"models ok {good}/{total}" if total else ""
    except Exception as e:
        health_log.warning("could not read model health: %s", str(e)[:200])
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
            usage = _quota_usage(now)
        except Exception as e:          # a broken quota read must not hide the rest of the line
            health_log.warning("could not read quota: %s", str(e)[:200])
            usage = []
        for i, (name, _unit, used, cap, _approx) in enumerate(usage):
            warn = warn or used >= QUOTA_LOW * cap
            if used >= cap:
                parts.append(f"{name} used up")
            else:
                parts.append(f"{name} {_quota_pct(used, cap)}%" + (" of today's quota" if i == 0 else ""))
        if mh := model_health():
            parts.append(mh)
        while len(parts) > 1 and len("Health: " + " · ".join(parts)) > 150:      # the line is a glance, not a report
            parts.pop()
        if not parts:
            return ""
        return ("⚠ " if warn else "") + "Health: " + " · ".join(parts)
    except Exception as e:
        health_log.warning("could not build the health line: %s", str(e)[:200])
        return ""


def log_health(scout: dict | None = None) -> None:
    """Log the health line after a scout: WARNING when a quota is at 80%+ or the scout found no ideas, else INFO."""
    line = health_line(scout)
    if not line:
        return
    empty = scout is not None and not int(scout.get("pitched") or 0)
    (health_log.warning if line.startswith("⚠") or empty else health_log.info)("%s", line)
