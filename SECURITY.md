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
- Prefer the built-in OAuth server (`MCP_AUTH=oauth`, `MCP_PUBLIC_URL`) for clients that support
  OAuth (ChatGPT, Claude): sign-in with Intervals.icu restricted to `OAUTH_ALLOWED_ATHLETES` (no extra
  password) or with the Intervals.icu API key / a server password, optionally with a TOTP second
  factor (`OAUTH_TOTP_SECRET`, recommended for public servers without the Intervals.icu sign-in);
  per-connection permission scopes on a consent page that only accepts submissions from the browser
  that opened it, client metadata documents only from allowlisted hosts or document URLs, redirect
  URIs of registered clients only on allowlisted hosts or path prefixes (no query strings), RFC 9207
  `iss`, audience-bound tokens, rotating refresh tokens with reuse detection, `private_key_jwt`
  required for clients that declare it (ChatGPT), urlencoded token requests only. In the default
  single-user mode the Intervals.icu token is used for the identity check only and never stored
  (multi-user mode: see below). See `docs/REMOTE_ACCESS.md`.
- Behind a proxy that is not on 127.0.0.1, set `FORWARDED_ALLOW_IPS` to the proxy's address so the
  sign-in rate limits see the real client addresses.
- Keep OAuth secrets out of the proxy's access log (the Intervals.icu callback carries a short-lived
  authorization code in its query string, and the consent POST's `Referer` the sign-in request id)
  and limit request bodies at the proxy; see `docs/REMOTE_ACCESS.md`, section 3. Until the proxy's
  log format is changed, its access log keeps containing these values.
- Use a long random password or the TOTP second factor: the global failed sign-in budget
  (`OAUTH_LOGIN_GLOBAL_RATE_LIMIT`) is also a lever to pause password sign-ins, which TOTP removes.
- Optionally restrict a transitional legacy path to the published OpenAI egress ranges
  (`https://openai.com/chatgpt-connectors.json`) at the proxy.
- Keep `MCP_PERMISSIONS` minimal; write, destructive and admin classes stay hidden unless enabled.
- In the default single-user mode one Intervals.icu API key serves every client of the server, so
  the server refuses to start when `OAUTH_ALLOWED_ATHLETES` names anyone but `ATHLETE_ID` (the
  owner's own other accounts go into `OAUTH_OWNER_ACCOUNTS`). To share a deployment use the
  multi-user mode (`MCP_TENANCY=multi`, threat model below).

## Multi-user mode threat model

With `MCP_TENANCY=multi` several athletes use one deployment. What protects them from each other:

| Threat | Mitigation |
| --- | --- |
| A connection reads another athlete's data by naming their id (`athlete_id` argument, prompt injection) | The tool guard allows only the connection's own athlete (`0`/`i0` are aliases of it) and refuses anything else before a request; the API client refuses every path outside `/athlete/<own id>/...` and `/activity/<id>/...`, refuses a fetched activity whose `icu_athlete_id` is another athlete's, and sends requests for an activity's sub-resources (streams, intervals, comments) or writes to it only after checking its owner (once per connection, cached). |
| A friend's request reaches Intervals.icu with the owner's API key | The credential comes from the MCP access token of the request (context variable), never from tool arguments; athlete grants carry the athlete's own OAuth token (Bearer). The API key is only used for owner grant records: the password / API-key sign-in (multi-user mode: only with TOTP), an Intervals.icu sign-in as `ATHLETE_ID`, single-user connections whose record names the owner, and those the operator adopted (`grants adopt-legacy --owner`). **Fail closed:** a token without a grant record is refused, never treated as the owner's; a missing or damaged `grants` table stops the server; tokens kept verbatim keep their grant record; every token record names its athlete. A request without a connection credential is refused (also for an unknown `MCP_TENANCY` value at runtime), and so is an explicit API key. |
| Friends who connected in the single-user mode | The single-user mode refuses other athletes on the allowlist; sign-ins record the athlete; connections from before that are refused in the multi-user mode until the operator adopts them, and those recorded with another athlete are never adopted. |
| One athlete's data in another's answer via caches | Cache keys carry the connection's partition (the grant id, not a digest of a token); the owner's API key keeps its own partition. Cookies are never stored by the shared HTTP client. |
| Session confusion | The SDK binds a session to the token's subject, which is the grant id: another connection's session id is answered with 404. |
| Stolen state file | Tokens are sealed with AES-256-GCM (key in `OAUTH_TOKEN_KEY` / a 0600 `OAUTH_TOKEN_KEY_FILE`, never in the file) and bound to grant and athlete; refresh tokens are stored as SHA-256 digests. With the file *and* the key an attacker can use every stored token within its scopes until the athlete revokes the app at Intervals.icu. Keep the key out of backups of the state file. |
| Excessive permissions | The Intervals.icu scopes requested follow the permission classes chosen on the consent page; a request outside the connection's scopes is refused with a "reconnect and allow" message. `CHATS` (activity comments, but also private chats) is requested only when the operator offers it (`INTERVALS_OAUTH_OFFER_CHATS`) and the athlete ticks it. |
| The owner's password on a page friends use | In the multi-user mode the password and API-key sign-ins require `OAUTH_TOTP_SECRET`; recommended is `OAUTH_LOGIN=intervals` (the owner signs in with Intervals.icu as `ATHLETE_ID`). |
| Old releases or a switch back to single-user mode serving friends with the owner's key | The multi-user state file is format 2, which older releases refuse to read. In the single-user mode only the owner's connections are loaded; other athletes' connections and tokens are removed at the next write. |
| Unwanted sign-ins | Only athletes on `OAUTH_ALLOWED_ATHLETES`; `*` needs `OAUTH_ALLOW_ANY_ATHLETE=true`. Removing an athlete from the list makes their connections unusable at the next start. |
| One athlete exhausts the shared Intervals.icu app limit | Daily soft budget per athlete (`MCP_ATHLETE_DAILY_REQUESTS`), a shared 15-minute budget (`MCP_APP_REQUESTS_PER_15MIN`) of which one athlete may use at most `MCP_ATHLETE_SHARE_PERCENT` and the others leave `MCP_OWNER_RESERVED_PERCENT` to the owner, retries counted, plus the per-call limits. |
| State file growth | At most `OAUTH_MAX_GRANTS_PER_ATHLETE` grants per athlete and 500 in total (least recently used revoked). |
| Tokens in logs or answers | Tokens and keys are never logged; error texts from Intervals.icu are redacted; `get_server_status` shows only the calling connection (no other athletes, no allowlist, and to friends not how the owner signs in). |
| Key rotation | Several keys in `OAUTH_TOKEN_KEY`: the first seals, a token opened with an older key is sealed again at its next use; `--doctor` counts the tokens still waiting. |

Not covered: the operator (and anyone controlling the host) can read the logs (athlete ids, tool
names, request paths, errors) and, with the key, the stored tokens; friends must trust the
operator. Friends' wellness data is health data (in the EU a special category, Art. 9 GDPR); the
operator is responsible for it (explicit consent, a short privacy note, log retention, a deletion
procedure: README "Sharing the server with friends"). A compromised MCP client of one athlete can
act within that athlete's grant. Deleting a connection removes the token from the server; it
stays valid at Intervals.icu until the athlete revokes the app there. A token is deleted on
disconnect only when the MCP client calls `/revoke`; otherwise when the refresh token expires,
by retention or with `grants remove`. A consent link opened in someone else's browser is an OAuth
request-phishing pattern that predates this mode; the redirect-host allowlist limits it.

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

- The server does not log request headers, request bodies or the API key; client-supplied values
  (client names, metadata URLs) are escaped and shortened, and uvicorn's access log keeps query
  parameter names but not their values. Keep the log level at `INFO` in production (`DEBUG` adds
  per-request lines) and redact `Authorization` headers and athlete IDs before sharing logs.
- Contributors: never add logging of credentials, full request or response bodies, or athlete
  data.

### Read-only by default

- The permission class is selected with `MCP_PERMISSIONS`. The default class is read-only: tools
  that create, update or delete data on Intervals.icu are not exposed unless you opt in
  explicitly. Keep the default unless you need writes, and review what your LLM client may do
  before enabling a write or destructive class (see the README for the available classes).
- The gate is enforced server-side when tools are registered. With the OAuth server each
  connection is further limited to the classes granted on the consent page; refreshing a token can
  only narrow them, and tool calls outside them are refused.

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
