"""The eval rules score a known-good plan clean and catch each defect they exist for."""

import copy

import pytest

from postpilot.evals import metrics

POST = (
    "Most agent demos hide the boring part: what happens when the file is bad.\n\n"
    "I wired a small FastAPI service to a model and fed it broken uploads on purpose. "
    "The first run crashed in {{seconds}} seconds. After adding a clear error message, "
    "the same upload now returns a readable reason in {{ms}} ms.\n\n"
    "What I took from it: validate at the edge, log the reason, and keep the model call small. "
    "The repo has the code, the tests and a short demo clip so you can try the same bad files yourself "
    "and compare the messages you get. It runs locally with no paid keys, which makes it easy to share.\n\n"
    "What is the worst file you have thrown at a model?\n\n#AI #Python #LLM"
)

DOC = f"""# BUILD: Demo-App

Story: A story  |  Type: demo-app  |  Difficulty: easy  |  Realistic time: ~2 h

# PART A: Build instructions (for Cursor)

## Steps

### Step 1 \U0001F916: Create the project  (~10 min)

```powershell
python -m venv .venv
```

**Done when:** `.\\.venv\\Scripts\\python.exe -m pytest --version` prints a version.

### Step 2 \U0001F916: Build the API  (~40 min)

Write `app.py` with one route.

**Done when:** `pytest -q` passes.

### Step 3 \U0001F916: Add the error messages  (~30 min)

Return a short reason for bad input.

**Done when:** `pytest -q` passes.

### Step 4 \U0001F9D1: Record the demo  (~15 min)

Record 40 seconds of the bad-file run.

**Done when:** `docs/demo.gif` exists.

### Step 5 \U0001F9D1: Publish the repo  (~10 min)

Push to GitHub.

**Done when:** the repo page shows the README.

# PART B: Demo and LinkedIn post (for you)

## 2. The post (replace every {{{{...}}}} with your real results)

```
{POST}
```

## 6. First comment (post it right after publishing)

Repo link and the test command.

## 7. Check before you post

Read it once out loud.
"""


def _opt(kind="demo-app", **over):
    base = {"kind": kind, "title": "T", "what_you_build": "an app", "steps_preview": ["a", "b"], "hook": "h",
            "minutes": 90, "inspired_by": "Hacker News", "check_before_posting": []}
    return {**base, **over}


def _options(**over):
    return {"options": [_opt("demo-app"), _opt("open-source-tool"), _opt("face-off")], "recommended": 1, **over}


def _plan(doc=DOC, **over):
    return {"doc": doc, "score": 78, "warnings": [], **over}


def _failed(checks):
    return {c.name for c in checks if not c.passed}


# ---------- options ----------

def test_a_good_options_list_scores_clean():
    assert _failed(metrics.evaluate_options(_options(), [])) == set()


@pytest.mark.parametrize("mutate, expected", [
    (lambda r: r["options"].pop(), "options_min_count"),
    (lambda r: r["options"].__setitem__(0, _opt("face-off")), "options_has_demo_and_repo"),
    (lambda r: r["options"][1].update(hook=""), "options_complete_fields"),
    (lambda r: r.update(recommended=9), "options_recommended_valid"),
    (lambda r: r["options"][0].update(minutes=600), "options_effort_in_range"),
    (lambda r: r["options"][2].update(inspired_by=" "), "options_name_a_source"),
    (lambda r: r["options"][0].update(check_before_posting=["Need to check: model x"]), "options_no_model_warnings"),
])
def test_each_option_defect_is_caught(mutate, expected):
    res = _options()
    mutate(res)
    assert expected in _failed(metrics.evaluate_options(res, []))


def test_too_many_dropped_options_fail():
    assert "options_few_dropped" in _failed(metrics.evaluate_options(_options(), ["a", "b"]))


def test_garbage_input_never_raises():
    for bad in (None, {}, {"options": "x"}, {"options": [1, None]}, [], "text"):
        checks = metrics.evaluate_options(bad, [])    # type: ignore[arg-type]
        assert checks and "options_min_count" in _failed(checks)
    for bad in (None, {}, {"doc": None}, {"doc": 5}, []):
        assert metrics.evaluate_plan(bad)             # type: ignore[arg-type]
        assert metrics.evaluate_post(bad)             # type: ignore[arg-type]


# ---------- plan ----------

def test_a_good_plan_scores_clean():
    assert _failed(metrics.evaluate_plan(_plan())) == set()


def test_steps_are_parsed_with_who_and_minutes():
    steps = metrics._steps(DOC)
    assert [s.n for s in steps] == [1, 2, 3, 4, 5]
    assert [s.who for s in steps] == ["cursor", "cursor", "cursor", "human", "human"]
    assert steps[1].minutes == 40 and "Done when" in steps[1].body


@pytest.mark.parametrize("edit, expected", [
    (lambda d: d.replace("# PART B", "# PART X"), "plan_has_parts"),
    (lambda d: d.replace("**Done when:** `pytest -q` passes.", "", 1), "plan_steps_have_done_when"),
    (lambda d: d.replace("~2 h", "~30 min"), "plan_time_adds_up"),
    (lambda d: d.replace("\U0001F916: Build the API", "\U0001F9D1: Build the API")
                .replace("\U0001F916: Add the error", "\U0001F9D1: Add the error"), "plan_human_share"),
    (lambda d: d.replace("python -m venv .venv", "openai.ChatCompletion.create(model='x')"), "plan_no_old_openai_call"),
    (lambda d: d.replace("python -m venv .venv", "pip install openai tailwindcss"), "plan_no_npm_in_pip"),
    (lambda d: d.replace("python -m venv .venv", "client.create(model='groq:llama-3')"), "plan_no_prefixed_model_ids"),
    (lambda d: d.replace("python -m venv .venv", "python –m venv .venv"), "plan_ascii_commands"),
    (lambda d: d.replace("```powershell", "'''powershell"), "plan_fences_clean"),
    (lambda d: d.replace("Record the demo", "Record the screen").replace("Publish the repo", "Record again"),
     "plan_no_duplicate_steps"),
])
def test_each_plan_defect_is_caught(edit, expected):
    assert expected in _failed(metrics.evaluate_plan(_plan(edit(DOC))))


def test_step_count_limits():
    one_step = DOC[:DOC.index("### Step 2")] + DOC[DOC.index("# PART B"):]
    assert "plan_step_count" in _failed(metrics.evaluate_plan(_plan(one_step)))


def test_real_warnings_fail_but_need_to_check_does_not():
    ok = _plan(warnings=["Need to check: could not confirm model x", "No step uses the demo model."])
    bad = _plan(warnings=["The plan calls a removed API."])
    assert "plan_warnings_clean" not in _failed(metrics.evaluate_plan(ok))
    assert "plan_warnings_clean" in _failed(metrics.evaluate_plan(bad))


def test_unmarked_third_party_install_in_a_cursor_step_fails():
    doc = DOC.replace("Write `app.py` with one route.", "Run `npx create-react-app demo` first.")
    assert "plan_third_party_gated" in _failed(metrics.evaluate_plan(_plan(doc)))


# ---------- post ----------

def test_a_good_post_scores_clean():
    assert _failed(metrics.evaluate_post(_plan())) == set()


def test_post_is_cut_out_of_its_fence():
    assert metrics._post_text(DOC).startswith("Most agent demos")
    assert metrics._post_text("# nothing here") is None


@pytest.mark.parametrize("post, expected", [
    ("Short.", "post_length_ok"),
    ("x" * 200 + "\n\n" + POST, "post_hook_short"),
    (POST.replace("{{seconds}}", "4").replace("{{ms}}", "30"), "post_uses_placeholders"),
    (POST.replace("{{ms}}", "30 percent faster, a 40% drop"), "post_no_fabricated_numbers"),
    (POST.replace("What is the worst file you have thrown at a model?", "That is all."), "post_ends_with_question"),
    (POST + " #A #B #C #D", "post_hashtag_count"),
    (POST.replace("The first run", "It took me 3 hours. The first run"), "post_no_invented_claims"),
])
def test_each_post_defect_is_caught(post, expected):
    doc = DOC.replace(POST, post)
    assert expected in _failed(metrics.evaluate_post(_plan(doc)))


def test_low_human_score_and_missing_sections():
    assert "post_human_score" in _failed(metrics.evaluate_post(_plan(score=55)))
    c = _failed(metrics.evaluate_post(_plan(score=72)))
    assert "post_human_score_good" in c and "post_human_score" not in c
    assert "post_human_score" in _failed(metrics.evaluate_post(_plan(score=None)))
    no_comment = DOC.replace("## 6. First comment", "## 6. Notes")
    assert "post_first_comment" in _failed(metrics.evaluate_post(_plan(no_comment)))
    gone = metrics.evaluate_post(_plan("# PART A\n"))
    assert "post_length_ok" in _failed(gone)


# ---------- summary ----------

def test_summary_is_weighted_and_split_by_stage():
    checks = [metrics.Check("a", True, "", 1.0, "plan"), metrics.Check("b", False, "x", 3.0, "plan"),
              metrics.Check("c", True, "", 1.0, "post")]
    s = metrics.summarize(checks)
    assert s["by_stage"] == {"plan": 25.0, "post": 100.0}
    assert s["score"] == 40.0 and s["failed"] == ["b"] and s["n"] == 3
    assert metrics.summarize([])["score"] == 0.0


def test_the_fixture_is_not_changed_by_a_run():
    before = copy.deepcopy(_options())
    metrics.evaluate_options(before, [])
    assert before == _options()

