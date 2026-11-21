from postpilot import brain


def test_build_option_new_fields_default():
    f = brain.BuildOption.model_fields
    assert f["baseline"].default == "" and f["pass_rule"].default == ""
    assert f["runs"].default == 0 and f["limit"].default == ""


def test_option_rules_require_believability_fields():
    r = brain.OPTION_RULES
    for w in ("baseline", "pass_rule", "runs", "limit", "works on {{N}} of"):
        assert w in r


def test_post_prompts_rules():
    for text in (brain.EXAMPLE_RULES, brain.POST_STYLE):
        assert "Thoughts?" in text and "NOT" in text
        assert "arrow bullets" not in text and "one command" not in text
    assert "2-4 rules" in brain.EXAMPLE_RULES and "2-4 rules" in brain.POST_STYLE


def test_brief_prompt_mentions_results_files(monkeypatch):
    seen = {}
    monkeypatch.setattr(brain.llm, "chat_json", lambda s, p, *a, **k: seen.setdefault("p", p))
    brain.build_brief({"title": "t"})
    assert "RESULTS.md" in seen["p"] and "results.json" in seen["p"]
    assert "What broke" in seen["p"] and "Limits" in seen["p"]


def test_placeholder_budget_covers_required_unknowns(monkeypatch):
    import re
    seen = {}
    monkeypatch.setattr(brain.llm, "chat_json", lambda s, p, *a, **k: seen.setdefault("p", p))
    brain.build_post({"title": "t"}, "", {"hooks": []})
    m = re.search(r"At most (\d+) \{\{placeholders", seen["p"])
    unknowns = ["comparison A", "comparison B", "failing input", "results", "time", "link"]
    assert m and int(m.group(1)) >= len(unknowns)
    assert "ALWAYS {{placeholders}}" in brain.POST_STYLE


def test_hook_rules_never_ask_for_a_result():
    for text in (brain.EXAMPLE_RULES, brain.OPTION_RULES):
        assert "concrete result" not in text and "measured results" not in text
    assert "result stays {{result}}" in brain.OPTION_RULES


def test_options_prompt_fits_groq_budget(monkeypatch):
    seen = {}
    monkeypatch.setattr(brain.llm, "chat_json", lambda s, p, *a, **k: seen.setdefault("p", s + p))
    facts = [brain.EvidenceFact(fact="x" * 240, source="web")] * 10
    pack = brain.render_evidence(brain.EvidencePack(facts=facts))
    brain.make_options({"topic": "t", "handle": "h"}, "", [], research_text=pack)
    assert len(brain.OPTION_RULES) < 4500
    assert len(seen["p"]) / 3.6 < 8000 - 2500 - 300
