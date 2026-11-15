# ADR-006: Evaluate with rules on golden stories, plus real-use signals; no model judge

**Status:** Accepted
**Date:** 2026-11-12
**Deciders:** Niraj Bhandarwar

## Context
PostPilot writes three things: an options email, a BUILD.md plan and a LinkedIn post. A prompt or model change can make any of them worse without a single test failing, and the only quality number was the post's human score. The earlier eval replayed saved stories with a paid model under a token budget. Everything now runs on free models with small daily limits, so an eval must be cheap, repeatable and honest about what it cannot measure.

## Decision
Two kinds of evidence, no model judge:

1. **Golden stories** (`evals/golden/*.json`, committed): a saved public story plus the option and research a past run picked. `postpilot eval run` pushes each one through the current prompts and scores the output with deterministic rules (`postpilot/evals/metrics.py`): option count and fields, step count, "Done when" on every step, wrong-usage warnings (removed OpenAI call, npm names in pip, provider-prefixed model ids), non-ASCII hyphens in commands, balanced code fences, duplicate steps, third-party commands gated for the human, post length, hook, placeholders, invented claims and figures, a closing question, hashtags and the human score.
2. **Real-use signals** (`postpilot eval live`): pick rate, your 1-5 idea ratings, share of model attempts that gave usable output (and so how often a fallback ran), speed per task, inbound messages per post. These are read from the database; nothing new is collected.

Runs are stored in `evals/results/` (not committed) and compared with the last run: a check that passed and now fails is a regression, and the exit code is 1 for a regression of weight 1 or more or an overall drop of more than 5 points. `eval replay` re-scores stored outputs offline, so a changed rule can be tried for free.

## Options Considered

### Option A: Rules plus real-use signals (chosen)
| Dimension | Assessment |
|---|---|
| Complexity | Low: pure functions, one runner |
| Cost | 2 to 3 model calls per story, on free tiers |
| Trust | High for what it checks; silent about taste |

**Pros:** repeatable, explains exactly what failed, works offline in replay. **Cons:** cannot tell a dull post from a sharp one.

### Option B: Model as judge
**Pros:** can rate taste. **Cons:** free models judge poorly and agree with themselves, and every judge call uses quota the product needs.

### Option C: Only your ratings
**Pros:** the truest signal. **Cons:** slow and sparse; no signal at all before a post goes out.

## Trade-off Analysis
Rules catch the failures we have actually seen (a hallucinated model, a wrong API call, duplicated steps) and can run every time a prompt changes. Taste is left to the person it is for: pick rate and ratings predict what rules cannot, and they cost nothing.

## Consequences
- Easier: change a prompt, run `eval run`, see which checks moved.
- Harder: a rule is only as good as the failure it was written for; new failures need a new check.
- Three stories is noisy. Add stories with `eval capture` as new kinds of trend appear.
- Revisit: a judge, if a free model ever scores consistently against your own ratings.

## Action Items
1. [x] Rules, runner, golden stories, `eval live`.
2. [ ] Run `eval run` before changing a prompt or the model order, and after.
3. [ ] Add a golden story whenever a new kind of failure appears in a real run.
