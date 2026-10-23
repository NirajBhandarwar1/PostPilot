"""Regression contract D11 (1): today's replies keep working; the rating form is new."""

import pytest

from postpilot.mailer import parse_choice, parse_reply


@pytest.mark.parametrize("body,want", [
    ("2\n\nOn Mon, 19 Oct 2026 PostPilot wrote:\n> 1. foo", "2"),
    ("Let's do #3 please", "3"),
    ("skip this one", "skip"),
    ("hmm\n> 1. quoted only", None),
    ("Build idea 1, looks fun", "1"),
])
def test_existing_replies(body, want):
    assert parse_choice(body) == want


def test_rating_form():
    assert parse_reply("2 rate 4 5 2") == ("2", [4, 5, 2])


@pytest.mark.parametrize("body", ["2 rate 9 9", "2 rate 4 5", "2 rate 0 3 3", "2 rate 1 2 3 4"])
def test_bad_ratings_ignored_pick_kept(body):
    assert parse_reply(body) == ("2", None)


def test_ratings_do_not_become_the_pick():
    # "rate 1 1 1" must not turn a skip or a missing pick into idea 1
    assert parse_reply("skip rate 1 1 1") == ("skip", [1, 1, 1])
    assert parse_reply("rate 1 1 1") == (None, [1, 1, 1])
