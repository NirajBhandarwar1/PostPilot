"""The command line: every subcommand reaches its function with the parsed arguments (eng review R7).

Only 14% of __main__.py was tested while code moved and was renamed around it, so a wrong call would
have shown up only when someone typed the command. The functions behind each command have their own tests;
these check the wiring, and that CI's `precommit` command really exits 0 or 1 as a separate process.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from postpilot import __main__ as cli
from postpilot import db

ROOT = Path(__file__).resolve().parents[1]


def run_cli(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["postpilot", *argv])
    monkeypatch.setattr(cli, "_setup_logging", lambda: None)      # no log file in the real data folder
    cli.main()


# argv, the function the command must call, and the exact arguments it must get
ROUTES = [
    (["run"], "postpilot.pipeline.run_forever", (), {}),
    (["scout"], "postpilot.pipeline.scout", (), {"send_email": True}),
    (["scout", "--no-email"], "postpilot.pipeline.scout", (), {"send_email": False}),
    (["options", "4", "2"], "postpilot.pipeline.propose", (4, 2), {"send_email": True, "fresh": False}),
    (["options", "4", "2", "--no-email", "--fresh"], "postpilot.pipeline.propose", (4, 2),
     {"send_email": False, "fresh": True}),
    (["check"], "postpilot.pipeline.check_replies", (), {}),
    (["report"], "postpilot.reports.report", (), {}),
    (["eval", "history"], "postpilot.reports.replay", (3,), {}),
    (["eval", "history", "5"], "postpilot.reports.replay", (5,), {}),
    (["status"], "postpilot.__main__.status", (), {}),
]


@pytest.mark.parametrize("argv, target, args, kwargs", ROUTES, ids=[" ".join(r[0]) for r in ROUTES])
def test_each_command_reaches_its_function(monkeypatch, argv, target, args, kwargs):
    calls = []
    monkeypatch.setattr(target, lambda *a, **k: calls.append((a, k)))
    run_cli(monkeypatch, *argv)
    assert calls == [(args, kwargs)]


def test_build_passes_the_option_and_prints_the_result(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr("postpilot.pipeline.build", lambda *a, **k: calls.append((a, k)) or "built it")
    run_cli(monkeypatch, "build", "4", "2", "--option", "3", "--no-email")
    assert calls == [((4, 2), {"send_email": False, "option": 3, "reuse": False})]
    assert "built it" in capsys.readouterr().out


@pytest.mark.parametrize("argv, target, code", [
    (["doctor"], "postpilot.__main__.doctor", 0),
    (["doctor"], "postpilot.__main__.doctor", 1),
    (["eval", "list"], "postpilot.evals.runner.cli", 0),
    (["eval", "run", "--max", "1"], "postpilot.evals.runner.cli", 3),
    (["eval", "capture", "4", "1", "1", "my-story"], "postpilot.evals.runner.cli", 0),
    (["eval", "replay"], "postpilot.evals.runner.cli", 0),
])
def test_commands_that_exit_pass_their_code_on(monkeypatch, argv, target, code):
    seen = []
    monkeypatch.setattr(target, lambda *a: seen.append(a) or code)
    with pytest.raises(SystemExit) as e:
        run_cli(monkeypatch, *argv)
    assert e.value.code == code and len(seen) == 1


def test_eval_run_hands_the_parsed_arguments_to_the_runner(monkeypatch):
    got = []
    monkeypatch.setattr("postpilot.evals.runner.cli", lambda args: got.append(args) or 0)
    with pytest.raises(SystemExit):
        run_cli(monkeypatch, "eval", "run", "--story", "a", "--story", "b", "--max", "2", "--no-fail")
    a = got[0]
    assert (a.eval_cmd, a.story, a.max, a.no_fail) == ("run", ["a", "b"], 2, True)


def test_eval_live_prints_the_signals(monkeypatch, capsys):
    monkeypatch.setattr("postpilot.evals.live.signals", lambda days: {"days": days})
    monkeypatch.setattr("postpilot.evals.live.render", lambda s: f"signals for {s['days']} days")
    run_cli(monkeypatch, "eval", "live", "7")
    assert "signals for 7 days" in capsys.readouterr().out


def test_precommit_passes_the_paths_and_exits_with_its_code(monkeypatch):
    seen = []
    monkeypatch.setattr("postpilot.__main__.precommit", lambda paths: seen.append(paths) or 1)
    with pytest.raises(SystemExit) as e:
        run_cli(monkeypatch, "precommit", "a.py", "b.py")
    assert e.value.code == 1 and seen == [["a.py", "b.py"]]


def _batch():
    return db.add_batch({"ideas": [{"title": "t"}]}, "built")


def test_posted_inbound_and_stats_save_to_the_batch(monkeypatch, capsys):
    bid = _batch()
    run_cli(monkeypatch, "posted", str(bid), "https://example.com/p")
    run_cli(monkeypatch, "inbound", str(bid), "--recruiters", "2", "--founders", "1")
    run_cli(monkeypatch, "stats", str(bid), "--impressions", "500", "--reactions", "20")
    row = db.get_outcome(bid)
    assert row["posted_url"] == "https://example.com/p"
    assert (row["recruiters"], row["founders"], row["other"]) == (2, 1, 0)
    assert (row["impressions"], row["reactions"]) == (500, 20)
    out = capsys.readouterr().out
    assert "Recorded post" in out and "2 recruiters" in out and "Saved stats" in out


def test_a_missing_batch_is_a_clear_message_not_a_crash(monkeypatch):
    with pytest.raises(SystemExit) as e:
        run_cli(monkeypatch, "posted", "99", "https://example.com/p")
    assert "No batch #99" in str(e.value)


def test_baseline_is_saved(monkeypatch):
    run_cli(monkeypatch, "baseline", "--profile-views", "40", "--followers", "900", "--recruiter-msgs", "1")
    saved = json.loads(db.get_meta("baseline"))
    assert (saved["profile_views"], saved["followers"], saved["recruiter_msgs"]) == (40, 900, 1)


def test_test_email_needs_gmail_setup(monkeypatch):
    monkeypatch.setattr(cli.settings.__class__, "has_email", property(lambda self: False))
    with pytest.raises(SystemExit) as e:
        run_cli(monkeypatch, "test-email")
    assert "GMAIL_ADDRESS" in str(e.value)


def test_status_runs_on_an_empty_database(monkeypatch, capsys):
    run_cli(monkeypatch, "status")
    out = capsys.readouterr().out
    assert "Last scout" in out and "Recent batches" in out
    assert "Loop           : never started" in out


# ---------- as a separate process: what CI runs ----------

def _precommit(*paths):
    return subprocess.run([sys.executable, "-m", "postpilot", "precommit", *map(str, paths)], cwd=ROOT,
                          capture_output=True, text=True, timeout=120)


def test_precommit_command_exits_0_on_a_clean_file(tmp_path):
    clean = tmp_path / "clean.txt"
    clean.write_text("nothing secret here\n", encoding="utf-8")
    done = _precommit(clean)
    assert done.returncode == 0 and "OK: no secrets found" in done.stdout


def test_precommit_command_exits_1_on_a_key_and_never_prints_it(tmp_path):
    planted = "gsk_" + "AbCdEfGhIjKlMnOpQrStUvWx"          # split so this test file is not itself flagged
    bad = tmp_path / "bad.txt"
    bad.write_text(f"token = {planted}\n", encoding="utf-8")
    done = _precommit(bad)
    assert done.returncode == 1 and "BLOCKED" in done.stdout
    assert planted not in done.stdout + done.stderr


def test_examples_fetch_passes_the_authors_the_searches_and_force(monkeypatch):
    calls = []
    monkeypatch.setattr("postpilot.examples.fetch", lambda *a, **k: calls.append((a, k)) or "done")
    run_cli(monkeypatch, "examples", "fetch")
    run_cli(monkeypatch, "examples", "fetch", "--author", "https://www.linkedin.com/in/a", "--author",
            "https://www.linkedin.com/in/b", "--query", "q1", "--force", "--add")
    assert calls == [((None, None, False), {"add": False}),
                     ((["q1"], ["https://www.linkedin.com/in/a", "https://www.linkedin.com/in/b"], True), {"add": True})]


def test_examples_show_prints_where_they_are(monkeypatch, capsys):
    monkeypatch.setattr("postpilot.examples.state", lambda: "7 posts in somewhere")
    run_cli(monkeypatch, "examples", "show")
    assert "7 posts in somewhere" in capsys.readouterr().out


def test_status_shows_the_linkedin_search_and_the_examples(monkeypatch, capsys):
    run_cli(monkeypatch, "status")
    out = capsys.readouterr().out
    assert "LinkedIn posts : off (set APIFY_TOKEN" in out and "Style examples : none (" in out
