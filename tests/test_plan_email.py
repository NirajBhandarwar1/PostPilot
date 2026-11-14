"""The plan email is a short summary; the full plan travels as the BUILD.md attachment."""

import types

from postpilot import mailer, pipeline

DOC = """# BUILD: Demo-app

Story: Agent tooling on GitHub  |  Type: demo-app  |  Difficulty: medium - wiring  |  Realistic time: ~82 min

# PART A: Build instructions (for Cursor)

### Step 1 🤖: Create the project (~10 min)
### Step 2 🧑: Review, then run the third-party setup (~5 min)
### Step 3 🤖: Add the wrapper (~12 min)

## Check before building
Unverified claim in the plan: "it is free"
Need to check: could not confirm model 'x' works right now (HTTP 404). Try one call with it first.
Cursor: stop here. Part B is for the human.

# PART B: Demo and LinkedIn post (for you)
## 2. The post
A long post body that must never be pasted into the email.
Human score: 79/100 (PASS)
## 7. Check before you post
Unverified claim in the post: "cost under $3"
## 8. After you post
"""


def _send(monkeypatch):
    sent = []
    monkeypatch.setattr(pipeline, "settings", types.SimpleNamespace(**{**vars(pipeline.settings), "has_email": True}))
    monkeypatch.setattr(mailer, "send", lambda subject, body, html=None, attachments=(): sent.append(
        {"subject": subject, "body": body, "html": html, "attachments": list(attachments)}))
    pipeline._email_build(7, 1, "Demo-app", "Built a CLI in an hour.", DOC)
    return sent[0]


def test_plan_email_is_short_and_the_plan_is_attached(monkeypatch):
    mail = _send(monkeypatch)
    assert len(mail["body"]) < 1600
    assert "A long post body" not in mail["body"] and "Step 1" not in mail["body"]
    assert mail["attachments"] == [("BUILD.md", DOC)]
    assert pipeline.CURSOR_PROMPT in mail["body"]
    assert "A long post body" not in mail["html"]


def test_plan_email_says_what_it_is_at_a_glance(monkeypatch):
    body = _send(monkeypatch)["body"]
    assert "demo-app · medium · about 82 min · 3 steps (1 are yours to do) · post score 79/100" in body
    assert "Check first: 3 thing(s)" in body
