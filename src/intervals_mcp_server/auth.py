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
``OAUTH_CLIENT_HOSTS``             hosts whose client metadata documents are accepted
                                   (default ``chatgpt.com,claude.ai,claude.com``; ``none``)
``OAUTH_REDIRECT_HOSTS``           hosts allowed as redirect URIs of dynamically registered
                                   clients (same default; ``*`` = any https host)
``OAUTH_DYNAMIC_REGISTRATION``     ``true`` (default) or ``false``
``OAUTH_PRIVATE_KEY_JWT``          advertise private_key_jwt client auth (default ``true``)
``OAUTH_STATE_FILE``               JSON file for clients and refresh tokens
``OAUTH_ACCESS_TOKEN_TTL`` / ``OAUTH_REFRESH_TOKEN_TTL`` / ``OAUTH_LOGIN_RATE_LIMIT``

Access tokens live in memory only; registered clients and refresh tokens are persisted
(as digests) so a restart does not disconnect clients.  Secrets are never logged.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import json
import logging
import os
import secrets
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

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

from intervals_mcp_server.auth_clients import ClientMetadataResolver, Fetcher, is_metadata_client_id
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
    "granted_classes",
    "hash_password",
    "install_login_routes",
    "normalize_athlete_id",
    "oauth_config_from_env",
    "oauth_from_env",
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

_STATE_VERSION = 1
_HASH_PREFIX = "pbkdf2_sha256"
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


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
    api_key_digest: str | None = None
    totp_secret: str | None = None

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
        if "apikey" not in self.login_methods or not self.api_key_digest:
            return False
        candidate = hashlib.sha256(api_key.strip().encode("utf-8")).hexdigest()
        return hmac.compare_digest(candidate, self.api_key_digest)

    def same_origin(self, url: str) -> bool:
        """True when *url* points at this server (scheme, host and port of the issuer)."""
        mine, other = urlsplit(self.public_url), urlsplit(url)
        return (mine.scheme, mine.hostname, mine.port) == (other.scheme, (other.hostname or "").lower(), other.port)


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
    raw = env.get(name)
    if raw is None or not raw.strip():
        return frozenset(DEFAULT_TRUSTED_CLIENT_HOSTS)
    value = raw.strip().lower()
    if value == "none":
        return frozenset()
    if value == "*":
        if not allow_any:
            raise ValueError(f"{name} does not accept '*'; list the hosts explicitly")
        return None
    return frozenset(h.strip() for h in value.split(",") if h.strip())


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
        api_key_digest=hashlib.sha256(api_key.encode("utf-8")).hexdigest() if "apikey" in methods else None,
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


@dataclass
class _UpstreamLogin:
    """A sign-in at Intervals.icu in progress (keyed by the OAuth ``state`` sent there)."""

    request_id: str
    granted: tuple[str, ...]
    browser_digest: str
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


class LoginError(Exception):
    """A sign-in could not be completed; ``status`` is the HTTP status for the page."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


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


def _redirect_uri_allowed(uri: str, hosts: frozenset[str] | None = None) -> bool:
    """https on an allowed host (``None`` = any host), or http on loopback."""
    parts = urlsplit(uri)
    if parts.scheme == "http":
        return parts.hostname in _LOOPBACK_HOSTS
    if parts.scheme != "https" or not parts.hostname or parts.fragment:
        return False
    return hosts is None or parts.hostname.lower() in hosts


def granted_classes(scopes: list[str] | None) -> set[str] | None:
    """Permission classes granted by token *scopes*; None = no per-token restriction.

    Tokens issued before permission scopes existed only carry ``mcp`` and keep the
    server-wide permissions (``MCP_PERMISSIONS``).
    """
    classes = {s[len(PERMISSION_SCOPE_PREFIX):] for s in scopes or [] if s.startswith(PERMISSION_SCOPE_PREFIX)}
    return classes or None


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
        self.metadata_clients = ClientMetadataResolver(
            config.client_hosts, " ".join(config.scopes_supported), fetch=fetch, clock=clock
        )
        self._intervals_exchange = intervals_exchange or self._exchange_intervals_code
        self._totp_last_counter = -1
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
                resource=raw.get("resource"),
            )
            known = record.client_id in self._clients or is_metadata_client_id(record.client_id)
            if record.expires_at > now and known:
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
                    **({"resource": record.resource} if record.resource else {}),
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
            client_id, scopes, now + self._config.refresh_token_ttl, grant_id, resource
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
        for state in [s for s, u in self._upstream.items() if u.expires_at <= now]:
            del self._upstream[state]
        while len(self._upstream) >= MAX_PENDING_LOGINS:
            del self._upstream[next(iter(self._upstream))]

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
                    f"redirect_uris must use https on an allowed host ({allowed}); "
                    "http is only allowed for localhost / 127.0.0.1",
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
        """Park the authorization request and send the athlete to the consent page."""
        if params.resource and not self._config.same_origin(params.resource):
            raise AuthorizeError("invalid_request", "the resource indicator does not name this server")
        self._purge_pending()
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
        """Rotate the refresh token and issue a new access token for the same grant."""
        record = self._tokens.refresh.pop(_digest(refresh_token.token), None)
        if record is None or record.client_id != client.client_id:
            raise TokenError("invalid_grant", "refresh token is not valid")
        logger.info("Refreshed tokens for client %s", client.client_id)
        return self._issue_tokens(record.client_id, scopes or record.scopes, record.grant_id, record.resource)

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
        bucket = self._tokens.refresh if isinstance(token, RefreshToken) else self._tokens.access
        record = bucket.get(_digest(token.token))
        if record is None:
            return
        logger.info("Revoked tokens of client %s", record.client_id)
        self._revoke_grant(record.grant_id)

    # ----- consent / sign-in support -------------------------------------- #

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

    def verify_api_key(self, api_key: str) -> bool:
        """Constant-time check of a submitted Intervals.icu API key."""
        return self._config.verify_api_key(api_key)

    @property
    def totp_required(self) -> bool:
        """True when the password and API-key sign-ins need an authenticator code."""
        return self._config.totp_secret is not None

    def verify_second_factor(self, code: str) -> bool:
        """Accept an authenticator code once (no replay); always True without TOTP."""
        secret = self._config.totp_secret
        if secret is None:
            return True
        counter = match_counter(secret, code or "", self._clock())
        if counter is None or counter <= self._totp_last_counter:
            return False
        self._totp_last_counter = counter
        return True

    def grant_for(self, pending: _PendingLogin, chosen: list[str] | None) -> tuple[str, ...]:
        """Permission classes to grant: ``read`` plus the chosen offered classes."""
        picked = set(chosen or []) | {"read"}
        return tuple(p for p in pending.offered if p in picked) or ("read",)

    def complete_login(self, request_id: str, login_key: str, granted: tuple[str, ...] | None = None) -> str:
        """Consume the pending request, mint a code and return the client redirect URL."""
        pending = self._pending.pop(request_id)
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
            "Sign-in succeeded; issuing authorization code to client %s (%s)", pending.client_id, ", ".join(classes)
        )
        return self._redirect(pending, code=code)

    def deny_login(self, request_id: str) -> str:
        """Consume the pending request and return the access_denied redirect URL."""
        pending = self._pending.pop(request_id)
        logger.info("Authorization denied for client %s", pending.client_id)
        return self._redirect(pending, error="access_denied", error_description="The athlete denied the request")

    # ----- sign-in with Intervals.icu -------------------------------------- #

    def begin_intervals_login(self, request_id: str, granted: tuple[str, ...]) -> tuple[str, str]:
        """Return (Intervals.icu authorize URL, browser binding value for a cookie)."""
        if self.pending_login(request_id) is None:
            raise LoginError("This sign-in link is invalid or has expired.")
        state = secrets.token_urlsafe(32)
        browser = secrets.token_urlsafe(32)
        self._upstream[state] = _UpstreamLogin(
            request_id=request_id,
            granted=granted,
            browser_digest=_digest(browser),
            expires_at=self._clock() + LOGIN_REQUEST_TTL,
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
            logger.warning("Intervals.icu athlete %s is not allowed on this server", athlete_id or "(unknown)")
            raise LoginError("This Intervals.icu account is not allowed to use this server.", 403)
        logger.info("Intervals.icu sign-in confirmed for athlete %s", athlete_id)
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


def auth_status_from_env(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Describe the auth configuration for a status tool; never includes secrets."""
    env = os.environ if environ is None else environ
    if _auth_mode(env) != "oauth":
        return {"mode": "none", "issuer": None, "state_file": None}
    try:
        methods: list[str] = list(_login_methods(env))
    except ValueError:
        methods = ["invalid"]
    athletes = env.get("OAUTH_ALLOWED_ATHLETES", "").strip() or env.get("ATHLETE_ID", "").strip()
    return {
        "mode": "oauth",
        "issuer": env.get("MCP_PUBLIC_URL", "").strip() or None,
        "state_file": str(Path(env.get("OAUTH_STATE_FILE", "").strip() or DEFAULT_STATE_FILE)),
        "login": methods,
        "intervals_app": "configured" if env.get("INTERVALS_OAUTH_CLIENT_ID", "").strip() else "not configured",
        "allowed_athletes": sorted(normalize_athlete_id(a) for a in athletes.split(",") if a.strip()),
        "username": env.get("OAUTH_USERNAME", "").strip() or DEFAULT_USERNAME,
        "password_source": (
            "hash"
            if env.get("OAUTH_PASSWORD_HASH", "").strip()
            else "plain" if env.get("OAUTH_PASSWORD") else "missing"
        ),
        "second_factor": "totp" if env.get("OAUTH_TOTP_SECRET", "").strip() else "none",
        "dynamic_registration": env.get("OAUTH_DYNAMIC_REGISTRATION", "true").strip().lower() not in _FALSE,
    }


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
