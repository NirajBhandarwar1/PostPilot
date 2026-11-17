"""A reply claim outlives a slow build, so a second check cannot start the same build (eng review R9)."""

from datetime import UTC, datetime, timedelta

from postpilot import db


def _claimed(key, minutes_ago):
    when = (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
    with db.connect() as c:
        c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, "busy:" + when))


def test_a_fresh_claim_is_taken_once():
    assert db.claim("reply:1") == "claimed"
    assert db.claim("reply:1") == "busy"


def test_a_30_minute_old_claim_is_still_busy():
    _claimed("reply:2", 30)            # a slow build still running: the old 20 minute window would have lost it
    assert db.claim("reply:2") == "busy"


def test_a_61_minute_old_claim_belongs_to_a_dead_run():
    _claimed("reply:3", 61)
    assert db.claim("reply:3") == "claimed"


def test_a_finished_claim_stays_done():
    assert db.claim("reply:4") == "claimed"
    db.release("reply:4", done=True)
    assert db.claim("reply:4") == "done"
