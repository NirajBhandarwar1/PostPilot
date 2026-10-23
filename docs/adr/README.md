# Architecture Decision Records

| ADR | Decision | Status |
|---|---|---|
| [001](001-email-as-interface.md) | Email replies are the user interface | Accepted |
| [002](002-cursor-build-plan.md) | Hand Cursor a build plan (BUILD.md) instead of generating code | Accepted |
| [003](003-human-gate-for-third-party-commands.md) | Every third-party install/run command goes through a human review step | Accepted (not yet built) |
| [004](004-per-build-state-merge-on-save.md) | Track state per build, save with merge-on-write in SQLite | Accepted (not yet built) |
| [005](005-writer-model-routing.md) | GPT-6 writes under a daily cap; free models are an ordered backup | Accepted (partly built) |

"Not yet built" = approved in the 2026-10-23 eng review (tasks T1-T10), implementation pending.
Full review: `C:\Users\DELL\.gstack\projects\postpilot\unknown-eng-review-20261023-181119.md`.
