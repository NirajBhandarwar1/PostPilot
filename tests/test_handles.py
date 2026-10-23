"""Idea picking v2: hands-on handles (model / repo / product), paid + new models, cost, plain email."""

import json

from postpilot import factgate as fg, pipeline

NOW = 1_800_000_000
OR_MODELS = json.dumps({"data": [
    {"id": "deepseek/deepseek-v4.1-flash", "created": NOW - 5 * 86400,
     "pricing": {"prompt": "0.0000003", "completion": "0.0000012"},
     "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]}},
    {"id": "anthropic/claude-sonnet-5.5", "created": NOW - 200 * 86400,
     "pricing": {"prompt": "0.000002", "completion": "0.00001"}, "architecture": {}},
    {"id": "old/boring-model", "created": NOW - 400 * 86400,
     "pricing": {"prompt": "0.000001", "completion": "0.000001"}, "architecture": {}},
    {"id": "typesafe/jev-router", "created": NOW - 3 * 86400,
     "pricing": {"prompt": "-1", "completion": "-1"}, "architecture": {}},
    {"id": "nvidia/nemotron-3-ultra:free", "created": NOW - 400 * 86400, "architecture": {}},
]})


def fetch(url, headers=None):
    return OR_MODELS.encode()


def test_openrouter_keeps_new_and_frontier_paid_models_with_prices():
    got = {m.id: m for m in fg.fetch_openrouter(fetch, include_paid=True, now=NOW)}
    assert set(got) == {"deepseek/deepseek-v4.1-flash", "anthropic/claude-sonnet-5.5",
                        "nvidia/nemotron-3-ultra:free"}     # old paid + negative-price router dropped
    ds = got["deepseek/deepseek-v4.1-flash"]
    assert ds.paid and ds.new and ds.vision and round(ds.price_in, 2) == 0.3
    assert not got["anthropic/claude-sonnet-5.5"].new
    assert not got["nvidia/nemotron-3-ultra:free"].paid


def test_no_credit_means_free_only():
    got = fg.fetch_openrouter(fetch, include_paid=False, now=NOW)
    assert [m.id for m in got] == ["nvidia/nemotron-3-ultra:free"]


def test_cost_estimate():
    cat = fg.Catalog([fg.Model("openai", "gpt-6-sol", paid=True, price_in=2.0, price_out=10.0),
                      fg.Model("groq", "openai/gpt-oss-120b")])
    # 20 prompts x (600 in x $2 + 600 out x $10) / 1M = $0.144; free model adds nothing
    assert round(fg.estimate_cost(["openai:gpt-6-sol", "groq:openai/gpt-oss-120b"], cat), 3) == 0.144
    assert fg.estimate_cost(["groq:openai/gpt-oss-120b"], cat) == 0
    cat.models.append(fg.Model("openai", "gpt-6-astra", paid=True))
    assert fg.estimate_cost(["openai:gpt-6-astra"], cat) is None


def test_github_handle_checked_against_github():
    real = {"full_name": "morluto/rea", "stars": 229000, "language": "Rust", "description": "Reverse engineer"}
    t = {"name": "REA goes viral", "handle_type": "github_repo", "handle": "https://github.com/morluto/rea"}
    pipeline._check_handle(t, lambda r: real if r == "morluto/rea" else None)
    assert t["handle"] == "morluto/rea" and "229,000 stars" in t["handle_facts"]
    fake = {"name": "Made up", "handle_type": "github_repo", "handle": "nobody/nothing"}
    pipeline._check_handle(fake, lambda r: None)
    assert fake["handle_type"] == "news" and fake["handle"] == ""


def test_viral_repo_heat_can_compete():
    sig = [{"kind": "GitHub trending", "rank": 0, "title": "morluto/rea", "detail": "", "engagement": 229268}]
    heat, _ = pipeline._heat({"signal_ids": [0]}, sig)
    assert heat == 3 + 15


def test_plain_words_email():
    idea = {"title": "DeepSeek V4.1 Flash vs GPT-6 Sol on 20 bug fixes", "short_title": "DeepSeek Flash vs GPT-6 on real bugs",
            "topic": "DeepSeek V4.1 Flash", "handle_type": "model", "handle": "deepseek v4.1 flash",
            "heat_where": "News x8 | Hacker News: 959 points, 300 comments", "difficulty": "easy",
            "the_news": "DeepSeek shipped a cheap fast model.", "what_you_do": "Run 20 bug fixes on 4 models.",
            "post_shows": "A scoreboard.", "why_hired": "Tests models on real work.", "build_minutes": 60,
            "why_hot": "x", "build_plan": "x", "visual": "x", "models_to_run": ["openai:gpt-6-sol"],
            "est_cost": 0.144, "hook_options": ["Hook"], "role_angle": "speed", "needs": ["OpenRouter credit"],
            "source_urls": ["https://news.google.com/rss/articles/abc", "https://huggingface.co/deepseek"]}
    text = pipeline.render_ideas(9, {"ideas": [idea], "recommended": 1, "recommended_reason": "hot"})
    for part in ("1. DeepSeek Flash vs GPT-6 on real bugs   [NEW MODEL]   ★ MY PICK",
                 "The story: DeepSeek shipped", "Why it's hot: 8 news articles · Hacker News: 959 points",
                 "What you'd do: Run 20 bug fixes", "What the post shows: A scoreboard.",
                 "Why it gets you hired: Tests models", "Effort: easy · 60 min · about $0.14",
                 "You need: OpenRouter credit", "Source: https://huggingface.co/deepseek", "Why I pick #1: hot"):
        assert part in text, part
    assert "news.google.com" not in text                 # unreadable redirect links hidden when others exist


def test_setup_counts_are_not_claims_but_results_are():
    e = ["deepinfer", "deepseek"]
    assert fg.claim_sentences(["DeepInfer vs a DeepSeek API on 10 support requests: faster, or not?"], e) == []
    assert fg.claim_sentences(["DeepInfer is 3x faster than DeepSeek API"], e)
    assert fg.claim_sentences(["DeepInfer scored 18 out of 20"], e)


def test_repo_handle_links_its_github_signal():
    sig = [{"url": "https://github.com/morluto/rea", "title": "morluto/rea: Reverse engineer anything"}]
    assert fg.filter_links({"entities": ["rea"], "handle": "morluto/rea", "signal_ids": [0]}, sig) == [0]


def test_face_off_setup_hooks_pass():
    e = ["muse spark open", "meta"]
    for hook in ("Muse Spark Open vs 3 rivals: which one can fix 20 bugs that already have failing tests?",
                 "Muse Spark Open vs four rivals on 20 code repairs."):
        assert fg.claim_sentences([hook], e) == [], hook
    assert fg.claim_sentences(["Muse Spark Open beat 3 rivals by 40 points."], e)
    assert fg.claim_sentences(["Muse Spark Open won 17 of 20 repairs."], e)
