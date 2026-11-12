"""Each call is shrunk to the provider's per-minute token limit; a call that cannot fit never goes out."""

import json

import pytest
from pydantic import BaseModel

from postpilot import llm


class Out(BaseModel):
    ok: bool


def _env(monkeypatch, order="groq", tpm="8000"):
    for k in ("LLM_PROVIDERS", "WRITER_PROVIDERS", *llm.TASK_ENV.values()):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LLM_PROVIDERS", order)
    monkeypatch.setenv("GROQ_API_KEY", "g")
    monkeypatch.setenv("GROQ_TPM", tpm)
    monkeypatch.setenv("PROVIDER_ORDER", "config")
    llm._no_credit_until.clear()


def _reply():
    return {"choices": [{"message": {"content": json.dumps({"ok": True})}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


@pytest.fixture
def sent(monkeypatch):
    calls = []

    def fake_post(p, body, timeout=600):
        calls.append((p.name, body))
        return _reply()
    monkeypatch.setattr(llm, "_post", fake_post)
    return calls


def test_estimate_is_pure_and_has_margin():
    assert llm._estimate_tokens("a" * 350) >= 110     # 100 tokens + 10%
    assert llm._estimate_tokens("") >= 0


def test_clamps_max_tokens_to_fit(monkeypatch, sent):
    _env(monkeypatch)
    user = "x" * int(3.5 * 3000)                      # ~3,000 input tokens (before margin)
    llm.chat_json("sys", user, Out, max_tokens=6000)
    body = sent[0][1]
    assert body["max_tokens"] < 6000
    assert llm._estimate_tokens("sys", user) + body["max_tokens"] <= 8000 - llm.TPM_MARGIN


def test_small_request_is_untouched_and_never_raised(monkeypatch, sent):
    _env(monkeypatch)
    llm.chat_json("s", "u", Out, max_tokens=3000)
    assert sent[0][1]["max_tokens"] == 3000           # plenty of room: unchanged, not increased


def test_skips_provider_without_a_request_when_too_small(monkeypatch, sent):
    _env(monkeypatch)
    with pytest.raises(RuntimeError, match="request too big for its per-minute limit"):
        llm.chat_json("s", "x" * 40000, Out, max_tokens=4000)      # ~12k input tokens > 8k
    assert sent == []


def test_unknown_tpm_is_untouched(monkeypatch, sent):
    _env(monkeypatch, order="openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    llm.chat_json("s", "x" * 40000, Out, max_tokens=4000)
    assert sent[0][0] == "openrouter" and sent[0][1]["max_tokens"] == 4000


def test_falls_through_to_next_provider(monkeypatch, sent):
    _env(monkeypatch, order="groq,openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    llm.chat_json("s", "x" * 40000, Out, max_tokens=4000)
    assert [n for n, _ in sent] == ["openrouter"]


def test_clamp_applies_after_token_scale(monkeypatch, sent):
    p = llm.Provider("x", "http://x", "m", "k", True, {}, token_scale=2.5, tpm=8000)
    monkeypatch.setattr(llm, "providers", lambda role="default": [p])
    monkeypatch.setenv("PROVIDER_ORDER", "config")
    llm.chat_json("s", "u", Out, max_tokens=4000)      # 4000 * 2.5 = 10000 > room
    assert sent[0][1]["max_tokens"] < 10000
    assert llm._estimate_tokens("s", "u") + sent[0][1]["max_tokens"] <= 8000


def test_task_effort_default_only_when_caller_gave_none(monkeypatch, sent):
    _env(monkeypatch)
    llm.chat_json("s", "u", Out, max_tokens=1000, role="options")
    llm.chat_json("s", "u", Out, max_tokens=1000, role="options", effort="high")
    assert sent[0][1]["reasoning_effort"] == "low"
    assert sent[1][1]["reasoning_effort"] == "high"


def test_a_busy_provider_with_a_fallback_behind_it_is_retried_once_and_briefly(monkeypatch):
    import contextlib
    import io
    import urllib.error
    import urllib.request

    calls, sleeps = [], []
    monkeypatch.setattr(llm.time, "sleep", lambda s: sleeps.append(s))

    def busy(req, timeout=0):
        calls.append(1)
        raise urllib.error.HTTPError("u", 503, "x", {}, io.BytesIO(b"busy"))

    monkeypatch.setattr(urllib.request, "urlopen", busy)
    p = llm.Provider("a", "https://x", "m", "k", True, {})
    for patient, expected_calls in ((False, 2), (True, 4)):
        calls.clear()
        sleeps.clear()
        with contextlib.suppress(RuntimeError):
            llm._post(p, {}, patient=patient)
        assert len(calls) == expected_calls
        assert max(sleeps) <= (30 if not patient else 120)
