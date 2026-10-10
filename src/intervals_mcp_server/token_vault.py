"""Encryption at rest for the Intervals.icu tokens of the multi-user mode.

The OAuth state file keeps, per grant, the athlete's Intervals.icu access token sealed with
AES-256-GCM. The associated data names the grant and the athlete, so a sealed token copied
to another grant or athlete in the file does not open. Keys come from ``OAUTH_TOKEN_KEY``
(base64 of 32 random bytes) or ``OAUTH_TOKEN_KEY_FILE`` (a file holding the key, readable by
the server user only, e.g. mode 0600); several keys may be given comma-separated (or one per
line): the first seals, all of them open, and a token opened with an older key is sealed again
with the first one at its next use, so a key can be rotated (put the new key first, remove the
old one once ``--doctor`` reports no token that only it can open). Create a key with::

    futureweb-intervals-mcp token-key --file /etc/intervals-mcp/token.key

The key never leaves the process and is never logged. A sealed value does not name its key:
opening tries the configured keys in turn (AES-GCM authenticates, so only the right key opens it).
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

__all__ = ["KEY_BYTES", "TokenVault", "VaultError", "generate_key", "vault_from_env", "write_key_file"]

KEY_BYTES = 32
_NONCE_BYTES = 12
_PREFIX = "v1"


class VaultError(Exception):
    """A sealed value could not be opened (wrong or missing key, damaged or moved value)."""


def _b64decode(text: str) -> bytes:
    padded = text.strip() + "=" * (-len(text.strip()) % 4)
    return base64.urlsafe_b64decode(padded.replace("+", "-").replace("/", "_"))


def generate_key() -> str:
    """A new random key in the form ``OAUTH_TOKEN_KEY`` expects."""
    return base64.urlsafe_b64encode(os.urandom(KEY_BYTES)).decode("ascii")


def _parse_keys(raw: str, source: str) -> list[bytes]:
    keys: list[bytes] = []
    for item in raw.replace("\n", ",").split(","):
        if not item.strip():
            continue
        try:
            key = _b64decode(item)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"{source} must contain base64 keys of {KEY_BYTES} bytes (create one with: futureweb-intervals-mcp token-key)") from exc
        if len(key) != KEY_BYTES:
            raise ValueError(
                f"{source} must contain base64 keys of {KEY_BYTES} bytes, got one of {len(key)} bytes "
                "(create one with: futureweb-intervals-mcp token-key)"
            )
        keys.append(key)
    if not keys:
        raise ValueError(f"{source} is empty")
    return keys


class TokenVault:
    """Seal and open small JSON payloads (the Intervals.icu tokens) with AES-256-GCM."""

    def __init__(self, keys: list[bytes]) -> None:
        if not keys:
            raise ValueError("at least one key is required")
        self._keys = [AESGCM(key) for key in keys]

    def __repr__(self) -> str:
        return f"TokenVault({len(self._keys)} key(s))"

    @property
    def key_count(self) -> int:
        """Number of keys that can open values (the first one seals)."""
        return len(self._keys)

    def seal(self, payload: Mapping[str, Any], context: str) -> str:
        """Encrypt *payload* bound to *context* (grant and athlete) with the first key."""
        nonce = os.urandom(_NONCE_BYTES)
        data = json.dumps(dict(payload), separators=(",", ":"), sort_keys=True).encode("utf-8")
        sealed = self._keys[0].encrypt(nonce, data, context.encode("utf-8"))
        return ".".join((_PREFIX, base64.urlsafe_b64encode(nonce + sealed).decode("ascii").rstrip("=")))

    def open(self, value: str, context: str) -> dict[str, Any]:
        """Decrypt a value made by :meth:`seal` with the same *context*; raise VaultError otherwise."""
        return self.open_with_key(value, context)[0]

    def open_with_key(self, value: str, context: str) -> tuple[dict[str, Any], int]:
        """Like :meth:`open`, plus the position of the key that opened it (0 = the current key)."""
        parts = value.split(".") if isinstance(value, str) else []
        if len(parts) != 2 or parts[0] != _PREFIX:
            raise VaultError("not a sealed value of this server")
        try:
            raw = _b64decode(parts[1])
        except (binascii.Error, ValueError) as exc:
            raise VaultError("the sealed value is damaged") from exc
        for index, aead in enumerate(self._keys):
            try:
                data = aead.decrypt(raw[:_NONCE_BYTES], raw[_NONCE_BYTES:], context.encode("utf-8"))
            except InvalidTag:
                continue
            try:
                payload = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise VaultError("unexpected sealed payload") from exc
            if not isinstance(payload, dict):
                raise VaultError("unexpected sealed payload")
            return payload, index
        raise VaultError(
            "no configured key opens it (OAUTH_TOKEN_KEY changed?), or it is damaged or belongs to another grant"
        )


def _read_key_file(path: Path) -> str:
    try:
        info = path.stat()
    except OSError as exc:
        raise ValueError(f"OAUTH_TOKEN_KEY_FILE {path} cannot be read ({exc.strerror or exc})") from exc
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"OAUTH_TOKEN_KEY_FILE {path} is not a regular file")
    if info.st_mode & 0o077:
        raise ValueError(
            f"OAUTH_TOKEN_KEY_FILE {path} is readable by other users (mode {stat.S_IMODE(info.st_mode):o}); "
            f"restrict it to the server user: chmod 600 {path}"
        )
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"OAUTH_TOKEN_KEY_FILE {path} cannot be read ({exc})") from exc


def vault_from_env(environ: Mapping[str, str] | None = None) -> TokenVault | None:
    """The vault configured by ``OAUTH_TOKEN_KEY`` or ``OAUTH_TOKEN_KEY_FILE``; None when neither is set."""
    env = os.environ if environ is None else environ
    inline = env.get("OAUTH_TOKEN_KEY", "").strip()
    path = env.get("OAUTH_TOKEN_KEY_FILE", "").strip()
    if inline and path:
        raise ValueError("Set either OAUTH_TOKEN_KEY or OAUTH_TOKEN_KEY_FILE, not both")
    if inline:
        return TokenVault(_parse_keys(inline, "OAUTH_TOKEN_KEY"))
    if path:
        return TokenVault(_parse_keys(_read_key_file(Path(path)), "OAUTH_TOKEN_KEY_FILE"))
    return None


def write_key_file(path: Path) -> None:
    """Create *path* with a new key, mode 0600; never overwrites an existing file."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(generate_key() + "\n")
