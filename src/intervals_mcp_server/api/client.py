"""
API client for Intervals.icu MCP Server.

This module handles all HTTP communication with the Intervals.icu API,
including request management, error handling, and client lifecycle.
"""

from json import JSONDecodeError
import asyncio
import json
import logging
import math
import os
import sys
import time
from collections.abc import Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, NamedTuple
from urllib.parse import quote

import httpx  # pylint: disable=import-error
from mcp.server.fastmcp import FastMCP  # pylint: disable=import-error

from intervals_mcp_server.config import get_config

logger = logging.getLogger("intervals_icu_mcp_server")

# Transient statuses that are retried with a short back-off (Retry-After is honoured).
RETRY_STATUSES = {429, 500, 502, 503, 504}
# POST is not idempotent: after a 5xx the event may already exist, so a retry could create a
# duplicate. Only 429 (rejected before processing) is retried for POST.
NON_IDEMPOTENT_RETRY_STATUSES = {429}


def retry_statuses(method: str) -> set[int]:
    """Statuses retried for *method* (GET/PUT/DELETE are idempotent, POST is not)."""
    return NON_IDEMPOTENT_RETRY_STATUSES if method.upper() == "POST" else RETRY_STATUSES


MAX_ATTEMPTS = 3
MAX_RETRY_DELAY_S = 30.0
REQUEST_TIMEOUT_S = 30.0

# Limits of ONE tool call (all API requests it makes): a tool that loops over many activities
# or durations stops with a clear message instead of running for minutes and burning the
# athlete's rate limit. Overridable with MCP_TOOL_MAX_REQUESTS / MCP_TOOL_TIMEOUT_S.
DEFAULT_TOOL_MAX_REQUESTS = 300
DEFAULT_TOOL_TIMEOUT_S = 120.0


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    """Seconds to wait before the next attempt (Retry-After header or exponential back-off)."""
    header = response.headers.get("Retry-After") if hasattr(response, "headers") else None
    if header:
        try:
            return min(MAX_RETRY_DELAY_S, max(0.0, float(header)))
        except ValueError:
            pass
    return min(MAX_RETRY_DELAY_S, 1.5 * (2**attempt))


class LimitSetting(NamedTuple):
    """A tool-call limit from the environment: the value the server uses and what is wrong with the setting."""

    value: float
    error: str | None = None  # the setting is unusable; the default is used
    warning: str | None = None  # the setting is usable after a correction


def _shown(number: float) -> str:
    return str(int(number)) if float(number).is_integer() else str(number)


def limit_setting(name: str, default: float, *, integer: bool, environ: Mapping[str, str] | None = None) -> LimitSetting:
    """Parse MCP_TOOL_MAX_REQUESTS / MCP_TOOL_TIMEOUT_S like the server does (also used by --doctor).

    Empty: the default. Not a number, not finite (inf, nan) or not positive: the default, with an
    error. A request limit with decimals is cut to whole requests (with a warning); one below 1
    after cutting falls back to the default.
    """
    raw = (os.environ if environ is None else environ).get(name, "").strip()
    if not raw:
        return LimitSetting(default)
    kind = "a positive whole number" if integer else "a positive number"
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value <= 0 or (integer and int(value) < 1):
        return LimitSetting(default, error=f"{name} must be {kind}, got {raw!r}; the server uses the default {_shown(default)}")
    if integer and int(value) != value:
        return LimitSetting(int(value), warning=f"{name}={raw} is not a whole number; the server uses {int(value)}")
    return LimitSetting(int(value) if integer else value)


@dataclass
class CallLimits:
    """Request budget and deadline of one tool call."""

    max_requests: int
    timeout_s: float
    started: float
    requests: int = 0
    hit: str | None = None

    def remaining_s(self) -> float:
        """Seconds left until the deadline."""
        return self.timeout_s - (time.monotonic() - self.started)

    def admit(self) -> str | None:
        """Count one request, or say why it may not be sent."""
        if self.requests >= self.max_requests:
            self.hit = f"its limit of {self.max_requests} Intervals.icu API requests"
        elif self.remaining_s() < 1.0:
            self.hit = f"its time limit of {self.timeout_s:.0f} s"
        else:
            self.requests += 1
            return None
        return (
            f"Not sent: this tool call reached {self.hit}. Narrow the request (fewer activities, "
            "ids or durations, a shorter date range) and call the tool again."
        )


_CALL_LIMITS: ContextVar[CallLimits | None] = ContextVar("intervals_call_limits", default=None)
# Set while a write tool runs with dry_run=true: every request other than GET is refused here,
# so a dry run can never write, whatever the tool code does.
_READ_ONLY: ContextVar[str | None] = ContextVar("intervals_read_only", default=None)


@contextmanager
def read_only_requests(reason: str = "dry run") -> Iterator[None]:
    """Refuse every non-GET request made inside this block (used for dry runs)."""
    token = _READ_ONLY.set(reason)
    try:
        yield
    finally:
        _READ_ONLY.reset(token)


def remaining_requests() -> int | None:
    """Requests the current tool call may still send, or None outside a tool call."""
    limits = _CALL_LIMITS.get()
    return None if limits is None else max(0, limits.max_requests - limits.requests)


@contextmanager
def call_limits(max_requests: int | None = None, timeout_s: float | None = None) -> Iterator[CallLimits]:
    """Request budget and deadline for the API requests of one tool call.

    Nested use (a tool calling another tool) keeps the outer limits.
    """
    current = _CALL_LIMITS.get()
    if current is not None:
        yield current
        return
    limits = CallLimits(
        max_requests=int(max_requests or limit_setting("MCP_TOOL_MAX_REQUESTS", DEFAULT_TOOL_MAX_REQUESTS, integer=True).value),
        timeout_s=float(timeout_s or limit_setting("MCP_TOOL_TIMEOUT_S", DEFAULT_TOOL_TIMEOUT_S, integer=False).value),
        started=time.monotonic(),
    )
    token = _CALL_LIMITS.set(limits)
    try:
        yield limits
    finally:
        _CALL_LIMITS.reset(token)


# Create a single AsyncClient instance for all requests (lazily initialized)
# This can be monkeypatched via server.httpx_client for testing
httpx_client: httpx.AsyncClient | None = None  # pylint: disable=invalid-name


async def _get_httpx_client() -> httpx.AsyncClient:
    """
    Lazily create or reuse the shared httpx AsyncClient.

    The client may be closed by downstream transports between tool invocations,
    so we recreate it when necessary.

    This function checks server.httpx_client first (if available) to support
    test monkeypatching via server.httpx_client.
    """
    global httpx_client  # pylint: disable=global-statement  # noqa: PLW0603 - we intentionally manage the shared client here

    # Check for monkeypatched client in server module first (for test compatibility)
    # This allows tests to monkeypatch server.httpx_client and have it work
    try:
        server_module = sys.modules.get("intervals_mcp_server.server")
        if server_module and hasattr(server_module, "httpx_client"):
            server_client = server_module.httpx_client
            if server_client is not None and not server_client.is_closed:
                return server_client
    except (AttributeError, ImportError):
        pass

    # Use this module's httpx_client
    if httpx_client is None or httpx_client.is_closed:
        httpx_client = httpx.AsyncClient()
    return httpx_client


_ACTIVE_SESSIONS = 0


@asynccontextmanager
async def setup_api_client(_app: FastMCP):
    """
    Lifespan of one MCP session: the shared httpx client is closed when the LAST session ends.

    FastMCP runs this lifespan once per session (SSE connection or streamable HTTP session),
    so closing on every exit would break the requests of other sessions still in flight.

    Args:
        _app (FastMCP): The MCP server application instance.
    """
    global _ACTIVE_SESSIONS  # pylint: disable=global-statement  # noqa: PLW0603 - process-wide session counter
    _ACTIVE_SESSIONS += 1
    try:
        yield
    finally:
        _ACTIVE_SESSIONS -= 1
        if _ACTIVE_SESSIONS <= 0:
            _ACTIVE_SESSIONS = 0
            await _close_shared_clients()


async def _close_shared_clients() -> None:
    """Close the module-level client (and a monkeypatched server client in tests)."""
    if httpx_client and not httpx_client.is_closed:
        await httpx_client.aclose()
    try:
        server_module = sys.modules.get("intervals_mcp_server.server")
        server_client = getattr(server_module, "httpx_client", None) if server_module else None
        if server_client is not None and not server_client.is_closed:
            await server_client.aclose()
    except (AttributeError, ImportError):
        pass


_STATUS_HINTS: dict[int, str] = {
    HTTPStatus.UNAUTHORIZED: "Please check your API key.",
    HTTPStatus.FORBIDDEN: "You may not have permission to access this resource.",
    HTTPStatus.NOT_FOUND: "The requested endpoint or ID doesn't exist.",
    HTTPStatus.UNPROCESSABLE_ENTITY: "The server couldn't process the request (invalid parameters or unsupported operation).",
    HTTPStatus.TOO_MANY_REQUESTS: "Too many requests in a short time period.",
    HTTPStatus.INTERNAL_SERVER_ERROR: "The Intervals.icu server encountered an internal error.",
    HTTPStatus.BAD_GATEWAY: "The Intervals.icu server or a proxy in front of it did not answer properly.",
    HTTPStatus.SERVICE_UNAVAILABLE: "The Intervals.icu server might be down or undergoing maintenance.",
    HTTPStatus.GATEWAY_TIMEOUT: "The Intervals.icu server did not answer in time.",
}
_MAX_REASON_CHARS = 300


def _get_error_message(error_code: int, reason: str | None = None) -> str:
    """User-friendly message for an HTTP status, with the API's own reason when it gave one."""
    try:
        status = HTTPStatus(error_code)
        text = f"{status.value} {status.phrase}"
    except ValueError:
        text = f"HTTP {error_code}"
    hint = _STATUS_HINTS.get(error_code)
    if hint:
        text += f": {hint}"
    if reason:
        text += f" Intervals.icu says: {reason}"
    return text


def _redact(text: str, secrets: tuple[str | None, ...]) -> str:
    """Remove secret values (the API key) from text that is shown to the client or logged."""
    for secret in secrets:
        if secret and len(secret) >= 4:
            text = text.replace(secret, "[redacted]")
    return text


def _api_reason(response: Any, secrets: tuple[str | None, ...] = ()) -> str | None:  # pylint: disable=too-many-return-statements
    """Short reason from an error response body ({"error": "..."} or plain text; never HTML)."""
    try:
        content = getattr(response, "content", b"") or b""
        if not content:
            return None
        try:
            body = response.json()
        except (JSONDecodeError, ValueError):
            body = None
        if isinstance(body, dict):
            reason = body.get("error") or body.get("message") or body.get("detail")
            if isinstance(reason, bool) or reason is None:
                return None
            text = str(reason)
        elif body is None:
            text = str(getattr(response, "text", "") or "")
            if "<html" in text[:500].lower() or "<!doctype" in text[:500].lower():
                return None
        else:
            return None
    except (AttributeError, TypeError, UnicodeDecodeError):
        return None
    text = " ".join(text.split())
    if not text:
        return None
    text = _redact(text, secrets)
    return text if len(text) <= _MAX_REASON_CHARS else text[: _MAX_REASON_CHARS - 3] + "..."


def _prepare_request_config(
    url: str,
    api_key: str | None,
    method: str,
) -> tuple[str, httpx.BasicAuth, dict[str, str], str | None]:
    """Prepare request configuration including headers, auth, and URL.

    Returns:
        Tuple of (full_url, auth, headers, error_message).
        error_message is None if configuration is valid.
    """
    config = get_config()
    headers = {"User-Agent": config.user_agent, "Accept": "application/json"}

    if method in ["POST", "PUT"]:
        headers["Content-Type"] = "application/json"

    # Use provided api_key or fall back to global API_KEY
    key_to_use = api_key if api_key is not None else config.api_key
    if not key_to_use:
        logger.error("No API key provided for request to: %s", url)
        return (
            "",
            httpx.BasicAuth("", ""),
            {},
            "API key is required. Set API_KEY in the server environment",
        )

    auth = httpx.BasicAuth("API_KEY", key_to_use)
    full_url = f"{config.intervals_api_base_url}{url}"
    return full_url, auth, headers, None


# Path segments of API URLs are built from tool arguments (activity, event, workout ids).
# Only plain identifier characters are allowed, so an argument such as "../athlete/i1" or
# "1?oldest=2000-01-01" cannot reach another endpoint (httpx resolves dot segments and a
# "?" would start a query string).
_SEGMENT_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-.,~:")
# Fields that must never reach a tool output (e.g. the raw athlete object carries the key).
SECRET_FIELDS = frozenset({"icu_api_key", "api_key", "apiKey", "password", "access_token", "refresh_token", "client_secret", "secret"})


def unsafe_segment_reason(value: Any) -> str | None:
    """Why *value* cannot be used as ONE path segment (an id), or None when it can."""
    text = str(value)
    if text in ("", ".", ".."):
        return "empty or dot path segment"
    if not set(text) <= _SEGMENT_CHARS:
        return "invalid characters in an identifier"
    return None


def seg(value: Any) -> str:
    """One URL path segment made from an identifier (activity, event, workout, athlete id ...).

    Use it wherever an id is formatted into an API path: ``f"/activity/{seg(activity_id)}/streams"``.
    Characters that are not plain identifier characters are percent-encoded, so a "/" or "?" in
    an argument cannot add a sub-path or a query string; make_intervals_request then refuses the
    request ("Invalid identifier") because "%" is not an identifier character.
    """
    return quote(str(value), safe=",:~")


def unsafe_path_reason(url: str) -> str | None:
    """Why *url* is not a safe API path, or None when every segment is a plain identifier."""
    if not url.startswith("/"):
        return "path must start with '/'"
    for segment in url[1:].split("/"):
        reason = unsafe_segment_reason(segment)
        if reason:
            return reason
    return None


def redact_secret_fields(value: Any) -> Any:
    """Recursively drop secret fields from API data before it reaches a tool."""
    if isinstance(value, dict):
        return {k: redact_secret_fields(v) for k, v in value.items() if k not in SECRET_FIELDS}
    if isinstance(value, list):
        return [redact_secret_fields(v) for v in value]
    return value


def _parse_response(
    response: httpx.Response, full_url: str
) -> dict[str, Any] | list[dict[str, Any]]:
    """Parse HTTP response and return JSON data or error dict.

    The status is checked first, so an HTML or plain-text error page (e.g. a 502 from a
    proxy) keeps its status code instead of becoming "Invalid JSON".

    Returns:
        Parsed JSON response or error dict.
    """
    status = getattr(response, "status_code", 200)
    if isinstance(status, int) and status >= 400:
        response.raise_for_status()
    try:
        response_data = response.json() if response.content else {}
    except JSONDecodeError:
        logger.error("Invalid JSON in response from: %s", full_url)
        return {"error": True, "status_code": status, "message": f"Invalid JSON in response (HTTP {status})"}
    response.raise_for_status()
    return redact_secret_fields(response_data)


def _transport_error(error: Exception, method: str) -> tuple[str, bool]:
    """Readable message for a transport error (timeouts, dropped connections) and whether
    the request may nevertheless have been applied by Intervals.icu."""
    detail = str(error).strip()
    text = f"Request error ({type(error).__name__})" + (f": {detail}" if detail else "")
    not_sent = isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))
    maybe_applied = method != "GET" and not not_sent
    if maybe_applied:
        consequence = "nothing is created twice" if method == "POST" else "you know what is left"
        text += (
            ". The request may still have reached Intervals.icu and been applied; check the current "
            f"state (e.g. get_events or the activity) before retrying, so {consequence}."
        )
    return text, maybe_applied


def _refusal(url: str, method: str) -> dict[str, Any] | None:
    """Error result for a request that must not be sent: an unsafe path, or a write during a dry run."""
    reason = unsafe_path_reason(url)
    if reason:
        logger.warning("Rejected API path built from tool arguments (%s)", reason)
        return {"error": True, "message": f"Invalid identifier: {reason}. Use the plain id (e.g. i123456789 or 123456)."}
    read_only = _READ_ONLY.get()
    if read_only and method != "GET":
        logger.warning("%s %s refused during a %s", method, url, read_only)
        return {"error": True, "read_only": True, "message": f"Not sent: {method} requests are refused during a {read_only}; nothing was written."}
    return None


async def make_intervals_request(  # pylint: disable=too-many-locals,too-many-statements,too-many-return-statements
    url: str,
    api_key: str | None = None,
    params: dict[str, Any] | None = None,
    method: str = "GET",
    data: dict[str, Any] | list[dict[str, Any]] | None = None,
) -> dict[str, Any] | list[dict[str, Any]]:
    """
    Make a request to the Intervals.icu API with proper error handling.

    Retries: HTTP 429/5xx for GET/PUT/DELETE, only 429 for POST; transport errors
    (timeouts, dropped connections) only for GET. Inside a tool call the request budget
    and deadline of the call apply (see call_limits); during a dry run (read_only_requests)
    only GET requests are sent.

    Args:
        url (str): The API endpoint path (e.g., '/athlete/{id}/activities').
        api_key (str | None): Optional API key to use for authentication. Defaults to the global API_KEY.
        params (dict[str, Any] | None): Optional query parameters for the request.
        method (str): HTTP method to use (GET, POST, etc.). Defaults to GET.
        data (dict[str, Any] | list[dict[str, Any]] | None): Optional JSON data (object or array) to send in the request body.

    Returns:
        dict[str, Any] | list[dict[str, Any]]: The parsed JSON response from the API, or an error dict
        ({"error": True, "message": ..., "status_code": ... when the API answered}).
    """
    method = method.upper()
    not_sent = _refusal(url, method)
    if not_sent:
        return not_sent

    # Prepare request configuration
    full_url, auth, headers, error_msg = _prepare_request_config(url, api_key, method)
    if error_msg:
        return {"error": True, "message": error_msg}
    secrets = (api_key, get_config().api_key)

    limits = _CALL_LIMITS.get()
    if limits is not None:
        refusal = limits.admit()
        if refusal:
            logger.warning("Tool call limit reached (%s); %s %s not sent", limits.hit, method, url)
            return {"error": True, "limit_reached": True, "message": refusal}

    def _timeout() -> float:
        if limits is None:
            return REQUEST_TIMEOUT_S
        return max(1.0, min(REQUEST_TIMEOUT_S, limits.remaining_s()))

    async def _send_request(client: httpx.AsyncClient) -> httpx.Response:
        if method in {"POST", "PUT"} and data is not None:
            body = json.dumps(data)
            # Bodies hold athlete data: log their size only (SECURITY.md).
            logger.debug("Request %s %s body: %d bytes", method, full_url, len(body))
            return await client.request(
                method=method,
                url=full_url,
                headers=headers,
                params=params,
                auth=auth,
                timeout=_timeout(),
                content=body,
            )
        return await client.request(
            method=method,
            url=full_url,
            headers=headers,
            params=params,
            auth=auth,
            timeout=_timeout(),
        )

    async def _send_with_client_recovery() -> httpx.Response:
        client = await _get_httpx_client()
        try:
            return await _send_request(client)
        except RuntimeError as runtime_error:
            # httpx closes the client when the underlying connection is severed;
            # recreate the shared client lazily and retry once.
            if "client has been closed" not in str(runtime_error).lower():
                raise
            logger.warning("HTTPX client was closed; creating a new instance for retries.")
            global httpx_client  # pylint: disable=global-statement  # noqa: PLW0603 - we intentionally manage the shared client here
            httpx_client = None
            client = await _get_httpx_client()
            return await _send_request(client)

    def _time_for_retry(delay: float) -> bool:
        return limits is None or limits.remaining_s() > delay + 1.0

    try:
        retryable = retry_statuses(method)
        response: httpx.Response | None = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                response = await _send_with_client_recovery()
            except httpx.TransportError as error:
                # Only GET is retried after a transport error: a POST/PUT/DELETE may already
                # have been applied when the connection dropped.
                delay = min(MAX_RETRY_DELAY_S, 1.5 * (2**attempt))
                if method != "GET" or attempt == MAX_ATTEMPTS - 1 or not _time_for_retry(delay):
                    raise
                logger.warning("%s for %s; retrying in %.1f s", type(error).__name__, url, delay)
                await asyncio.sleep(delay)
                continue
            status = getattr(response, "status_code", 200)
            if status not in retryable or attempt == MAX_ATTEMPTS - 1:
                break
            delay = _retry_delay(response, attempt)
            if not _time_for_retry(delay):
                break
            logger.warning("Intervals.icu answered %s for %s; retrying in %.1f s", status, url, delay)
            await asyncio.sleep(delay)

        assert response is not None  # the loop either set it or raised
        return _parse_response(response, full_url)
    except httpx.HTTPStatusError as e:
        return _handle_http_status_error(e, secrets)
    except httpx.RequestError as e:
        message, maybe_applied = _transport_error(e, method)
        message = _redact(message, secrets)
        logger.error("%s %s failed: %s", method, url, message)
        return {"error": True, "message": message, **({"maybe_applied": True} if maybe_applied else {})}
    except httpx.HTTPError as e:
        message = _redact(f"HTTP client error ({type(e).__name__}): {e}", secrets)
        logger.error("%s", message)
        return {"error": True, "message": message}


def _handle_http_status_error(e: httpx.HTTPStatusError, secrets: tuple[str | None, ...] = ()) -> dict[str, Any]:
    """Handle HTTP status errors and return formatted error dict.

    The message keeps the status code and the reason Intervals.icu gave (e.g. "Invalid oldest:
    Text 'bad' could not be parsed"), shortened and with the API key removed.

    Args:
        e: The HTTPStatusError exception.
        secrets: Values that must not appear in the message (API keys).

    Returns:
        Error dictionary with status code and message.
    """
    error_code = e.response.status_code
    reason = _api_reason(e.response, secrets)
    logger.error("HTTP error: %s - %s", error_code, reason or "(no reason given)")
    return {
        "error": True,
        "status_code": error_code,
        "message": _get_error_message(error_code, reason),
    }
