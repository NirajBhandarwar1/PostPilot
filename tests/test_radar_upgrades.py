"""What finds and ranks the stories that win: builder bonus, the new signal kinds, the sources behind them."""


from postpilot import brain, examples, pipeline, ranking, sources

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

