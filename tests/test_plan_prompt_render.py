"""Prompt rules for the plan/post (OpenAI v1 style, pip vs npm, image input, no invented claims) and
render_brief dropping the model's own record/publish steps (render appends the fixed ones)."""

import contextlib
import re

from postpilot import brain
from tests.test_options import IDEA, fake_brief
from tests.test_state_and_safety import _kit


def _capture(monkeypatch, fn):
    seen = []

    def chat_json(system, prompt, *a, **k):
        seen.append(prompt)
        raise RuntimeError("stop")
    monkeypatch.setattr(brain.llm, "chat_json", chat_json)
    with contextlib.suppress(RuntimeError):
        fn()
    return seen[0]


def test_brief_prompt_has_the_new_rules(monkeypatch):
    p = _capture(monkeypatch, lambda: brain.build_brief(dict(IDEA, option={"title": "T"})))
    for phrase in ("client.chat.completions.create", "never `provider:model`", "never mix",
                   "image", "vision-capable", "Never pretend", "paid key", "reuse visual_spec",
                   "record/demo/publish"):
        assert phrase in p, phrase


def test_post_prompt_bans_invented_claims(monkeypatch):
    p = _capture(monkeypatch, lambda: brain.build_post(IDEA, "", {"hooks": []}))
    assert "product" in p and "employer" in p and "customers" in p
    assert "admin panel" in p and "placeholders" in p


def test_render_drops_model_record_and_publish_steps():
    from postpilot import pipeline
    tasks = [brain.BuildTask(title="Step 1: Write parser", details="Create parser.py.", done_when="ok"),
             brain.BuildTask(title="Record demo and publish repo", details="Record the demo, then gh repo create.",
                             done_when="repo is live"),
             brain.BuildTask(title="Add the README", details="Write README.md.", done_when="ok")]
    doc = pipeline.render_brief(dict(IDEA, handle_type="model", handle="x"), fake_brief(tasks=tasks), _kit(),
                                "post", (70, 76, "PASS"), {}, [], [])
    heads = re.findall(r"^### Step (\d+) \S+: (.+?)  \(", doc, re.M)
    assert [int(n) for n, _ in heads] == list(range(1, len(heads) + 1))
    titles = [t for _, t in heads]
    assert sum("ecord" in t for t in titles) == 1 and sum("ublish" in t for t in titles) == 1
    assert "Write parser" in titles and "Add the README" in titles and len(titles) == 6


def test_model_numbering_is_stripped_from_step_titles():
    import re

    from postpilot import render
    clean = lambda t: re.sub(render._TITLE_PREFIX, "", t, flags=re.I)  # noqa: E731
    assert clean("2️⃣ Pull the docx-edit skill") == "Pull the docx-edit skill"
    assert clean("Task 2 – Scaffold the front-end") == "Scaffold the front-end"
    assert clean("Step 3: Add the wrapper") == "Add the wrapper"
    assert clean("4. Write tests") == "Write tests"
    assert clean("3D viewer for results") == "3D viewer for results"      # a number that is part of the name
