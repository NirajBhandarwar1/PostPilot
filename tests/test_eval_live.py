"""Signals from real use: pick rate, ratings, model health, results."""

import json
from datetime import UTC, datetime, timedelta

from postpilot import db
from postpilot.evals import live


def _batch(age_days, chosen=None, status="sent"):
    bid = db.add_batch({"ideas": []}, status)
    when = (datetime.now(UTC) - timedelta(days=age_days)).isoformat()
    with db.connect() as c:
        c.execute("UPDATE batches SET created_at=?, chosen=? WHERE id=?", (when, chosen, bid))
    return bid


def test_with_no_data_everything_says_so_and_nothing_crashes():
    s = live.signals()
    assert s["ideas"]["pick_rate"] is None and s["ratings"]["average"] is None
    assert s["models"]["fallback_pct"] is None and s["results"]["per_post"] is None
    text = live.render(s)
    assert "no data yet" in text and "none yet" in text and "no posts recorded yet" in text


def test_pick_rate_ignores_emails_too_new_to_have_been_answered():
    _batch(5, chosen=2, status="built")
    _batch(4)
    _batch(3, chosen=1, status="posted")
    _batch(0)                                   # today: you may not have read it yet
    _batch(5, status="skipped")                 # skipped on purpose: not a miss
    i = live.signals()["ideas"]
    assert (i["emails"], i["picked"], i["built"], i["pick_rate"]) == (3, 2, 2, 66.7)


def test_batches_outside_the_window_are_left_out():
    _batch(40, chosen=1)
    assert live.signals(14)["ideas"]["emails"] == 0
    assert live.signals(60)["ideas"]["emails"] == 1


def test_ratings_average_and_a_broken_value_is_skipped():
    db.set_meta("ratings:1", json.dumps([5, 3]))
    db.set_meta("ratings:2", json.dumps([4]))
    db.set_meta("ratings:3", "not json")
    r = live.signals()["ratings"]
    assert r == {"rated": 3, "average": 4.0}


def test_model_health_by_task_and_fallback_rate():
    for ok, reason in ((True, ""), (True, ""), (False, "http_503"), (False, "too_big")):
        db.record_llm_call("groq", "m", "post", ok, reason, secs=10, tokens=100)
    db.record_llm_call("cloudflare", "m", "options", True, "", secs=20, tokens=50)
    m = live.signals()["models"]
    assert m["roles"]["post"] == {"attempts": 3, "usable": 2, "usable_pct": 66.7, "avg_seconds": 10.0, "tokens": 300}
    assert m["attempts"] == 4 and m["fallback_pct"] == 25.0          # 'too_big' is a size limit, not a failure
    assert "post" in live.render(live.signals()) and "25%" in live.render(live.signals())


def test_results_count_inbound_per_posted_build():
    bid = _batch(3, chosen=1, status="posted")
    db.set_outcome(bid, 0, posted_url="https://x", recruiters=2, founders=1, other=0)
    r = live.signals()["results"]
    assert r == {"posts": 1, "inbound": 3, "per_post": 3.0}
