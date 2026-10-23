# TODOS

## Models

### Keep the newest OpenAI model family automatically

**What:** Make `fetch_openai` in `postpilot/factgate.py` keep the newest gpt-N chat family (for example GPT-7 when it ships) instead of matching only `gpt-6(\.\d)?-(sol|luna|astra)`.

**Why:** A new OpenAI flagship is a prime face-off story, but today it would never appear in the model list or as a writer, and nothing warns you.

**Context:** From the 2026-10-23 eng review (decision D27). The OpenAI catalog is fetched with your key and filtered by a hard-coded regex. Start in `fetch_openai`: find the highest `gpt-N` among returned ids, keep its chat variants, skip non-chat ids (embeddings, audio, image). Add a test with a fake catalog containing gpt-6 and gpt-7 ids.

**Effort:** S
**Priority:** P3
**Depends on:** None

## Research

### Run the research sources in parallel

**What:** Run the YouTube, GitHub, Hacker News, LinkedIn/X and docs lookups at the same time (thread pool, same per-source timeout) instead of one after another.

**Why:** When sites are slow, an options request can block the app for about 5 minutes; in parallel the worst case is about 1 minute.

**Context:** From the 2026-10-23 eng review (decision D28). Today `research()` loops `for key, job in jobs.items():` with one retry per source and 25 s HTTP timeouts. Do this after the D22 merge moves research into `sources.py` with renamed functions. Keep the "a failed source returns nothing" behavior and its tests.

**Effort:** S
**Priority:** P3
**Depends on:** D22 merge (research.py into sources.py)

## Completed
