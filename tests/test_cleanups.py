"""Email cleanups: model mentions by id, rounded numbers, no hyphen breaks."""

from postpilot import factgate as fg, pipeline, render


def test_model_mention_covered_by_provider_id():
    assert fg.covered_by("gpt-6", ["openai:gpt-6-luna"])
    assert fg.covered_by("north mini code", ["openrouter:cohere/north-mini-code:free"])
    assert not fg.covered_by("claude", ["openai:gpt-6-luna"])


def test_rounded_numbers_with_hedge_are_supported():
    titles = ["[Hacker News] DeepInfer: high-performance inference (2335 points, 890 comments)"]
    ents = ["deepinfer"]
    assert fg.supported("DeepInfer drew more than 2,300 Hacker News points.", titles, ents)
    assert not fg.supported("DeepInfer drew 2,300 Hacker News points.", titles, ents)       # no hedge: exact only
    assert not fg.supported("DeepInfer drew more than 3,000 Hacker News points.", titles, ents)


def test_wrap_keeps_ids_whole():
    lines = render._wrap("You need", "A documented model connection for openrouter:cohere/north-mini-code:free "
                                       "and an OpenRouter API key " * 2)
    assert any("north-mini-code:free" in ln for ln in lines)
