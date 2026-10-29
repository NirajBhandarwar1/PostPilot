# Contributing

```
python -m venv .venv && .venv\Scripts\activate      # source .venv/bin/activate on macOS/Linux
pip install -e ".[dev]"
ruff check . && mypy postpilot && pytest
```

- Tests never call the network or a model; `tests/conftest.py` blocks sockets. Add a test with every fix.
- Commit messages: one plain sentence in the imperative ("Fall back at once when a provider has no credit").
- Design decisions are recorded in `docs/adr/`. Add one when you change how the system works.
- Never commit `.env`; run `python -m postpilot precommit` before pushing.
