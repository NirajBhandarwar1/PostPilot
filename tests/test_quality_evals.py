"""New quality checks: one passing and one failing sample each, written in the style of real posts."""

from postpilot import examples
from postpilot.evals import metrics
from postpilot.vendor import detect
from tests.test_eval_metrics import DOC, POST, _failed, _opt, _plan

LEX = {"words": [], "phrases": [], "structures": []}


def _post(text, **over):
    over.setdefault("evidence_pack", "fixed 14 of 20 and 9 of 20")
    return _failed(metrics.evaluate_post(_plan(DOC.replace(POST, text), **over)))


def _opts(**over):
    return _failed(metrics.evaluate_options({"options": [_opt("demo-app"), _opt("open-source-tool", **over)],
                                             "recommended": 1}, []))


# Passing style: Nicholas Harder / Taft Wallace / Valentino De Vivo. Failing: Hima Teja Raju / Amit Rawat.
GOOD = ("I gave 3 coding agents the same 20 bugs. Two fixed 14 of 20, one fixed 9 of 20.\n\n"
        "One run, one seed, so treat it as an early result. The rule: never trust a fix you did not rerun.\n\n"
        "Repo: https://github.com/example/bugs")


def test_options_have_baseline():
    assert "options_have_baseline" not in _opts(what_you_build="rules-only baseline vs model on 20 tasks, pass rate")
    assert "options_have_baseline" not in _opts(what_you_build="a tool", baseline="plain regex")
    assert "options_have_baseline" in _opts(what_you_build="Revolutionize your workflow with AI")


def test_options_sized_and_scored():
    assert "options_sized_and_scored" not in _opts(what_you_build="baseline vs model on 20 tasks, pass rate")
    assert "options_sized_and_scored" in _opts(what_you_build="baseline vs model, pass rate")
    assert "options_sized_and_scored" in _opts(what_you_build="baseline vs model on 20 tasks")


def _plan_steps(first, second):
    doc = DOC.replace("### Step 2 \U0001F916: Build the API  (~40 min)", f"### Step 2 \U0001F916: {first}  (~40 min)") \
             .replace("### Step 3 \U0001F916: Add the error messages  (~30 min)", f"### Step 3 \U0001F916: {second}  (~30 min)")
    return doc


def test_plan_pass_rule_first():
    ok = _plan_steps("Write the pass rule", "Run the 20 tasks").replace("Write `app.py` with one route.",
                                                                         "Set the threshold: 14 of 20.")
    bad = _plan_steps("Run the 20 tasks", "Write the pass rule").replace("Return a short reason for bad input.",
                                                                         "Success criteria: 14 of 20.")
    assert "plan_pass_rule_first" not in _failed(metrics.evaluate_plan(_plan(ok)))
    assert "plan_pass_rule_first" in _failed(metrics.evaluate_plan(_plan(bad)))
    no_rule = DOC.replace(" Pass rule: 3 of 5 bad uploads return a readable reason.", "")
    assert "plan_pass_rule_first" in _failed(metrics.evaluate_plan(_plan(no_rule)))


def test_results_have_n():
    assert "post_results_have_n" not in _post(GOOD)
    assert "post_results_have_n" in _post("Scored 91% on my agent.\n\nCheers.\n\nRepo: https://x.io/a")


def test_states_limit():
    assert "post_states_limit" not in _post(GOOD)
    assert "post_states_limit" in _post("Agents are changing everything and you should try them today.")


def test_numbers_traceable():
    assert "post_numbers_traceable" not in _post(GOOD)
    assert "post_numbers_traceable" in _post(GOOD + "\n\nThat is a 37% gain.")
    assert "post_numbers_traceable" not in _post(GOOD + "\n\nThat is a 37% gain.", evidence_pack="up 37% in March, 14 of 20, 9 of 20")
    assert "post_numbers_traceable" not in _post(GOOD + "\n\nI got {{pct}}% faster.")


def test_ending_rules():
    assert "post_ends_with_question" not in _post(GOOD)               # takeaway, then a link
    assert "post_ends_with_question" not in _post(GOOD.replace("never trust", "when did you last trust?"))
    assert "post_ends_with_question" in _post(GOOD + "\n\nThoughts?")
    assert "post_ends_with_question" in _post(GOOD + "\n\nWhat do you think?")


def test_placeholders_are_informational():
    c = next(c for c in metrics.evaluate_post(_plan()) if c.name == "post_uses_placeholders")
    assert c.passed and c.weight == 0


def test_hashtag_limit_is_three():
    assert metrics.MAX_HASHTAGS == 3
    assert "post_hashtag_count" in _post(GOOD + "\n\n#A #B #C #D")


def test_not_copied_from_examples(monkeypatch):
    src = "Every agent demo I saw this month hid the part where the file was broken and nothing worked at all."
    monkeypatch.setattr(examples, "load", lambda: [{"header": "Nicholas Harder", "text": src}])
    assert "post_not_copied_from_examples" in _post("Intro line.\n\n" + src)
    assert "post_not_copied_from_examples" not in _post(GOOD)


def test_scorer_counts_short_sentences_and_arrow_bullets():
    long = "This sentence is rather long and goes on for quite a few words here. " * 3
    assert len(detect.sentences("Not great. Bad. " + long)) >= 5
    flat = "\n".join(f"→ item number {n} here" for n in range(4))
    assert "uniform-bullets" in detect.check_voice(flat + "\n" + "word " * 30, {"structures": []})[1]


def test_placeholders_score_as_nothing():
    from postpilot import pipeline
    with_ph = "Ran it. Got {{a}} and {{b}} and {{c}} on {{d}} tasks. " * 4
    assert pipeline._score(with_ph)[1] <= pipeline._score(with_ph.replace("{{a}}", "12").replace("{{b}}", "34"))[1]
