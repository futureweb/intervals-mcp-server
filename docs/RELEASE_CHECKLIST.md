# Release checklist

Use this list for every tagged release of `futureweb-intervals-mcp`
(<https://github.com/futureweb/intervals-mcp-server>). Copy it into the release tracking issue
and tick the boxes. A release is only cut when every gate is green.

## Release gates

### Functionality

- [ ] **Existing tools pass**: `uv run --locked pytest -q` is green locally and in CI on
      Python 3.12 and 3.13.
- [ ] **New tools pass**: every tool added since the last release has unit tests (happy path,
      argument validation, API error mapping) and was exercised once against a real Intervals.icu
      account by its author (results redacted, nothing committed).
- [ ] **No unreviewed live writes**: every code path that sends a `POST`, `PUT` or `DELETE` to
      Intervals.icu was reviewed in a pull request by a second person, and the tool description
      states clearly that it writes data.
- [ ] **Write/destructive tools gated server-side**: tools that create, update or delete data are
      not registered under the default (read-only) permission class. The gate lives in the server
      (`MCP_PERMISSIONS`), not in the client or in a prompt. Verify by starting the server with
      default settings and listing the tools from a client.

### Security and privacy

- [ ] **No publicly reachable unauthenticated API**: the default transport is `stdio`; `sse` and
      `streamable-http` bind to `127.0.0.1` by default; README, `Dockerfile` and `SECURITY.md`
      state that remote exposure requires an authenticating reverse proxy. No example config
      publishes a port on `0.0.0.0` without that warning.
- [ ] **No credentials or personal data in the repository or in fixtures.** Run and review:

      ```sh
      # real-looking athlete ids (tests use i1 / i12345 only)
      git grep -nE '\bi[0-9]{4,}\b' -- ':!uv.lock'
      # e-mail addresses (only the maintainer contact in pyproject.toml is allowed)
      git grep -nIE '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}' -- ':!uv.lock' ':!LICENSE'
      # hostnames and URLs; everything that is not a known public site needs a reason
      git grep -nIoE 'https?://[^ )"<>]+' -- ':!uv.lock' \
        | grep -vE 'intervals\.icu|github\.com|githubusercontent\.com|pypi\.org|python\.org|astral\.sh|docker\.com|opencontainers\.org|localhost|127\.0\.0\.1|example\.com'
      # anything that looks like a key or token
      git grep -nIE '(api[_-]?key|token|secret|password)[^=:]{0,10}[=:][[:space:]]*[A-Za-z0-9_-]{16,}' -- ':!uv.lock'
      # files that must never be tracked
      git ls-files | grep -E '(^|/)\.env($|\.)' | grep -v '\.env\.example$'
      ```

      Then open `tests/ressources/` and `tests/sample_data.py` and confirm that names, IDs, dates,
      locations and physiological values are synthetic.
- [ ] `uv.lock` contains no known vulnerable versions (the Dependabot alerts page is empty or
      every alert is triaged).

### Legal and project hygiene

- [ ] **License/attribution correct**: `LICENSE` is GPL-3.0, `pyproject.toml` declares
      `GPL-3.0-only`, the README credits the upstream project
      (`mvilanova/intervals-mcp-server`) and presents this repository as a fork; new files carry
      no conflicting license headers.
- [ ] **CI green** on `main` for the commit to be tagged (`ci.yml`: ruff, mypy and pytest on
      3.12 and 3.13; the pylint job is informational only).
- [ ] `CHANGELOG.md` has an entry for the new version listing all user-visible changes, including
      new or changed environment variables.

### Documentation

- [ ] **README install steps tested** from a clean checkout on Linux, macOS and Windows
      (`uv sync`, starting the server, configuring the client).
- [ ] **Garmin bridge optional and documented**: the server runs without any Garmin integration.
      If a Garmin bridge or sync is documented, it is clearly marked optional and has its own
      setup steps and its own data and privacy notes.
- [ ] **Example configs for ChatGPT and Claude verified**: the Claude Desktop / Claude Code
      configuration (`claude_desktop_config.json`) and the ChatGPT connector setup (SSE
      transport) in the README were followed verbatim on the release candidate and work.
- [ ] `.env.example` lists every supported environment variable with a safe default.

## Release steps

1. Make sure `main` is up to date and every gate above is ticked.
2. Bump the version in `pyproject.toml` (`uv version X.Y.Z`, or edit `[project].version`) and run
   `uv lock` so that `uv.lock` reflects the new version. The release workflow refuses tags whose
   version does not match `pyproject.toml`.
3. Update `CHANGELOG.md`: move the *Unreleased* entries under `## [X.Y.Z] - YYYY-MM-DD`.
4. Commit (`git commit -am "Release vX.Y.Z"`), open a pull request and merge it into `main`
   once CI is green.
5. Tag the merge commit and push the tag:

   ```sh
   git checkout main && git pull --ff-only
   git tag -a vX.Y.Z -m "vX.Y.Z"
   git push origin vX.Y.Z
   ```

6. Verify the **Release** workflow (`.github/workflows/release.yml`) on the Actions tab:
   - the `checks` jobs are green;
   - `build` produced `futureweb_intervals_mcp-X.Y.Z.tar.gz` and
     `futureweb_intervals_mcp-X.Y.Z-py3-none-any.whl`;
   - the GitHub release `vX.Y.Z` exists with auto-generated notes and both artifacts attached
     (edit the notes if they need polishing);
   - the `docker` job pushed `ghcr.io/futureweb/intervals-mcp-server:X.Y.Z`, `:X.Y` and
     `:latest`.
7. Verify that the GHCR image runs:

   ```sh
   docker pull ghcr.io/futureweb/intervals-mcp-server:latest
   docker run --rm -e API_KEY=... -e ATHLETE_ID=... ghcr.io/futureweb/intervals-mcp-server:latest
   ```

   The server must start without a traceback. Without `-i` its stdin is closed immediately, so it
   logs the stdio start-up line and exits with status 0; add `-i` to keep it attached to a client.
   A quick import check that needs no client:
   `docker run --rm -e API_KEY=x -e ATHLETE_ID=i1 ghcr.io/futureweb/intervals-mcp-server:latest python -c "import intervals_mcp_server.server"`.
8. First release only: the GHCR package is private after the first push. Make it public in the
   package settings (<https://github.com/orgs/futureweb/packages>) and confirm it is linked to the
   repository.
9. If the PyPI job is enabled (it is disabled by default, see `release.yml`): check
   <https://pypi.org/project/futureweb-intervals-mcp/> and install the new version into a fresh
   virtual environment (`pip install futureweb-intervals-mcp==X.Y.Z`).
10. Announce the release (GitHub Discussions, upstream issue if the change is relevant there) and
    open the next *Unreleased* section in `CHANGELOG.md`.
