# Releasing

One version tag publishes everything. Pushing `vX.Y.Z` (or `vX.Y.ZbN`, `vX.Y.ZrcN`) runs
[`.github/workflows/release.yml`](.github/workflows/release.yml):

| Job | Publishes | Needs |
| --- | --- | --- |
| `checks` | nothing: the whole CI (`ci.yml`): tests, sdist/wheel, Docker smoke test, `server.json` validation, Claude Desktop bundle build and smoke test, actionlint | – |
| `version` | nothing: tag = `pyproject.toml` = `__version__` = `server.json` = bundle manifest; classifies final / pre-release | `checks` |
| `docker` | `ghcr.io/futureweb/intervals-mcp-server` (amd64 + arm64) | `version` |
| `dockerhub` | the same image on Docker Hub (`vars.DOCKERHUB_IMAGE`), the Docker Hub overview from [docs/DOCKERHUB.md](docs/DOCKERHUB.md) | `docker`; skipped without the variable |
| `pypi` | `futureweb-intervals-mcp` on PyPI (Trusted Publishing, PEP 740 attestations) | `docker`, environment `pypi` |
| `github-release` | GitHub (pre-)release with sdist, wheel and `futureweb-intervals-mcp-X.Y.Z.mcpb` | `docker`, `pypi` |
| `mcp-registry` | `io.github.futureweb/intervals-mcp-server` in the [official MCP Registry](https://registry.modelcontextprotocol.io) | `docker`, `pypi` |

Image tags: final `X.Y.Z`, `X.Y` and `latest`; betas and release candidates only `X.Y.ZbN` /
`X.Y.ZrcN`. PyPI marks pre-releases as such (pip and uv skip them unless asked, or while no final
version exists). The release gates are in [docs/RELEASE_CHECKLIST.md](docs/RELEASE_CHECKLIST.md).

## One-time setup (repository owner)

Nothing here is changed by the workflow; these settings are made by hand.

1. **GitHub environment `pypi`** (Settings → Environments → New environment `pypi`):
   *Deployment branches and tags* → *Selected branches and tags* → add the tag rule `v*`.
   Optionally *Required reviewers*: the PyPI job then waits for an approval in the run. Without
   this step GitHub creates the environment on the first run, without any protection.
2. **PyPI Trusted Publisher**: registered as a pending publisher for the project
   `futureweb-intervals-mcp`, owner `futureweb`, repository `intervals-mcp-server`, workflow
   `release.yml`, environment *any*. The first upload turns it into a normal publisher.
   Recommended afterwards: on PyPI (project → Settings → Publishing) add the same publisher
   with environment `pypi` and remove the one without environment, so that only the protected
   environment can publish.
3. **Docker Hub** (optional): public repository `futurewebat/futureweb-intervals-mcp`, the
   repository variable `DOCKERHUB_IMAGE=futurewebat/futureweb-intervals-mcp` and the secrets
   `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` (a personal access token; *Read & Write* is enough
   for the image, the overview sync needs *Read, Write, Delete*, otherwise that step fails
   without failing the job). Without the variable the job is skipped; with the variable but
   without the secrets it warns and does nothing. The short description is kept as set on
   Docker Hub; only the full overview is synced.
4. **GHCR visibility**: the first `docker` job creates the package
   `ghcr.io/futureweb/intervals-mcp-server`, which may start out **private**. It must be public
   for anonymous pulls and for the MCP Registry, which reads the image label anonymously. Make
   it public (organization → Packages → `intervals-mcp-server` → Package settings → Change
   visibility; check that it is linked to this repository); the `mcp-registry` job stops with a
   clear error until then and can simply be re-run.
5. **MCP Registry**: nothing to configure. `mcp-publisher login github-oidc` exchanges the
   workflow's OIDC token for the namespace `io.github.futureweb/*`; no secret is stored.

## Release steps

1. Tick the gates in [docs/RELEASE_CHECKLIST.md](docs/RELEASE_CHECKLIST.md) on `main`.
2. Set the new version everywhere it is stored and check it (jq rewrites the JSON layout, which
   is fine):

   ```sh
   NEW=1.0.0b2
   uv version "$NEW"                                   # pyproject.toml and uv.lock
   sed -i "s/^__version__ = .*/__version__ = \"$NEW\"/" src/intervals_mcp_server/__init__.py
   jq --arg v "$NEW" '.version = $v
     | (.packages[] | select(.registryType == "pypi")).version = $v
     | (.packages[] | select(.registryType == "oci")).identifier = "ghcr.io/futureweb/intervals-mcp-server:" + $v' \
     server.json > server.json.new && mv server.json.new server.json
   jq --arg v "$NEW" '.version = $v' packaging/mcpb/manifest.json > manifest.json.new \
     && mv manifest.json.new packaging/mcpb/manifest.json
   uv run --no-project python scripts/check_release_metadata.py
   ```

   Also update the version in the install examples of `README.md` and `docs/DOCKERHUB.md`.
3. `CHANGELOG.md`: move the *Unreleased* entries under `## [X.Y.Z] - YYYY-MM-DD`.
4. Open a pull request, wait for CI, merge.
5. Tag the merge commit and push the tag:

   ```sh
   git checkout main && git pull --ff-only
   git tag -a vX.Y.Z -m "vX.Y.Z"
   git push origin vX.Y.Z
   ```

6. Follow the run under Actions → *Release*; approve the `pypi` deployment if the environment
   requires a reviewer.

## Verify

```sh
V=X.Y.Z
# PyPI: page, files, provenance (each file shows its attestation)
open "https://pypi.org/project/futureweb-intervals-mcp/$V/"
uvx "futureweb-intervals-mcp@$V" --version
# GHCR and Docker Hub: same digest, both architectures, server name label
docker buildx imagetools inspect "ghcr.io/futureweb/intervals-mcp-server:$V"
docker buildx imagetools inspect "futurewebat/futureweb-intervals-mcp:$V"
docker run --rm "ghcr.io/futureweb/intervals-mcp-server:$V" futureweb-intervals-mcp --version
docker run --rm "futurewebat/futureweb-intervals-mcp:$V" futureweb-intervals-mcp --version
# MCP Registry
curl -s "https://registry.modelcontextprotocol.io/v0.1/servers/io.github.futureweb%2Fintervals-mcp-server/versions/$V" | jq .
```

On the GitHub release: the sdist, the wheel and `futureweb-intervals-mcp-$V.mcpb` are attached;
the bundle installs in Claude Desktop (*Settings → Extensions → Install Extension…*). After a
final release, `latest` and `X.Y` point to the new image on both registries.

## When a job fails

Use **Re-run failed jobs** on the run. It re-runs only the failed jobs and the jobs after them,
with the sdist, wheel and bundle that CI built in the same run, so PyPI, the GitHub release and
the registry always get identical files. Avoid *Re-run all jobs* once something was published:
it rebuilds the image (new digest under the same tags).

| Failed job | Already published | What to do |
| --- | --- | --- |
| `checks`, `version` | nothing | Fix on `main` (pull request), delete the tag (`git push origin :refs/tags/vX.Y.Z`, `git tag -d vX.Y.Z`) and tag the fixed commit with the same version. |
| `docker` | nothing (tags are pushed only after both architectures built) | Transient: re-run failed jobs. Code problem: as above, re-tag. |
| `pypi` | GHCR image | Usually the trusted publisher (project, workflow `release.yml`, environment) or a rejected approval: fix it, then re-run failed jobs. A partial upload is fine to re-run: PyPI accepts an identical file again and adds the missing one. |
| `github-release` | GHCR, PyPI | Re-run failed jobs; the release is updated and the files are uploaded again. |
| `mcp-registry` | GHCR, PyPI, GitHub release | "not publicly readable": make the GHCR package public, re-run. "not found" on PyPI: wait a few minutes, re-run. "invalid audience": the pinned `mcp-publisher` is too old; bump it in `scripts/install-mcp-publisher.sh` for the next release and publish this version by hand (below). |
| `dockerhub` | everything else | Check the variable, secrets and token scope, re-run failed jobs. A failed overview step does not fail the job. |

**PyPI versions cannot be replaced.** A file name, and with it a version, can never be uploaded
again with different content, not even after deleting it. If a published version is broken, yank
it on PyPI (project → Manage → the release → Yank) and release a new version (e.g. `1.0.0b2`).
The same holds for the MCP Registry: a published version's metadata is immutable; fixes go into
the next version.

Publishing to the registry by hand (an owner of the `futureweb` GitHub organization, from a
checkout of the tag):

```sh
git checkout vX.Y.Z
scripts/install-mcp-publisher.sh .tools
.tools/mcp-publisher login github     # device flow in the browser
.tools/mcp-publisher publish server.json
```

## Notes

- **Registry entry contents.** `server.json` lists packages only (the PyPI package and the GHCR
  image, both stdio): every user runs an own instance with their own Intervals.icu credentials.
  A `remotes` entry (a hosted URL) may only be added if a public multi-user deployment is
  offered deliberately; private single-user deployments are never listed.
  `scripts/check_release_metadata.py` refuses `remotes` until that decision is made.
- **Registry ownership proofs.** The `mcp-name: io.github.futureweb/intervals-mcp-server` comment in
  `README.md` (it becomes the PyPI description) and the `io.modelcontextprotocol.server.name`
  label in the `Dockerfile`. CI checks both, plus the built README (`scripts/check_dist.py`).
- **Versions in the registry.** PEP 440 versions such as `1.0.0b1` are not semantic versions;
  the registry orders them by publication time and always ranks semantic versions (finals such
  as `1.0.0`) above them, so a later beta never replaces a final release as "latest" there.
- **Claude Desktop bundle.** Built in CI by `packaging/mcpb/build.sh` with the official MCPB CLI
  (`@anthropic-ai/mcpb` 2.1.2, dependency tree resolved as of a fixed date), manifest 0.4 with
  the `uv` server type: the bundle carries the sources and `uv.lock`, and Claude Desktop runs
  `uv run --frozen futureweb-intervals-mcp` in it. The bundle is not signed (`mcpb sign` needs a
  code-signing certificate). To build it locally: `packaging/mcpb/build.sh` (Node.js and npm).
  It is attached to the GitHub release only, not listed in the registry.
- **Pinned tools.** Every action is pinned to a commit SHA (Dependabot updates them);
  `mcp-publisher` (version and SHA-256 in `scripts/install-mcp-publisher.sh`), actionlint
  (version and SHA-256 in `ci.yml`), check-jsonschema (version in `ci.yml`) and the MCPB CLI
  (version and date in `packaging/mcpb/build.sh`) are upgraded by hand.
