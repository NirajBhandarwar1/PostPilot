"""Fixes from the final Opus review: kinds, golden files, matching, token budget, bonus limits, copy warnings."""


from postpilot import brain, db, examples, pipeline, sources
from tests.test_examples import BODY, file, long_item  # noqa: F401  (file: a fixture that points at a temp examples.md)

# ---------- no new option kind: a skill or plugin is a flavour of open-source-tool ----------

def test_there_is_no_agent_skill_kind_to_fight_the_required_kinds():
    assert "agent-skill" not in brain.OPTION_RULES and "agent-skill" not in brain.BuildOption.model_fields["kind"].metadata.__repr__()
    assert "skill, plugin or MCP server for a coding agent" in brain.OPTION_RULES
    assert pipeline.REQUIRED_KINDS == ("demo-app", "open-source-tool")


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
