# TODOS

## Research

### Run the research sources in parallel

**What:** Run the YouTube, GitHub, Hacker News, LinkedIn/X and docs lookups at the same time (thread pool, same per-source timeout) instead of one after another.

**Why:** When sites are slow, an options request can block the app for about 5 minutes; in parallel the worst case is about 1 minute.

**Context:** From the 2026-10-23 eng review (decision D28). Today `research()` loops `for key, job in jobs.items():` with one retry per source and 25 s HTTP timeouts. Do this after the D22 merge moves research into `sources.py` with renamed functions. Keep the "a failed source returns nothing" behavior and its tests.

**Effort:** S
**Priority:** P3
**Depends on:** D22 merge (research.py into sources.py)

## Learning

### Learn from your own posts

**What:** Once you have posted for a while, save the text of each post you publish together with its results (impressions, reactions, inbound), and use your own best posts as the style examples instead of other people's.

**Why:** Your own results are the only data that says what works for your profile and audience. `python -m postpilot report` already ranks formats and topics by inbound; it does not yet keep the post text.

**Pros:** better examples that sound like you; no scraping needed once there are enough posts; ends the dependence on Apify.

**Cons:** needs a few weeks of real posts before it means anything; `posted` must learn to store the final text (today it stores the link only).

**Context:** From the 2026-11 discussion about style examples. `examples.py` reads any `examples.md`, so your own best posts can go straight into that file. Start in `__main__.py` (`posted`) and `db.py` (outcomes) to keep the text.

**Effort:** M
**Priority:** P2
**Depends on:** at least 10 posts published and their numbers recorded

### Build ideas for your own posts (from the 2026-11 example review)

Six one-hour builds, each free to run and each giving real numbers to post. Do them in this order; the first is the most natural because it measures PostPilot itself.

1. **Small-or-frontier map of PostPilot.** For each pipeline step, latency and quality with a small local model against a free large model.
2. **Run-it-10-times eval.** One agent task on two free models in promptfoo; a single run's pass rate next to the spread over 10 runs.
3. **Retrieval regression check.** 20 golden questions, recall@5 before and after a chunking change, with a test that fails on a drop.
4. **Stale-approval guard.** Re-check a request's version between approval and running it; show a stale approval makes no changes.
5. **Revocation test.** Revoke a user in SQLite, then check whether the agent's own service identity can still read their rows.
6. **Tool-call firewall in about 100 lines.** Role checks plus an approval step, with a table of blocked calls.

**Why:** these topics kept coming back in the posts that teach something (agent permissions, evals run more than once, small models next to large ones). **Effort:** S each. **Priority:** P2.

## Release

### Prove CI, Docker and the first release

**What:** Push a branch and read the GitHub Actions run (lint, types, tests on Linux and Windows), build the Dockerfile and run `python -m postpilot status` inside it, then tag v0.1.0 with the changelog.

**Why:** A public repo whose CI badge, Docker command and release have never been run looks worse to recruiters and strangers than any file layout does.

**Pros:** Turns the biggest public risk into a green check; finds a broken workflow or image before a stranger does.

**Cons:** About 1 hour of your time; needs Docker installed on the laptop; the push and the tag are yours to do.

**Context:** From the 2026-11-12 eng review (decision D12, finding S4). `.github/workflows/ci.yml` has never run remotely and `Dockerfile` has never been built. Start with a push of a branch, then `docker build .` and `docker run --rm <image> python -m postpilot status`, then `git tag v0.1.0`.

**Effort:** S
**Priority:** P2
**Depends on:** Your push; Docker installed

## Completed
