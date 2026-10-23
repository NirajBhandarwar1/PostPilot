# ADR-005: GPT-6 writes under a daily cap; free models are an ordered backup

**Status:** Accepted (cap and GPT-6 built; backup order and retry change pending: task T7)
**Date:** 2026-10-23
**Deciders:** Niraj Bhandarwar (session choices; review decisions D7, D21, D24)

## Context
The project started free-only (Groq, Cloudflare, OpenRouter). In a bake-off on the same viral stories, only GPT-6 Sol produced sharp, feasible ideas without invented results, so the user approved his own OpenAI key. Costs must stay small and predictable, and a bug must never run up the bill. Live testing showed the Groq free backups reject the large options/build requests ("Request too large") and that the client retried those hopeless errors for about 2 minutes per call.

## Decision
- **Writer:** GPT-6 Sol for topics, ideas, options, BUILD.md and posts.
- **Cap:** at most 300,000 GPT-6 tokens per day (`OPENAI_DAILY_TOKENS`); past it, backups take over and the email says so.
- **Backup order:** openai, cloudflare (gpt-oss-120b), openrouter (free), qwen, groq; each new backup is confirmed with one live JSON-schema call before it's enabled.
- **Retries:** "request too large" is never retried; busy 429s and 5xx get up to 3 retries.
- **Eval budget:** the eval has its own 60k-token limit per run and never leaves the live loop under 100k for the day.

## Options Considered

### Option A: GPT-6 + capped budget + ordered free backups (chosen)
| Dimension | Assessment |
|---|---|
| Complexity | Low |
| Cost | About $0.30-0.50 per cycle, about $3-5 a month |
| Scalability | Cap bounds worst-case spend |
| Team familiarity | High |

**Pros:** best quality where it matters; spend can't run away; outages degrade instead of failing.
**Cons:** backup-written emails are weaker; free tiers can change limits without notice.

### Option B: Free models only
**Pros:** $0. **Cons:** generic, hedged ideas in testing; small free limits fail on large requests.

### Option C: Size-aware routing (original D6)
**Pros:** skips backups that can't fit a request. **Cons:** a hand-kept size table that goes stale; once hopeless retries stop, simple ordering gives the same result.

### Option D: Queue and wait for GPT-6 during outages
**Pros:** quality never drops. **Cons:** hours of delay; a queue to build and maintain.

## Trade-off Analysis
The user values on-time emails and clearly flagged quality drops over waiting, and doesn't want to maintain provider limit tables. Ordering by capacity plus fail-fast on "too large" gets the benefit of C without its upkeep. The cap plus the eval reserve bounds the worst-case bill.

## Consequences
- Easier: predictable cost; resilience to a GPT-6 outage.
- Harder: prompts must work acceptably on weaker backups; free-tier changes need the live probe rerun.
- Revisit: when a newer OpenAI family ships (TODO: detect it automatically); raise or lower the cap after a month of real usage data.

## Action Items
1. [x] GPT-6 writer and 300k/day cap.
2. [ ] Fail fast on "request too large"; new backup order after live probes (task T7).
3. [ ] Eval budget and live reserve (task T10).
4. [ ] TODO: keep the newest OpenAI family automatically.
