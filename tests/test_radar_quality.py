"""New free collectors (faked network) and the content ranking rules, each with one up and one down case."""

import json
import types

import pytest

from postpilot import ranking, sources


def serve(payload):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return lambda url, headers=None: body


def sig(kind, title, eng=0, url=""):
    return {"kind": kind, "rank": 0, "title": title, "engagement": eng, "detail": "d", "url": url}


def heat(*signals):
    """Heat as the pipeline combines it: measured attention plus the content adjustment."""
    t, sg = {"signal_ids": list(range(len(signals)))}, list(signals)
    raw = ranking.heat(t, sg)[0]
    return max(raw + ranking.content_adjust(t, sg, None, raw), 0.0)


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


# ---------- ranking ----------

def test_new_kinds_have_sane_scales():
    assert 3 < heat(sig("Paper", "A paper", 100)) < 10 and heat(sig("Paper", "A paper", 5000)) <= 13
    assert heat(sig("HN lab post", "x", 500)) <= 19 and heat(sig("HN lab post", "x", 500)) > heat(sig("HN lab post", "x", 60))


def hn(title, kind="Hacker News"):
    return sig(kind, title, 500)          # enough raw heat (>= 10) for the positive adjustments to apply


def test_lab_how_to_up():
    base = heat(hn("Anthropic: Claude Code is here", "HN lab post"))
    assert heat(hn("Anthropic: how we built Claude Code", "HN lab post")) == base + 5
    assert heat(hn("Randomco: how we built a thing", "HN lab post")) == base       # not a lab
    assert heat(sig("Eng blog", "Anthropic: how we built Claude Code")) == heat(sig("Eng blog", "Anthropic: Claude Code"))  # low raw heat


def test_numbers_up_and_none_down():
    assert heat(hn("Model is 40% faster")) == heat(hn("Model is faster")) + 2
    assert heat(hn("Pays $5 per million tokens")) > heat(hn("Pays tokens"))
    assert heat(hn("Costs $5 a month")) == heat(hn("Costs a month"))     # a bare $ is not a number


def test_free_local_up_and_remote_down():
    assert heat(hn("Run it locally, no API key")) == heat(hn("Run it remotely")) + 2


def test_money_news_down():
    assert heat(sig("News", "Startup raises $2 billion in funding")) < heat(sig("News", "Startup ships a tool"))
    assert heat(sig("News", "This raises an exception")) == heat(sig("News", "This is fine"))
    assert heat(sig("News", "A 37 billion active parameters model")) == heat(sig("News", "A big model"))


def test_hn_argument_down_unless_there_is_an_artifact():
    arg = heat(sig("Hacker News", "Ask HN: is AI a bubble?", 300))
    assert arg < heat(sig("Hacker News", "Show HN: a bubble tracker", 300))
    assert heat(sig("Hacker News", "Ask HN: best MCP server?", 300)) > arg


def test_junk_hf_models_lose_bonus_and_good_ones_keep_it():
    junk = {"handle_type": "model", "handle": "u/Llama-3-GGUF", "heat": 20}
    assert ranking.builder_bonus(junk) == -8.0
    assert ranking.builder_bonus({**junk, "handle": "u/Qwen3-coder"}) == 0.0
    vlm = {"handle_type": "model", "handle": "u/Flagship-VL", "handle_facts": "u/Flagship-VL (image-text-to-text)"}
    assert ranking.builder_bonus(vlm) == 0.0                                  # a VLM is not an image generator
    assert ranking.builder_bonus({**vlm, "handle_facts": "u/x (text-to-image)"}) == -8.0
    assert ranking.builder_bonus({**vlm, "name": "x merge"}) == 0.0           # the topic name is not read


def test_leaked_prompt_and_prompt_lists_down():
    t = {"handle_type": "github_repo", "handle": "x/awesome-chatgpt-prompts", "heat": 15}
    assert ranking.builder_bonus(t) < 0 and ranking.builder_bonus({**t, "handle": "x/rag-toolkit"}) == 4.0
    assert heat(sig("GitHub trending", "x/leaked-system-prompts: all", 5000)) == heat(
        sig("GitHub trending", "x/some-tool: all", 5000))                    # penalised once, in builder_bonus


def test_wider_dev_tool_and_big_orgs():
    r = {"handle_type": "github_repo", "heat": 12.0}
    assert ranking.builder_bonus({**r, "handle": "randomdev/ocr-toolkit"}) == 4.0
    assert ranking.builder_bonus({**r, "handle": "airbnb/notes"}) == 5.0
    assert ranking.builder_bonus({**r, "handle": "randomdev/notes"}) == 0.0


def test_google_news_how_to_gets_no_lab_bonus_but_lab_feed_does():
    def news(title):
        return {"kind": "News", "rank": 0, "title": title, "engagement": 500, "detail": "d", "url": ""}
    plain = ranking.content_adjust({"signal_ids": [0]}, [news("OpenAI hires a person")], None, 20.0)
    assert ranking.content_adjust({"signal_ids": [0]}, [news("OpenAI hires a guide for lawyers")], None, 20.0) == plain
    assert ranking.content_adjust({"signal_ids": [0]}, [news("OpenAI news: a guide to Codex")], None, 20.0) == plain + 5


def test_adjust_gated_capped_and_best_signal_only():
    t, s = {"signal_ids": [0, 1]}, [sig("Eng blog", "Anthropic: how we run locally, 40% faster"), sig("News", "x")]
    assert ranking.content_adjust(t, s, None, 5.0) == 0.0                     # raw heat too low for a bonus
    assert ranking.content_adjust(t, s, None, 20.0) == 6.0                    # 5+2+2 capped at +6
    t2, s2 = {"signal_ids": [0, 1]}, [sig("News", "Plain", 100), sig("News", "Run it locally, free", 1)]
    assert ranking.content_adjust(t2, s2, None, 20.0) == 0.0                  # only the best signal's text counts


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
