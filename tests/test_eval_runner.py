"""The eval runner: capture, run, store, compare, replay. No real model call and no real folders."""

import argparse
import json

import pytest

from postpilot import db, llm, pipeline
from postpilot.evals import runner
from tests.test_eval_metrics import _options, _plan

IDEA = {"title": "A public story", "topic": "A public story", "topic_kind": "news"}
OPTION = {"title": "Demo", "kind": "demo-app"}


@pytest.fixture
def folders(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "GOLDEN_DIR", tmp_path / "golden")
    monkeypatch.setattr(runner, "RESULTS_DIR", tmp_path / "results")
    return tmp_path


def _story(name="one"):
    return {"name": name, "idea": IDEA, "option": OPTION, "research": {"x": 1}, "evidence_pack": {"facts": ["f"]}}


@pytest.fixture
def fake_pipeline(monkeypatch):
    seen = {"options": 0, "plan": 0}

    def make_options(idea):
        seen["options"] += 1
        llm.USAGE.clear()
        llm.USAGE["groq:m"] = {"calls": 1, "in": 100, "out": 50}
        return _options(), []

    def make_plan(idea, batch_id=None, build_no=None):
        seen["plan"] += 1
        llm.USAGE.clear()
        llm.USAGE["cloudflare:m"] = {"calls": 2, "in": 200, "out": 80}
        return _plan()

    monkeypatch.setattr(pipeline, "make_options", make_options)
    monkeypatch.setattr(pipeline, "make_plan", make_plan)
    monkeypatch.setattr(pipeline, "idea_from_option", lambda idea, o, research=None, pack=None: {**idea, "o": o})
    return seen


# ---------- golden set ----------

def _batch_with_options(**extra):
    listing = {"options": [{**OPTION, "note": "mail me at a.b@example.com"}], "research": {"r": 1},
               "evidence_pack": {"facts": ["key sk-" + "abcdefghijklmnopqrstuv"]}, **extra}
    return db.add_batch({"ideas": [IDEA], "options": {"1": listing}}, "options_sent")


def test_capture_saves_public_text_only_and_removes_emails_and_keys(folders):
    bid = _batch_with_options()
    path = runner.capture(bid, 1, 1, "my-story")
    text = path.read_text(encoding="utf-8")
    assert "example.com" not in text and "sk-abcdefghij" not in text and "[removed]" in text
    story = json.loads(text)
    assert story["idea"]["title"] == "A public story" and story["name"] == "my-story"
    assert runner.golden_stories(["my-story"])[0]["option"]["title"] == "Demo"


@pytest.mark.parametrize("args, message", [
    ((99, 1, 1, "ok"), "No batch #99"),
    ((1, 5, 1, "ok"), "has ideas 1-1"),
    ((1, 1, 7, "ok"), "has options 1-1"),
    ((1, 1, 1, "Bad Name!"), "lowercase"),
])
def test_capture_rejects_bad_input(folders, args, message):
    _batch_with_options()
    with pytest.raises(ValueError, match=message):
        runner.capture(*args)


def test_capture_needs_saved_options(folders):
    bid = db.add_batch({"ideas": [IDEA]}, "draft")
    with pytest.raises(ValueError, match="no saved options"):
        runner.capture(bid, 1, 1, "x")


def test_unknown_golden_story_names_the_ones_that_exist(folders):
    _batch_with_options()
    runner.capture(1, 1, 1, "real")
    with pytest.raises(ValueError, match=r"No golden story nope. Have: real"):
        runner.golden_stories(["nope"])


# ---------- running ----------

def test_a_run_scores_all_three_stages_and_counts_tokens(folders, fake_pipeline):
    run = runner.run_eval(stories=[_story("a"), _story("b")])
    s = run["stories"]["a"]
    assert set(s["summary"]["by_stage"]) == {"options", "plan", "post"}
    assert s["tokens"] == {"in": 300, "out": 130, "calls": 3}
    assert run["overall"] > 80 and fake_pipeline == {"options": 2, "plan": 2}


def test_a_run_saves_no_batch_and_sends_nothing(folders, fake_pipeline):
    bid = _batch_with_options()
    before = dict(db.get_batch(bid))
    runner.run_eval(stories=[_story()])
    assert dict(db.get_batch(bid)) == before and len(db.recent_batches(10)) == 1


def test_max_stories_limits_the_run(folders, fake_pipeline):
    runner.run_eval(stories=[_story(str(i)) for i in range(5)], max_stories=2)
    assert fake_pipeline["options"] == 2


def test_a_crashed_stage_is_recorded_and_costs_points(folders, monkeypatch, fake_pipeline):
    good = runner.run_eval(stories=[_story()])["overall"]

    def boom(idea):
        raise RuntimeError("provider exploded")
    monkeypatch.setattr(pipeline, "make_options", boom)
    run = runner.run_eval(stories=[_story()])
    s = run["stories"]["one"]
    assert "options" in s["errors"] and "plan" not in s["errors"]
    assert "options stage ran" in s["summary"]["failed"] and run["overall"] < good


def test_an_empty_provider_stops_the_run_early(folders, monkeypatch, fake_pipeline, capsys):
    def empty(idea):
        raise RuntimeError("cloudflare: no credit (used up your daily free allocation)")
    monkeypatch.setattr(pipeline, "make_options", empty)
    run = runner.run_eval(stories=[_story("a"), _story("b"), _story("c")])
    assert list(run["stories"]) == ["a"] and run["skipped"] == ["b", "c"]
    assert fake_pipeline["plan"] == 0                       # the next stage did not run on an empty provider
    assert "Stopping early" in capsys.readouterr().out
    s = run["stories"]["a"]
    assert s["incomplete"] is True
    assert "plan" not in s["summary"]["by_stage"]           # a stage that never ran is not scored as 100


def test_a_stage_that_never_started_is_not_scored():
    checks, summary = runner._score({}, {"options": "boom"})
    assert [c.name for c in checks] == ["options stage ran"] and summary["by_stage"] == {"options": 0.0}


# ---------- store, compare, replay ----------

def test_saved_run_is_loaded_back_by_pointer_name_and_path(folders, fake_pipeline):
    run = runner.run_eval(stories=[_story()])
    path = runner.save_run(run)
    assert runner.load_run()["overall"] == run["overall"]
    assert runner.load_run(path.name)["overall"] == run["overall"]
    assert runner.load_run(path.stem)["overall"] == run["overall"]
    assert runner.load_run("missing") is None


def test_no_saved_run_means_none(folders):
    assert runner.load_run() is None


def _worse(run, check="plan_no_npm_in_pip"):
    bad = json.loads(json.dumps(run))
    for c in bad["stories"]["one"]["checks"]:
        if c["name"] == check:
            c["passed"] = False
    return bad


def test_compare_finds_regressions_and_fixes(folders, fake_pipeline):
    good = runner.run_eval(stories=[_story()])
    bad = _worse(good)
    assert runner.compare(None, good)["overall"] is None
    d = runner.compare(good, bad)
    assert [r["check"] for r in d["regressions"]] == ["plan_no_npm_in_pip"] and d["fixed"] == []
    assert [r["check"] for r in runner.compare(bad, good)["fixed"]] == ["plan_no_npm_in_pip"]


def test_exit_code_fails_on_a_real_regression_or_a_big_drop():
    assert runner.exit_code({"overall": None, "regressions": []}) == 0
    assert runner.exit_code({"overall": -1.0, "regressions": []}) == 0
    assert runner.exit_code({"overall": -9.0, "regressions": []}) == 1
    assert runner.exit_code({"overall": 0.0, "regressions": [{"weight": 1.0}]}) == 1
    assert runner.exit_code({"overall": 0.0, "regressions": [{"weight": 0.5}]}) == 0


def test_replay_scores_stored_outputs_again_without_a_model_call(folders, fake_pipeline, monkeypatch, capsys):
    runner.save_run(runner.run_eval(stories=[_story()]))
    calls = dict(fake_pipeline)
    monkeypatch.setattr(pipeline, "make_options", lambda idea: pytest.fail("replay called a model"))
    monkeypatch.setattr(pipeline, "make_plan", lambda *a, **k: pytest.fail("replay called a model"))
    new = runner.replay()
    assert fake_pipeline == calls and new["overall"] > 80
    assert "vs previous run: overall +0.0" in capsys.readouterr().out


def test_replay_without_a_run_explains_what_to_do(folders):
    with pytest.raises(ValueError, match="eval run"):
        runner.replay()


# ---------- command line ----------

def _args(**kw):
    return argparse.Namespace(**{"eval_cmd": "run", "story": None, "max": 3, "no_fail": False, **kw})


def test_cli_run_prints_a_table_saves_and_exits_zero_the_first_time(folders, fake_pipeline, capsys):
    runner.GOLDEN_DIR.mkdir(parents=True)
    (runner.GOLDEN_DIR / "one.json").write_text(json.dumps(_story()), encoding="utf-8")
    assert runner.cli(_args()) == 0
    out = capsys.readouterr().out
    assert "| one |" in out and "No previous run to compare with" in out and "Saved" in out
    assert len(list(runner.RESULTS_DIR.glob("*.json"))) == 1


def test_cli_incomplete_run_is_reported_but_not_saved_or_compared(folders, fake_pipeline, monkeypatch, capsys):
    runner.GOLDEN_DIR.mkdir(parents=True)
    (runner.GOLDEN_DIR / "one.json").write_text(json.dumps(_story()), encoding="utf-8")
    assert runner.cli(_args()) == 0                               # a normal first run: saved
    monkeypatch.setattr(pipeline, "make_options", lambda idea: (_ for _ in ()).throw(RuntimeError("no credit")))
    assert runner.cli(_args()) == 3
    assert "Incomplete" in capsys.readouterr().out
    assert len(list(runner.RESULTS_DIR.glob("*.json"))) == 1      # still only the first run
    assert runner.cli(_args(no_fail=True)) == 0


def test_cli_run_with_no_stories_says_so(folders, capsys):
    assert runner.cli(_args()) == 1
    assert "No golden stories" in capsys.readouterr().out


def test_cli_list_and_bad_story_name(folders, capsys):
    runner.GOLDEN_DIR.mkdir(parents=True)
    (runner.GOLDEN_DIR / "one.json").write_text(json.dumps(_story()), encoding="utf-8")
    assert runner.cli(argparse.Namespace(eval_cmd="list")) == 0
    assert "one" in capsys.readouterr().out
    assert runner.cli(_args(story=["zzz"])) == 2


def test_real_golden_stories_are_valid_and_clean():
    """The committed stories must load, carry what the runner needs, and hold no email or key."""
    stories = runner.golden_stories()                                # default folder: evals/golden
    assert len(stories) >= 3
    for s in stories:
        assert {"name", "idea", "option"} <= set(s) and s["idea"].get("title") and s["option"].get("title")
        dumped = json.dumps(s)
        assert not any(p.search(dumped) for p in runner.SECRETS)


