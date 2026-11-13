"""Checks for mistakes seen in a real generated plan: removed OpenAI SDK calls, provider-prefixed model ids,
npm packages installed with pip, and invented personal claims in the post."""

from postpilot import factgate as fg

TOPIC = {"signal_ids": [0], "entities": ["Acme"], "name": "Acme"}
SIGNALS = [{"title": "Acme tool runs in 30 min on a laptop"}]


def code(text):
    return fg.scan_code([("step", text)])


def post(*texts):
    return fg.scan_text(list(texts), TOPIC, SIGNALS, fg.Catalog(), [])


def test_old_openai_call_and_prefixed_model_flagged():
    out = code('openai.ChatCompletion.create(model="openai:gpt-6.1-sol", messages=[])')
    assert any("ChatCompletion was removed" in w for w in out)
    assert any("'openai:gpt-6.1-sol' has a provider prefix" in w and "'gpt-6.1-sol'" in w for w in out)


def test_groq_prefix_flagged_in_plan_text():
    out = post('client.chat.completions.create(model="groq:llama-4-scout", messages=m)')
    assert any("provider prefix" in w for w in out)


def test_correct_v1_usage_is_clean():
    out = code('client = OpenAI(base_url="https://api.groq.com/openai/v1")\n'
               'client.chat.completions.create(model="llama-4-scout", messages=m)')
    assert not any("prefix" in w or "ChatCompletion" in w for w in out)


def test_models_to_run_style_ids_outside_code_are_not_flagged():
    assert not any("prefix" in w for w in post("Models to run: groq:llama-4-scout and openai:gpt-6-luna"))


def test_npm_packages_in_pip_flagged_only_for_npm_names():
    cmd = ".\\.venv\\Scripts\\python.exe -m pip install 'fastapi' 'next' 'openai>=1.0.0' 'react' 'react-dom' 'uvicorn[standard]'"
    out = code(cmd)
    npm = [w for w in out if "install it with npm" in w]
    assert len(npm) == 3
    assert all(f"'{n}'" in " ".join(npm) for n in ("next", "react", "react-dom"))
    assert not any("'openai'" in w or "'uvicorn'" in w for w in npm)


def test_pip_install_of_python_packages_has_no_npm_warning():
    assert not any("npm" in w for w in code("pip install requests pytest"))
    assert code("python -m pip install requests pytest") == []


def test_npm_install_is_fine():
    assert code("npm install next react react-dom") == []


def test_invented_personal_claim_and_cost_flagged():
    out = post("I'm already testing the same flow on our product's admin panel. The whole thing cost under $3.")
    assert len(out) == 2
    assert all(w.startswith("Invented claim in the post:") and "{{placeholder}}" in w for w in out)


def test_other_invented_claims_flagged():
    for s in ("At my company we use it daily.", "Our customers loved it.", "It took me 2 hours.",
              "We shipped it last week.", "This saved a day of work."):
        assert any(w.startswith("Invented claim") for w in post(s)), s


def test_placeholders_and_provided_facts_do_not_warn():
    out = post("I ran it on {{n}} real tickets and it took me {{hours}} hours.",
               "The whole thing cost under {{cost}}.",
               "It finished the setup in 30 min on a laptop.")
    assert not any("Invented claim" in w for w in out)


def test_patching_the_openai_library_in_a_test_is_flagged():
    from postpilot import factgate
    bad = [
        '@mock.patch.object(OpenAI, "chat", mock.Mock())',
        "monkeypatch.setattr('openai.OpenAI.chat.completions.create', fake)",
        "with patch('openai.OpenAI') as client:",
    ]
    for line in bad:
        assert any("patches the OpenAI library" in w for w in factgate._wrong_usage(line)), line
    # a fake client passed in, or patching your own module's name, is fine
    for ok in ["result = summarize(text, client=FakeClient())", "monkeypatch.setattr(app, 'call_model', fake)"]:
        assert not factgate._wrong_usage(ok), ok
