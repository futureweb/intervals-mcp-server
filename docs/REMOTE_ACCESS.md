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

* **Three sign-in options**, combinable with `OAUTH_LOGIN`:
  | Option | Needs | Default when |
  | --- | --- | --- |
  | `intervals`: "Continue with Intervals.icu" | an approved Intervals.icu OAuth app | `INTERVALS_OAUTH_CLIENT_ID` is set |
  | `password`: server password | `OAUTH_PASSWORD_HASH` (or `OAUTH_PASSWORD`) | a password is set |
  | `apikey`: your Intervals.icu API key | nothing extra: the `API_KEY` the server already uses | otherwise (zero configuration) |

  The API-key sign-in compares the entered key in constant time with the configured key; the
  entered key is never stored, logged or sent anywhere. With `OAUTH_TOTP_SECRET` the
  password and API-key sign-ins additionally ask for the 6-digit code of an authenticator app
  (TOTP, RFC 6238, single use).
* **Consent page** showing which client asks (verified for clients with a metadata
  document, e.g. "ChatGPT · verified: chatgpt.com") and which permissions the connection
  gets. The permission classes of the server become OAuth scopes: `intervals:read`,
  `intervals:write`, `intervals:destructive`, `intervals:admin`. Tools of classes a
  connection was not granted are hidden from it and refused if called. The form only
  accepts a submission from the browser that opened it (a cookie-bound form token plus an
  `Origin` / `Sec-Fetch-Site` check), so another web page cannot sign in and pick the
  permissions in your browser.
* **Client ID Metadata Documents** (preferred by the MCP spec 2026-07-28 and by ChatGPT)
  from an allowlist of hosts, with `private_key_jwt` client authentication, and dynamic
  client registration (RFC 7591) for older clients, restricted to allowlisted redirect hosts.
* **RFC 9207 `iss`** on every authorization response, so ChatGPT can use its stable
  redirect URI, plus audience checks (RFC 8707) on every token.
* **Both transports in one process** with `MCP_TRANSPORT=http+sse`: streamable HTTP at
  `/mcp` (what ChatGPT expects) and SSE at `/sse` + `/messages/`.
* Access tokens in memory (default 1 h); clients and refresh tokens (default 30 days)
  persisted as digests in `OAUTH_STATE_FILE` (mode 0600). Restarts do not disconnect clients.
  Refresh tokens rotate. A retry with the previous token within a short grace period gets the
  same answer again; presented later, it revokes the whole connection (RFC 9700 reuse
  detection, `OAUTH_REFRESH_REUSE_REVOKE`). Clients whose metadata document declares
  `private_key_jwt` (ChatGPT) must sign every token request, so for them a leaked refresh token
  alone is useless; for public clients (e.g. Claude via dynamic registration) rotation and reuse
  detection limit what a leaked token is worth. `/token` and `/revoke` accept only
  `application/x-www-form-urlencoded` bodies (RFC 6749).

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
it; client id and secret are shown under Settings → Apps → *Manage App*. Until then (or
instead) use the API-key or password sign-in: no app is needed for those.

## 2. Configuration

| Variable | Meaning |
| --- | --- |
| `MCP_AUTH` | `none` (default) or `oauth`. |
| `MCP_PUBLIC_URL` | Public base URL, e.g. `https://mcp.example.com`. Required with `oauth`; OAuth issuer and resource. `http://` only for `localhost`. |
| `MCP_TRANSPORT` | `http+sse` (both transports), `http` / `streamable-http` or `sse`. |
| `OAUTH_LOGIN` | Comma-separated `intervals`, `password`, `apikey`. Default: `intervals` with an Intervals.icu app, else `password` when a password is set, else `apikey`. |
| `INTERVALS_OAUTH_CLIENT_ID` / `INTERVALS_OAUTH_CLIENT_SECRET` | The Intervals.icu OAuth app (required for `intervals`). |
| `INTERVALS_OAUTH_SCOPE` | Scope requested at Intervals.icu for the identity check, default `ACTIVITY:READ`. |
| `OAUTH_ALLOWED_ATHLETES` | Comma-separated athlete ids allowed to sign in, default `ATHLETE_ID`. `i123` and `123` are the same. |
| `OAUTH_PASSWORD` or `OAUTH_PASSWORD_HASH`, `OAUTH_USERNAME` | Password sign-in (required for `password`). Username default `athlete`. |
| `API_KEY` | The Intervals.icu API key of the deployment; also the secret of the `apikey` sign-in. |
| `OAUTH_TOTP_SECRET` | Optional second factor for `password` and `apikey` (create with `python -m intervals_mcp_server.auth totp-secret`). |
| `OAUTH_CLIENT_HOSTS` | Where client metadata documents are accepted, comma-separated: a host (every path on it), `host/path` (exactly that document URL) or `host/path/` (every path below it). Default `chatgpt.com,claude.ai,claude.com`; `none` disables them. Client ids with a query string, percent-encoding, `.`/`..` or empty path segments are never accepted, and a document's redirect URIs must stay on its own host, another listed host or loopback. Documents of unknown client ids are fetched at most 10 times per minute in total; pinned ids (`host/path`), ids accepted before and ids holding a refresh token (read from the state file at startup) are never held back by that budget. Stricter (only ChatGPT's document, which also stops random client ids from causing any fetch): `chatgpt.com/oauth/client.json`. |
| `OAUTH_REDIRECT_HOSTS` | Redirect URIs allowed for dynamically registered clients: a host, an exact `host/path`, or a `host/path/` prefix. Same default; `*` allows any https host. Redirect URIs with a query string are refused; loopback http is always allowed. Stricter: `claude.ai/api/mcp/auth_callback,claude.com/api/mcp/auth_callback,chatgpt.com/connector_platform_oauth_redirect,chatgpt.com/connector/oauth/`. |
| `OAUTH_DYNAMIC_REGISTRATION` | `true` (default) or `false`. At most 10 registrations per client address and hour; 50 clients are kept (idle ones are evicted first). |
| `OAUTH_PRIVATE_KEY_JWT` | Advertise and verify `private_key_jwt` for metadata-document clients, default `true`. |
| `OAUTH_REQUIRE_PRIVATE_KEY_JWT` | Default `true`: a token request without a client assertion is refused (`invalid_client`) when the client's metadata document declares `token_endpoint_auth_method: private_key_jwt` (ChatGPT does, and signs its code and refresh requests). `false` accepts such clients without an assertion again (PKCE still protects the code). |
| `OAUTH_REFRESH_REUSE_GRACE` | Seconds in which a just-rotated refresh token gets the same answer again (the same new tokens), for a client that lost the response or refreshed twice concurrently (default 120; `0` = no grace). The grant never forks into several live chains; once the new refresh token was used, the old one is refused. The new tokens are kept in memory for this period only. |
| `OAUTH_REFRESH_REUSE_REVOKE` | Default `true`: a rotated refresh token used after the grace period revokes the whole connection (both parties then have to sign in again, which exposes a stolen token). `false` only refuses that request and keeps the connection, for a client that keeps stale copies of its refresh token. The rotation history is kept in memory: after a restart an old token is simply rejected. |
| `OAUTH_STATE_FILE` | Clients and refresh tokens, default `./oauth_state.json` (`/data/oauth_state.json` in the Docker image). Keep it on persistent storage; one server process per file. |
| `OAUTH_ACCESS_TOKEN_TTL` / `OAUTH_REFRESH_TOKEN_TTL` | Seconds, defaults 3600 and 2592000. |
| `OAUTH_LOGIN_RATE_LIMIT` | Failed sign-ins per client address (IPv4 address, IPv6 /64) per 15 minutes before `429`, default 5. |
| `OAUTH_LOGIN_GLOBAL_RATE_LIMIT` | Failed password / API-key sign-ins from all addresses together per 15 minutes before those sign-ins pause, default 500. Trade-off: it limits distributed guessing of a weak password, but enough attacking addresses (default: 100) can pause the athlete's own password sign-in for 15 minutes (existing connections keep working). With `OAUTH_TOTP_SECRET` it never pauses a sign-in, since a guess cannot succeed without the code; with a long random password a high value is fine. The Intervals.icu sign-in is not affected. |
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

Without an Intervals.icu app and without a password, the sign-in uses the API key: nothing else
to configure. Add a second factor for a public server:

```bash
python -m intervals_mcp_server.auth totp-secret      # prints OAUTH_TOTP_SECRET=... and an otpauth:// URI
```

Add the URI (or the secret) to an authenticator app and set `OAUTH_TOTP_SECRET` on the server.

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
limit keys on. A proxy on another address (Docker network, separate host) must be listed in
`FORWARDED_ALLOW_IPS` (uvicorn's setting, e.g. `FORWARDED_ALLOW_IPS=172.18.0.1`), otherwise
every visitor shares the proxy's address: five wrong passwords then lock out everybody for
15 minutes. Never use `FORWARDED_ALLOW_IPS=*` on a port that clients can reach directly.
`MCP_PUBLIC_URL` should be exactly the origin clients use, without a path.

Recommended proxy settings (not required; the server limits its own form bodies):

* **Access log without OAuth secrets.** The proxy logs full request URLs, which include the
  short-lived Intervals.icu authorization code (`/oauth/intervals/callback?code=...`) and the
  sign-in request id (`/oauth/login?request=...`, also the `Referer` of the consent form POST).
  The server's own access log keeps only the parameter names. For Apache, log the path without
  the query string and without the referrer for this vhost:

  ```apache
  LogFormat "%h %l %u %t \"%m %U %H\" %>s %b \"%{User-Agent}i\"" mcp_noquery
  CustomLog /var/log/httpd/mcp-access.log mcp_noquery
  ```

  (nginx: a `log_format` with `$uri` instead of `$request` and without `$http_referer`.)
* **Request body limit,** e.g. Apache `LimitRequestBody 4194304` (nginx `client_max_body_size 4m`,
  the limit the MCP SDK applies to its own OAuth endpoints); Apache's default allows 1 GiB.

The MCP SDK protects servers bound to localhost against DNS rebinding and then accepts only
localhost `Host` headers. A proxy that keeps the public `Host` header (Apache `ProxyPreserveHost On`)
therefore needs the public host on the allowlist: it is added automatically from `MCP_PUBLIC_URL`;
other hosts (e.g. a secret-path endpoint without OAuth) go into `FASTMCP_ALLOWED_HOSTS`
(comma-separated; `FASTMCP_ALLOWED_ORIGINS` for browser origins). Requests for other hosts get `421`.

## 4. Connect ChatGPT

1. ChatGPT → Settings → Apps / Plugins (developer mode) → create a connection with the URL
   `https://mcp.example.com/mcp` and authentication **OAuth**. Leave client id and secret
   empty: ChatGPT identifies itself with its metadata document.
2. ChatGPT opens the consent page. Check which permissions the connection gets and press
   **Continue with Intervals.icu**, or sign in with the API key or the password (plus the
   authenticator code if TOTP is enabled).
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
3. `/authorize` (PKCE S256, `resource` must name this server) leads to the consent page. The
   page sets an HttpOnly consent cookie (`__Host-` prefixed on https) and the form carries a
   token bound to it; the POST must come from that page (same origin).
4. "Continue with Intervals.icu" goes to `https://intervals.icu/oauth/authorize`, bound to
   your browser by an HttpOnly cookie. The callback exchanges the code, checks the athlete
   id against the allowlist and redirects to the client with `code`, `state` and `iss`.
5. The client redeems the code at `/token` (ChatGPT signs a `private_key_jwt` assertion
   with a key from its JWKS; it is verified, including audience, lifetime and replay, and
   required because ChatGPT's document declares it).
6. Access tokens are refreshed with rotating refresh tokens; a retry with the previous token
   within `OAUTH_REFRESH_REUSE_GRACE` gets the same answer, later reuse revokes the connection
   (unless `OAUTH_REFRESH_REUSE_REVOKE=false`).

Sign-in links live 10 minutes, codes 5 minutes; both are single use. Each client address
may have at most 20 pending sign-ins, and a full table (500) drops entries of the busiest
network first (IPv6 counted per /48), so a flood of `/authorize` requests from one address or
network cannot push out your own; an attacker with hundreds of separate networks still can.

## Operations

* **Restart:** access tokens are dropped (clients refresh silently); registered clients and
  refresh tokens survive via the state file. Deleting the file disconnects every client.
* **State file:** checked at startup (the server stops with a one-line error when its
  directory is not writable or the file cannot be read; such a file is never overwritten or
  moved). Entries a newer SDK cannot read are kept in the file unchanged. The file is
  written in a worker thread; when a write fails (disk full), the sign-in or refresh fails
  with HTTP 500 and nothing changes, so the client can retry. `futureweb-intervals-mcp --doctor`
  checks all of this without starting the server.
* **Switch from password to Intervals.icu sign-in:** set the client id/secret and
  `OAUTH_LOGIN=intervals`, restart. Existing connections keep working; the next sign-in uses
  Intervals.icu.
* **Revoke a connection:** `POST /revoke`, or delete the state file to cut off all.
* **Logs:** the server's log lines contain client ids, athlete ids and events, never
  passwords, codes or tokens; client-supplied values are escaped and shortened. uvicorn's
  access log keeps query parameter names but not their values. The reverse proxy's access
  log contains full URLs unless configured as in section 3.
* `get_server_status` shows the sign-in method and whether an Intervals.icu app is configured;
  `--doctor` (on the server) also shows the user name, the allowed athletes, the state file,
  the bind address and the SSE path, which the MCP tool does not reveal to connected clients.

## Security model

Data access uses the one Intervals.icu API key of the deployment, so this is a
single-athlete server: the allowlist decides who may connect, and the consent page decides
which permission classes each connection gets, never more than `MCP_PERMISSIONS`. A refresh
can only narrow a connection's classes, and every tool call is checked against them. The
classes are a server-side guard for the tools; they cannot make an API key with full
account access safe against a compromised host. With the Intervals.icu
sign-in there is no extra password to leak; with the password sign-in it must be as strong
as the API key itself. Multiple athletes need separate deployments. A future multi-athlete
mode would use each athlete's own Intervals.icu OAuth token for data access instead of a
shared API key.
