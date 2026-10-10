"""Grants of the OAuth state file: who connected, with which credential, and the ``grants`` CLI.

A *grant* is one connection of an MCP client (one consent): its access and refresh tokens
share a ``grant_id``. In the single-user mode the state file stores no grant records
(format version 1, unchanged); every grant uses the server's API key. In the multi-user mode
(``MCP_TENANCY=multi``, format version 2) every new grant has a record::

    "grants": {
      "<grant_id>": {
        "athlete_id": "i123456",          # canonical id of the athlete who signed in
        "kind": "athlete" | "owner",      # own Intervals.icu token, or the owner's API key
        "method": "intervals" | "password" | "apikey",
        "client_id": "...", "created_at": 1760000000, "last_used_at": 1760000000,
        "intervals_scopes": ["ACTIVITY:READ", ...],
        "credential": "v1.<key id>.<AES-GCM sealed token>"   # kind athlete only
      }
    }

Refresh tokens without a grant record come from the single-user mode (``legacy``): they
belong to the owner, because in the single-user mode every connection used the owner's API
key. Version 2 makes older releases refuse the file instead of serving other athletes'
connections with the owner's API key.

``futureweb-intervals-mcp grants list|remove|prune`` works on the state file directly (no
tokens are shown or decrypted); a running server notices the change and drops the removed
connections at its next request. The file is locked (``<state file>.lock``) while it is
changed, by the CLI and by the server.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_STATE_FILE",
    "GRANT_KINDS",
    "SINGLE_USER_STATE_VERSION",
    "STATE_VERSION",
    "Grant",
    "PendingGrant",
    "file_signature",
    "grant_context",
    "grants_main",
    "state_file_lock",
    "state_format_problem",
    "write_state_file",
]

DEFAULT_STATE_FILE = "./oauth_state.json"
# Format 1: single-user mode (unchanged). Format 2: multi-user mode, adds "grants" with the
# sealed Intervals.icu tokens; older releases refuse it instead of serving those grants with
# the owner's API key.
STATE_VERSION = 2
SINGLE_USER_STATE_VERSION = 1
GRANT_KINDS = ("athlete", "owner")
GRANT_METHODS = ("intervals", "password", "apikey")


def state_format_problem(data: Any) -> str | None:
    """Why *data* is not a state file this version can use, or None."""
    if not isinstance(data, dict):
        return "is not a JSON object"
    version = data.get("version", 1)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        return f"has an unknown format version {version!r}"
    if version > STATE_VERSION:
        return f"was written by a newer version of the server (format {version}, this version reads {STATE_VERSION})"
    for key in ("clients", "refresh_tokens"):
        if not isinstance(data.get(key, {}), dict):
            return f"has an unexpected format ('{key}' is not an object)"
    return None


@dataclass
class Grant:  # pylint: disable=too-many-instance-attributes
    """The record of one multi-user grant (persisted; the sealed token is kept out of ``repr()``)."""

    athlete_id: str
    kind: str
    method: str
    client_id: str
    created_at: int
    last_used_at: int
    intervals_scopes: tuple[str, ...] = ()
    sealed: str | None = field(default=None, repr=False)
    # When last_used_at was last written to the file (memory only).
    persisted_use: int = field(default=0, compare=False)

    def to_state(self) -> dict[str, Any]:
        """The JSON object stored in the state file."""
        data: dict[str, Any] = {
            "athlete_id": self.athlete_id,
            "kind": self.kind,
            "method": self.method,
            "client_id": self.client_id,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "intervals_scopes": list(self.intervals_scopes),
        }
        if self.sealed:
            data["credential"] = self.sealed
        return data

    @classmethod
    def from_state(cls, raw: Any) -> Grant:
        """Parse a stored record; raises TypeError / KeyError / ValueError when malformed."""
        if not isinstance(raw, dict):
            raise TypeError("not an object")
        athlete, kind, method, client = raw["athlete_id"], raw["kind"], raw.get("method", "intervals"), raw["client_id"]
        if not all(isinstance(value, str) and value for value in (athlete, kind, method, client)):
            raise TypeError("athlete_id, kind, method and client_id must be non-empty strings")
        if kind not in GRANT_KINDS or method not in GRANT_METHODS:
            raise ValueError(f"unknown kind {kind!r} or method {method!r}")
        created, used = raw.get("created_at", 0), raw.get("last_used_at", raw.get("created_at", 0))
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in (created, used)):
            raise TypeError("created_at and last_used_at must be numbers")
        scopes = raw.get("intervals_scopes", [])
        if not isinstance(scopes, list) or not all(isinstance(scope, str) for scope in scopes):
            raise TypeError("intervals_scopes must be a list of strings")
        sealed = raw.get("credential")
        if sealed is not None and not isinstance(sealed, str):
            raise TypeError("credential must be a string")
        if kind == "athlete" and not sealed:
            raise ValueError("an athlete grant needs its sealed credential")
        created_at, used_at = int(created or 0), int(used or 0)
        return cls(athlete, kind, method, client, created_at, used_at, tuple(scopes), sealed, used_at)


@dataclass
class PendingGrant:
    """A grant between the sign-in and the code exchange (memory only, at most a few minutes)."""

    athlete_id: str
    kind: str
    method: str
    intervals_scopes: tuple[str, ...] = ()
    token: dict[str, Any] | None = field(default=None, repr=False)


def grant_context(grant_id: str, athlete_id: str) -> str:
    """Associated data of a sealed token: it only opens for this grant and athlete."""
    return f"intervals-mcp-token|grant={grant_id}|athlete={athlete_id}"


# --------------------------------------------------------------------------- #
# File helpers shared by the server and the CLI
# --------------------------------------------------------------------------- #


def file_signature(path: Path) -> tuple[int, int, int] | None:
    """(inode, mtime ns, size) of *path*, None when it does not exist: tells whether someone else wrote it."""
    try:
        info = path.stat()
    except (FileNotFoundError, NotADirectoryError):
        return None
    return info.st_ino, info.st_mtime_ns, info.st_size


@contextlib.contextmanager
def state_file_lock(path: Path) -> Iterator[None]:
    """Exclusive advisory lock on ``<path>.lock`` while the state file is read and replaced."""
    try:
        import fcntl  # pylint: disable=import-outside-toplevel
    except ImportError:  # pragma: no cover - not on POSIX: no locking
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path.with_name(path.name + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_state_file(path: Path, data: Mapping[str, Any]) -> None:
    """Atomically replace *path* with *data* (mode 0600, fsynced)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    _fsync_directory(path.parent)


# --------------------------------------------------------------------------- #
# CLI: futureweb-intervals-mcp grants list | remove | prune
# --------------------------------------------------------------------------- #


def _read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "clients": {}, "refresh_tokens": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path} could not be read ({exc})") from exc
    problem = state_format_problem(data)
    if problem:
        raise ValueError(f"{path} {problem}")
    return data


def _when(stamp: Any) -> str:
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or stamp <= 0:
        return "-"
    return datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%d %H:%M")


def _client_label(data: Mapping[str, Any], client_id: str) -> str:
    client = data.get("clients", {}).get(client_id)
    if isinstance(client, dict) and client.get("client_name"):
        return f"{client['client_name']} ({client_id[:12]})"
    if client_id.startswith("https://"):
        return client_id.split("/")[2]
    return client_id[:24]


def grant_rows(data: Mapping[str, Any], now: float | None = None) -> list[dict[str, Any]]:
    """One row per grant of a state file: no tokens, only ids, client, dates and what is stored."""
    now = time.time() if now is None else now
    grants = data.get("grants", {}) if isinstance(data.get("grants", {}), dict) else {}
    expiries: dict[str, int] = {}
    clients: dict[str, str] = {}
    for record in data.get("refresh_tokens", {}).values():
        if isinstance(record, dict) and isinstance(record.get("grant_id"), str):
            expires = record.get("expires_at", 0)
            if isinstance(expires, (int, float)) and expires > now:
                expiries[record["grant_id"]] = max(expiries.get(record["grant_id"], 0), int(expires))
                clients.setdefault(record["grant_id"], str(record.get("client_id", "")))
    rows = []
    for grant_id, raw in grants.items():
        raw = raw if isinstance(raw, dict) else {}
        rows.append(
            {
                "grant_id": grant_id,
                "athlete_id": raw.get("athlete_id", "?"),
                "kind": raw.get("kind", "?"),
                "method": raw.get("method", "?"),
                "client": _client_label(data, str(raw.get("client_id", ""))),
                "created": _when(raw.get("created_at")),
                "last_used": _when(raw.get("last_used_at")),
                "refresh_expires": _when(expiries.get(grant_id)),
                "token_stored": bool(raw.get("credential")),
            }
        )
    for grant_id, expires in expiries.items():
        if grant_id not in grants:
            rows.append(
                {
                    "grant_id": grant_id,
                    "athlete_id": "owner (ATHLETE_ID)",
                    "kind": "legacy",
                    "method": "single-user mode",
                    "client": _client_label(data, clients.get(grant_id, "")),
                    "created": "-",
                    "last_used": "-",
                    "refresh_expires": _when(expires),
                    "token_stored": False,
                }
            )
    return rows


def _remove(data: dict[str, Any], doomed: set[str]) -> int:
    grants = data.get("grants", {}) if isinstance(data.get("grants"), dict) else {}
    for grant_id in doomed:
        grants.pop(grant_id, None)
    refresh = data.get("refresh_tokens", {})
    for digest in [d for d, r in refresh.items() if isinstance(r, dict) and r.get("grant_id") in doomed]:
        del refresh[digest]
    if "grants" in data:
        data["grants"] = grants
    return len(doomed)


def _select(data: Mapping[str, Any], args: argparse.Namespace, now: float) -> set[str]:
    from intervals_mcp_server.tenancy import same_athlete  # pylint: disable=import-outside-toplevel

    rows = grant_rows(data, now)
    grants = data.get("grants", {}) if isinstance(data.get("grants"), dict) else {}
    if args.command == "prune":
        cutoff = now - args.days * 86400
        return {
            gid for gid, raw in grants.items()
            if isinstance(raw, dict) and raw.get("kind") == "athlete" and (raw.get("last_used_at") or 0) < cutoff
        }
    if args.grant:
        return {row["grant_id"] for row in rows if row["grant_id"] == args.grant}
    if args.legacy:
        return {row["grant_id"] for row in rows if row["kind"] == "legacy"}
    return {gid for gid, raw in grants.items() if isinstance(raw, dict) and same_athlete(raw.get("athlete_id", ""), args.athlete)}


def grants_main(argv: list[str] | None = None, environ: Mapping[str, str] | None = None) -> int:  # pylint: disable=too-many-locals
    """``grants list``, ``grants remove <athlete> | --grant ID | --legacy`` and ``grants prune --days N``."""
    env = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(
        prog="futureweb-intervals-mcp grants",
        description="List or remove the connections (OAuth grants) stored in OAUTH_STATE_FILE. Tokens are never shown.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    lister = commands.add_parser("list", help="list connected athletes, clients and dates")
    lister.add_argument("--json", action="store_true", help="print JSON instead of a table")
    remover = commands.add_parser("remove", help="remove grants and the stored Intervals.icu tokens")
    target = remover.add_mutually_exclusive_group(required=True)
    target.add_argument("athlete", nargs="?", help="athlete id: remove every grant of this athlete")
    target.add_argument("--grant", help="remove one grant by its id")
    target.add_argument("--legacy", action="store_true", help="remove the grants from the single-user mode")
    pruner = commands.add_parser("prune", help="remove athlete grants (and their tokens) unused for some days")
    default_days = env.get("OAUTH_TOKEN_RETENTION_DAYS", "").strip()
    pruner.add_argument("--days", type=int, default=int(default_days) if default_days.isdigit() and int(default_days) > 0 else None,
                        help="days without use (default: OAUTH_TOKEN_RETENTION_DAYS)")
    args = parser.parse_args(argv)
    if args.command == "prune" and (args.days is None or args.days < 1):
        parser.error("prune needs --days N (N >= 1) or OAUTH_TOKEN_RETENTION_DAYS")
    path = Path(env.get("OAUTH_STATE_FILE", "").strip() or DEFAULT_STATE_FILE)
    now = time.time()
    try:
        if args.command == "list":
            rows = grant_rows(_read_state(path), now)
            if args.json:
                print(json.dumps(rows, indent=2))
            elif not rows:
                print(f"No connections in {path}.")
            else:
                print(f"{'athlete':<20} {'kind':<8} {'client':<34} {'created':<17} {'last used':<17} {'token':<6} grant id")
                for row in rows:
                    print(
                        f"{row['athlete_id']:<20} {row['kind']:<8} {row['client'][:34]:<34} {row['created']:<17} "
                        f"{row['last_used']:<17} {'yes' if row['token_stored'] else 'no':<6} {row['grant_id']}"
                    )
            return 0
        with state_file_lock(path):
            data = _read_state(path)
            doomed = _select(data, args, now)
            if not doomed:
                print("Nothing to remove.")
                return 1 if args.command == "remove" else 0
            removed = _remove(data, doomed)
            write_state_file(path, data)
        print(f"Removed {removed} grant(s) and their stored tokens from {path}; a running server drops them at its next request.")
        return 0
    except (ValueError, OSError) as exc:
        print(f"grants: {exc}", file=sys.stderr)
        return 2
