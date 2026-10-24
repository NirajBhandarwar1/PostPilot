"""Build options: reply "3" -> 3-5 fact-checked, priced build options; reply to "#8.3" -> build one;
"3 go" -> build the top option straight away."""

import json
import types

import pytest

from postpilot import brain, db, factgate as fg, mailer, pipeline

CAT = fg.Catalog([fg.Model("groq", "openai/gpt-oss-120b"),
                  fg.Model("openai", "gpt-6-sol", paid=True, price_in=2.0, price_out=10.0)])
IDEA = {"title": "Can morluto/rea reverse-engineer a rule it can't see?", "topic": "rea goes viral",
        "handle_type": "github_repo", "handle": "morluto/rea", "entities": ["morluto/rea", "rea"],
        "evidence": ["[GitHub trending] morluto/rea: Reverse engineer anything with agents (+129,417 stars)"],
        "evidence_titles": ["morluto/rea: Reverse engineer anything with agents"],
        "source_urls": ["https://github.com/morluto/rea"], "models_to_run": [], "format": "build-with-repo",
        "proof": "experiments/2026-10-22-rea/", "check_before_posting": []}


def option(title, kind="demo-app", models=(), hook=None, minutes=40, difficulty="easy"):
    return brain.BuildOption(
        title=title, kind=kind, inspired_by="How To Use REA To Clone Any Software In Minutes (359,678 views)",
        your_edge="Measures it on a target you own.", what_you_build="A tiny app with a hidden rule, then run rea on it.",
        post_shows="A 30-second screen recording.", why_hiring_cares="Tests agents against ground truth.",
        who_it_impresses="Founders hiring a founding AI engineer", stand_out="Everyone else just reposts.",
        minutes=minutes, difficulty=difficulty, difficulty_why="Small app, one tool.",
        steps_preview=["Write the app", "Run rea", "Compare"], models_to_run=list(models), needs=[],
        risk="rea may need a GPU; fallback: smaller target.", hook=hook or f"{title}: can rea find it?")


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline, "ROOT", tmp_path)
    monkeypatch.setattr(pipeline, "settings", types.SimpleNamespace(
        output_dir=tmp_path / "linkedin", voice_file=tmp_path / "voice.md", data_dir=tmp_path / "data",
        has_email=False))
    monkeypatch.setattr(pipeline.factgate, "load_catalogs", lambda: CAT)
    calls = {"options": 0, "builds": []}

    def make_options(idea, voice, keys, notes=None, research_text="", extra=""):
        calls["options"] += 1
        return brain.BuildOptions(options=[
            option("Hidden-rule demo"),
            option("Public harness", kind="open-source-tool"),
            option("Agent face-off", kind="face-off", models=["openai:gpt-6-sol", "groq:openai/gpt-oss-120b"],
                   minutes=150, difficulty="hard"),
            option("Made-up model run", kind="face-off", models=["groq:imaginary-9"]),     # fails the gate
            option("Bragging hook", hook="rea found 9 of 10 hidden rules in my app."),     # unproven result
        ], recommended_reason="Best wow per minute", recommended=2)
    monkeypatch.setattr(pipeline.brain, "make_options", make_options)
    monkeypatch.setattr(pipeline, "build",
                        lambda b, c, send_email=True, option=None: calls["builds"].append((b, c, option)))
    bid = db.add_batch({"ideas": [dict(IDEA, title=f"Idea {n}") if n != 3 else IDEA for n in (1, 2, 3)]},
                       status="sent")
    return bid, calls


def test_propose_gates_prices_and_saves(env, tmp_path):
    bid, calls = env
    res = pipeline.propose(bid, 3, send_email=False)
    assert [o["title"] for o in res["options"]] == ["Hidden-rule demo", "Public harness", "Agent face-off"]
    assert res["recommended"] == 2                       # recommended option kept its number
    assert res["options"][0]["est_cost"] == 0 and res["options"][2]["est_cost"] > 0
    row = db.get_batch(bid)
    assert row["status"] == "options_sent"
    assert len(json.loads(row["ideas_json"])["options"]["3"]["options"]) == 3
    ideas = tmp_path / "linkedin" / "ideas"
    short = next(ideas.glob("*-options.md")).read_text(encoding="utf-8")
    for part in ("what to build on morluto/rea", "Reply with a number (1-3)", "1. Hidden-rule demo   [LOOK WHAT I BUILT]",
                 "[PUBLIC GITHUB REPO]", "What it is: ", "What you show: ", "Why it gets you hired: ",
                 "Inspired by: How To Use REA", "Effort: easy", "2. Public harness   [PUBLIC GITHUB REPO]   ★ MY PICK",
                 "Effort: hard · 2.5 h · about $0.14", "Why I pick #2:"):
        assert part in short, part
    assert len(short.splitlines()) <= 60                      # readable on a phone
    full = next(ideas.glob("*-options-full.md")).read_text(encoding="utf-8")
    for part in ("WHY HIRING PEOPLE CARE", "WHY IT STANDS OUT", "RISK:", "INSPIRED BY"):
        assert part in full, part


def test_reply_routing(env):
    bid, calls = env
    pipeline.handle_reply(bid, None, "3")                # ideas email -> options, no build yet
    assert calls["options"] == 1 and calls["builds"] == []
    pipeline.handle_reply(bid, 3, "1")                   # options email "#N.3" -> build option 1
    assert calls["builds"] == [(bid, 3, 1)]
    pipeline.handle_reply(bid, None, "3 go")             # shortcut -> straight to my top option
    assert calls["builds"][-1] == (bid, 3, 2)


def test_build_uses_the_picked_option(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline, "ROOT", tmp_path)
    monkeypatch.setattr(pipeline, "settings", types.SimpleNamespace(
        output_dir=tmp_path / "linkedin", voice_file=tmp_path / "voice.md", data_dir=tmp_path / "data",
        has_email=False))
    monkeypatch.setattr(pipeline.factgate, "load_catalogs", lambda: CAT)
    seen = {}

    def build_post(idea, v, h, **k):
        seen.update(idea)
        return brain.PostKit(summary="s", steps=["x"], post_draft="Hook.", placeholders=[], alt_hooks=[],
                             visual_spec="v", fact_checks=[])
    monkeypatch.setattr(pipeline.brain, "build_post", build_post)
    monkeypatch.setattr(pipeline.brain, "build_brief", lambda *a, **k: fake_brief(uses_models=True))
    o = option("Agent face-off", kind="face-off", models=["openai:gpt-6-sol"]).model_dump()
    bid = db.add_batch({"ideas": [IDEA], "options": {"1": {"options": [o]}}}, status="options_sent")
    out = pipeline.build(bid, 1, send_email=False, option=1)
    assert seen["title"] == "Agent face-off" and seen["models_to_run"] == ["openai:gpt-6-sol"]
    assert seen["option"]["kind"] == "face-off"                 # the brief sees the full option
    doc = (out / "BUILD.md").read_text(encoding="utf-8")
    assert "| `gpt-6-sol` | `https://api.openai.com/v1` | `OPENAI_API_KEY` |" in doc
    assert "OPENAI_API_KEY=" in doc and "# PART A" in doc and "# PART B" in doc
    assert seen["topic"] == "rea goes viral" and "Run rea" in seen["build_plan"]
    with pytest.raises(ValueError, match="no option 4"):
        pipeline.build(bid, 1, send_email=False, option=4)


def test_reply_parsing_for_options():
    assert mailer.parse_reply("4", n_ideas=5) == ("4", None)
    assert mailer.parse_reply("4", n_ideas=3) == (None, None)      # no option 4 shown
    assert mailer.parse_reply("3 go", n_ideas=3) == ("3 go", None)
    m = mailer.TAG_RE.search("Re: [PostPilot #8.3] Build options")
    assert m.group(1) == "8" and m.group(2) == "3"
    assert mailer.TAG_RE.search("Re: [PostPilot #8] ideas").group(2) is None
    assert mailer.subject_for(8, "x", idea=3) == "[PostPilot #8.3] x"


def test_pick_reason_uses_shown_numbers(env, monkeypatch):
    bid, calls = env
    res = pipeline.propose(bid, 3, send_email=False)
    assert res["recommended"] == 2
    o = brain.BuildOptions(options=[option("Dropped", kind="face-off", models=["groq:imaginary-9"]),
                                    option("Kept demo"), option("Repo", kind="open-source-tool")],
                           recommended_reason="Option 2 is best.", recommended=2)
    monkeypatch.setattr(pipeline.brain, "make_options", lambda *a, **k: o)
    res = pipeline.propose(bid, 3, send_email=False, fresh=True)       # D23: ask for a new list
    assert res["recommended"] == 1 and res["recommended_reason"] == "Option 1 is best."


def test_retry_when_no_demo_app(env, monkeypatch):
    bid, calls = env
    seen = []
    no_demo = brain.BuildOptions(options=[option("Eval A", kind="face-off", models=["groq:openai/gpt-oss-120b"]),
                                          option("Eval B", kind="stress-test")], recommended_reason="r", recommended=1)
    with_demo = brain.BuildOptions(options=[option("Eval A", kind="face-off", models=["groq:openai/gpt-oss-120b"]),
                                            option("Cool app", kind="demo-app"),
                                            option("Repo", kind="open-source-tool")], recommended_reason="r", recommended=2)

    def make_options(*a, extra="", **k):
        seen.append(extra)
        return no_demo if len(seen) == 1 else with_demo
    monkeypatch.setattr(pipeline.brain, "make_options", make_options)
    res = pipeline.propose(bid, 3, send_email=False)
    assert len(seen) == 2 and seen[0] == "" and "demo-app" in seen[1] and "open-source-tool" in seen[1]
    assert [o["kind"] for o in res["options"]] == ["face-off", "demo-app", "open-source-tool"]


def fake_brief(**kw):
    base = dict(project_name="csv-first-aid", goal="A tiny app.", done_criteria=["app runs"],
                stack=["Python 3.12: simple"], file_tree=[brain.FileSpec(path="app.py", purpose="the app")],
                setup_commands=["python -m venv .venv", "pip install requests"],
                tasks=[brain.BuildTask(title="Step 1: setup", details="Create the project.", done_when="pytest passes")],
                tests=["test parse"], demo_data="3 sample CSVs", readme_outline=["What it is"],
                pitfalls=["Windows paths"], demo_script=["Show the app"])
    base.update(kw)
    return brain.CursorBrief(**base)


def test_build_emails_the_plan_back(monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(pipeline, "ROOT", tmp_path)
    monkeypatch.setattr(pipeline, "settings", types.SimpleNamespace(
        output_dir=tmp_path / "linkedin", voice_file=tmp_path / "voice.md", data_dir=tmp_path / "data",
        has_email=True))
    monkeypatch.setattr(pipeline.factgate, "load_catalogs", lambda: CAT)
    monkeypatch.setattr(pipeline.brain, "build_post", lambda i, v, h, **k: brain.PostKit(
        summary="Plan ready.", steps=[], post_draft="Hook.", placeholders=[], alt_hooks=[], visual_spec="v",
        fact_checks=[]))
    monkeypatch.setattr(pipeline.brain, "build_brief", lambda *a, **k: fake_brief())
    monkeypatch.setattr(pipeline.mailer, "send", lambda subj, body, html=None, attachments=():
                        sent.append((subj, body, attachments, html)))
    o = option("Cool app").model_dump()
    bid = db.add_batch({"ideas": [IDEA], "options": {"1": {"options": [o]}}}, status="options_sent")
    pipeline.build(bid, 1, option=1)
    subj, body, att, html = sent[0]
    assert "<h1" in html and "<pre" in html and "stop and tell me exactly what to do" in html
    assert subj.startswith(f"[PostPilot #{bid}.1 plan] Build plan ready")
    assert att[0][0] == "BUILD.md" and "# PART A" in att[0][1]
    assert "# PART A" in body and "Read BUILD.md. Build Part A one step at a time" in body      # readable in the email too


def test_post_rewritten_until_it_reads_human(monkeypatch):
    flat = ("this tool checks claims against outputs and reports results. it is useful for teams that "
            "rely on agents and want evidence. it saves time and reduces risk for everyone involved. "
            "it works well in practice and it is simple to adopt for most teams today.")
    good = ("I gave DeepSeek's DeepInfer 20 inputs. It didn't blink. Then GitHub user aicodeking posted "
            "leanrea with 1,545 stars, and I wondered whether the README claims survive a real Windows "
            "laptop with 16 GB and no GPU at all. Short answer? {{result}}. I'm publishing the Python "
            "checker so you can rerun all 20 cases in 3 minutes. Here's what broke first: {{first break}}.")
    calls = []

    def build_post(idea, v, h, feedback="", plan="", facts=""):
        calls.append(feedback)
        return brain.PostKit(summary="s", steps=[], post_draft=flat if len(calls) == 1 else good,
                             placeholders=[], alt_hooks=[], visual_spec="v", fact_checks=[])
    monkeypatch.setattr(pipeline.brain, "build_post", build_post)
    kit, clean, report, before, after = pipeline._write_post(
        {"research": {"repos": [{"title": "aicodeking/leanrea: x", "stars": 1545}]}}, plan="p")
    assert len(calls) >= 2 and calls[0] == "" and "REWRITE" in calls[1] and "specificity" in calls[1]
    assert after[1] > 60 and "DeepInfer" in clean                     # the better draft was kept
    assert "1,545 stars" in pipeline._facts({"research": {"repos": [{"title": "aicodeking/leanrea: x", "stars": 1545}]}})
