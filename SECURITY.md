# Security Policy

This policy covers the Intervals.icu MCP server maintained at
<https://github.com/futureweb/intervals-mcp-server> (PyPI package `futureweb-intervals-mcp`,
container image `ghcr.io/futureweb/intervals-mcp-server`). It is a fork of
[mvilanova/intervals-mcp-server](https://github.com/mvilanova/intervals-mcp-server); problems in
the upstream project should be reported there as well.

## Remote deployment hardening (SSE / HTTP transports)

- Bind to `127.0.0.1` and terminate TLS in a reverse proxy that forwards `X-Forwarded-Proto`.
- Use a secret path (`FASTMCP_SSE_PATH=/mcp-<random>/sse`, `FASTMCP_MESSAGE_PATH=/mcp-<random>/messages/`)
  and let the proxy forward only that prefix; deny every other path. Treat the URL as a credential.
- Enable the built-in OAuth server (`MCP_AUTH=oauth`, `MCP_PUBLIC_URL`, `OAUTH_PASSWORD_HASH`) for
  clients that support OAuth (ChatGPT, Claude); see `docs/REMOTE_ACCESS.md`.
- Optionally restrict a transitional legacy path to the published OpenAI egress ranges
  (`https://openai.com/chatgpt-connectors.json`) at the proxy.
- Keep `MCP_PERMISSIONS` minimal; write, destructive and admin classes stay hidden unless enabled.
- One Intervals.icu API key serves every client of the server: never share a deployment between
  athletes without a multi-tenant design.

## Supported versions

Only the **latest minor release line** receives security fixes. Fixes ship as patch releases of
that line (`vX.Y.Z` -> `vX.Y.(Z+1)`). Older minor versions are not patched; please upgrade.

| Version                    | Supported   |
| -------------------------- | ----------- |
| latest `X.Y.*` line        | yes         |
| older `X.Y.*` lines        | no          |
| `main` branch (unreleased) | best effort |

## Reporting a vulnerability

Please do **not** open a public issue, discussion or pull request for a security problem.

Use GitHub's private vulnerability reporting for this repository:

<https://github.com/futureweb/intervals-mcp-server/security/advisories/new>

Include the affected version or commit, how the server is deployed (transport, Docker / PyPI /
git checkout, reverse proxy), a description of the issue and its impact, reproduction steps or a
proof of concept and, if you have one, a suggested fix. **Do not include your API key or real
athlete data** in the report; use placeholders.

What to expect:

- We acknowledge new reports within 7 days and keep you informed about the progress.
- Confirmed vulnerabilities are fixed in a patch release and published together with a GitHub
  security advisory that credits the reporter (unless you prefer to stay anonymous).
- Please give us reasonable time to ship a fix before disclosing details publicly (90 days by
  default, or earlier once the fix is released).

If private vulnerability reporting is not available to you, contact the maintainers through the
contact listed in `pyproject.toml` and start the subject with `[SECURITY] intervals-mcp-server`.

## Scope

In scope:

- The MCP server code in this repository (`src/`), its packaging (`pyproject.toml`, `uv.lock`),
  the `Dockerfile` and the published container image, and the GitHub Actions workflows.

Out of scope (please report these to the respective vendor or project):

- **Intervals.icu** itself, its web application and its API (<https://intervals.icu/>).
- **Garmin** Connect, Garmin devices and any Garmin bridge or sync integration. The server only
  reads what Intervals.icu already stores; Garmin data never passes through it directly.
- The MCP clients you connect (Claude Desktop, Claude Code, ChatGPT, Cursor, ...) and the MCP
  Python SDK. Dependency vulnerabilities are tracked through Dependabot; a report is welcome if we
  are slow to pick up an available fix.
- Issues that require an already compromised host, user account or MCP client, and prompt
  injection against the LLM itself, unless the server fails to enforce its own guards (permission
  class, input validation).

## Operational guidance

The server is a thin, unauthenticated bridge between an LLM client and the Intervals.icu API. Its
security depends mostly on how you run it.

### API key handling

- The Intervals.icu API key gives full access to the athlete's account. Treat it like a password.
- Pass it through the environment (`API_KEY`), a local `.env` file (ignored by git) or your MCP
  client's configuration. Never commit it and never paste it into issues, chats or logs.
- Use one key per machine where possible and rotate it in your Intervals.icu settings (Developer
  Settings) immediately if it may have leaked.
- With Docker, pass the key with `-e API_KEY=...` or `--env-file`. Never bake it into an image or
  a `docker-compose.yml` that is checked in.

### Never log secrets

- The server does not log request headers or the API key. Keep the log level at `INFO` or lower
  in production and redact `Authorization` headers and athlete IDs before sharing logs.
- Contributors: never add logging of credentials, full request or response bodies, or athlete
  data.

### Read-only by default

- The permission class is selected with `MCP_PERMISSIONS`. The default class is read-only: tools
  that create, update or delete data on Intervals.icu are not exposed unless you opt in
  explicitly. Keep the default unless you need writes, and review what your LLM client may do
  before enabling a write or destructive class (see the README for the available classes).
- The gate is enforced server-side when tools are registered; a client cannot escalate it.

### Network transports

- The default `stdio` transport opens no network socket and is the recommended way to run the
  server locally.
- The `sse` and `streamable-http` transports have **no built-in authentication, authorisation or
  TLS**. Anyone who can reach the port can act with your API key. Bind them to `127.0.0.1` (the
  default) and never expose them on a public interface or the open internet. If remote access is
  required, put an authenticating, TLS-terminating reverse proxy (nginx, Caddy, Traefik,
  Cloudflare Access, ...) in front of the server and restrict access to known clients.
- Inside Docker, `127.0.0.1` is the container's own loopback; a network transport only works with
  `FASTMCP_HOST=0.0.0.0`. Publish the port on the host loopback only (`-p 127.0.0.1:8000:8000`)
  and apply the reverse-proxy rule above.
- Tunnels such as ngrok make the server publicly reachable. Use them only with authentication
  enabled on the tunnel and only for as long as needed.

### No real athlete data in issues

- Bug reports, discussions and pull requests must not contain real athlete IDs, names, e-mail
  addresses, GPS traces, or heart rate, power and wellness values of real people. Use synthetic
  data (`ATHLETE_ID=i12345`) and redact logs. The test fixtures in this repository are synthetic
  as well, and contributions must keep it that way.
