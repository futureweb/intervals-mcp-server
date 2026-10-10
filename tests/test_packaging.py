# pylint: disable=missing-function-docstring
"""OPS-15 / OPS-16 / OPS-17 / OPS-18 / SEC-13: packaging, image and release workflow."""

import tomllib
from pathlib import Path

from intervals_mcp_server import __version__

ROOT = Path(__file__).resolve().parent.parent

# --------------------------------------------------------------------------- #
# OPS-15 / OPS-16 / OPS-17 / OPS-18 / SEC-13: packaging
# --------------------------------------------------------------------------- #


def test_version_matches_pyproject():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert __version__ == project["version"]
    assert project["scripts"]["futureweb-intervals-mcp"] == "intervals_mcp_server.cli:main"


def test_dependency_floors_cover_the_security_updates():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    deps = {d.split(">=")[0].split("[")[0].strip(): d for d in project["dependencies"]}
    assert deps["mcp"].startswith("mcp>=1.30") and "[cli]" not in deps["mcp"]
    for name in ("starlette", "uvicorn", "anyio", "httpx"):
        assert name in deps, f"{name} is imported directly and needs a floor"


def test_dockerfile_uses_the_console_script_and_pinned_images():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert 'CMD ["futureweb-intervals-mcp"]' in dockerfile
    froms = [line for line in dockerfile.splitlines() if line.startswith("FROM ") or "COPY --from=ghcr.io" in line]
    assert froms and all("@sha256:" in line for line in froms)
    assert "HEALTHCHECK" in dockerfile and "OAUTH_STATE_FILE=/data/oauth_state.json" in dockerfile


def test_release_publishes_only_after_the_image():
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    release_job = workflow.split("  github-release:")[1].split("\n  docker:")[0]
    assert "needs: [version, docker]" in release_job
    assert "concurrency:" in workflow
    assert "__version__" in workflow
