"""The loop tells you when it stopped, and a scout with no ideas is tried again (eng review R2, R3, R8)."""

from datetime import UTC, datetime, timedelta

import pytest

from postpilot import db, pipeline

NOW = datetime(2026, 11, 12, 18, 0, tzinfo=UTC)


class Stop(Exception):
    pass


@pytest.fixture
def loop(monkeypatch):
    """run_forever with the model, mail and sleeping faked; it stops at the first sleep."""
    seen = {"emails": [], "beats": 0}
    monkeypatch.setattr(pipeline.llm, "available", lambda: True)
    monkeypatch.setattr(pipeline, "scout", lambda: None)
    monkeypatch.setattr(pipeline, "resend_unsent", lambda: None)
    monkeypatch.setattr(pipeline, "check_replies", lambda: None)
    monkeypatch.setattr(pipeline, "_notify", lambda subject, body: seen["emails"].append((subject, body)))
    monkeypatch.setattr(pipeline.time, "sleep", lambda s: (_ for _ in ()).throw(Stop()))
    real = pipeline.heartbeat

    def counted(now=None):
        seen["beats"] += 1
        real(now)
    monkeypatch.setattr(pipeline, "heartbeat", counted)

    def run():
        with pytest.raises(Stop):
            pipeline.run_forever()
    seen["run"] = run
    return seen


def _beat(hours_ago):
    db.set_meta("last_loop_at", (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat(timespec="seconds"))


# ---------- heartbeat, status line, "was down" email ----------

def test_first_ever_start_sends_no_email_and_writes_the_mark(loop):
    loop["run"]()
    assert loop["emails"] == [] and db.get_meta("last_loop_at")


def test_a_short_gap_sends_no_email(loop):
    _beat(0.5)
    loop["run"]()
    assert loop["emails"] == []


def test_a_gap_over_an_hour_sends_exactly_one_email(loop):
    _beat(2)
    loop["run"]()
    assert len(loop["emails"]) == 1
    subject, body = loop["emails"][0]
    assert subject == "[PostPilot] was down for 2.0 h" and "not running for 2.0 h" in body


def test_a_failed_email_does_not_stop_the_start(loop, monkeypatch):
    _beat(3)
    monkeypatch.setattr(pipeline, "_notify", lambda s, b: (_ for _ in ()).throw(OSError("gmail down")))
    loop["run"]()                                         # reaches the first sleep: the start was not stopped
    assert db.get_meta("last_loop_at")


def test_a_mark_is_written_at_the_start_and_after_each_stage(loop):
    loop["run"]()
    assert loop["beats"] == 5            # on start, then round start, after scout, after resend, after replies


def test_the_mark_is_fresh_after_the_run(loop):
    _beat(5)
    loop["run"]()
    assert pipeline.loop_age() < 60


def test_status_line_says_never_alive_or_stopped():
    assert pipeline.loop_line().startswith("never started")
    db.set_meta("last_loop_at", (NOW - timedelta(minutes=5)).isoformat())
    assert pipeline.loop_line(NOW) == "alive, last seen 5 min ago"
    db.set_meta("last_loop_at", (NOW - timedelta(hours=3)).isoformat())
    assert pipeline.loop_line(NOW).startswith("STOPPED? last seen 3.0 h ago")


def test_one_hour_is_the_limit():
    db.set_meta("last_loop_at", (NOW - timedelta(minutes=59)).isoformat())
    assert pipeline.loop_line(NOW).startswith("alive")
    db.set_meta("last_loop_at", (NOW - timedelta(minutes=61)).isoformat())
    assert pipeline.loop_line(NOW).startswith("STOPPED?")


# ---------- a scout with no ideas is tried again ----------

def test_a_retry_is_planned_three_hours_out():
    assert pipeline.schedule_zero_idea_retry(NOW) is True
    assert datetime.fromisoformat(db.get_meta("retry_scout_at")) == NOW + timedelta(hours=3)


def test_the_third_zero_idea_scout_of_a_day_is_not_retried():
    morning = NOW.replace(hour=6)                       # three tries, all on the same UTC day
    assert pipeline.schedule_zero_idea_retry(morning) is True
    assert pipeline.schedule_zero_idea_retry(morning + timedelta(hours=3)) is True
    db.set_meta("retry_scout_at", "")
    assert pipeline.schedule_zero_idea_retry(morning + timedelta(hours=6)) is False
    assert not db.get_meta("retry_scout_at")


def test_the_daily_count_starts_again_the_next_day():
    for _ in range(2):
        pipeline.schedule_zero_idea_retry(NOW)
    assert pipeline.schedule_zero_idea_retry(NOW) is False
    assert pipeline.schedule_zero_idea_retry(NOW + timedelta(days=1)) is True


def test_the_loop_does_not_wipe_the_retry_it_just_planned(loop, monkeypatch):
    """Outside voice O2: run_forever clears retry_scout_at after every scout, so the retry is set after that."""
    monkeypatch.setattr(pipeline, "scout_due", lambda: True)
    loop["run"]()                                                       # scout returned None: no ideas, no error
    left = (datetime.fromisoformat(db.get_meta("retry_scout_at")) - datetime.now(UTC)).total_seconds()
    assert 10700 < left <= 10800


def test_a_scout_that_found_ideas_plans_no_retry(loop, monkeypatch):
    monkeypatch.setattr(pipeline, "scout_due", lambda: True)
    monkeypatch.setattr(pipeline, "scout", lambda: 7)
    db.set_meta("retry_scout_at", "2026-01-01T00:00:00+00:00")
    loop["run"]()
    assert not db.get_meta("retry_scout_at")                            # cleared, none planned
