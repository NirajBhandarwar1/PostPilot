"""What finds and ranks the stories that win: builder bonus, the new signal kinds, the sources behind them."""


from postpilot import brain, examples, pipeline, ranking, sources
from postpilot.evals import runner

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
