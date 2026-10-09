"""
Server setup and initialization for Intervals.icu MCP Server.

This module handles transport configuration and server startup logic.
"""

import os
import logging
from typing import Any

from mcp.server.fastmcp import FastMCP  # pylint: disable=import-error

from intervals_mcp_server.utils.types import TransportAliases

logger = logging.getLogger("intervals_icu_mcp_server")


def setup_transport() -> TransportAliases:
    """
    Setup and validate the MCP transport configuration.

    Reads MCP_TRANSPORT environment variable and validates it against
    supported transport types.

    Returns:
        TransportAliases: The selected transport type.

    Raises:
        ValueError: If the transport type is not supported.
    """
    transport_env = os.getenv("MCP_TRANSPORT", TransportAliases.STDIO.value).lower()
    try:
        transport_alias = TransportAliases(transport_env)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in TransportAliases)
        raise ValueError(f"Unsupported MCP_TRANSPORT value. Use one of: {allowed}.") from exc

    # Map HTTP to STREAMABLE_HTTP
    selected_transport = (
        TransportAliases.STREAMABLE_HTTP
        if transport_alias == TransportAliases.HTTP
        else transport_alias
    )

    return selected_transport


def start_server(mcp_instance: FastMCP, transport: TransportAliases, provider: Any = None) -> None:
    """
    Start the MCP server with the specified transport.

    Args:
        mcp_instance (FastMCP): The FastMCP server instance to start.
        transport (TransportAliases): The transport type to use.
        provider: The built-in OAuth provider when MCP_AUTH=oauth (adds the OAuth refinements).
    """
    host = mcp_instance.settings.host
    port = mcp_instance.settings.port

    if transport == TransportAliases.STDIO:
        logger.info("Starting MCP server with stdio transport.")
        mcp_instance.run()
        return

    import uvicorn  # pylint: disable=import-outside-toplevel

    from intervals_mcp_server.http_app import build_http_app  # pylint: disable=import-outside-toplevel

    settings = mcp_instance.settings
    endpoints = {
        TransportAliases.SSE: f"{settings.sse_path} (messages: {settings.message_path})",
        TransportAliases.STREAMABLE_HTTP: settings.streamable_http_path,
        TransportAliases.HTTP_SSE: (
            f"{settings.streamable_http_path} and {settings.sse_path} (messages: {settings.message_path})"
        ),
    }
    logger.info(
        "Starting MCP server with %s transport at http://%s:%s%s%s.",
        transport.value,
        host,
        port,
        endpoints.get(transport, ""),
        " with OAuth" if provider is not None else "",
    )
    app = build_http_app(mcp_instance, transport.value, provider=provider, mount_path=os.getenv("MCP_SSE_MOUNT_PATH"))
    uvicorn.run(app, host=host, port=port, log_level=settings.log_level.lower())
