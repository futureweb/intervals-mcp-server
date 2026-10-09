"""Optional built-in single-user OAuth 2.1 authorization server.

Remote MCP clients such as ChatGPT or Claude support either "no authentication"
or OAuth 2.1 with dynamic client registration (RFC 7591), PKCE S256 and public
clients (``token_endpoint_auth_method: "none"``).  This module adds the small
authorization server such clients need on top of what the MCP SDK already
serves when ``FastMCP`` is created with ``auth_server_provider`` and ``auth``:

* ``/.well-known/oauth-authorization-server`` (RFC 8414 metadata)
* ``/.well-known/oauth-protected-resource`` (RFC 9728 metadata)
* ``/authorize``, ``/token``, ``/register`` and ``/revoke``
* bearer-token enforcement on the MCP transport routes

The SDK verifies PKCE, redirect URIs and client credentials; this module keeps
the state (clients, codes, tokens), renders the login page under
``/oauth/login`` and checks the single configured password.

Everything is driven by environment variables (all optional):

``MCP_AUTH``                ``none`` (default) or ``oauth``
``MCP_PUBLIC_URL``          public base URL of the server, issuer and resource
``OAUTH_PASSWORD``          plain password, or
``OAUTH_PASSWORD_HASH``     ``pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>``
``OAUTH_USERNAME``          user name shown on the login form (default ``athlete``)
``OAUTH_STATE_FILE``        JSON file for clients and refresh tokens
``OAUTH_ACCESS_TOKEN_TTL``  seconds (default 3600)
``OAUTH_REFRESH_TOKEN_TTL`` seconds (default 30 days)
``OAUTH_LOGIN_RATE_LIMIT``  failed logins per 15 minutes per client IP (default 5)

Access tokens live in memory only; registered clients and refresh tokens are
persisted so a restart does not invalidate a client's registration.  The module
performs no network calls and never logs secrets.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

__all__ = [
    "LOGIN_PATH",
    "SCOPE",
    "OAuthConfig",
    "SingleUserOAuthProvider",
    "auth_status_from_env",
    "hash_password",
    "install_login_routes",
    "oauth_config_from_env",
    "oauth_from_env",
    "verify_password_hash",
]

logger = logging.getLogger(__name__)

SCOPE = "mcp"
LOGIN_PATH = "/oauth/login"
DEFAULT_USERNAME = "athlete"
DEFAULT_STATE_FILE = "./oauth_state.json"
DEFAULT_ACCESS_TOKEN_TTL = 3600
DEFAULT_REFRESH_TOKEN_TTL = 30 * 24 * 3600
DEFAULT_LOGIN_RATE_LIMIT = 5
LOGIN_RATE_LIMIT_WINDOW = 15 * 60
AUTHORIZATION_CODE_TTL = 5 * 60
LOGIN_REQUEST_TTL = 10 * 60
PBKDF2_ITERATIONS = 600_000
MAX_CLIENTS = 50
MAX_PENDING_LOGINS = 500

_STATE_VERSION = 1
_HASH_PREFIX = "pbkdf2_sha256"
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


# --------------------------------------------------------------------------- #
# Password hashing
# --------------------------------------------------------------------------- #


def hash_password(password: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    """Return a ``pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>`` line for *password*."""
    if not password:
        raise ValueError("password must not be empty")
    if iterations < 1:
        raise ValueError("iterations must be a positive integer")
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "$".join(
        (
            _HASH_PREFIX,
            str(iterations),
            base64.b64encode(salt).decode("ascii"),
            base64.b64encode(digest).decode("ascii"),
        )
    )


def _parse_password_hash(encoded: str) -> tuple[int, bytes, bytes]:
    """Split a hash line into (iterations, salt, digest); raise ValueError if malformed."""
    message = (
        "OAUTH_PASSWORD_HASH must look like pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>"
        " (create it with: python -m intervals_mcp_server.auth hash-password)"
    )
    parts = encoded.strip().split("$")
    if len(parts) != 4 or parts[0] != _HASH_PREFIX:
        raise ValueError(message)
    try:
        iterations = int(parts[1])
        salt = base64.b64decode(parts[2], validate=True)
        digest = base64.b64decode(parts[3], validate=True)
    except ValueError as exc:  # binascii.Error is a ValueError
        raise ValueError(message) from exc
    if iterations < 1 or not salt or not digest:
        raise ValueError(message)
    return iterations, salt, digest


def verify_password_hash(password: str, encoded: str) -> bool:
    """Check *password* against a line produced by :func:`hash_password` in constant time."""
    try:
        iterations, salt, expected = _parse_password_hash(encoded)
    except ValueError:
        return False
    candidate = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations, dklen=len(expected)
    )
    return hmac.compare_digest(candidate, expected)


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class OAuthConfig:  # pylint: disable=too-many-instance-attributes
    """Validated OAuth settings read from the environment (a plain settings record)."""

    public_url: str
    username: str = DEFAULT_USERNAME
    password: str | None = None
    password_hash: str | None = None
    state_file: Path = Path(DEFAULT_STATE_FILE)
    access_token_ttl: int = DEFAULT_ACCESS_TOKEN_TTL
    refresh_token_ttl: int = DEFAULT_REFRESH_TOKEN_TTL
    login_rate_limit: int = DEFAULT_LOGIN_RATE_LIMIT
    login_rate_window: int = LOGIN_RATE_LIMIT_WINDOW

    @property
    def issuer(self) -> str:
        """Issuer / public base URL without a trailing slash."""
        return self.public_url.rstrip("/")

    @property
    def login_url(self) -> str:
        """Absolute URL of the login page."""
        return self.issuer + LOGIN_PATH

    def verify_credentials(self, username: str, password: str) -> bool:
        """Compare user name and password in constant time (no short-circuit)."""
        user_ok = hmac.compare_digest(username.encode("utf-8"), self.username.encode("utf-8"))
        if self.password_hash is not None:
            password_ok = verify_password_hash(password, self.password_hash)
        else:
            password_ok = hmac.compare_digest(
                password.encode("utf-8"), (self.password or "").encode("utf-8")
            )
        return bool(user_ok & password_ok)


def _auth_mode(env: Mapping[str, str]) -> str:
    return env.get("MCP_AUTH", "none").strip().lower() or "none"


def _env_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer, got {raw!r}") from exc
    if value < 1:
        raise ValueError(f"{name} must be a positive integer, got {raw!r}")
    return value


def _validate_public_url(url: str) -> None:
    parts = urlsplit(url)
    secure = parts.scheme == "https"
    local = parts.scheme == "http" and parts.hostname in _LOOPBACK_HOSTS
    if not parts.hostname or not (secure or local):
        raise ValueError(
            "MCP_PUBLIC_URL must be an https URL such as https://mcp.example.com "
            "(http is only accepted for localhost / 127.0.0.1)"
        )
    if parts.query or parts.fragment:
        raise ValueError("MCP_PUBLIC_URL must not contain a query string or fragment")


def oauth_config_from_env(environ: Mapping[str, str] | None = None) -> OAuthConfig:
    """Build an :class:`OAuthConfig` from the environment; raise ValueError when incomplete."""
    env = os.environ if environ is None else environ
    public_url = env.get("MCP_PUBLIC_URL", "").strip()
    if not public_url:
        raise ValueError(
            "MCP_AUTH=oauth requires MCP_PUBLIC_URL, the public base URL under which the "
            "server is reachable (e.g. https://mcp.example.com)"
        )
    _validate_public_url(public_url)

    password = env.get("OAUTH_PASSWORD") or None
    password_hash = env.get("OAUTH_PASSWORD_HASH", "").strip() or None
    if password and password_hash:
        raise ValueError("Set either OAUTH_PASSWORD or OAUTH_PASSWORD_HASH, not both")
    if not password and not password_hash:
        raise ValueError(
            "MCP_AUTH=oauth requires OAUTH_PASSWORD or OAUTH_PASSWORD_HASH "
            "(create a hash with: python -m intervals_mcp_server.auth hash-password)"
        )
    if password_hash:
        _parse_password_hash(password_hash)

    return OAuthConfig(
        public_url=public_url,
        username=env.get("OAUTH_USERNAME", "").strip() or DEFAULT_USERNAME,
        password=password,
        password_hash=password_hash,
        state_file=Path(env.get("OAUTH_STATE_FILE", "").strip() or DEFAULT_STATE_FILE),
        access_token_ttl=_env_int(env, "OAUTH_ACCESS_TOKEN_TTL", DEFAULT_ACCESS_TOKEN_TTL),
        refresh_token_ttl=_env_int(env, "OAUTH_REFRESH_TOKEN_TTL", DEFAULT_REFRESH_TOKEN_TTL),
        login_rate_limit=_env_int(env, "OAUTH_LOGIN_RATE_LIMIT", DEFAULT_LOGIN_RATE_LIMIT),
    )


# --------------------------------------------------------------------------- #
# In-memory records
# --------------------------------------------------------------------------- #


@dataclass
class _PendingLogin:
    """An /authorize request waiting for the user to log in."""

    client_id: str
    params: AuthorizationParams
    scopes: list[str]
    expires_at: float


@dataclass
class _TokenRecord:
    """Access or refresh token metadata, keyed by the token digest in the store."""

    client_id: str
    scopes: list[str]
    expires_at: int
    grant_id: str
    resource: str | None = None


@dataclass
class _TokenStore:
    """All token state; only ``refresh`` is persisted."""

    codes: dict[str, AuthorizationCode] = field(default_factory=dict)
    used_codes: dict[str, tuple[float, str]] = field(default_factory=dict)
    access: dict[str, _TokenRecord] = field(default_factory=dict)
    refresh: dict[str, _TokenRecord] = field(default_factory=dict)


class _LoginRateLimiter:
    """Counts failed logins per key (client IP) inside a sliding window."""

    def __init__(self, limit: int, window: int, clock: Callable[[], float]) -> None:
        self._limit = limit
        self._window = window
        self._clock = clock
        self._failures: dict[str, list[float]] = {}

    def _recent(self, key: str) -> list[float]:
        cutoff = self._clock() - self._window
        recent = [stamp for stamp in self._failures.get(key, []) if stamp > cutoff]
        if recent:
            self._failures[key] = recent
        else:
            self._failures.pop(key, None)
        return recent

    def is_blocked(self, key: str) -> bool:
        """True when *key* has reached the failure limit inside the window."""
        return len(self._recent(key)) >= self._limit

    def record_failure(self, key: str) -> None:
        """Remember one failed attempt for *key*."""
        self._failures.setdefault(key, []).append(self._clock())

    def reset(self, key: str) -> None:
        """Forget the failures of *key* (after a successful login)."""
        self._failures.pop(key, None)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _redirect_uri_allowed(uri: str) -> bool:
    parts = urlsplit(uri)
    if parts.scheme == "https":
        return bool(parts.hostname)
    return parts.scheme == "http" and parts.hostname in _LOOPBACK_HOSTS


# --------------------------------------------------------------------------- #
# Provider
# --------------------------------------------------------------------------- #


class SingleUserOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    """OAuth 2.1 provider with one user, dynamic registration and persisted refresh tokens."""

    def __init__(self, config: OAuthConfig, clock: Callable[[], float] = time.time) -> None:
        self._config = config
        self._clock = clock
        self._clients: dict[str, OAuthClientInformationFull] = {}
        self._tokens = _TokenStore()
        self._pending: dict[str, _PendingLogin] = {}
        self._rate_limiter = _LoginRateLimiter(
            config.login_rate_limit, config.login_rate_window, clock
        )
        self._load_state()

    @property
    def config(self) -> OAuthConfig:
        """The configuration this provider was built from."""
        return self._config

    @property
    def login_url(self) -> str:
        """Absolute URL of the login page."""
        return self._config.login_url

    # ----- persistence ---------------------------------------------------- #

    def _load_state(self) -> None:
        path = self._config.state_file
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"OAUTH_STATE_FILE {path} is not readable JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"OAUTH_STATE_FILE {path} has an unexpected format")
        now = self._clock()
        for client_id, raw in data.get("clients", {}).items():
            self._clients[client_id] = OAuthClientInformationFull.model_validate(raw)
        for digest, raw in data.get("refresh_tokens", {}).items():
            record = _TokenRecord(
                client_id=str(raw["client_id"]),
                scopes=[str(scope) for scope in raw.get("scopes", [SCOPE])],
                expires_at=int(raw["expires_at"]),
                grant_id=str(raw.get("grant_id", secrets.token_urlsafe(16))),
            )
            if record.expires_at > now and record.client_id in self._clients:
                self._tokens.refresh[digest] = record
        logger.info(
            "OAuth state loaded: %d client(s), %d refresh token(s)",
            len(self._clients),
            len(self._tokens.refresh),
        )

    def _save_state(self) -> None:
        now = self._clock()
        data = {
            "version": _STATE_VERSION,
            "clients": {
                client_id: client.model_dump(mode="json", exclude_none=True)
                for client_id, client in self._clients.items()
            },
            "refresh_tokens": {
                digest: {
                    "client_id": record.client_id,
                    "scopes": record.scopes,
                    "expires_at": record.expires_at,
                    "grant_id": record.grant_id,
                }
                for digest, record in self._tokens.refresh.items()
                if record.expires_at > now
            },
        }
        path = self._config.state_file
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, path)
        except OSError:
            Path(tmp_name).unlink(missing_ok=True)
            raise

    # ----- helpers -------------------------------------------------------- #

    def _now(self) -> int:
        return int(self._clock())

    def _client_scopes(self, client: OAuthClientInformationFull) -> list[str]:
        return client.scope.split() if client.scope else [SCOPE]

    def _issue_tokens(
        self, client_id: str, scopes: list[str], grant_id: str, resource: str | None
    ) -> OAuthToken:
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        now = self._now()
        self._tokens.access[_digest(access)] = _TokenRecord(
            client_id, scopes, now + self._config.access_token_ttl, grant_id, resource
        )
        self._tokens.refresh[_digest(refresh)] = _TokenRecord(
            client_id, scopes, now + self._config.refresh_token_ttl, grant_id
        )
        self._purge_expired_tokens()
        self._save_state()
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=self._config.access_token_ttl,
            scope=" ".join(scopes),
            refresh_token=refresh,
        )

    def _purge_expired_tokens(self) -> None:
        now = self._clock()
        store = self._tokens
        for bucket in (store.access, store.refresh):
            for digest in [d for d, rec in bucket.items() if rec.expires_at <= now]:
                del bucket[digest]
        for code in [c for c, (expires, _) in store.used_codes.items() if expires <= now]:
            del store.used_codes[code]
        for code in [c for c, rec in store.codes.items() if rec.expires_at <= now]:
            del store.codes[code]

    def _revoke_grant(self, grant_id: str) -> None:
        store = self._tokens
        for digest in [d for d, rec in store.access.items() if rec.grant_id == grant_id]:
            del store.access[digest]
        refresh_digests = [d for d, rec in store.refresh.items() if rec.grant_id == grant_id]
        for digest in refresh_digests:
            del store.refresh[digest]
        if refresh_digests:
            self._save_state()

    def _purge_pending(self) -> None:
        now = self._clock()
        for request_id in [r for r, p in self._pending.items() if p.expires_at <= now]:
            del self._pending[request_id]
        while len(self._pending) >= MAX_PENDING_LOGINS:
            del self._pending[next(iter(self._pending))]

    def _evict_clients(self) -> None:
        """Keep the client table bounded; /register is reachable without credentials."""
        if len(self._clients) < MAX_CLIENTS:
            return
        active = {record.client_id for record in self._tokens.refresh.values()}
        by_age = sorted(self._clients.values(), key=lambda c: c.client_id_issued_at or 0)
        idle = [c for c in by_age if c.client_id not in active]
        victim = (idle or by_age)[0]
        if victim.client_id is not None:
            logger.info("Evicting registered OAuth client %s to make room", victim.client_id)
            del self._clients[victim.client_id]

    # ----- OAuthAuthorizationServerProvider ------------------------------- #

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        """Return the registered client or None."""
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        """Accept a dynamically registered public or confidential client."""
        if client_info.token_endpoint_auth_method not in ("none", "client_secret_post"):
            raise RegistrationError(
                "invalid_client_metadata",
                "token_endpoint_auth_method must be 'none' or 'client_secret_post'",
            )
        uris = [str(uri) for uri in client_info.redirect_uris or []]
        if not uris:
            raise RegistrationError("invalid_redirect_uri", "at least one redirect_uri is required")
        for uri in uris:
            if not _redirect_uri_allowed(uri):
                raise RegistrationError(
                    "invalid_redirect_uri",
                    "redirect_uris must use https (http is only allowed for localhost / 127.0.0.1)",
                )
        if client_info.client_id is None:
            raise RegistrationError("invalid_client_metadata", "client_id is missing")
        self._evict_clients()
        self._clients[client_info.client_id] = client_info
        self._save_state()
        logger.info(
            "Registered OAuth client %s (%s, auth=%s)",
            client_info.client_id,
            client_info.client_name or "unnamed",
            client_info.token_endpoint_auth_method,
        )

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        """Park the authorization request and send the user to the login page."""
        self._purge_pending()
        request_id = secrets.token_urlsafe(32)
        self._pending[request_id] = _PendingLogin(
            client_id=client.client_id or "",
            params=params,
            scopes=params.scopes or self._client_scopes(client),
            expires_at=self._clock() + LOGIN_REQUEST_TTL,
        )
        logger.info("Authorization requested by client %s", client.client_id)
        return f"{self.login_url}?request={request_id}"

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        """Return a live code of *client*; a reused code revokes the tokens it produced."""
        self._purge_expired_tokens()
        used = self._tokens.used_codes.pop(authorization_code, None)
        if used is not None:
            logger.warning("Authorization code reused by client %s; revoking grant", client.client_id)
            self._revoke_grant(used[1])
            return None
        code = self._tokens.codes.get(authorization_code)
        if code is None or code.client_id != client.client_id:
            return None
        return code

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        """Consume the single-use code and issue access + refresh tokens."""
        code = self._tokens.codes.pop(authorization_code.code, None)
        if code is None or code.client_id != client.client_id:
            raise TokenError("invalid_grant", "authorization code is not valid")
        grant_id = secrets.token_urlsafe(16)
        self._tokens.used_codes[code.code] = (self._clock() + AUTHORIZATION_CODE_TTL, grant_id)
        logger.info("Issued tokens to client %s", client.client_id)
        return self._issue_tokens(code.client_id, code.scopes, grant_id, code.resource)

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        """Return the refresh token if it belongs to *client* and has not expired."""
        self._purge_expired_tokens()
        record = self._tokens.refresh.get(_digest(refresh_token))
        if record is None or record.client_id != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=record.client_id,
            scopes=record.scopes,
            expires_at=record.expires_at,
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        """Rotate the refresh token and issue a new access token."""
        record = self._tokens.refresh.pop(_digest(refresh_token.token), None)
        if record is None or record.client_id != client.client_id:
            raise TokenError("invalid_grant", "refresh token is not valid")
        logger.info("Refreshed tokens for client %s", client.client_id)
        return self._issue_tokens(record.client_id, scopes or record.scopes, record.grant_id, None)

    async def load_access_token(self, token: str) -> AccessToken | None:
        """Return the access token metadata used by the bearer middleware."""
        self._purge_expired_tokens()
        record = self._tokens.access.get(_digest(token))
        if record is None:
            return None
        return AccessToken(
            token=token,
            client_id=record.client_id,
            scopes=record.scopes,
            expires_at=record.expires_at,
            resource=record.resource,
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        """Revoke the whole grant (access + refresh token) the given token belongs to."""
        bucket = self._tokens.refresh if isinstance(token, RefreshToken) else self._tokens.access
        record = bucket.get(_digest(token.token))
        if record is None:
            return
        logger.info("Revoked tokens of client %s", record.client_id)
        self._revoke_grant(record.grant_id)

    # ----- login page support --------------------------------------------- #

    def pending_login(self, request_id: str) -> _PendingLogin | None:
        """Return the parked authorization request or None if unknown / expired."""
        self._purge_pending()
        return self._pending.get(request_id) if request_id else None

    def login_blocked(self, key: str) -> bool:
        """True when *key* (client IP) exceeded the failed-login limit."""
        return self._rate_limiter.is_blocked(key)

    def record_login_failure(self, key: str) -> None:
        """Count a failed login for *key*."""
        self._rate_limiter.record_failure(key)

    def verify_credentials(self, username: str, password: str) -> bool:
        """Constant-time check of the submitted credentials."""
        return self._config.verify_credentials(username, password)

    def complete_login(self, request_id: str, login_key: str) -> str:
        """Consume the pending request, mint a code and return the client redirect URL."""
        pending = self._pending.pop(request_id)
        self._rate_limiter.reset(login_key)
        params = pending.params
        code = secrets.token_urlsafe(32)
        self._tokens.codes[code] = AuthorizationCode(
            code=code,
            scopes=pending.scopes,
            expires_at=self._clock() + AUTHORIZATION_CODE_TTL,
            client_id=pending.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
        )
        logger.info("Login succeeded; issuing authorization code to client %s", pending.client_id)
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)


# --------------------------------------------------------------------------- #
# Login page
# --------------------------------------------------------------------------- #

_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Sign in - Intervals MCP</title>
<style>
body{font-family:system-ui,-apple-system,sans-serif;background:#f4f5f7;color:#1f2933;margin:0;
display:flex;min-height:100vh;align-items:center;justify-content:center}
main{background:#fff;padding:2rem;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,.08);
width:min(22rem,90vw)}
h1{font-size:1.25rem;margin:0 0 1rem}
p{font-size:.9rem;color:#52606d;margin:.5rem 0}
label{display:block;font-size:.9rem;margin:.75rem 0 .25rem}
input{width:100%;box-sizing:border-box;padding:.6rem;border:1px solid #cbd2d9;border-radius:6px;
font-size:1rem}
button{margin-top:1.25rem;width:100%;padding:.7rem;border:0;border-radius:6px;background:#2563eb;
color:#fff;font-size:1rem;cursor:pointer}
.error{color:#b91c1c}
</style>
</head>
<body>
<main>
<h1>Sign in to Intervals MCP</h1>
__BODY__
</main>
</body>
</html>
"""

_FORM = """<p>Your MCP client asks for access to your Intervals.icu data.</p>
<form method="post" autocomplete="on">
<input type="hidden" name="request" value="__REQUEST__">
<label for="username">Username</label>
<input id="username" name="username" value="__USERNAME__" autocomplete="username" required>
<label for="password">Password</label>
<input id="password" name="password" type="password" autocomplete="current-password" required autofocus>
__ERROR__
<button type="submit">Sign in</button>
</form>
"""

_INVALID_LINK = (
    '<p class="error">This sign-in link is invalid or has expired.</p>'
    "<p>Go back to your MCP client and connect again.</p>"
)


def _page(body: str, status_code: int, extra_headers: dict[str, str] | None = None) -> Response:
    headers = dict(_SECURITY_HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    return HTMLResponse(_PAGE.replace("__BODY__", body), status_code=status_code, headers=headers)


def _form_page(request_id: str, username: str, error: str | None, status_code: int) -> Response:
    body = (
        _FORM.replace("__REQUEST__", html.escape(request_id, quote=True))
        .replace("__USERNAME__", html.escape(username, quote=True))
        .replace("__ERROR__", f'<p class="error">{html.escape(error)}</p>' if error else "")
    )
    return _page(body, status_code)


def _form_value(form: Mapping[str, Any], key: str) -> str:
    value = form.get(key)
    return value if isinstance(value, str) else ""


def _login_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def install_login_routes(mcp: FastMCP[Any], provider: SingleUserOAuthProvider) -> None:
    """Register GET and POST ``/oauth/login`` on *mcp* (call before building the ASGI app)."""

    async def login_form(request: Request) -> Response:
        request_id = request.query_params.get("request", "")
        if provider.pending_login(request_id) is None:
            return _page(_INVALID_LINK, 400)
        return _form_page(request_id, provider.config.username, None, 200)

    async def login_submit(request: Request) -> Response:
        form = await request.form()
        request_id = _form_value(form, "request")
        if provider.pending_login(request_id) is None:
            return _page(_INVALID_LINK, 400)
        key = _login_key(request)
        if provider.login_blocked(key):
            logger.warning("Login rate limit reached for %s", key)
            return _page(
                '<p class="error">Too many failed sign-in attempts. Please try again later.</p>',
                429,
                {"Retry-After": str(provider.config.login_rate_window)},
            )
        username = _form_value(form, "username")
        password = _form_value(form, "password")
        if not provider.verify_credentials(username, password):
            provider.record_login_failure(key)
            logger.warning("Failed login attempt from %s", key)
            return _form_page(request_id, username or provider.config.username, "Invalid credentials.", 401)
        location = provider.complete_login(request_id, key)
        return RedirectResponse(location, status_code=302, headers=dict(_SECURITY_HEADERS))

    mcp.custom_route(LOGIN_PATH, methods=["GET"], name="oauth_login_form")(login_form)
    mcp.custom_route(LOGIN_PATH, methods=["POST"], name="oauth_login_submit")(login_submit)


# --------------------------------------------------------------------------- #
# FastMCP integration
# --------------------------------------------------------------------------- #


def oauth_from_env(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Return FastMCP keyword arguments for OAuth, or ``{}`` when ``MCP_AUTH`` is not ``oauth``.

    Usage::

        kwargs = oauth_from_env()
        mcp = FastMCP("intervals-icu", **kwargs)
        if kwargs:
            install_login_routes(mcp, kwargs["auth_server_provider"])
    """
    env = os.environ if environ is None else environ
    mode = _auth_mode(env)
    if mode == "none":
        return {}
    if mode != "oauth":
        raise ValueError(f"MCP_AUTH must be 'none' or 'oauth', got {mode!r}")
    config = oauth_config_from_env(env)
    provider = SingleUserOAuthProvider(config)
    logger.info("OAuth enabled; issuer %s, state file %s", config.issuer, config.state_file)
    settings = AuthSettings(
        issuer_url=AnyHttpUrl(config.public_url),
        resource_server_url=AnyHttpUrl(config.public_url),
        client_registration_options=ClientRegistrationOptions(
            enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
        ),
        revocation_options=RevocationOptions(enabled=True),
        required_scopes=[SCOPE],
    )
    return {"auth_server_provider": provider, "auth": settings}


def auth_status_from_env(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Describe the auth configuration for a status tool; never includes the password."""
    env = os.environ if environ is None else environ
    if _auth_mode(env) != "oauth":
        return {"mode": "none", "issuer": None, "state_file": None}
    return {
        "mode": "oauth",
        "issuer": env.get("MCP_PUBLIC_URL", "").strip() or None,
        "state_file": str(Path(env.get("OAUTH_STATE_FILE", "").strip() or DEFAULT_STATE_FILE)),
        "username": env.get("OAUTH_USERNAME", "").strip() or DEFAULT_USERNAME,
        "password_source": (
            "hash"
            if env.get("OAUTH_PASSWORD_HASH", "").strip()
            else "plain" if env.get("OAUTH_PASSWORD") else "missing"
        ),
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    """``python -m intervals_mcp_server.auth hash-password`` prints an OAUTH_PASSWORD_HASH value."""
    parser = argparse.ArgumentParser(
        prog="python -m intervals_mcp_server.auth",
        description="Helpers for the built-in OAuth authorization server.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    hasher = commands.add_parser(
        "hash-password", help="hash a password for OAUTH_PASSWORD_HASH (read from stdin by default)"
    )
    hasher.add_argument("--password", help="password to hash (default: read from stdin)")
    hasher.add_argument(
        "--iterations", type=int, default=PBKDF2_ITERATIONS, help="PBKDF2 iterations"
    )
    args = parser.parse_args(argv)

    password = args.password
    if password is None:
        if sys.stdin.isatty():
            password = getpass.getpass("Password: ")
        else:
            password = sys.stdin.readline().rstrip("\r\n")
    if not password:
        parser.error("password must not be empty")
    print(hash_password(password, args.iterations))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
