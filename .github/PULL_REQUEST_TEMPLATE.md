## What and why

<!-- One or two sentences. Link the issue if there is one. -->

## Checklist

- [ ] `ruff check . && mypy postpilot && pytest` pass locally
- [ ] New behaviour has a test (tests never touch the network or a model)
- [ ] No secrets, personal emails or local paths in the diff (`python -m postpilot precommit`)
- [ ] Docs or an ADR updated if behaviour changed
