"""
Project icon: served icon routes (also without a token when OAuth is on), serverInfo icons and
websiteUrl on initialize, the logo on the consent page, and the asset files in the package.
"""

import base64
import json
import os
import pathlib
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP
from starlette.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("API_KEY", "test")
os.environ.setdefault("ATHLETE_ID", "i1")

from intervals_mcp_server import server  # noqa: E402,F401  # pylint: disable=wrong-import-position,unused-import
from intervals_mcp_server.auth import install_login_routes, oauth_from_env  # noqa: E402  # pylint: disable=wrong-import-position
from intervals_mcp_server.auth_pages import _page  # noqa: E402  # pylint: disable=wrong-import-position,protected-access
from intervals_mcp_server.branding import (  # noqa: E402  # pylint: disable=wrong-import-position
    ICON_ROUTES,
    WEBSITE_URL,
    asset_bytes,
    install_icon_routes,
    server_icons,
)
from intervals_mcp_server.http_app import build_http_app  # noqa: E402  # pylint: disable=wrong-import-position
from intervals_mcp_server.mcp_instance import mcp  # noqa: E402  # pylint: disable=wrong-import-position

ROOT = pathlib.Path(__file__).resolve().parents[1]
MEDIA_TYPES = {
    "/favicon.ico": "image/x-icon", "/favicon.png": "image/png", "/icon.png": "image/png",
    "/icon.svg": "image/svg+xml", "/apple-touch-icon.png": "image/png",
}


def _check_routes(client: TestClient) -> None:
    for path, media_type in MEDIA_TYPES.items():
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.headers["content-type"].startswith(media_type), (path, response.headers["content-type"])
        assert "max-age" in response.headers["cache-control"] and response.headers["x-content-type-options"] == "nosniff"
        assert response.content == asset_bytes(ICON_ROUTES[path][0])
    assert client.get("/icon.png").content[:8] == b"\x89PNG\r\n\x1a\n"
    assert client.get("/favicon.ico").content[:4] == b"\x00\x00\x01\x00"
    assert client.get("/icon.svg").text.lstrip().startswith("<svg")


def test_icon_routes_on_every_http_transport():
    """The icons are served by the streamable HTTP, SSE and combined apps."""
    assert set(ICON_ROUTES) == set(MEDIA_TYPES)
    for transport in ("streamable-http", "sse", "http+sse"):
        # Static routes need no lifespan (the streamable HTTP session manager runs once per server).
        _check_routes(TestClient(build_http_app(mcp, transport), base_url="http://127.0.0.1:8000"))


def test_icon_routes_need_no_token_with_oauth(tmp_path):
    """With the built-in OAuth server the icon routes stay public (the MCP endpoint needs a token)."""
    kwargs = oauth_from_env({
        "MCP_AUTH": "oauth", "MCP_PUBLIC_URL": "http://localhost", "OAUTH_PASSWORD": "correct horse battery staple",
        "OAUTH_STATE_FILE": str(tmp_path / "oauth_state.json"),
    })
    oauth_mcp: FastMCP[Any] = FastMCP("test", **kwargs)
    install_login_routes(oauth_mcp, kwargs["auth_server_provider"])
    install_icon_routes(oauth_mcp)
    client = TestClient(build_http_app(oauth_mcp, "http+sse", provider=kwargs["auth_server_provider"]),
                        base_url="http://localhost")
    _check_routes(client)
    assert client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 401


def test_server_info_carries_icons_and_website_on_initialize():
    """initialize answers with serverInfo.websiteUrl and icons (served URLs or the inlined SVG)."""
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}}
    options = mcp._mcp_server.create_initialization_options()  # pylint: disable=protected-access
    assert options.website_url == WEBSITE_URL and options.icons
    mcp._session_manager = None  # pylint: disable=protected-access  # a fresh session manager for this app
    with TestClient(build_http_app(mcp, "streamable-http"), base_url="http://127.0.0.1:8000") as client:
        response = client.post("/mcp", json=body, headers=headers)
    assert response.status_code == 200
    payload = next(json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:"))
    info = payload["result"]["serverInfo"]
    assert info["websiteUrl"] == WEBSITE_URL
    icons = info["icons"]
    assert icons and icons[0]["mimeType"] == "image/svg+xml"
    if not os.environ.get("MCP_PUBLIC_URL"):
        prefix = "data:image/svg+xml;base64,"
        assert icons[0]["src"].startswith(prefix) and base64.b64decode(icons[0]["src"][len(prefix):]) == asset_bytes("icon-futureweb.svg")


def test_public_url_icons_point_to_the_served_files():
    """With MCP_PUBLIC_URL the icons are the served URLs; without it the SVG is inlined."""
    icons = server_icons({"MCP_PUBLIC_URL": "https://mcp.example.com/"})
    assert [(i.src, i.mimeType, i.sizes) for i in icons] == [
        ("https://mcp.example.com/icon.svg", "image/svg+xml", ["any"]),
        ("https://mcp.example.com/icon.png", "image/png", ["512x512"]),
        ("https://mcp.example.com/favicon.png", "image/png", ["48x48"]),
    ]
    assert server_icons({})[0].src.startswith("data:image/svg+xml;base64,")


def test_consent_page_shows_the_logo():
    """The sign-in page shows the logo and allows same-origin images only."""
    page = _page("<p>x</p>", 200)
    text = page.body.decode()
    assert '<img src="/icon.svg"' in text and 'rel="icon" href="/favicon.ico"' in text
    assert "img-src 'self'" in page.headers["content-security-policy"]
    assert "default-src 'none'" in page.headers["content-security-policy"]


def test_assets_are_in_the_package_and_the_docs():
    """The icon files are in the package (wheel) and in docs/assets for the README."""
    package = ROOT / "src" / "intervals_mcp_server" / "assets"
    for name in ("icon-futureweb.svg", "icon-futureweb-512.png", "icon-futureweb-256.png", "icon-futureweb-128.png",
                 "icon-futureweb-48.png", "favicon.ico"):
        assert (package / name).stat().st_size > 0, name
    for name in ("icon-futureweb.svg", "icon-futureweb-128.png"):
        assert (ROOT / "docs" / "assets" / name).read_bytes() == (package / name).read_bytes()
    assert "docs/assets/icon-futureweb" in (ROOT / "README.md").read_text(encoding="utf-8").split("\n## ")[0]
