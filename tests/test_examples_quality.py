"""The knowledge-not-selling filter, the three-different-authors pick, and the real examples.md."""

import re
from pathlib import Path

import pytest

from postpilot import examples

OK = "I measured the build: it cut latency by 40% on 20 real inputs and I wrote down what failed."


@pytest.mark.parametrize("bad", [
    "Please repost " + OK, "Follow Jane Doe for more. " + OK, "Star the repo. " + OK, "Like and share. " + OK, OK + " ♻️",
    OK + " Watch the full episode", "The article covers it. " + OK, "Join my course. " + OK, "Subscribe. " + OK,
    OK + " Join the webinar", "Our agency ships fast. " + OK, "We help teams. " + OK, "Our clients love it. " + OK,
    "Book a call. " + OK, "DM me. " + OK, "Free audit. " + OK, "We're hiring. " + OK,
    OK + " Read more at https://", OK + " Read more at https:/", OK + " The result:", OK + " and,",
    OK + " a.com/1 https://a.com/1 https://b.com/2 https://c.com/3 https://d.com/4",
])
def test_each_rule_rejects_its_sample(bad):
    assert examples.rejected(bad), bad


@pytest.mark.parametrize("good", [
    OK, OK + " See https://github.com/x/y/issues/1 for the detail.", "A gold mine. " + OK,
    OK + " #a #b #c", "Link in the first comment. " + OK, OK + " #a #b #c #d #e #f",
    "A gold mine 🤯 " + OK, "I ran it and it was fast, really fast, every time.", "It took 3x longer on 12 GB of logs, so I switched.", "Latency fell from 900ms to 120ms."])
def test_a_knowledge_post_passes(good):
    assert examples.rejected(good) == ""


def _post(author, text, likes=0):
    return {"header": f"{author} | {likes} likes, 1 comments | https://x/{author}", "text": text}


def test_load_drops_rejected_posts_but_append_still_sees_them(tmp_path, monkeypatch):
    import types
    f = tmp_path / "e.md"
    monkeypatch.setattr(examples, "settings", types.SimpleNamespace(examples_file=f))
    f.write_text(examples.HEADER + f"---\nA | 1 likes, 1 comments | u1\n{OK}\n---\nB | 1 likes, 1 comments | u2\nDM me. {OK}\n---\n",
                 encoding="utf-8")
    assert [p["header"][0] for p in examples.load()] == ["A"] and len(examples.load(quality=False)) == 2


def test_three_different_authors_with_at_most_one_curator():
    build = "I built a router and measured 30% fewer tokens with agents."
    posts = [_post("A", "Someone shipped a great agents tool, 10x faster.", 900),
             _post("A", build, 100), _post("A", build + " again", 50),
             _post("B", "Another curated agents tool, 5x faster.", 800), _post("B", build, 20),
             _post("C", "Yet another agents tool, 2x cheaper.", 700), _post("D", build + " more", 10)]
    got = examples.pick(posts, {"title": "agents tool"})
    authors = [p["header"].split(" |")[0] for p in got]
    assert len(got) == 3 and len(set(authors)) == 3
    assert [p["kind"] for p in got].count("curation") <= 1 and "own-build" in {p["kind"] for p in got}


def test_fewer_authors_means_fewer_examples():
    assert len(examples.pick([_post("A", OK), _post("A", OK + " more")], {"title": "build"})) == 1


def test_filler_words_do_not_count_as_topic_overlap():
    posts = [_post("A", "which would have been about this thing with those very much " + "pad " * 5),
             _post("B", "kubernetes scaling notes: 3x")]
    assert examples.pick(posts, {"title": "which would have been about this kubernetes"})[0]["header"].startswith("B")


def test_the_ending_is_kept_and_the_header_has_no_likes_or_links():
    post = _post("Ann", "start " + "mid " * 400 + "THE END, 5x.", likes=1234)
    out = examples.for_prompt({"title": "x"}, [post])
    assert out.endswith("THE END, 5x.") and out.startswith("Example 1:\nstart")
    assert "1234" not in out and "http" not in out and "likes" not in out
    assert len(out) < examples.SHOW_CHARS + examples.TAIL_CHARS + 60


def test_the_real_examples_file_loads_and_stays_clean():
    real = Path(__file__).resolve().parents[1] / "examples.md"
    if not real.exists():
        pytest.skip("no private examples.md here")
    posts = examples.parse(real.read_text(encoding="utf-8-sig"))
    assert posts and not re.search(r"Ghumare.*482", " ".join(p["text"][:40] + p["header"] for p in posts))
    kept = [p for p in posts if not examples.rejected(p["text"])]
    assert kept and examples.pick(kept, {"title": "Claude Code skills"})


@pytest.mark.parametrize("two", [
    OK + " #a #b #c #d #e #f Repo in the comments.", "A gold mine 🤯 I ran it and it was fast, really fast, every time.",
    "Link in the first comment. I ran it and it was fast, really fast, every time."])
def test_two_soft_signs_together_reject(two):
    assert examples.rejected(two), two


def test_links_and_handles_never_reach_the_prompt():
    post = _post("Ann", "I measured 3x. " + "word " * 20 + "thanks @bob\nrepo: https://lnkd.in/x")
    out = examples.for_prompt({"title": "x"}, [post])
    assert "lnkd" not in out and "http" not in out and "@bob" not in out and "[link]" in out


def test_real_examples_file_has_no_links_in_prompt():
    if not examples.path().exists():
        import pytest
        pytest.skip("no examples.md")
    assert "lnkd.in" not in examples.for_prompt({"title": "Claude Code skills"})


def test_more_first_hand_verbs_count_as_own_build():
    for t in ("I fine-tuned a small model on 200 rows.", "We connected the queue. We found 3x lag."):
        assert examples.OWN_BUILD.search(t)


def test_slot_three_varies_by_idea_but_is_stable_for_one():
    posts = [_post(a, f"a curated agents tool number {a}, 2x", 100) for a in "ABCDEFGH"]
    posts.insert(0, _post("Z", "I built an agents tool and measured 30%", 50))
    picks = {examples.pick(posts, {"title": t})[2]["header"] for t in ("one", "two", "three", "four", "five", "six")}
    assert len(picks) > 1
    assert examples.pick(posts, {"title": "one"}) == examples.pick(posts, {"title": "one"})


def test_no_topic_overlap_falls_back_to_best_first_hand_build():
    posts = [_post("A", "curated zzz", 900), _post("B", "I built a thing and measured 3x", 10),
             _post("C", "I built another and measured 2x", 50)]
    assert examples.pick(posts, {"title": "qqqq"})[0]["header"].startswith("C")


def test_copied_numbers_flags_only_example_numbers_missing_from_facts():
    picked = [{"text": "It cut cost 40% and ran in 90ms on 3x GPUs."}]
    got = examples.copied_numbers("We saw 40% and 90ms and 12%.", picked, "- latency 90ms")
    assert got == ["40%"]
