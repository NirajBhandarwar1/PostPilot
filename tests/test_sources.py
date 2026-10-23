"""Scraper tests (eng review R2): each trend source parses a saved sample of its real response,
and a broken page yields 0 signals without crashing the run."""

import json

import pytest

from postpilot import sources

HN_JSON = json.dumps({"hits": [
    {"objectID": "1", "title": "DeepInfer: high-performance inference for DeepSeek", "url": "https://github.com/x/deepinfer",
     "points": 2293, "num_comments": 879},
    {"objectID": "2", "title": "PHP2Shell: AI uncovers critical RCE in PHP", "url": "https://php.net/x",
     "points": 412, "num_comments": 156},
    {"objectID": "3", "title": "Show HN: my sourdough timer", "url": "https://bread.example",
     "points": 300, "num_comments": 20},
]})

TECHMEME_RSS = """<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>Techmeme</title>
<item><title>Recursive releases Flint, an open-weight 40B coding model (Will Knight/Wired)</title>
<link>https://www.techmeme.com/261021/p1</link><pubDate>{now}</pubDate></item>
<item><title>EA plans to cut ~400 jobs (Jason Schreier/Bloomberg)</title>
<link>https://www.techmeme.com/261021/p2</link><pubDate>{now}</pubDate></item>
</channel></rss>"""

REDDIT_ATOM = """<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom">
<title>top scoring links : LocalLLaMA</title>
<entry><title>DeepSeek V4.1 Flash is wild</title><link href="https://www.reddit.com/r/LocalLLaMA/comments/a/x/"/></entry>
<entry><title>Muse Spark Open benchmarks</title><link href="https://www.reddit.com/r/LocalLLaMA/comments/b/y/"/></entry>
</feed>"""

GITHUB_HTML = """<html><body>
<article class="Box-row">
  <a href="/login?return_to=%2Fmorluto%2Frea" rel="nofollow">Star</a>
  <h2 class="h3 lh-condensed"><a href="/morluto/rea">morluto / rea</a></h2>
  <p class="col-9 color-fg-muted my-1 pr-4">Reverse engineer anything with <em>agents</em></p>
  <span>229,268 stars this week</span>
</article>
<article class="Box-row">
  <a href="/login?return_to=%2Fanthropics%2Fclaude-plugins-official" rel="nofollow">Star</a>
  <p class="col-9 color-fg-muted my-1 pr-4">Official plugin directory</p>
  <span>4,774 stars this week</span>
</article>
</body></html>"""

GNEWS_RSS = """<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>
<item><title>Google launches Gemini 4 Argon - Reuters</title><link>https://news.google.com/rss/articles/abc</link>
<pubDate>{now}</pubDate></item>
<item><title>Old story - Example</title><link>https://news.google.com/rss/articles/old</link>
<pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate></item>
</channel></rss>"""

HF_JSON = json.dumps([
    {"id": "deepseek-ai/DeepSeek-V4.1-Flash-1017", "pipeline_tag": "image-text-to-text", "trendingScore": 1484, "likes": 2136},
    {"id": "meta-models/Muse-Spark-Open-816B-A42B", "pipeline_tag": "text-generation", "trendingScore": 529, "likes": 632},
])


@pytest.fixture
def now_rfc822():
    from email.utils import format_datetime
    from datetime import datetime, timezone
    return format_datetime(datetime.now(timezone.utc))


@pytest.fixture
def fake_web(monkeypatch, now_rfc822):
    """Route sources._get by URL to the saved samples; no sleeping."""
    pages = {
        "hn.algolia.com": HN_JSON,
        "techmeme.com": TECHMEME_RSS.format(now=now_rfc822),
        "reddit.com": REDDIT_ATOM,
        "github.com/trending": GITHUB_HTML,
        "news.google.com": GNEWS_RSS.format(now=now_rfc822),
        "huggingface.co/api/models": HF_JSON,
    }
    calls = []

    def get(url, headers=None):
        calls.append(url)
        for key, body in pages.items():
            if key in url:
                if isinstance(body, Exception):
                    raise body
                return body.encode()
        raise OSError(f"no sample for {url}")
    monkeypatch.setattr(sources, "_get", get)
    monkeypatch.setattr(sources.time, "sleep", lambda s: None)
    return pages


def test_hacker_news_by_points(fake_web):
    got = sources.hacker_news()
    titles = [s["title"] for s in got]
    assert titles[0].startswith("DeepInfer") and got[0]["engagement"] == 2293
    assert "2293 points, 879 comments" in got[0]["detail"]
    assert not any("sourdough" in t for t in titles)          # not an AI story
    assert len(titles) == len(set(titles))                    # deduped across queries


def test_techmeme_keeps_ai_and_strips_byline(fake_web):
    got = sources.techmeme()
    assert [s["title"] for s in got] == ["Recursive releases Flint, an open-weight 40B coding model"]


def test_reddit_atom(fake_web):
    got = sources.reddit()
    assert got and got[0]["title"].startswith("r/") and got[0]["url"].startswith("https://www.reddit.com/")
    assert got[0]["detail"].endswith("#1")


def test_github_trending_html(fake_web):
    got = sources.github_trending()
    assert got[0]["title"] == "morluto/rea: Reverse engineer anything with agents"
    assert got[0]["engagement"] == 229268
    assert got[1]["title"].startswith("anthropics/claude-plugins-official")


def test_google_news_drops_old_items(fake_web):
    got = sources.news()
    titles = [s["title"] for s in got]
    assert any("Gemini 4 Argon" in t for t in titles)
    assert not any("Old story" in t for t in titles)


def test_hf_trending(fake_web):
    got = sources.hf_trending_models()
    assert got[0]["title"] == "deepseek-ai/DeepSeek-V4.1-Flash-1017 (image-text-to-text)"
    assert got[0]["detail"].startswith("HF trending #1")


@pytest.mark.parametrize("site", ["techmeme.com", "github.com/trending", "huggingface.co/api/models",
                                  "hn.algolia.com", "reddit.com", "news.google.com"])
def test_broken_page_gives_zero_signals_not_a_crash(fake_web, site):
    fake_web[site] = "<html>Something went wrong</html>"
    got = sources.collect()                      # the whole run survives one broken source
    kinds = {s["kind"] for s in got}
    assert got, "other sources still return signals"
    expected_missing = {"techmeme.com": "Techmeme", "github.com/trending": "GitHub trending",
                        "huggingface.co/api/models": "Hugging Face trending models",
                        "hn.algolia.com": "Hacker News", "reddit.com": "Reddit",
                        "news.google.com": None}[site]
    if expected_missing:
        assert expected_missing not in kinds
