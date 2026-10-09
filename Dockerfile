# syntax=docker/dockerfile:1.7
#
# Container image for the Intervals.icu MCP server (package: futureweb-intervals-mcp).
#
#   Build:  docker build -t ghcr.io/futureweb/intervals-mcp-server:dev .
#   Run:    docker run --rm -i -e API_KEY=... -e ATHLETE_ID=... ghcr.io/futureweb/intervals-mcp-server:latest
#
# Environment variables (see README.md / .env.example for the full list):
#   API_KEY, ATHLETE_ID          required, Intervals.icu credentials
#   MCP_TRANSPORT                stdio (default) | sse | http (alias of streamable-http)
#   FASTMCP_HOST, FASTMCP_PORT   listen address for sse/http, defaults 127.0.0.1:8000
#
# Transports and port exposure
# ----------------------------
# The default transport is stdio: the server talks over stdin/stdout only and
# opens no network socket. Run the container with `-i` so the MCP client can
# attach to its stdin. Nothing is exposed in this mode.
#
# With MCP_TRANSPORT=sse or MCP_TRANSPORT=http the server listens on
# FASTMCP_HOST:FASTMCP_PORT. Inside a container 127.0.0.1 (the default) is the
# container's own loopback interface and is NOT reachable from the host or from
# other containers, so a network transport only works if you explicitly set
# FASTMCP_HOST=0.0.0.0. The server has no built-in authentication or TLS:
# anyone who can reach the port can use your API key. Therefore
#   * publish the port on the host loopback only: -p 127.0.0.1:8000:8000
#   * put an authenticating, TLS-terminating reverse proxy (nginx, Caddy,
#     Traefik, ...) in front of it before any other machine may reach it
#   * never publish the port on a public interface directly.
#
# The EXPOSE instruction below is documentation only (Docker never publishes a
# port by itself) and is relevant for the sse/http transports exclusively.

###########################################
# Stage 1: build the virtual environment  #
###########################################
FROM python:3.12-slim AS builder

# uv, pinned. Keep in sync with the version used by CI and the maintainers.
COPY --from=ghcr.io/astral-sh/uv:0.11.3 /uv /uvx /bin/

ENV UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_NO_PROGRESS=1

WORKDIR /app

# 1) Runtime dependencies only, strictly from the lock file. This layer is
#    cached as long as pyproject.toml and uv.lock do not change.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

# 2) The project itself (README.md and LICENSE are read by the build backend).
COPY README.md LICENSE ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

###########################################
# Stage 2: runtime image                  #
###########################################
FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="intervals-mcp-server" \
      org.opencontainers.image.description="Model Context Protocol server for Intervals.icu" \
      org.opencontainers.image.source="https://github.com/futureweb/intervals-mcp-server" \
      org.opencontainers.image.licenses="GPL-3.0-only"

# Unprivileged runtime user without a login shell.
RUN groupadd --system --gid 10001 mcp \
    && useradd --system --uid 10001 --gid mcp --home-dir /app --shell /usr/sbin/nologin mcp

WORKDIR /app
COPY --from=builder --chown=mcp:mcp /app /app

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MCP_TRANSPORT=stdio

USER 10001:10001

# Only meaningful with MCP_TRANSPORT=sse|http, see the note at the top of this file.
EXPOSE 8000

CMD ["python", "src/intervals_mcp_server/server.py"]
