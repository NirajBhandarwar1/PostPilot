# ADR-001: Email replies are the user interface

**Status:** Accepted
**Date:** 2026-10-23 (decision made 2026-10-19, recorded now)
**Deciders:** Niraj Bhandarwar

## Context
PostPilot has one user who wants hot AI ideas every 3 days, picks one, and gets a build plan. He reads on his phone, often between other work, and wants to start with zero UI to build or host. Answers take minutes to generate, so the interaction is naturally asynchronous.

## Decision
Use Gmail as the interface: PostPilot sends tagged emails (`[PostPilot #9]`, `[PostPilot #9.3]`) and acts on short replies (`3`, `3 go`, `3 new`, `2`, `skip`). Replies are accepted only from the user's own address with a passing DKIM/DMARC check.

## Options Considered

### Option A: Email replies (chosen)
| Dimension | Assessment |
|---|---|
| Complexity | Low: SMTP/IMAP with an App Password |
| Cost | Free |
| Scalability | Fine for one user; reply grammar limits richer interactions |
| Team familiarity | High (standard library only) |

**Pros:** works on any phone, no hosting, a permanent record of every batch, fits the async pace.
**Cons:** only simple number replies; no editing an option; IMAP edge cases (read flags, sessions); polling every 10 minutes.

### Option B: Telegram bot
| Dimension | Assessment |
|---|---|
| Complexity | Medium: bot API, webhook or long polling |
| Cost | Free |
| Scalability | Same single user; buttons and free-text follow-ups possible |
| Team familiarity | Medium |

**Pros:** instant, buttons instead of typed numbers, easy follow-ups ("make it easier").
**Cons:** another app and account; long BUILD.md content is awkward in chat; no inbox archive.

### Option C: Small web app
| Dimension | Assessment |
|---|---|
| Complexity | High: UI, auth, hosting |
| Cost | Hosting and a domain |
| Scalability | Best for rich interactions |
| Team familiarity | Medium |

**Pros:** full control: edit options, compare builds, dashboards.
**Cons:** most work; needs always-on hosting and login security for a one-person tool.

## Trade-off Analysis
For one user and a 3-day cadence, the cost of building and securing a UI outweighs richer interaction. Email gives an archive and phone access for free; its weakness (simple replies only) is covered by a small reply grammar. The state machine behind the replies is interface-independent, so switching later is cheap.

## Consequences
- Easier: nothing to host or log into; every batch is searchable in Gmail.
- Harder: reliable reply handling (read flags, crashes mid-handling, old emails replied to later); see ADR-004 and review decisions D4, D8, D23.
- Revisit: when you want to edit or discuss options, add a Telegram bot on top of the same state machine.

## Action Items
1. [ ] Set up the Gmail App Password in `.env` (Niraj).
2. [ ] Mark replies read only after handling, using IMAP UIDs (review D8, task T6).
3. [ ] Revisit after a month of real use: is the reply grammar enough?
