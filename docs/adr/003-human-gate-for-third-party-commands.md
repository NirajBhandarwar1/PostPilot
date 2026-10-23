# ADR-003: Every third-party install/run command goes through a human review step

**Status:** Accepted (implementation pending: task T1)
**Date:** 2026-10-23
**Deciders:** Niraj Bhandarwar (review decisions D3, D16)

## Context
BUILD.md (ADR-002) is pasted into Cursor, which runs commands. When the hot thing is a viral GitHub repo, the plan's install and usage steps come from that repo's README, which is attacker-controllable. In 2026 Mozilla's 0DIN team demonstrated exactly this: a README whose setup steps make a coding agent pipe a downloaded script into bash. Today, model-written setup commands and the package list sit in Step 1, which Cursor runs automatically; later steps are marked 🤖 or 🧑 by the model itself; the safety scan only lists warnings near the end of the file.

## Decision
- Step 1 contains only fixed template commands and packages.
- Every model-written install, clone or run command, and the model-written package list, goes into a 🧑 "Review, then run" step, shown next to its safety-scan warning.
- Every step's text is scanned; any step that clones, installs or runs the third-party repo is forced to 🧑, regardless of the model's label.
- BUILD.md recommends Windows Sandbox (or a dev container) for running third-party repos.
- The prompt no longer pastes commands from README text; it names the official install command.

## Options Considered

### Option A: Human gate for all third-party commands (chosen)
| Dimension | Assessment |
|---|---|
| Complexity | Low-Med: renderer and scan changes |
| Cost | A few minutes of reading per build |
| Scalability | Same for every plan |
| Team familiarity | High |

**Pros:** closes all three paths (setup list, package list, later steps); matches 2026 guidance (human approval for untrusted setup steps).
**Cons:** one or two extra manual steps per build; still can't see what a repo does at runtime (hence the sandbox advice).

### Option B: Gate only setup_commands (original D3)
**Pros:** smaller change. **Cons:** package list and later 🤖 steps stay open; found incomplete by the second reviewer.

### Option C: Gate only what the scan flags
**Pros:** no extra steps when the scan is clean. **Cons:** text scans miss runtime tricks (a package that downloads more code later).

### Option D: Rely on Cursor's own command approval
**Pros:** no PostPilot change. **Cons:** users often allow all commands for a session, which removes the prompt; not under PostPilot's control.

## Trade-off Analysis
The cost of A is minutes per build; the downside it prevents is code execution on the laptop that holds `.env` with API keys. B and C leave known paths open. D depends on a setting the user may disable. A defense in the plan itself works regardless of editor settings.

## Consequences
- Easier: trusting BUILD.md; the 🧑 marks show exactly where judgment is needed.
- Harder: builds have a couple more manual steps; plan rendering must classify commands reliably.
- Revisit: if Cursor gains reliable per-command sandboxing, consider relaxing the gate for commands that run inside it.

## Action Items
1. [ ] Implement the gate, scans and sandbox advice (task T1).
2. [ ] Test: no model-written command or package ever appears in Step 1; clone/install steps are always 🧑.
