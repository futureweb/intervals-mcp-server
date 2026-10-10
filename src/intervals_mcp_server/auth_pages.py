"""Consent and sign-in pages of the built-in OAuth authorization server.

``GET  /oauth/login?request=...``   consent page: which client, which permissions, sign-in
``POST /oauth/login``               ``action=intervals`` (continue at Intervals.icu),
                                    ``action=password`` (password sign-in) or ``action=deny``
``GET  /oauth/intervals/callback``  return from Intervals.icu; checks the athlete

The consent form is bound to the browser that loaded it (CSRF protection): the consent
page sets a random HttpOnly cookie and puts an HMAC of it and the request id into the
form; a POST needs both, and a POST whose ``Origin`` or ``Sec-Fetch-Site`` names another
site is refused.  A page elsewhere therefore cannot submit the consent (and pick the
permissions) in the athlete's browser.  The sign-in at Intervals.icu is bound to the
browser that started it with a second HttpOnly cookie, so a callback URL cannot be
replayed in another browser.  Pages are served with ``no-store``, a strict content
security policy and frame protection.
"""

from __future__ import annotations

import html
import logging
import re
import secrets
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import anyio
import anyio.to_thread
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from intervals_mcp_server.auth import (
    INTERVALS_CALLBACK_PATH,
    LOGIN_PATH,
    LOGIN_REQUEST_TTL,
    LoginError,
    SingleUserOAuthProvider,
    client_key,
)
from intervals_mcp_server.auth_clients import log_safe

logger = logging.getLogger(__name__)

__all__ = ["install_routes"]

COOKIE_NAME = "intervals_mcp_login"
SECURE_COOKIE_NAME = "__Host-intervals_mcp_login"
CSRF_COOKIE_NAME = "intervals_mcp_csrf"
SECURE_CSRF_COOKIE_NAME = "__Host-intervals_mcp_csrf"
CSRF_FIELD = "csrf"
# The consent form is small; Starlette would otherwise parse bodies of any size.
MAX_FORM_BYTES = 16 * 1024
MAX_FORM_FIELDS = 50
_BROWSER_VALUE = re.compile(r"[A-Za-z0-9_-]{32,64}")
# Worker threads for the password hash: a sign-in flood queues here instead of filling the
# shared pool that also writes the OAuth state file.
HASH_THREADS = 4

_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "Content-Security-Policy": (
        # No form-action: Chrome applies it to the redirect after the POST, which goes to
        # Intervals.icu or to the MCP client's redirect URI.
        "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    # same-origin (not no-referrer): with no-referrer browsers send "Origin: null" on the
    # form POST, which the same-origin check below could not tell apart from a sandboxed
    # frame. Cross-origin requests still get no referrer.
    "Referrer-Policy": "same-origin",
}

_PERMISSION_TEXT = {
    "read": "Read your training data (activities, streams, wellness, calendar, settings)",
    "write": "Create and edit calendar entries, notes, activity RPE/feel and subjective wellness",
    "destructive": "Delete calendar entries, workouts and custom items",
    "admin": "Change sport settings (FTP, zones) and run bulk operations",
}

_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Connect - Intervals MCP</title>
<link rel="icon" href="/favicon.ico" sizes="48x48">
<link rel="icon" href="/icon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<style>
body{font-family:system-ui,-apple-system,sans-serif;background:#f4f5f7;color:#1f2933;margin:0;
display:flex;min-height:100vh;align-items:center;justify-content:center}
main{background:#fff;padding:2rem;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,.08);
width:min(28rem,90vw);box-sizing:border-box}
h1{font-size:1.25rem;margin:0 0 1rem}
.brand{display:flex;align-items:center;gap:.6rem;margin:0 0 1rem;font-size:.85rem;font-weight:600;color:#0b2550}
.brand img{width:40px;height:40px}
p{font-size:.9rem;color:#52606d;margin:.5rem 0}
.client{font-weight:600;color:#1f2933}
.badge{display:inline-block;font-size:.75rem;padding:.1rem .45rem;border-radius:999px;margin-left:.3rem}
.ok{background:#dcfce7;color:#166534}.warn{background:#fef3c7;color:#92400e}
fieldset{border:1px solid #e4e7eb;border-radius:8px;margin:1rem 0;padding:.5rem .75rem}
legend{font-size:.85rem;color:#52606d;padding:0 .25rem}
.perm{display:flex;gap:.5rem;align-items:flex-start;font-size:.9rem;margin:.4rem 0}
.perm input{margin-top:.2rem}
label{display:block;font-size:.9rem;margin:.75rem 0 .25rem}
input[type=text],input[type=password]{width:100%;box-sizing:border-box;padding:.6rem;border:1px solid #cbd2d9;
border-radius:6px;font-size:1rem}
button{margin-top:.75rem;width:100%;padding:.7rem;border:0;border-radius:6px;background:#2563eb;
color:#fff;font-size:1rem;cursor:pointer}
button.secondary{background:#e4e7eb;color:#1f2933}
.note{font-size:.8rem;color:#7b8794}
.error{color:#b91c1c}
hr{border:0;border-top:1px solid #e4e7eb;margin:1rem 0}
</style>
</head>
<body>
<main>
<div class="brand"><img src="/icon.svg" alt="" width="40" height="40"><span>Futureweb Intervals MCP</span></div>
<h1>__TITLE__</h1>
__BODY__
</main>
</body>
</html>
"""

_INVALID_LINK = (
    '<p class="error">This sign-in link is invalid or has expired.</p>'
    "<p>Go back to your MCP client and connect again.</p>"
)


def _page(body: str, status_code: int, title: str = "Connect to Intervals MCP", headers: dict[str, str] | None = None) -> Response:
    merged = dict(_SECURITY_HEADERS)
    if headers:
        merged.update(headers)
    content = _PAGE.replace("__TITLE__", html.escape(title)).replace("__BODY__", body)
    return HTMLResponse(content, status_code=status_code, headers=merged)


def _esc(value: str) -> str:
    return html.escape(value, quote=True)


def _consent_body(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    provider: SingleUserOAuthProvider, request_id: str, username: str, error: str | None, form_token: str
) -> str:
    pending = provider.pending_login(request_id)
    assert pending is not None  # checked by the caller
    config = provider.config
    name = pending.client_name or "An MCP client"
    badge = (
        f'<span class="badge ok">verified: {_esc(pending.client_id.split("/")[2])}</span>'
        if pending.client_verified
        else '<span class="badge warn">name not verified</span>'
    )
    perms = []
    for permission in pending.offered:
        checked = "checked" if permission in ("read", "write") else ""
        disabled = " disabled" if permission == "read" else ""
        perms.append(
            f'<div class="perm"><input type="checkbox" id="perm_{permission}" name="grant" value="{permission}" '
            f'{checked}{disabled}><label for="perm_{permission}" style="margin:0">{_esc(_PERMISSION_TEXT.get(permission, permission))}</label></div>'
        )
    if config.multi_user and config.intervals_offer_chats and "intervals" in config.login_methods:
        perms.append(
            '<div class="perm"><input type="checkbox" id="perm_chats" name="chats" value="1">'
            '<label for="perm_chats" style="margin:0">Activity comments (read; post with the write permission). '
            "Intervals.icu grants this as chat access, which also covers your private chats; this server only uses "
            "activity comments. Optional.</label></div>"
        )
    parts = [
        f'<p><span class="client">{_esc(name)}</span>{badge} wants to access your Intervals.icu data through this server.</p>',
        f'<p class="note">Redirect after sign-in: {_esc(pending.redirect_host or "unknown")}</p>',
        f'<form method="post" action="{_esc(urlsplit(config.login_url).path or LOGIN_PATH)}" autocomplete="on">',
        f'<input type="hidden" name="request" value="{_esc(request_id)}">',
        f'<input type="hidden" name="{CSRF_FIELD}" value="{_esc(form_token)}">',
        "<fieldset><legend>Permissions for this connection</legend>" + "".join(perms) + "</fieldset>",
        f'<p class="error">{_esc(error)}</p>' if error else "",
    ]
    local = [m for m in ("password", "apikey") if m in config.login_methods]
    if "intervals" in config.login_methods:
        parts.append('<button type="submit" name="action" value="intervals">Continue with Intervals.icu</button>')
        if config.multi_user:
            days = max(1, config.refresh_token_ttl // 86400)
            if config.token_retention_days:
                days = min(days, config.token_retention_days)
            parts.append(
                '<p class="note">This server is shared. After the sign-in it stores your Intervals.icu access token '
                "(encrypted) for this connection and uses it only for your own data, with the permissions chosen above. "
                "It is deleted when your client revokes the connection on disconnect, after "
                f"{days} days without use, or when you ask the server owner; revoking the app in Intervals.icu stops it "
                "at once. The server owner can see the server's logs.</p>"
            )
        if local:
            label = "Or, server owner only, sign in on this server:" if config.multi_user else "Or sign in on this server:"
            parts.append(f'<hr><p class="note">{label}</p>')
    if local and provider.totp_required:
        parts.append(
            '<label for="totp">Authenticator code</label><input type="text" id="totp" name="totp" '
            'inputmode="numeric" pattern="[0-9 ]*" maxlength="8" autocomplete="one-time-code">'
        )
    if "password" in config.login_methods:
        parts.append(
            f'<label for="username">Username</label><input type="text" id="username" name="username" value="{_esc(username)}" autocomplete="username">'
            '<label for="password">Password</label><input type="password" id="password" name="password" autocomplete="current-password">'
            '<button type="submit" name="action" value="password">Sign in</button>'
        )
    if "apikey" in config.login_methods:
        if "password" in config.login_methods:
            parts.append('<hr>')
        parts.append(
            '<label for="api_key">Intervals.icu API key</label><input type="password" id="api_key" name="api_key" autocomplete="off">'
            '<p class="note">Intervals.icu &rarr; Settings &rarr; Developer Settings. Only checked against the key this server uses; never stored or logged.</p>'
            '<button type="submit" name="action" value="apikey">Sign in with API key</button>'
        )
    parts.append('<button type="submit" name="action" value="deny" class="secondary" formnovalidate>Deny</button>')
    parts.append("</form>")
    parts.append('<p class="note">Only continue if you started this connection yourself just now.</p>')
    return "".join(parts)


class _Form:
    """The fields of a small urlencoded form."""

    def __init__(self, fields: list[tuple[str, str]]) -> None:
        self._fields = fields

    def get(self, key: str) -> str:
        """First value of *key*, or ``""``."""
        return next((value for name, value in self._fields if name == key), "")

    def getlist(self, key: str) -> list[str]:
        """Every value of *key*."""
        return [value for name, value in self._fields if name == key]


class _FormError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


async def _read_form(request: Request) -> _Form:
    """Read an urlencoded form of at most MAX_FORM_BYTES (the route has no other body limit)."""
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type not in ("", "application/x-www-form-urlencoded"):
        raise _FormError(415, "Unsupported form encoding.")
    declared = request.headers.get("content-length", "")
    if declared and (not declared.isdigit() or int(declared) > MAX_FORM_BYTES):
        raise _FormError(413, "The form is too large.")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_FORM_BYTES:
            raise _FormError(413, "The form is too large.")
    try:
        fields = parse_qsl(body.decode("utf-8"), keep_blank_values=True, max_num_fields=MAX_FORM_FIELDS)
    except (UnicodeDecodeError, ValueError) as exc:
        raise _FormError(400, "The form could not be read.") from exc
    return _Form(fields)


def _login_key(request: Request) -> str:
    return client_key(request.client.host if request.client else None)


def _secure(provider: SingleUserOAuthProvider) -> bool:
    return provider.config.public_url.startswith("https://")


def _cookie_name(provider: SingleUserOAuthProvider) -> str:
    return SECURE_COOKIE_NAME if _secure(provider) else COOKIE_NAME


def _csrf_cookie_name(provider: SingleUserOAuthProvider) -> str:
    return SECURE_CSRF_COOKIE_NAME if _secure(provider) else CSRF_COOKIE_NAME


def _browser_value(request: Request, provider: SingleUserOAuthProvider) -> str:
    """The consent cookie of this browser, or ``""`` when it has none (or a malformed one)."""
    value = request.cookies.get(_csrf_cookie_name(provider), "")
    return value if _BROWSER_VALUE.fullmatch(value) else ""


def _set_browser_cookie(response: Response, provider: SingleUserOAuthProvider, value: str) -> None:
    response.set_cookie(
        _csrf_cookie_name(provider),
        value,
        max_age=LOGIN_REQUEST_TTL,
        path="/",
        secure=_secure(provider),
        httponly=True,
        samesite="lax",
    )


def _same_origin_post(request: Request, provider: SingleUserOAuthProvider) -> bool:
    """Reject a POST that a browser marks as coming from another site.

    Headers that are absent (non-browser clients, old browsers) do not fail the check;
    the cookie-bound form token is required in any case.
    """
    site = request.headers.get("sec-fetch-site")
    if site is not None and site.strip().lower() != "same-origin":
        return False
    origin = request.headers.get("origin")
    if origin is None:
        return True
    if origin.strip() == "null":
        return site is not None
    return provider.config.same_origin(origin.strip())


def _redirect(location: str, headers: dict[str, str] | None = None) -> RedirectResponse:
    merged = dict(_SECURITY_HEADERS)
    if headers:
        merged.update(headers)
    return RedirectResponse(location, status_code=302, headers=merged)


def install_routes(mcp: FastMCP[Any], provider: SingleUserOAuthProvider) -> None:  # pylint: disable=too-many-statements
    """Register the consent page, the sign-in handlers and the Intervals.icu callback on *mcp*."""
    hash_limiter = anyio.CapacityLimiter(HASH_THREADS)

    def consent_page(request: Request, request_id: str, username: str, error: str | None, status: int) -> Response:
        """The consent page with a form token bound to this browser's consent cookie."""
        if provider.pending_login(request_id) is None:
            # Expired or finished in another tab while the credentials were checked.
            return _page(_INVALID_LINK, 400)
        browser = _browser_value(request, provider) or secrets.token_urlsafe(32)
        body = _consent_body(provider, request_id, username, error, provider.form_token(browser, request_id))
        response = _page(body, status)
        _set_browser_cookie(response, provider, browser)
        return response

    def error_page(message: str, status: int, headers: dict[str, str] | None = None) -> Response:
        return _page(f'<p class="error">{_esc(message)}</p>', status, headers=headers)

    async def login_form(request: Request) -> Response:
        request_id = request.query_params.get("request", "")
        if provider.pending_login(request_id) is None:
            return _page(_INVALID_LINK, 400)
        return consent_page(request, request_id, provider.config.username, None, 200)

    async def login_submit(request: Request) -> Response:  # pylint: disable=too-many-return-statements,too-many-branches
        try:
            form = await _read_form(request)
        except _FormError as exc:
            return error_page(str(exc), exc.status)
        request_id = form.get("request")
        pending = provider.pending_login(request_id)
        if pending is None:
            return _page(_INVALID_LINK, 400)
        key = _login_key(request)
        if not _same_origin_post(request, provider):
            logger.warning(
                "Consent form posted from another site refused (origin %s, sec-fetch-site %s, from %s)",
                log_safe(request.headers.get("origin", "-"), 80),
                log_safe(request.headers.get("sec-fetch-site", "-"), 20),
                key,
            )
            return error_page("This form can only be submitted from the sign-in page itself.", 403)
        if not provider.check_form_token(_browser_value(request, provider), request_id, form.get(CSRF_FIELD)):
            # No or another browser's consent cookie: show the page again in this browser.
            logger.warning("Consent form without a valid form token from %s; page shown again", key)
            message = "Please confirm again: this browser did not open the sign-in page (cookies are required)."
            return consent_page(request, request_id, provider.config.username, message, 403)
        action = form.get("action") or ("apikey" if form.get("api_key") else "password")
        try:
            if action == "deny":
                return _redirect(provider.deny_login(request_id))
            return await finish_submit(request, form, request_id, pending, action, key)
        except LoginError as exc:
            return error_page(str(exc), exc.status)

    async def finish_submit(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-return-statements
        request: Request, form: _Form, request_id: str, pending: Any, action: str, key: str
    ) -> Response:
        granted = provider.grant_for(pending, form.getlist("grant"))
        retry_after = {"Retry-After": str(provider.config.login_rate_window)}
        if provider.login_blocked(key):
            logger.warning("Login rate limit reached for %s", key)
            return error_page("Too many failed sign-in attempts. Please try again later.", 429, retry_after)
        if action == "intervals" and "intervals" in provider.config.login_methods:
            location, browser = provider.begin_intervals_login(request_id, granted, key, chats=form.get("chats") == "1")
            response = _redirect(location)
            response.set_cookie(
                _cookie_name(provider), browser, max_age=600, path="/", secure=_secure(provider), httponly=True, samesite="lax"
            )
            return response
        if action not in ("password", "apikey") or action not in provider.config.login_methods:
            return error_page("This sign-in method is not enabled.", 400)
        # Check and count the attempt before the threaded check (no await in between).
        refused = provider.begin_local_login(key)
        if refused is not None:
            if refused == "global":
                logger.warning("Global sign-in failure limit reached; %s sign-in from %s paused", action, key)
            else:
                logger.warning("Login rate limit reached for %s", key)
            return error_page("Too many failed sign-in attempts. Please try again later.", 429, retry_after)
        username = form.get("username")
        if action == "password":
            # PBKDF2 with 600k iterations takes ~0.3 s: keep it off the event loop.
            valid = await anyio.to_thread.run_sync(
                provider.verify_credentials, username, form.get("password"), limiter=hash_limiter
            )
        else:
            valid = provider.verify_api_key(form.get("api_key"))
        # Evaluate the second factor even after a wrong first factor (no early exit), but
        # only use up the code when the first factor was right.
        second = provider.verify_second_factor(form.get("totp"), consume=valid)
        if not (valid and second):
            logger.warning("Failed %s sign-in from %s", action, key)
            message = "Invalid credentials or authenticator code." if provider.totp_required else "Invalid credentials."
            return consent_page(request, request_id, username or provider.config.username, message, 401)
        provider.local_login_succeeded(key)
        return _redirect(provider.complete_login(request_id, key, granted, method=action))

    async def intervals_callback(request: Request) -> Response:
        params = request.query_params
        try:
            location = await provider.finish_intervals_login(
                state=params.get("state", ""),
                browser=request.cookies.get(_cookie_name(provider), ""),
                code=params.get("code"),
                error=params.get("error"),
                login_key=_login_key(request),
            )
        except LoginError as exc:
            return _page(f'<p class="error">{_esc(str(exc))}</p>', exc.status, title="Sign-in failed")
        response = _redirect(location)
        response.delete_cookie(_cookie_name(provider), path="/")
        return response

    mcp.custom_route(LOGIN_PATH, methods=["GET"], name="oauth_login_form")(login_form)
    mcp.custom_route(LOGIN_PATH, methods=["POST"], name="oauth_login_submit")(login_submit)
    mcp.custom_route(INTERVALS_CALLBACK_PATH, methods=["GET"], name="oauth_intervals_callback")(intervals_callback)
