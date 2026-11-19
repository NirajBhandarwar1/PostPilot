"""Fixes from the code review: the file format, encodings, and a draft that copies an example."""

from postpilot import brain, examples, pipeline, sources
from tests.test_apify_linkedin import on  # noqa: F401
from tests.test_examples import BODY, file, long_item  # noqa: F401  (file: a fixture that points at a temp examples.md)


def test_a_line_of_three_dashes_inside_a_post_cannot_split_it():
    row = sources._linkedin_row(long_item(1, content="Part one.\n---\nPart two. " + BODY))
    back = examples.parse(examples.render([row]))
    assert len(back) == 1 and "- - -" in back[0]["text"] and "Part two." in back[0]["text"]


def test_the_authors_promo_p_s_is_not_kept_as_style():
    row = sources._linkedin_row(long_item(1, content=BODY + "\n\n--\nP.S. We're building the best tutor. Try it."))
    text = examples.render([row])
    assert "P.S." not in text and BODY in text
    assert examples.parse("---\nh\nA post.\n--\nP.S. buy my thing\n---\n") == [{"header": "h", "text": "A post."}]


def test_a_file_in_another_encoding_means_no_examples_not_a_crash(file):
    file.write_bytes("---\nh\nit’s a post\n---\n".encode("cp1252"))        # a curly apostrophe is byte 0x92 here
    assert examples.load() == [] and examples.for_prompt({"title": "x"}) == "" and examples.state().startswith("none")


def test_a_byte_order_mark_does_not_drop_the_first_post(file):
    file.write_bytes(b"\xef\xbb\xbf---\nh\nfirst post cut 40%\n---\n")
    assert [p["text"] for p in examples.load()] == ["first post cut 40%"]


def test_a_copied_phrase_is_found_whatever_the_case_or_punctuation():
    posts = [{"header": "h", "text": "The agent that wrote your spec is the worst reviewer of it. Always."}]
    assert examples.shared_run("Honestly: the AGENT that wrote your spec, is the worst reviewer of it!", posts) \
        == "the agent that wrote your spec is the worst reviewer of it"      # the whole run they share
    assert examples.shared_run("The agent that wrote your spec is fine", posts) == ""        # only 6 words in a row
    assert examples.shared_run("", posts) == "" and examples.shared_run("anything at all here", []) == ""


def _file_with_one_example(file):
    file.write_text(examples.HEADER + "---\nh0\nthe agent that wrote your spec is the worst reviewer of it, 40% of the time\n---\n",
                    encoding="utf-8")


def test_a_draft_that_copies_an_example_is_rewritten_and_the_copy_never_ships(monkeypatch, file):
    _file_with_one_example(file)
    drafts = ["Here is the truth: the agent that wrote your spec is the worst reviewer of it, I measured {{result}}.",
              "I ran the same spec past a fresh agent with no context and it found {{n}} gaps in {{minutes}} minutes."]
    seen = []

    def build_post(idea, v, h, feedback="", plan="", facts="", previous="", **kw):
        seen.append({"feedback": feedback, "previous": previous})
        return brain.PostKit(summary="s", steps=[], post_draft=drafts[min(len(seen), 2) - 1], placeholders=[],
                             alt_hooks=[], visual_spec="v", fact_checks=[])
    monkeypatch.setattr(pipeline.brain, "build_post", build_post)
    monkeypatch.setattr(pipeline, "_score", lambda t: ({}, 90.0, "PASS"))           # a clean draft ends the loop
    out = pipeline._write_post({"title": "spec reviewer agent"}, plan="p")
    assert len(seen) == 2 and "repeats this phrase" in seen[1]["feedback"] and "worst reviewer" in seen[1]["feedback"]
    assert "fresh agent" in out[1] and "worst reviewer" not in out[1]


def test_out_of_tries_a_copied_draft_is_dropped_for_an_earlier_clean_one(monkeypatch, file):
    _file_with_one_example(file)
    clean = "I ran 20 real inputs through it and it failed on {{n}} of them, so I fixed the cheapest three first."
    copied = "Truth: the agent that wrote your spec is the worst reviewer of it, I measured {{result}} today."
    order = iter([clean, copied, copied])
    monkeypatch.setattr(pipeline, "_score", lambda t: ({}, 60.0, "FLAGGED"))          # never reaches the target
    monkeypatch.setattr(pipeline.brain, "build_post", lambda *a, **k: brain.PostKit(
        summary="s", steps=[], post_draft=next(order), placeholders=[], alt_hooks=[], visual_spec="v", fact_checks=[]))
    out = pipeline._write_post({"title": "spec reviewer agent"}, plan="p")
    assert "worst reviewer" not in out[1]


def test_an_author_run_keeps_only_the_authors_own_posts_but_searches_are_untouched(on):
    def post(url, payload, headers):
        other = long_item(2)
        other["author"]["name"] = "Someone Reposted"
        return [long_item(1), other, long_item(3)]
    got = sources.linkedin_examples([], authors=["https://www.linkedin.com/in/owner"], post=post)
    assert {r["author"] for r in got} == {"Owner"} and len(got) == 2
    assert {r["author"] for r in sources.linkedin_examples(["a search"], post=post)} == {"Owner", "Someone Reposted"}
