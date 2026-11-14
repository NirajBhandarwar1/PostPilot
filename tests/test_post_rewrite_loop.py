"""The post rewrite loop: no network, a fake writer. Rewrites must edit the best draft so far,
stop after one that does not improve, keep the best, and stop early on PASS."""
from postpilot import brain, pipeline

FLAT = ("it is a good tool and it helps people. it saves time and reduces risk for everyone involved. "
        "it works well in practice and it is simple to adopt for most teams today. " * 3)
GOOD = ("I gave DeepSeek's DeepInfer 20 inputs. It didn't blink. Then GitHub user aicodeking posted "
        "leanrea with 1,545 stars, and I wondered whether the README claims survive a real Windows "
        "laptop with 16 GB and no GPU at all. Short answer? {{result}}. I'm publishing the Python "
        "checker so you can rerun all 20 cases in 3 minutes. Here's what broke first: {{first break}}.")


def _run(monkeypatch, drafts):
    calls = []

    def build_post(idea, v, h, feedback="", plan="", facts="", previous=""):
        calls.append({"feedback": feedback, "previous": previous})
        return brain.PostKit(summary="s", steps=[], post_draft=drafts[min(len(calls), len(drafts)) - 1],
                             placeholders=[], alt_hooks=[], visual_spec="v", fact_checks=[])
    monkeypatch.setattr(pipeline.brain, "build_post", build_post)
    out = pipeline._write_post({}, plan="p")
    return calls, out


def test_stops_after_one_non_improving_rewrite(monkeypatch):
    calls, out = _run(monkeypatch, [FLAT, FLAT, FLAT])
    assert len(calls) == 2                     # draft + one rewrite, no third try
    assert out[4][1] < pipeline.POST_TARGET


def test_rewrite_receives_best_draft_and_feedback(monkeypatch):
    calls, _ = _run(monkeypatch, [FLAT, GOOD])
    assert calls[0]["previous"] == "" and calls[0]["feedback"] == ""
    assert "REWRITE" in calls[1]["feedback"]
    assert calls[1]["previous"].strip() and "it is a good tool" in calls[1]["previous"]


def test_keeps_the_best_draft(monkeypatch):
    calls, out = _run(monkeypatch, [GOOD, FLAT, FLAT])
    assert "DeepInfer" in out[1]
    assert len(calls) <= 2
    if len(calls) == 2:                        # the rewrite edited the best (first) draft
        assert "DeepInfer" in calls[1]["previous"]


def test_pass_stops_early(monkeypatch):
    monkeypatch.setattr(pipeline, "_score", lambda t: ({}, 90.0, "PASS"))
    calls, out = _run(monkeypatch, [GOOD, GOOD, GOOD])
    assert len(calls) == 1 and out[4][2] == "PASS"
