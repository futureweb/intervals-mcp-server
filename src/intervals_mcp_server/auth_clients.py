"""OAuth client identification beyond dynamic registration.

* **Client ID Metadata Documents (CIMD).**  MCP clients may use an HTTPS URL as their
  ``client_id`` (MCP authorization spec 2026-07-28, preferred over dynamic client
  registration).  The authorization server fetches the JSON document behind the URL,
  checks that its ``client_id`` equals the URL and only accepts the ``redirect_uris``
  listed there.  ChatGPT identifies itself as ``https://chatgpt.com/oauth/client.json``.
  Documents are only fetched from an allowlist of hosts or exact document URLs (no SSRF
  to arbitrary URLs, no query strings), without redirects, with a size limit and a short
  timeout, and cached in a bounded cache; cache misses share a global fetch budget, and a
  known document is served a while longer when its host is unreachable.  The redirect URIs
  of a document must stay on the document's host, another allowlisted host or loopback.

* **private_key_jwt client authentication (RFC 7523).**  A CIMD client may authenticate
  at the token endpoint with a JWT signed by a key from the ``jwks_uri`` of its metadata
  document.  :class:`ClientAssertionMiddleware` verifies such an assertion before the
  MCP SDK's token handler runs (which only knows client secrets) and hands the request
  on as a public-client request.  A token request without an assertion is refused when
  the client's document declares ``private_key_jwt`` (``OAUTH_REQUIRE_PRIVATE_KEY_JWT``,
  default on); other requests without an assertion are passed through unchanged and
  PKCE still protects them.

The module never logs assertions, tokens or document contents beyond the client id.
"""

from __future__ import annotations

import functools
import json
import logging
import ssl
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

import anyio
import httpx
import jwt
from mcp.shared.auth import OAuthClientInformationFull
from starlette.types import ASGIApp, Message, Receive, Scope, Send

__all__ = [
    "ASSERTION_TYPE",
    "ClientAssertionMiddleware",
    "ClientAssertionVerifier",
    "ClientMetadataResolver",
    "allowlist_match",
    "is_metadata_client_id",
    "log_safe",
    "normalize_allow_entry",
]

logger = logging.getLogger(__name__)

ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
MAX_DOCUMENT_BYTES = 64 * 1024
FETCH_TIMEOUT = 5.0
MIN_CACHE_SECONDS = 60
MAX_CACHE_SECONDS = 3600
DEFAULT_CACHE_SECONDS = 300
NEGATIVE_CACHE_SECONDS = 60
MAX_ASSERTION_LIFETIME = 600
CLOCK_SKEW = 30
ASSERTION_ALGORITHMS = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384"]
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
MAX_LOGGED_CHARS = 120
MAX_CLIENT_ID_URL_CHARS = 512
# Bounded caches: unauthenticated /authorize and /token requests choose the client id.
MAX_CACHED_DOCUMENTS = 256
MAX_CACHED_JWKS = 64
# Cache misses (outbound fetches) allowed per window across all client ids.
FETCH_BUDGET = 30
FETCH_BUDGET_WINDOW = 60
# How long a known document is still served while its host cannot be reached.
MAX_STALE_SECONDS = 24 * 3600

Fetcher = Callable[[str], Awaitable[tuple[int, dict[str, str], bytes]]]


def log_safe(value: object, limit: int = MAX_LOGGED_CHARS) -> str:
    """A client-supplied value made safe for a log line: escaped and clipped."""
    text = str(value)
    if not text.isprintable():
        text = text.encode("unicode_escape").decode("ascii")
    return text if len(text) <= limit else f"{text[:limit]}…(+{len(text) - limit} chars)"


def is_metadata_client_id(client_id: str) -> bool:
    """True when *client_id* has the shape of a client metadata document URL."""
    return client_id.startswith("https://")


def normalize_allow_entry(entry: str) -> str:
    """``Host`` or ``host/path`` of an allowlist entry (host lower-cased, path kept as is).

    ``chatgpt.com`` allows every path on the host, ``chatgpt.com/oauth/client.json`` only that
    path (and paths below it). A leading ``https://`` is ignored. Raises ValueError for
    entries with a query string, a fragment or without a host.
    """
    text = entry.strip()
    if text.lower().startswith("https://"):
        text = text[len("https://"):]
    host, sep, path = text.partition("/")
    host = host.strip().lower()
    if not host or "?" in text or "#" in text or any(ch.isspace() for ch in text) or "@" in host:
        raise ValueError(f"invalid allowlist entry {entry!r}: use host or host/path")
    return f"{host}/{path}" if sep else host


def allowlist_match(host: str, path: str, entries: Iterable[str]) -> bool:
    """True when *host* and *path* match an entry from :func:`normalize_allow_entry`."""
    host = host.lower()
    for entry in entries:
        entry_host, sep, entry_path = entry.partition("/")
        if entry_host != host:
            continue
        if not sep:
            return True
        prefix = "/" + entry_path.rstrip("/")
        if prefix == "/" or path == prefix or path.startswith(prefix + "/"):
            return True
    return False


@functools.lru_cache(maxsize=1)
def _ssl_context() -> ssl.SSLContext:
    """One TLS context for all metadata fetches (loading the CA bundle costs ~10 ms each time)."""
    return httpx.create_ssl_context()


def _cache_seconds(headers: dict[str, str]) -> int:
    """Lifetime from ``Cache-Control: max-age``, clamped to sane bounds."""
    for part in headers.get("cache-control", "").split(","):
        name, _, value = part.strip().partition("=")
        if name.lower() == "max-age" and value.isdigit():
            return max(MIN_CACHE_SECONDS, min(MAX_CACHE_SECONDS, int(value)))
        if name.lower() in ("no-store", "no-cache"):
            return MIN_CACHE_SECONDS
    return DEFAULT_CACHE_SECONDS


async def _default_fetch(url: str) -> tuple[int, dict[str, str], bytes]:
    """GET *url* without redirects; read at most MAX_DOCUMENT_BYTES (+1 to detect overflow)."""
    async with httpx.AsyncClient(timeout=FETCH_TIMEOUT, follow_redirects=False, verify=_ssl_context()) as client:
        async with client.stream("GET", url, headers={"Accept": "application/json"}) as response:
            body = b""
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > MAX_DOCUMENT_BYTES:
                    break
            headers = {k.lower(): v for k, v in response.headers.items()}
            return response.status_code, headers, body


def _redirect_uri_ok(uri: Any) -> bool:
    if not isinstance(uri, str):
        return False
    parts = urlsplit(uri)
    if parts.fragment:
        return False
    if parts.scheme == "https":
        return bool(parts.hostname)
    return parts.scheme == "http" and parts.hostname in _LOOPBACK_HOSTS


@dataclass
class _CachedDocument:
    client: OAuthClientInformationFull | None
    expires_at: float
    jwks_uri: str | None = None
    auth_method: str | None = None
    # A failed fetch (network, 5xx) as opposed to a rejected document.
    transient: bool = False


class ClientMetadataResolver:  # pylint: disable=too-many-instance-attributes
    """Fetch, validate and cache client metadata documents from allowlisted hosts."""

    def __init__(
        self,
        allowed_hosts: Iterable[str],
        client_scope: str,
        fetch: Fetcher | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._entries = frozenset(normalize_allow_entry(h) for h in allowed_hosts if h.strip())
        self._hosts = frozenset(entry.partition("/")[0] for entry in self._entries)
        self._scope = client_scope
        self._fetch = fetch or _default_fetch
        self._clock = clock
        self._cache: OrderedDict[str, _CachedDocument] = OrderedDict()
        self._inflight: dict[str, anyio.Event] = {}
        self._fetches: list[float] = []

    @property
    def enabled(self) -> bool:
        """True when at least one host is allowed."""
        return bool(self._hosts)

    @property
    def fetch(self) -> Fetcher:
        """The fetcher used for documents (shared with the assertion verifier)."""
        return self._fetch

    @property
    def allowed_hosts(self) -> frozenset[str]:
        """Hosts whose metadata documents are accepted (on every path or on listed paths)."""
        return self._hosts

    def url_allowed(self, url: str) -> bool:
        """Shape and allowlist check of a metadata URL (no network access)."""
        if len(url) > MAX_CLIENT_ID_URL_CHARS or not url.isascii() or not url.isprintable() or " " in url:
            return False
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError:
            return False
        return (
            parts.scheme == "https"
            and bool(parts.hostname)
            and port in (None, 443)
            and not parts.username
            and not parts.password
            and not parts.query
            and not parts.fragment
            and parts.path not in ("", "/")
            and allowlist_match(parts.hostname or "", parts.path, self._entries)
        )

    def jwks_uri(self, client_id: str) -> str | None:
        """``jwks_uri`` of a cached, valid document (same host as the client id)."""
        cached = self._cache.get(client_id)
        return cached.jwks_uri if cached and cached.client else None

    def requires_assertion(self, client_id: str) -> bool:
        """True when the cached document declares ``private_key_jwt`` and names its keys."""
        cached = self._cache.get(client_id)
        return bool(cached and cached.client and cached.jwks_uri and cached.auth_method == "private_key_jwt")

    async def get(self, client_id: str) -> OAuthClientInformationFull | None:
        """Client information for a metadata URL, or None when unknown / invalid."""
        if not self.url_allowed(client_id):
            return None
        now = self._clock()
        cached = self._cache.get(client_id)
        if cached and cached.expires_at > now:
            self._cache.move_to_end(client_id)
            return cached.client
        inflight = self._inflight.get(client_id)
        if inflight is not None:
            # Single flight: concurrent requests for one document share the fetch.
            await inflight.wait()
            cached = self._cache.get(client_id)
            return cached.client if cached else None
        stale = cached if cached and cached.client and now - cached.expires_at < MAX_STALE_SECONDS else None
        if not self._take_fetch_budget(now):
            logger.warning("Client metadata fetch budget exhausted; %s not fetched", log_safe(client_id))
            return stale.client if stale else None
        event = anyio.Event()
        self._inflight[client_id] = event
        try:
            entry = await self._load(client_id)
            if entry.client is None and entry.transient and stale is not None:
                logger.warning("Keeping the last good metadata document of %s for now", log_safe(client_id))
                entry = _CachedDocument(
                    stale.client, now + NEGATIVE_CACHE_SECONDS, stale.jwks_uri, stale.auth_method
                )
            self._store(client_id, entry)
        finally:
            self._inflight.pop(client_id, None)
            event.set()
        return entry.client

    def _take_fetch_budget(self, now: float) -> bool:
        self._fetches = [stamp for stamp in self._fetches if stamp > now - FETCH_BUDGET_WINDOW]
        if len(self._fetches) >= FETCH_BUDGET:
            return False
        self._fetches.append(now)
        return True

    def _store(self, client_id: str, entry: _CachedDocument) -> None:
        self._cache[client_id] = entry
        self._cache.move_to_end(client_id)
        if len(self._cache) <= MAX_CACHED_DOCUMENTS:
            return
        # Drop rejected / unreachable documents first, then the least recently used ones.
        for key in [k for k, v in self._cache.items() if v.client is None and k != client_id]:
            del self._cache[key]
            if len(self._cache) <= MAX_CACHED_DOCUMENTS:
                return
        while len(self._cache) > MAX_CACHED_DOCUMENTS:
            self._cache.popitem(last=False)

    async def _load(self, url: str) -> _CachedDocument:
        try:
            status, headers, body = await self._fetch(url)
        except (httpx.HTTPError, httpx.InvalidURL, OSError, ValueError) as exc:
            logger.warning("Client metadata document %s could not be fetched: %s", log_safe(url), type(exc).__name__)
            return _CachedDocument(None, self._clock() + NEGATIVE_CACHE_SECONDS, transient=True)
        problem, client, jwks_uri = self._validate(url, status, body)
        if problem:
            logger.warning("Client metadata document %s rejected: %s", log_safe(url), log_safe(problem))
            transient = status >= 500 or status == 429
            return _CachedDocument(None, self._clock() + NEGATIVE_CACHE_SECONDS, transient=transient)
        logger.info("Client metadata document %s accepted (%s)", log_safe(url), log_safe(client.client_name if client else ""))
        method = json.loads(body).get("token_endpoint_auth_method")
        return _CachedDocument(
            client, self._clock() + _cache_seconds(headers), jwks_uri, method if isinstance(method, str) else None
        )

    def _redirect_host_ok(self, uri: str, document_host: str) -> bool:
        """A document's redirect URI stays on its own host, another allowlisted host or loopback."""
        host = (urlsplit(uri).hostname or "").lower()
        return host in _LOOPBACK_HOSTS or host == document_host or host in self._hosts

    def _validate(  # pylint: disable=too-many-return-statements
        self, url: str, status: int, body: bytes
    ) -> tuple[str | None, OAuthClientInformationFull | None, str | None]:
        if status != 200:
            return f"HTTP {status}", None, None
        if len(body) > MAX_DOCUMENT_BYTES:
            return "document too large", None, None
        try:
            data = json.loads(body)
        except ValueError:
            return "not JSON", None, None
        if not isinstance(data, dict):
            return "not a JSON object", None, None
        if data.get("client_id") != url:
            return "client_id does not match the document URL", None, None
        redirect_uris = data.get("redirect_uris")
        if not isinstance(redirect_uris, list) or not redirect_uris or not all(_redirect_uri_ok(u) for u in redirect_uris):
            return "redirect_uris missing or not https", None, None
        document_host = (urlsplit(url).hostname or "").lower()
        if not all(self._redirect_host_ok(u, document_host) for u in redirect_uris):
            return "redirect_uris must stay on the client's host, an allowlisted host or loopback", None, None
        jwks_uri = data.get("jwks_uri")
        if jwks_uri is not None:
            jwks_parts = urlsplit(str(jwks_uri))
            if jwks_parts.scheme != "https" or (jwks_parts.hostname or "").lower() != (urlsplit(url).hostname or "").lower():
                return "jwks_uri must be https on the client's host", None, None
        name = data.get("client_name")
        try:
            client = OAuthClientInformationFull(
                client_id=url,
                client_name=name if isinstance(name, str) else urlsplit(url).hostname,
                redirect_uris=redirect_uris,
                # The SDK only knows client secrets; private_key_jwt is verified by
                # ClientAssertionMiddleware before the SDK sees the request.
                token_endpoint_auth_method="none",
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                scope=self._scope,
                client_uri=data.get("client_uri") if isinstance(data.get("client_uri"), str) else None,
            )
        except ValueError as exc:
            return f"invalid metadata ({type(exc).__name__})", None, None
        return None, client, str(jwks_uri) if jwks_uri else None


class ClientAssertionVerifier:  # pylint: disable=too-few-public-methods
    """Verify RFC 7523 client assertions of metadata-document clients."""

    def __init__(
        self,
        resolver: ClientMetadataResolver,
        audiences: Iterable[str],
        fetch: Fetcher | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._resolver = resolver
        self._audiences = sorted({a for a in audiences if a})
        self._fetch = fetch or resolver.fetch
        self._clock = clock
        self._jwks: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
        self._last_forced_refresh: OrderedDict[str, float] = OrderedDict()
        self._seen_jti: dict[str, float] = {}

    async def requires_assertion(self, client_id: str) -> bool:
        """True when *client_id* is an accepted metadata document that declares ``private_key_jwt``."""
        if await self._resolver.get(client_id) is None:
            return False
        return self._resolver.requires_assertion(client_id)

    async def _keys(self, jwks_uri: str, force: bool = False) -> dict[str, Any]:
        cached = self._jwks.get(jwks_uri)
        now = self._clock()
        if cached and cached[0] > now and not force:
            return cached[1]
        if force and now - self._last_forced_refresh.get(jwks_uri, 0) < MIN_CACHE_SECONDS and cached:
            return cached[1]
        self._last_forced_refresh[jwks_uri] = now
        self._last_forced_refresh.move_to_end(jwks_uri)
        while len(self._last_forced_refresh) > MAX_CACHED_JWKS:
            self._last_forced_refresh.popitem(last=False)
        status, headers, body = await self._fetch(jwks_uri)
        if status != 200 or len(body) > MAX_DOCUMENT_BYTES:
            raise ValueError(f"JWKS fetch returned HTTP {status}")
        data = json.loads(body)
        if not isinstance(data, dict) or not isinstance(data.get("keys"), list):
            raise ValueError("JWKS has no keys")
        self._jwks[jwks_uri] = (now + _cache_seconds(headers), data)
        self._jwks.move_to_end(jwks_uri)
        while len(self._jwks) > MAX_CACHED_JWKS:
            self._jwks.popitem(last=False)
        return data

    @staticmethod
    def _pick_key(jwks: dict[str, Any], kid: str | None, alg: str) -> jwt.PyJWK | None:
        for raw in jwks.get("keys", []):
            if not isinstance(raw, dict) or (kid and raw.get("kid") != kid) or raw.get("use", "sig") != "sig":
                continue
            try:
                return jwt.PyJWK(raw, algorithm=raw.get("alg") or alg)
            except (jwt.PyJWKError, jwt.InvalidKeyError, ValueError):
                continue
        return None

    def _remember_jti(self, jti: str, expires_at: float) -> bool:
        now = self._clock()
        for key in [k for k, exp in self._seen_jti.items() if exp <= now]:
            del self._seen_jti[key]
        if jti in self._seen_jti:
            return False
        self._seen_jti[jti] = expires_at
        return True

    async def verify(self, assertion: str, client_id_hint: str | None) -> str:  # pylint: disable=too-many-branches
        """Return the client id the assertion proves, or raise ValueError."""
        try:
            header = jwt.get_unverified_header(assertion)
            unverified = jwt.decode(assertion, options={"verify_signature": False})
        except jwt.PyJWTError as exc:
            raise ValueError(f"malformed assertion ({type(exc).__name__})") from exc
        client_id = unverified.get("iss")
        if not isinstance(client_id, str) or unverified.get("sub") != client_id:
            raise ValueError("assertion iss and sub must both be the client id")
        if client_id_hint and client_id_hint != client_id:
            raise ValueError("assertion does not belong to the client_id of the request")
        alg = header.get("alg")
        if alg not in ASSERTION_ALGORITHMS:
            raise ValueError(f"unsupported assertion algorithm {alg!r}")
        if await self._resolver.get(client_id) is None:
            raise ValueError("assertion issuer is not an accepted client metadata document")
        jwks_uri = self._resolver.jwks_uri(client_id)
        if not jwks_uri:
            raise ValueError("client metadata document has no jwks_uri")
        try:
            key = self._pick_key(await self._keys(jwks_uri), header.get("kid"), alg)
            if key is None:
                key = self._pick_key(await self._keys(jwks_uri, force=True), header.get("kid"), alg)
        except (httpx.HTTPError, httpx.InvalidURL, OSError, ValueError) as exc:
            raise ValueError(f"client keys unavailable ({type(exc).__name__})") from exc
        if key is None:
            raise ValueError("no matching signing key in the client's JWKS")
        try:
            claims = jwt.decode(
                assertion,
                key=key,
                algorithms=[alg],
                audience=self._audiences,
                issuer=client_id,
                leeway=CLOCK_SKEW,
                options={"require": ["exp", "iss", "sub", "aud"]},
            )
        except jwt.PyJWTError as exc:
            raise ValueError(f"assertion rejected ({type(exc).__name__})") from exc
        now = self._clock()
        exp = float(claims["exp"])
        if exp - now > MAX_ASSERTION_LIFETIME + CLOCK_SKEW:
            raise ValueError("assertion lifetime is too long")
        jti = claims.get("jti")
        if jti is not None and not self._remember_jti(f"{client_id}|{jti}", exp + CLOCK_SKEW):
            raise ValueError("assertion was already used")
        return client_id


class ClientAssertionMiddleware:  # pylint: disable=too-few-public-methods
    """ASGI wrapper for ``/token`` and ``/revoke`` that verifies private_key_jwt assertions.

    A request carrying ``client_assertion_type=...jwt-bearer`` is verified; on success
    the assertion fields are removed and ``client_id`` is set, so the SDK handler sees a
    public-client request.  A failed verification is answered with ``invalid_client``.
    With *require_declared* a request without an assertion is refused when the client's
    metadata document declares ``token_endpoint_auth_method: private_key_jwt``: its
    refresh tokens are then useless without the client's private key.
    """

    def __init__(self, app: ASGIApp, verifier: ClientAssertionVerifier, require_declared: bool = False) -> None:
        self.app = app
        self.verifier = verifier
        self.require_declared = require_declared

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return
        body = b""
        more = True
        while more:
            message = await receive()
            if message["type"] != "http.request":
                break
            body += message.get("body", b"")
            more = message.get("more_body", False)
            if len(body) > MAX_DOCUMENT_BYTES:
                await _json_error(send, 413, "invalid_request", "request body too large")
                return
        fields = parse_qsl(body.decode("utf-8", "replace"), keep_blank_values=True)
        form = dict(fields)
        if form.get("client_assertion_type") == ASSERTION_TYPE or "client_assertion" in form:
            if form.get("client_assertion_type") != ASSERTION_TYPE or not form.get("client_assertion"):
                await _json_error(send, 400, "invalid_request", "unsupported client assertion")
                return
            try:
                client_id = await self.verifier.verify(form["client_assertion"], form.get("client_id"))
            except ValueError as exc:
                logger.warning("Client assertion rejected: %s", log_safe(exc))
                await _json_error(send, 401, "invalid_client", "client authentication failed")
                return
            logger.info(
                "Client assertion verified for client %s (%s)",
                log_safe(client_id),
                log_safe(form.get("grant_type") or scope.get("path", "")),
            )
            fields = [(k, v) for k, v in fields if k not in ("client_assertion", "client_assertion_type", "client_id")]
            fields.append(("client_id", client_id))
            body = urlencode(fields).encode("utf-8")
        elif self.require_declared and await self._assertion_required(form.get("client_id", "")):
            logger.warning(
                "Token request of client %s without the private_key_jwt assertion its metadata document declares; refused",
                log_safe(form.get("client_id", "")),
            )
            await _json_error(send, 401, "invalid_client", "client authentication with private_key_jwt is required")
            return
        await self.app(scope, _replay(body, receive), send)

    async def _assertion_required(self, client_id: str) -> bool:
        return is_metadata_client_id(client_id) and await self.verifier.requires_assertion(client_id)


def _replay(body: bytes, original: Receive) -> Receive:
    """Hand the (possibly rewritten) body to the app once, then defer to the real channel."""
    sent = False

    async def receive() -> Message:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await original()

    return receive


async def _json_error(send: Send, status: int, error: str, description: str) -> None:
    payload = json.dumps({"error": error, "error_description": description}).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"cache-control", b"no-store"),
                (b"content-length", str(len(payload)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})
