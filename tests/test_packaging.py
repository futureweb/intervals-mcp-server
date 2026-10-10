# pylint: disable=missing-function-docstring
"""OPS-15 / OPS-16 / OPS-17 / OPS-18 / SEC-13: packaging, image and release workflow."""

import importlib.util
import json
import re
import shutil
import tomllib
from pathlib import Path
from typing import Any

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


def _job(workflow: str, name: str) -> str:
    """Text of one job of a workflow (from its key to the next job at the same indentation)."""
    match = re.search(rf"^  {re.escape(name)}:\n(.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)", workflow, re.MULTILINE | re.DOTALL)
    assert match, f"job {name} not found"
    return match.group(1)


def _release_workflow() -> str:
    return (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")


def test_release_publishes_only_after_the_image():
    workflow = _release_workflow()
    assert "needs: [version, docker, pypi]" in _job(workflow, "github-release")
    assert "concurrency:" in workflow
    assert "__version__" in workflow


def test_release_publishes_in_order_with_least_privilege():
    workflow = _release_workflow()
    pypi = _job(workflow, "pypi")
    assert "needs: [version, docker]" in pypi and "if:" not in pypi.split("steps:")[0]
    assert "name: pypi" in pypi and "permissions:\n      id-token: write\n" in pypi
    assert "pypa/gh-action-pypi-publish@" in pypi and "attestations: true" in pypi
    registry = _job(workflow, "mcp-registry")
    assert "needs: [version, docker, pypi]" in registry
    assert "id-token: write" in registry and "login github-oidc" in registry
    dockerhub = _job(workflow, "dockerhub")
    assert "if: ${{ vars.DOCKERHUB_IMAGE != '' }}" in dockerhub and "needs: [version, docker]" in dockerhub
    assert "packages: write" not in dockerhub and "imagetools create" in dockerhub
    release = _job(workflow, "github-release")
    assert "mcpb/*.mcpb" in release and "contents: write" in release
    assert "check_release_metadata.py --tag" in _job(workflow, "version")
    # Nothing in the workflow may need write access to the repository except the release job.
    assert workflow.count("contents: write") == 1


def test_every_action_is_pinned_to_a_commit_sha():
    for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        for line in path.read_text(encoding="utf-8").splitlines():
            match = re.search(r"uses:\s*(\S+)", line)
            if match and not match.group(1).startswith("./"):
                assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", match.group(1)), f"{path.name}: {line.strip()}"
                assert re.search(r"# v\d", line), f"{path.name}: pinned action without version comment: {line.strip()}"


def _metadata_checker() -> Any:
    spec = importlib.util.spec_from_file_location("check_release_metadata", ROOT / "scripts" / "check_release_metadata.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_metadata_is_consistent():
    checker = _metadata_checker()
    assert checker.collect_errors(ROOT) == []
    assert checker.collect_errors(ROOT, f"v{__version__}") == []
    assert checker.collect_errors(ROOT, "v0.0.1") == [
        f"tag 'v0.0.1' does not match pyproject.toml version '{__version__}' (expected 'v{__version__}')"
    ]


def test_release_metadata_check_finds_drift(tmp_path):
    for name in ("pyproject.toml", "server.json", "README.md", "Dockerfile"):
        shutil.copy(ROOT / name, tmp_path / name)
    (tmp_path / "src" / "intervals_mcp_server").mkdir(parents=True)
    shutil.copy(ROOT / "src" / "intervals_mcp_server" / "__init__.py", tmp_path / "src" / "intervals_mcp_server")
    (tmp_path / "packaging" / "mcpb").mkdir(parents=True)
    manifest = json.loads((ROOT / "packaging" / "mcpb" / "manifest.json").read_text(encoding="utf-8"))
    manifest["version"] = "9.9.9"
    (tmp_path / "packaging" / "mcpb" / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    server = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    server["packages"][1]["identifier"] = "ghcr.io/futureweb/intervals-mcp-server:latest"
    server["remotes"] = [{"type": "streamable-http", "url": "https://mcp.example.com/mcp"}]
    (tmp_path / "server.json").write_text(json.dumps(server), encoding="utf-8")
    readme = (tmp_path / "README.md").read_text(encoding="utf-8")
    (tmp_path / "README.md").write_text(readme.replace("mcp-name: ", "mcp-name:"), encoding="utf-8")

    errors = _metadata_checker().collect_errors(tmp_path)
    assert any("manifest.json version is '9.9.9'" in e for e in errors)
    assert any("OCI identifier" in e for e in errors)
    assert any("remotes" in e for e in errors)
    assert any(e.startswith("README.md: missing 'mcp-name:") for e in errors)
    assert len(errors) == 4


def test_server_json_lists_the_published_packages():
    server = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert server["name"] == "io.github.futureweb/intervals-mcp-server"
    assert len(server["description"]) <= 100  # registry limit
    assert {p["registryType"] for p in server["packages"]} == {"pypi", "oci"}
    for package in server["packages"]:
        assert package["transport"] == {"type": "stdio"}
        variables = {v["name"]: v for v in package["environmentVariables"]}
        assert variables["API_KEY"]["isSecret"] and variables["API_KEY"]["isRequired"]
        assert variables["ATHLETE_ID"]["isRequired"] and variables["MCP_PERMISSIONS"]["default"] == "read"
    assert server["packages"][0]["identifier"] == project["name"]


def test_mcpb_manifest_runs_the_locked_project():
    manifest = json.loads((ROOT / "packaging" / "mcpb" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == "0.4" and manifest["server"]["type"] == "uv"
    assert (ROOT / manifest["server"]["entry_point"]).is_file() and (ROOT / manifest["icon"]).is_file()
    config = manifest["server"]["mcp_config"]
    assert config["command"] == "uv" and "--frozen" in config["args"] and config["args"][-1] == "futureweb-intervals-mcp"
    assert manifest["user_config"]["api_key"]["sensitive"] is True
    assert manifest["user_config"]["permissions"]["default"] == "read"
    assert config["env"]["API_KEY"] == "${user_config.api_key}"
