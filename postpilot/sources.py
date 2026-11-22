"""Trend radar: free sources that show what is going viral in AI right now.

Every signal has a `kind` (where it was seen) and a `rank` inside its list (0 = top), so the
pipeline can score a topic by how many different places it is trending and how high.
Each source fails quietly so one dead site never stops a cycle.
"""

import html
import ipaddress
import json
import logging
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from functools import partial

from . import db
from .config import env, settings

log = logging.getLogger("postpilot.sources")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) postpilot/0.3"
RADAR_DAYS = float(env("RADAR_DAYS", "10"))   # viral things stay hot for 1-3 weeks
AI_WORDS = re.compile(r"\b(AI|A\.I\.|LLM|GPT|model|models|agent|agents|Claude|Gemini|OpenAI|Anthropic|"
                      r"DeepSeek|Qwen|Llama|Mistral|Muse|Grok|Copilot|chatbot|neural|diffusion|"
                      r"open[- ]source|open[- ]weight|inference|benchmark|AGI|robot|MCP|Codex|coding agents?|"
                      r"agent skills?)(?:s|\d[\w.]*)?\b", re.I)


def _public_url(url: str) -> bool:
    """Only http(s) to a public host: scraped links can point at file://, the local network or a
    cloud metadata address. Checks the literal host (incl. shorthand IPs like 127.1) and every
    address the name resolves to."""
    p = urllib.parse.urlparse(url)
    host = (p.hostname or "").lower().rstrip(".")
    if p.scheme not in ("http", "https") or not host or host == "localhost" or \
            host.endswith((".local", ".internal", ".localhost")):
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        pass
    if re.fullmatch(r"(?:0x[0-9a-f]+|\d+)(?:\.(?:0x[0-9a-f]+|\d+)){0,3}", host):   # 127.1, 2130706433, 0x7f000001
        try:
            return ipaddress.ip_address(socket.inet_aton(host)).is_global
        except (OSError, ValueError):
            return False
    try:
        addrs = {ai[4][0] for ai in socket.getaddrinfo(host, None)}
    except OSError:
        return True                                   # can't resolve: the fetch itself will fail
    return all(ipaddress.ip_address(str(a).split("%")[0]).is_global for a in addrs)


class _CheckedRedirect(urllib.request.HTTPRedirectHandler):
    """Every redirect target passes the same check as the first URL."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _public_url(newurl):
            raise urllib.error.URLError(f"refused redirect to non-public URL: {newurl[:80]}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


urllib.request.install_opener(urllib.request.build_opener(_CheckedRedirect))


def _get(url: str, headers: dict | None = None) -> bytes:
    if not _public_url(url):
        raise ValueError(f"refusing to fetch non-public URL: {url[:80]}")
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read()


def _sig(kind, rank, title, url, engagement=0, detail=""):
    return {"kind": kind, "rank": rank, "title": " ".join(html.unescape(title).split())[:110],
            "url": url, "engagement": engagement, "detail": detail}


# ---------- sources ----------

def hf_trending_models() -> list[dict]:
    data = json.loads(_get("https://huggingface.co/api/models?sort=trendingScore&limit=12"))
    return [_sig("Hugging Face trending models", i, f"{m['id']} ({m.get('pipeline_tag') or 'model'})",
                 f"https://huggingface.co/{m['id']}", m.get("trendingScore", 0),
                 f"HF trending #{i + 1}, {m.get('likes', 0)} likes") for i, m in enumerate(data)]


def github_trending() -> list[dict]:
    page = _get("https://github.com/trending?since=weekly").decode("utf-8", "replace")
    out: list[dict] = []
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
    out: list[dict] = []
    for sub in ["ClaudeAI", "LLMDevs", "MachineLearning", "LocalLLaMA"]:   # where builders talk, not hype
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
        for i, entry in enumerate(root.findall(f"{ns}entry")[:6]):
            link = entry.find(f"{ns}link")
            out.append(_sig("Reddit", i, f"r/{sub}: {entry.findtext(f'{ns}title') or ''}",
                            link.get("href") if link is not None else "", 0, f"r/{sub} top of week #{i + 1}"))
        time.sleep(7)  # Reddit throttles fast requests
    return out


HN_QUERIES = ["AI", "LLM", "GPT", "model", "agent", "OpenAI", "Anthropic", "Claude", "Gemini",
              "DeepSeek", "Qwen", "open source model", "inference", "AI coding", "Claude Code", "MCP", "Codex",
              "open-sourced"]


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
                if AI_WORDS.search(h.get("title") or "") or re.search(rf"\b{re.escape(q)}(?:s|\d[\w.]*)?\b", h.get("title") or "", re.I):
                    hits[h["objectID"]] = h
        except Exception as e:
            log.warning("hn query %r failed: %s", q, e)
    ranked = sorted(hits.values(), key=lambda h: -h.get("points", 0))[:25]
    return [_sig("Hacker News", i, h["title"],
                 h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}",
                 h.get("points", 0), f"{h.get('points', 0)} points, {h.get('num_comments', 0)} comments")
            for i, h in enumerate(ranked)]


def hf_daily_papers() -> list[dict]:
    """Hugging Face Daily Papers: the five most-upvoted research papers of the day (one request)."""
    data = json.loads(_get("https://huggingface.co/api/daily_papers?limit=30"))
    rows = []
    for d in data if isinstance(data, list) else []:
        p = d.get("paper") if isinstance(d, dict) and isinstance(d.get("paper"), dict) else d
        if not isinstance(p, dict) or not p.get("id") or not (p.get("title") or d.get("title")):
            continue
        up = p.get("upvotes", d.get("upvotes", 0))
        rows.append((up if isinstance(up, int) and not isinstance(up, bool) else 0, p["id"], p.get("title") or d["title"]))
    rows.sort(key=lambda r: -r[0])
    return [_sig("Paper", i, str(t), f"https://huggingface.co/papers/{pid}", up, f"{up} upvotes on HF Daily Papers")
            for i, (up, pid, t) in enumerate(rows[:5])]


def _hn_hits(url: str) -> list[dict]:
    hits = json.loads(_get(url)).get("hits", [])
    return [h for h in hits if isinstance(h, dict) and h.get("objectID") and h.get("title")] if isinstance(hits, list) else []


def _hn_link(h: dict) -> str:
    return h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}"


def show_hn() -> list[dict]:
    """Show HN projects about AI with real upvotes: things people built and shipped (one request)."""
    since = int(time.time() - min(RADAR_DAYS, 7) * 86400)
    hits = _hn_hits("https://hn.algolia.com/api/v1/search?tags=show_hn&hitsPerPage=30"
                    f"&numericFilters=points>=40,created_at_i>{since}")
    hits = sorted((h for h in hits if AI_WORDS.search(h["title"])), key=lambda h: -(h.get("points") or 0))[:8]
    return [_sig("Hacker News", i, h["title"], _hn_link(h), h.get("points") or 0,
                 f"Show HN, {h.get('points') or 0} points, {h.get('num_comments') or 0} comments")
            for i, h in enumerate(hits)]


HN_LAB_QUERIES = ("anthropic.com", "engineering")      # two requests in total, never one per domain


def hn_lab_posts() -> list[dict]:
    """Stories whose link is a lab's own site or an engineering blog (the best how-to posts reach HN this way)."""
    since = int(time.time() - min(RADAR_DAYS, 7) * 86400)
    hits: dict[str, dict] = {}
    for q in HN_LAB_QUERIES:
        url = ("https://hn.algolia.com/api/v1/search?tags=story&hitsPerPage=20&restrictSearchableAttributes=url"
               f"&query={urllib.parse.quote(q)}&numericFilters=points>=50,created_at_i>{since}")
        try:
            for h in _hn_hits(url):
                if q != "engineering" or AI_WORDS.search(h["title"]):
                    hits[h["objectID"]] = h
        except Exception as e:
            log.warning("hn lab query %r failed: %s", q, e)
    ranked = sorted(hits.values(), key=lambda h: -(h.get("points") or 0))[:10]
    return [_sig("HN lab post", i, h["title"], _hn_link(h), h.get("points") or 0,
                 f"{h.get('points') or 0} points on HN, linked from a lab or engineering blog")
            for i, h in enumerate(ranked)]


def techmeme() -> list[dict]:
    """Techmeme's curated river: the stories the tech industry itself treats as top news."""
    cutoff = datetime.now(UTC) - timedelta(days=min(RADAR_DAYS, 7))
    out: list[dict] = []
    for title, link, when in _feed_items(_get("https://www.techmeme.com/feed.xml")):
        if when >= cutoff and AI_WORDS.search(title):
            out.append(_sig("Techmeme", len(out), re.sub(r"\s*\([^)]*\)\s*$", "", title), link, 0,
                            when.strftime("%d %b")))
    return out[:15]


# Broad AI-industry searches: the stories people in AI are actually talking about.
# What a developer can try today beats money and policy news: the posts that win are about tools, repos and releases
# (eng review of the first 15 examples), so the finance, regulation and jobs searches are gone.
NEWS_QUERIES = [
    "OpenAI", "Anthropic OR Claude", "Google Gemini OR DeepMind", "Meta AI OR Microsoft AI OR Apple AI",
    "AI viral OR \"went viral\" OR backlash", "AI model launch OR unveils OR releases",
    "AI agents", "open-source AI model", "Claude Code OR Codex OR MCP", "open-sourced AI tool",
]
# Engineering blogs of the companies that open-source tools people try (feeds checked 2026-11-18).
ENG_BLOGS = {
    "Cloudflare blog": "https://blog.cloudflare.com/rss/",
    "GitHub blog": "https://github.blog/feed/",
    "Meta Engineering": "https://engineering.fb.com/feed/",
    "OpenAI news": "https://openai.com/news/rss.xml",
}
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
    cutoff = datetime.now(UTC) - timedelta(days=min(RADAR_DAYS, 7))
    out: list[dict] = []
    seen: set[str] = set()
    for q in NEWS_QUERIES:
        url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(f"{q} when:7d")
               + "&hl=en-US&gl=US&ceid=US:en")
        try:
            items = list(_feed_items(_get(url)))
        except Exception as e:
            log.warning("google news failed: %s", e)
            continue
        for title, link, when in items[:2]:
            key = title.lower()[:60]
            if when >= cutoff and key not in seen:
                seen.add(key)
                out.append(_sig("News", len(out), title, link, 0, when.strftime("%d %b")))
    for name, url in ENG_BLOGS.items():
        try:
            items = list(_feed_items(_get(url)))
        except Exception as e:
            log.warning("eng blog %s failed: %s", name, e)
            continue
        about_ai = [i for i in sorted(items, key=lambda i: i[2], reverse=True)
                    if AI_WORDS.search(i[0]) or name == "OpenAI news"]
        for title, link, when in about_ai[:2]:
            if when >= cutoff:
                out.append(_sig("Eng blog", len(out), f"{name}: {title}", link, 0, when.strftime("%d %b")))
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


AGENT_TOPICS = ("claude-code", "mcp-server", "agent-skills")
WIDE_TOPICS = ("rag", "llm-evaluation", "ocr", "ai-engineering", "llm-inference")   # 30 days, 300+ stars, one repo each


def github_agent_tools() -> list[dict]:
    """New repos a developer can try today. Agent topics (14 days, 200+ stars, two repos each) plus wider
    knowledge topics (30 days, 300+ stars, one repo each), most-starred first. One request per topic."""
    out: list[dict] = []
    seen: set[str] = set()
    headers = {"Accept": "application/vnd.github+json"}
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"
    plan = [(t, 14, 200, 2) for t in AGENT_TOPICS] + [(t, 30, 300, 1) for t in WIDE_TOPICS]
    for topic, days, min_stars, take in plan:
        since = time.strftime("%Y-%m-%d", time.gmtime(time.time() - days * 86400))
        stars = f"+stars:>{min_stars}" if days == 30 else ""
        url = (f"https://api.github.com/search/repositories?q=topic:{topic}+created:>{since}{stars}"
               "&sort=stars&order=desc&per_page=5")
        try:
            repos = json.loads(_get(url, headers)).get("items", [])
        except urllib.error.HTTPError as e:
            log.warning("github %s search failed: HTTP %s", topic, e.code)
            if e.code in (403, 429):      # rate limited: the remaining topics would fail the same way
                break
            continue
        except Exception as e:
            log.warning("github %s search failed: %s", topic, e)
            continue
        got = 0
        for r in repos if isinstance(repos, list) else []:
            desc = (r.get("description") or "").strip()
            if r["full_name"] in seen or not desc or r.get("stargazers_count", 0) < min_stars:
                continue
            seen.add(r["full_name"])
            out.append(_sig("GitHub new tools", len(out), f"{r['full_name']}: {desc}", r["html_url"],
                            r["stargazers_count"], f"{topic}, {r['stargazers_count']:,} stars"))
            got += 1
            if got >= take:
                break
    return out


COLLECTORS = [hn_lab_posts, hacker_news, show_hn, techmeme, reddit, github_trending, github_agent_tools,
              hf_trending_models, hf_daily_papers, news]


HN_KINDS = ("Hacker News", "HN lab post")


def collect(days: float | None = None) -> list[dict]:
    items = []
    seen_hn: set[str] = set()      # one HN story is one signal, whichever HN source found it first
    for fn in COLLECTORS:
        try:
            got = fn()
            log.info("%s: %d signals", fn.__name__, len(got))
            for s in got:
                if s["kind"] in HN_KINDS:
                    key = (s.get("url") or "").rstrip("/").lower()
                    if key in seen_hn:
                        continue
                    seen_hn.add(key)
                items.append(s)
        except Exception as e:  # network hiccup, rate limit, site change
            log.warning("%s failed: %s", fn.__name__, e)
    return items


# ======================= per-idea research (was research.py) =======================
# What people are already building and posting about a hot thing, plus its official docs, so
# build options copy what works and fill the gaps. Free, no keys; a blocked source returns
# nothing. Function names say "search" to keep them apart from the trend collectors above.

# Repos that only copy the original: not "something people built".
COPY_RE = re.compile(r"\b(mirror|backup|fork|copy of|clone of|archive|unofficial guide|reupload)\b", re.I)


def _q(s: str) -> str:
    return urllib.parse.quote(s)


def _text(node) -> str:
    """YouTube text node: {"simpleText": ...} or {"runs": [{"text": ...}]}."""
    if not isinstance(node, dict):
        return ""
    return node.get("simpleText") or "".join(r.get("text", "") for r in node.get("runs", []))


def _views(s: str) -> int:
    m = re.search(r"([\d,.]+)\s*([KMB]?)", s or "")
    if not m:
        return 0
    n = float(m.group(1).replace(",", ""))
    return int(n * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9}[m.group(2)])


def youtube_search(query: str, fetch=None, limit: int = 8) -> list[dict]:
    """Most-viewed videos uploaded this month."""
    url = f"https://www.youtube.com/results?search_query={_q(query)}&sp=CAMSBAgEEAE"
    html = (fetch or _get)(url, {"Accept-Language": "en"}).decode("utf-8", "replace")
    m = re.search(r"var ytInitialData = (\{.*?\});</script>", html, re.S)
    if not m:
        return []
    out: list[dict] = []

    def walk(node):
        if isinstance(node, dict):
            v = node.get("videoRenderer")
            if isinstance(v, dict) and v.get("videoId"):
                out.append({"title": _text(v.get("title"))[:120],
                            "views": _views(_text(v.get("viewCountText"))),
                            "channel": _text(v.get("ownerText"))[:60],
                            "age": _text(v.get("publishedTimeText")),
                            "url": f"https://www.youtube.com/watch?v={v['videoId']}"})
                return
            for x in node.values():
                walk(x)
        elif isinstance(node, list):
            for x in node:
                walk(x)
    walk(json.loads(m.group(1)))
    out.sort(key=lambda v: -v["views"])
    return out[:limit]


def github_search(query: str, exclude: str = "", fetch=None, days: int = 45, limit: int = 8) -> list[dict]:
    """New repos about the thing, most-starred first, without plain copies of the original."""
    since = time.strftime("%Y-%m-%d", time.gmtime(time.time() - days * 86400))
    url = (f"https://api.github.com/search/repositories?q={_q(query)}+created:>{since}"
           f"&sort=stars&order=desc&per_page=30")
    d = json.loads((fetch or _get)(url, {"Accept": "application/vnd.github+json"}))
    out = []
    for r in d.get("items", []):
        desc = (r.get("description") or "").strip()
        if r["full_name"].lower() == exclude.lower() or COPY_RE.search(desc + " " + r["full_name"]) or not desc:
            continue
        out.append({"title": f"{r['full_name']}: {desc[:110]}", "stars": r.get("stargazers_count", 0),
                    "url": r["html_url"]})
    return out[:limit]


def hn_search(query: str, fetch=None, days: int = 30, limit: int = 6) -> list[dict]:
    """Top threads whose title really names the thing (search alone matches loosely)."""
    since = int(time.time()) - days * 86400
    word = re.compile(r"\b" + r"\W*".join(map(re.escape, query.split())) + r"\b", re.I)
    url = (f"https://hn.algolia.com/api/v1/search?query={_q(query)}&tags=story"
           f"&numericFilters=created_at_i>{since}&hitsPerPage=40")
    hits = json.loads((fetch or _get)(url)).get("hits", [])
    hits = sorted((h for h in hits if (h.get("points") or 0) >= 5 and word.search(h.get("title") or "")),
                  key=lambda h: -(h.get("points") or 0))
    return [{"title": h["title"][:120], "points": h.get("points") or 0, "comments": h.get("num_comments") or 0,
             "url": f"https://news.ycombinator.com/item?id={h['objectID']}"} for h in hits[:limit]]


# ---------- LinkedIn posts through Apify (optional, free plan only) ----------
# DuckDuckGo and Bing no longer give LinkedIn results, so public posts come from an Apify actor that needs no
# LinkedIn login. It is OFF unless APIFY_TOKEN is set. It can never cost money: use Apify's free plan with no payment
# method on file (Apify then stops at the monthly credit), PostPilot stops itself at APIFY_MAX_POSTS_PER_MONTH, and
# every run carries Apify's own per-run charge limit. LinkedIn's terms forbid automated collection: your risk.

APIFY_ACTOR = "harvestapi~linkedin-post-search"      # pay per post (about $0.002); input fields read from its public schema
APIFY_RUN_CHARGE_LIMIT_USD = "0.05"                  # Apify refuses to charge a run above this
APIFY_DEFAULT_MONTHLY_POSTS = 500                    # about $1.00 of the $5 free credit
APIFY_DEFAULT_POSTS_PER_QUERY = 10


def _int_setting(name: str, default: int) -> int:
    try:
        return max(int(env(name, str(default)) or default), 0)
    except ValueError:
        return default


def _apify_key(prefix: str) -> str:
    return f"{prefix}:{datetime.now(UTC):%Y-%m}"


def apify_usage() -> tuple[int, int]:
    """(posts used this month, monthly cap). Counted here, so the cap holds even if Apify's own limit is higher."""
    used = int(db.get_meta(_apify_key("apify_posts"), "0") or 0)
    return used, _int_setting("APIFY_MAX_POSTS_PER_MONTH", APIFY_DEFAULT_MONTHLY_POSTS)


def apify_state() -> str:
    """One line for `status`: off, on with usage, or off for the month because the free credit ran out."""
    if not env("APIFY_TOKEN"):
        return "off (set APIFY_TOKEN in .env to turn on; free plan only)"
    if db.get_meta(_apify_key("apify_off")):
        return "off until next month (Apify said the free credit is used up)"
    if _apify_paused():
        left = max(int(float(db.get_meta("apify_pause_until", "0") or 0) - time.time()) // 60, 1)
        return f"paused for about {left} more min (Apify refused: too many runs at once or a limit)"
    used, cap = apify_usage()
    return f"on, {used} of {cap} posts used this month"


def _apify_paused() -> bool:
    return float(db.get_meta("apify_pause_until", "0") or 0) > time.time()


def _apify_error_type(e: urllib.error.HTTPError) -> str:
    """Apify's own name for why it refused (for example 'concurrent-runs-limit-exceeded'); '' when unreadable."""
    try:
        return str(json.loads(e.read().decode("utf-8", "replace"))["error"]["type"])[:80]
    except Exception:
        return ""


def _post_json(url: str, payload: dict, headers: dict, timeout: int = 150):
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json", "User-Agent": "postpilot/0.4", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _linkedin_query(text: str) -> str:
    """Plain words only: model-written names carry typographic hyphens and slashes that match nothing."""
    t = re.sub(r"[‐-―−_/-]+", " ", text or "")
    t = re.sub(r"[^\w\s.+#]", " ", t)
    return " ".join(t.split())[:80]


def _linkedin_row(item) -> dict | None:
    """One public post as a small plain dict, or None when it is not a post with text."""
    if not isinstance(item, dict) or item.get("type") != "post":
        return None
    raw = re.sub(r"\n{3,}", "\n\n", str(item.get("content") or "").replace("\r\n", "\n")).strip()
    text = " ".join(raw.split())
    if not text:
        return None
    author = item["author"] if isinstance(item.get("author"), dict) else {}
    eng = item["engagement"] if isinstance(item.get("engagement"), dict) else {}

    def count(key: str) -> int:
        v = eng.get(key)
        return v if isinstance(v, int) and not isinstance(v, bool) else 0

    url = str(item.get("linkedinUrl") or "")
    return {"title": text[:120], "url": url[:300] if url.startswith("https://www.linkedin.com/") else "",
            "author": str(author.get("name") or "")[:60],
            "likes": count("likes"), "comments": count("comments"), "shares": count("shares"), "text": text[:600],
            "raw": raw[:1800], "length": len(raw)}      # line breaks kept: the shape of a post is part of its style


def _engagement(row: dict) -> int:
    return row["likes"] + 2 * row["comments"] + row["shares"]


def linkedin_posts(query: str, post=None, limit: int = 5) -> list[dict]:
    """Public LinkedIn posts about `query` from the last month, most engaged first. [] when off, over the monthly
    cap, or on any error: research must never stop because this is unavailable."""
    rows = _apify_search(query, _int_setting("APIFY_POSTS_PER_QUERY", APIFY_DEFAULT_POSTS_PER_QUERY), "month", post)
    return sorted(rows, key=lambda r: -_engagement(r))[:limit]


def _only_the_owner(rows: list[dict]) -> list[dict]:
    """A profile's feed also holds posts the person reposted or was shown with. The author who wrote most of the
    run is the owner: keep only their own posts, so the examples are the style of the person you chose."""
    names = [r["author"] for r in rows if r["author"]]
    if not names:
        return rows
    owner = max(set(names), key=names.count)
    return [r for r in rows if r["author"] == owner]


def linkedin_examples(queries: list[str], per_query: int = 20, post=None, authors: list[str] | None = None) -> list[dict]:
    """Posts from the last year for several searches and/or several chosen authors (the one-time pull for the
    examples file), duplicates removed. Every run goes through the same cap and error handling as linkedin_posts."""
    seen: set[str] = set()
    out: list[dict] = []
    runs = [_only_the_owner(_apify_search("", per_query, "year", post, author=a)) for a in authors or []]
    runs += [_apify_search(q, per_query, "year", post) for q in queries]
    for rows in runs:
        for row in rows:
            key = row["url"] or row["text"][:80]
            if key not in seen:
                seen.add(key)
                out.append(row)
    return out


def _apify_search(query: str, per_query: int, posted_limit: str, post=None, author: str = "") -> list[dict]:
    """One capped Apify run: a keyword search, or the posts of ONE LinkedIn profile or company (`author`, its URL).
    One author per run, so the per-run charge limit also limits the cost of each author. Never raises; every
    returned item is counted against the monthly cap. `post` replaces the HTTP call in tests."""
    token = env("APIFY_TOKEN")
    q = _linkedin_query(query)
    if author and not author.startswith("https://www.linkedin.com/"):
        return []
    if not token or not (q or author) or db.get_meta(_apify_key("apify_off")) or _apify_paused():
        return []
    used, cap = apify_usage()
    want = min(per_query, cap - used)
    if want <= 0:                       # never send maxPosts 0: for this actor that means "scrape everything"
        log.info("LinkedIn search skipped: %d of %d posts used this month", used, cap)
        return []
    url = (f"https://api.apify.com/v2/acts/{APIFY_ACTOR}/run-sync-get-dataset-items"
           f"?maxTotalChargeUsd={APIFY_RUN_CHARGE_LIMIT_USD}")
    payload = ({"authorUrls": [author], "maxPosts": want, "postedLimit": posted_limit} if author else
               {"searchQueries": [q], "maxPosts": want, "postedLimit": posted_limit, "sortBy": "relevance"})
    counter = _apify_key("apify_posts")
    try:
        items = (post or _post_json)(url, payload, {"Authorization": f"Bearer {token}"})
    except urllib.error.HTTPError as e:
        if e.code in (402, 403):
            why = _apify_error_type(e)
            if re.search(r"(?i)usage|credit|insufficient", why):     # the plan's money is really gone
                db.set_meta(_apify_key("apify_off"), "1")
                log.warning("Apify says the free credit is used up (%s): LinkedIn search is off until next month", why)
            else:                       # too many runs at once, a plan limit, a token problem: wait, do not give up
                wait = 300 if "concurrent" in why else 3600
                db.set_meta("apify_pause_until", str(time.time() + wait))
                log.warning("Apify refused (HTTP %d %s): LinkedIn search paused for %d min", e.code, why or "?", wait // 60)
        else:
            log.warning("Apify LinkedIn search failed: HTTP %d", e.code)
            if e.code not in (401, 404, 429):
                db.add_meta_int(counter, want)      # a run may have been billed (408 wait ended, 400 run failed, 5xx)
        return []
    except Exception as e:
        db.add_meta_int(counter, want)              # a timeout may still have been billed
        log.warning("Apify LinkedIn search failed: %s", str(e)[:150])
        return []
    items = items if isinstance(items, list) else []
    db.add_meta_int(counter, max(len(items), 1))    # every returned item is billed; a search with no result costs a little too
    return [r for r in map(_linkedin_row, items) if r]


def web_search(query: str, fetch=None, limit: int = 6) -> list[dict]:
    """Backup when DuckDuckGo refuses: Bing's top results (guides, articles, posts), titles only."""
    url = "https://www.bing.com/search?setlang=en&q=" + _q(query)
    html = (fetch or _get)(url, {"Accept-Language": "en"}).decode("utf-8", "replace")
    out = []
    for _, title in re.findall(r'<h2[^>]*><a[^>]+href="(http[^"]+)"[^>]*>(.*?)</a>', html):
        t = re.sub(r"<.*?>", "", title).replace("&amp;", "&").replace("&#183;", "·").replace("&quot;", '"')
        out.append({"title": t[:120], "url": ""})
    return out[:limit]


README_KEYS = re.compile(r"quick ?start|install|setup|getting started|usage|how .* works|how to|use\b|"
                        r"cursor|mcp|configur|example|what you can|requirements", re.I)


def readme(repo: str, fetch=None, limit: int = 5000) -> str:
    """The parts of a GitHub repo's README that say how to install and use it, as plain text."""
    raw = (fetch or _get)(f"https://api.github.com/repos/{repo}/readme",
                                  {"Accept": "application/vnd.github.raw"}).decode("utf-8", "replace")
    return clean_markdown(raw, limit)


def model_card(repo: str, fetch=None, limit: int = 5000) -> str:
    """A Hugging Face model card (README.md): what the model is for, how to call it, its limits."""
    raw = (fetch or _get)(f"https://huggingface.co/{repo}/raw/main/README.md").decode("utf-8", "replace")
    raw = re.sub(r"\A---\n.*?\n---\n", "", raw, flags=re.S)        # yaml header
    return clean_markdown(raw, limit)


def page_text(url: str, fetch=None, limit: int = 5000) -> str:
    """Readable text of an official page (product docs, blog post, paper abstract)."""
    html = (fetch or _get)(url).decode("utf-8", "replace")
    html = re.sub(r"(?is)<(script|style|nav|footer|header|svg)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;|&#160;", " ", text).replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'")
    return re.sub(r"\s+", " ", text).strip()[:limit]


# Sources that are discussion or aggregation, not the official page of the thing.
NOT_OFFICIAL = re.compile(r"news\.google|reddit\.com|ycombinator\.com|techmeme\.com|youtube\.com|x\.com|"
                          r"twitter\.com|linkedin\.com")


def docs_for(idea: dict, fetch=None) -> tuple[str, str]:
    """(docs text, where it came from) for whatever the hot thing is: a GitHub repo's README, a
    model's Hugging Face card, or the official page among the story's sources."""
    kind, handle = idea.get("handle_type", ""), (idea.get("handle") or "").strip()
    urls = idea.get("source_urls", [])
    tries: list[tuple[str, Callable[[], str]]] = []
    if kind == "github_repo" or (re.fullmatch(r"[\w.-]+/[\w.-]+", handle) and kind != "model"):
        tries.append((f"README of github.com/{handle}", lambda: readme(handle, fetch)))
    if kind == "model":
        hf = [u.split("huggingface.co/")[1].strip("/") for u in urls if "huggingface.co/" in u]
        if re.fullmatch(r"[\w.-]+/[\w.-]+", handle):
            hf.insert(0, handle)
        for repo in hf[:2]:
            tries.append((f"Hugging Face model card {repo}", partial(model_card, repo, fetch)))
    for u in [u for u in urls if not NOT_OFFICIAL.search(u)][:2]:
        if "github.com/" in u and kind != "github_repo":
            repo = "/".join(u.split("github.com/")[1].split("/")[:2])
            tries.append((f"README of github.com/{repo}", partial(readme, repo, fetch)))
        elif "huggingface.co/" not in u:
            tries.append((f"official page {u}", partial(page_text, u, fetch)))
    for label, job in tries:
        try:
            text = job()
        except Exception as e:
            log.info("docs from %s failed: %s", label, e)
            continue
        if len(text) > 300:
            return text, label
    return "", ""


def clean_markdown(raw: str, limit: int = 5000) -> str:
    """Keep the intro plus the install / usage / how-it-works sections of a README or model card."""
    text = re.sub(r"<!--.*?-->", "", raw, flags=re.S)
    text = re.sub(r"<[^>]+>", "", text)                           # html tags
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)              # images / badges
    text = re.sub(r"\[!\[.*?\]\(.*?\)\]\(.*?\)", "", text)
    lines = [ln.rstrip() for ln in text.splitlines()]
    sections: list[list] = []
    cur: list = ["", []]
    for ln in lines:
        if re.match(r"^#{1,4}\s", ln):
            sections.append(cur)
            cur = [ln.lstrip("#").strip(), []]
        else:
            cur[1].append(ln)
    sections.append(cur)
    def body(sec):
        return re.sub(r"\n{3,}", "\n\n", "\n".join(sec[1])).strip()
    if not body(sections[0]) and len(sections) > 1:      # README starts with a title: its first section is the intro
        sections = [sections[1], *sections[2:]]
    intro = body(sections[0])[:800]
    keep = [f"## {h}\n{body(sec)}" for h, *rest in sections[1:] for sec in [(h, *rest)]
            if README_KEYS.search(h) and body(sec)]
    out = (intro + "\n\n" + "\n\n".join(keep)).strip()
    return out[:limit]


def research(idea: dict, fetch=None, post=None) -> dict:
    """Search every source for the idea's hot thing. Returns lists per source (may be empty).
    `fetch` replaces GET requests and `post` the Apify call (tests)."""
    handle = (idea.get("handle") or "").strip()
    name = handle.replace("/", " ") if handle else (idea.get("entities") or [idea.get("topic", "")])[0]
    short = handle.split("/")[-1] if "/" in handle else name
    jobs = {
        "videos": lambda: youtube_search(name, fetch),
        "repos": lambda: github_search(short if "/" in handle else name, handle, fetch),
        "hn": lambda: hn_search(short if "/" in handle else name, fetch),
        "linkedin": lambda: linkedin_posts(name, post=post),
    }
    out: dict = {"query": name, "x": []}        # X has no free search any more; the key stays for saved batches
    for key, job in jobs.items():
        out[key] = []
        for attempt in range(2):            # one retry: a single dropped connection is common
            try:
                out[key] = job()
                break
            except Exception as e:
                log.info("research %s for %r failed (try %d): %s", key, name, attempt + 1, e)
        log.info("research %s: %d results for %r", key, len(out[key]), name)
    out["docs"], out["docs_from"] = docs_for(idea, fetch)
    log.info("research docs: %d chars from %s", len(out["docs"]), out["docs_from"] or "nowhere")
    if not out["linkedin"]:
        try:
            out["web"] = web_search(f"{name} guide OR tutorial OR built OR tested", fetch)
        except Exception as e:
            log.info("research web for %r failed: %s", name, e)
            out["web"] = []
    return out


def research_digest(r: dict) -> str:
    """Compact, model-readable digest."""
    lines = []
    if r.get("videos"):
        lines.append("Most-viewed YouTube videos this month:")
        lines += [f"- {v['views']:,} views: {v['title']} ({v['channel']}, {v['age']})" for v in r["videos"]]
    if r.get("repos"):
        lines.append("New GitHub repos people built around it (stars):")
        lines += [f"- {g['stars']:,} stars: {g['title']}" for g in r["repos"]]
    if r.get("hn"):
        lines.append("Top Hacker News threads:")
        lines += [f"- {h['points']} points, {h['comments']} comments: {h['title']}" for h in r["hn"]]
    if r.get("linkedin"):
        lines.append("LinkedIn posts about it (what people already said: pick a fresh angle, never copy):")
        lines += [_linkedin_line(p) for p in r["linkedin"]]
    for key, label in (("x", "X posts"), ("web", "Web posts and guides")):
        if r.get(key):
            lines.append(f"{label} about it:")
            lines += [f"- {p['title']}" for p in r[key]]
    return "\n".join(lines) or "(no research results)"


def _linkedin_line(p: dict) -> str:
    if "likes" not in p:                # a saved batch from before the Apify search: title only
        return f"- {p['title']}"
    who = p.get("author") or "someone"
    return f"- {p['likes']} likes, {p['comments']} comments, {who}: \"{p['text'][:260]}\""


def research_titles(r: dict) -> list[str]:
    return [x["title"] for k in ("videos", "repos", "hn", "linkedin", "x", "web") for x in r.get(k, [])]
