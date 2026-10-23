"""2026-10-22 upgrade: rank by real attention; ideas written by the 'writer' provider list."""

from postpilot import llm, pipeline


def sig(kind, rank, eng=0):
    return {"kind": kind, "rank": rank, "title": "t", "url": "u", "engagement": eng, "detail": ""}


def test_viral_hn_story_beats_wide_news_coverage():
    signals = [sig("Hacker News", 0, 959), sig("Hugging Face trending models", 0)] + \
              [sig("News", i) for i in range(8)]
    viral = {"signal_ids": [0, 1], "buzz": 7}
    covered = {"signal_ids": list(range(2, 10)), "buzz": 7}
    assert pipeline._heat(viral, signals)[0] > pipeline._heat(covered, signals)[0]


def test_news_coverage_is_capped():
    signals = [sig("News", i) for i in range(40)]
    few = pipeline._heat({"signal_ids": list(range(8)), "buzz": 5}, signals)[0]
    many = pipeline._heat({"signal_ids": list(range(40)), "buzz": 5}, signals)[0]
    assert many == few


def test_more_hn_points_means_hotter():
    a = pipeline._heat({"signal_ids": [0], "buzz": 5}, [sig("Hacker News", 0, 2291)])[0]
    b = pipeline._heat({"signal_ids": [0], "buzz": 5}, [sig("Hacker News", 0, 120)])[0]
    assert a > b


def test_writer_role_uses_writer_providers(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDERS", "groq")
    monkeypatch.setenv("WRITER_PROVIDERS", "openai,qwen")
    monkeypatch.setenv("GROQ_API_KEY", "g")
    monkeypatch.setenv("OPENAI_API_KEY", "o")
    assert [p.name for p in llm.providers()] == ["groq"]
    writers = llm.providers("writer")
    assert [p.name for p in writers] == ["openai", "qwen"]
    assert writers[0].tokens_field == "max_completion_tokens"


def test_writer_falls_back_to_default_list(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDERS", "groq")
    monkeypatch.delenv("WRITER_PROVIDERS", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "g")
    assert [p.name for p in llm.providers("writer")] == ["groq"]


def test_openai_request_uses_completion_token_field(monkeypatch):
    monkeypatch.setenv("WRITER_PROVIDERS", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "o")
    seen = {}

    def fake_post(p, body, timeout=600):
        seen.update(body)
        return {"choices": [{"finish_reason": "stop", "message": {"content": '{"ok": true}'}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 7}}
    monkeypatch.setattr(llm, "_post", fake_post)
    from pydantic import BaseModel

    class OK(BaseModel):
        ok: bool
    assert llm.chat_json("s", "u", OK, max_tokens=1000, role="writer").ok
    assert "max_tokens" not in seen and seen["max_completion_tokens"] == 3000
    assert llm.USAGE["openai:gpt-6-sol"]["out"] >= 7
