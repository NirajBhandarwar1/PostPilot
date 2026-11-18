# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Added
- Health line: logged after every scout and shown in `status` (today's free Cloudflare/Groq quota, model pass rate), with a warning at 80%.
- Per-task model lists (`SCOUT_`, `IDEAS_`, `OPTIONS_`, `BRIEF_`, `POST_PROVIDERS`) and `provider:model` pinning.
- Persona settings (`PERSONA_ROLE`, `PERSONA_WHO`, `PERSONA_GOAL`) and `voice.example.md`, so anyone can use it.
- CI (lint, types, tests on Linux and Windows), pyproject, LICENSE, SECURITY, CONTRIBUTING.

- Evidence pack: one short fact list per idea replaces the raw research text in the options, BUILD.md
  and post prompts (options prompt 32% smaller, BUILD.md prompt 39% smaller).
- Calls are fitted to a provider's per-minute limit (`GROQ_TPM`): the reserved output shrinks to fit, and a
  provider with no room is skipped without a network call.
- Every model call is recorded per provider and model. The configured order is kept, but a model that keeps
  failing (5+ calls in 7 days, under 50% usable) is moved to the end; nothing is ever promoted
  (`PROVIDER_ORDER=config` turns that off). `status` shows the table.
- Plan checks: warnings for the removed OpenAI v0 call, provider-prefixed model ids, JavaScript packages in
  `pip install`, and invented personal claims or timings in the post.
- Plan prompts: OpenAI SDK v1 usage, one package manager per language, image input must reach the model,
  one visual spec; the model's own record/publish steps are dropped so they are not repeated.
- Verified models: a plan only recommends a model whose provider key is set; an option whose model cannot be
  confirmed with a live test call is kept, with a "Need to check" warning.
- The plan email is a short summary (about 1,000 characters); the full plan is the BUILD.md attachment.
- Post rewrites now edit the best draft so far and stop after one that does not improve it.

- Evaluation layer ([ADR-006](docs/adr/006-evaluation-layer.md)): `eval run` scores the options, BUILD.md and
  post on golden stories with deterministic rules and compares with the last run; `eval capture` adds a story;
  `eval replay` re-scores a saved run offline; `eval live` shows pick rate, your ratings, model health and
  inbound per post. No model judge. A run cut short by a provider's quota is reported, not saved.

- Loop health: the loop writes a heartbeat after each stage, `status` shows `Loop: alive / STOPPED?`, and a start
  after more than an hour of silence emails you once ("was down for X h"). The first start sends nothing.
  README has a "Keep it running" section with a Windows Task Scheduler snippet.
- A scout that finds no ideas (and no error) is retried 3 hours later, at most twice per UTC day.
- `eval run` prints today's free Groq and Cloudflare quota before it starts.
- Command-line tests (every subcommand reaches its function; `precommit` exits 0 or 1 as a real process),
  and import-rule tests that keep the modules in layers.

- Optional LinkedIn research through Apify's free plan (off without `APIFY_TOKEN`): each story gets a search of public
  posts, and `examples fetch` pulls posts from authors or searches you choose into a private, gitignored `examples.md`
  (best 30, 600 to 1,800 characters, no job ads or pitches). The post writer sees 3 examples per post as style only.
  Cost guards: a monthly post cap (default 500), a 5-cent charge limit per run, one author per run, off for the month
  when Apify says the credit is used up. `status` shows the usage and the example count.

### Changed
- The DuckDuckGo LinkedIn and X search is removed: DuckDuckGo now answers with a bot check, so it found nothing and
  cost a few calls on every scout.
- A reply claim now lasts 60 minutes instead of 20, so a slow build is not started twice.
- `precommit` and the quota/health code moved into `__main__.py` and `llm.py` (15 files), and `pipeline.py`
  gave its ranking rules to `ranking.py` and its replay and report code to `reports.py`. `make_options`,
  `make_plan`, `idea_from_option` and `voice` are public now. Commands and behavior are the same.
- The "a backup model wrote this" note is printed from facts saved with the batch or plan (`writers`,
  `expected`), not from live model state. Items saved before this change show no note.
- The README, ADR-005 and the design doc now describe the free-models-only setup and the daily cycle.
- The eval no longer uses a paid model or a token budget; the old `eval --builds` is replaced by `eval run`,
  and the fact-check replay is now `eval history`.
- Scouting runs daily instead of every three days.
- Drafts and the voice profile now default to `./output` and `./voice.md` inside the project.
- A provider with another one waiting behind it is retried once and briefly, so a busy provider no longer
  costs minutes before the fallback runs.
- BUILD.md: valid PowerShell for requirements.txt, ASCII hyphens in commands, real code fences, exact model ids,
  de-duplicated warnings, and no pip warnings for ordinary words.
- A provider that reports no credit or an exhausted daily quota is skipped at once for 30 minutes.

### Fixed
- Replies sent from the owner's own Gmail account are now recognised.
- A failed email send retries only the email, not the whole scout.
