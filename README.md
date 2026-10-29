# PostPilot

[![CI](https://github.com/NirajBhandarwar1/PostPilot/actions/workflows/ci.yml/badge.svg)](https://github.com/NirajBhandarwar1/PostPilot/actions/workflows/ci.yml) ![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue) [![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

An always-on agent that finds what's hot in AI every day, emails you 3 LinkedIn post ideas,
and when you reply with a number, builds that idea: the experiment code, the step-by-step plan,
and a humanized post draft ready to fill in and paste.

The goal is to get noticed by people who hire in AI, by posting experiments rather than news.

## How a cycle works

1. **Scout.** Pulls trending items from AI news feeds (Google News, TechCrunch, The Verge,
   OpenAI, Google AI, Hugging Face, Simon Willison), Hacker News, new GitHub repos and
   Hugging Face papers. All free, no keys.
2. **Pick.** The writer model (GPT-6 first, free models as backup) picks 3 ideas you can build, test or compare in about an hour. Each one
   targets a different role family and skips topics you've already covered.
3. **Email.** You get `[PostPilot #N] 3 AI post ideas`. Reply `1`, `2`, `3` or `skip`.
4. **Options.** Your reply `2` brings `[PostPilot #N.2]`: 3-5 build options for that story,
   based on what is already viral (YouTube, GitHub, Hacker News). Always one "look what I
   built" demo and one public GitHub repo. Reply `3` to pick option 3, or `3 new` for a fresh
   list (or reply `2 go` to the first email to skip the menu). Replies to the build-plan email
   never start anything, so you can write notes there safely.
5. **Build plan.** You get `BUILD.md` (emailed and saved to `../linkedin/builds/`). Part A is
   for Cursor: goal, stack, files, setup, models with exact base URLs and key names, 6-10 steps
   each with a "Done when" check, tests and rules. Anything that downloads, installs or runs
   someone else's code is a 🧑 step or a 🧑⚠ line that you run yourself, never Cursor. Part B is for you: demo shot list, the
   LinkedIn post (humanized, `{{placeholders}}` for real results), first comment, fact checks.
6. **You** open an empty folder in Cursor, add BUILD.md, tell it to build Part A, then record
   the demo, fill in the placeholders and post it yourself.

PostPilot never posts to LinkedIn. Automating a personal profile breaks LinkedIn's terms, and
the drafts never make up results.

## Setup

Needs Python 3.12+, a Gmail account with an [App Password](https://myaccount.google.com/apppasswords),
and at least one free model key (Groq is the quickest: https://console.groq.com/keys).

```bash
git clone https://github.com/NirajBhandarwar1/PostPilot.git && cd PostPilot
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e .
cp .env.example .env               # Windows: copy .env.example .env  -- then fill in .env
cp voice.example.md voice.md       # fill in your profile; without it drafts sound generic
python -m postpilot status         # checks your keys, Gmail and profile
python -m postpilot test-email
python -m postpilot scout          # first batch, right now
python -m postpilot run            # keep running (Windows: start.bat)
```

Keep `run` alive with `tmux`, a systemd user service or launchd on macOS/Linux, or Task Scheduler
(`start.bat`) on Windows. A `Dockerfile` is included for running it as a container
(`docker build -t postpilot . && docker run --env-file .env -v postpilot-data:/app/data -v postpilot-out:/app/output postpilot`);
the image build has not been tested yet.

Posts are written for the person described in `voice.md` and the `PERSONA_*` lines in `.env`.

## Commands

| command | does |
| --- | --- |
| `python -m postpilot run` | keep running: scout every day (`CYCLE_DAYS` in `.env`), check replies every 10 min |
| `python -m postpilot scout [--no-email]` | scout once now |
| `python -m postpilot options 4 2 [--no-email] [--fresh]` | build options for idea 2 of batch #4 (email reply `2`); saved options are resent, `--fresh` (reply `2 new`) makes a new list tagged `#4.2b` |
| `python -m postpilot build 4 2 --option 3` | always writes a fresh BUILD.md for option 3 (an email reply resends an existing plan instead of paying twice) |
| `python -m postpilot eval --builds [N]` | re-run N saved stories through options, BUILD.md and post with GPT-6 (max 60k tokens, keeps 100k of the day's cap for live emails); PASS/FAIL bars vs the last run |
| `python -m postpilot check` | check email replies once |
| `python -m postpilot status` | setup check and recent batches |
| `python -m postpilot precommit` | secret check before you commit (blocks .env, real key values, key shapes) |
| `python -m postpilot eval [N]` | replay the last N saved runs: fact-check failure rate and unverified claims per idea |
| `python -m postpilot doctor` | live check: how many signals each trend source returns right now, and whether each backup writer model answers |
| `python -m postpilot posted 5 <url> [--build N]` | after you publish: record the LinkedIn link for batch #5 (latest build unless `--build`; BUILD.md's "After you post" shows the exact command) |
| `python -m postpilot inbound 5 --recruiters 2 --founders 1` | record who reached out after that post (goes to the build you recorded as posted) |
| `python -m postpilot report` | which formats and topics bring the most recruiters and founders |
| `.venv\Scripts\python -m pytest -q` | run the tests (offline, ~1 second) |

## Models (updated 2026-10-22)

- Topic finding and all writing (ideas, posts, code) use `WRITER_PROVIDERS` in `.env`:
  `openai,groq,cloudflare` = GPT-6 Sol first, then free gpt-oss-120b on Groq (best free idea and
  plan writer, but its free limit is too small for the options request), then gpt-oss-120b on
  Cloudflare (slower, but takes every request size). A live comparison on 2026-10-28 ranked them.
  Chosen by a bake-off on the same viral stories: GPT-6 Sol was the only model with sharp,
  feasible ideas and no invented results. About 8K input + 4K output tokens per batch.
- Remove `openai` from `WRITER_PROVIDERS` to run 100% free again.
- Trend radar ranks by real attention: Hacker News points, Techmeme, Reddit, GitHub and
  Hugging Face trending; news coverage counts but is capped.

## Fact gate

Every idea is checked before you see it (`postpilot/factgate.py`):
- Models to run can only be picked from the live catalogs, with outdated models hidden:
  free ones (Groq, Cloudflare, OpenRouter `:free`), your OpenAI models, and OpenRouter paid
  models (top models + anything launched in the last 60 days) when OpenRouter has credit.
  Set `PAID_EXPERIMENTS=off` in `.env` for free models only. Each idea shows an estimated cost.
  Image/video builds need models that accept images.
- Every story gets a "handle" (new model, viral GitHub repo, product, paper, or news only).
  The idea follows it: model -> face-off vs top models, repo -> build with it (repo checked on
  GitHub), product -> first look, paper -> replicate. Stories with a handle rank higher.
- Model names, money, percentages and company claims in the text are checked against the
  scouted headlines; anything unproven is listed under "Check before you post".
- A failing idea is rewritten once, then dropped. The finished post is checked the same way.
- Build code goes to `experiments/` (the proof your post links to); drafts stay private.

Design and review decisions: `docs/designs/postpilot-content-engine.md`.

## Development

```
pip install -e ".[dev]"
ruff check . && mypy postpilot && pytest --cov
```

CI runs the same checks on Linux and Windows, Python 3.12 and 3.13. See [CONTRIBUTING.md](CONTRIBUTING.md)
and [SECURITY.md](SECURITY.md); design decisions are in [docs/adr/](docs/adr/).

## Files

```
postpilot/sources.py    trend radar + per-idea research (YouTube, GitHub, HN, social, official docs)
postpilot/brain.py      prompts and output schemas: topics, ideas, options, BUILD.md, post
postpilot/llm.py        model client: GPT-6 writer, daily cap, ordered free backups, retries
postpilot/factgate.py   fact check: live model catalogs, claims vs sources, code/command scans, cost
postpilot/pipeline.py   the flow: scout, options, build, reply handling, the always-on loop, eval
postpilot/render.py     what you see: the 3 emails (text + HTML) and the BUILD.md layout
postpilot/mailer.py     Gmail send + read replies (App Password, DKIM-verified)
postpilot/db.py         SQLite: batches, builds, per-build outcomes, merge-on-save
postpilot/vendor/       humanizer + 21 hook formulas, from
                        github.com/Jakeschincariol/linkedin-agent-skill (MIT)
data/                   SQLite history and log (not committed)
```

## Providers

Any OpenAI-compatible endpoint works (`postpilot/llm.py`). Two lists in `.env` decide who answers,
and the first provider that works wins; if it fails, the next one takes over:

- `WRITER_PROVIDERS` for ideas, build plans and posts. Recommended free setup: `groq,cloudflare`;
  with OpenAI credit: `openai,groq,cloudflare`.
- Each task can have its own list: `SCOUT_PROVIDERS`, `IDEAS_PROVIDERS`, `OPTIONS_PROVIDERS`,
  `BRIEF_PROVIDERS`, `POST_PROVIDERS`. Unset, a task uses `WRITER_PROVIDERS`. An entry can pin a
  model, e.g. `OPTIONS_PROVIDERS=cloudflare:@cf/nvidia/nemotron-3-120b-a12b,cloudflare,groq`
  (the options request is too big for Groq's free 8K tokens/min).
- `LLM_PROVIDERS` for everything else, and for the writer too when `WRITER_PROVIDERS` is not set.

| provider | model | cost | notes |
| --- | --- | --- | --- |
| OpenAI (`openai`) | `gpt-6-sol` | paid, capped | first writer; `OPENAI_DAILY_TOKENS` is the daily cap (code default 100000) |
| Groq (`groq`) | `openai/gpt-oss-120b` | free tier | fast, strict JSON; ~8K tokens/min, so it cannot take the options request. `qwen` (Qwen3.8 on Groq) is capped at 1K output tokens/min and does not work |
| OpenRouter | `openai/gpt-oss-120b:free` | free tier | backup; ~50 requests/day on free |
| Cloudflare | `@cf/openai/gpt-oss-120b`, `@cf/nvidia/nemotron-3-120b-a12b` | free tier, 10K neurons/day shared | takes every request size; output allowance is 2.5x because its models think first |
| Ollama (local) | `qwen3:8b` | free | runs on this laptop, slow, weaker writing |
| custom | e.g. DeepSeek V4.1 Flash, Kimi K3 | cents/cycle | any paid or rented host |

Cost: a scout is about 12K tokens, and each build plan a few times that, so the default cap
covers several batches a day. Past the cap the free backups write and the email says so.
`python -m postpilot doctor` tests that each backup really answers. Every setting is in `.env.example`.

## Disclaimer

PostPilot is an independent project, not affiliated with or endorsed by LinkedIn, Google, OpenAI,
Groq, Cloudflare or any other service it talks to. It drafts text and emails it to you; it never
posts anywhere. You are responsible for what you publish and for following the terms of every
service you connect. Model output can be wrong: check the facts and results before you post.

## Credits

The human-writing checks in `postpilot/vendor/` come from the MIT-licensed
[linkedin-agent-skill](postpilot/vendor/LICENSE-linkedin-agent-skill) by Jake Schincariol.

## License

[MIT](LICENSE)
