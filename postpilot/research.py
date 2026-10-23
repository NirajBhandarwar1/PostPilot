"""What people are already building and posting about a hot thing, so build options copy what
works and fill the gaps, instead of being invented from nothing.

Free, no keys: YouTube search (most viewed this month), GitHub search (new repos about it, by
stars), Hacker News (top threads), and LinkedIn / X posts found through DuckDuckGo.
Every source is optional: a blocked one just returns nothing."""

import json
import logging
import re
import time
import urllib.parse

from . import sources

log = logging.getLogger("postpilot.research")

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


def youtube(query: str, fetch=None, limit: int = 8) -> list[dict]:
    """Most-viewed videos uploaded this month."""
    url = f"https://www.youtube.com/results?search_query={_q(query)}&sp=CAMSBAgEEAE"
    html = (fetch or sources._get)(url, {"Accept-Language": "en"}).decode("utf-8", "replace")
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


def github(query: str, exclude: str = "", fetch=None, days: int = 45, limit: int = 8) -> list[dict]:
    """New repos about the thing, most-starred first, without plain copies of the original."""
    since = time.strftime("%Y-%m-%d", time.gmtime(time.time() - days * 86400))
    url = (f"https://api.github.com/search/repositories?q={_q(query)}+created:>{since}"
           f"&sort=stars&order=desc&per_page=30")
    d = json.loads((fetch or sources._get)(url, {"Accept": "application/vnd.github+json"}))
    out = []
    for r in d.get("items", []):
        desc = (r.get("description") or "").strip()
        if r["full_name"].lower() == exclude.lower() or COPY_RE.search(desc + " " + r["full_name"]) or not desc:
            continue
        out.append({"title": f"{r['full_name']}: {desc[:110]}", "stars": r.get("stargazers_count", 0),
                    "url": r["html_url"]})
    return out[:limit]


def hacker_news(query: str, fetch=None, days: int = 30, limit: int = 6) -> list[dict]:
    """Top threads whose title really names the thing (search alone matches loosely)."""
    since = int(time.time()) - days * 86400
    word = re.compile(r"\b" + r"\W*".join(map(re.escape, query.split())) + r"\b", re.I)
    url = (f"https://hn.algolia.com/api/v1/search?query={_q(query)}&tags=story"
           f"&numericFilters=created_at_i>{since}&hitsPerPage=40")
    hits = json.loads((fetch or sources._get)(url)).get("hits", [])
    hits = sorted((h for h in hits if (h.get("points") or 0) >= 5 and word.search(h.get("title") or "")),
                  key=lambda h: -(h.get("points") or 0))
    return [{"title": h["title"][:120], "points": h.get("points") or 0, "comments": h.get("num_comments") or 0,
             "url": f"https://news.ycombinator.com/item?id={h['objectID']}"} for h in hits[:limit]]


def social(query: str, site: str, fetch=None, limit: int = 6) -> list[dict]:
    """Public LinkedIn / X posts about it, via DuckDuckGo's HTML results (titles only)."""
    url = "https://html.duckduckgo.com/html/?q=" + _q(f"site:{site} {query}")
    html = (fetch or sources._get)(url).decode("utf-8", "replace")
    out = []
    for href, title in re.findall(r'class="result__a" href="(.*?)">(.*?)</a>', html):
        t = re.sub(r"<.*?>", "", title)
        t = re.sub(r"&#x27;|&#39;", "'", t).replace("&amp;", "&").replace("&quot;", '"')
        real = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("uddg", [href])[0]
        out.append({"title": t[:120], "url": real})
    return out[:limit]


def web(query: str, fetch=None, limit: int = 6) -> list[dict]:
    """Backup when DuckDuckGo refuses: Bing's top results (guides, articles, posts), titles only."""
    url = "https://www.bing.com/search?setlang=en&q=" + _q(query)
    html = (fetch or sources._get)(url, {"Accept-Language": "en"}).decode("utf-8", "replace")
    out = []
    for _, title in re.findall(r'<h2[^>]*><a[^>]+href="(http[^"]+)"[^>]*>(.*?)</a>', html):
        t = re.sub(r"<.*?>", "", title).replace("&amp;", "&").replace("&#183;", "·").replace("&quot;", '"')
        out.append({"title": t[:120], "url": ""})
    return out[:limit]


README_KEYS = re.compile(r"quick ?start|install|setup|getting started|usage|how .* works|how to|use\b|"
                        r"cursor|mcp|configur|example|what you can|requirements", re.I)


def readme(repo: str, fetch=None, limit: int = 5000) -> str:
    """The parts of a GitHub repo's README that say how to install and use it, as plain text."""
    raw = (fetch or sources._get)(f"https://api.github.com/repos/{repo}/readme",
                                  {"Accept": "application/vnd.github.raw"}).decode("utf-8", "replace")
    return clean_markdown(raw, limit)


def model_card(repo: str, fetch=None, limit: int = 5000) -> str:
    """A Hugging Face model card (README.md): what the model is for, how to call it, its limits."""
    raw = (fetch or sources._get)(f"https://huggingface.co/{repo}/raw/main/README.md").decode("utf-8", "replace")
    raw = re.sub(r"\A---\n.*?\n---\n", "", raw, flags=re.S)        # yaml header
    return clean_markdown(raw, limit)


def page_text(url: str, fetch=None, limit: int = 5000) -> str:
    """Readable text of an official page (product docs, blog post, paper abstract)."""
    html = (fetch or sources._get)(url).decode("utf-8", "replace")
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
        "videos": lambda: youtube(name, fetch),
        "repos": lambda: github(short if "/" in handle else name, handle, fetch),
        "hn": lambda: hacker_news(short if "/" in handle else name, fetch),
        "linkedin": lambda: social(name, "linkedin.com/posts", fetch),
        "x": lambda: social(name, "x.com", fetch),
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
            out["web"] = web(f"{name} guide OR tutorial OR built OR tested", fetch)
        except Exception as e:
            log.info("research web for %r failed: %s", name, e)
            out["web"] = []
    return out


def as_text(r: dict) -> str:
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


def titles(r: dict) -> list[str]:
    return [x["title"] for k in ("videos", "repos", "hn", "linkedin", "x", "web") for x in r.get(k, [])]
