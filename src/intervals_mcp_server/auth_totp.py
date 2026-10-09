"""Time-based one-time passwords (RFC 6238) as an optional second factor for the sign-in.

With ``OAUTH_TOTP_SECRET`` set, the password and API-key sign-ins additionally ask for the
6-digit code of an authenticator app (any app that supports TOTP: Google/Microsoft
Authenticator, 1Password, Bitwarden, ...). The sign-in with Intervals.icu does not need it:
there Intervals.icu itself confirms the athlete.

Create a secret with ``python -m intervals_mcp_server.auth totp-secret`` and add the printed
``otpauth://`` URI (or the secret) to the authenticator app.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
from urllib.parse import quote

__all__ = ["generate_secret", "match_counter", "normalize_secret", "provisioning_uri", "totp"]

STEP_SECONDS = 30
DIGITS = 6
MIN_SECRET_BYTES = 10


def generate_secret() -> str:
    """A new random 160-bit secret in base32 (the format authenticator apps expect)."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _key(secret: str) -> bytes:
    cleaned = secret.replace(" ", "").replace("-", "").upper()
    return base64.b32decode(cleaned + "=" * (-len(cleaned) % 8))


def normalize_secret(secret: str) -> str:
    """Upper-case base32 without spaces; raise ValueError when it is not a usable secret."""
    cleaned = secret.replace(" ", "").replace("-", "").upper().rstrip("=")
    try:
        key = _key(cleaned)
    except (ValueError, TypeError) as exc:  # binascii.Error is a ValueError
        raise ValueError("OAUTH_TOTP_SECRET must be a base32 secret (create one with: python -m intervals_mcp_server.auth totp-secret)") from exc
    if len(key) < MIN_SECRET_BYTES:
        raise ValueError(f"OAUTH_TOTP_SECRET is too short (at least {MIN_SECRET_BYTES} bytes, 16 base32 characters)")
    return cleaned


def _hotp(key: bytes, counter: int, digits: int = DIGITS) -> str:
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return f"{code:0{digits}d}"


def totp(secret: str, at: float, digits: int = DIGITS, step: int = STEP_SECONDS) -> str:
    """The code for time *at* (Unix seconds)."""
    return _hotp(_key(secret), int(at // step), digits)


def match_counter(secret: str, code: str, at: float, window: int = 1, step: int = STEP_SECONDS) -> int | None:
    """Time step of a valid *code* within +/- *window* steps of *at*, or None."""
    candidate = "".join(ch for ch in code if ch.isdigit())
    if len(candidate) != DIGITS:
        return None
    key = _key(secret)
    base = int(at // step)
    for offset in sorted(range(-window, window + 1), key=abs):
        counter = base + offset
        if hmac.compare_digest(_hotp(key, counter), candidate):
            return counter
    return None


def provisioning_uri(secret: str, account: str, issuer: str = "Intervals MCP") -> str:
    """``otpauth://`` URI to add the secret to an authenticator app (as text or QR code)."""
    label = quote(f"{issuer}:{account}")
    return (
        f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}"
        f"&algorithm=SHA1&digits={DIGITS}&period={STEP_SECONDS}"
    )
