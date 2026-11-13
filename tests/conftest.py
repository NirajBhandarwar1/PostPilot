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
