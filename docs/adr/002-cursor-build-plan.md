# ADR-002: Hand Cursor a build plan (BUILD.md) instead of generating code

**Status:** Accepted
**Date:** 2026-10-23
**Deciders:** Niraj Bhandarwar

## Context
After picking an option, the user needs to build it in roughly 1-3 hours. The first version of PostPilot generated the experiment code itself with one model call. That code was written blind (never run), often broke, and was limited to tiny scripts. The user builds with Cursor, an AI code editor that can run commands, read errors and fix its own code.

## Decision
PostPilot writes a structured plan, BUILD.md, and emails it. Part A is for Cursor (goal, stack, files, fixed Step 1 setup, 🤖/🧑 steps each with a runnable "Done when" check, tests, rules, README outline). Part B is for the user (demo shot list, humanized LinkedIn post, first comment, fact checks). Model ids, API base URLs and key names are filled in by code, never by the model.

## Options Considered

### Option A: Build plan for Cursor (chosen)
| Dimension | Assessment |
|---|---|
| Complexity | Medium: a schema'd plan plus a renderer |
| Cost | About $0.10-0.20 of GPT-6 per build |
| Scalability | Any project size Cursor can handle |
| Team familiarity | High (user already uses Cursor) |

**Pros:** code is written by an agent that can run and fix it; bigger, nicer demos; checks per step; the plan doubles as documentation.
**Cons:** depends on Cursor; the plan is executed by an agent, which creates a prompt-injection path (see ADR-003).

### Option B: Generate the code in PostPilot
| Dimension | Assessment |
|---|---|
| Complexity | Low |
| Cost | Similar |
| Scalability | Only small single-file scripts |
| Team familiarity | High |

**Pros:** runnable files arrive in the email; no other tool needed.
**Cons:** code is never executed before delivery; breaks often; can't iterate.

### Option C: Run a coding agent inside PostPilot
| Dimension | Assessment |
|---|---|
| Complexity | High: sandbox, agent loop, long jobs |
| Cost | Much higher per build |
| Scalability | Good |
| Team familiarity | Low |

**Pros:** finished project in the inbox.
**Cons:** unattended code execution on the laptop; large security and cost surface; the user loses the hands-on part that the post is about.

## Trade-off Analysis
Option B was tried and failed in practice. Option C removes the user's own building, which is the point of the posts, and adds unattended execution risk. Option A keeps the human in the loop at low cost, and the fixed template parts (Step 1, model table, publish and GIF steps) make the plan reliable regardless of model quality.

## Consequences
- Easier: ambitious builds; a reusable plan format for every kind of hot thing (repo, model, product, paper).
- Harder: plan quality must be measured (review D14/D24/D25 eval); the plan is untrusted input to an agent (ADR-003).
- Revisit: macOS commands for the MacBook Pro build machine; a Cursor rules file (`.cursor/rules`) generated alongside BUILD.md.

## Action Items
1. [x] Fixed Step 1, 🤖/🧑 steps, publish and GIF steps, model table filled by code.
2. [ ] Eval with pass/fail bars for BUILD.md and posts (task T10).
3. [ ] macOS variant of setup commands (later).
