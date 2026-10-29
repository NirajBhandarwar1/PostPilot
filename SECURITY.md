# Security

PostPilot sends and reads email for one owner and runs nothing it writes. Its main safeguards:

- Secrets live only in `.env` (git-ignored). `python -m postpilot precommit` blocks any file that
  contains a `.env` value or an API-key shape.
- Replies are accepted only from the owner's Gmail address, and only when Gmail's own
  `Authentication-Results` header passes.
- Model-written build steps that fetch or run third-party code are marked as human-only
  (see `docs/adr/003-human-gate-for-third-party-commands.md`); web fetches refuse private addresses.

Found a vulnerability? Please open a private security advisory on GitHub instead of a public issue.
