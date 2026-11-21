"""Fixes from the final Opus review: kinds, golden files, matching, token budget, bonus limits, copy warnings."""

import json
from email.utils import formatdate

from postpilot import brain, db, examples, pipeline, sources
from postpilot.evals import runner
from tests.test_examples import BODY, file, long_item  # noqa: F401  (file: a fixture that points at a temp examples.md)

# ---------- no new option kind: a skill or plugin is a flavour of open-source-tool ----------

def test_there_is_no_agent_skill_kind_to_fight_the_required_kinds():
    assert "agent-skill" not in brain.OPTION_RULES and "agent-skill" not in brain.BuildOption.model_fields["kind"].metadata.__repr__()
    assert "skill, plugin or MCP server for a coding agent" in brain.OPTION_RULES
    assert pipeline.REQUIRED_KINDS == ("demo-app", "open-source-tool")


def test_the_example_rules_do_not_invite_made_up_numbers_or_a_link_ending():
    r = brain.EXAMPLE_RULES
    assert "placeholders" in r and "900-1600 characters" in r and "first comment" in r and "HOW it works" in r
    assert "at least 3 numbers" not in r and "ends with the link" not in r


# ---------- strangers' posts stay out of the tracked golden files ----------

def test_capture_drops_linkedin_posts_and_facts_taken_from_them(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "GOLDEN_DIR", tmp_path / "golden")
    listing = {"options": [{"title": "Demo", "kind": "demo-app"}],
               "research": {"hn": [{"title": "an HN thread"}], "linkedin": [{"title": "stranger", "text": "secret words"}]},
               "evidence_pack": {"facts": [{"fact": "a stranger's claim", "source": "linkedin"},
                                           {"fact": "5,000 stars", "source": "github"}]}}
    bid = db.add_batch({"ideas": [{"title": "t"}], "options": {"1": listing}}, "options_sent")
    text = runner.capture(bid, 1, 1, "story").read_text(encoding="utf-8")
    assert "secret words" not in text and "stranger" not in text and "linkedin" not in text
    assert "an HN thread" in text and "5,000 stars" in text


# ---------- Hacker News matching keeps plurals and versions ----------

def test_plurals_and_version_numbers_still_count_as_ai_words():
    for title in ("Why LLMs still cannot count", "Qwen3.6 is out", "GPTs are everywhere", "New MCP servers"):
        assert sources.AI_WORDS.search(title), title
    assert not sources.AI_WORDS.search("Paper Airplanes")


def test_a_plural_story_survives_the_hacker_news_query(monkeypatch):
    body = json.dumps({"hits": [{"objectID": "1", "title": "Why LLMs still cannot count", "points": 400},
                                {"objectID": "2", "title": "Paper Airplanes", "points": 900}]}).encode()
    monkeypatch.setattr(sources, "_get", lambda url, headers=None: body)
    titles = [s["title"] for s in sources.hacker_news()]
    assert titles == ["Why LLMs still cannot count"]


# ---------- engineering blogs: only posts about AI ----------

def test_a_blog_post_that_is_not_about_ai_is_left_out(monkeypatch):
    when = formatdate(usegmt=True)
    rss = ("<rss><channel>"
           f"<item><title>Kubernetes upgrade notes</title><link>https://x/1</link><pubDate>{when}</pubDate></item>"
           f"<item><title>Our new AI agent platform</title><link>https://x/2</link><pubDate>{when}</pubDate></item>"
           "</channel></rss>").encode()
    monkeypatch.setattr(sources, "_get", lambda url, headers=None: rss)
    eng = [s["title"] for s in sources.news() if s["kind"] == "Eng blog"]
    assert any(t == "Cloudflare blog: Our new AI agent platform" for t in eng)
    assert not any(t.startswith(("Cloudflare blog", "GitHub blog", "Meta Engineering")) and "Kubernetes" in t for t in eng)
    assert any(t.startswith("OpenAI news") and "Kubernetes" in t for t in eng)         # OpenAI's own news is all AI


def test_every_signal_title_is_cut_for_the_free_token_limit():
    line = brain._compact_signals([{"kind": "News", "title": "x" * 200, "detail": "d"}])
    assert len(line) < 120


# ---------- the examples file ----------

def test_one_post_without_a_link_does_not_hide_the_next_one(file):
    one = sources._linkedin_row({"type": "post", "content": "First linkless post. " + BODY, "linkedinUrl": "x"})
    two = sources._linkedin_row({"type": "post", "content": "Second linkless post. " + BODY, "linkedinUrl": "x"})
    assert one["url"] == "" and two["url"] == ""
    assert examples.append([one]) == 1 and examples.append([two]) == 1 and examples.append([one, two]) == 0
    assert len(examples.load()) == 2


def test_a_rewrite_gets_fewer_and_shorter_examples_than_the_first_draft(monkeypatch, file):
    file.write_text(examples.HEADER + "".join(f"---\nh{i}\n{'agents ' * 200}\n" for i in range(3)) + "---\n", encoding="utf-8")
    seen = []

    def build_post(idea, v, h, feedback="", plan="", facts="", previous="", **kw):
        seen.append(kw.get("examples", ""))
        return brain.PostKit(summary="s", steps=[], post_draft=f"Draft number {len(seen)} about shipping an agent.",
                             placeholders=[], alt_hooks=[], visual_spec="v", fact_checks=[])
    monkeypatch.setattr(pipeline.brain, "build_post", build_post)
    monkeypatch.setattr(pipeline, "_score", lambda t: ({"x": (50, "weak")}, 60.0, "FLAGGED"))
    pipeline._write_post({"title": "agents"}, plan="p")
    assert len(seen) == 2 and seen[0].count("Example ") == 3 and seen[1].count("Example ") == 2
    assert len(seen[1]) < len(seen[0]) * 0.75


def test_a_draft_that_copied_in_every_try_ships_with_a_warning(monkeypatch, file):
    file.write_text(examples.HEADER + "---\nh0\nthe agent that wrote your spec is the worst reviewer of it\n---\n",
                    encoding="utf-8")
    copied = "Truth: the agent that wrote your spec is the worst reviewer of it, I measured {{result}} today."
    monkeypatch.setattr(pipeline.brain, "build_post", lambda *a, **k: brain.PostKit(
        summary="s", steps=[], post_draft=copied, placeholders=[], alt_hooks=[], visual_spec="v", fact_checks=[]))
    out = pipeline._write_post({"title": "spec reviewer agent"}, plan="p")
    assert any("repeats a phrase from a style example" in c for c in out[0].fact_checks)


def test_long_posts_are_kept_whole_in_the_row_but_filtered_by_their_real_length():
    row = sources._linkedin_row(long_item(1, content="x " * 1500))            # 3,000 characters
    assert len(row["raw"]) == 1800 and row["length"] > 1800 and examples.best([row]) == []
