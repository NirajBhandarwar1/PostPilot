"""Health line: daily free-quota counters, their wording, and the line at the end of the ideas email."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import BaseModel

from postpilot import db, llm, mailer, pipeline

DAY1 = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)
DAY2 = DAY1 + timedelta(days=1)
GPT = "@cf/openai/gpt-oss-120b"


@pytest.fixture(autouse=True)
def _budgets(monkeypatch):
    monkeypatch.delenv("CLOUDFLARE_DAILY_NEURONS", raising=False)
    monkeypatch.delenv("GROQ_DAILY_TOKENS", raising=False)


def test_counters_accumulate_per_provider_and_reset_next_day():
    llm.record_usage("groq", "openai/gpt-oss-120b", 1000, 500, now=DAY1)
    llm.record_usage("qwen", "qwen/qwen3.8-27b", 100, 100, now=DAY1)       # Qwen runs on Groq: same counter
    llm.record_usage("openai", "gpt-6", 5000, 5000, now=DAY1)              # untracked
    assert db.get_meta("quota:groq:2026-11-12") == "1700"
    assert db.get_meta("quota:openai:2026-11-12") is None
    assert llm.quota_lines(DAY2) == []
    llm.record_usage("groq", "x", 10, 0, now=DAY2)
    assert db.get_meta("quota:groq:2026-11-13") == "10"
    assert db.get_meta("quota:groq:2026-11-12") == "1700"


def test_cloudflare_neuron_math_known_and_unknown_model():
    # 1M in + 1M out of gpt-oss = 31818 + 68182 neurons
    assert llm.neurons(GPT, 1_000_000, 1_000_000) == (100000, False)
    assert llm.neurons("@cf/nvidia/nemotron-3-120b-a12b", 1_000_000, 0) == (45455, False)
    n, guess = llm.neurons("@cf/meta/some-new-model", 1_000_000, 0)
    assert (n, guess) == (31818, True)
    llm.record_usage("cloudflare", "@cf/meta/some-new-model", 100_000, 0, now=DAY1)
    assert "approx." in llm.quota_lines(DAY1)[0]


def test_no_credit_marks_provider_full_for_today(monkeypatch):
    class Out(BaseModel):
        ok: bool

    for k, v in {"LLM_PROVIDERS": "cloudflare", "CLOUDFLARE_API_TOKEN": "c", "CLOUDFLARE_ACCOUNT_ID": "a"}.items():
        monkeypatch.setenv(k, v)
    llm._no_credit_until.clear()

    def no_credit(*a, **k):
        raise llm.NoCredit("used up your daily free allocation")
    monkeypatch.setattr(llm, "_post", no_credit)
    with pytest.raises(RuntimeError):
        llm.chat_json("s", "u", Out)
    llm._no_credit_until.clear()
    assert "used up for today" in llm.quota_lines()[0]


def test_successful_call_is_tallied(monkeypatch):
    class Out(BaseModel):
        ok: bool

    monkeypatch.setenv("LLM_PROVIDERS", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "g")
    monkeypatch.setattr(llm, "_post", lambda *a, **k: {
        "usage": {"prompt_tokens": 300, "completion_tokens": 200},
        "choices": [{"finish_reason": "stop", "message": {"content": '{"ok": true}'}}]})
    llm.chat_json("s", "u", Out)
    assert any("500/200,000 tokens" in line for line in llm.quota_lines())


def test_quota_wording():
    llm.record_usage("cloudflare", GPT, 0, 0, now=DAY1)
    db.set_meta("quota:cloudflare:2026-11-12", "6200")
    assert llm.quota_lines(DAY1) == ["Cloudflare 62% of today's free quota (~6,200/10,000 neurons)"]
    db.set_meta("quota:cloudflare:2026-11-12", "8500")
    assert llm.quota_lines(DAY1)[0].endswith("neurons), running low")
    db.set_meta("quota:cloudflare:2026-11-12", "10000")
    assert llm.quota_lines(DAY1) == ["Cloudflare used up for today (~10,000/10,000 neurons)"]
    db.set_meta("quota:groq:2026-11-12", "28000")
    assert llm.quota_lines(DAY1)[1] == "Groq 14% of today's free quota (28,000/200,000 tokens)"


def test_health_line_is_one_short_line_with_warning_prefix():
    db.set_meta("quota:cloudflare:2026-11-12", "6200")
    db.set_meta("quota:groq:2026-11-12", "28000")
    for i in range(44):
        db.record_llm_call("groq", "m", "default", i < 41)
    line = llm.health_line({"pitched": 3, "dropped": 1}, now=DAY1)
    assert line == ("Health: scout ok, 3 ideas (1 dropped) · Cloudflare 62% of today's quota · Groq 14% "
                    "· models ok 41/44")
    assert "\n" not in line and len(line) <= 160 and not line.startswith("⚠")
    db.set_meta("quota:groq:2026-11-12", "170000")
    assert llm.health_line({"pitched": 3, "dropped": 0}, now=DAY1).startswith("⚠ Health:")
    assert llm.health_line(None, now=DAY1).startswith("⚠ Health:")


def test_ideas_email_is_unchanged_by_health(monkeypatch):
    bid = db.add_batch({"ideas": [], "stats": {"pitched": 2, "dropped": 0}}, status="draft")
    sent = []
    monkeypatch.setattr(mailer, "send", lambda subject, text, html=None: sent.append((text, html)))
    assert pipeline._send_ideas(bid, "PostPilot #1\n\nbody")
    assert sent[0][0] == "PostPilot #1\n\nbody" and "Health" not in sent[0][1]


def test_log_health_levels(caplog):
    caplog.set_level("INFO", logger="postpilot.health")
    db.set_meta("quota:groq:" + llm._quota_day(), "28000")
    llm.log_health({"pitched": 3, "dropped": 0})
    rec = caplog.records[-1]
    assert rec.levelname == "INFO" and rec.getMessage().startswith("Health: scout ok, 3 ideas")
    llm.log_health({"pitched": 0, "dropped": 2})                  # nothing passed: warn
    assert caplog.records[-1].levelname == "WARNING"
    db.set_meta("quota:groq:" + llm._quota_day(), "170000")
    llm.log_health({"pitched": 3, "dropped": 0})
    assert caplog.records[-1].levelname == "WARNING" and caplog.records[-1].getMessage().startswith("⚠ Health:")


def test_threshold_warnings_logged_once_per_level_per_day(caplog):
    caplog.set_level("INFO", logger="postpilot.health")
    warns = lambda: [r for r in caplog.records if r.levelname == "WARNING" and "free quota" in r.getMessage()]  # noqa: E731
    llm.record_usage("groq", "m", 100_000, 0, now=DAY1)
    assert not warns()
    llm.record_usage("groq", "m", 70_000, 0, now=DAY1)           # 85%: first crossing
    llm.record_usage("groq", "m", 1_000, 0, now=DAY1)            # still 80%+: silent
    assert len(warns()) == 1 and "running low" in warns()[0].getMessage()
    llm.record_usage("groq", "m", 40_000, 0, now=DAY1)           # 100%
    llm.record_usage("groq", "m", 1_000, 0, now=DAY1)
    assert len(warns()) == 2 and "used up" in warns()[1].getMessage()
    llm.record_usage("groq", "m", 190_000, 0, now=DAY2)          # next day warns again
    assert len(warns()) == 3


def test_tracking_never_raises_with_broken_db(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db is gone")
    for name in ("add_meta_int", "get_meta", "set_meta", "llm_pass_rates"):
        monkeypatch.setattr(db, name, boom)
    llm.record_usage("cloudflare", "@cf/x/y", 10, 10)
    llm.mark_used_up("groq")
    assert llm.quota_lines() == []
    assert llm.health_line({"pitched": 1}) == "Health: scout ok, 1 idea"
