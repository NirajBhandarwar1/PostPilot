"""Gate outcomes with a fake writer (no model calls): send, regenerate, short batch, nothing.
Also regression contract D11 (2): ideas keep their topic, heat and links after the gate."""

import types

import pytest

from postpilot import brain, factgate as fg, pipeline

CAT = fg.Catalog([fg.Model("groq", "openai/gpt-oss-120b"), fg.Model("groq", "qwen/qwen3.8-27b"),
                  fg.Model("cloudflare", "@cf/moonshotai/kimi-k2.6")])
GOOD_MODELS = ["groq:openai/gpt-oss-120b", "groq:qwen/qwen3.8-27b"]

SIGNALS = [
    {"kind": "News", "rank": 0, "title": "Google launches Gemini 4 Argon", "url": "https://n/0", "detail": "19 Oct"},
    {"kind": "Hacker News", "rank": 1, "title": "Gemini 4 Argon hands-on", "url": "https://n/1", "detail": "300 points"},
    {"kind": "News", "rank": 2, "title": "Kimi K2.6 tops coding leaderboard", "url": "https://n/2", "detail": "18 Oct"},
    {"kind": "Reddit", "rank": 0, "title": "r/LocalLLaMA: Kimi K2.6 is wild", "url": "https://r/3", "detail": "#1"},
    {"kind": "News", "rank": 3, "title": "Anthropic opens Sydney office", "url": "https://n/4", "detail": "17 Oct"},
    {"kind": "News", "rank": 4, "title": "Unrelated story about phones", "url": "https://n/5", "detail": "17 Oct"},
]


def topic(name, ids, entities, buzz=8):
    return brain.HotTopic(name=name, kind="launch", what_happened="x", the_debate="y", signal_ids=ids,
                          buzz=buzz, how_to_try="api", entities=entities)


TOPICS = [topic("Gemini 4 Argon launch", [0, 1], ["gemini 4 argon", "google"], 9),
          topic("Kimi K2.6 coding lead", [2, 3], ["kimi k2.6", "moonshot"], 8),
          topic("Anthropic Sydney office", [4], ["anthropic", "sydney"], 5),
          topic("Phone story", [5], ["nothingmatches"], 9)]   # no source names it -> excluded


def make_idea(n, ok=True, title=None):
    return brain.Idea(topic_number=n, title=title or f"Idea {n}", topic="t", format="face-off", why_hot="hot",
                      role_angle="eng", build_plan="Race the models on 20 prompts.", build_minutes=60,
                      hook_options=[f"Hook {n}"], hook_formula_ids=[2], visual="chart", needs=["Groq key"],
                      models_to_run=GOOD_MODELS if ok else ["groq:made-up-model-9"])


@pytest.fixture
def fake_brain(monkeypatch):
    calls = {"one": []}

    def install(ideas_ok, regen_ok=True, recommended=1):
        monkeypatch.setattr(pipeline.brain, "find_topics", lambda s: brain.TopicScan(topics=TOPICS))

        def make_ideas(chosen, history, voice, hooks, model_keys, **kw):
            assert all(k in [m.key for m in CAT.models] for k in model_keys)
            return brain.IdeaBatch(ideas=[make_idea(n, ok) for n, ok in enumerate(ideas_ok, 1)],
                                   recommended_reason="because", recommended=recommended)

        def make_one_idea(t, n, history, voice, hooks, model_keys, problems, **kw):
            calls["one"].append((n, problems))
            return make_idea(n, regen_ok, title=f"Retry {n}")
        monkeypatch.setattr(pipeline.brain, "make_ideas", make_ideas)
        monkeypatch.setattr(pipeline.brain, "make_one_idea", make_one_idea)
        return calls
    return install


def run():
    return pipeline.generate_batch(SIGNALS, [], "", CAT)


def test_all_pass(fake_brain):
    b = run() if fake_brain([True, True, True]) else None
    assert len(b["ideas"]) == 3 and not b["short"]
    assert b["stats"] == {"pitched": 3, "failed_first": 0, "regenerated": 0, "dropped": 0, "unverified": 0}


def test_failed_idea_regenerated_once(fake_brain):
    calls = fake_brain([True, False, True])
    b = run()
    assert len(b["ideas"]) == 3
    assert [c[0] for c in calls["one"]] == [2]
    assert "not in a model catalog" in calls["one"][0][1][0]
    assert b["ideas"][1]["title"] == "Retry 2"


def test_short_batch_when_one_passes(fake_brain):
    fake_brain([False, True, False], regen_ok=False)
    b = run()
    assert len(b["ideas"]) == 1 and b["short"]
    assert b["stats"]["dropped"] == 2
    text = pipeline.render_ideas(7, b)
    assert "SHORT BATCH" in text and "Reply with a number (1-1)" in text and "★ MY PICK" in text


def test_nothing_passes_no_crash(fake_brain):
    fake_brain([False, False, False], regen_ok=False)
    b = run()
    assert b["ideas"] == [] and b["stats"]["dropped"] == 3


def test_topic_without_named_source_is_skipped(fake_brain):
    fake_brain([True, True, True])
    b = run()
    assert "Phone story" not in [i["topic"] for i in b["ideas"]]


def test_renumbering_and_pick_fallback(fake_brain):
    # Recommended topic 2 is dropped -> pick falls back to #1 (hottest remaining).
    fake_brain([True, False, True], regen_ok=False, recommended=2)
    b = run()
    assert [i["topic_number"] for i in b["ideas"]] == [1, 3]
    assert b["recommended"] == 1
    text = pipeline.render_ideas(1, b)
    assert "Why I pick #1:" in text and "2. Idea 3   [NEWS]" in text


def test_pick_maps_to_shown_position(fake_brain):
    fake_brain([False, True, True], regen_ok=False, recommended=3)
    b = run()
    assert b["recommended"] == 2   # topic 3 is shown as idea 2


def test_regression_fields_after_gate(fake_brain):
    fake_brain([True, True, True])
    b = run()
    for i in b["ideas"]:
        for field in ("topic", "heat", "heat_where", "source_urls", "topic_number", "check_before_posting"):
            assert field in i
    gemini = next(i for i in b["ideas"] if i["topic"] == "Gemini 4 Argon launch")
    assert gemini["source_urls"] == ["https://n/0", "https://n/1"]
    # Contract D11: each idea stays attached to the topic it was written for. The fake writer
    # titles idea n "Idea n", so title and topic_number must agree (whatever the ranking order).
    for i in b["ideas"]:
        assert i["title"] == f"Idea {i['topic_number']}"
