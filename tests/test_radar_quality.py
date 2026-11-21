"""New free collectors (faked network) and the content ranking rules, each with one up and one down case."""

import json
import types

import pytest

from postpilot import ranking, sources


def serve(payload):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return lambda url, headers=None: body


# ---------- collectors ----------

def test_daily_papers_keep_top_five_by_upvotes(monkeypatch):
    data = [{"paper": {"id": f"2601.{n}", "title": f"Paper {n}", "upvotes": n}} for n in range(10)]
    monkeypatch.setattr(sources, "_get", serve(data))
    got = sources.hf_daily_papers()
    assert [g["engagement"] for g in got] == [9, 8, 7, 6, 5] and got[0]["kind"] == "Paper"
    assert got[0]["url"] == "https://huggingface.co/papers/2601.9"


def test_daily_papers_empty_and_garbage(monkeypatch):
    for bad in ([], {"error": "x"}, [None, 3, {"paper": {}}]):
        monkeypatch.setattr(sources, "_get", serve(bad))
        assert sources.hf_daily_papers() == []
    monkeypatch.setattr(sources, "_get", serve([{"paper": {"id": "1", "title": "t", "upvotes": "many"}}]))
    assert sources.hf_daily_papers()[0]["engagement"] == 0
    monkeypatch.setattr(sources, "_get", serve(b"not json"))
    with pytest.raises(ValueError):                       # collect() catches this
        sources.hf_daily_papers()


def test_show_hn_keeps_only_ai_titles(monkeypatch):
    hits = [{"objectID": "1", "title": "Show HN: An MCP server for Postgres", "points": 90, "num_comments": 4},
            {"objectID": "2", "title": "Show HN: My todo app", "points": 200}]
    monkeypatch.setattr(sources, "_get", serve({"hits": hits}))
    got = sources.show_hn()
    assert [g["title"] for g in got] == ["Show HN: An MCP server for Postgres"]
    assert got[0]["engagement"] == 90 and got[0]["url"].endswith("item?id=1")
    for bad in ({"hits": []}, {"hits": "x"}, {"hits": [None, {"title": "x"}]}):
        monkeypatch.setattr(sources, "_get", serve(bad))
        assert sources.show_hn() == []


def test_hn_lab_posts_use_two_requests_and_dedupe(monkeypatch):
    urls = []

    def get(url, headers=None):
        urls.append(url)
        hits = [{"objectID": "7", "title": "Building effective agents", "points": 400, "url": "https://anthropic.com/x"}]
        if "engineering" in url:
            hits += [{"objectID": "8", "title": "A cache for Redis", "points": 300}]
        return json.dumps({"hits": hits}).encode()
    monkeypatch.setattr(sources, "_get", get)
    got = sources.hn_lab_posts()
    assert len(urls) == 2 and [g["title"] for g in got] == ["Building effective agents"]
    assert got[0]["kind"] == "HN lab post"


def test_hn_lab_posts_survive_failure_and_garbage(monkeypatch):
    monkeypatch.setattr(sources, "_get", serve(b"garbage"))
    assert sources.hn_lab_posts() == []
    monkeypatch.setattr(sources, "_get", serve({"hits": []}))
    assert sources.hn_lab_posts() == []


def test_reddit_subreddits_replaced_singularity(monkeypatch):
    seen = []

    def get(url, headers=None):
        seen.append(url)
        raise OSError("429")
    monkeypatch.setattr(sources, "_get", get)
    monkeypatch.setattr(sources.time, "sleep", lambda s: None)
    assert sources.reddit() == []
    assert {u.split("/r/")[1].split("/")[0] for u in seen} == {"ClaudeAI", "LLMDevs", "MachineLearning", "LocalLLaMA"}


def test_wide_github_topics_use_30_days_and_300_stars(monkeypatch):
    urls = []

    def get(url, headers=None):
        urls.append(url)
        return json.dumps({"items": [{"full_name": f"o/r{len(urls)}", "description": "d",
                                      "stargazers_count": 350, "html_url": "u"}]}).encode()
    monkeypatch.setattr(sources, "_get", get)
    got = sources.github_agent_tools()
    wide = [u for u in urls if "stars:>300" in u]
    assert len(wide) == len(sources.WIDE_TOPICS) and len(got) == len(urls)
    monkeypatch.setattr(sources, "_get", serve({"items": None}))
    assert sources.github_agent_tools() == []
    monkeypatch.setattr(sources, "_get", serve({}))
    assert sources.github_agent_tools() == []


def test_new_collectors_registered():
    for fn in (sources.hf_daily_papers, sources.show_hn, sources.hn_lab_posts):
        assert fn in sources.COLLECTORS


def test_collect_drops_same_hn_story_from_two_sources(monkeypatch):
    one = lambda kind: (lambda: [sources._sig(kind, 0, "Same", "https://news.ycombinator.com/item?id=1", 100)])  # noqa: E731
    monkeypatch.setattr(sources, "COLLECTORS", [one("HN lab post"), one("Hacker News")])
    got = sources.collect()
    assert len(got) == 1 and got[0]["kind"] == "HN lab post"
    assert sources.COLLECTORS is not None and sources.hn_lab_posts is not None


def test_github_stops_on_rate_limit_and_sends_token(monkeypatch):
    import urllib.error
    calls = []

    def get(url, headers=None):
        calls.append(headers)
        raise urllib.error.HTTPError(url, 403, "limit", None, None)
    monkeypatch.setattr(sources, "_get", get)
    monkeypatch.setattr(sources, "settings", types.SimpleNamespace(github_token="tok"))
    assert sources.github_agent_tools() == [] and len(calls) == 1
    assert calls[0]["Authorization"] == "Bearer tok"


def test_paper_title_newlines_collapsed():
    assert sources._sig("Paper", 0, "A\n  long\ntitle", "u")["title"] == "A long title"
