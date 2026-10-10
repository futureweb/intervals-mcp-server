# pylint: disable=missing-function-docstring
"""SEC-11: get_server_status must not tell connected clients how the server is deployed."""

import asyncio
import json

# --------------------------------------------------------------------------- #
# SEC-11: status tool
# --------------------------------------------------------------------------- #


def test_status_for_clients_omits_deployment_details(monkeypatch, tmp_path):
    from intervals_mcp_server.tools import status as status_module  # pylint: disable=import-outside-toplevel

    monkeypatch.setenv("MCP_AUTH", "oauth")
    monkeypatch.setenv("OAUTH_USERNAME", "zx-operator")
    monkeypatch.setenv("OAUTH_STATE_FILE", str(tmp_path / "secret-dir" / "state.json"))
    monkeypatch.setenv("FASTMCP_SSE_PATH", "/mcp-0123456789abcdef/sse")
    monkeypatch.setenv("FASTMCP_HOST", "10.1.2.3")
    monkeypatch.setattr(status_module.config, "athlete_id", "")
    public = asyncio.run(status_module.server_status())
    text = json.dumps(public)
    for secret in ("zx-operator", "secret-dir", "0123456789abcdef", "10.1.2.3"):
        assert secret not in text
    assert "Transport:" in status_module.format_status(public)
    private = asyncio.run(status_module.server_status(include_private=True))
    assert private["auth"]["username"] == "zx-operator" and private["host"] == "10.1.2.3"
    assert "secret path" in status_module.format_status(private)
