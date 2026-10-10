# Contributor Guide

This project is a Python 3.12 backend service built with FastMCP and httpx. All source code lives under `src/intervals_mcp_server` and tests live under `tests`.

## Development Environment
- Use [uv](https://github.com/astral-sh/uv) to create and manage the virtual environment.
  - `uv venv --python 3.12`
  - `source .venv/bin/activate`
- Sync dependencies including dev extras with `uv sync --all-extras`.
- When editing or running the server manually use `uv run futureweb-intervals-mcp` (`--doctor` checks the configuration).

## Testing Instructions
- Run unit tests with `pytest` from the repository root.
- Ensure linting passes with `uv run --locked ruff check .`.
- Run static type checks using `uv run --locked mypy src tests`.
- Pylint (`pylint --disable=C0301 $(git ls-files '*.py')`) is advisory; keep new code free of new messages (targeted `# pylint: disable=` comments are accepted for long tool functions).
- Every MCP tool is registered with `@tool("<permission class>")` from `intervals_mcp_server.mcp_instance`; tools of disabled classes are not exposed (MCP_PERMISSIONS, default read).
- Tests use synthetic fixtures (see `tests/sample_data.py`); never add real athlete ids, hostnames or keys.
- All three steps (`ruff`, `mypy`, and `pytest`) should succeed before committing.

## PR Instructions
- Use concise commit messages.
- Use a short imperative pull request title; mention merged upstream PRs or adopted ideas with their number and author.
- Describe any manual testing steps performed and mention whether `pytest`, `ruff`, and `mypy` passed.

There is currently no frontend code in this repository. If a frontend is added in the future (for example with React or another framework), document how to run and test it within this file.
