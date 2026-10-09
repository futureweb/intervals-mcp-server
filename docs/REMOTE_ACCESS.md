# Remote access with OAuth (ChatGPT, Claude, other remote MCP clients)

Remote MCP clients such as ChatGPT or Claude's custom connectors support either "no
authentication" or OAuth 2.1 with PKCE. A server reachable from the internet without
authentication publishes your Intervals.icu data to anyone who finds the URL, so the
server ships an optional OAuth 2.1 authorization server. It is off by default
(`MCP_AUTH=none`); nothing changes unless you enable it.

```
ChatGPT / Claude  <-- OAuth 2.1: PKCE, client metadata documents or DCR, RFC 9207 iss -->  this server
this server       <-- OAuth 2: "Continue with Intervals.icu" (identity check only) ---->  Intervals.icu
this server       <-- API key (data access, unchanged) ---------------------------------->  Intervals.icu
```

Intervals.icu cannot be the authorization server of an MCP client itself (it has no
discovery metadata, no PKCE and no client registration). This server therefore issues
its own tokens to the MCP client and lets you prove who you are by signing in at
Intervals.icu. Only the athletes in `OAUTH_ALLOWED_ATHLETES` (default: `ATHLETE_ID`)
are accepted. The Intervals.icu token is used for that check only; it is never stored
and never given to the MCP client.

## What you get

* **Sign-in with Intervals.icu** (recommended) or with a server password, or both.
* **Consent page** showing which client asks (verified for clients with a metadata
  document, e.g. "ChatGPT · verified: chatgpt.com") and which permissions the connection
  gets. The permission classes of the server become OAuth scopes: `intervals:read`,
  `intervals:write`, `intervals:destructive`, `intervals:admin`. Tools of classes a
  connection was not granted are hidden from it and refused if called.
* **Client ID Metadata Documents** (preferred by the MCP spec 2026-07-28 and by ChatGPT)
  from an allowlist of hosts, with `private_key_jwt` client authentication, and dynamic
  client registration (RFC 7591) for older clients, restricted to allowlisted redirect hosts.
* **RFC 9207 `iss`** on every authorization response, so ChatGPT can use its stable
  redirect URI, plus audience checks (RFC 8707) on every token.
* **Both transports in one process** with `MCP_TRANSPORT=http+sse`: streamable HTTP at
  `/mcp` (what ChatGPT expects) and SSE at `/sse` + `/messages/`.
* Access tokens in memory (default 1 h); clients and refresh tokens (default 30 days)
  persisted as digests in `OAUTH_STATE_FILE` (mode 0600). Restarts do not disconnect clients.

## 1. Register an Intervals.icu OAuth app (for "Continue with Intervals.icu")

Apply at <https://intervals.icu/oauth/apply> while logged in as the athlete who owns the
server. Fill in name, description (max. 250 characters), website and privacy policy, and
add exactly one redirect URL:

```
https://mcp.example.com/oauth/intervals/callback
```

No webhooks are needed. If the form answers "Invalid url" with an empty *Activity URL
Template*, enter any URL of your host there (e.g. `https://mcp.example.com/activity/$external_id$`);
this server never uploads activities. The app stays *Pending* until Intervals.icu approves
it; client id and secret are shown under Settings → Apps → *Manage App*. Until then you can
use the password sign-in.

## 2. Configuration

| Variable | Meaning |
| --- | --- |
| `MCP_AUTH` | `none` (default) or `oauth`. |
| `MCP_PUBLIC_URL` | Public base URL, e.g. `https://mcp.example.com`. Required with `oauth`; OAuth issuer and resource. `http://` only for `localhost`. |
| `MCP_TRANSPORT` | `http+sse` (both transports), `http` / `streamable-http` or `sse`. |
| `OAUTH_LOGIN` | `intervals`, `password` or `intervals,password`. Default: `intervals` when `INTERVALS_OAUTH_CLIENT_ID` is set, otherwise `password`. |
| `INTERVALS_OAUTH_CLIENT_ID` / `INTERVALS_OAUTH_CLIENT_SECRET` | The Intervals.icu OAuth app (required for `intervals`). |
| `INTERVALS_OAUTH_SCOPE` | Scope requested at Intervals.icu for the identity check, default `ACTIVITY:READ`. |
| `OAUTH_ALLOWED_ATHLETES` | Comma-separated athlete ids allowed to sign in, default `ATHLETE_ID`. `i123` and `123` are the same. |
| `OAUTH_PASSWORD` or `OAUTH_PASSWORD_HASH`, `OAUTH_USERNAME` | Password sign-in (required for `password`). Username default `athlete`. |
| `OAUTH_CLIENT_HOSTS` | Hosts whose client metadata documents are accepted. Default `chatgpt.com,claude.ai,claude.com`; `none` disables them. |
| `OAUTH_REDIRECT_HOSTS` | Redirect hosts allowed for dynamically registered clients. Same default; `*` allows any https host. Loopback http is always allowed. |
| `OAUTH_DYNAMIC_REGISTRATION` | `true` (default) or `false`. |
| `OAUTH_PRIVATE_KEY_JWT` | Advertise and verify `private_key_jwt` for metadata-document clients, default `true`. |
| `OAUTH_STATE_FILE` | Clients and refresh tokens, default `./oauth_state.json`. Keep it on persistent storage. |
| `OAUTH_ACCESS_TOKEN_TTL` / `OAUTH_REFRESH_TOKEN_TTL` | Seconds, defaults 3600 and 2592000. |
| `OAUTH_LOGIN_RATE_LIMIT` | Failed sign-ins per client IP per 15 minutes before `429`, default 5. |
| `MCP_PERMISSIONS` | Upper limit of what any connection can be granted (default `read`). |

Minimal configuration:

```bash
MCP_TRANSPORT=http+sse
FASTMCP_HOST=127.0.0.1
FASTMCP_PORT=8001
MCP_AUTH=oauth
MCP_PUBLIC_URL=https://mcp.example.com
INTERVALS_OAUTH_CLIENT_ID=123
INTERVALS_OAUTH_CLIENT_SECRET=...
OAUTH_STATE_FILE=/var/lib/intervals-mcp/oauth_state.json
MCP_PERMISSIONS=read,write
```

For the password sign-in, store a hash instead of the plain password:

```bash
python -m intervals_mcp_server.auth hash-password          # prompts, or reads stdin
OAUTH_PASSWORD_HASH='pbkdf2_sha256$600000$...$...'         # quote it: it contains "$"
```

## 3. Reverse proxy

TLS terminates at the proxy; the server speaks plain HTTP on localhost. Give the server
its own hostname and forward everything; every MCP path requires a token, the OAuth
endpoints are public by design.

Apache:

```apache
<VirtualHost *:443>
    ServerName mcp.example.com
    ProxyRequests Off
    ProxyPreserveHost On
    RequestHeader set X-Forwarded-Proto "https"
    ProxyPass        / http://127.0.0.1:8001/ timeout=600
    ProxyPassReverse / http://127.0.0.1:8001/
    SSLCertificateFile    /etc/letsencrypt/live/mcp.example.com/fullchain.pem
    SSLCertificateKeyFile /etc/letsencrypt/live/mcp.example.com/privkey.pem
</VirtualHost>
```

nginx: `proxy_pass http://127.0.0.1:8001;`, `proxy_set_header X-Forwarded-Proto https;`,
`proxy_buffering off;` and a long `proxy_read_timeout` for SSE.

uvicorn trusts `X-Forwarded-For`/`-Proto` from 127.0.0.1, which is what the sign-in rate
limit keys on. `MCP_PUBLIC_URL` must be exactly the origin clients use, without a path.

## 4. Connect ChatGPT

1. ChatGPT → Settings → Apps / Plugins (developer mode) → create a connection with the URL
   `https://mcp.example.com/mcp` and authentication **OAuth**. Leave client id and secret
   empty: ChatGPT identifies itself with its metadata document.
2. ChatGPT opens the consent page. Check which permissions the connection gets and press
   **Continue with Intervals.icu** (or sign in with the password).
3. Intervals.icu asks you to approve the app; afterwards you are sent back to ChatGPT.
4. After a server update use **Refresh** on the connection so ChatGPT reloads the tools.

Claude: Settings → Connectors → *Add custom connector* with the same URL; the flow is the same.

## What happens on the wire

1. The client calls `/mcp`, gets `401` with `WWW-Authenticate: Bearer resource_metadata=...`
   and reads `/.well-known/oauth-protected-resource` and
   `/.well-known/oauth-authorization-server`.
2. ChatGPT uses `client_id=https://chatgpt.com/oauth/client.json`; the server fetches that
   document (allowlisted host, no redirects, 64 KiB limit, cached per `Cache-Control`),
   checks that its `client_id` equals the URL and accepts only its `redirect_uris`.
   Older clients register via `POST /register` instead.
3. `/authorize` (PKCE S256, `resource` must name this server) leads to the consent page.
4. "Continue with Intervals.icu" goes to `https://intervals.icu/oauth/authorize`, bound to
   your browser by an HttpOnly cookie. The callback exchanges the code, checks the athlete
   id against the allowlist and redirects to the client with `code`, `state` and `iss`.
5. The client redeems the code at `/token` (ChatGPT signs a `private_key_jwt` assertion
   with a key from its JWKS; it is verified, including audience, lifetime and replay).
6. Access tokens are refreshed with rotating refresh tokens.

Sign-in links live 10 minutes, codes 5 minutes; both are single use.

## Operations

* **Restart:** access tokens are dropped (clients refresh silently); registered clients and
  refresh tokens survive via the state file. Deleting the file disconnects every client.
* **Switch from password to Intervals.icu sign-in:** set the client id/secret and
  `OAUTH_LOGIN=intervals`, restart. Existing connections keep working; the next sign-in uses
  Intervals.icu.
* **Revoke a connection:** `POST /revoke`, or delete the state file to cut off all.
* **Logs** contain client ids, athlete ids and events, never passwords, codes or tokens.
* `get_server_status` / `--doctor` show the sign-in method, whether an Intervals.icu app is
  configured and the allowed athletes.

## Security model

Data access uses the one Intervals.icu API key of the deployment, so this is a
single-athlete server: the allowlist decides who may connect, and the consent page decides
what each connection may do, never more than `MCP_PERMISSIONS`. With the Intervals.icu
sign-in there is no extra password to leak; with the password sign-in it must be as strong
as the API key itself. Multiple athletes need separate deployments. A future multi-athlete
mode would use each athlete's own Intervals.icu OAuth token for data access instead of a
shared API key.
