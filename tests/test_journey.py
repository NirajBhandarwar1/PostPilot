"""The whole email journey through real pipeline code (eng review D12): scout -> ideas email ->
reply "1" -> options email -> reply "2" -> BUILD.md email. Only the model, the web and Gmail are
faked. Catches hand-off bugs between steps: wrong tag, wrong batch, wrong option built."""

import json
import types

import pytest

from postpilot import brain, mailer, pipeline
from tests.test_options import fake_brief, option
from tests.test_pipeline_gate import CAT, SIGNALS, TOPICS, make_idea


@pytest.fixture
def world(monkeypatch, tmp_path):
    sent, inbox, marked = [], [], []
    monkeypatch.setattr(pipeline, "settings", types.SimpleNamespace(
        output_dir=tmp_path / "linkedin", voice_file=tmp_path / "voice.md", data_dir=tmp_path / "data",
        has_email=True, cycle_days=3, check_minutes=10))
    monkeypatch.setattr(pipeline.llm, "available", lambda: True)
    monkeypatch.setattr(pipeline.sources, "collect", lambda: SIGNALS)
    monkeypatch.setattr(pipeline.factgate, "load_catalogs", lambda: CAT)
    # fake model
    monkeypatch.setattr(pipeline.brain, "find_topics", lambda s: brain.TopicScan(topics=TOPICS))
    monkeypatch.setattr(pipeline.brain, "make_ideas", lambda chosen, *a, **k: brain.IdeaBatch(
        ideas=[make_idea(n) for n in range(1, len(chosen) + 1)], recommended_reason="r", recommended=1))
    monkeypatch.setattr(pipeline.brain, "make_options", lambda *a, **k: brain.BuildOptions(
        options=[option("Quick demo"), option("Public benchmark repo", kind="open-source-tool")],
        recommended_reason="Option 2 is best.", recommended=2))
    monkeypatch.setattr(pipeline.brain, "build_brief", lambda *a, **k: fake_brief())
    monkeypatch.setattr(pipeline.brain, "build_post", lambda *a, **k: brain.PostKit(
        summary="Plan ready.", steps=[], post_draft="I built it. 20 inputs, 3 models.", placeholders=[],
        alt_hooks=[], visual_spec="v", fact_checks=[]))
    # fake Gmail
    monkeypatch.setattr(pipeline.mailer, "send", lambda subj, body, html=None, attachments=():
                        sent.append({"subject": subj, "body": body, "attachments": attachments}))
    monkeypatch.setattr(pipeline.mailer, "fetch_replies", lambda: [inbox.pop(0)] if inbox else [])
    monkeypatch.setattr(pipeline.mailer, "mark_read", lambda uids: marked.extend(uids))
    return types.SimpleNamespace(sent=sent, inbox=inbox, marked=marked)


def test_full_email_journey(world):
    bid = pipeline.scout()
    ideas_email = world.sent[-1]
    assert ideas_email["subject"] == f"[PostPilot #{bid}] What's hot in AI: reply with a number"
    assert "1. Idea 1" in ideas_email["body"]

    world.inbox.append((bid, None, "1", b"uid-1"))            # you reply "1" to the ideas email
    pipeline.check_replies()
    options_email = world.sent[-1]
    assert options_email["subject"].startswith(f"[PostPilot #{bid}.1] Build options")
    assert "2. Public benchmark repo" in options_email["body"] and "★ MY PICK" in options_email["body"]

    world.inbox.append((bid, "1", "2", b"uid-2"))             # you reply "2" to the options email
    pipeline.check_replies()
    plan_email = world.sent[-1]
    # "plan" in the tag: a reply to the plan ("step 4 failed") can never start another build
    assert plan_email["subject"].startswith(f"[PostPilot #{bid}.1 plan] Build plan ready: Public benchmark repo")
    name, doc = plan_email["attachments"][0]
    assert name == "BUILD.md" and doc.startswith("# BUILD: Public benchmark repo")   # the option you picked
    assert world.marked == [b"uid-1", b"uid-2"]               # each reply marked read after handling
    first_story = json.loads(pipeline.db.get_batch(bid)["ideas_json"])["ideas"][0]["topic"]
    assert [h["topic"] for h in pipeline._history()] == [first_story]   # the build counts as covered
