"""Fact gate: check ideas and posts against live, free model catalogs and the scouted evidence.

Pipeline (see docs/designs/postpilot-content-engine.md, Stage 1 + eng review decisions):

  load_catalogs()   Groq /models, Cloudflare /ai/models/search (paged), OpenRouter :free
                    -> chat models only, outdated families hidden (D7), 7-day cache in SQLite
  check_idea()      models_to_run must be real catalog ids (D13), image/video builds need
                    vision models, model names in the text must be covered or are flagged,
                    money / percent / company-claim sentences are checked against signal
                    titles (D14)
  scan_text()       the same scans for a finished post (D12)
  filter_links()    keep only sources whose URL or title names the topic

Every network call goes through `fetch` (default sources._get) so tests run offline.
"""

import json
import logging
import re
import time
import urllib.error
import urllib.parse
from dataclasses import dataclass, field

from . import db, sources
from .config import env

log = logging.getLogger("postpilot.factgate")
CACHE_DAYS = 7

# ---------- names ----------

# Old model names. In posts they look dated; in free catalogs they are hidden (D7).
OUTDATED = re.compile(
    r"\b(GPT-?3\.5|GPT-?4o?|GPT-?4\.1|Llama[- ]?[23](?:\.\d)?|Mixtral|Mistral[- ]7B|"
    r"Claude[- ]?[23](?:\.\d)?|Gemini[- ]?1\.5|Gemini[- ]?2(?:\.\d)?|PaLM|Falcon)\b", re.I)
OUTDATED_ID = re.compile(
    r"^(gpt-3-5|gpt-4|llama-[23]\b|llama-[23]-|mixtral|mistral-7b|claude-[23]\b|gemini-1-5|"
    r"gemini-2\b|gemini-2-|palm|falcon|gemma-[27]b|gemma-2-)")

# Model families the mention scan understands. Catalog and Hugging Face names add more.
BASE_FAMILIES = ["gpt", "claude", "gemini", "llama", "qwen", "deepseek", "mistral", "mixtral",
                 "grok", "muse", "phi", "gemma", "kimi", "glm", "nemotron", "minicpm", "flux",
                 "ltx", "jev", "gpt-oss", "olmo", "granite", "hermes"]
VENDORS = {"openai", "meta", "meta-llama", "google", "qwen", "deepseek-ai", "mistralai",
           "nvidia", "moonshotai", "zai-org", "ibm-granite", "microsoft", "anthropic", "cf"}
SUFFIXES = ("-instruct", "-chat", "-versatile", "-instant", "-free", "-preview", "-latest")
NON_CHAT = re.compile(r"whisper|guard|orpheus|tts|embed|safeguard|rerank|-lora\b|lora$|clip|ocr|"
                      r"stable-diffusion|flux|sdxl|melotts|nova-3|aura|bge|m2m100|distilbert",
                      re.I)
STOP = {"ai", "model", "models", "open", "new", "launch", "launches", "the", "and", "for",
        "with", "its", "from", "into", "this", "that", "over", "after"}
EVENT_WORDS = re.compile(r"\b(raised|raises|valuation|valued|funding|filed|filing|lawsuit|sued|"
                         r"sues|banned|bans|acquired|acquires|acquisition|launched|launches|"
                         r"released|releases|ipo|layoffs?|fined)\b", re.I)
# A build needs an image-capable model only when it FEEDS images/video to the model, not when
# it just takes a screenshot of its own results.
VISION_NEED = re.compile(
    r"\b(send|sends|feed|feeds|upload|uploads|pass|passes|give|gives|input)\b"
    r"[^.\n]{0,60}\b(image|images|photo|photos|picture|pictures|screenshot|screenshots|video|videos|"
    r"scan|scans)\b[^.\n]{0,40}\b(to|into)\b"
    r"|\b(caption|captions|captioning|ocr|describe the image|vision model|image input|"
    r"multimodal input)\b", re.I)


def plain(text: str) -> str:
    """Fancy hyphens and spaces -> plain ones, so name checks can't be dodged by typography."""
    text = re.sub(r"[    ]", " ", text or "")
    return re.sub(r"[‐-―−]", "-", text)


def normalize(name: str) -> str:
    """'groq:openai/gpt-oss-120b' or 'GPT-OSS 120B' -> 'gpt-oss-120b'."""
    s = name.strip().lower()
    if ":" in s and s.split(":", 1)[0] in {"groq", "cloudflare", "openrouter"}:
        s = s.split(":", 1)[1]
    s = s.removesuffix(":free")
    s = s.split("/")[-1]
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    changed = True
    while changed:
        changed = False
        for suf in SUFFIXES:
            if s.endswith(suf):
                s, changed = s[: -len(suf)], True
    return s


def family_of(norm_id: str) -> str:
    """'qwen3-8-27b' -> 'qwen', 'gpt-oss-120b' -> 'gpt-oss'."""
    m = re.match(r"([a-z]+(?:-oss)?)", norm_id)
    return m.group(1) if m else ""


def is_outdated_id(norm_id: str) -> bool:
    return bool(OUTDATED_ID.match(norm_id))


# ---------- catalogs ----------

@dataclass
class Model:
    provider: str
    id: str                      # exact id to call the API with
    vision: bool | None = None   # True / False / None = unknown
    paid: bool = False           # costs money per call (OpenRouter paid, OpenAI)
    price_in: float | None = None    # USD per 1M input tokens, when known
    price_out: float | None = None   # USD per 1M output tokens, when known
    new: bool = False            # launched in the last NEW_DAYS days (a candidate "hot model")

    @property
    def key(self) -> str:        # what the idea writer chooses from
        return f"{self.provider}:{self.id}"

    @property
    def norm(self) -> str:
        return normalize(self.id)


@dataclass
class Catalog:
    models: list[Model] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)   # providers with no data at all
    from_cache: list[str] = field(default_factory=list)

    def by_key(self) -> dict[str, Model]:
        return {m.key: m for m in self.models}

    def to_json(self) -> dict:
        return {"models": [m.__dict__ for m in self.models], "unavailable": self.unavailable}

    @staticmethod
    def from_json(d: dict) -> "Catalog":
        return Catalog([Model(**m) for m in d.get("models", [])], d.get("unavailable", []))


def _json(fetch, url, headers=None):
    return json.loads(fetch(url, headers or {}))


def fetch_groq(fetch, key: str) -> list[Model]:
    data = _json(fetch, "https://api.groq.com/openai/v1/models", {"Authorization": f"Bearer {key}"})
    return [Model("groq", m["id"], None) for m in data.get("data", [])
            if not NON_CHAT.search(m["id"]) and m.get("active", True)]


def fetch_cloudflare(fetch, account: str, token: str) -> list[Model]:
    out, page, seen = [], 1, 0
    while True:
        url = (f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/models/search?"
               + urllib.parse.urlencode({"task": "Text Generation", "per_page": 100, "page": page}))
        data = _json(fetch, url, {"Authorization": f"Bearer {token}"})
        results = data.get("result") or []
        for m in results:
            task = (m.get("task") or {}).get("name", "Text Generation")
            name = m.get("name", "")
            props = {p.get("property_id"): p.get("value") for p in m.get("properties") or []}
            # Listed but not callable on the Workers Free plan (e.g. Kimi K2.6, GLM-5.3).
            if str(props.get("require_workers_paid", "")).lower() == "true":
                continue
            if task != "Text Generation" or not name or NON_CHAT.search(name):
                continue
            vision = True if re.search(r"vision|vl\b|-vl-|omni|llava|scout|gemma-[34]", name) else None
            out.append(Model("cloudflare", name, vision))
        info = data.get("result_info") or {}
        seen += len(results)                    # count what actually arrived, not page sizes
        if not results or seen >= info.get("total_count", seen) or page >= 50:
            return out
        page += 1


NEW_DAYS = 60
# The models a "new model vs the top models" face-off compares against (paid, via OpenRouter).
FRONTIER = ("anthropic/claude-sonnet-5.5", "anthropic/claude-haiku-5.5", "google/gemini-4-argon",
            "openai/gpt-6-sol", "openai/gpt-6-luna", "x-ai/grok-4.7", "deepseek/deepseek-v4.1-flash",
            "moonshotai/kimi-k2.6", "z-ai/glm-5.3", "meta/muse-spark-open", "qwen/qwen3.8-max-0902")


def fetch_openrouter(fetch, include_paid: bool = True, now: float | None = None) -> list[Model]:
    """Free models always; paid ones only if they are frontier rivals or launched recently,
    so a face-off can include the hot new model and the current top models."""
    data = _json(fetch, "https://openrouter.ai/api/v1/models")
    now = time.time() if now is None else now
    frontier = set(env("FRONTIER_MODELS", ",".join(FRONTIER)).split(","))
    out = []
    for m in data.get("data", []):
        mid = m["id"]
        arch = m.get("architecture") or {}
        outputs = arch.get("output_modalities") or ["text"]
        inputs = arch.get("input_modalities") or ["text"]
        if "text" not in outputs or NON_CHAT.search(mid) or mid.startswith("~") or mid.endswith(":batch"):
            continue
        vision = "image" in inputs or "video" in inputs
        new = bool(m.get("created")) and now - m["created"] <= NEW_DAYS * 86400
        if mid.endswith(":free"):
            out.append(Model("openrouter", mid, vision, new=new))
            continue
        if not include_paid or not (mid in frontier or new):
            continue
        pr = m.get("pricing") or {}
        try:
            pin, pout = float(pr.get("prompt", 0)) * 1e6, float(pr.get("completion", 0)) * 1e6
        except (TypeError, ValueError):
            pin = pout = None
        if pin is not None and (pin < 0 or pout < 0):
            continue                     # routers with "variable" pricing: not a model you can test
        out.append(Model("openrouter", mid, vision, paid=True, price_in=pin, price_out=pout, new=new))
    return out


def fetch_openai(fetch, key: str) -> list[Model]:
    """Your own OpenAI account (paid): the current GPT-6 family."""
    data = _json(fetch, "https://api.openai.com/v1/models", {"Authorization": f"Bearer {key}"})
    ids = [m["id"] for m in data.get("data", [])]
    keep = [i for i in ids if re.fullmatch(r"gpt-6(\.\d)?-(sol|luna|astra)", i)]
    return [Model("openai", i, True, paid=True) for i in sorted(keep)]


def openrouter_balance(fetch=None) -> float | None:
    """Remaining OpenRouter credit in USD, or None if unknown."""
    key = env("OPENROUTER_API_KEY")
    if not key:
        return None
    try:
        d = json.loads((fetch or sources._get)("https://openrouter.ai/api/v1/credits",
                                               {"Authorization": f"Bearer {key}"}))["data"]
        return float(d.get("total_credits", 0)) - float(d.get("total_usage", 0))
    except Exception:
        return None


def load_catalogs(fetch=None, use_cache=True, now=None) -> Catalog:
    """Fetch every configured free provider. A failed fetch uses the 7-day cache."""
    fetch = fetch or sources._get
    now = time.time() if now is None else now
    cache = json.loads(db.get_meta("catalog_cache", "{}") or "{}") if use_cache else {}
    paid_ok = env("PAID_EXPERIMENTS", "on").lower() != "off"
    # OpenRouter paid models only when there is credit to call them (or forced with =force).
    bal = openrouter_balance(fetch) if paid_ok else None
    or_paid = env("PAID_EXPERIMENTS", "on").lower() == "force" or (bal is not None and bal > 0)
    # Paid OpenRouter models are always read (their prices price your OpenAI models too), then
    # removed below when there is no credit to call them.
    jobs = {"openrouter": lambda: fetch_openrouter(fetch, include_paid=paid_ok)}
    if paid_ok and env("OPENAI_API_KEY"):
        jobs["openai"] = lambda: fetch_openai(fetch, env("OPENAI_API_KEY"))
    if env("GROQ_API_KEY"):
        jobs["groq"] = lambda: fetch_groq(fetch, env("GROQ_API_KEY"))
    if env("CLOUDFLARE_ACCOUNT_ID") and env("CLOUDFLARE_API_TOKEN"):
        jobs["cloudflare"] = lambda: fetch_cloudflare(fetch, env("CLOUDFLARE_ACCOUNT_ID"),
                                                      env("CLOUDFLARE_API_TOKEN"))
    cat = Catalog()
    for name, job in jobs.items():
        try:
            models = job()
            cache[name] = {"at": now, "models": [m.__dict__ for m in models]}
        except Exception as e:
            entry = cache.get(name)
            if entry and now - entry["at"] <= CACHE_DAYS * 86400:
                log.warning("%s catalog fetch failed (%s); using cache", name, e)
                models = [Model(**m) for m in entry["models"]]
                cat.from_cache.append(name)
            else:
                log.warning("%s catalog unavailable and no fresh cache: %s", name, e)
                cat.unavailable.append(name)
                continue
        # D7: outdated families are hidden from the writer and never runnable.
        cat.models.extend(m for m in models if not is_outdated_id(m.norm))
    # Same model on your own OpenAI account and on OpenRouter: keep the direct one, but borrow
    # OpenRouter's price so cost estimates still work.
    direct = {m.norm: m for m in cat.models if m.provider == "openai"}
    kept = []
    for m in cat.models:
        twin = direct.get(m.norm)
        if m.provider == "openrouter" and m.paid and twin:
            twin.price_in, twin.price_out = twin.price_in or m.price_in, twin.price_out or m.price_out
            continue
        if m.provider == "openrouter" and m.paid and not or_paid:
            continue                     # no OpenRouter credit: can't call it
        kept.append(m)
    cat.models = kept
    if use_cache:
        db.set_meta("catalog_cache", json.dumps(cache))
    return cat


def estimate_cost(keys: list[str], cat: Catalog, prompts: int = 20, tok_in: int = 600,
                  tok_out: int = 600) -> float | None:
    """Rough USD cost to run an experiment: `prompts` requests per model. None if a paid model
    has no known price."""
    by_key, total = cat.by_key(), 0.0
    for k in keys:
        m = by_key.get(k)
        if not m or not m.paid:
            continue
        if m.price_in is None or m.price_out is None:
            return None
        total += prompts * (tok_in * m.price_in + tok_out * m.price_out) / 1e6
    return total


def shortlist(cat: Catalog, focus_words: list[str], per_provider: int = 12) -> list[Model]:
    """The models offered to the idea writer: all Groq chat models, then the most relevant
    Cloudflare/OpenRouter ones (names matching the hot topics first). Keeps prompts small
    enough for Groq's free 8K tokens/minute."""
    words = {w for w in (normalize(x) for x in focus_words) if len(w) >= 3}
    out = []
    def relevant(m: Model) -> bool:
        return any(w in m.norm or (family_of(w) and m.norm.startswith(family_of(w))) for w in words)

    def rank(ms):
        # relevant to this week's stories first, then new launches, then catalog order
        return [m for _, m in sorted(enumerate(ms), key=lambda im: (not relevant(im[1]), not im[1].new, im[0]))]

    for provider in ("groq", "cloudflare", "openrouter", "openai"):
        free = rank([m for m in cat.models if m.provider == provider and not m.paid])
        paid = rank([m for m in cat.models if m.provider == provider and m.paid])
        out.extend(free if provider == "groq" else free[:per_provider])
        out.extend(paid[:per_provider])
    return out


# ---------- Hugging Face ----------

def github_repo(repo: str, fetch=None) -> dict | None:
    """Public facts about a GitHub repo, or None if it does not exist (or the API fails)."""
    try:
        d = json.loads((fetch or sources._get)(f"https://api.github.com/repos/{repo}",
                                               {"Accept": "application/vnd.github+json"}))
    except Exception as e:
        log.info("GitHub lookup %s failed: %s", repo, e)
        return None
    if not isinstance(d, dict) or "full_name" not in d:
        return None
    return {"full_name": d["full_name"], "stars": d.get("stargazers_count", 0),
            "language": d.get("language"), "description": (d.get("description") or "")[:200]}


def hf_model(repo: str, fetch=None, use_cache=True) -> dict | None | str:
    """HF metadata dict, None if the repo does not exist (401/404), 'unknown' on network error."""
    fetch = fetch or sources._get
    cache = json.loads(db.get_meta("hf_cache", "{}") or "{}") if use_cache else {}
    hit = cache.get(repo)
    if hit and time.time() - hit["at"] <= CACHE_DAYS * 86400:
        return hit["info"]
    try:
        info = json.loads(fetch(f"https://huggingface.co/api/models/{repo}", {}))
        info = {"id": info.get("id"), "pipeline_tag": info.get("pipeline_tag")}
    except urllib.error.HTTPError as e:
        if e.code in (401, 404):
            info = None              # HF answers 401, not 404, for repos that don't exist
        else:
            return "unknown"
    except Exception:
        return "unknown"
    if use_cache:
        cache[repo] = {"at": time.time(), "info": info}
        db.set_meta("hf_cache", json.dumps(cache))
    return info


# ---------- scans ----------

def families(cat: Catalog, extra_names: list[str] = ()) -> list[str]:
    fams = set(BASE_FAMILIES)
    for m in cat.models:
        f = family_of(m.norm)
        if len(f) >= 3:
            fams.add(f)
    for n in extra_names:
        f = family_of(normalize(n))
        if len(f) >= 3:
            fams.add(f)
    return sorted(fams, key=len, reverse=True)


def model_mentions(text: str, fams: list[str]) -> list[str]:
    """Model names in free text: a known family followed by a version ("Qwen3.8-27B",
    "Gemini 4 Argon", "Kimi K2.6"), plus any outdated name. "Python 3.12" never counts."""
    fam_re = "|".join(re.escape(f) for f in fams)
    # Family is case-insensitive; the words after the version are case-sensitive suffixes
    # like "Flash", "Super", "120B", "Argon" (lowercase words such as "vs", "on" end the name).
    pat = re.compile(
        rf"\b(?i:{fam_re})(?:[\s\-]?(?i:[a-z])?\d[\w.]*)"
        rf"(?:[\s\-](?:[A-Z][a-z]+|[A-Z]?\d[\w.]*|[A-Z]{{2,}}\d*)){{0,3}}")
    found = [m.group(0).strip(" .-") for m in pat.finditer(text)]
    found += [m.group(0) for m in OUTDATED.finditer(text)]
    seen, out = set(), []
    for f in found:
        k = normalize(f)
        if k and k not in seen:
            seen.add(k)
            out.append(f)
    return out


def covered_by(mention: str, model_keys: list[str]) -> bool:
    """'Nemotron 3 Super 120B' is covered by 'openrouter:nvidia/nemotron-3-super-120b-a12b:free'."""
    n = normalize(mention)

    def forms(k):                     # "openai:gpt-6-luna" -> the full key, "gpt-6-luna"; also "org/name" -> "name"
        mid = k.split(":", 1)[-1]
        return {normalize(k), normalize(mid), normalize(mid.split("/")[-1])}
    return any(f.startswith(n) or n.startswith(f) for k in model_keys for f in forms(k) if f)


def sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+|\n+", text or "")
    return [p.strip() for p in parts if p.strip()]


def entity_tokens(entities: list[str]) -> set[str]:
    toks = set()
    for e in entities:
        for t in re.split(r"[^a-z0-9.]+", e.lower()):
            t = t.strip(".")
            if len(t) >= 4 and t not in STOP:
                toks.add(t)
    return toks


# A number that sizes the experiment, not a result: "20 tasks", "10 support requests", "3 models".
SETUP_COUNT = re.compile(
    # "vs 3 rivals", "on 20 code repairs", "against 4 models": a count right after a setup word
    r"\b(?:vs\.?|versus|against|on|across|with|gave(?:\s+\w+)?|using|over|into)\s+(?:the\s+same\s+)?"
    r"(?:\d+|three|four|five|ten|twenty)\s+[a-z-]+(?:\s+[a-z-]+)?"
    r"|\b\d+\s+(?:[a-z-]+\s+){0,2}?(?:prompts?|rivals?|repairs?|fixes|competitors?|llms?|rules?|tasks?|requests?|questions?|models?|minutes?|mins?|hours?|"
    r"runs?|tries|examples?|tests?|files?|bugs?|issues?|receipts?|images?|pdfs?|documents?|docs?|"
    r"tickets?|emails?|queries|calls?|apps?|repos?|rounds?|challenges?|problems?|cases?|inputs?|"
    r"screenshots?|videos?|pages?|steps?|agents?|tools?|lines?)\b")


RESULT_SHAPE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:/|of|out of)\s*\d+\b(?!\s*(?:min|minutes|hours?)\b)"
    r"|\b\d+(?:\.\d+)?\s*x\s+(?:faster|slower|cheaper|better|more|less|fewer)\b"
    r"|\b(?:scored|won|beat|solved|passed|fixed|found|failed|crashed|hit)\s+(?:only\s+)?\d+")


def claim_sentences(texts: list[str], entities: list[str]) -> list[str]:
    """D14: sentences worth checking: money, percent, or a topic entity with an event word or
    a number. Setup counts like "3 models" or "1 hour" on their own are not claims."""
    toks = entity_tokens(entities)
    names = sorted({e.lower() for e in entities if e}, key=len, reverse=True)
    out = []
    for t in texts:
        for s in sentences(t):
            low = s.lower()
            money = bool(re.search(r"[$€£₹]\s?\d|\d\s?(?:million|billion|bn|m)\b", s, re.I))
            pct = "%" in s
            ent = any(re.search(rf"\b{re.escape(tok)}\b", low) for tok in toks)
            # Digits inside the topic's own names ("Gemini 4 Argon") are not claims.
            bare = low
            for n in names:
                bare = bare.replace(n, " ")
            bare = re.sub(r"\b[a-z]+[\w.-]*\d[\w.-]*\b", " ", bare)   # model-style names like k2.6
            bare = SETUP_COUNT.sub(" ", bare)                              # "on 20 real tasks" is the setup
            result = RESULT_SHAPE.search(low)          # "9 of 10", "3x faster", "scored 18": a measured result
            if money or pct or result or (ent and (EVENT_WORDS.search(s) or re.search(r"\d", bare))):
                out.append(s)
    return out


def supported(sentence: str, titles: list[str], entities: list[str]) -> str | None:
    """The signal title backing a claim sentence, or None. Every number in the sentence must
    appear in the title, and the title must name the entity plus one more word of the claim."""
    nums = [n.strip(".,") for n in re.findall(r"\d[\d.,]*", sentence)]
    toks = entity_tokens(entities)
    # Compare word stems so "launched" matches "launches" and "raised" matches "raises".
    stems = {w[:5] for w in re.findall(r"[a-z]{4,}", sentence.lower()) if w not in STOP and w not in toks}
    approx = bool(re.search(r"\b(more than|over|nearly|almost|about|around|roughly)\b|~|\+", sentence, re.I))

    def has(n, title):
        if n in title:
            return True
        if not approx:
            return False
        try:
            want = float(n.replace(",", ""))
        except ValueError:
            return False
        for m in re.findall(r"\d[\d,]*\.?\d*", title):
            got = float(m.replace(",", "").rstrip("."))
            if got and abs(got - want) / got <= 0.06:       # rounded within 6%
                return True
        return False

    for t in titles:
        low = t.lower()
        if not all(has(n, t) for n in nums):
            continue
        if toks and not any(tok in low for tok in toks):
            continue
        title_stems = {w[:5] for w in re.findall(r"[a-z]{4,}", low)}
        if stems and not stems & title_stems:
            continue
        return t
    return None


# ---------- checks ----------

def _evidence(topic: dict, signals: list[dict]) -> list[str]:
    """Each source as "title (detail)": the detail carries facts like "HF trending #1" or
    "959 points" that claims may rely on."""
    out = []
    for i in topic.get("signal_ids", []):
        if 0 <= i < len(signals):
            s = signals[i]
            detail = re.sub(r"\bHF\b", "Hugging Face", s.get("detail", ""))
            out.append(f"[{s.get('kind', '')}] {s['title']} ({detail})")
    return out


RUN_LANES = {"face-off", "first-look", "build-with-it", "stress-test", "replicate"}


@dataclass
class Verdict:
    ok: bool
    reasons: list[str] = field(default_factory=list)        # why it failed (regenerate/drop)
    unverified: list[str] = field(default_factory=list)     # "Check before you post"


def check_idea(idea: dict, topic: dict, signals: list[dict], cat: Catalog,
               hf_names: list[str] = (), hf_lookup=None) -> Verdict:
    v = Verdict(True)
    by_key = cat.by_key()
    lane = (idea.get("format") or "").lower()
    runs = list(idea.get("models_to_run") or [])

    # D13: models to run must be real, current, free catalog ids (backstop for providers that
    # ignore the schema enum).
    for k in runs:
        m = by_key.get(k)
        if not m:
            v.ok = False
            v.reasons.append(f"{k} is not in a free model catalog")
        elif VISION_NEED.search(idea.get("build_plan", "")) and m.vision is False:
            v.ok = False
            v.reasons.append(f"{k} cannot take image/video input")
        elif VISION_NEED.search(idea.get("build_plan", "")) and m.vision is None:
            v.unverified.append(f"Check that {m.id} accepts images/video on {m.provider}")
    if lane == "face-off" and not runs:
        v.ok = False
        v.reasons.append(f"a {lane} idea must name the models it runs")

    # Model names in the text: covered by models_to_run, or checked as mentions.
    text_fields = [plain(t) for t in (idea.get("title", ""), idea.get("build_plan", ""),
                                      idea.get("why_hot", ""), *idea.get("hook_options", []),
                                      *idea.get("needs", []))]
    titles = _evidence(topic, signals)
    fams = families(cat, hf_names)
    for mention in model_mentions(" \n".join(text_fields), fams):
        if covered_by(mention, runs):
            continue
        if OUTDATED.fullmatch(mention.strip()) or OUTDATED.search(mention):
            if lane in RUN_LANES:
                v.ok = False
                v.reasons.append(f"mentions outdated model {mention}")
            else:
                v.unverified.append(f"Older model {mention}: make sure the post presents it as older")
            continue
        known = (covered_by(mention, [m.key for m in cat.models])
                 or covered_by(mention, list(hf_names))
                 or any(normalize(mention) in normalize(t) for t in titles))
        if not known:
            v.unverified.append(f"Model \"{mention}\" not found in the model lists, HF trending or the sources")

    # Hugging Face repo ids written in the text must exist.
    catalog_keys = [m.key for m in cat.models] + runs
    for repo in set(re.findall(r"\b([A-Za-z0-9][\w.-]+/[A-Za-z0-9][\w.-]*\d[\w.-]*)\b", " ".join(text_fields))):
        # Provider ids like "openrouter:nvidia/nemotron-3.5-lightning:free" are not HF repos.
        if any(normalize(repo) == normalize(k) for k in catalog_keys):
            continue
        if not repo.startswith(("http", "www")) and hf_lookup:
            info = hf_lookup(repo)
            if info is None:
                v.unverified.append(f"Hugging Face repo {repo} does not exist")

    # D14: money, percent and company claims are checked against the sources.
    # Eng review O1 (D6): in a HOOK (the line people actually read) an unproven claim fails the
    # idea; in why_hot it is only flagged.
    ents = topic.get("entities", [])
    for s in claim_sentences(list(map(plain, idea.get("hook_options", []))), ents):
        if "{{" in s:
            continue                       # a placeholder is honest: the person fills it in
        if not supported(s, titles, ents):
            v.ok = False
            v.reasons.append(f"hook makes an unproven claim: \"{s}\"")
    for s in claim_sentences([plain(idea.get("why_hot", ""))], ents):
        if not supported(s, titles, ents):
            v.unverified.append(f"Unverified claim: \"{s}\"")
    return v


# ---------- generated code (eng review R1) ----------

ALLOWED_PACKAGES = {"requests", "pandas", "matplotlib", "numpy", "pydantic", "openai", "groq",
                    "pillow", "pil", "httpx", "python-dotenv", "dotenv", "tabulate", "rich",
                    "pytest", "jsonschema", "pyyaml", "yaml", "tqdm", "packaging"}
ALLOWED_HOSTS = {"api.openai.com", "api.groq.com", "openrouter.ai", "api.cloudflare.com",
                 "huggingface.co", "localhost", "127.0.0.1"}
STDLIB = set(getattr(__import__("sys"), "stdlib_module_names", ()))
RISKY_CALLS = [
    (re.compile(r"\bsubprocess\.|\bos\.system\(|\bos\.popen\("), "runs a shell command"),
    (re.compile(r"(?<![\w.])eval\(|(?<![\w.])exec\("), "uses eval/exec on generated text"),
    (re.compile(r"shutil\.rmtree\(|os\.remove\(|os\.unlink\(|\.unlink\(|os\.rmdir\("), "deletes files"),
    (re.compile(r"\bpickle\.loads?\(|\bmarshal\.loads?\("), "loads pickled data"),
]


def scan_code(files: list[tuple[str, str]], extra_hosts: set[str] = frozenset()) -> list[str]:
    """'Check before you run' lines for model-written files: package installs or imports outside
    a small allowlist, shell commands, eval/exec, deletes, and network calls to unknown hosts.
    Never blocks; the person reads the list before running anything."""
    hosts = ALLOWED_HOSTS | set(extra_hosts)
    out = []
    for path, text in files:
        for n, line in enumerate(text.splitlines(), 1):
            where = f"{path}:{n}"
            for m in re.finditer(r"pip3?\s+install\s+([^\n#;&|]+)", line):
                args = m.group(1).split()
                for i, pkg in enumerate(args):
                    if i and args[i - 1] in ("-r", "--requirement", "-c", "--constraint"):
                        continue                      # a requirements file, not a package
                    name = re.split(r"[<>=\[]", pkg.lower())[0].strip()
                    if name and not name.startswith("-") and name not in ALLOWED_PACKAGES:
                        out.append(f"{where}: installs package '{name}': check it is real and wanted")
            m = re.match(r"\s*(?:from\s+([A-Za-z_][\w]*)|import\s+([A-Za-z_][\w]*))", line)
            if m and path.endswith(".py"):
                mod = (m.group(1) or m.group(2)).lower()
                if mod not in STDLIB and mod not in ALLOWED_PACKAGES and mod != "__future__":
                    out.append(f"{where}: imports '{mod}': check the package is real before installing")
            for rx, why in RISKY_CALLS:
                if rx.search(line):
                    out.append(f"{where}: {why}")
            for host in re.findall(r"https?://([A-Za-z0-9.-]+)", line):
                if host.lower() not in hosts and not any(host.lower().endswith("." + h) for h in hosts):
                    out.append(f"{where}: calls an unknown host '{host}'")
    return out


def scan_text(texts: list[str], topic: dict, signals: list[dict], cat: Catalog,
              runs: list[str], hf_names: list[str] = ()) -> list[str]:
    """D12: the same scans on a finished post and its steps. Returns 'Check before you post' lines."""
    titles = _evidence(topic, signals)
    texts = [plain(t) for t in texts]
    out = []
    for mention in model_mentions(" \n".join(texts), families(cat, hf_names)):
        if covered_by(mention, runs):
            continue
        if OUTDATED.search(mention):
            out.append(f"Older model {mention}: make sure the post presents it as older")
        elif not (covered_by(mention, [m.key for m in cat.models]) or covered_by(mention, list(hf_names))
                  or any(normalize(mention) in normalize(t) for t in titles)):
            out.append(f"Model \"{mention}\" in the post was not found in catalogs or sources")
    for s in claim_sentences(texts, topic.get("entities", [])):
        if not supported(s, titles, topic.get("entities", [])):
            out.append(f"Unverified claim in the post: \"{s}\"")
    return out


def filter_links(topic: dict, signals: list[dict]) -> list[int]:
    """Signal ids whose URL or title names the topic (entity token, 4+ chars)."""
    toks = entity_tokens(topic.get("entities", []) or [topic.get("name", "")])
    handle = (topic.get("handle") or "").lower().strip()
    if "/" in handle:                    # a repo "owner/name" names the story even if "name" is short
        toks = [*toks, re.sub(r"^https?://github\.com/", "", handle).strip("/")]
    keep = []
    for i in topic.get("signal_ids", []):
        if not 0 <= i < len(signals):
            continue
        s = signals[i]
        hay = (s.get("url", "") + " " + s.get("title", "")).lower()
        if any(tok in hay for tok in toks):
            keep.append(i)
    return keep
