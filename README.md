# PostPilot

An always-on agent that finds what's hot in AI every 3 days, emails you 3 LinkedIn post ideas,
and when you reply with a number, builds that idea: the experiment code, the step-by-step plan,
and a humanized post draft ready to fill in and paste.

The goal is to get noticed by people who hire in AI, by posting experiments rather than news.

## How a cycle works

1. **Scout.** Pulls trending items from AI news feeds (Google News, TechCrunch, The Verge,
   OpenAI, Google AI, Hugging Face, Simon Willison), Hacker News, new GitHub repos and
   Hugging Face papers. All free, no keys.
2. **Pick.** An open-source model picks 3 ideas you can build, test or compare in about an hour. Each one
   targets a different role family and skips topics you've already covered.
3. **Email.** You get `[PostPilot #N] 3 AI post ideas`. Reply `1`, `2`, `3` or `skip`.
4. **Options.** Your reply `2` brings `[PostPilot #N.2]`: 3-5 build options for that story,
   based on what is already viral (YouTube, GitHub, Hacker News). Always one "look what I
   built" demo and one public GitHub repo. Reply `3` to pick option 3 (or reply `2 go` to the
   first email to skip the menu).
5. **Build plan.** You get `BUILD.md` (emailed and saved to `../linkedin/builds/`). Part A is
   for Cursor: goal, stack, files, setup, models with exact base URLs and key names, 6-10 steps
   each with a "Done when" check, tests and rules. Part B is for you: demo shot list, the
   LinkedIn post (humanized, `{{placeholders}}` for real results), first comment, fact checks.
6. **You** open an empty folder in Cursor, add BUILD.md, tell it to build Part A, then record
   the demo, fill in the placeholders and post it yourself.

PostPilot never posts to LinkedIn. Automating a personal profile breaks LinkedIn's terms, and
the drafts never make up results.

## Setup (Windows)

```bat
cd D:\Trading\plan\postpilot
copy .env.example .env      & REM then fill in .env
.venv\Scripts\python -m postpilot status
.venv\Scripts\python -m postpilot test-email
.venv\Scripts\python -m postpilot scout       & REM first batch, right now
start.bat                                      & REM keep it running
```

Fill in `D:\Trading\plan\linkedin\voice.md` too. Without it the drafts sound generic.

## Commands

| command | does |
| --- | --- |
| `python -m postpilot run` | keep running: scout every 3 days, check replies every 10 min |
| `python -m postpilot scout [--no-email]` | scout once now |
| `python -m postpilot options 4 2 [--no-email]` | 3-5 build options for idea 2 of batch #4 (what your email reply `2` does) |
| `python -m postpilot build 4 2 --option 3` | build option 3 of that idea (reply `3` to the `#4.2` email); without `--option`, builds the rough idea |
| `python -m postpilot check` | check email replies once |
| `python -m postpilot status` | setup check and recent batches |
| `python -m postpilot precommit` | secret check before you commit (blocks .env, real key values, key shapes) |
| `python -m postpilot eval [N]` | replay the last N saved runs: fact-check failure rate and unverified claims per idea |
| `python -m postpilot doctor` | live check: how many signals each trend source returns right now |
| `python -m postpilot posted 5 <url>` | after you publish: record the LinkedIn link for batch #5 |
| `python -m postpilot inbound 5 --recruiters 2 --founders 1` | record who reached out after that post |
| `python -m postpilot report` | which formats and topics bring the most recruiters and founders |
| `.venv\Scripts\python -m pytest -q` | run the tests (offline, ~1 second) |

## Models (updated 2026-10-22)

- Topic finding and all writing (ideas, posts, code) use `WRITER_PROVIDERS` in `.env`:
  `openai,qwen,groq` = GPT-6 Sol first, then free Qwen3.8 and gpt-oss on Groq as backups.
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

## Files

```
postpilot/sources.py   free trend sources
postpilot/llm.py       open-model client (Groq / OpenRouter / Ollama / any OpenAI-compatible)
postpilot/brain.py     prompts: pick 3 ideas, build post + code
postpilot/mailer.py    Gmail send + read replies (App Password)
postpilot/pipeline.py  the cycle and the always-on loop
postpilot/vendor/      humanizer + 21 hook formulas, from
                       github.com/Jakeschincariol/linkedin-agent-skill (MIT)
data/                  SQLite history and log (not committed)
```

## Model

Open-source models only, through any OpenAI-compatible endpoint (`postpilot/llm.py`).
Providers in `LLM_PROVIDERS` are tried in order; if one fails, the next one takes over.

| provider | model | cost | notes |
| --- | --- | --- | --- |
| Groq (default) | `openai/gpt-oss-120b` | free tier | fast, strict JSON; ~8K tokens/min, prompts are sized for it |
| OpenRouter | `openai/gpt-oss-120b:free` | free tier | backup; ~50 requests/day on free |
| Ollama (local) | `qwen3:8b` | free | runs on this laptop, slow, weaker writing |
| custom | e.g. DeepSeek V4.1 Flash, Kimi K3 | cents/cycle | best open-weight quality, paid host |

A cycle uses 1 model call to pick ideas and 2 per build, well inside free limits.
