"""
API client for Intervals.icu MCP Server.

This module handles all HTTP communication with the Intervals.icu API,
including request management, error handling, and client lifecycle.
"""

from json import JSONDecodeError
import asyncio
import json
import logging
import sys
from contextlib import asynccontextmanager
from http import HTTPStatus
from typing import Any

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


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    """Seconds to wait before the next attempt (Retry-After header or exponential back-off)."""
    header = response.headers.get("Retry-After") if hasattr(response, "headers") else None
    if header:
        try:
            return min(MAX_RETRY_DELAY_S, max(0.0, float(header)))
        except ValueError:
            pass
    return min(MAX_RETRY_DELAY_S, 1.5 * (2**attempt))


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


def _get_error_message(error_code: int, error_text: str) -> str:
    """Return a user-friendly error message for a given HTTP status code."""
    error_messages = {
        HTTPStatus.UNAUTHORIZED: f"{HTTPStatus.UNAUTHORIZED.value} {HTTPStatus.UNAUTHORIZED.phrase}: Please check your API key.",
        HTTPStatus.FORBIDDEN: f"{HTTPStatus.FORBIDDEN.value} {HTTPStatus.FORBIDDEN.phrase}: You may not have permission to access this resource.",
        HTTPStatus.NOT_FOUND: f"{HTTPStatus.NOT_FOUND.value} {HTTPStatus.NOT_FOUND.phrase}: The requested endpoint or ID doesn't exist.",
        HTTPStatus.UNPROCESSABLE_ENTITY: f"{HTTPStatus.UNPROCESSABLE_ENTITY.value} {HTTPStatus.UNPROCESSABLE_ENTITY.phrase}: The server couldn't process the request (invalid parameters or unsupported operation).",
        HTTPStatus.TOO_MANY_REQUESTS: f"{HTTPStatus.TOO_MANY_REQUESTS.value} {HTTPStatus.TOO_MANY_REQUESTS.phrase}: Too many requests in a short time period.",
        HTTPStatus.INTERNAL_SERVER_ERROR: f"{HTTPStatus.INTERNAL_SERVER_ERROR.value} {HTTPStatus.INTERNAL_SERVER_ERROR.phrase}: The Intervals.icu server encountered an internal error.",
        HTTPStatus.SERVICE_UNAVAILABLE: f"{HTTPStatus.SERVICE_UNAVAILABLE.value} {HTTPStatus.SERVICE_UNAVAILABLE.phrase}: The Intervals.icu server might be down or undergoing maintenance.",
    }
    try:
        status = HTTPStatus(error_code)
        return error_messages.get(status, error_text)
    except ValueError:
        return error_text


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
            "API key is required. Set API_KEY env var or pass api_key",
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


def unsafe_path_reason(url: str) -> str | None:
    """Why *url* is not a safe API path, or None when every segment is a plain identifier."""
    if not url.startswith("/"):
        return "path must start with '/'"
    for segment in url[1:].split("/"):
        if segment in ("", ".", ".."):
            return "empty or dot path segment"
        if not set(segment) <= _SEGMENT_CHARS:
            return "invalid characters in an identifier"
    return None


def scrub_secrets(value: Any) -> Any:
    """Recursively drop secret fields from API data before it reaches a tool."""
    if isinstance(value, dict):
        return {k: scrub_secrets(v) for k, v in value.items() if k not in SECRET_FIELDS}
    if isinstance(value, list):
        return [scrub_secrets(v) for v in value]
    return value


def _parse_response(
    response: httpx.Response, full_url: str
) -> dict[str, Any] | list[dict[str, Any]]:
    """Parse HTTP response and return JSON data or error dict.

    Returns:
        Parsed JSON response or error dict.
    """
    try:
        response_data = response.json() if response.content else {}
    except JSONDecodeError:
        logger.error("Invalid JSON in response from: %s", full_url)
        return {"error": True, "message": "Invalid JSON in response"}
    response.raise_for_status()
    return scrub_secrets(response_data)


async def make_intervals_request(  # pylint: disable=too-many-locals
    url: str,
    api_key: str | None = None,
    params: dict[str, Any] | None = None,
    method: str = "GET",
    data: dict[str, Any] | list[dict[str, Any]] | None = None,
) -> dict[str, Any] | list[dict[str, Any]]:
    """
    Make a request to the Intervals.icu API with proper error handling.

    Args:
        url (str): The API endpoint path (e.g., '/athlete/{id}/activities').
        api_key (str | None): Optional API key to use for authentication. Defaults to the global API_KEY.
        params (dict[str, Any] | None): Optional query parameters for the request.
        method (str): HTTP method to use (GET, POST, etc.). Defaults to GET.
        data (dict[str, Any] | list[dict[str, Any]] | None): Optional JSON data (object or array) to send in the request body.

    Returns:
        dict[str, Any] | list[dict[str, Any]]: The parsed JSON response from the API, or an error dict.
    """
    reason = unsafe_path_reason(url)
    if reason:
        logger.warning("Rejected API path built from tool arguments (%s)", reason)
        return {"error": True, "message": f"Invalid identifier: {reason}. Use the plain id (e.g. i123456789 or 123456)."}

    # Prepare request configuration
    full_url, auth, headers, error_msg = _prepare_request_config(url, api_key, method)
    if error_msg:
        return {"error": True, "message": error_msg}

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
                timeout=30.0,
                content=body,
            )
        return await client.request(
            method=method,
            url=full_url,
            headers=headers,
            params=params,
            auth=auth,
            timeout=30.0,
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

    try:
        response = await _send_with_client_recovery()
        retryable = retry_statuses(method)
        for attempt in range(MAX_ATTEMPTS - 1):
            status = getattr(response, "status_code", 200)
            if status not in retryable:
                break
            delay = _retry_delay(response, attempt)
            logger.warning("Intervals.icu answered %s for %s; retrying in %.1f s", status, url, delay)
            await asyncio.sleep(delay)
            response = await _send_with_client_recovery()

        return _parse_response(response, full_url)
    except httpx.HTTPStatusError as e:
        return _handle_http_status_error(e)
    except httpx.RequestError as e:
        logger.error("Request error: %s", str(e))
        return {"error": True, "message": f"Request error: {str(e)}"}
    except httpx.HTTPError as e:
        logger.error("HTTP client error: %s", str(e))
        return {"error": True, "message": f"HTTP client error: {str(e)}"}


def _handle_http_status_error(e: httpx.HTTPStatusError) -> dict[str, Any]:
    """Handle HTTP status errors and return formatted error dict.

    Args:
        e: The HTTPStatusError exception.

    Returns:
        Error dictionary with status code and message.
    """
    error_code = e.response.status_code
    error_text = e.response.text
    # The body can echo athlete data; a short excerpt is enough to diagnose the error.
    logger.error("HTTP error: %s - %.200r", error_code, error_text)
    return {
        "error": True,
        "status_code": error_code,
        "message": _get_error_message(error_code, error_text),
    }
