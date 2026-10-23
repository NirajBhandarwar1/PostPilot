"""Research step: parse what's viral about the hot thing (YouTube, GitHub, HN, social), offline."""

import json

from postpilot import research

YT = """<html><script>var ytInitialData = {"contents": {"x": [
 {"videoRenderer": {"videoId": "abc", "title": {"runs": [{"text": "How To Use REA To Clone Any Software"}]},
  "viewCountText": {"simpleText": "359,678 views"}, "ownerText": {"runs": [{"text": "Eric Tech"}]},
  "publishedTimeText": {"simpleText": "12 days ago"}}},
 {"videoRenderer": {"videoId": "def", "title": {"runs": [{"text": "The Fastest-Rising Repo on GitHub"}]},
  "viewCountText": {"simpleText": "494,444 views"}, "ownerText": {"runs": [{"text": "Rob"}]},
  "publishedTimeText": {"simpleText": "13 days ago"}}}]}};</script></html>"""

GH = json.dumps({"items": [
    {"full_name": "morluto/rea", "description": "the original", "stargazers_count": 200000, "html_url": "u0"},
    {"full_name": "aicodeking/leanrea", "description": "One 8 KB skill that replaces REA", "stargazers_count": 1519,
     "html_url": "u1"},
    {"full_name": "someone/rea-backup", "description": "Independent mirror backup of morluto/rea",
     "stargazers_count": 10, "html_url": "u2"}]})

HN = json.dumps({"hits": [
    {"objectID": "1", "title": "REA Reverse – Engineer Anything", "points": 814, "num_comments": 340},
    {"objectID": "2", "title": "Hybrid Reasoning Agents", "points": 519, "num_comments": 239}]})


README = "# REA\nReverse engineer anything.\n\n## Quick start\n" + "Run `npx rea-agents setup`. " * 20 + "\n\n## Sponsors\nthanks"
CARD = "---\nlicense: mit\n---\n# DeepSeek V4.1 Flash\nA fast model.\n\n## How to use\n" + "Call it via the API. " * 20


def fetch(url, headers=None):
    if url.endswith("/readme"):
        return README.encode()
    for key, body in (("huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash/raw", CARD), ("youtube.com", YT),
                      ("api.github.com", GH), ("hn.algolia.com", HN)):
        if key in url:
            return body.encode()
    raise OSError("blocked")                      # DuckDuckGo / Bing refuse: must not crash


def test_research_parses_and_filters():
    r = research.research({"handle": "morluto/rea", "entities": ["rea"]}, fetch)
    assert [v["views"] for v in r["videos"]] == [494444, 359678]        # most viewed first
    assert [g["title"].split(":")[0] for g in r["repos"]] == ["aicodeking/leanrea"]   # no original, no mirror
    assert [h["points"] for h in r["hn"]] == [814]                      # "Reasoning" is not "rea"
    assert r["linkedin"] == [] and r["x"] == [] and r["web"] == []
    text = research.as_text(r)
    assert "494,444 views: The Fastest-Rising Repo on GitHub" in text and "1,519 stars" in text


def test_docs_for_every_kind_of_hot_thing():
    text, src = research.docs_for({"handle_type": "github_repo", "handle": "morluto/rea"}, fetch)
    assert src == "README of github.com/morluto/rea" and "npx rea-agents setup" in text and "Sponsors" not in text
    text, src = research.docs_for({"handle_type": "model", "handle": "deepseek v4.1 flash",
                                   "source_urls": ["https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash"]}, fetch)
    assert src.startswith("Hugging Face model card") and "license: mit" not in text and "How to use" in text
    text, src = research.docs_for({"handle_type": "news", "handle": "",
                                   "source_urls": ["https://news.google.com/x", "https://reddit.com/y"]}, fetch)
    assert (text, src) == ("", "")                    # discussion sites are never "official docs"
