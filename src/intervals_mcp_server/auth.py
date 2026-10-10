# pylint: disable=too-many-lines
"""Optional built-in OAuth 2.1 authorization server for remote MCP clients.

Remote MCP clients such as ChatGPT or Claude support either "no authentication" or OAuth
2.1 with PKCE.  Intervals.icu cannot act as their authorization server (no discovery
metadata, no PKCE, no client registration), so this server is the authorization server
for the MCP client and, optionally, an OAuth *client* of Intervals.icu for the sign-in:

``MCP client  <-- OAuth 2.1 (PKCE, CIMD / DCR, RFC 9207 iss) -->  this server``
``this server <-- OAuth 2 ("Sign in with Intervals.icu") ------>  Intervals.icu``

The athlete proves who they are by signing in at Intervals.icu; only the athletes in
``OAUTH_ALLOWED_ATHLETES`` (default: ``ATHLETE_ID``) may connect.  The Intervals.icu token
is used for that identity check only and is never stored or handed to the MCP client;
data access keeps using the configured API key.  A password sign-in is available as an
alternative (or fallback) when no Intervals.icu OAuth app is configured.

Served on top of what the MCP SDK provides when ``FastMCP`` is created with
``auth_server_provider`` and ``auth`` (metadata, ``/authorize``, ``/token``,
``/register``, ``/revoke``, bearer enforcement on the transports):

* consent / sign-in page ``/oauth/login`` and ``/oauth/intervals/callback``
  (:mod:`intervals_mcp_server.auth_pages`)
* Client ID Metadata Documents and ``private_key_jwt`` client assertions
  (:mod:`intervals_mcp_server.auth_clients`)
* ``iss`` on every authorization response (RFC 9207) and the extra metadata fields
  (:mod:`intervals_mcp_server.http_app`)

The permission classes of the server (``MCP_PERMISSIONS``) become OAuth scopes
``intervals:read``, ``intervals:write``, ...; the athlete chooses on the consent page which
of them a connection gets, and tool calls are checked against the token's scopes.

Environment variables (all optional unless noted):

``MCP_AUTH``                       ``none`` (default) or ``oauth``
``MCP_PUBLIC_URL``                 public base URL (required with oauth); issuer and resource
``OAUTH_LOGIN``                    comma-separated ``intervals``, ``password``, ``apikey``
                                   (default: intervals with an Intervals.icu app, else password
                                   when one is set, else apikey: sign in with the API_KEY)
``INTERVALS_OAUTH_CLIENT_ID``      Intervals.icu OAuth app (required for the intervals sign-in)
``INTERVALS_OAUTH_CLIENT_SECRET``  its secret
``INTERVALS_OAUTH_SCOPE``          scope requested at Intervals.icu (default ``ACTIVITY:READ``)
``OAUTH_ALLOWED_ATHLETES``         comma-separated athlete ids (default ``ATHLETE_ID``)
``OAUTH_PASSWORD`` / ``OAUTH_PASSWORD_HASH`` / ``OAUTH_USERNAME``  password sign-in
``OAUTH_TOTP_SECRET``              optional second factor (authenticator app) for the password
                                   and API-key sign-ins
``OAUTH_CLIENT_HOSTS``             hosts (or ``host/path`` document URLs) whose client metadata
                                   documents are accepted (default ``chatgpt.com,claude.ai,claude.com``;
                                   ``none``)
``OAUTH_REDIRECT_HOSTS``           hosts (or ``host/path`` prefixes) allowed as redirect URIs of
                                   dynamically registered clients (same default; ``*`` = any https host)
``OAUTH_DYNAMIC_REGISTRATION``     ``true`` (default) or ``false``
``OAUTH_PRIVATE_KEY_JWT``          advertise private_key_jwt client auth (default ``true``)
``OAUTH_REQUIRE_PRIVATE_KEY_JWT``  refuse token requests without an assertion from clients whose
                                   metadata document declares private_key_jwt (default ``true``)
``OAUTH_STATE_FILE``               JSON file for clients and refresh tokens
``OAUTH_ACCESS_TOKEN_TTL`` / ``OAUTH_REFRESH_TOKEN_TTL`` / ``OAUTH_LOGIN_RATE_LIMIT``
``OAUTH_LOGIN_GLOBAL_RATE_LIMIT``  failed password / API-key sign-ins from all addresses together
                                   per window before those sign-ins pause (default 500; with TOTP a
                                   sign-in with a valid code is never paused by it)
``OAUTH_REFRESH_REUSE_GRACE``      seconds in which a just-rotated refresh token still gets the same
                                   answer again (a client retrying after a lost response; default 120)
``OAUTH_REFRESH_REUSE_REVOKE``     revoke the grant when a rotated refresh token is used after the
                                   grace period (default ``true``; ``false`` only refuses that request)

Access tokens live in memory only; registered clients and refresh tokens are persisted
(as digests) so a restart does not disconnect clients.  The state file is written in a
worker thread, in-memory state only changes once the write succeeded, and a file that
cannot be read is never overwritten.  Secrets are never logged.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import getpass
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

import anyio
import anyio.to_thread
import httpx
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl, ValidationError

from intervals_mcp_server.auth_clients import (
    ClientMetadataResolver,
    Fetcher,
    allowlist_match,
    is_metadata_client_id,
    normalize_allow_entry,
)
from intervals_mcp_server.auth_clients import log_safe as _for_log
from intervals_mcp_server.auth_totp import generate_secret, match_counter, normalize_secret, provisioning_uri

__all__ = [
    "INTERVALS_CALLBACK_PATH",
    "LOGIN_PATH",
    "PERMISSION_SCOPE_PREFIX",
    "SCOPE",
    "LoginError",
    "OAuthConfig",
    "SingleUserOAuthProvider",
    "auth_settings",
    "auth_status_from_env",
    "client_key",
    "granted_classes",
    "hash_password",
    "install_login_routes",
    "normalize_athlete_id",
    "oauth_config_from_env",
    "oauth_from_env",
    "request_client_key",
    "reset_request_client_key",
    "set_request_client_key",
    "verify_password_hash",
]

logger = logging.getLogger(__name__)

SCOPE = "mcp"
PERMISSION_SCOPE_PREFIX = "intervals:"
LOGIN_PATH = "/oauth/login"
INTERVALS_CALLBACK_PATH = "/oauth/intervals/callback"
INTERVALS_AUTHORIZE_URL = "https://intervals.icu/oauth/authorize"
INTERVALS_TOKEN_URL = "https://intervals.icu/api/oauth/token"
DEFAULT_INTERVALS_SCOPE = "ACTIVITY:READ"
DEFAULT_TRUSTED_CLIENT_HOSTS = ("chatgpt.com", "claude.ai", "claude.com")
LOGIN_METHODS = ("intervals", "password", "apikey")
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
# Pending sign-ins per client address (IPv4 address or IPv6 /64): a flood from one address
# only evicts its own requests.
MAX_PENDING_PER_KEY = 20
MAX_CLIENT_NAME_CHARS = 100
MAX_REDIRECT_URIS = 10
MAX_CLIENT_METADATA_BYTES = 8 * 1024
DEFAULT_LOGIN_GLOBAL_RATE_LIMIT = 500
REGISTRATIONS_PER_KEY = 10
REGISTRATION_WINDOW = 3600
DEFAULT_REFRESH_REUSE_GRACE = 120
MAX_ROTATED_REFRESH_TOKENS = 2000
MAX_RATE_LIMIT_KEYS = 10_000

_STATE_VERSION = 1
_HASH_PREFIX = "pbkdf2_sha256"
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})

# Client address of the request being handled (set by a middleware on /authorize and
# /register, whose SDK handlers do not pass the request on to the provider).
_REQUEST_CLIENT_KEY: ContextVar[str] = ContextVar("intervals_mcp_request_client_key", default="unknown")


def client_key(host: str | None) -> str:
    """Rate-limit key of a client address: the IPv4 address or the /64 network of an IPv6 address."""
    if not host:
        return "unknown"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host[:64]
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.IPv6Network((address, 64), strict=False))
    return str(address)


def set_request_client_key(host: str | None) -> Token[str]:
    """Remember the client address of the current request (see :func:`request_client_key`)."""
    return _REQUEST_CLIENT_KEY.set(client_key(host))


def reset_request_client_key(token: Token[str]) -> None:
    """Undo :func:`set_request_client_key` at the end of the request."""
    _REQUEST_CLIENT_KEY.reset(token)


def request_client_key() -> str:
    """Rate-limit key of the current request's client, ``unknown`` outside a request."""
    return _REQUEST_CLIENT_KEY.get()


def _eviction_group(key: str) -> str:
    """The network a rate-limit key is counted in when tables are full: IPv6 by /48."""
    if "/" not in key:
        return key
    try:
        return str(ipaddress.IPv6Network(key, strict=False).supernet(new_prefix=48))
    except ValueError:
        return key


def _one_line(exc: BaseException) -> str:
    return " ".join(str(exc).split()) or type(exc).__name__


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


def normalize_athlete_id(value: Any) -> str:
    """``i219504``, ``I219504`` and ``219504`` all become ``219504``."""
    text = str(value).strip().lower()
    return text[1:] if text.startswith("i") and text[1:].isdigit() else text


def _scope_for(permission: str) -> str:
    return PERMISSION_SCOPE_PREFIX + permission


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
    login_global_rate_limit: int = DEFAULT_LOGIN_GLOBAL_RATE_LIMIT
    refresh_reuse_grace: int = DEFAULT_REFRESH_REUSE_GRACE
    refresh_reuse_revoke: bool = True
    require_private_key_jwt: bool = True
    login_methods: tuple[str, ...] = ("password",)
    intervals_client_id: str | None = None
    intervals_client_secret: str | None = None
    intervals_scope: str = DEFAULT_INTERVALS_SCOPE
    allowed_athletes: frozenset[str] = frozenset()
    client_hosts: frozenset[str] = frozenset(DEFAULT_TRUSTED_CLIENT_HOSTS)
    redirect_hosts: frozenset[str] | None = frozenset(DEFAULT_TRUSTED_CLIENT_HOSTS)
    dynamic_registration: bool = True
    private_key_jwt: bool = True
    permission_classes: tuple[str, ...] = ("read",)
    # Kept out of repr(); compared in constant time. It is the key the server already holds.
    api_key: str | None = field(default=None, repr=False)
    totp_secret: str | None = field(default=None, repr=False)

    @property
    def issuer(self) -> str:
        """Public base URL without a trailing slash (used to build absolute URLs)."""
        return self.public_url.rstrip("/")

    @property
    def metadata_issuer(self) -> str:
        """The issuer exactly as published in the metadata (RFC 9207 ``iss`` value)."""
        return str(AnyHttpUrl(self.public_url))

    @property
    def login_url(self) -> str:
        """Absolute URL of the consent / sign-in page."""
        return self.issuer + LOGIN_PATH

    @property
    def intervals_redirect_uri(self) -> str:
        """Redirect URI to register in the Intervals.icu OAuth app."""
        return self.issuer + INTERVALS_CALLBACK_PATH

    @property
    def scopes_supported(self) -> list[str]:
        """``mcp`` plus one scope per enabled permission class."""
        return [SCOPE] + [_scope_for(p) for p in self.permission_classes]

    def verify_credentials(self, username: str, password: str) -> bool:
        """Compare user name and password in constant time (no short-circuit)."""
        user_ok = hmac.compare_digest(username.encode("utf-8"), self.username.encode("utf-8"))
        if self.password_hash is not None:
            password_ok = verify_password_hash(password, self.password_hash)
        else:
            password_ok = hmac.compare_digest(
                password.encode("utf-8"), (self.password or "").encode("utf-8")
            )
        return bool(user_ok & password_ok) and "password" in self.login_methods

    def verify_api_key(self, api_key: str) -> bool:
        """Constant-time check of an Intervals.icu API key against the configured one."""
        if "apikey" not in self.login_methods or not self.api_key:
            return False
        return hmac.compare_digest(api_key.strip().encode("utf-8"), self.api_key.encode("utf-8"))

    def same_origin(self, url: str) -> bool:
        """True when *url* points at this server (scheme, host and port of the issuer)."""
        mine, other = _origin(self.public_url), _origin(url)
        return other is not None and mine == other


_DEFAULT_PORTS = {"https": 443, "http": 80}


def _origin(url: str) -> tuple[str, str, int | None] | None:
    """(scheme, host, port) with the default port filled in; None for an unparsable URL."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    return scheme, (parts.hostname or "").lower(), port or _DEFAULT_PORTS.get(scheme)


def _auth_mode(env: Mapping[str, str]) -> str:
    return env.get("MCP_AUTH", "none").strip().lower() or "none"


def _env_int(env: Mapping[str, str], name: str, default: int, minimum: int = 1) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    kind = "a positive integer" if minimum == 1 else f"an integer >= {minimum}"
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be {kind}, got {raw!r}") from exc
    if value < minimum:
        raise ValueError(f"{name} must be {kind}, got {raw!r}")
    return value


def _env_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise ValueError(f"{name} must be true or false, got {raw!r}")


def _env_hosts(env: Mapping[str, str], name: str, allow_any: bool) -> frozenset[str] | None:
    """Allowlist of ``host`` or ``host/path`` entries; None = any host (only with *allow_any*)."""
    raw = env.get(name)
    if raw is None or not raw.strip():
        return frozenset(DEFAULT_TRUSTED_CLIENT_HOSTS)
    value = raw.strip()
    if value.lower() == "none":
        return frozenset()
    if value == "*":
        if not allow_any:
            raise ValueError(f"{name} does not accept '*'; list the hosts explicitly")
        return None
    try:
        return frozenset(normalize_allow_entry(h) for h in value.split(",") if h.strip())
    except ValueError as exc:
        raise ValueError(f"{name}: {exc}") from exc


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


def _login_methods(env: Mapping[str, str]) -> tuple[str, ...]:
    raw = env.get("OAUTH_LOGIN", "").strip().lower()
    if not raw:
        if env.get("INTERVALS_OAUTH_CLIENT_ID", "").strip():
            return ("intervals",)
        if env.get("OAUTH_PASSWORD") or env.get("OAUTH_PASSWORD_HASH", "").strip():
            return ("password",)
        return ("apikey",)
    methods = tuple(dict.fromkeys(m.strip() for m in raw.split(",") if m.strip()))
    unknown = [m for m in methods if m not in LOGIN_METHODS]
    if unknown or not methods:
        raise ValueError(f"OAUTH_LOGIN must be a comma-separated subset of {', '.join(LOGIN_METHODS)}")
    return methods


def oauth_config_from_env(environ: Mapping[str, str] | None = None) -> OAuthConfig:  # pylint: disable=too-many-locals
    """Build an :class:`OAuthConfig` from the environment; raise ValueError when incomplete."""
    env = os.environ if environ is None else environ
    public_url = env.get("MCP_PUBLIC_URL", "").strip()
    if not public_url:
        raise ValueError(
            "MCP_AUTH=oauth requires MCP_PUBLIC_URL, the public base URL under which the "
            "server is reachable (e.g. https://mcp.example.com)"
        )
    _validate_public_url(public_url)
    methods = _login_methods(env)

    password = env.get("OAUTH_PASSWORD") or None
    password_hash = env.get("OAUTH_PASSWORD_HASH", "").strip() or None
    if password and password_hash:
        raise ValueError("Set either OAUTH_PASSWORD or OAUTH_PASSWORD_HASH, not both")
    if "password" in methods and not password and not password_hash:
        raise ValueError(
            "OAUTH_LOGIN=password requires OAUTH_PASSWORD or OAUTH_PASSWORD_HASH "
            "(create a hash with: python -m intervals_mcp_server.auth hash-password)"
        )
    if password_hash:
        _parse_password_hash(password_hash)
    api_key = env.get("API_KEY", "").strip()
    if "apikey" in methods and not api_key:
        raise ValueError(
            "MCP_AUTH=oauth needs a sign-in method: API_KEY for the sign-in with the Intervals.icu API key "
            "(the default), OAUTH_PASSWORD or OAUTH_PASSWORD_HASH for a password, or an Intervals.icu OAuth app "
            "(OAUTH_LOGIN=intervals)"
        )
    totp_raw = env.get("OAUTH_TOTP_SECRET", "").strip()
    totp_secret = normalize_secret(totp_raw) if totp_raw else None

    client_id = env.get("INTERVALS_OAUTH_CLIENT_ID", "").strip() or None
    client_secret = env.get("INTERVALS_OAUTH_CLIENT_SECRET", "").strip() or None
    athletes_raw = env.get("OAUTH_ALLOWED_ATHLETES", "").strip() or env.get("ATHLETE_ID", "").strip()
    athletes = frozenset(normalize_athlete_id(a) for a in athletes_raw.split(",") if a.strip())
    if "intervals" in methods:
        if not client_id or not client_secret:
            raise ValueError(
                "OAUTH_LOGIN=intervals requires INTERVALS_OAUTH_CLIENT_ID and INTERVALS_OAUTH_CLIENT_SECRET "
                "(apply for an app at https://intervals.icu/oauth/apply)"
            )
        if not athletes:
            raise ValueError("OAUTH_LOGIN=intervals requires OAUTH_ALLOWED_ATHLETES or ATHLETE_ID")

    # Imported here: the config module loads .env on import, which must not happen
    # merely because the auth module was imported (tests, --doctor).
    from intervals_mcp_server.config import PERMISSION_CLASSES, parse_permissions  # pylint: disable=import-outside-toplevel

    permissions = parse_permissions(env.get("MCP_PERMISSIONS", "read"))
    client_hosts = _env_hosts(env, "OAUTH_CLIENT_HOSTS", allow_any=False)
    return OAuthConfig(
        public_url=public_url,
        username=env.get("OAUTH_USERNAME", "").strip() or DEFAULT_USERNAME,
        password=password,
        password_hash=password_hash,
        state_file=Path(env.get("OAUTH_STATE_FILE", "").strip() or DEFAULT_STATE_FILE),
        access_token_ttl=_env_int(env, "OAUTH_ACCESS_TOKEN_TTL", DEFAULT_ACCESS_TOKEN_TTL),
        refresh_token_ttl=_env_int(env, "OAUTH_REFRESH_TOKEN_TTL", DEFAULT_REFRESH_TOKEN_TTL),
        login_rate_limit=_env_int(env, "OAUTH_LOGIN_RATE_LIMIT", DEFAULT_LOGIN_RATE_LIMIT),
        login_global_rate_limit=_env_int(env, "OAUTH_LOGIN_GLOBAL_RATE_LIMIT", DEFAULT_LOGIN_GLOBAL_RATE_LIMIT),
        refresh_reuse_grace=_env_int(env, "OAUTH_REFRESH_REUSE_GRACE", DEFAULT_REFRESH_REUSE_GRACE, minimum=0),
        refresh_reuse_revoke=_env_bool(env, "OAUTH_REFRESH_REUSE_REVOKE", True),
        require_private_key_jwt=_env_bool(env, "OAUTH_REQUIRE_PRIVATE_KEY_JWT", True),
        login_methods=methods,
        intervals_client_id=client_id,
        intervals_client_secret=client_secret,
        intervals_scope=env.get("INTERVALS_OAUTH_SCOPE", "").strip() or DEFAULT_INTERVALS_SCOPE,
        allowed_athletes=athletes,
        client_hosts=client_hosts if client_hosts is not None else frozenset(),
        redirect_hosts=_env_hosts(env, "OAUTH_REDIRECT_HOSTS", allow_any=True),
        dynamic_registration=_env_bool(env, "OAUTH_DYNAMIC_REGISTRATION", True),
        private_key_jwt=_env_bool(env, "OAUTH_PRIVATE_KEY_JWT", True),
        permission_classes=tuple(p for p in PERMISSION_CLASSES if p in permissions),
        api_key=api_key if "apikey" in methods else None,
        totp_secret=totp_secret,
    )


# --------------------------------------------------------------------------- #
# In-memory records
# --------------------------------------------------------------------------- #


@dataclass
class _PendingLogin:  # pylint: disable=too-many-instance-attributes
    """An /authorize request waiting for the athlete's consent and sign-in."""

    client_id: str
    params: AuthorizationParams
    scopes: list[str]
    expires_at: float
    client_name: str = ""
    client_verified: bool = False
    redirect_host: str = ""
    offered: tuple[str, ...] = ("read",)
    key: str = "unknown"


@dataclass
class _UpstreamLogin:
    """A sign-in at Intervals.icu in progress (keyed by the OAuth ``state`` sent there)."""

    request_id: str
    granted: tuple[str, ...]
    browser_digest: str
    expires_at: float
    key: str = "unknown"


@dataclass
class _TokenRecord:
    """Access or refresh token metadata, keyed by the token digest in the store."""

    client_id: str
    scopes: list[str]
    expires_at: int
    grant_id: str
    resource: str | None = None


@dataclass
class _RotatedToken:
    """A refresh token that was exchanged.

    For the grace period the answer of the exchange (``response``, kept in memory only and
    dropped when the grace period ends) is returned again to a client that presents the old
    token once more: a retry after a lost response gets the same tokens, so one grant never
    forks into several live refresh-token chains.
    """

    record: _TokenRecord
    rotated_at: float
    successor: str = ""
    response: OAuthToken | None = None


class _AlreadyRotated(Exception):
    """The refresh token was exchanged by a concurrent request while this one waited."""


class _Refused(Exception):
    """A token request refused inside :meth:`SingleUserOAuthProvider._state_change`.

    The SDK's TokenError is a frozen dataclass and cannot pass through an async context
    manager (contextlib sets ``__traceback__`` on it); callers turn this into TokenError.
    """


@dataclass
class _TokenStore:
    """All token state; only ``refresh`` is persisted."""

    codes: dict[str, AuthorizationCode] = field(default_factory=dict)
    used_codes: dict[str, tuple[float, str]] = field(default_factory=dict)
    access: dict[str, _TokenRecord] = field(default_factory=dict)
    refresh: dict[str, _TokenRecord] = field(default_factory=dict)
    # Digests of exchanged refresh tokens (in memory): reuse after the grace period
    # revokes the grant (RFC 9700 4.14.2).
    rotated: dict[str, _RotatedToken] = field(default_factory=dict)


class LoginError(Exception):
    """A sign-in could not be completed; ``status`` is the HTTP status for the page."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class _LoginRateLimiter:
    """Counts events (failed logins, registrations) per key inside a sliding window.

    The table is bounded: expired keys are swept and, above ``MAX_RATE_LIMIT_KEYS``
    keys, the oldest ones are dropped (an attacker rotating addresses cannot grow it).
    """

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
        """True when *key* has reached the limit inside the window."""
        return len(self._recent(key)) >= self._limit

    def record_failure(self, key: str) -> None:
        """Remember one event for *key*."""
        self._failures.setdefault(key, []).append(self._clock())
        if len(self._failures) > MAX_RATE_LIMIT_KEYS:
            self._sweep()

    def _sweep(self) -> None:
        for key in list(self._failures):
            self._recent(key)
        while len(self._failures) > MAX_RATE_LIMIT_KEYS:
            del self._failures[next(iter(self._failures))]

    def reset(self, key: str) -> None:
        """Forget the events of *key* (after a successful login)."""
        self._failures.pop(key, None)

    def forget_last(self, key: str) -> None:
        """Take back the latest event of *key*."""
        stamps = self._failures.get(key)
        if stamps:
            stamps.pop()
            if not stamps:
                del self._failures[key]

    def __len__(self) -> int:
        return len(self._failures)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _check_registration_size(client_info: OAuthClientInformationFull, redirect_count: int) -> None:
    """Bound what an unauthenticated /register request can store and log."""
    name = client_info.client_name or ""
    if len(name) > MAX_CLIENT_NAME_CHARS or not name.isprintable():
        raise RegistrationError(
            "invalid_client_metadata", f"client_name must be at most {MAX_CLIENT_NAME_CHARS} printable characters"
        )
    if redirect_count > MAX_REDIRECT_URIS:
        raise RegistrationError("invalid_redirect_uri", f"at most {MAX_REDIRECT_URIS} redirect_uris are allowed")
    if len(client_info.model_dump_json(exclude_none=True)) > MAX_CLIENT_METADATA_BYTES:
        raise RegistrationError(
            "invalid_client_metadata", f"client metadata must not exceed {MAX_CLIENT_METADATA_BYTES} bytes"
        )


def _redirect_uri_allowed(uri: str, hosts: frozenset[str] | None = None) -> bool:
    """https without a query on an allowed host or path prefix (``None`` = any host), or http on loopback."""
    parts = urlsplit(uri)
    if parts.scheme == "http":
        return parts.hostname in _LOOPBACK_HOSTS
    if parts.scheme != "https" or not parts.hostname or parts.fragment or parts.query or "@" in parts.netloc:
        return False
    return hosts is None or allowlist_match(parts.hostname, parts.path, hosts)


def _record_from_state(raw: Any) -> _TokenRecord:
    """A persisted refresh-token record; raises ValueError / TypeError / KeyError when malformed."""
    if not isinstance(raw, dict):
        raise TypeError("not an object")
    client_id = raw["client_id"]
    scopes = raw.get("scopes", [SCOPE])
    expires_at = raw["expires_at"]
    grant_id = raw.get("grant_id") or secrets.token_urlsafe(16)
    resource = raw.get("resource")
    if not isinstance(client_id, str) or not isinstance(grant_id, str):
        raise TypeError("client_id and grant_id must be strings")
    if not isinstance(scopes, list) or not all(isinstance(scope, str) for scope in scopes):
        raise TypeError("scopes must be a list of strings")
    if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
        raise TypeError("expires_at must be a number")
    if resource is not None and not isinstance(resource, str):
        raise TypeError("resource must be a string")
    return _TokenRecord(client_id, list(scopes), int(expires_at), grant_id, resource)


def _state_format_problem(data: Any) -> str | None:
    """Why *data* is not a state file this version can use, or None."""
    if not isinstance(data, dict):
        return "is not a JSON object"
    version = data.get("version", 1)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        return f"has an unknown format version {version!r}"
    if version > _STATE_VERSION:
        return f"was written by a newer version of the server (format {version}, this version reads {_STATE_VERSION})"
    for key in ("clients", "refresh_tokens"):
        if not isinstance(data.get(key, {}), dict):
            return f"has an unexpected format ('{key}' is not an object)"
    return None


def _fsync_directory(path: Path) -> None:
    """Make a rename in *path* durable; not every file system supports it."""
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


def granted_classes(scopes: list[str] | None) -> set[str] | None:
    """Permission classes granted by token *scopes*; None = no token, so no per-token restriction.

    A token without any ``intervals:*`` scope only gets ``read``: every token this server
    issues names its classes, so a bare ``mcp`` token can only come from narrowing scopes
    and must never widen the grant to the server-wide permissions.
    """
    if scopes is None:
        return None
    classes = {s[len(PERMISSION_SCOPE_PREFIX):] for s in scopes if s.startswith(PERMISSION_SCOPE_PREFIX)}
    return classes or {"read"}


def _refresh_scopes(granted: list[str], requested: list[str] | None) -> list[str]:
    """Scopes for a refreshed token: the requested subset, but never fewer permission scopes than ``read``.

    The SDK accepts any subset of the refresh token's scopes. Dropping every ``intervals:*``
    scope keeps the grant's own permission scopes instead of producing a bare ``mcp`` token,
    and ``mcp`` itself (required by the transports) always stays.
    """
    if not requested:
        return list(granted)
    narrowed = [scope for scope in granted if scope in requested or scope == SCOPE]
    if not any(scope.startswith(PERMISSION_SCOPE_PREFIX) for scope in narrowed):
        narrowed += [scope for scope in granted if scope.startswith(PERMISSION_SCOPE_PREFIX)]
    return narrowed or list(granted)


IntervalsTokenExchange = Callable[[str], Awaitable[dict[str, Any]]]


# --------------------------------------------------------------------------- #
# Provider
# --------------------------------------------------------------------------- #


class SingleUserOAuthProvider(  # pylint: disable=too-many-instance-attributes,too-many-public-methods
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    """OAuth 2.1 provider for one athlete: consent, Intervals.icu or password sign-in, persisted refresh tokens."""

    def __init__(
        self,
        config: OAuthConfig,
        clock: Callable[[], float] = time.time,
        fetch: Fetcher | None = None,
        intervals_exchange: IntervalsTokenExchange | None = None,
    ) -> None:
        self._config = config
        self._clock = clock
        self._clients: dict[str, OAuthClientInformationFull] = {}
        self._tokens = _TokenStore()
        self._pending: dict[str, _PendingLogin] = {}
        self._upstream: dict[str, _UpstreamLogin] = {}
        self._rate_limiter = _LoginRateLimiter(config.login_rate_limit, config.login_rate_window, clock)
        self._global_failures = _LoginRateLimiter(config.login_global_rate_limit, config.login_rate_window, clock)
        self._registrations = _LoginRateLimiter(REGISTRATIONS_PER_KEY, REGISTRATION_WINDOW, clock)
        self.metadata_clients = ClientMetadataResolver(
            config.client_hosts, " ".join(config.scopes_supported), fetch=fetch, clock=clock
        )
        self._intervals_exchange = intervals_exchange or self._exchange_intervals_code
        self._totp_last_counter = -1
        # Key for the consent form tokens (CSRF); a restart also drops the pending requests.
        self._form_key = secrets.token_bytes(32)
        # Entries of the state file this version could not read: written back unchanged.
        self._raw_clients: dict[str, Any] = {}
        self._raw_refresh: dict[str, Any] = {}
        self._state_lock = anyio.Lock()
        self._load_state()
        self._check_state_writable()

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
        """Read the state file; anything unusable raises ValueError and the file stays untouched."""
        path = self._config.state_file
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(
                f"OAUTH_STATE_FILE {path} could not be read ({_one_line(exc)}); the file was left unchanged. "
                "Repair it, restore a backup or move it away (all clients then have to connect again)"
            ) from exc
        problem = _state_format_problem(data)
        if problem:
            raise ValueError(
                f"OAUTH_STATE_FILE {path} {problem}; the file was left unchanged. "
                "Repair it, restore a backup or move it away (all clients then have to connect again)"
            )
        now = self._clock()
        for client_id, raw in data.get("clients", {}).items():
            try:
                self._clients[client_id] = OAuthClientInformationFull.model_validate(raw)
            except ValidationError as exc:
                # Kept verbatim (a stricter SDK must not silently delete registrations).
                logger.warning(
                    "OAuth state: client %s could not be read (%d validation error(s)); kept in the file, not usable",
                    _for_log(client_id),
                    exc.error_count(),
                )
                self._raw_clients[client_id] = raw
        for digest, raw in data.get("refresh_tokens", {}).items():
            try:
                record = _record_from_state(raw)
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning("OAuth state: a refresh token entry could not be read (%s); kept in the file", _one_line(exc))
                self._raw_refresh[digest] = raw
                continue
            if record.expires_at <= now:
                continue
            if record.client_id in self._raw_clients:
                self._raw_refresh[digest] = raw
                continue
            known = record.client_id in self._clients or is_metadata_client_id(record.client_id)
            if known:
                self._tokens.refresh[digest] = record
                if is_metadata_client_id(record.client_id):
                    # Its document is fetched even while unknown client ids use up the budget.
                    self.metadata_clients.remember(record.client_id)
        logger.info(
            "OAuth state loaded: %d client(s), %d refresh token(s)",
            len(self._clients),
            len(self._tokens.refresh),
        )

    def _check_state_writable(self) -> None:
        """Fail at startup, not at the first sign-in, when the state file cannot be written."""
        directory = self._config.state_file.parent
        try:
            directory.mkdir(parents=True, exist_ok=True)
            fd, probe = tempfile.mkstemp(prefix=self._config.state_file.name + ".", suffix=".probe", dir=directory)
            os.close(fd)
            os.unlink(probe)
        except OSError as exc:
            raise ValueError(
                f"OAUTH_STATE_FILE directory {directory} is not writable ({exc.strerror or _one_line(exc)})"
            ) from exc

    def _state_snapshot(self) -> dict[str, Any]:
        now = self._clock()
        clients: dict[str, Any] = dict(self._raw_clients)
        clients.update(
            {client_id: client.model_dump(mode="json", exclude_none=True) for client_id, client in self._clients.items()}
        )
        refresh: dict[str, Any] = dict(self._raw_refresh)
        refresh.update(
            {
                digest: {
                    "client_id": record.client_id,
                    "scopes": record.scopes,
                    "expires_at": record.expires_at,
                    "grant_id": record.grant_id,
                    **({"resource": record.resource} if record.resource else {}),
                }
                for digest, record in self._tokens.refresh.items()
                if record.expires_at > now
            }
        )
        return {"version": _STATE_VERSION, "clients": clients, "refresh_tokens": refresh}

    def _write_state(self, data: dict[str, Any]) -> None:
        """Atomically replace the state file (runs in a worker thread)."""
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
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        _fsync_directory(path.parent)

    def _tables(self) -> list[dict[str, Any]]:
        store = self._tokens
        return [self._clients, store.codes, store.used_codes, store.access, store.refresh, store.rotated]

    def _changes_since(self, before: list[dict[str, Any]]) -> list[tuple[list[str], dict[str, Any]]]:
        """Per table: keys added or replaced, and the old entries removed or replaced."""
        changes = []
        for old, table in zip(before, self._tables(), strict=True):
            added = [key for key, value in table.items() if old.get(key) is not value]
            removed = {key: value for key, value in old.items() if table.get(key) is not value}
            changes.append((added, removed))
        return changes

    def _undo(self, changes: list[tuple[list[str], dict[str, Any]]]) -> None:
        for (added, removed), table in zip(changes, self._tables(), strict=True):
            for key in added:
                table.pop(key, None)
            table.update(removed)

    @contextlib.asynccontextmanager
    async def _state_change(self) -> AsyncIterator[None]:
        """Serialize a persisted change: apply it, write the file off the event loop, or undo it.

        The body changes the in-memory tables; if the body raises or the file cannot be
        written, exactly the body's changes are undone (concurrent requests keep theirs),
        so a client can simply retry: a refresh token or code is never lost to a full disk.
        """
        async with self._state_lock:
            before = [dict(table) for table in self._tables()]
            try:
                yield
            except Exception:
                self._undo(self._changes_since(before))
                raise
            changes = self._changes_since(before)
            try:
                await anyio.to_thread.run_sync(self._write_state, self._state_snapshot())
            except Exception as exc:
                self._undo(changes)
                if isinstance(exc, OSError):
                    logger.error("Could not write OAUTH_STATE_FILE %s: %s", self._config.state_file, _one_line(exc))
                raise

    # ----- helpers -------------------------------------------------------- #

    def _now(self) -> int:
        return int(self._clock())

    def _client_scopes(self, client: OAuthClientInformationFull) -> list[str]:
        return client.scope.split() if client.scope else [SCOPE]

    def _issue_tokens(
        self, client_id: str, scopes: list[str], grant_id: str, resource: str | None
    ) -> OAuthToken:
        """Add a new access / refresh token pair (call inside :meth:`_state_change`)."""
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        now = self._now()
        self._tokens.access[_digest(access)] = _TokenRecord(
            client_id, scopes, now + self._config.access_token_ttl, grant_id, resource
        )
        self._tokens.refresh[_digest(refresh)] = _TokenRecord(
            client_id, scopes, now + self._config.refresh_token_ttl, grant_id, resource
        )
        if is_metadata_client_id(client_id):
            self.metadata_clients.remember(client_id)
        self._purge_expired_tokens()
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
        for digest in [d for d, rot in store.rotated.items() if rot.record.expires_at <= now]:
            del store.rotated[digest]
        for rotated in store.rotated.values():
            if rotated.response is not None and not self._in_grace(rotated):
                rotated.response = None  # raw tokens only live for the grace period

    def _remember_rotated(self, digest: str, record: _TokenRecord, token: OAuthToken) -> None:
        rotated = self._tokens.rotated
        successor = _digest(token.refresh_token or "")
        rotated[digest] = _RotatedToken(record, self._clock(), successor, token)
        while len(rotated) > MAX_ROTATED_REFRESH_TOKENS:
            del rotated[next(iter(rotated))]

    def _in_grace(self, rotated: _RotatedToken) -> bool:
        return self._clock() - rotated.rotated_at <= self._config.refresh_reuse_grace

    def _grace_answer(self, digest: str, client: OAuthClientInformationFull) -> OAuthToken | None:
        """The earlier answer for an exchanged refresh token presented again within the grace period.

        None when *digest* was never exchanged; TokenError when it may not be answered again
        (other client, grace period over, or its successor was already used).
        """
        rotated = self._tokens.rotated.get(digest)
        if rotated is None:
            return None
        if (
            rotated.record.client_id != client.client_id
            or not self._in_grace(rotated)
            or rotated.response is None
            or rotated.successor not in self._tokens.refresh
        ):
            raise TokenError("invalid_grant", "refresh token is not valid")
        logger.info(
            "Refresh token of client %s presented again within the grace period; same tokens returned",
            _for_log(client.client_id),
        )
        elapsed = int(self._clock() - rotated.rotated_at)
        return rotated.response.model_copy(update={"expires_in": max(1, (rotated.response.expires_in or 1) - elapsed)})

    def _revoke_grant(self, grant_id: str) -> None:
        """Drop every token of a grant (call inside :meth:`_state_change`)."""
        store = self._tokens
        for bucket in (store.access, store.refresh):
            for digest in [d for d, rec in bucket.items() if rec.grant_id == grant_id]:
                del bucket[digest]
        for digest in [d for d, rot in store.rotated.items() if rot.record.grant_id == grant_id]:
            del store.rotated[digest]

    @staticmethod
    def _evict_for_key(table: dict[str, Any], key: str, per_key: int, total: int) -> None:
        """Make room in *table* (insertion ordered) for one more entry of *key*.

        A key may hold at most *per_key* entries (its oldest is dropped); when the table is
        full, the oldest entry of the busiest network goes (IPv6 grouped by /48), so one
        address or one network cannot push out another address's sign-in. Without a known
        address (``unknown``: an app built without the middleware) only the global limit
        applies.
        """
        if key == "unknown":
            per_key = total
        mine = [k for k, entry in table.items() if entry.key == key]
        while len(mine) >= per_key:
            del table[mine.pop(0)]
        while len(table) >= total:
            counts: dict[str, int] = {}
            for entry in table.values():
                group = _eviction_group(entry.key)
                counts[group] = counts.get(group, 0) + 1
            busiest = max(counts, key=lambda k: counts[k])
            del table[next(k for k, entry in table.items() if _eviction_group(entry.key) == busiest)]

    def _purge_pending(self) -> None:
        now = self._clock()
        for request_id in [r for r, p in self._pending.items() if p.expires_at <= now]:
            del self._pending[request_id]
        for state in [s for s, u in self._upstream.items() if u.expires_at <= now]:
            del self._upstream[state]

    def _evict_clients(self) -> None:
        """Keep the client table bounded; /register is reachable without credentials."""
        if len(self._clients) < MAX_CLIENTS:
            return
        active = {record.client_id for record in self._tokens.refresh.values()}
        active |= {pending.client_id for pending in self._pending.values()}
        active |= {code.client_id for code in self._tokens.codes.values()}
        by_age = sorted(self._clients.values(), key=lambda c: c.client_id_issued_at or 0)
        idle = [c for c in by_age if c.client_id not in active]
        victim = (idle or by_age)[0]
        if victim.client_id is not None:
            logger.info("Evicting registered OAuth client %s to make room", _for_log(victim.client_id))
            del self._clients[victim.client_id]

    def _redirect(self, pending: _PendingLogin, **params: str | None) -> str:
        """Authorization response to the client: always carries ``state`` and ``iss`` (RFC 9207)."""
        return construct_redirect_uri(
            str(pending.params.redirect_uri), state=pending.params.state, iss=self._config.metadata_issuer, **params
        )

    # ----- OAuthAuthorizationServerProvider ------------------------------- #

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        """Registered client, or the client described by an accepted metadata document."""
        if is_metadata_client_id(client_id):
            return await self.metadata_clients.get(client_id) if self.metadata_clients.enabled else None
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        """Accept a dynamically registered public or confidential client."""
        if not self._config.dynamic_registration:
            raise RegistrationError("invalid_client_metadata", "dynamic client registration is disabled")
        if client_info.token_endpoint_auth_method not in ("none", "client_secret_post"):
            raise RegistrationError(
                "invalid_client_metadata",
                "token_endpoint_auth_method must be 'none' or 'client_secret_post'",
            )
        uris = [str(uri) for uri in client_info.redirect_uris or []]
        if not uris:
            raise RegistrationError("invalid_redirect_uri", "at least one redirect_uri is required")
        for uri in uris:
            if not _redirect_uri_allowed(uri, self._config.redirect_hosts):
                hosts = self._config.redirect_hosts
                allowed = "any https host" if hosts is None else ", ".join(sorted(hosts)) or "none"
                raise RegistrationError(
                    "invalid_redirect_uri",
                    f"redirect_uris must use https without a query string on an allowed host ({allowed}); "
                    "http is only allowed for localhost / 127.0.0.1",
                )
        if client_info.client_id is None:
            raise RegistrationError("invalid_client_metadata", "client_id is missing")
        _check_registration_size(client_info, len(uris))
        key = request_client_key()
        if key != "unknown" and self._registrations.is_blocked(key):
            logger.warning("Client registration rate limit reached for %s", key)
            raise RegistrationError(
                "invalid_client_metadata", "too many client registrations from this address; try again later"
            )
        async with self._state_change():
            self._evict_clients()
            self._clients[client_info.client_id] = client_info
        self._registrations.record_failure(key)
        logger.info(
            "Registered OAuth client %s (%s, auth=%s)",
            _for_log(client_info.client_id),
            _for_log(client_info.client_name or "unnamed"),
            client_info.token_endpoint_auth_method,
        )

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        """Park the authorization request and send the athlete to the consent page."""
        if params.resource and not self._config.same_origin(params.resource):
            raise AuthorizeError("invalid_request", "the resource indicator does not name this server")
        self._purge_pending()
        key = request_client_key()
        self._evict_for_key(self._pending, key, MAX_PENDING_PER_KEY, MAX_PENDING_LOGINS)
        request_id = secrets.token_urlsafe(32)
        scopes = params.scopes or self._client_scopes(client)
        requested = tuple(p for p in self._config.permission_classes if _scope_for(p) in scopes)
        self._pending[request_id] = _PendingLogin(
            client_id=client.client_id or "",
            params=params,
            scopes=scopes,
            expires_at=self._clock() + LOGIN_REQUEST_TTL,
            client_name=client.client_name or "",
            client_verified=is_metadata_client_id(client.client_id or ""),
            redirect_host=urlsplit(str(params.redirect_uri)).hostname or "",
            offered=requested or self._config.permission_classes,
            key=key,
        )
        logger.info("Authorization requested by client %s", _for_log(client.client_id))
        return f"{self.login_url}?request={request_id}"

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        """Return a live code of *client*; a reused code revokes the tokens it produced."""
        self._purge_expired_tokens()
        used = self._tokens.used_codes.get(authorization_code)
        if used is not None:
            logger.warning("Authorization code reused by client %s; revoking grant", _for_log(client.client_id))
            async with self._state_change():
                self._tokens.used_codes.pop(authorization_code, None)
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
        try:
            async with self._state_change():
                code = self._tokens.codes.pop(authorization_code.code, None)
                if code is None or code.client_id != client.client_id:
                    raise _Refused("authorization code is not valid")
                grant_id = secrets.token_urlsafe(16)
                self._tokens.used_codes[code.code] = (self._clock() + AUTHORIZATION_CODE_TTL, grant_id)
                token = self._issue_tokens(code.client_id, code.scopes, grant_id, code.resource)
        except _Refused as exc:
            raise TokenError("invalid_grant", str(exc)) from None
        logger.info("Issued tokens to client %s", _for_log(client.client_id))
        return token

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        """Return the refresh token if it belongs to *client* and has not expired.

        A token that was already exchanged is accepted again for the grace period (a client
        that lost the response retries); presented later it revokes the whole grant, because
        then two parties hold tokens of one grant (RFC 9700, refresh token rotation).
        """
        self._purge_expired_tokens()
        digest = _digest(refresh_token)
        record = self._tokens.refresh.get(digest)
        if record is None:
            rotated = self._tokens.rotated.get(digest)
            if rotated is None or rotated.record.client_id != client.client_id:
                return None
            if not self._in_grace(rotated):
                if not self._config.refresh_reuse_revoke:
                    logger.warning(
                        "Refresh token of client %s was used again after it had been exchanged; refused "
                        "(OAUTH_REFRESH_REUSE_REVOKE=false keeps the grant)",
                        _for_log(client.client_id),
                    )
                    return None
                logger.warning(
                    "Refresh token of client %s was used again after it had been exchanged; revoking the grant",
                    _for_log(client.client_id),
                )
                async with self._state_change():
                    self._revoke_grant(rotated.record.grant_id)
                return None
            if rotated.response is None or rotated.successor not in self._tokens.refresh:
                # Its successor was used already: a stale copy, not a lost response.
                return None
            record = rotated.record
        elif record.client_id != client.client_id:
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
        """Rotate the refresh token and issue a new access token for the same grant.

        A token exchanged moments ago (concurrently, or a retry after a lost response) gets
        the same answer again within the grace period instead of a second chain.
        """
        digest = _digest(refresh_token.token)
        answer = self._grace_answer(digest, client)
        if answer is not None:
            return answer
        try:
            async with self._state_change():
                record = self._tokens.refresh.get(digest)
                if record is None:
                    raise _AlreadyRotated()
                if record.client_id != client.client_id:
                    raise _Refused("refresh token is not valid")
                del self._tokens.refresh[digest]
                token = self._issue_tokens(
                    record.client_id, _refresh_scopes(record.scopes, scopes), record.grant_id, record.resource
                )
                self._remember_rotated(digest, record, token)
        except _Refused as exc:
            raise TokenError("invalid_grant", str(exc)) from None
        except _AlreadyRotated:
            answer = self._grace_answer(digest, client)
            if answer is None:
                raise TokenError("invalid_grant", "refresh token is not valid") from None
            return answer
        logger.info("Refreshed tokens for client %s", _for_log(client.client_id))
        return token

    async def load_access_token(self, token: str) -> AccessToken | None:
        """Return the access token metadata used by the bearer middleware (audience-checked)."""
        self._purge_expired_tokens()
        record = self._tokens.access.get(_digest(token))
        if record is None:
            return None
        if record.resource and not self._config.same_origin(record.resource):
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
        digest = _digest(token.token)
        record: _TokenRecord | None
        if isinstance(token, RefreshToken):
            record = self._tokens.refresh.get(digest)
            rotated = self._tokens.rotated.get(digest)
            if record is None and rotated is not None:
                record = rotated.record
        else:
            record = self._tokens.access.get(digest)
        if record is None:
            return
        logger.info("Revoked tokens of client %s", _for_log(record.client_id))
        async with self._state_change():
            self._revoke_grant(record.grant_id)

    # ----- consent / sign-in support -------------------------------------- #

    def pending_login(self, request_id: str) -> _PendingLogin | None:
        """Return the parked authorization request or None if unknown / expired."""
        self._purge_pending()
        return self._pending.get(request_id) if request_id else None

    def form_token(self, browser: str, request_id: str) -> str:
        """Consent form token bound to the browser cookie *browser* and the request (CSRF)."""
        message = f"{browser}|{request_id}".encode("utf-8")
        return hmac.new(self._form_key, message, hashlib.sha256).hexdigest()

    def check_form_token(self, browser: str, request_id: str, token: str) -> bool:
        """Constant-time check of a submitted consent form token."""
        if not browser or not token:
            return False
        return hmac.compare_digest(self.form_token(browser, request_id).encode("ascii"), token.encode("utf-8"))

    def login_blocked(self, key: str) -> bool:
        """True when *key* (client IP) exceeded the failed-login limit."""
        return self._rate_limiter.is_blocked(key)

    def local_login_blocked(self) -> bool:
        """True when password / API-key sign-ins failed too often across all addresses.

        With TOTP the global budget does not pause sign-ins: without the code a password
        guess cannot succeed anyway, and the athlete must not be locked out by others.
        """
        return self._global_failures.is_blocked("*") and not self.totp_required

    def record_login_failure(self, key: str, local: bool = False) -> None:
        """Count a failed login for *key*; *local* failures (password, API key) also count globally."""
        self._rate_limiter.record_failure(key)
        if local:
            self._global_failures.record_failure("*")

    def begin_local_login(self, key: str) -> str | None:
        """Check the limits and count a password / API-key attempt in one step.

        The attempt is counted before the (threaded) password check, so concurrent requests
        cannot all pass the check first. Returns why the attempt is refused, or None.
        :meth:`local_login_succeeded` takes the count back.
        """
        if self._rate_limiter.is_blocked(key):
            return "address"
        if self.local_login_blocked():
            return "global"
        self.record_login_failure(key, local=True)
        return None

    def local_login_succeeded(self, key: str) -> None:
        """Take back the attempt counted by :meth:`begin_local_login` after a successful sign-in."""
        self._rate_limiter.reset(key)
        self._global_failures.forget_last("*")

    def verify_credentials(self, username: str, password: str) -> bool:
        """Constant-time check of the submitted credentials (CPU heavy with a hash: run it in a thread)."""
        return self._config.verify_credentials(username, password)

    def verify_api_key(self, api_key: str) -> bool:
        """Constant-time check of a submitted Intervals.icu API key."""
        return self._config.verify_api_key(api_key)

    @property
    def totp_required(self) -> bool:
        """True when the password and API-key sign-ins need an authenticator code."""
        return self._config.totp_secret is not None

    def verify_second_factor(self, code: str, consume: bool = True) -> bool:
        """Accept an authenticator code once (no replay); always True without TOTP.

        With ``consume=False`` (the first factor failed) the code is checked the same way but
        stays usable, so a typo in the password does not burn the current code.
        """
        secret = self._config.totp_secret
        if secret is None:
            return True
        counter = match_counter(secret, code or "", self._clock())
        if counter is None or counter <= self._totp_last_counter:
            return False
        if consume:
            self._totp_last_counter = counter
        return True

    def grant_for(self, pending: _PendingLogin, chosen: list[str] | None) -> tuple[str, ...]:
        """Permission classes to grant: ``read`` plus the chosen offered classes."""
        picked = set(chosen or []) | {"read"}
        return tuple(p for p in pending.offered if p in picked) or ("read",)

    def complete_login(self, request_id: str, login_key: str, granted: tuple[str, ...] | None = None) -> str:
        """Consume the pending request, mint a code and return the client redirect URL."""
        pending = self._pending.pop(request_id, None)
        if pending is None:
            # Expired, denied or finished in another tab while this sign-in was running.
            raise LoginError("This sign-in link is invalid or has expired. Go back to your MCP client and connect again.")
        self._rate_limiter.reset(login_key)
        params = pending.params
        classes = granted if granted is not None else ("read",)
        scopes = [SCOPE] + [_scope_for(p) for p in classes]
        code = secrets.token_urlsafe(32)
        self._tokens.codes[code] = AuthorizationCode(
            code=code,
            scopes=scopes,
            expires_at=self._clock() + AUTHORIZATION_CODE_TTL,
            client_id=pending.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
        )
        logger.info(
            "Sign-in succeeded; issuing authorization code to client %s (%s)", _for_log(pending.client_id), ", ".join(classes)
        )
        return self._redirect(pending, code=code)

    def deny_login(self, request_id: str) -> str:
        """Consume the pending request and return the access_denied redirect URL."""
        pending = self._pending.pop(request_id, None)
        if pending is None:
            raise LoginError("This sign-in link is invalid or has expired. Go back to your MCP client and connect again.")
        logger.info("Authorization denied for client %s", _for_log(pending.client_id))
        return self._redirect(pending, error="access_denied", error_description="The athlete denied the request")

    # ----- sign-in with Intervals.icu -------------------------------------- #

    def begin_intervals_login(self, request_id: str, granted: tuple[str, ...], login_key: str = "unknown") -> tuple[str, str]:
        """Return (Intervals.icu authorize URL, browser binding value for a cookie)."""
        if self.pending_login(request_id) is None:
            raise LoginError("This sign-in link is invalid or has expired.")
        # Only the latest attempt of a request counts (the cookie of an earlier one is overwritten anyway).
        for state in [s for s, u in self._upstream.items() if u.request_id == request_id]:
            del self._upstream[state]
        self._evict_for_key(self._upstream, login_key, MAX_PENDING_PER_KEY, MAX_PENDING_LOGINS)
        state = secrets.token_urlsafe(32)
        browser = secrets.token_urlsafe(32)
        self._upstream[state] = _UpstreamLogin(
            request_id=request_id,
            granted=granted,
            browser_digest=_digest(browser),
            expires_at=self._clock() + LOGIN_REQUEST_TTL,
            key=login_key,
        )
        query = urlencode(
            {
                "client_id": self._config.intervals_client_id or "",
                "redirect_uri": self._config.intervals_redirect_uri,
                "scope": self._config.intervals_scope,
                "state": state,
            }
        )
        return f"{INTERVALS_AUTHORIZE_URL}?{query}", browser

    async def finish_intervals_login(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self, state: str, browser: str, code: str | None, error: str | None, login_key: str
    ) -> str:
        """Check the Intervals.icu callback and return the redirect URL for the MCP client."""
        self._purge_pending()
        upstream = self._upstream.pop(state, None) if state else None
        if upstream is None:
            raise LoginError("This sign-in has expired. Go back to your MCP client and connect again.")
        if not browser or not hmac.compare_digest(_digest(browser), upstream.browser_digest):
            raise LoginError("This sign-in was started in a different browser. Connect again from your MCP client.")
        if self._pending.get(upstream.request_id) is None:
            raise LoginError("This sign-in has expired. Go back to your MCP client and connect again.")
        if error or not code:
            return self.deny_login(upstream.request_id)
        try:
            payload = await self._intervals_exchange(code)
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            logger.warning("Intervals.icu token exchange failed: %s", type(exc).__name__)
            raise LoginError("Intervals.icu did not confirm the sign-in. Please try again.", 502) from exc
        athlete = payload.get("athlete") if isinstance(payload, dict) else None
        athlete_id = normalize_athlete_id(athlete.get("id", "")) if isinstance(athlete, dict) else ""
        if not athlete_id or athlete_id not in self._config.allowed_athletes:
            self._pending.pop(upstream.request_id, None)
            self.record_login_failure(login_key)
            logger.warning("Intervals.icu athlete %s is not allowed on this server", _for_log(athlete_id or "(unknown)"))
            raise LoginError("This Intervals.icu account is not allowed to use this server.", 403)
        logger.info("Intervals.icu sign-in confirmed for athlete %s", _for_log(athlete_id))
        return self.complete_login(upstream.request_id, login_key, upstream.granted)

    async def _exchange_intervals_code(self, code: str) -> dict[str, Any]:
        """POST the code to Intervals.icu; only the athlete id of the answer is used."""
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            response = await client.post(
                INTERVALS_TOKEN_URL,
                data={
                    "client_id": self._config.intervals_client_id or "",
                    "client_secret": self._config.intervals_client_secret or "",
                    "code": code,
                },
                headers={"Accept": "application/json"},
            )
        if response.status_code != 200:
            raise ValueError(f"token endpoint answered HTTP {response.status_code}")
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("unexpected token response")
        return data


# --------------------------------------------------------------------------- #
# FastMCP integration
# --------------------------------------------------------------------------- #


def install_login_routes(mcp: FastMCP[Any], provider: SingleUserOAuthProvider) -> None:
    """Register the consent / sign-in routes on *mcp* (call before building the ASGI app)."""
    from intervals_mcp_server.auth_pages import install_routes  # pylint: disable=import-outside-toplevel

    install_routes(mcp, provider)


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
    logger.info(
        "OAuth enabled; issuer %s, sign-in %s, state file %s",
        config.issuer,
        "+".join(config.login_methods),
        config.state_file,
    )
    return {"auth_server_provider": provider, "auth": auth_settings(config)}


def auth_settings(config: OAuthConfig) -> AuthSettings:
    """The MCP SDK auth settings for *config* (issuer, resource, registration, scopes)."""
    settings: dict[str, Any] = {
        "issuer_url": AnyHttpUrl(config.public_url),
        "resource_server_url": AnyHttpUrl(config.public_url),
        "client_registration_options": ClientRegistrationOptions(
            enabled=config.dynamic_registration,
            valid_scopes=config.scopes_supported,
            default_scopes=config.scopes_supported,
        ),
        "revocation_options": RevocationOptions(enabled=True),
        "required_scopes": [SCOPE],
    }
    try:
        # The provider checks the audience itself (same origin as MCP_PUBLIC_URL, so /mcp and
        # /sse tokens and clients that send no resource indicator keep working). Older SDKs
        # do not know the field.
        return AuthSettings(**settings, validate_token_resource=False)
    except (TypeError, ValidationError):
        return AuthSettings(**settings)


def auth_status_from_env(environ: Mapping[str, str] | None = None, include_private: bool = False) -> dict[str, Any]:
    """Describe the auth configuration for a status report; never includes secrets.

    The sign-in user name, the password source, the allowed athletes and the state file
    path describe the deployment rather than the connection; they are only included with
    *include_private* (``--doctor`` on the server itself), not in the MCP tool's answer.
    """
    env = os.environ if environ is None else environ
    if _auth_mode(env) != "oauth":
        return {"mode": "none", "issuer": None, **({"state_file": None} if include_private else {})}
    try:
        methods: list[str] = list(_login_methods(env))
    except ValueError:
        methods = ["invalid"]
    status: dict[str, Any] = {
        "mode": "oauth",
        "issuer": env.get("MCP_PUBLIC_URL", "").strip() or None,
        "login": methods,
        "intervals_app": "configured" if env.get("INTERVALS_OAUTH_CLIENT_ID", "").strip() else "not configured",
        "second_factor": "totp" if env.get("OAUTH_TOTP_SECRET", "").strip() else "none",
        "dynamic_registration": env.get("OAUTH_DYNAMIC_REGISTRATION", "true").strip().lower() not in _FALSE,
    }
    if include_private:
        athletes = env.get("OAUTH_ALLOWED_ATHLETES", "").strip() or env.get("ATHLETE_ID", "").strip()
        status.update(
            {
                "state_file": str(Path(env.get("OAUTH_STATE_FILE", "").strip() or DEFAULT_STATE_FILE)),
                "allowed_athletes": sorted(normalize_athlete_id(a) for a in athletes.split(",") if a.strip()),
                "username": env.get("OAUTH_USERNAME", "").strip() or DEFAULT_USERNAME,
                "password_source": (
                    "hash"
                    if env.get("OAUTH_PASSWORD_HASH", "").strip()
                    else "plain" if env.get("OAUTH_PASSWORD") else "missing"
                ),
            }
        )
    return status


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:  # pylint: disable=too-many-locals
    """CLI helpers: ``hash-password`` (OAUTH_PASSWORD_HASH) and ``totp-secret`` (OAUTH_TOTP_SECRET)."""
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
    totp_cmd = commands.add_parser(
        "totp-secret", help="create an OAUTH_TOTP_SECRET and the otpauth:// URI for an authenticator app"
    )
    totp_cmd.add_argument("--account", default=os.environ.get("OAUTH_USERNAME", "").strip() or DEFAULT_USERNAME,
                          help="account name shown in the authenticator app")
    totp_cmd.add_argument("--issuer", default="Intervals MCP", help="issuer shown in the authenticator app")
    args = parser.parse_args(argv)

    if args.command == "totp-secret":
        secret = generate_secret()
        print(f"OAUTH_TOTP_SECRET={secret}")
        print(provisioning_uri(secret, args.account, args.issuer))
        return 0

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
