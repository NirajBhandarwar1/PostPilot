"""Shared test setup: every test gets its own empty SQLite store and no network."""

import types

import pytest

from postpilot import db


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "settings", types.SimpleNamespace(data_dir=tmp_path / "data"))
    return tmp_path


@pytest.fixture(autouse=True)
def models_answer(monkeypatch):
    """Options only keep models that answered a live call; tests never make one, so assume they all do.
    tests/test_verified_models.py restores the real probe_model to test it."""
    from postpilot import llm
    monkeypatch.setattr(llm, "probe_model", lambda provider, model, now=None: (True, "ok"))


@pytest.fixture(autouse=True)
def no_apify(monkeypatch):
    """A developer's own APIFY_TOKEN must never leak into a test: LinkedIn search stays off unless a test turns it on."""
    for name in ("APIFY_TOKEN", "APIFY_MAX_POSTS_PER_MONTH", "APIFY_POSTS_PER_QUERY", "APIFY_LINKEDIN_ACTOR"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def no_examples_file(tmp_path, monkeypatch):
    """A developer's own examples.md must never reach a test prompt; tests that want one write their own."""
    from postpilot import examples
    monkeypatch.setattr(examples, "settings", types.SimpleNamespace(examples_file=tmp_path / "no-examples.md"))


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any real HTTP call in a test is a bug: fail loudly."""
    import urllib.request

    def boom(*a, **k):
        raise AssertionError("test tried to use the network")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    import socket

    def no_dns(*a, **k):                  # no real DNS lookups either (offline, fast)
        raise OSError("test tried to resolve a host name")
    monkeypatch.setattr(socket, "getaddrinfo", no_dns)


@pytest.fixture(autouse=True)
def eval_dirs_in_tmp(tmp_path, monkeypatch):
    """The eval runner must never write the real evals/golden (frozen examples copy) or evals/results.
    Returns the real FROZEN_EXAMPLES function for the one test that checks it."""
    from postpilot.evals import runner
    real = runner.FROZEN_EXAMPLES
    monkeypatch.setattr(runner, "FROZEN_EXAMPLES", lambda: tmp_path / "examples.frozen.md")
    monkeypatch.setattr(runner, "RESULTS_DIR", tmp_path / "eval-results")
    return real
