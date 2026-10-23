"""Fact gate rules. All offline: catalogs and Hugging Face replies are fixtures."""

import io
import json
import urllib.error

import pytest

from postpilot import factgate as fg


# ---------- fixtures ----------

GROQ = {"data": [{"id": "openai/gpt-oss-120b"}, {"id": "qwen/qwen3.8-27b"}, {"id": "whisper-large-v3"},
                 {"id": "canopylabs/orpheus-v1-english"}, {"id": "meta-llama/llama-prompt-guard-2-86m"},
                 {"id": "openai/gpt-oss-safeguard-20b"}]}
CF_PAGE_1 = {"result": [{"name": "@cf/moonshotai/kimi-k2.6", "task": {"name": "Text Generation"}},
                        {"name": "@cf/meta/llama-3.3-70b-instruct-fp8-fast", "task": {"name": "Text Generation"}},
                        {"name": "@cf/mistral/mistral-7b-instruct-v0.2-lora", "task": {"name": "Text Generation"}}],
             "result_info": {"page": 1, "per_page": 100, "count": 3, "total_count": 4}}
CF_PAGE_2 = {"result": [{"name": "@cf/zai-org/glm-5.3", "task": {"name": "Text Generation"}},
                        {"name": "@cf/deepseek-ai/deepseek-v4-pro-0813", "task": {"name": "Text Generation"},
                         "properties": [{"property_id": "require_workers_paid", "value": "true"}]}],
             "result_info": {"page": 2, "per_page": 100, "count": 2, "total_count": 5}}
OPENROUTER = {"data": [
    {"id": "nvidia/nemotron-3-super-120b-a12b:free", "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]}},
    {"id": "nvidia/nemotron-3.5-super-vl:free", "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]}},
    {"id": "openai/gpt-6-sol", "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]}},
    {"id": "black-forest-labs/flux-3:free", "architecture": {"input_modalities": ["text"], "output_modalities": ["image"]}},
]}


def fake_fetch(routes):
    """fetch(url, headers) -> bytes; routes maps a URL substring to a dict or an exception."""
    def fetch(url, headers=None):
        for key, val in routes.items():
            if key in url:
                if isinstance(val, Exception):
                    raise val
                return json.dumps(val(url) if callable(val) else val).encode()
        raise AssertionError(f"unexpected URL {url}")
    return fetch


def http_error(code):
    return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(b""))


@pytest.fixture
def env_keys(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "g")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")


def all_routes():
    return {"api.groq.com": GROQ,
            "ai/models/search": lambda url: CF_PAGE_2 if "page=2" in url else CF_PAGE_1,
            "openrouter.ai": OPENROUTER}


@pytest.fixture
def catalog(env_keys):
    return fg.load_catalogs(fetch=fake_fetch(all_routes()), use_cache=False)


# ---------- names ----------

@pytest.mark.parametrize("raw,norm", [
    ("nvidia/nemotron-3-super-120b-a12b:free", "nemotron-3-super-120b-a12b"),
    ("@cf/meta/llama-3.3-70b-instruct-fp8-fast", "llama-3-3-70b-instruct-fp8-fast"),
    ("groq:qwen/qwen3.8-27b", "qwen3-8-27b"),
    ("llama-3.3-70b-versatile", "llama-3-3-70b"),
    ("GPT-OSS 120B", "gpt-oss-120b"),
])
def test_normalize(raw, norm):
    assert fg.normalize(raw) == norm


def test_exact_match_never_partial():
    # "llama-3" must not count as a catalog id just because a longer id starts with it.
    cat = fg.Catalog([fg.Model("groq", "llama-3.3-70b-versatile")])
    assert "groq:llama-3" not in cat.by_key()


def test_plain_defeats_fancy_hyphens():
    assert fg.plain("GPT‑4o wins") == "GPT-4o wins"


# ---------- catalogs ----------

def test_catalog_filters_non_chat_outdated_and_paid(catalog):
    keys = {m.key for m in catalog.models}
    assert "groq:openai/gpt-oss-120b" in keys
    assert "groq:qwen/qwen3.8-27b" in keys
    # non-chat Groq models
    assert not any(k.startswith("groq:") and ("whisper" in k or "orpheus" in k or "guard" in k) for k in keys)
    # D7: outdated families hidden
    assert not any("llama-3.3" in k or "mistral-7b" in k for k in keys)
    # Cloudflare second page was read
    assert "cloudflare:@cf/zai-org/glm-5.3" in keys and "cloudflare:@cf/moonshotai/kimi-k2.6" in keys
    # Cloudflare models that need the paid Workers plan are not free (live bug 2026-10-22)
    assert "cloudflare:@cf/deepseek-ai/deepseek-v4-pro-0813" not in keys
    # OpenRouter: only :free text models
    assert "openrouter:openai/gpt-6-sol" not in keys
    assert not any("flux" in k for k in keys)
    vl = catalog.by_key()["openrouter:nvidia/nemotron-3.5-super-vl:free"]
    assert vl.vision is True


def test_catalog_fetch_failure_uses_fresh_cache(env_keys):
    fg.load_catalogs(fetch=fake_fetch(all_routes()))           # fills the cache
    broken = all_routes() | {"api.groq.com": http_error(503)}
    cat = fg.load_catalogs(fetch=fake_fetch(broken))
    assert "groq" in cat.from_cache
    assert "groq:openai/gpt-oss-120b" in cat.by_key()


def test_catalog_failure_without_cache_marks_unavailable(env_keys):
    broken = all_routes() | {"api.groq.com": http_error(503)}
    cat = fg.load_catalogs(fetch=fake_fetch(broken), use_cache=False)
    assert "groq" in cat.unavailable
    assert not any(m.provider == "groq" for m in cat.models)


def test_stale_cache_is_not_used(env_keys):
    fg.load_catalogs(fetch=fake_fetch(all_routes()), now=0)
    broken = all_routes() | {"api.groq.com": http_error(503)}
    cat = fg.load_catalogs(fetch=fake_fetch(broken), now=8 * 86400)
    assert "groq" in cat.unavailable


# ---------- Hugging Face ----------

@pytest.mark.parametrize("code", [401, 404])
def test_hf_missing_repo(code):
    assert fg.hf_model("nope/missing", fetch=fake_fetch({"huggingface.co": http_error(code)}), use_cache=False) is None


def test_hf_existing_repo():
    info = fg.hf_model("deepseek-ai/DeepSeek-V4.1-Flash-1017", use_cache=False,
                       fetch=fake_fetch({"huggingface.co": {"id": "x", "pipeline_tag": "image-text-to-text"}}))
    assert info["pipeline_tag"] == "image-text-to-text"


def test_hf_network_error_is_unknown():
    assert fg.hf_model("a/b1", fetch=fake_fetch({"huggingface.co": OSError("down")}), use_cache=False) == "unknown"


# ---------- scans ----------

def test_mentions_ignore_ordinary_versions(catalog):
    fams = fg.families(catalog)
    assert fg.model_mentions("Python 3.12 on Windows 11, Stage 1, Top 3, Step 2", fams) == []
    found = fg.model_mentions("Nemotron 3 Super 120B vs Qwen3.8-27B and Kimi K2.6 on Python 3.12", fams)
    assert found == ["Nemotron 3 Super 120B", "Qwen3.8-27B", "Kimi K2.6"]


def test_outdated_names_are_found(catalog):
    found = fg.model_mentions("Better than GPT-4o or Llama 3?", fg.families(catalog))
    assert "GPT-4o" in found and "Llama 3" in found


def test_display_name_covered_by_declared_id():
    assert fg.covered_by("Nemotron 3 Super 120B", ["openrouter:nvidia/nemotron-3-super-120b-a12b:free"])
    assert not fg.covered_by("Kimi K2.6", ["groq:openai/gpt-oss-120b"])


def test_claim_sentences_skip_setup_numbers():
    text = ("I tested 3 models in 1 hour. Anthropic raised $5B at a $300B valuation. "
            "Gemini 4 Argon scored 92% on a benchmark. Each run takes 20 questions.")
    got = fg.claim_sentences([text], ["anthropic", "gemini 4 argon"])
    assert got == ["Anthropic raised $5B at a $300B valuation.", "Gemini 4 Argon scored 92% on a benchmark."]


def test_digits_in_topic_name_are_not_claims():
    # Live false alarm 2026-10-20: the "4" in "Gemini 4 Argon" counted as a number.
    assert fg.claim_sentences(["Gemini 4 Argon sounds pricey - can a free model do the same for less?"],
                              ["gemini 4 argon", "google"]) == []
    # A real number next to the entity still counts.
    assert fg.claim_sentences(["Gemini 4 Argon is 3x cheaper than last year."], ["gemini 4 argon"])


def test_provider_ids_are_not_checked_as_hf_repos(catalog):
    # Live false alarm 2026-10-20: an OpenRouter id was looked up on Hugging Face.
    seen = []
    v = fg.check_idea(idea(build_plan="Call openrouter:nvidia/nemotron-3-super-120b-a12b:free via API.",
                           models_to_run=["openrouter:nvidia/nemotron-3-super-120b-a12b:free"],
                           hook_options=["Nemotron 3 Super 120B on a budget"]),
                      TOPIC, SIGNALS, catalog, hf_lookup=lambda r: seen.append(r))
    assert seen == [] and v.ok


def test_claim_supported_by_title():
    titles = ["Anthropic raises $5B at $300B valuation - Reuters"]
    assert fg.supported("Anthropic raised $5B at a $300B valuation.", titles, ["anthropic"])
    assert not fg.supported("Anthropic filed an S-1 for an IPO.", titles, ["anthropic"])


def test_filter_links_keeps_only_named_sources():
    signals = [{"title": "Google launches Gemini 4 Argon", "url": "https://news/x"},
               {"title": "Flux 3 image model trends", "url": "https://huggingface.co/spaces/flux"},
               {"title": "Hands-on with Argon", "url": "https://blog/gemini-4-argon"}]
    topic = {"name": "Gemini 4 Argon launch", "entities": ["gemini 4 argon", "google"], "signal_ids": [0, 1, 2, 9]}
    assert fg.filter_links(topic, signals) == [0, 2]


# ---------- check_idea ----------

SIGNALS = [{"title": "Google launches Gemini 4 Argon", "url": "u0"},
           {"title": "Kimi K2.6 tops the leaderboard", "url": "u1"}]
TOPIC = {"name": "Gemini 4 Argon", "entities": ["gemini 4 argon", "google"], "signal_ids": [0, 1]}


def idea(**kw):
    base = {"title": "Kimi K2.6 vs Qwen3.8 on real tasks", "format": "face-off",
            "build_plan": "Send the same 20 prompts to both models and time them.",
            "why_hot": "Gemini 4 Argon just launched.", "hook_options": ["Kimi K2.6 vs Qwen3.8: who is faster?"],
            "needs": ["Groq key"], "models_to_run": ["cloudflare:@cf/moonshotai/kimi-k2.6", "groq:qwen/qwen3.8-27b"]}
    base.update(kw)
    return base


def test_good_idea_passes(catalog):
    v = fg.check_idea(idea(), TOPIC, SIGNALS, catalog)
    assert v.ok, v.reasons
    assert v.unverified == []


def test_unknown_model_id_fails(catalog):
    v = fg.check_idea(idea(models_to_run=["groq:llama-3.3-70b-versatile"]), TOPIC, SIGNALS, catalog)
    assert not v.ok and "not in a free model catalog" in v.reasons[0]


def test_run_lane_without_models_fails(catalog):
    v = fg.check_idea(idea(models_to_run=[]), TOPIC, SIGNALS, catalog)
    assert not v.ok


def test_news_lane_may_run_nothing(catalog):
    v = fg.check_idea(idea(format="data-take", models_to_run=[], hook_options=["Who is winning the AI race?"],
                           title="Charting Gemini 4 Argon coverage"), TOPIC, SIGNALS, catalog)
    assert v.ok, v.reasons


def test_outdated_name_fails_in_run_lane_but_is_flagged_in_news_lane(catalog):
    run = fg.check_idea(idea(hook_options=["Kimi K2.6 vs Qwen3.8 vs GPT-4o"]), TOPIC, SIGNALS, catalog)
    assert not run.ok and any("GPT-4o" in r for r in run.reasons)
    news = fg.check_idea(idea(format="data-take", models_to_run=[], hook_options=["Gemini 4 Argon vs GPT-4o, a year on"]),
                         TOPIC, SIGNALS, catalog)
    assert news.ok and any("Older model GPT-4o" in u for u in news.unverified)


def test_vision_build_needs_vision_model(catalog):
    # Unknown vision support (Groq lists no modalities) -> allowed but flagged.
    v = fg.check_idea(idea(build_plan="Caption 10 photos with both models.",
                           models_to_run=["groq:qwen/qwen3.8-27b"], hook_options=["Qwen3.8 reads my photos"]),
                      TOPIC, SIGNALS, catalog)
    assert v.ok and any("accepts images" in u for u in v.unverified)
    # Known text-only (OpenRouter says input is text) -> fails.
    v2 = fg.check_idea(idea(build_plan="Caption 10 photos.",
                            models_to_run=["openrouter:nvidia/nemotron-3-super-120b-a12b:free"],
                            hook_options=["Nemotron 3 Super 120B reads my photos"]), TOPIC, SIGNALS, catalog)
    assert not v2.ok and "cannot take image" in v2.reasons[0]
    # Known vision model -> passes cleanly.
    v3 = fg.check_idea(idea(build_plan="Caption 10 photos.",
                            models_to_run=["openrouter:nvidia/nemotron-3.5-super-vl:free"],
                            hook_options=["Nemotron 3.5 Super VL reads my photos"]), TOPIC, SIGNALS, catalog)
    assert v3.ok, v3.reasons


def test_unverified_claim_is_flagged(catalog):
    v = fg.check_idea(idea(why_hot="Google raised $9B for Gemini 4 Argon."), TOPIC, SIGNALS, catalog)
    assert v.ok and any("Unverified claim" in u for u in v.unverified)


def test_missing_hf_repo_is_flagged(catalog):
    v = fg.check_idea(idea(build_plan="Download fake-org/ghost-model-7 and compare."), TOPIC, SIGNALS, catalog,
                      hf_lookup=lambda repo: None)
    assert any("does not exist" in u for u in v.unverified)


def test_scan_text_flags_post_claims(catalog):
    lines = fg.scan_text(["Kimi K2.6 beat Qwen3.8. Google raised $9B for Gemini 4 Argon."], TOPIC, SIGNALS,
                         catalog, ["cloudflare:@cf/moonshotai/kimi-k2.6", "groq:qwen/qwen3.8-27b"])
    assert any("Unverified claim in the post" in l for l in lines)
    assert not any("Kimi" in l for l in lines)


def test_shortlist_keeps_groq_and_caps_others(catalog):
    keys = [m.key for m in fg.shortlist(catalog, ["kimi k2.6"], per_provider=1)]
    assert "groq:openai/gpt-oss-120b" in keys and "groq:qwen/qwen3.8-27b" in keys
    assert keys.count("cloudflare:@cf/moonshotai/kimi-k2.6") == 1
    assert sum(k.startswith("cloudflare:") for k in keys) == 1


@pytest.mark.parametrize("plan,needs_vision", [
    ("Post a screenshot of the route log.", False),
    ("Record a screen recording of the run.", False),
    ("Share a screenshot of results to LinkedIn.", False),
    ("Send 10 photos to the vision model and ask for captions.", True),
    ("Upload the dashboard screenshot to Qwen3.8 and ask what it shows.", True),
    ("Feed each image into gpt-oss and compare.", True),
])
def test_vision_only_when_images_go_to_the_model(plan, needs_vision):
    # Live false alarm 2026-10-22: "screenshot of the route log" asked for an image model.
    assert bool(fg.VISION_NEED.search(plan)) is needs_vision


def test_claim_supported_by_source_detail():
    # Live false alarm 2026-10-22: "#1 on Hugging Face" lives in the signal detail, not the title.
    topic = {"signal_ids": [0], "entities": ["deepseek-v4.1-flash"]}
    signals = [{"title": "deepseek-ai/DeepSeek-V4.1-Flash-1017 (image-text-to-text)", "detail": "HF trending #1, 1626 likes"}]
    assert fg.supported("DeepSeek-V4.1-Flash is Hugging Face #1.", fg._evidence(topic, signals), topic["entities"])
