"""Open-model client. Any OpenAI-compatible endpoint works: Groq, OpenRouter, Ollama (local).

Providers are tried in the order of LLM_PROVIDERS; if one fails (rate limit, outage, bad JSON
twice) the next one gets the same request. Output is validated against a Pydantic model.
"""

import contextlib
import copy
import json
import logging
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from pydantic import BaseModel, ValidationError

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


def providers(role: str = "default") -> list[Provider]:
    """Providers for a job, in fallback order.

    role "default" (topic finding etc.) reads LLM_PROVIDERS; role "writer" (ideas, posts, code)
    reads WRITER_PROVIDERS, falling back to LLM_PROVIDERS when that is not set."""
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
            strict_schema=True, extra={}),
        "groq": Provider(
            "groq", "https://api.groq.com/openai/v1",
            env("GROQ_MODEL", "openai/gpt-oss-120b"), env("GROQ_API_KEY"),
            strict_schema=True, extra={"reasoning_effort": env("REASONING_EFFORT", "medium")}),
        "openrouter": Provider(
            "openrouter", "https://openrouter.ai/api/v1",
            env("OPENROUTER_MODEL", "openai/gpt-oss-120b:free"), env("OPENROUTER_API_KEY"),
            strict_schema=True, extra={}),
        "cloudflare": Provider(
            "cloudflare",
            f"https://api.cloudflare.com/client/v4/accounts/{env('CLOUDFLARE_ACCOUNT_ID')}/ai/v1",
            env("CLOUDFLARE_MODEL", "@cf/openai/gpt-oss-120b"), env("CLOUDFLARE_API_TOKEN"),
            strict_schema=True, extra={}),
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
    order_text = env("WRITER_PROVIDERS", default_order) if role == "writer" else default_order
    order = [p.strip() for p in order_text.split(",") if p.strip()]
    out = []
    for name in order:
        p = presets.get(name)
        if p and p.base_url and p.model and p.api_key:
            out.append(p)
    return out


def providers_all() -> list[Provider]:
    """Every known provider preset, configured or not (for probes and diagnostics)."""
    return providers("all")


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

HOPELESS = re.compile(r"request too large|context[_ ]length|maximum context|too many tokens|"
                      r"reduce the length|prompt is too long|payload too large", re.I)


def probe(name: str) -> str:
    """One tiny strict-JSON request to a provider: "ok", or why it failed. `postpilot doctor`
    runs it on every backup in WRITER_PROVIDERS (eng review D21)."""
    p = next((x for x in providers_all() if x.name == name), None)
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


def _post(p: Provider, body: dict, timeout: int = 600) -> dict:
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
            if e.code == 413 or HOPELESS.search(detail):
                raise RuntimeError(f"{p.name} HTTP {e.code} (not retried): {detail}") from None
            if e.code in (429, 500, 502, 503) and attempt < 3:
                wait = float(e.headers.get("retry-after") or 0) or 20 * (attempt + 1)
                log.warning("%s HTTP %s, retrying in %.0fs: %s", p.name, e.code, wait, detail[:150])
                time.sleep(min(wait, 120))
                continue
            raise RuntimeError(f"{p.name} HTTP {e.code}: {detail}") from None
    raise RuntimeError(f"{p.name}: gave up after retries")


def chat_json(system: str, user: str, schema: type[BaseModel], max_tokens: int = 4000,
              effort: str | None = None, json_schema: dict | None = None, role: str = "default"):
    """Ask for JSON matching `schema`. Returns a validated instance."""
    plist = providers(role)
    if not plist:
        raise RuntimeError("No model provider configured. Set GROQ_API_KEY (or another provider) in .env.")
    js = json_schema or strict_schema(schema)   # custom schema, e.g. with an enum of model ids
    errors = []
    # Inside a token budget (the eval) only paid writers run: a silent fall-through to a free
    # model would score the wrong model.
    if _BUDGET is not None and any(p.paid for p in plist):
        plist = [p for p in plist if p.paid]
    for p in plist:
        if _over_cap(p):
            errors.append(f"{p.name}: daily cap reached")
            continue
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        for attempt in range(2):
            if p.paid and _BUDGET is not None and _BUDGET["used"] >= _BUDGET["limit"]:
                raise BudgetExceeded(f"token budget of {_BUDGET['limit']:,} reached")
            body = {"model": p.model, "messages": messages,
                    p.tokens_field: int(max_tokens * p.token_scale), **p.extra}
            if effort and "reasoning_effort" in body:
                body["reasoning_effort"] = effort
            if p.strict_schema:
                body["response_format"] = {"type": "json_schema", "json_schema": {
                    "name": schema.__name__, "strict": True, "schema": js}}
            else:
                body["response_format"] = {"type": "json_object"}
                body["messages"] = messages[:-1] + [{"role": "user", "content": (
                    messages[-1]["content"] + "\n\nReply with only a JSON object matching this schema:\n"
                    + json.dumps(js))}]
            try:
                resp = _post(p, body)
                # Count tokens for every answered call, valid or not: a bad answer still costs.
                usage = resp.get("usage", {}) or {}
                tally = USAGE.setdefault(f"{p.name}:{p.model}", {"calls": 0, "in": 0, "out": 0})
                tally["calls"] += 1
                tally["in"] += usage.get("prompt_tokens") or 0
                tally["out"] += usage.get("completion_tokens") or 0
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
                return result
            except (ValidationError, json.JSONDecodeError) as e:
                log.warning("%s returned invalid JSON (attempt %d): %s", p.name, attempt + 1, str(e)[:300])
                errors.append(f"{p.name}: invalid JSON")
                messages = messages + [
                    {"role": "assistant", "content": text[:2000]},
                    {"role": "user", "content": f"That did not match the schema: {str(e)[:500]}. "
                                                "Send the corrected JSON only."}]
            except Exception as e:
                log.warning("%s failed: %s", p.name, str(e)[:300])
                errors.append(f"{p.name}: {str(e)[:200]}")
                break  # move on to the next provider
    raise RuntimeError("All model providers failed: " + " | ".join(errors))
