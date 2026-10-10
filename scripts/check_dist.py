"""Check the built sdist and wheel before they can be published.

    uv build && python scripts/check_dist.py dist/

* exactly one sdist and one wheel of the version in pyproject.toml;
* every file of the package (modules, ``assets/``, ``guides/``) is in the wheel and the sdist;
* the long description (README.md) is Markdown, carries the MCP Registry ownership proof
  (``mcp-name: ...``) and has only absolute links and images, so the PyPI page renders
  (relative paths and in-page anchors only work on GitHub).

``twine check --strict`` validates the rest of the metadata. Standard library only.
"""

from __future__ import annotations

import argparse
import email
import re
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = "intervals_mcp_server"
# Markdown links/images and HTML src/href attributes.
LINK_RE = re.compile(r"\]\(\s*<?([^)\s>]+)|\b(?:src|href)\s*=\s*[\"']([^\"']+)[\"']")
FENCE_RE = re.compile(r"^(```|~~~).*?^\1", re.MULTILINE | re.DOTALL)
ABSOLUTE = ("https://", "http://", "mailto:")


def package_files(root: Path = ROOT) -> list[str]:
    """Files of the package relative to ``src/`` (what both distributions must contain)."""
    base = root / "src"
    return sorted(
        path.relative_to(base).as_posix()
        for path in (base / PACKAGE).rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo"}
    )


def relative_links(markdown: str) -> list[str]:
    """Link and image targets outside code blocks that are not absolute URLs."""
    text = FENCE_RE.sub("", markdown)
    targets = [a or b for a, b in LINK_RE.findall(text)]
    return [t for t in targets if not t.startswith(ABSOLUTE)]


def description_errors(metadata: str) -> list[str]:
    """Problems of the long description in a wheel's METADATA (what PyPI renders)."""
    message = email.message_from_string(metadata)
    if message.get("Description-Content-Type", "").split(";")[0].strip() != "text/markdown":
        return [f"long description content type is {message.get('Description-Content-Type')!r}, expected text/markdown"]
    description = message.get_payload()
    if not isinstance(description, str) or "mcp-name: " not in description:
        return ["long description has no 'mcp-name: ...' line (MCP Registry ownership proof)"]
    relative = relative_links(description)
    if relative:
        return [f"long description has links/images PyPI cannot resolve: {', '.join(sorted(set(relative)))}"]
    return []


def check(dist: Path, root: Path = ROOT) -> list[str]:
    """Return every problem of the distributions in ``dist`` (empty when they can be published)."""
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    stem = f"{project['name'].replace('-', '_')}-{project['version']}"
    wheel_path, sdist_path = dist / f"{stem}-py3-none-any.whl", dist / f"{stem}.tar.gz"
    found = sorted(p.name for p in [*dist.glob("*.whl"), *dist.glob("*.tar.gz")])
    if found != sorted([wheel_path.name, sdist_path.name]):
        return [f"expected exactly {sdist_path.name} and {wheel_path.name} in {dist}, found {found}"]

    expected = package_files(root)
    errors = [
        f"no files under src/{PACKAGE}/{required} found in the source tree"
        for required in ("assets/", "guides/")
        if not any(f.startswith(f"{PACKAGE}/{required}") for f in expected)
    ]
    with zipfile.ZipFile(wheel_path) as wheel:
        missing = sorted(set(expected) - set(wheel.namelist()))
        errors += description_errors(wheel.read(f"{stem}.dist-info/METADATA").decode("utf-8"))
    if missing:
        errors.append(f"wheel is missing {len(missing)} package file(s): {', '.join(missing[:10])}")
    with tarfile.open(sdist_path) as sdist:
        missing = sorted({f"{stem}/src/{f}" for f in expected} - set(sdist.getnames()))
    if missing:
        errors.append(f"sdist is missing {len(missing)} package file(s): {', '.join(missing[:10])}")
    if not errors:
        print(f"{wheel_path.name} and {sdist_path.name}: {len(expected)} package files each, README renders on PyPI")
    return errors


def main(argv: list[str] | None = None) -> int:
    """Command line entry point; exit status 1 when a check fails."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("dist", nargs="?", default="dist", type=Path, help="directory with the built distributions")
    errors = check(parser.parse_args(argv).dist)
    for error in errors:
        print(f"error: {error}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
