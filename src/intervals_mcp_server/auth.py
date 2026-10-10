# pylint: disable=too-many-lines
"""Optional built-in OAuth 2.1 authorization server for remote MCP clients.

Remote MCP clients such as ChatGPT or Claude support either "no authentication" or OAuth
2.1 with PKCE.  Intervals.icu cannot act as their authorization server (no discovery
metadata, no PKCE, no client registration), so this server is the authorization server
for the MCP client and, optionally, an OAuth *client* of Intervals.icu for the sign-in:

``MCP client  <-- OAuth 2.1 (PKCE, CIMD / DCR, RFC 9207 iss) -->  this server``
``this server <-- OAuth 2 ("Sign in with Intervals.icu") ------>  Intervals.icu``

The athlete proves who they are by signing in at Intervals.icu; only the athletes in
``OAUTH_ALLOWED_ATHLETES`` (default: ``ATHLETE_ID``) may connect.  In the default single-user
mode the Intervals.icu token is used for that identity check only and is never stored or
handed to the MCP client; data access keeps using the configured API key.  A password
sign-in is available as an alternative (or fallback) when no Intervals.icu OAuth app is
configured.

In the multi-user mode (``MCP_TENANCY=multi``, see :mod:`intervals_mcp_server.tenancy`) the
server keeps the athlete's Intervals.icu token, sealed with AES-GCM
(:mod:`intervals_mcp_server.token_vault`), in a grant record of the state file
(:mod:`intervals_mcp_server.auth_grants`, format version 2), and every tool call of that
connection uses it.  The API key is only used for the owner (``ATHLETE_ID``) after the owner
signed in.  Revoking a grant deletes its stored token.

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
``MCP_TENANCY``                    ``single`` (default) or ``multi``: one credential per connection
``OAUTH_TOKEN_KEY`` / ``OAUTH_TOKEN_KEY_FILE``  key(s) that seal the stored Intervals.icu tokens
                                   (required with ``multi``)
``OAUTH_ALLOW_ANY_ATHLETE``        accept ``OAUTH_ALLOWED_ATHLETES=*`` in the multi-user mode
``OAUTH_TOKEN_RETENTION_DAYS``     multi-user mode: drop stored tokens unused for this many days (0 = off)
``INTERVALS_OAUTH_EXCLUDE_AREAS``  multi-user mode: Intervals.icu scope areas never requested
``INTERVALS_OAUTH_OFFER_CHATS``    multi-user mode: offer "activity comments" (CHATS) on the consent page
``OAUTH_MAX_GRANTS_PER_ATHLETE``   multi-user mode: connections kept per athlete (default 5)
``OAUTH_OWNER_ACCOUNTS``           single-user mode: the owner's other accounts allowed to sign in
``INTERVALS_OAUTH_BASE_URL``       Intervals.icu OAuth endpoints (default https://intervals.icu; tests)

Access tokens live in memory only; registered clients and refresh tokens are persisted
(as digests) so a restart does not disconnect clients.  The state file is written in a
worker thread, in-memory state only changes once the write succeeded, and a file that
cannot be read is never overwritten.  A change of the file by someone else (the ``grants``
CLI) is picked up before the next request.  Secrets are never logged.
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
from intervals_mcp_server.auth_grants import (
    DEFAULT_STATE_FILE,
    SINGLE_USER_STATE_VERSION,
    STATE_VERSION,
    Grant,
    PendingGrant,
    file_signature,
    grant_context,
    state_file_lock,
    state_format_problem,
    write_state_file,
)
from intervals_mcp_server.auth_totp import generate_secret, match_counter, normalize_secret, provisioning_uri
from intervals_mcp_server.tenancy import (
    AREA_ORDER,
    Credential,
    canonical_athlete_id,
    intervals_scopes_for,
    parse_intervals_scopes,
    same_athlete,
    tenancy_from_env,
)
from intervals_mcp_server.token_vault import TokenVault, VaultError, vault_from_env

__all__ = [
    "CredentialError",
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
DEFAULT_INTERVALS_OAUTH_BASE = "https://intervals.icu"
INTERVALS_AUTHORIZE_URL = DEFAULT_INTERVALS_OAUTH_BASE + "/oauth/authorize"
INTERVALS_TOKEN_URL = DEFAULT_INTERVALS_OAUTH_BASE + "/api/oauth/token"
DEFAULT_INTERVALS_SCOPE = "ACTIVITY:READ"
DEFAULT_TRUSTED_CLIENT_HOSTS = ("chatgpt.com", "claude.ai", "claude.com")
LOGIN_METHODS = ("intervals", "password", "apikey")
DEFAULT_USERNAME = "athlete"
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

# A grant's last use is written to the state file at most this often (seconds).
LAST_USE_WRITE_INTERVAL = 3600
# Multi-user mode: grants per athlete (OAUTH_MAX_GRANTS_PER_ATHLETE) and in total; the least
# recently used ones are revoked first.
DEFAULT_MAX_GRANTS_PER_ATHLETE = 5
MAX_GRANT_RECORDS = 500
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
    # Multi-user mode (MCP_TENANCY=multi); see intervals_mcp_server.tenancy.
    tenancy: str = "single"
    owner_athlete: str | None = None
    # The server's API_KEY for the owner's own connections (multi-user mode only).
    owner_api_key: str | None = field(default=None, repr=False)
    allow_any_athlete: bool = False
    vault: TokenVault | None = field(default=None, repr=False, compare=False)
    token_retention_days: int = 0
    intervals_exclude_areas: frozenset[str] = frozenset()
    intervals_offer_chats: bool = False
    intervals_oauth_base: str = DEFAULT_INTERVALS_OAUTH_BASE
    max_grants_per_athlete: int = DEFAULT_MAX_GRANTS_PER_ATHLETE
    # Other Intervals.icu accounts of the owner (OAUTH_OWNER_ACCOUNTS): allowed in the single-user mode.
    owner_accounts: frozenset[str] = frozenset()

    @property
    def multi_user(self) -> bool:
        """True with ``MCP_TENANCY=multi``: every connection uses its own credential."""
        return self.tenancy == "multi"

    @property
    def intervals_authorize_url(self) -> str:
        """Authorization endpoint of Intervals.icu."""
        return self.intervals_oauth_base.rstrip("/") + "/oauth/authorize"

    @property
    def intervals_token_url(self) -> str:
        """Token endpoint of Intervals.icu."""
        return self.intervals_oauth_base.rstrip("/") + "/api/oauth/token"

    def athlete_allowed(self, athlete_id: str) -> bool:
        """True when *athlete_id* may connect (``OAUTH_ALLOWED_ATHLETES``, or any with the explicit opt-in)."""
        if self.allow_any_athlete:
            return bool(athlete_id)
        return normalize_athlete_id(athlete_id) in self.allowed_athletes

    def is_owner(self, athlete_id: str) -> bool:
        """True when *athlete_id* is the server owner (``ATHLETE_ID``)."""
        return self.owner_athlete is not None and same_athlete(athlete_id, self.owner_athlete)

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
    tenancy = tenancy_from_env(env)
    athletes_raw = env.get("OAUTH_ALLOWED_ATHLETES", "").strip() or env.get("ATHLETE_ID", "").strip()
    athletes = frozenset(normalize_athlete_id(a) for a in athletes_raw.split(",") if a.strip())
    allow_any = "*" in athletes
    if allow_any:
        if tenancy != "multi" or not _env_bool(env, "OAUTH_ALLOW_ANY_ATHLETE", False):
            raise ValueError(
                "OAUTH_ALLOWED_ATHLETES='*' (any Intervals.icu athlete) is only accepted with MCP_TENANCY=multi and "
                "OAUTH_ALLOW_ANY_ATHLETE=true; list the athlete ids instead"
            )
        athletes = athletes - {"*"}
    if "intervals" in methods:
        if not client_id or not client_secret:
            raise ValueError(
                "OAUTH_LOGIN=intervals requires INTERVALS_OAUTH_CLIENT_ID and INTERVALS_OAUTH_CLIENT_SECRET "
                "(apply for an app at https://intervals.icu/oauth/apply)"
            )
        if not athletes and not allow_any:
            raise ValueError("OAUTH_LOGIN=intervals requires OAUTH_ALLOWED_ATHLETES or ATHLETE_ID")
    owner_accounts = frozenset(
        normalize_athlete_id(a) for a in env.get("OAUTH_OWNER_ACCOUNTS", "").split(",") if a.strip()
    )
    if tenancy == "single":
        _check_single_user_allowlist(env, athletes, owner_accounts)
    multi = _multi_user_settings(env, tenancy, methods, api_key, totp_secret is not None)

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
        tenancy=tenancy,
        allow_any_athlete=allow_any,
        owner_accounts=owner_accounts,
        **multi,
    )


def _check_single_user_allowlist(env: Mapping[str, str], athletes: frozenset[str], owner_accounts: frozenset[str]) -> None:
    """Single-user mode: every allowed athlete sees the owner's data, so only the owner may be on the list.

    ``OAUTH_OWNER_ACCOUNTS`` names other Intervals.icu accounts of the owner that may sign in too.
    """
    owner = env.get("ATHLETE_ID", "").strip()
    others = sorted(athletes - owner_accounts - ({normalize_athlete_id(owner)} if owner else set()))
    if (owner and others) or (not owner and len(others) > 1):
        raise ValueError(
            f"OAUTH_ALLOWED_ATHLETES names {len(others)} athlete(s) besides ATHLETE_ID: in the single-user mode every "
            "allowed athlete sees the owner's data. To share the server use MCP_TENANCY=multi; for your own other "
            "Intervals.icu accounts set OAUTH_OWNER_ACCOUNTS"
        )


def _oauth_base(env: Mapping[str, str]) -> str:
    raw = env.get("INTERVALS_OAUTH_BASE_URL", "").strip().rstrip("/")
    if not raw:
        return DEFAULT_INTERVALS_OAUTH_BASE
    parts = urlsplit(raw)
    if not parts.hostname or not (parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in _LOOPBACK_HOSTS)):
        raise ValueError("INTERVALS_OAUTH_BASE_URL must be an https URL (http only for localhost / 127.0.0.1)")
    return raw


def _multi_user_settings(  # pylint: disable=too-many-locals
    env: Mapping[str, str], tenancy: str, methods: tuple[str, ...], api_key: str, totp: bool
) -> dict[str, Any]:
    """The OAuthConfig fields of the multi-user mode; raise ValueError when it is incomplete."""
    athlete = env.get("ATHLETE_ID", "").strip()
    settings: dict[str, Any] = {
        "owner_athlete": canonical_athlete_id(athlete) if athlete else None,
        "intervals_oauth_base": _oauth_base(env),
    }
    if tenancy != "multi":
        return settings
    if "intervals" not in methods:
        raise ValueError(
            "MCP_TENANCY=multi needs the Intervals.icu sign-in (OAUTH_LOGIN=intervals, optionally with password): "
            "other athletes can only connect with their own Intervals.icu account"
        )
    local = [m for m in methods if m != "intervals"]
    if local and not (athlete and api_key):
        raise ValueError(
            f"MCP_TENANCY=multi with OAUTH_LOGIN={','.join(methods)} needs ATHLETE_ID and API_KEY: the "
            f"{'/'.join(local)} sign-in is the owner's and uses the owner's API key"
        )
    if local and not totp:
        raise ValueError(
            f"MCP_TENANCY=multi with the {'/'.join(local)} sign-in needs OAUTH_TOTP_SECRET: on a shared sign-in page it "
            "is the one way to the owner's API key. Use OAUTH_LOGIN=intervals (you sign in with Intervals.icu as "
            "ATHLETE_ID and still use the API key) or add the authenticator code"
        )
    try:
        vault = vault_from_env(env)
    except ValueError as exc:
        raise ValueError(f"MCP_TENANCY=multi: {exc}") from exc
    if vault is None:
        raise ValueError(
            "MCP_TENANCY=multi needs OAUTH_TOKEN_KEY or OAUTH_TOKEN_KEY_FILE to encrypt the stored Intervals.icu tokens "
            "(create a key file with: futureweb-intervals-mcp token-key --file <path>)"
        )
    exclude = {a.strip().upper() for a in env.get("INTERVALS_OAUTH_EXCLUDE_AREAS", "").split(",") if a.strip()}
    unknown = sorted(exclude - set(AREA_ORDER))
    if unknown:
        raise ValueError(f"INTERVALS_OAUTH_EXCLUDE_AREAS contains unknown area(s) {', '.join(unknown)}; use {', '.join(AREA_ORDER)}")
    settings.update(
        owner_api_key=(api_key or None) if athlete else None,
        vault=vault,
        token_retention_days=_env_int(env, "OAUTH_TOKEN_RETENTION_DAYS", 0, minimum=0),
        intervals_exclude_areas=frozenset(exclude),
        intervals_offer_chats=_env_bool(env, "INTERVALS_OAUTH_OFFER_CHATS", False),
        max_grants_per_athlete=_env_int(env, "OAUTH_MAX_GRANTS_PER_ATHLETE", DEFAULT_MAX_GRANTS_PER_ATHLETE),
    )
    return settings


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
    # Scope requested at Intervals.icu (multi-user mode: from the granted permission classes).
    intervals_scope: str = ""


@dataclass
class _TokenRecord:
    """Access or refresh token metadata, keyed by the token digest in the store."""

    client_id: str
    scopes: list[str]
    expires_at: int
    grant_id: str
    resource: str | None = None
    # The athlete who signed in (canonical id) and how; None for grants from before they were recorded.
    athlete_id: str | None = None
    method: str | None = None


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
    # Set once the state file holding the successor was written: the answer is only
    # handed out again after that (a failed write undoes the rotation).
    committed: bool = False


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


class _ExternalChange(Exception):
    """The state file was changed by someone else (the grants CLI) since it was last read."""


class CredentialError(Exception):
    """The connection has no usable Intervals.icu credential (the message says what to do)."""


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


def _record_from_state(raw: Any, version: int = 1) -> _TokenRecord:
    """A persisted refresh-token record; raises ValueError / TypeError / KeyError when malformed.

    Format 2 records must name their grant (no id is invented: a token is only served with
    its grant record).
    """
    if not isinstance(raw, dict):
        raise TypeError("not an object")
    client_id = raw["client_id"]
    scopes = raw.get("scopes", [SCOPE])
    expires_at = raw["expires_at"]
    if version >= STATE_VERSION and not raw.get("grant_id"):
        raise KeyError("grant_id")
    grant_id = raw.get("grant_id") or secrets.token_urlsafe(16)
    resource = raw.get("resource")
    athlete, method = raw.get("athlete_id"), raw.get("method")
    if not isinstance(client_id, str) or not isinstance(grant_id, str):
        raise TypeError("client_id and grant_id must be strings")
    if not isinstance(scopes, list) or not all(isinstance(scope, str) for scope in scopes):
        raise TypeError("scopes must be a list of strings")
    if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
        raise TypeError("expires_at must be a number")
    if resource is not None and not isinstance(resource, str):
        raise TypeError("resource must be a string")
    if (athlete is not None and not (isinstance(athlete, str) and athlete)) or (method is not None and not isinstance(method, str)):
        raise TypeError("athlete_id and method must be strings")
    return _TokenRecord(
        client_id, list(scopes), int(expires_at), grant_id, resource,
        canonical_athlete_id(athlete) if athlete else None, method,
    )


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
    """OAuth 2.1 provider: consent, Intervals.icu or password sign-in, persisted refresh tokens.

    Despite its name it also serves the multi-user mode (``MCP_TENANCY=multi``): each grant then
    has a record naming the athlete who connected and, for athletes other than the owner, their
    sealed Intervals.icu token (see :meth:`connection_credential`).
    """

    def __init__(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self,
        config: OAuthConfig,
        clock: Callable[[], float] = time.time,
        fetch: Fetcher | None = None,
        intervals_exchange: IntervalsTokenExchange | None = None,
        intervals_refresh: IntervalsTokenExchange | None = None,
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
        self._intervals_refresh = intervals_refresh or self._refresh_intervals_token
        self._totp_last_counter = -1
        # Key for the consent form tokens (CSRF); a restart also drops the pending requests.
        self._form_key = secrets.token_bytes(32)
        # Entries of the state file this version could not read: written back unchanged.
        self._raw_clients: dict[str, Any] = {}
        self._raw_refresh: dict[str, Any] = {}
        self._raw_grants: dict[str, Any] = {}
        # Multi-user mode: grant records (persisted) and the grants of issued codes (memory only).
        self._grants: dict[str, Grant] = {}
        self._code_grants: dict[str, PendingGrant] = {}
        # Multi-user mode: grants of the single-user mode refused until `grants adopt-legacy --owner`.
        self._awaiting_adoption: set[str] = set()
        # (inode, mtime, size) of the state file as last read or written by this process.
        self._known_signature: tuple[int, int, int] | None = None
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

    def _read_state_file(self) -> dict[str, Any] | None:
        """The parsed state file, None when there is none; ValueError when it cannot be used."""
        path = self._config.state_file
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(
                f"OAUTH_STATE_FILE {path} could not be read ({_one_line(exc)}); the file was left unchanged. "
                "Repair it, restore a backup or move it away (all clients then have to connect again)"
            ) from exc
        problem = state_format_problem(data)
        if problem:
            raise ValueError(
                f"OAUTH_STATE_FILE {path} {problem}; the file was left unchanged. "
                "Repair it, restore a backup or move it away (all clients then have to connect again)"
            )
        return data

    def _load_state(self) -> None:
        """Read the state file; anything unusable raises ValueError and the file stays untouched."""
        signature = file_signature(self._config.state_file)
        data = self._read_state_file()
        self._known_signature = signature
        if data is None:
            return
        self._apply_state(data)
        logger.info(
            "OAuth state loaded: %d client(s), %d refresh token(s)%s",
            len(self._clients),
            len(self._tokens.refresh),
            f", {len(self._grants)} multi-user grant record(s)" if self._config.multi_user else "",
        )

    def _apply_state(self, data: dict[str, Any]) -> None:  # pylint: disable=too-many-locals,too-many-branches,too-many-statements
        """Replace the persisted tables with *data*; memory-only tokens of vanished grants are dropped.

        Multi-user mode, fail closed: a token is only served with its grant record. Connections of
        the single-user mode (format 1) whose record names the owner become owner grant records
        here, once; those without a recorded athlete are refused but kept in the file until the
        operator adopts them (``grants adopt-legacy --owner``); other athletes' are dropped. Tokens
        kept verbatim (an unreadable client or grant) keep their grant record in the file.
        """
        now = self._clock()
        multi = self._config.multi_user
        version = data.get("version", 1) if isinstance(data.get("version", 1), int) else 1
        clients: dict[str, OAuthClientInformationFull] = {}
        raw_clients: dict[str, Any] = {}
        for client_id, raw in data.get("clients", {}).items():
            try:
                clients[client_id] = OAuthClientInformationFull.model_validate(raw)
            except ValidationError as exc:
                # Kept verbatim (a stricter SDK must not silently delete registrations).
                logger.warning(
                    "OAuth state: client %s could not be read (%d validation error(s)); kept in the file, not usable",
                    _for_log(client_id),
                    exc.error_count(),
                )
                raw_clients[client_id] = raw
        stored_grants: dict[str, Any] = data.get("grants") or {}
        grants: dict[str, Grant] = {}
        raw_grants: dict[str, Any] = {}
        for grant_id, raw in stored_grants.items():
            try:
                grants[grant_id] = Grant.from_state(raw)
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning("OAuth state: a grant entry could not be read (%s)", _one_line(exc))
                raw_grants[grant_id] = raw
        refresh: dict[str, _TokenRecord] = {}
        raw_refresh: dict[str, Any] = {}
        dropped: set[str] = set()
        awaiting: set[str] = set()
        converted: dict[str, Grant] = {}
        for digest, raw in data.get("refresh_tokens", {}).items():
            try:
                record = _record_from_state(raw, version)
            except (KeyError, TypeError, ValueError) as exc:
                if not multi and self._raw_record_of_other_athlete(raw, grants, raw_grants):
                    dropped.add(str(raw.get("grant_id")))
                    continue
                logger.warning("OAuth state: a refresh token entry could not be read (%s); kept in the file", _one_line(exc))
                raw_refresh[digest] = raw
                continue
            if record.expires_at <= now:
                continue
            grant = grants.get(record.grant_id)
            if record.grant_id in raw_grants:
                if multi:  # kept with its grant record for a version that can read it; never usable here
                    raw_refresh[digest] = raw
                else:  # a single-user file must not hand it to the owner's API key
                    dropped.add(record.grant_id)
                continue
            if multi and grant is None:
                grant = self._grant_for_record(record, version, converted)
                if grant is None:
                    if record.athlete_id is None or self._config.is_owner(record.athlete_id):
                        raw_refresh[digest] = raw  # refused until adopted; kept for the owner
                        awaiting.add(record.grant_id)
                    else:
                        dropped.add(record.grant_id)
                    continue
            if not self._usable(record, grant):
                dropped.add(record.grant_id)
                continue
            if record.client_id in raw_clients:
                raw_refresh[digest] = raw  # kept verbatim, with its grant record
                continue
            if not (record.client_id in clients or is_metadata_client_id(record.client_id)):
                continue
            refresh[digest] = record
            if is_metadata_client_id(record.client_id):
                # Its document is fetched even while unknown client ids use up the budget.
                self.metadata_clients.remember(record.client_id)
        grants.update(converted)
        live = {record.grant_id for record in refresh.values()}
        kept_raw = {raw.get("grant_id") for raw in raw_refresh.values() if isinstance(raw, dict)}
        self._clients, self._raw_clients = clients, raw_clients
        self._tokens.refresh, self._raw_refresh = refresh, raw_refresh
        self._grants = {grant_id: grant for grant_id, grant in grants.items() if grant_id in live}
        self._raw_grants = {}
        if multi:
            self._raw_grants = dict(raw_grants)
            self._raw_grants.update(
                {gid: stored_grants[gid] for gid in kept_raw if isinstance(gid, str) and gid in stored_grants and gid not in live}
            )
        self._tokens.access = {d: r for d, r in self._tokens.access.items() if r.grant_id in live}
        self._tokens.rotated = {d: r for d, r in self._tokens.rotated.items() if r.record.grant_id in live}
        if converted:
            logger.info("OAuth state: %d connection(s) of the owner from the single-user mode became owner grants", len(converted))
        self._awaiting_adoption = awaiting
        if awaiting:
            logger.warning(
                "OAuth state: %d connection(s) from the single-user mode are refused in the multi-user mode until you "
                "confirm they are yours: futureweb-intervals-mcp grants adopt-legacy --owner (grants list shows them)",
                len(awaiting),
            )
        if dropped:
            logger.warning(
                "OAuth state: %d connection(s) cannot be used in the %s-user mode (other athletes, an athlete no longer "
                "allowed, or no owner API key); they are removed at the next write",
                len(dropped),
                "multi" if multi else "single",
            )

    def _grant_for_record(self, record: _TokenRecord, version: int, converted: dict[str, Grant]) -> Grant | None:
        """Multi-user mode, a token without grant record: an owner grant only for a format-1 record naming the owner."""
        config = self._config
        if version >= STATE_VERSION or not record.athlete_id or not config.is_owner(record.athlete_id):
            return None
        if not self._owner_key_available():
            return None
        now = self._now()
        return converted.setdefault(
            record.grant_id,
            Grant(config.owner_athlete or record.athlete_id, "owner", record.method or "legacy", record.client_id, now, now, (), None, now),
        )

    def _raw_record_of_other_athlete(self, raw: Any, grants: dict[str, Grant], raw_grants: dict[str, Any]) -> bool:
        """Single-user mode: an unreadable refresh record that evidently belongs to another athlete."""
        if not isinstance(raw, dict):
            return False
        grant_id = raw.get("grant_id")
        if isinstance(grant_id, str) and grant_id in raw_grants:
            return True
        grant = grants.get(grant_id) if isinstance(grant_id, str) else None
        athlete = grant.athlete_id if grant is not None else raw.get("athlete_id")
        if grant is not None and grant.kind == "athlete" and not self._config.is_owner(grant.athlete_id):
            return True
        return isinstance(athlete, str) and not self._single_user_athlete(athlete)

    def _owner_key_available(self) -> bool:
        return bool(self._config.owner_athlete and self._config.owner_api_key)

    def _single_user_athlete(self, athlete: str) -> bool:
        """Single-user mode: the owner, or one of the owner's other accounts on the allowlist."""
        config = self._config
        return config.is_owner(athlete) or normalize_athlete_id(athlete) in (config.allowed_athletes | config.owner_accounts)

    def _usable(self, record: _TokenRecord | None, grant: Grant | None) -> bool:
        """Whether a token (its record and grant record) may be used in the configured mode.

        Single-user mode: every connection reaches the owner's data with the API key, so only the
        owner's connections (and those from before athletes were recorded) are kept. Multi-user
        mode, fail closed: only with a grant record; owner grants need the owner's API key, athlete
        grants their sealed token, and the athlete must still be allowed.
        """
        config = self._config
        recorded = record.athlete_id if record is not None else None
        if grant is not None and recorded and not same_athlete(recorded, grant.athlete_id):
            return False
        if not config.multi_user:
            if grant is not None and grant.kind == "athlete" and not config.is_owner(grant.athlete_id):
                return False
            athlete = grant.athlete_id if grant is not None else recorded
            return athlete is None or self._single_user_athlete(athlete)
        if grant is None:
            return False
        if grant.kind == "owner":
            return self._owner_key_available() and config.is_owner(grant.athlete_id)
        return bool(grant.sealed) and config.vault is not None and config.athlete_allowed(grant.athlete_id)

    def _sync_from_disk(self) -> None:
        """Pick up a change of the state file made by someone else (``grants remove``)."""
        signature = file_signature(self._config.state_file)
        if signature == self._known_signature:
            return
        try:
            data = self._read_state_file()
        except ValueError as exc:
            # Keep serving from memory; writes are refused until the file is readable again.
            logger.error("%s", _one_line(exc))
            return
        self._known_signature = signature
        self._apply_state(data or {})
        logger.info("OAUTH_STATE_FILE %s was changed by another process; reloaded", self._config.state_file)

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
                    **({"athlete_id": record.athlete_id} if record.athlete_id else {}),
                    **({"method": record.method} if record.method else {}),
                }
                for digest, record in self._tokens.refresh.items()
                if record.expires_at > now
            }
        )
        if not self._config.multi_user:
            return {"version": SINGLE_USER_STATE_VERSION, "clients": clients, "refresh_tokens": refresh}
        live = {record.grant_id for record in self._tokens.refresh.values() if record.expires_at > now}
        grants: dict[str, Any] = dict(self._raw_grants)
        grants.update({grant_id: grant.to_state() for grant_id, grant in self._grants.items() if grant_id in live})
        return {"version": STATE_VERSION, "clients": clients, "refresh_tokens": refresh, "grants": grants}

    def _write_state(self, data: dict[str, Any]) -> None:
        """Atomically replace the state file (runs in a worker thread, under the file lock).

        Refused (``_ExternalChange``) when the file changed since this process last read or
        wrote it, so a change by the grants CLI is never overwritten.
        """
        path = self._config.state_file
        with state_file_lock(path):
            if file_signature(path) != self._known_signature:
                raise _ExternalChange()
            write_state_file(path, data)
            self._known_signature = file_signature(path)

    def _tables(self) -> list[dict[str, Any]]:
        store = self._tokens
        return [
            self._clients, store.codes, store.used_codes, store.access, store.refresh, store.rotated,
            self._grants, self._code_grants,
        ]

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
            self._sync_from_disk()
            before = [dict(table) for table in self._tables()]
            try:
                yield
            except Exception:
                self._undo(self._changes_since(before))
                raise
            changes = self._changes_since(before)
            try:
                await anyio.to_thread.run_sync(self._write_state, self._state_snapshot())
            except _ExternalChange:
                self._undo(changes)
                self._sync_from_disk()
                logger.warning("OAUTH_STATE_FILE %s changed during a write; the change was not saved", self._config.state_file)
                raise OSError("the OAuth state file was changed by another process; please try again") from None
            except Exception as exc:
                self._undo(changes)
                if isinstance(exc, OSError):
                    logger.error("Could not write OAUTH_STATE_FILE %s: %s", self._config.state_file, _one_line(exc))
                raise
            # Every successor in memory is on disk now (bodies only run under this lock).
            for rotated in self._tokens.rotated.values():
                rotated.committed = True

    # ----- helpers -------------------------------------------------------- #

    def _now(self) -> int:
        return int(self._clock())

    def _client_scopes(self, client: OAuthClientInformationFull) -> list[str]:
        return client.scope.split() if client.scope else [SCOPE]

    def _issue_tokens(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self,
        client_id: str,
        scopes: list[str],
        grant_id: str,
        resource: str | None,
        athlete_id: str | None = None,
        method: str | None = None,
    ) -> OAuthToken:
        """Add a new access / refresh token pair (call inside :meth:`_state_change`)."""
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        now = self._now()
        athlete = canonical_athlete_id(athlete_id) if athlete_id else None
        self._tokens.access[_digest(access)] = _TokenRecord(
            client_id, scopes, now + self._config.access_token_ttl, grant_id, resource, athlete, method
        )
        self._tokens.refresh[_digest(refresh)] = _TokenRecord(
            client_id, scopes, now + self._config.refresh_token_ttl, grant_id, resource, athlete, method
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
        for code in [c for c in self._code_grants if c not in store.codes]:
            del self._code_grants[code]  # the token of a sign-in whose code was never exchanged
        self._purge_grants(now)
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
            or not rotated.committed
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

    def _purge_grants(self, now: float) -> None:
        """Drop grant records (and their stored tokens) without live tokens or unused for too long."""
        if not self._grants:
            return
        store = self._tokens
        live = {rec.grant_id for rec in store.refresh.values()} | {rec.grant_id for rec in store.access.values()}
        retention = self._config.token_retention_days * 86400
        for grant_id, grant in list(self._grants.items()):
            if grant_id not in live:
                del self._grants[grant_id]
            elif retention and grant.kind == "athlete" and grant.last_used_at < now - retention:
                logger.info(
                    "Dropping the connection of athlete %s unused for %d days (OAUTH_TOKEN_RETENTION_DAYS)",
                    _for_log(grant.athlete_id),
                    self._config.token_retention_days,
                )
                self._revoke_grant(grant_id)

    def _revoke_grant(self, grant_id: str) -> None:
        """Drop every token of a grant and its stored Intervals.icu token (call inside :meth:`_state_change`)."""
        store = self._tokens
        for bucket in (store.access, store.refresh):
            for digest in [d for d, rec in bucket.items() if rec.grant_id == grant_id]:
                del bucket[digest]
        for digest in [d for d, rot in store.rotated.items() if rot.record.grant_id == grant_id]:
            del store.rotated[digest]
        self._grants.pop(grant_id, None)

    @staticmethod
    def _evict_for_key(table: dict[str, Any], key: str, per_key: int, total: int) -> None:
        """Make room in *table* (insertion ordered) for one more entry of *key*.

        A key may hold at most *per_key* entries (its oldest is dropped). When the table is
        full, the busiest network (IPv6 grouped by /48) is chosen, and within it the busiest
        address (IPv6 /64) loses its oldest entry: many /64s of one /48 cannot push out
        another network's sign-in, and an attacker sharing the athlete's /48 only evicts its
        own entries. Without a known address (``unknown``: an app built without the
        middleware) only the global limit applies.
        """
        if key == "unknown":
            per_key = total
        mine = [k for k, entry in table.items() if entry.key == key]
        while len(mine) >= per_key:
            del table[mine.pop(0)]
        while len(table) >= total:
            groups: dict[str, dict[str, int]] = {}
            for entry in table.values():
                keys = groups.setdefault(_eviction_group(entry.key), {})
                keys[entry.key] = keys.get(entry.key, 0) + 1
            group = max(groups, key=lambda g: sum(groups[g].values()))
            busiest = max(groups[group], key=lambda k: groups[group][k])
            del table[next(k for k, entry in table.items() if entry.key == busiest)]

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
                athlete, method = self._attach_grant(code.code, grant_id, code.client_id)
                token = self._issue_tokens(code.client_id, code.scopes, grant_id, code.resource, athlete, method)
        except _Refused as exc:
            raise TokenError("invalid_grant", str(exc)) from None
        logger.info("Issued tokens to client %s", _for_log(client.client_id))
        return token

    def _attach_grant(self, code: str, grant_id: str, client_id: str) -> tuple[str | None, str | None]:
        """The athlete and sign-in method of a code; multi-user mode: its grant record (inside :meth:`_state_change`).

        The athlete's Intervals.icu token is sealed for this grant. Intervals.icu applies the
        scopes of an athlete's latest sign-in to all of the athlete's tokens, so the other
        grants of the athlete get the new scopes as well. An athlete keeps at most
        ``OAUTH_MAX_GRANTS_PER_ATHLETE`` grants (the least recently used ones are revoked).
        """
        pending = self._code_grants.pop(code, None)
        if not self._config.multi_user:
            return ((pending.athlete_id or None), pending.method) if pending is not None else (None, None)
        if pending is None:
            raise _Refused("authorization code is not valid")
        now = self._now()
        self._limit_grants(pending.athlete_id)
        sealed = None
        if pending.kind == "athlete":
            vault = self._config.vault
            if vault is None or not pending.token:
                raise _Refused("authorization code is not valid")
            sealed = vault.seal(pending.token, grant_context(grant_id, pending.athlete_id))
            for other_id, other in list(self._grants.items()):
                if other.kind == "athlete" and same_athlete(other.athlete_id, pending.athlete_id):
                    self._grants[other_id] = Grant(
                        other.athlete_id, other.kind, other.method, other.client_id, other.created_at,
                        other.last_used_at, pending.intervals_scopes, other.sealed, other.persisted_use,
                    )
        self._grants[grant_id] = Grant(
            athlete_id=pending.athlete_id,
            kind=pending.kind,
            method=pending.method,
            client_id=client_id,
            created_at=now,
            last_used_at=now,
            intervals_scopes=pending.intervals_scopes,
            sealed=sealed,
            persisted_use=now,
        )
        return pending.athlete_id, pending.method

    def _limit_grants(self, athlete_id: str) -> None:
        """Make room for one more grant of *athlete_id* (per athlete and in total), least recently used first."""
        def by_use(grant_id: str) -> int:
            return self._grants[grant_id].last_used_at

        mine = sorted((gid for gid, g in self._grants.items() if same_athlete(g.athlete_id, athlete_id)), key=by_use)
        while mine and len(mine) >= self._config.max_grants_per_athlete:
            victim = mine.pop(0)
            logger.info("Athlete %s has too many connections; revoking the least recently used one", _for_log(athlete_id))
            self._revoke_grant(victim)
        while len(self._grants) >= MAX_GRANT_RECORDS:
            self._revoke_grant(min(self._grants, key=by_use))

    async def load_refresh_token(  # pylint: disable=too-many-return-statements
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        """Return the refresh token if it belongs to *client* and has not expired.

        A token that was already exchanged is accepted again for the grace period (a client
        that lost the response retries); presented later it revokes the whole grant, because
        then two parties hold tokens of one grant (RFC 9700, refresh token rotation).
        """
        self._sync_unlocked()
        self._purge_expired_tokens()
        digest = _digest(refresh_token)
        record = self._tokens.refresh.get(digest)
        if record is not None and not self._usable(record, self._grants.get(record.grant_id)):
            return None
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
        pending = self._tokens.rotated.get(digest)
        if pending is not None and not pending.committed:
            # A concurrent request is still writing this rotation: wait for it, so the same
            # answer is only given once it is on disk (a failed write undoes the rotation).
            async with self._state_lock:
                pass
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
                    record.client_id, _refresh_scopes(record.scopes, scopes), record.grant_id, record.resource,
                    record.athlete_id, record.method,
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

    def _sync_unlocked(self) -> None:
        """Pick up a change of the state file by another process unless a write is in progress."""
        if not self._state_lock.locked():
            self._sync_from_disk()

    async def load_access_token(self, token: str) -> AccessToken | None:
        """Return the access token metadata used by the bearer middleware (audience-checked).

        In the multi-user mode the grant id is the token's ``subject``: the SDK binds a
        session to it, so one connection cannot use another connection's session.
        """
        self._sync_unlocked()
        self._purge_expired_tokens()
        record = self._tokens.access.get(_digest(token))
        if record is None:
            return None
        if record.resource and not self._config.same_origin(record.resource):
            return None
        multi = self._config.multi_user
        if not self._usable(record, self._grants.get(record.grant_id)):
            return None
        return AccessToken(
            token=token,
            client_id=record.client_id,
            scopes=record.scopes,
            expires_at=record.expires_at,
            resource=record.resource,
            subject=record.grant_id if multi else None,
        )

    # ----- multi-user mode: the credential of a connection ----------------- #

    def _owner_credential(self, grant_id: str | None) -> Credential:
        config = self._config
        if not (config.owner_athlete and config.owner_api_key):
            raise CredentialError(
                "This connection belongs to the server owner, but the server has no owner API key (ATHLETE_ID and "
                "API_KEY). Disconnect and reconnect with 'Continue with Intervals.icu'."
            )
        return Credential(config.owner_athlete, "apikey", config.owner_api_key, grant_id, None, owner=True)

    async def connection_credential(self, token: str) -> Credential:
        """The Intervals.icu credential of the connection holding access *token* (multi-user mode).

        Owner grants use the owner's API key, athlete grants their own sealed Intervals.icu token.
        Fail closed: a token without a grant record is refused, never served as the owner's.
        Raises :class:`CredentialError` with a message for the client when the connection cannot
        be served (revoked, not allowed, unreadable token).
        """
        record = self._tokens.access.get(_digest(token))
        if record is None or record.expires_at <= self._clock():
            raise CredentialError("This connection's access token is no longer valid. Reconnect the server in your MCP client.")
        grant = self._grants.get(record.grant_id)
        if grant is None or not self._usable(record, grant):
            raise CredentialError(
                "This connection can no longer be used on this server. Disconnect and reconnect the server in your MCP client."
            )
        if grant.kind == "owner":
            credential = self._owner_credential(record.grant_id)
        else:
            credential = await self._athlete_credential(record.grant_id, grant)
        await self._touch(record.grant_id)
        return credential

    async def _athlete_credential(self, grant_id: str, grant: Grant) -> Credential:
        vault = self._config.vault
        try:
            if vault is None or grant.sealed is None:
                raise VaultError("no stored token")
            payload, key_index = vault.open_with_key(grant.sealed, grant_context(grant_id, grant.athlete_id))
        except VaultError as exc:
            logger.warning("Stored Intervals.icu token of athlete %s cannot be opened: %s", _for_log(grant.athlete_id), exc)
            raise CredentialError(
                "The stored Intervals.icu sign-in of this connection cannot be read (the server's token key changed). "
                "Disconnect and reconnect the server in your MCP client."
            ) from None
        expires_at = payload.get("expires_at")
        if isinstance(expires_at, (int, float)) and not isinstance(expires_at, bool) and expires_at <= self._clock() + 60:
            payload = await self._refresh_athlete_token(grant_id, grant, payload)
        elif key_index:
            await self._reseal(grant_id, payload)  # opened with an older key: rotate it to the first key
        access = payload.get("access_token")
        if not isinstance(access, str) or not access:
            raise CredentialError("The stored Intervals.icu sign-in of this connection is incomplete. Reconnect the server.")
        return Credential(
            canonical_athlete_id(grant.athlete_id),
            "bearer",
            access,
            grant_id,
            frozenset(grant.intervals_scopes),
            owner=self._config.is_owner(grant.athlete_id),
        )

    async def _refresh_athlete_token(self, grant_id: str, grant: Grant, payload: dict[str, Any]) -> dict[str, Any]:
        """Renew an expiring Intervals.icu token with its refresh token (only if Intervals.icu issued one)."""
        refresh_token = payload.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise CredentialError(
                "The Intervals.icu sign-in of this connection has expired. Disconnect and reconnect the server in your MCP client."
            )
        try:
            answer = await self._intervals_refresh(refresh_token)
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            logger.warning("Intervals.icu token refresh for athlete %s failed: %s", _for_log(grant.athlete_id), type(exc).__name__)
            raise CredentialError(
                "Intervals.icu did not renew the sign-in of this connection. Disconnect and reconnect the server in your MCP client."
            ) from None
        renewed = _token_payload(answer, self._now(), previous=payload)
        if renewed is None:
            raise CredentialError("Intervals.icu did not renew the sign-in of this connection. Reconnect the server.")
        await self._reseal(grant_id, renewed)
        return renewed

    async def _reseal(self, grant_id: str, payload: dict[str, Any]) -> None:
        """Store *payload* sealed with the first key (after a refresh or a key rotation)."""
        vault = self._config.vault
        assert vault is not None  # checked by the caller
        try:
            async with self._state_change():
                current = self._grants.get(grant_id)
                if current is not None:
                    self._grants[grant_id] = Grant(
                        current.athlete_id, current.kind, current.method, current.client_id, current.created_at,
                        current.last_used_at, current.intervals_scopes,
                        vault.seal(payload, grant_context(grant_id, current.athlete_id)), current.persisted_use,
                    )
        except (OSError, ValueError) as exc:
            logger.warning("Could not store the re-sealed token of a connection: %s", _one_line(exc))

    async def _touch(self, grant_id: str) -> None:
        """Remember the grant's last use; written to the file at most every LAST_USE_WRITE_INTERVAL."""
        grant = self._grants.get(grant_id)
        if grant is None:
            return
        now = self._now()
        grant.last_used_at = now
        if now - grant.persisted_use < LAST_USE_WRITE_INTERVAL:
            return
        grant.persisted_use = now
        try:
            async with self._state_change():
                self._purge_expired_tokens()  # also applies OAUTH_TOKEN_RETENTION_DAYS
        except (OSError, ValueError) as exc:
            logger.warning("Could not record the last use of a connection: %s", _one_line(exc))

    def grant_overview(self) -> dict[str, Any]:
        """Counts for ``--doctor`` (no ids, no tokens): grants by kind and unreadable stored tokens."""
        vault = self._config.vault
        unreadable = old_key = 0
        for grant_id, grant in self._grants.items():
            if grant.kind != "athlete":
                continue
            try:
                if vault is None or grant.sealed is None:
                    raise VaultError("missing")
                old_key += 1 if vault.open_with_key(grant.sealed, grant_context(grant_id, grant.athlete_id))[1] else 0
            except VaultError:
                unreadable += 1
        return {
            "athletes": len({normalize_athlete_id(g.athlete_id) for g in self._grants.values() if g.kind == "athlete"}),
            "athlete_grants": sum(1 for g in self._grants.values() if g.kind == "athlete"),
            "owner_grants": sum(1 for g in self._grants.values() if g.kind == "owner"),
            "legacy_grants": len(self._awaiting_adoption),
            "unreadable_tokens": unreadable,
            "old_key_tokens": old_key,
        }

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

    def complete_login(  # pylint: disable=too-many-arguments
        self,
        request_id: str,
        login_key: str,
        granted: tuple[str, ...] | None = None,
        *,
        method: str = "password",
        grant: PendingGrant | None = None,
    ) -> str:
        """Consume the pending request, mint a code and return the client redirect URL.

        The code carries who signed in: *grant* for an Intervals.icu sign-in, otherwise (password
        or API key, *method*) the owner. In the multi-user mode it becomes the grant record; in the
        single-user mode the athlete is recorded with the refresh token.
        """
        if grant is None:
            # A password or API-key sign-in is the owner's: it uses the owner's API key.
            owner = self._config.owner_athlete
            if self._config.multi_user and (method not in ("password", "apikey") or not owner or not self._owner_key_available()):
                raise LoginError("This sign-in cannot be used on this server.", 403)
            grant = PendingGrant(owner or "", "owner", method)
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
        self._code_grants[code] = grant
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

    def begin_intervals_login(
        self, request_id: str, granted: tuple[str, ...], login_key: str = "unknown", *, chats: bool = False
    ) -> tuple[str, str]:
        """Return (Intervals.icu authorize URL, browser binding value for a cookie).

        *chats*: the athlete ticked "activity comments" (multi-user mode, only when offered).
        """
        if self.pending_login(request_id) is None:
            raise LoginError("This sign-in link is invalid or has expired.")
        # Only the latest attempt of a request counts (the cookie of an earlier one is overwritten anyway).
        for state in [s for s, u in self._upstream.items() if u.request_id == request_id]:
            del self._upstream[state]
        self._evict_for_key(self._upstream, login_key, MAX_PENDING_PER_KEY, MAX_PENDING_LOGINS)
        state = secrets.token_urlsafe(32)
        browser = secrets.token_urlsafe(32)
        scope = self.intervals_scope_for(granted, chats=chats)
        self._upstream[state] = _UpstreamLogin(
            request_id=request_id,
            granted=granted,
            browser_digest=_digest(browser),
            expires_at=self._clock() + LOGIN_REQUEST_TTL,
            key=login_key,
            intervals_scope=scope,
        )
        query = urlencode(
            {
                "client_id": self._config.intervals_client_id or "",
                "redirect_uri": self._config.intervals_redirect_uri,
                "scope": scope,
                "state": state,
            }
        )
        return f"{self._config.intervals_authorize_url}?{query}", browser

    def intervals_scope_for(self, granted: tuple[str, ...], chats: bool = False) -> str:
        """The scope requested at Intervals.icu: in the multi-user mode exactly what the granted classes need.

        ``CHATS`` (activity comments; Intervals.icu then also allows reading private chats) only
        when the server offers it (``INTERVALS_OAUTH_OFFER_CHATS``) and the athlete ticked it.
        """
        if not self._config.multi_user:
            return self._config.intervals_scope
        include = chats and self._config.intervals_offer_chats
        return intervals_scopes_for(granted, self._config.intervals_exclude_areas, include_chats=include)

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
        allowed = (
            self._config.athlete_allowed(athlete_id) if self._config.multi_user else athlete_id in self._config.allowed_athletes
        )
        if not athlete_id or not allowed:
            self._pending.pop(upstream.request_id, None)
            self.record_login_failure(login_key)
            logger.warning("Intervals.icu athlete %s is not allowed on this server", _for_log(athlete_id or "(unknown)"))
            raise LoginError("This Intervals.icu account is not allowed to use this server.", 403)
        if self._config.multi_user:
            grant = self._grant_from_sign_in(payload, athlete_id, upstream)
        else:
            athlete = canonical_athlete_id(athlete_id)
            grant = PendingGrant(athlete, "owner" if self._config.is_owner(athlete) else "athlete", "intervals")
        logger.info("Intervals.icu sign-in confirmed for athlete %s", _for_log(athlete_id))
        return self.complete_login(upstream.request_id, login_key, upstream.granted, method="intervals", grant=grant)

    def _grant_from_sign_in(self, payload: dict[str, Any], athlete_id: str, upstream: _UpstreamLogin) -> PendingGrant:
        """Multi-user mode: the grant of an Intervals.icu sign-in.

        The owner (``ATHLETE_ID``) keeps using the server's API key when one is configured; its
        Intervals.icu token is not kept. Every other athlete's token is kept (sealed at the code
        exchange) with the scopes Intervals.icu granted.
        """
        athlete = canonical_athlete_id(athlete_id)
        requested = parse_intervals_scopes(upstream.intervals_scope)
        if self._config.is_owner(athlete) and self._owner_key_available():
            return PendingGrant(athlete, "owner", "intervals", tuple(sorted(requested)))
        token = _token_payload(payload, self._now())
        if token is None:
            self._pending.pop(upstream.request_id, None)
            raise LoginError("Intervals.icu did not return an access token. Please try again.", 502)
        granted = parse_intervals_scopes(payload.get("scope")) or requested
        return PendingGrant(athlete, "athlete", "intervals", tuple(sorted(granted)), token)

    async def _post_intervals_token(self, fields: dict[str, str]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            response = await client.post(
                self._config.intervals_token_url,
                data={
                    "client_id": self._config.intervals_client_id or "",
                    "client_secret": self._config.intervals_client_secret or "",
                    **fields,
                },
                headers={"Accept": "application/json"},
            )
        if response.status_code != 200:
            raise ValueError(f"token endpoint answered HTTP {response.status_code}")
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("unexpected token response")
        return data

    async def _exchange_intervals_code(self, code: str) -> dict[str, Any]:
        """POST the code to Intervals.icu (single-user mode: only the athlete id of the answer is used)."""
        return await self._post_intervals_token({"code": code})

    async def _refresh_intervals_token(self, refresh_token: str) -> dict[str, Any]:
        """Standard OAuth refresh at Intervals.icu (only used if Intervals.icu ever issues refresh tokens)."""
        return await self._post_intervals_token({"grant_type": "refresh_token", "refresh_token": refresh_token})


def _token_payload(answer: Any, now: int, previous: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    """What is sealed from an Intervals.icu token answer: the access token and, if issued, refresh token and expiry."""
    if not isinstance(answer, dict):
        return None
    access = answer.get("access_token")
    if not isinstance(access, str) or not access.strip():
        return None
    payload: dict[str, Any] = {"access_token": access.strip(), "token_type": str(answer.get("token_type") or "Bearer")}
    refresh = answer.get("refresh_token") or (previous or {}).get("refresh_token")
    if isinstance(refresh, str) and refresh:
        payload["refresh_token"] = refresh
    expires_in = answer.get("expires_in")
    if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool) and expires_in > 0:
        payload["expires_at"] = int(now + expires_in)
    return payload


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
    tenancy = tenancy_from_env(env)
    if mode == "none":
        if tenancy == "multi":
            raise ValueError("MCP_TENANCY=multi requires MCP_AUTH=oauth: every connection signs in with its own account")
        return {}
    if mode != "oauth":
        raise ValueError(f"MCP_AUTH must be 'none' or 'oauth', got {mode!r}")
    config = oauth_config_from_env(env)
    provider = SingleUserOAuthProvider(config)
    logger.info(
        "OAuth enabled; issuer %s, sign-in %s, state file %s, %s-user mode",
        config.issuer,
        "+".join(config.login_methods),
        config.state_file,
        config.tenancy,
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
    """CLI helpers: ``hash-password``, ``totp-secret``, ``token-key`` and ``grants``."""
    arguments = sys.argv[1:] if argv is None else list(argv)
    if arguments[:1] == ["grants"]:
        from intervals_mcp_server.auth_grants import grants_main  # pylint: disable=import-outside-toplevel

        return grants_main(arguments[1:])
    parser = argparse.ArgumentParser(
        prog="python -m intervals_mcp_server.auth",
        description="Helpers for the built-in OAuth authorization server.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("grants", help="list or remove connections (see: grants --help)")
    key_cmd = commands.add_parser("token-key", help="create a key for OAUTH_TOKEN_KEY / OAUTH_TOKEN_KEY_FILE (multi-user mode)")
    key_cmd.add_argument("--file", help="write the key to this new file (mode 0600) instead of printing it")
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
    args = parser.parse_args(arguments)

    if args.command == "token-key":
        from intervals_mcp_server.token_vault import generate_key, write_key_file  # pylint: disable=import-outside-toplevel

        if args.file:
            try:
                write_key_file(Path(args.file))
            except OSError as exc:
                parser.error(f"cannot create {args.file}: {exc.strerror or exc}")
            print(f"Key written to {args.file} (mode 0600). Set OAUTH_TOKEN_KEY_FILE={args.file}")
        else:
            print(f"OAUTH_TOKEN_KEY={generate_key()}")
        return 0
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
