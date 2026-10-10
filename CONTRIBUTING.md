# Contributing to Futureweb Intervals MCP

Thank you for taking the time to contribute! This is the Futureweb fork of the Intervals.icu MCP server (upstream: mvilanova/intervals-mcp-server). The project uses **Python 3.12** and manages its dependencies with [uv](https://github.com/astral-sh/uv). The following guide summarizes how to set up your environment and outlines the workflow we expect for pull requests.

## Development environment

1. Create a virtual environment and activate it:
   ```bash
   uv venv --python 3.12
   source .venv/bin/activate
   ```
2. Install all dependencies (including development extras):
   ```bash
   uv sync --all-extras
   ```
3. When working on or manually running the server, use:
   ```bash
   uv run futureweb-intervals-mcp            # --doctor checks the configuration first
   ```
   The MCP Inspector needs the SDK's CLI extra, which is not a project dependency:
   `uv run --with "mcp[cli]" mcp dev src/intervals_mcp_server/server.py`.

## Dependency changes

1. Edit `pyproject.toml`.
2. Run `uv lock` (or `uv sync`).
3. Commit **both** `pyproject.toml` and `uv.lock` in the same commit.

If you add, remove, or relax a dependency but forget to update the lock file, CI will fail. Treat `uv.lock` as a first-class artifact: review it when it changes, but don’t fear committing it.

## Code-only changes

For changes that do not modify dependencies, keep the lock file untouched. Run your tests with:

```bash
uv run --locked pytest
```

CI will also run `uv lock --check` to ensure `uv.lock` stays in sync.

## Why keep the lock file?

* **Reproducibility** – All collaborators and CI runners install identical hashes.
* **Security** – Hash pinning in `uv.lock` helps prevent supply-chain attacks.
* **Speed** – `uv` skips resolution when the lock matches, keeping installs lightning-fast.

Automated dependency upgrades are encouraged. You can use Dependabot, Renovate, or a scheduled GitHub Action that runs `uv lock --upgrade && git push` to keep the file fresh and generate tidy PRs.

## Testing

Before opening a pull request, ensure all checks pass locally:

```bash
uv run --locked ruff check .
uv run --locked mypy src tests
uv run --locked pytest
```

CI runs the same three checks on Python 3.12 and 3.13; pylint is advisory. New tools must declare their permission class with `@tool("read" | "write" | "destructive" | "admin")`, be read-only unless they really write, carry a short docstring (at most 900 characters, first sentence: when to use the tool) and a described parameter for every argument (`Annotated[..., Field(description=...)]`, `Literal` enums for fixed values; details in `guides/methods/`), and come with tests that use synthetic fixtures only (never real athlete data, hostnames or keys).

## Pull request guidelines

* Use concise commit messages.
* Give the pull request a short imperative title and describe what changed and why.
* Describe any manual testing you performed and confirm whether `ruff`, `mypy`, and `pytest` passed.

We appreciate your contributions and your attention to these guidelines. Happy coding!
