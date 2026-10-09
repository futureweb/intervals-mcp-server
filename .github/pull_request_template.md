## Summary

<!-- What does this change and why? Link the issue it closes, e.g. "Closes #12". -->

## Type of change

- [ ] Bug fix
- [ ] New tool or feature
- [ ] Refactoring or maintenance
- [ ] Documentation
- [ ] CI, packaging or Docker

## Checklist

- [ ] Tests added or updated; `uv run --locked pytest -q` passes locally
- [ ] `uv run --locked ruff check .` passes
- [ ] `uv run --locked mypy src tests` passes
- [ ] No real athlete data in code, tests or fixtures (athlete IDs, names, e-mail addresses, GPS traces and physiological values are synthetic)
- [ ] No secrets (API keys, tokens, `.env` contents) in the diff or in the commit history
- [ ] Read-only default respected: tools that write or delete data are gated behind the permission class (`MCP_PERMISSIONS`) and are not exposed by default
- [ ] Docs updated where behaviour or configuration changed (README tool list, `.env.example`, `CHANGELOG.md`)

## How was this tested?

<!-- Unit tests only? Against a real Intervals.icu account (which tools, data redacted)? Which client and transport? -->
