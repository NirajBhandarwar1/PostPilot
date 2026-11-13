"""A model is recommended only when its provider has a key AND it answered a live call. No network."""

import io
import json
import urllib.error

import pytest

from postpilot import db, llm, pipeline
from postpilot import factgate as fg

REAL_PROBE = llm.probe_model            # conftest replaces llm.probe_model for every other test
GEMMA = "openrouter:google/gemma-4-26b-a4b-it"
GPT = "groq:openai/gpt-oss-120b"
CAT = fg.Catalog([fg.Model("groq", "openai/gpt-oss-120b"),
                  fg.Model("openrouter", "google/gemma-4-26b-a4b-it:free"),
                  fg.Model("cloudflare", "@cf/openai/gpt-oss-120b")])
OPT = {"title": "Face-off", "kind": "face-off", "what_you_build": "run models", "hook": "which is better?",
       "steps_preview": ["run"], "needs": [], "models_to_run": [GPT]}
IDEA = {"entities": [], "evidence": [], "evidence_titles": []}


@pytest.fixture
def real_probe(monkeypatch):
    monkeypatch.setattr(llm, "probe_model", REAL_PROBE)
    monkeypatch.setenv("GROQ_API_KEY", "k")
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")


def fake_urlopen(monkeypatch, behaviour):
    calls = []

    def urlopen(req, timeout=0):
        calls.append(json.loads(req.data)["model"])
        return behaviour()
    monkeypatch.setattr(llm.urllib.request, "urlopen", urlopen)
    return calls


def ok_reply():
    return io.BytesIO(json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode())


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http(code):
    def raise_it():
        raise urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(b"not found"))
    return raise_it


def test_models_without_a_key_are_not_offered(monkeypatch):
    for k in ("GROQ_API_KEY", "OPENROUTER_API_KEY", "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN", "OPENAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "k")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")            # account id missing: still not runnable
    assert [m.key for m in fg.shortlist(CAT, [])] == [GPT]


def test_option_with_unconfirmed_model_is_kept_with_a_need_to_check_warning(monkeypatch):
    monkeypatch.setattr(llm, "probe_model", lambda p, m, now=None: (False, "HTTP 404"))
    v = pipeline._check_option(dict(OPT, models_to_run=[GPT]), IDEA, CAT)
    assert v.ok                                         # not dropped: the user decides
    assert any(f"Need to check: could not confirm model '{GPT}'" in w and "HTTP 404" in w for w in v.unverified)


def test_option_with_working_model_is_kept_and_unique_models_probed_once(monkeypatch):
    seen = []
    monkeypatch.setattr(llm, "probe_model", lambda p, m, now=None: seen.append((p, m)) or (True, "ok"))
    v = pipeline._check_option(dict(OPT, models_to_run=[GPT, GPT]), IDEA, CAT)
    assert v.ok and seen == [("groq", "openai/gpt-oss-120b")]


def test_no_probe_without_models_or_for_options_the_gate_already_dropped(monkeypatch):
    seen = []
    monkeypatch.setattr(llm, "probe_model", lambda p, m, now=None: seen.append(m) or (True, "ok"))
    assert pipeline._check_option(dict(OPT, kind="demo-app", models_to_run=[]), IDEA, CAT).ok
    assert not pipeline._check_option(dict(OPT, models_to_run=["groq:imaginary-9"]), IDEA, CAT).ok
    assert seen == []


def test_probe_success_is_cached(real_probe, monkeypatch):
    calls = fake_urlopen(monkeypatch, lambda: Resp(ok_reply().getvalue()))
    assert llm.probe_model("groq", "m1") == (True, "ok")
    assert llm.probe_model("groq", "m1") == (True, "ok")
    assert calls == ["m1"]                                      # second call made no request


def test_failures_expire_sooner_than_successes(real_probe, monkeypatch):
    t0 = 1_000_000.0
    calls = fake_urlopen(monkeypatch, http(404))
    assert llm.probe_model("openrouter", "bad", now=t0) == (False, "HTTP 404")
    llm.probe_model("openrouter", "bad", now=t0 + 600)
    assert len(calls) == 1                                      # still cached after 10 min
    llm.probe_model("openrouter", "bad", now=t0 + llm.PROBE_FAIL_S + 1)
    assert len(calls) == 2                                      # re-tested after the failure window
    calls = fake_urlopen(monkeypatch, lambda: Resp(ok_reply().getvalue()))
    llm.probe_model("groq", "good", now=t0)
    llm.probe_model("groq", "good", now=t0 + llm.PROBE_FAIL_S + 1)
    assert len(calls) == 1                                      # a success outlives that window
    llm.probe_model("groq", "good", now=t0 + llm.PROBE_OK_S + 1)
    assert len(calls) == 2
    assert llm.PROBE_FAIL_S < llm.PROBE_OK_S


def test_probe_never_raises(real_probe, monkeypatch):
    fake_urlopen(monkeypatch, lambda: (_ for _ in ()).throw(TimeoutError("slow")))
    ok, why = llm.probe_model("groq", "m")
    assert not ok and "TimeoutError" in why
    monkeypatch.delenv("GROQ_API_KEY")
    assert llm.probe_model("groq", "other")[0] is False         # no key: a reason, not an exception
    fake_urlopen(monkeypatch, http(429))
    assert llm.probe_model("openrouter", "busy") == (False, "HTTP 429")
    monkeypatch.setattr(db, "get_meta", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db")))
    monkeypatch.setattr(db, "set_meta", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db")))
    fake_urlopen(monkeypatch, lambda: Resp(b"{not json"))
    assert llm.probe_model("openrouter", "garbled")[0] is False
