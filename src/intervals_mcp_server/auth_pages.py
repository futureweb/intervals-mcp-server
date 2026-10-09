"""Consent and sign-in pages of the built-in OAuth authorization server.

``GET  /oauth/login?request=...``   consent page: which client, which permissions, sign-in
``POST /oauth/login``               ``action=intervals`` (continue at Intervals.icu),
                                    ``action=password`` (password sign-in) or ``action=deny``
``GET  /oauth/intervals/callback``  return from Intervals.icu; checks the athlete

The sign-in at Intervals.icu is bound to the browser that started it with an HttpOnly
cookie, so a callback URL cannot be replayed in another browser.  Pages are served with
``no-store``, a strict content security policy and frame protection.
"""

from __future__ import annotations

import html
import logging
from collections.abc import Mapping
from typing import Any

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from intervals_mcp_server.auth import (
    INTERVALS_CALLBACK_PATH,
    LOGIN_PATH,
    LoginError,
    SingleUserOAuthProvider,
)

logger = logging.getLogger(__name__)

__all__ = ["install_routes"]

COOKIE_NAME = "intervals_mcp_login"
SECURE_COOKIE_NAME = "__Host-intervals_mcp_login"

_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "Content-Security-Policy": (
        # No form-action: Chrome applies it to the redirect after the POST, which goes to
        # Intervals.icu or to the MCP client's redirect URI.
        "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
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
<style>
body{font-family:system-ui,-apple-system,sans-serif;background:#f4f5f7;color:#1f2933;margin:0;
display:flex;min-height:100vh;align-items:center;justify-content:center}
main{background:#fff;padding:2rem;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,.08);
width:min(28rem,90vw);box-sizing:border-box}
h1{font-size:1.25rem;margin:0 0 1rem}
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


def _consent_body(provider: SingleUserOAuthProvider, request_id: str, username: str, error: str | None) -> str:
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
    parts = [
        f'<p><span class="client">{_esc(name)}</span>{badge} wants to access your Intervals.icu data through this server.</p>',
        f'<p class="note">Redirect after sign-in: {_esc(pending.redirect_host or "unknown")}</p>',
        f'<form method="post" action="{LOGIN_PATH}" autocomplete="on">',
        f'<input type="hidden" name="request" value="{_esc(request_id)}">',
        "<fieldset><legend>Permissions for this connection</legend>" + "".join(perms) + "</fieldset>",
        f'<p class="error">{_esc(error)}</p>' if error else "",
    ]
    if "intervals" in config.login_methods:
        parts.append('<button type="submit" name="action" value="intervals">Continue with Intervals.icu</button>')
    if "password" in config.login_methods:
        if "intervals" in config.login_methods:
            parts.append("<hr><p class=\"note\">Or sign in with the server password:</p>")
        parts.append(
            f'<label for="username">Username</label><input type="text" id="username" name="username" value="{_esc(username)}" autocomplete="username">'
            '<label for="password">Password</label><input type="password" id="password" name="password" autocomplete="current-password">'
            '<button type="submit" name="action" value="password">Sign in</button>'
        )
    parts.append('<button type="submit" name="action" value="deny" class="secondary" formnovalidate>Deny</button>')
    parts.append("</form>")
    parts.append('<p class="note">Only continue if you started this connection yourself just now.</p>')
    return "".join(parts)


def _form_value(form: Mapping[str, Any], key: str) -> str:
    value = form.get(key)
    return value if isinstance(value, str) else ""


def _login_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _cookie_name(provider: SingleUserOAuthProvider) -> str:
    return SECURE_COOKIE_NAME if provider.config.public_url.startswith("https://") else COOKIE_NAME


def _redirect(location: str, headers: dict[str, str] | None = None) -> RedirectResponse:
    merged = dict(_SECURITY_HEADERS)
    if headers:
        merged.update(headers)
    return RedirectResponse(location, status_code=302, headers=merged)


def install_routes(mcp: FastMCP[Any], provider: SingleUserOAuthProvider) -> None:  # pylint: disable=too-many-statements
    """Register the consent page, the sign-in handlers and the Intervals.icu callback on *mcp*."""

    async def login_form(request: Request) -> Response:
        request_id = request.query_params.get("request", "")
        if provider.pending_login(request_id) is None:
            return _page(_INVALID_LINK, 400)
        return _page(_consent_body(provider, request_id, provider.config.username, None), 200)

    async def login_submit(request: Request) -> Response:  # pylint: disable=too-many-return-statements
        form = await request.form()
        request_id = _form_value(form, "request")
        pending = provider.pending_login(request_id)
        if pending is None:
            return _page(_INVALID_LINK, 400)
        action = _form_value(form, "action") or "password"
        if action == "deny":
            return _redirect(provider.deny_login(request_id))
        granted = provider.grant_for(pending, [v for v in form.getlist("grant") if isinstance(v, str)])
        key = _login_key(request)
        if provider.login_blocked(key):
            logger.warning("Login rate limit reached for %s", key)
            return _page(
                '<p class="error">Too many failed sign-in attempts. Please try again later.</p>',
                429,
                headers={"Retry-After": str(provider.config.login_rate_window)},
            )
        if action == "intervals" and "intervals" in provider.config.login_methods:
            try:
                location, browser = provider.begin_intervals_login(request_id, granted)
            except LoginError as exc:
                return _page(f'<p class="error">{_esc(str(exc))}</p>', exc.status)
            secure = provider.config.public_url.startswith("https://")
            response = _redirect(location)
            response.set_cookie(
                _cookie_name(provider), browser, max_age=600, path="/", secure=secure, httponly=True, samesite="lax"
            )
            return response
        if action != "password" or "password" not in provider.config.login_methods:
            return _page('<p class="error">This sign-in method is not enabled.</p>', 400)
        username = _form_value(form, "username")
        password = _form_value(form, "password")
        if not provider.verify_credentials(username, password):
            provider.record_login_failure(key)
            logger.warning("Failed login attempt from %s", key)
            body = _consent_body(provider, request_id, username or provider.config.username, "Invalid credentials.")
            return _page(body, 401)
        return _redirect(provider.complete_login(request_id, key, granted))

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
