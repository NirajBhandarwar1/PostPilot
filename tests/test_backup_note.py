"""The 'a backup model wrote this' note comes from facts saved when the batch or plan was made, so it prints the
same way every time and render.py needs no live model state (eng review R6)."""

from postpilot import llm, render
from tests.test_options import IDEA, fake_brief
from tests.test_state_and_safety import _kit

BACKUP = ["cloudflare:@cf/openai/gpt-oss-120b"]


def _plan(**kw):
    return render.render_brief(dict(IDEA, handle="a/b", handle_type="github_repo"), fake_brief(), _kit(), "post",
                               (70, 76, "PASS"), {}, [], [], **kw)


def _ideas(**extra):
    return render.render_ideas(1, {"ideas": [], "recommended": 1, "recommended_reason": "r", **extra})


def test_the_plan_note_comes_from_the_saved_facts():
    doc = _plan(writers=BACKUP, expected="groq")
    assert "written by a backup model (cloudflare:@cf/openai/gpt-oss-120b) because groq was unavailable" in doc


def test_the_same_note_prints_again_whatever_the_process_knows_now(monkeypatch):
    first = _plan(writers=BACKUP, expected="groq")
    monkeypatch.setattr(llm, "USAGE", {"groq:openai/gpt-oss-120b": {"calls": 9, "in": 1, "out": 1}})
    monkeypatch.setenv("WRITER_PROVIDERS", "cloudflare")
    assert _plan(writers=BACKUP, expected="groq") == first


def test_no_note_when_the_expected_provider_wrote_it():
    assert "backup model" not in _plan(writers=["groq:openai/gpt-oss-120b"], expected="groq")


def test_an_old_plan_without_the_saved_facts_shows_no_note_and_does_not_crash():
    assert "backup model" not in _plan()
    assert "backup model" not in _plan(writers=BACKUP)          # writers without `expected`: unknown, so quiet


def test_the_ideas_email_note_uses_the_saved_expected_provider():
    assert "groq did not answer" in _ideas(writers=BACKUP, expected="groq")
    assert "backup model" not in _ideas(writers=BACKUP)          # a batch saved before this change
    assert "backup model" not in _ideas(writers=["groq:m"], expected="groq")


def test_render_does_not_import_the_model_layer():
    assert not hasattr(render, "llm")
