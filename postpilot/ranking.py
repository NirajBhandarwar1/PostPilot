"""How hot a story is: measured attention only, plus the bonuses that decide which story is picked.

Pure functions over the collected signals; no model call, no database, no network. Kept apart from the
pipeline so a ranking change does not mean reading the mail and build flow (eng review R5).
"""

import math
import re

MODEL_SCORE_CAP = 15.0   # the model's own opinion (relevance + buzz) can add at most this
# Stories you can get your hands on beat news-only ones (the user's rule: test it, build with it).
HANDLE_BONUS = {"model": 8.0, "github_repo": 8.0, "product": 5.0, "paper": 4.0}

# What won in the first 15 examples (a reviewer's reading, one author, one month: small weights on purpose, to be
# re-tuned on your own results): repos that big companies open-sourced, and tools for coding agents.
BIG_ORGS = frozenset({"google", "google-deepmind", "anthropics", "openai", "meta-llama", "facebookresearch", "microsoft",
                      "nvidia", "cloudflare", "vercel-labs", "huggingface", "github", "apple", "mistralai",
                      "deepseek-ai", "qwenlm", "airbnb", "stripe", "uber", "netflix", "shopify", "revolut"})
DEV_TOOL = re.compile(r"\b(skills?|plugins?|mcp|claude code|codex|cursor|coding agents?|agents\.md|rag|ocr|eval|evals|inference|fine-?tun\w*|interview|toolkit)\b", re.I)
LAB_ORG = re.compile(r"\b(anthropic|openai|google|deepmind|claude)\b", re.I)
HOW_TO = re.compile(r"\b(recommends?|guide|how we|lessons?|best practices|playbook|how to)\b", re.I)
NUMBERS = re.compile(r"\d\s?%\s+(?:faster|cheaper|more accurate|accuracy|latency|cost|memory)|"
                     r"\b\d+(?:\.\d+)?x\s+(?:faster|cheaper|smaller|less)|per million|"
                     r"\d+(?:\.\d+)?\s?%?\s?(?:faster|cheaper|accuracy|tokens|latency|cost|memory)\b", re.I)
FREE = re.compile(r"\bfree\b|\bMIT[- ]licen[sc]ed\b|\brun(?:s|ning)?\s+locally\b|\bon-device\b|\bself-hosted\b|\bno API key\b|\boffline\b")
MONEY = re.compile(r"\braise[sd]?\s+\$|\$\s?[\d.]+\s?(?:billion|bn|B)\b|\bfunding round|\bseries [A-F]\b|\bvaluation|\bacquires|\bacquisition|\bIPO\b", re.I)
LAB_PREFIXES = ("Google AI blog:", "Hugging Face blog:", "OpenAI news:", "Anthropic")
ARGUMENT = re.compile(r"^\s*Ask HN|\b(lawsuit|sues?|layoffs?|essay|opinion|manifesto|I quit|is dead|considered harmful)\b", re.I)
JUNK_MODEL = re.compile(r"\b(?:quantized|quants?|GGUF|GPTQ|AWQ|merge[ds]?|abliterated|uncensored|diffusion|lora)\b|-(?:\d)?bit\b|"
                        r"\((?:text-to-image|text-to-video|image-to-image|image-to-video|unconditional-image-generation)\)", re.I)
JUNK_REPO = re.compile(r"leaked?[- ]system[- ]prompts?|system[- ]prompts? leak|awesome[- ]chatgpt[- ]prompts|awesome[- ]prompts", re.I)
MIN_HEAT_FOR_BONUS = 10.0     # a repo with no real attention gets nothing, whoever owns it


def builder_bonus(topic: dict) -> float:
    """Extra points for a repo from a big organisation (+5) and for a tool for coding agents (+4), only for a story
    that already has real attention (heat of 10 or more). Read from the repo owner and the repo's own name and
    description (GitHub's words), not from the free model's label, which is unreliable. The only place junk
    (re-uploaded models, prompt lists) is penalised."""
    if topic.get("handle_type") == "model":
        mtext = " ".join(str(topic.get(k) or "") for k in ("handle", "handle_facts"))
        return -HANDLE_BONUS["model"] if JUNK_MODEL.search(mtext) else 0.0   # re-upload, quantized/merged, image/video model
    text = " ".join(str(topic.get(k) or "") for k in ("handle", "handle_facts", "name"))
    if topic.get("handle_type") != "github_repo" or float(topic.get("heat") or 0) < MIN_HEAT_FOR_BONUS:
        return 0.0
    if JUNK_REPO.search(text):
        return -HANDLE_BONUS["github_repo"]    # leaked system prompts, prompt lists: not something to build with
    owner = str(topic.get("handle") or "").split("/")[0].lower()
    text = " ".join(str(topic.get(k) or "") for k in ("handle", "handle_facts"))
    return (5.0 if owner in BIG_ORGS else 0.0) + (4.0 if DEV_TOOL.search(text) else 0.0)


MAX_POSITIVE_ADJ = 6.0


def content_adjust(topic: dict, signals: list[dict], ids: list[int] | None = None, raw_heat: float = 1e9) -> float:
    """What the best signal's title says about whether a developer can use the story: how-to guidance from a lab, hard
    numbers and free/local framing score a little (only when raw heat >= MIN_HEAT_FOR_BONUS, total capped at +6);
    money news and pure arguments score less. Kept apart from heat() so the measured attention stays pure."""
    ids = topic.get("signal_ids", []) if ids is None else ids
    hits = [signals[i] for i in ids if 0 <= i < len(signals)]
    if not hits:
        return 0.0
    best = min(hits, key=lambda h: (-(h.get("engagement") or 0), h["rank"]))   # one story, one headline
    title = str(best.get("title") or "")
    pos = neg = 0.0
    if any((h["kind"] in ("Eng blog", "HN lab post") or str(h.get("title") or "").startswith(LAB_PREFIXES))
           and LAB_ORG.search(str(h.get("title") or "")) and HOW_TO.search(str(h.get("title") or "")) for h in hits):
        pos += 5.0
    if NUMBERS.search(title):
        pos += 2.0
    if FREE.search(title):
        pos += 2.0
    if MONEY.search(title):
        neg -= 6.0
    if ARGUMENT.search(title) and not DEV_TOOL.search(title) and not any("github.com" in str(h.get("url") or "") for h in hits):
        neg -= 5.0
    return neg + (min(pos, MAX_POSITIVE_ADJ) if raw_heat >= MIN_HEAT_FOR_BONUS else 0.0)


def heat(topic: dict, signals: list[dict], ids: list[int] | None = None) -> tuple[float, str]:
    """Measured attention only: upvotes, curated front pages, trending ranks, capped coverage.
    `ids` should be the sources that really name the story (link_ids)."""
    ids = topic["signal_ids"] if ids is None else ids
    hits = [signals[i] for i in ids if 0 <= i < len(signals)]
    kinds: dict[str, list[dict]] = {}
    for h in hits:
        kinds.setdefault(h["kind"], []).append(h)
    # Real attention beats article counts: upvotes, curated front pages, trending ranks.
    score = 3.0 * len(kinds)                                   # seen in many places = viral
    news_pts = 0.0
    for h in hits:
        kind, rank, eng = h["kind"], h["rank"], h.get("engagement", 0) or 0
        if kind.startswith("Hacker News"):
            pts = min(20.0, 4 * math.log2(eng / 100 + 1)) if eng else 3.0   # 959 -> ~14, 2293 -> ~18
        elif kind == "Techmeme":
            pts = 6.0 if rank < 5 else 4.0                   # curated by the industry itself
        elif kind == "Reddit":
            pts = 4.0 if rank < 3 else 2.0
        elif kind == "GitHub trending":
            pts = min(15.0, 3 * math.log2(eng / 1000 + 1)) if eng else 1.0   # 5k -> ~8, 30k+ -> 15
        elif kind.startswith("Hugging Face"):
            pts = 5.0 if rank < 3 else 3.0 if rank < 10 else 1.5
        elif kind == "GitHub new tools":                     # new coding-agent repos: smaller than the trending page
            pts = min(8.0, 2 * math.log2(eng / 100 + 1)) if eng else 1.0
        elif kind == "Paper":                                # HF Daily Papers upvotes: 100 -> ~7
            pts = min(10.0, 3 * math.log2(eng / 50 + 1)) if eng else 2.0
        elif kind == "HN lab post":                          # a lab's own post that HN upvoted
            pts = min(16.0, 4 * math.log2(eng / 100 + 1)) if eng else 3.0
        elif kind == "Eng blog":                             # a company's own announcement
            pts = 3.0
        else:                                                # plain news coverage
            news_pts += 1.0
            continue
        score += pts
    score += min(news_pts, 8)                                # coverage helps, but is capped
    parts = []
    for kind, hs in sorted(kinds.items(), key=lambda kv: -len(kv[1])):
        best = min(hs, key=lambda h: h["rank"])
        parts.append(f"{kind} x{len(hs)}" if len(hs) > 1 else f"{kind}: {best['detail']}")
    return max(score, 0.0), " | ".join(parts)
