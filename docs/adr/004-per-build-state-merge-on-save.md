# ADR-004: Track state per build, save with merge-on-write in SQLite

**Status:** Accepted (implementation pending: tasks T2-T5)
**Date:** 2026-10-23
**Deciders:** Niraj Bhandarwar (review decisions D4, D5, D18, D19, D20, D23, D26)

## Context
Each scout creates one batch row in SQLite with a JSON blob (ideas, options per idea, research) and single `status` / `chosen` columns. The flow grew to options per idea and several builds per batch, which the single-value columns can't represent:
- Asking options for idea 1 after building idea 3 overwrote "built", so a built story dropped out of history (breaking the rule "built stories count as covered").
- Re-asking options replaced the list behind an older options email, so a reply could build a different option than the one shown.
- Saves read the whole blob, spend minutes on model calls, then write it all back, so two overlapping saves can erase each other.
- Post results (`outcomes`) are keyed per batch, so results get credited to the wrong build.

## Decision
- Keep SQLite and the JSON blob, but store a **builds list** per batch (idea, option, time, folder). Status only moves forward: draft < sent < options_sent < built < posted; "skip" only before a build.
- **Options are reused** for repeat requests; `3 new` / `--fresh` makes a new tagged list (#N.3b) and replies to the old tag get a "replaced" note.
- A redo of an already-built idea+option **resends the saved BUILD.md**.
- **Merge-on-save:** each save re-reads the latest blob inside one SQLite write transaction and merges only its own change.
- **Outcomes per build:** `posted`/`inbound` attach to the latest build by default, `--build N` otherwise.

## Options Considered

### Option A: Builds list in the JSON blob + merge-on-save (chosen)
| Dimension | Assessment |
|---|---|
| Complexity | Medium |
| Cost | None |
| Scalability | Fine for hundreds of batches |
| Team familiarity | High |

**Pros:** small change from today; old batches stay readable; one place to look per batch.
**Cons:** cross-batch queries need JSON parsing; correctness depends on every save using the merge helper.

### Option B: Normalized tables (batches, ideas, options, builds, outcomes)
| Dimension | Assessment |
|---|---|
| Complexity | High: migration plus rewritten access code |
| Cost | None |
| Scalability | Best; easy analytics |
| Team familiarity | High |

**Pros:** row-level updates with no overwrite risk; easy reports.
**Cons:** large migration now for one user's data volume; more code to change at once.

### Option C: Never downgrade status only
**Pros:** a one-line guard. **Cons:** still one build per batch; overwrites and wrong-option replies remain.

## Trade-off Analysis
At about 10 batches a month, B's benefits (queries, scale) don't pay for its migration cost yet. A fixes every observed bug class (lost history, wrong option, overwrite, mis-credited results) with small, testable changes. C fixes only the most common symptom.

## Consequences
- Easier: building more than one idea per batch; honest recruiter reports; safe redos.
- Harder: every new writer must use the merge helper; JSON shape changes need care for old batches.
- Revisit: move to normalized tables (Option B) when analytics across many batches matter, or past about 500 batches.

## Action Items
1. [ ] Options reuse and fresh lists (task T2).
2. [ ] Builds list, status order, second-idea replies, redo resend (task T3).
3. [ ] Merge-on-save with an overlap test (task T4).
4. [ ] Outcomes per build (task T5).
