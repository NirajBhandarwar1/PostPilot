"""Router stats: calls are recorded, and a model that keeps failing moves to the end (never promoted)."""

import json

import pytest
from pydantic import BaseModel

from postpilot import db, llm


class Out(BaseModel):
    ok: bool


def _env(monkeypatch, order="groq,openrouter,cloudflare", mode=None):
    for k in ("LLM_PROVIDERS", "WRITER_PROVIDERS", *llm.TASK_ENV.values()):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LLM_PROVIDERS", order)
    for k, v in {"GROQ_API_KEY": "g", "OPENROUTER_API_KEY": "o", "CLOUDFLARE_API_TOKEN": "c",
                 "CLOUDFLARE_ACCOUNT_ID": "a", "OPENAI_API_KEY": "p"}.items():
        monkeypatch.setenv(k, v)
    if mode:
        monkeypatch.setenv("PROVIDER_ORDER", mode)
    else:
        monkeypatch.delenv("PROVIDER_ORDER", raising=False)
    llm._no_credit_until.clear()


def _model(name):
    return next(p.model for p in llm.providers("default") if p.name == name)


def _rec(name, ok, bad, role="ideas", model=None, reason="invalid_json"):
    model = model or _model(name)
    for _ in range(ok):
        db.record_llm_call(name, model, role, True, "", 1.0, 100)
    for _ in range(bad):
        db.record_llm_call(name, model, role, False, reason, 1.0, 100)


def _names(role="ideas"):
    return [p.name for p in llm.ordered(llm.providers("default"), role)]


def test_recording_and_rates():
    _rec("groq", 3, 2, model="m")
    assert db.llm_pass_rates()[("groq", "m", "ideas")] == (3, 5)


def test_recording_never_raises(monkeypatch):
    def boom():
        raise RuntimeError("disk gone")
    monkeypatch.setattr(db, "connect", boom)
    db.record_llm_call("groq", "m", "ideas", True)          # no exception
    assert db.llm_pass_rates() == {}


def test_failing_model_moves_to_the_end(monkeypatch):
    _env(monkeypatch)
    _rec("groq", 1, 4)                 # 20% of 5
    _rec("openrouter", 5, 0)
    assert _names() == ["openrouter", "cloudflare", "groq"]


def test_exactly_half_is_not_demoted(monkeypatch):
    _env(monkeypatch)
    _rec("groq", 3, 3)
    assert _names() == ["groq", "openrouter", "cloudflare"]


def test_fewer_than_five_samples_keep_config_order(monkeypatch):
    _env(monkeypatch)
    _rec("groq", 0, 4)                 # terrible but only 4 samples
    assert _names() == ["groq", "openrouter", "cloudflare"]


def test_never_promotes_a_healthy_provider(monkeypatch):
    _env(monkeypatch)
    _rec("groq", 3, 2)                 # 60%: fine
    _rec("openrouter", 20, 0)          # perfect, but configured later
    assert _names() == ["groq", "openrouter", "cloudflare"]


def test_demoted_entries_keep_relative_order(monkeypatch):
    _env(monkeypatch)
    _rec("groq", 0, 5)
    _rec("openrouter", 0, 5)
    assert _names() == ["cloudflare", "groq", "openrouter"]


def test_stats_are_per_model(monkeypatch):
    _env(monkeypatch, order="cloudflare:good-model,cloudflare:bad-model,groq")
    _rec("cloudflare", 0, 6, model="bad-model")
    _rec("cloudflare", 6, 0, model="good-model")
    order = [(p.name, p.model) for p in llm.ordered(llm.providers("default"), "ideas")]
    assert order[0] == ("cloudflare", "good-model")
    assert order[-1] == ("cloudflare", "bad-model")


def test_config_mode_disables_demotion(monkeypatch):
    _env(monkeypatch, mode="config")
    _rec("groq", 0, 6)
    assert _names() == ["groq", "openrouter", "cloudflare"]


def test_role_is_respected(monkeypatch):
    _env(monkeypatch)
    _rec("groq", 0, 6, role="brief")
    assert _names("ideas") == ["groq", "openrouter", "cloudflare"]
    assert _names("brief")[-1] == "groq"


def test_paid_provider_stays_first(monkeypatch):
    _env(monkeypatch, order="openai,groq,openrouter")
    _rec("openai", 0, 9)
    _rec("groq", 0, 6)
    assert _names() == ["openai", "openrouter", "groq"]


def test_too_big_skips_are_not_counted(monkeypatch):
    _env(monkeypatch)
    _rec("groq", 0, 9, reason="too_big")
    assert ("groq", _model("groq"), "ideas") not in db.llm_pass_rates()
    assert _names() == ["groq", "openrouter", "cloudflare"]


def test_chat_json_records_outcomes(monkeypatch):
    _env(monkeypatch, order="groq", mode="config")
    monkeypatch.setattr(llm, "_post", lambda p, body, timeout=600: {
        "choices": [{"message": {"content": json.dumps({"ok": True})}, "finish_reason": "stop"}], "usage": {}})
    llm.chat_json("s", "u", Out, role="ideas")
    assert db.llm_pass_rates()[("groq", _model("groq"), "ideas")] == (1, 1)

    def fail(p, body, timeout=600):
        raise RuntimeError("groq HTTP 503: busy")
    monkeypatch.setattr(llm, "_post", fail)
    with pytest.raises(RuntimeError):
        llm.chat_json("s", "u", Out, role="ideas")
    assert db.llm_pass_rates()[("groq", _model("groq"), "ideas")] == (1, 2)
    with db.connect() as c:
        assert c.execute("SELECT reason FROM llm_calls ORDER BY id DESC").fetchone()[0] == "http_503"
