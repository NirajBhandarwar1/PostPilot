"""Trend radar: free sources that show what is going viral in AI right now.

Every signal has a `kind` (where it was seen) and a `rank` inside its list (0 = top), so the
pipeline can score a topic by how many different places it is trending and how high.
Each source fails quietly so one dead site never stops a cycle.
"""

import html
import json
import logging
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from .config import env, settings

log = logging.getLogger("postpilot.sources")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) postpilot/0.3"
RADAR_DAYS = float(env("RADAR_DAYS", "10"))   # viral things stay hot for 1-3 weeks
AI_WORDS = re.compile(r"\b(AI|A\.I\.|LLM|GPT|model|models|agent|agents|Claude|Gemini|OpenAI|Anthropic|"
                      r"DeepSeek|Qwen|Llama|Mistral|Muse|Grok|Copilot|chatbot|neural|diffusion|"
                      r"open[- ]source|open[- ]weight|inference|benchmark|AGI|robot)\b", re.I)


def _get(url: str, headers: dict | None = None) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read()


def _sig(kind, rank, title, url, engagement=0, detail=""):
    return {"kind": kind, "rank": rank, "title": html.unescape(title).strip()[:110],
            "url": url, "engagement": engagement, "detail": detail}


# ---------- sources ----------

def hf_trending_models() -> list[dict]:
    data = json.loads(_get("https://huggingface.co/api/models?sort=trendingScore&limit=12"))
    return [_sig("Hugging Face trending models", i, f"{m['id']} ({m.get('pipeline_tag') or 'model'})",
                 f"https://huggingface.co/{m['id']}", m.get("trendingScore", 0),
                 f"HF trending #{i + 1}, {m.get('likes', 0)} likes") for i, m in enumerate(data)]


def github_trending() -> list[dict]:
    page = _get("https://github.com/trending?since=weekly").decode("utf-8", "replace")
    out = []
    for block in page.split('class="Box-row"')[1:]:
        m = re.search(r'return_to=%2F([^%"]+)%2F([^"&]+)"', block)
        if not m:
            continue
        repo = f"{m.group(1)}/{urllib.parse.unquote(m.group(2))}"
        desc = re.search(r'<p class="col-9[^"]*">\s*(.*?)\s*</p>', block, re.S)
        stars = re.search(r"([\d,]+) stars this week", block)
        n = int(stars.group(1).replace(",", "")) if stars else 0
        text = re.sub(r"<[^>]+>", "", desc.group(1)) if desc else ""
        out.append(_sig("GitHub trending", len(out), f"{repo}: {text}", f"https://github.com/{repo}",
                        n, f"+{n:,} stars this week"))
    return out


def reddit() -> list[dict]:
    out = []
    for sub in ["singularity", "OpenAI", "artificial", "LocalLLaMA"]:
        root = None
        for attempt in range(2):
            try:
                root = ET.fromstring(_get(f"https://www.reddit.com/r/{sub}/top/.rss?t=week&limit=10"))
                break
            except Exception as e:
                log.warning("reddit r/%s failed (try %d): %s", sub, attempt + 1, e)
                time.sleep(8)
        if root is None:
            continue
        ns = "{http://www.w3.org/2005/Atom}"
        for i, e in enumerate(root.findall(f"{ns}entry")[:6]):
            link = e.find(f"{ns}link")
            out.append(_sig("Reddit", i, f"r/{sub}: {e.findtext(f'{ns}title') or ''}",
                            link.get("href") if link is not None else "", 0, f"r/{sub} top of week #{i + 1}"))
        time.sleep(7)  # Reddit throttles fast requests
    return out


HN_QUERIES = ["AI", "LLM", "GPT", "model", "agent", "OpenAI", "Anthropic", "Claude", "Gemini",
              "DeepSeek", "Qwen", "open source model", "inference", "AI coding"]


def hacker_news() -> list[dict]:
    """The most-upvoted AI stories on Hacker News this week, by points (a real attention signal).
    Searching by points finds viral stories the front-page keyword filter used to miss."""
    since = int(time.time() - min(RADAR_DAYS, 7) * 86400)
    hits = {}
    for q in HN_QUERIES:
        url = ("https://hn.algolia.com/api/v1/search?tags=story&hitsPerPage=20&query="
               + urllib.parse.quote(q) + f"&numericFilters=points>=80,created_at_i>{since}")
        try:
            for h in json.loads(_get(url)).get("hits", []):
                if AI_WORDS.search(h.get("title") or "") or q.lower() in (h.get("title") or "").lower():
                    hits[h["objectID"]] = h
        except Exception as e:
            log.warning("hn query %r failed: %s", q, e)
    ranked = sorted(hits.values(), key=lambda h: -h.get("points", 0))[:25]
    return [_sig("Hacker News", i, h["title"],
                 h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}",
                 h.get("points", 0), f"{h.get('points', 0)} points, {h.get('num_comments', 0)} comments")
            for i, h in enumerate(ranked)]


def techmeme() -> list[dict]:
    """Techmeme's curated river: the stories the tech industry itself treats as top news."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=min(RADAR_DAYS, 7))
    out = []
    for title, link, when in _feed_items(_get("https://www.techmeme.com/feed.xml")):
        if when >= cutoff and AI_WORDS.search(title):
            out.append(_sig("Techmeme", len(out), re.sub(r"\s*\([^)]*\)\s*$", "", title), link, 0,
                            when.strftime("%d %b")))
    return out[:15]


# Broad AI-industry searches: the stories people in AI are actually talking about.
NEWS_QUERIES = [
    "OpenAI", "Anthropic OR Claude", "Google Gemini OR DeepMind", "Meta AI OR Microsoft AI OR Apple AI",
    "Nvidia AI chips", "AI startup raises OR valuation OR acquisition",
    "AI viral OR \"went viral\" OR backlash", "AI model launch OR unveils OR releases",
    "AI regulation OR lawsuit OR ban", "AI jobs OR layoffs OR hiring",
    "AI agents", "open-source AI model",
]
NEWS_FEEDS = {
    "TechCrunch": "https://techcrunch.com/category/artificial-intelligence/feed/",
    "The Verge": "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
    "Hugging Face blog": "https://huggingface.co/blog/feed.xml",
    "Google AI blog": "https://blog.google/technology/ai/rss/",
    "Simon Willison": "https://simonwillison.net/atom/everything/",
}
ATOM = "{http://www.w3.org/2005/Atom}"


def _feed_items(raw: bytes):
    root = ET.fromstring(raw)
    for it in root.findall(".//item") or root.findall(f".//{ATOM}entry"):
        title = it.findtext("title") or it.findtext(f"{ATOM}title") or ""
        link_el = it.find(f"{ATOM}link")
        link = it.findtext("link") or (link_el.get("href") if link_el is not None else "")
        raw_date = it.findtext("pubDate") or it.findtext(f"{ATOM}updated") or it.findtext(f"{ATOM}published") or ""
        try:
            when = (parsedate_to_datetime(raw_date) if "," in raw_date
                    else datetime.fromisoformat(raw_date.replace("Z", "+00:00")))
        except Exception:
            continue
        yield title, link, when


def news() -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=min(RADAR_DAYS, 7))
    out, seen = [], set()
    for q in NEWS_QUERIES:
        url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(f"{q} when:7d")
               + "&hl=en-US&gl=US&ceid=US:en")
        try:
            items = list(_feed_items(_get(url)))
        except Exception as e:
            log.warning("google news failed: %s", e)
            continue
        for title, link, when in items[:5]:
            key = title.lower()[:60]
            if when >= cutoff and key not in seen:
                seen.add(key)
                out.append(_sig("News", len(out), title, link, 0, when.strftime("%d %b")))
    for name, url in NEWS_FEEDS.items():
        try:
            items = list(_feed_items(_get(url)))
        except Exception as e:
            log.warning("feed %s failed: %s", name, e)
            continue
        for title, link, when in items[:3]:
            if when >= cutoff:
                out.append(_sig("News", len(out), f"{name}: {title}", link, 0, when.strftime("%d %b")))
    return out


COLLECTORS = [hacker_news, techmeme, reddit, github_trending, hf_trending_models, news]


def collect(days: float | None = None) -> list[dict]:
    items = []
    for fn in COLLECTORS:
        try:
            got = fn()
            log.info("%s: %d signals", fn.__name__, len(got))
            items.extend(got)
        except Exception as e:  # network hiccup, rate limit, site change
            log.warning("%s failed: %s", fn.__name__, e)
    return items
