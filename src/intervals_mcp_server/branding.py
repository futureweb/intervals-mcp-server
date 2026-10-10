"""
Project icon and website for MCP clients and browsers.

- ``serverInfo`` of the MCP ``initialize`` answer carries ``websiteUrl`` and ``icons``: with
  ``MCP_PUBLIC_URL`` the icons point to the PNG and SVG served by this server, without it (stdio)
  the small SVG is inlined as a ``data:`` URI.
- The HTTP app serves ``/favicon.ico``, ``/favicon.png``, ``/icon.png``, ``/icon.svg`` and
  ``/apple-touch-icon.png`` without authentication (the consent page uses them too).

The files live in ``intervals_mcp_server/assets`` and are part of the wheel.
"""

import base64
from collections.abc import Awaitable, Callable, Mapping
from functools import cache
from importlib import resources
from typing import Any

from mcp.types import Icon
from starlette.requests import Request
from starlette.responses import Response

__all__ = ["ICON_ROUTES", "WEBSITE_URL", "asset_bytes", "install_icon_routes", "server_icons"]

WEBSITE_URL = "https://github.com/futureweb/intervals-mcp-server"
SVG_FILE = "icon-futureweb.svg"

# path -> (asset file, media type)
ICON_ROUTES: dict[str, tuple[str, str]] = {
    "/favicon.ico": ("favicon.ico", "image/x-icon"),
    "/favicon.png": ("icon-futureweb-48.png", "image/png"),
    "/icon.png": ("icon-futureweb-512.png", "image/png"),
    "/icon.svg": (SVG_FILE, "image/svg+xml"),
    "/apple-touch-icon.png": ("icon-futureweb-256.png", "image/png"),
}
_HEADERS = {
    "Cache-Control": "public, max-age=86400",
    "X-Content-Type-Options": "nosniff",
    # The SVG is static artwork: no scripts, no external resources even if opened directly.
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
}


@cache
def asset_bytes(name: str) -> bytes:
    """Bytes of a file in intervals_mcp_server/assets."""
    return (resources.files("intervals_mcp_server") / "assets" / name).read_bytes()


def server_icons(environ: Mapping[str, str]) -> list[Icon]:
    """Icons for ``serverInfo``: served URLs with MCP_PUBLIC_URL, else the SVG as a data: URI."""
    public = environ.get("MCP_PUBLIC_URL", "").strip().rstrip("/")
    if public:
        return [
            Icon(src=f"{public}/icon.svg", mimeType="image/svg+xml", sizes=["any"]),
            Icon(src=f"{public}/icon.png", mimeType="image/png", sizes=["512x512"]),
            Icon(src=f"{public}/favicon.png", mimeType="image/png", sizes=["48x48"]),
        ]
    data = base64.b64encode(asset_bytes(SVG_FILE)).decode("ascii")
    return [Icon(src=f"data:image/svg+xml;base64,{data}", mimeType="image/svg+xml", sizes=["any"])]


def _handler(name: str, media_type: str) -> Callable[[Request], Awaitable[Response]]:
    async def serve(_request: Request) -> Response:
        return Response(asset_bytes(name), media_type=media_type, headers=_HEADERS)

    return serve


def install_icon_routes(mcp: Any) -> None:
    """Serve the icons from the HTTP app (custom routes are not behind the OAuth bearer check)."""
    for path, (name, media_type) in ICON_ROUTES.items():
        mcp.custom_route(path, methods=["GET"])(_handler(name, media_type))
