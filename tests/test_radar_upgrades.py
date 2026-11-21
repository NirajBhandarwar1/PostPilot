"""What finds and ranks the stories that win: builder bonus, the new signal kinds, the sources behind them."""

import json
from email.utils import formatdate

from postpilot import brain, examples, pipeline, ranking, sources
from postpilot.evals import runner

def test_new_signal_kinds_score_but_stay_modest():
    def one(kind, eng):
        return ranking.heat({"signal_ids": [0]}, [{"kind": kind, "rank": 0, "engagement": eng, "detail": "d"}])[0]
    assert one("Eng blog", 0) == 6.0 and one("News", 0) == 4.0
    assert 4.0 < one("GitHub new tools", 2500) <= 3.0 + 8.0


# ---------- sources ----------

def _hn(titles):
    body = json.dumps({"hits": [{"objectID": str(i), "title": t, "points": 300 + i, "num_comments": 5}
                                for i, t in enumerate(titles)]}).encode()
    return lambda url, headers=None: body


def test_a_hacker_news_query_matches_whole_words_only(monkeypatch):
    monkeypatch.setattr(sources, "_get", _hn(["Paper Airplanes", "FAA floppy disks", "Claude Code now has hooks"]))
    titles = " | ".join(s["title"] for s in sources.hacker_news())
    assert "Claude Code now has hooks" in titles and "Airplanes" not in titles and "floppy" not in titles


def test_the_radar_asks_about_coding_agents():
    for q in ("Claude Code", "MCP", "Codex", "open-sourced"):
        assert q in sources.HN_QUERIES
    assert sources.AI_WORDS.search("A new MCP server for Codex") and not sources.AI_WORDS.search("Paper Airplanes")


def test_new_coding_agent_repos_become_signals_two_per_topic(monkeypatch):
    def get(url, headers=None):
        topic = url.split("topic:")[1].split("+")[0]
        items = [{"full_name": f"o/{topic}-{n}", "description": "a tool", "stargazers_count": 900 - n * 100,
                  "html_url": f"https://github.com/o/{topic}-{n}"} for n in range(5)]
        items += [{"full_name": "o/tiny", "description": "tiny", "stargazers_count": 5, "html_url": "u"},
                  {"full_name": "o/nodesc", "description": None, "stargazers_count": 5000, "html_url": "u"}]
        return json.dumps({"items": items}).encode()
    monkeypatch.setattr(sources, "_get", get)
    got = sources.github_agent_tools()
    assert len(got) == 6 + len(sources.WIDE_TOPICS) and {s["kind"] for s in got} == {"GitHub new tools"}
    assert all("tiny" not in s["title"] and "nodesc" not in s["title"] for s in got)
    assert got[0]["engagement"] == 900 and got[0]["detail"] == "claude-code, 900 stars"
    assert sources.github_agent_tools in sources.COLLECTORS


def test_a_failing_topic_search_does_not_stop_the_others(monkeypatch):
    def get(url, headers=None):
        if "topic:mcp-server" in url:
            raise OSError("rate limited")
        topic = url.split("topic:")[1].split("+")[0]
        return json.dumps({"items": [{"full_name": f"o/{topic}", "description": "d", "stargazers_count": 300,
                                      "html_url": "u"}]}).encode()
    monkeypatch.setattr(sources, "_get", get)
    assert [s["detail"].split(",")[0] for s in sources.github_agent_tools()] == ["claude-code", "agent-skills", *sources.WIDE_TOPICS]


def test_engineering_blogs_become_eng_blog_signals_two_each(monkeypatch):
    when = formatdate(usegmt=True)
    items = "".join(f"<item><title>New AI agent post {n}</title><link>https://x/{n}</link><pubDate>{when}</pubDate></item>"
                    for n in range(5))
    rss = f"<rss><channel>{items}</channel></rss>".encode()
    monkeypatch.setattr(sources, "_get", lambda url, headers=None: rss)
    eng = [s for s in sources.news() if s["kind"] == "Eng blog"]
    assert len(eng) == 2 * len(sources.ENG_BLOGS) and eng[0]["title"].split(":")[0] in sources.ENG_BLOGS


# ---------- LinkedIn facts are not facts ----------

PACK = {"facts": [{"fact": "a stranger says it cut costs 80%", "source": "linkedin"},
                  {"fact": "5,000 stars on GitHub", "source": "github"}]}


def test_facts_from_linkedin_posts_never_reach_the_post_as_facts():
    facts = pipeline._facts({"evidence_pack": PACK})
    assert "5,000 stars" in facts and "stranger" not in facts


def test_the_options_and_plan_still_see_what_people_said():
    text = pipeline._pack_text({"evidence_pack": PACK})
    assert "stranger" in text and "5,000 stars" in text
    pack = brain.EvidencePack.model_validate(PACK)
    assert brain.render_evidence(pack, ("linkedin",)) == "- [github] 5,000 stars on GitHub"


# ---------- an eval never spends credit or reads your private file ----------

def test_an_eval_run_turns_linkedin_and_examples_off_and_restores_them(monkeypatch, tmp_path):
    monkeypatch.setenv("APIFY_TOKEN", "tok-abcdef123456")
    own = tmp_path / "examples.md"
    own.write_text(examples.HEADER + "---\nh\nan example post\n---\n", encoding="utf-8")
    monkeypatch.setattr(examples, "settings", type("S", (), {"examples_file": own})())
    frozen = tmp_path / "examples.frozen.md"
    monkeypatch.setattr(runner, "FROZEN_EXAMPLES", frozen)
    inside = {}

    def make_options(idea):
        inside["posts"] = sources.linkedin_posts("anything")
        inside["examples"] = examples.for_prompt({"title": "x"})
        inside["chosen"] = examples.chosen({"title": "x"})
        inside["path"] = examples.path()
        raise RuntimeError("stop here")
    monkeypatch.setattr(pipeline, "make_options", make_options)
    runner.run_eval(stories=[{"name": "a", "idea": {"title": "t"}, "option": {}}])
    assert inside["posts"] == [] and inside["path"] == frozen
    own.write_text("changed", encoding="utf-8")                                      # the frozen copy is not re-read
    assert frozen.exists() and "an example post" in frozen.read_text(encoding="utf-8")
    own.write_text(examples.HEADER + "---\nh\nan example post\n---\n", encoding="utf-8")
    assert examples.path() == own                                                   # back to normal afterwards
