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
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from .config import env, settings

log = logging.getLogger("postpilot.sources")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) postpilot/0.3"
RADAR_DAYS = float(env("RADAR_DAYS", "10"))   # viral things stay hot for 1-3 weeks
AI_WORDS = re.compile(r"\b(AI|A\.I\.|LLM|GPT|model|models|agent|agents|Claude|Gemini|OpenAI|Anthropic|"
                      r"DeepSeek|Qwen|Llama|Mistral|Muse|Grok|Copilot|chatbot|neural|diffusion|"
                      r"open[- ]source|open[- ]weight|inference|benchmark|AGI|robot)\b", re.I)


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
    return all(ipaddress.ip_address(a.split("%")[0]).is_global for a in addrs)


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
    out = []

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


def social_search(query: str, site: str, fetch=None, limit: int = 6) -> list[dict]:
    """Public LinkedIn / X posts about it, via DuckDuckGo's HTML results (titles only)."""
    url = "https://html.duckduckgo.com/html/?q=" + _q(f"site:{site} {query}")
    html = (fetch or _get)(url).decode("utf-8", "replace")
    out = []
    for href, title in re.findall(r'class="result__a" href="(.*?)">(.*?)</a>', html):
        t = re.sub(r"<.*?>", "", title)
        t = re.sub(r"&#x27;|&#39;", "'", t).replace("&amp;", "&").replace("&quot;", '"')
        real = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("uddg", [href])[0]
        out.append({"title": t[:120], "url": real})
    return out[:limit]


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
    tries = []
    if kind == "github_repo" or (re.fullmatch(r"[\w.-]+/[\w.-]+", handle) and kind != "model"):
        tries.append((f"README of github.com/{handle}", lambda: readme(handle, fetch)))
    if kind == "model":
        hf = [u.split("huggingface.co/")[1].strip("/") for u in urls if "huggingface.co/" in u]
        if re.fullmatch(r"[\w.-]+/[\w.-]+", handle):
            hf.insert(0, handle)
        for repo in hf[:2]:
            tries.append((f"Hugging Face model card {repo}", lambda r=repo: model_card(r, fetch)))
    for u in [u for u in urls if not NOT_OFFICIAL.search(u)][:2]:
        if "github.com/" in u and kind != "github_repo":
            repo = "/".join(u.split("github.com/")[1].split("/")[:2])
            tries.append((f"README of github.com/{repo}", lambda r=repo: readme(r, fetch)))
        elif "huggingface.co/" not in u:
            tries.append((f"official page {u}", lambda u=u: page_text(u, fetch)))
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
    sections, cur = [], ["", []]
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


def research(idea: dict, fetch=None) -> dict:
    """Search every source for the idea's hot thing. Returns lists per source (may be empty)."""
    handle = (idea.get("handle") or "").strip()
    name = handle.replace("/", " ") if handle else (idea.get("entities") or [idea.get("topic", "")])[0]
    short = handle.split("/")[-1] if "/" in handle else name
    jobs = {
        "videos": lambda: youtube_search(name, fetch),
        "repos": lambda: github_search(short if "/" in handle else name, handle, fetch),
        "hn": lambda: hn_search(short if "/" in handle else name, fetch),
        "linkedin": lambda: social_search(name, "linkedin.com/posts", fetch),
        "x": lambda: social_search(name, "x.com", fetch),
    }
    out = {"query": name}
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
    if not out["linkedin"] and not out["x"]:
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
    for key, label in (("linkedin", "LinkedIn posts"), ("x", "X posts"), ("web", "Web posts and guides")):
        if r.get(key):
            lines.append(f"{label} about it:")
            lines += [f"- {p['title']}" for p in r[key]]
    return "\n".join(lines) or "(no research results)"


def research_titles(r: dict) -> list[str]:
    return [x["title"] for k in ("videos", "repos", "hn", "linkedin", "x", "web") for x in r.get(k, [])]
