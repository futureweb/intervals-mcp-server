"""Check that every file that carries the release version or the registry name agrees.

    python scripts/check_release_metadata.py              # consistency of the checkout (CI)
    python scripts/check_release_metadata.py --tag v1.2.3 # and the release tag (release.yml)

Compared with pyproject.toml ``[project].version``:

* ``__version__`` in ``src/intervals_mcp_server/__init__.py``
* ``server.json``: ``version``, the PyPI package version and the tag of the GHCR image
* ``packaging/mcpb/manifest.json``: ``version`` of the Claude Desktop bundle
* the tag (``v`` + version), when given

and the MCP Registry ownership proofs for the name in ``server.json``: ``mcp-name: <name>`` in
README.md (the PyPI description) and the ``io.modelcontextprotocol.server.name`` label in the
Dockerfile. Exits 1 and lists every mismatch. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
GHCR_IMAGE = "ghcr.io/futureweb/intervals-mcp-server"
# PEP 440 subset the release workflow accepts: X.Y.Z, X.Y.ZaN, X.Y.ZbN, X.Y.ZrcN.
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+((a|b|rc)\d+)?$")


def _load_json(path: Path, errors: list[str]) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        errors.append(f"{path.name}: cannot read ({exc})")
        return {}
    if not isinstance(data, dict):
        errors.append(f"{path.name}: expected a JSON object")
        return {}
    return data


def _packages(server: dict[str, Any], registry_type: str, errors: list[str]) -> dict[str, Any]:
    found = [p for p in server.get("packages") or [] if isinstance(p, dict) and p.get("registryType") == registry_type]
    if len(found) != 1:
        errors.append(f"server.json: expected exactly one package with registryType {registry_type!r}, found {len(found)}")
        return {}
    return found[0]


def _server_package_errors(server: dict[str, Any], dist_name: str, version: str) -> list[str]:
    """The PyPI package and the GHCR image in server.json must be this release; no remotes."""
    errors: list[str] = []
    pypi = _packages(server, "pypi", errors)
    if pypi and pypi.get("identifier") != dist_name:
        errors.append(f"server.json: PyPI identifier is {pypi.get('identifier')!r}, pyproject.toml name is {dist_name!r}")
    if pypi and pypi.get("version") != version:
        errors.append(f"server.json PyPI package version is {pypi.get('version')!r}, pyproject.toml has {version!r}")
    oci = _packages(server, "oci", errors)
    if oci and oci.get("identifier") != f"{GHCR_IMAGE}:{version}":
        errors.append(f"server.json: OCI identifier is {oci.get('identifier')!r}, expected '{GHCR_IMAGE}:{version}'")
    if server.get("remotes"):
        # Every user runs an own instance with their own Intervals.icu credentials. A hosted
        # endpoint may only be listed for a deliberate public multi-user deployment (RELEASING.md).
        errors.append("server.json lists 'remotes'; only packages are published (see RELEASING.md before changing this check)")
    return errors


def collect_errors(root: Path = ROOT, tag: str | None = None) -> list[str]:
    """Return every inconsistency between the release metadata files below ``root``."""
    errors: list[str] = []
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    version: str = project["version"]
    dist_name: str = project["name"]
    if not VERSION_RE.match(version):
        errors.append(f"pyproject.toml: version {version!r} is not X.Y.Z, X.Y.ZaN, X.Y.ZbN or X.Y.ZrcN")

    def same(label: str, value: object) -> None:
        if value != version:
            errors.append(f"{label} is {value!r}, pyproject.toml has {version!r}")

    if tag is not None and tag != f"v{version}":
        errors.append(f"tag {tag!r} does not match pyproject.toml version {version!r} (expected 'v{version}')")

    init_text = (root / "src" / "intervals_mcp_server" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__ = "([^"]+)"$', init_text, re.MULTILINE)
    same("__version__ in src/intervals_mcp_server/__init__.py", match.group(1) if match else None)

    server = _load_json(root / "server.json", errors)
    same("server.json version", server.get("version"))
    errors += _server_package_errors(server, dist_name, version)

    manifest = _load_json(root / "packaging" / "mcpb" / "manifest.json", errors)
    same("packaging/mcpb/manifest.json version", manifest.get("version"))

    name = server.get("name")
    if isinstance(name, str) and name:
        readme = (root / "README.md").read_text(encoding="utf-8")
        # The registry needs a boundary after the name (whitespace, an HTML tag or "-->").
        if not re.search(rf"mcp-name: {re.escape(name)}(?=\s|<|-->|$)", readme):
            errors.append(f"README.md: missing 'mcp-name: {name}' (PyPI ownership proof for the MCP Registry)")
        dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
        if not re.search(rf'io\.modelcontextprotocol\.server\.name="{re.escape(name)}"', dockerfile):
            errors.append(f'Dockerfile: missing LABEL io.modelcontextprotocol.server.name="{name}" (OCI ownership proof)')
    else:
        errors.append("server.json: missing 'name'")
    return errors


def main(argv: list[str] | None = None) -> int:
    """Command line entry point; exit status 1 when anything disagrees."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--tag", help="release tag to compare, e.g. v1.0.0b1")
    args = parser.parse_args(argv)
    errors = collect_errors(ROOT, args.tag)
    for error in errors:
        print(f"::error::{error}" if os.environ.get("GITHUB_ACTIONS") else f"error: {error}")
    if not errors:
        print("release metadata consistent" + (f" with tag {args.tag}" if args.tag else ""))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
