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
are accepted. In the default single-user mode the Intervals.icu token is used for that
check only; it is never stored and never given to the MCP client. The optional
[multi-user mode](#5-multi-user-mode-sharing-the-server) instead keeps each athlete's token
(encrypted) and uses it for that athlete's data.

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
| `OAUTH_REDIRECT_HOSTS` | Redirect URIs allowed for dynamically registered clients: a host, an exact `host/path`, or a `host/path/` prefix (the trailing `/` makes it a prefix: `chatgpt.com/connector/oauth` without it would refuse ChatGPT's per-connection `.../connector/oauth/<id>` redirect URIs). Same default; `*` allows any https host. Redirect URIs with a query string are refused; loopback http is always allowed. Stricter: `claude.ai/api/mcp/auth_callback,claude.com/api/mcp/auth_callback,chatgpt.com/connector_platform_oauth_redirect,chatgpt.com/connector/oauth/`. |
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
| `MCP_TENANCY` | `single` (default) or `multi`, see [section 5](#5-multi-user-mode-sharing-the-server). |

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

## 5. Multi-user mode (sharing the server)

With `MCP_TENANCY=multi` one deployment serves several athletes, each with their own data:

```
friend's client  <-- OAuth 2.1 -->  this server  <-- Bearer: the friend's own Intervals.icu token -->  Intervals.icu
owner's client   <-- OAuth 2.1 -->  this server  <-- API_KEY (only for ATHLETE_ID, only after the owner signed in) -->
```

* **Sign-in.** Athletes other than the owner can only connect with "Continue with Intervals.icu".
  The server keeps the access token Intervals.icu returns (and a refresh token and expiry, should
  Intervals.icu ever issue them; currently its tokens do not expire) in the grant record of the
  state file, sealed with AES-256-GCM and bound to the grant and athlete. When the owner signs in
  with Intervals.icu as `ATHLETE_ID`, the server's `API_KEY` is used and the owner's token is not
  kept. The password and API-key sign-ins are the owner's: in the multi-user mode they need
  `ATHLETE_ID`, `API_KEY` **and** `OAUTH_TOTP_SECRET` (on a page that friends use routinely they are
  the one way to the owner's key); recommended is `OAUTH_LOGIN=intervals`.
* **Fail closed.** Every token is served only with its grant record (and every token record names
  its athlete). A token without a grant record is refused, never treated as the owner's; a state
  file whose `grants` table is missing or damaged is not loaded. Tokens kept verbatim (e.g. an
  unreadable client registration) keep their grant record.
* **Per request.** The MCP access token of each request selects the connection's credential (a
  context variable, never a tool argument). `athlete_id` arguments may only name the connection's
  own athlete (`0` / `i0` are aliases of it); other ids are refused before any request. The API
  client additionally refuses every path outside `/athlete/<own id>/…` and `/activity/<id>/…`,
  sends a request for an activity's sub-resources (streams, intervals, comments …) or a write to an
  activity only when that activity belongs to the athlete (its owner is looked up once per
  connection and cached; activity listings fill the cache), refuses a fetched activity of another
  athlete, and refuses a request whose Intervals.icu scope the connection was not granted, with a
  message that says which permission to allow when reconnecting. Caches are partitioned per
  connection.
* **Scopes.** The scope requested at Intervals.icu follows the permission classes chosen on the
  consent page: `read` → `ACTIVITY:READ,WELLNESS:READ,CALENDAR:READ,LIBRARY:READ,SETTINGS:READ`;
  `write` adds `ACTIVITY`, `WELLNESS`, `CALENDAR` and `LIBRARY` `:WRITE`; `destructive` adds
  `CALENDAR`, `LIBRARY` and `SETTINGS` `:WRITE`; `admin` adds `CALENDAR` and `SETTINGS` `:WRITE`
  (`WRITE` implies `READ`). Activity comments need `CHATS`, which at Intervals.icu also covers private
  chats; it is requested only when `INTERVALS_OAUTH_OFFER_CHATS=true` and the athlete ticks
  "Activity comments" (`CHATS:READ`, with `write` `CHATS:WRITE`). Without it the two comment tools
  answer that the permission is missing. `INTERVALS_OAUTH_EXCLUDE_AREAS` never asks for further
  areas. Intervals.icu applies an athlete's latest sign-in scopes to all of their tokens, so the
  server updates the scopes of the athlete's other connections at each sign-in.
* **Budgets.** All OAuth-token connections share the Intervals.icu app limit: a daily budget per
  athlete, a 15-minute budget for all together, of which one athlete may use at most a share and
  the others leave a reserve for the owner. Every attempt counts, retries included. The owner's API
  key is not an app token and is not counted. The per-call request budget applies on top.
* **Lifecycle.** A token Intervals.icu rejects (401/403) gives a "disconnect and reconnect" message.
  Revoking a connection (`/revoke` when the client calls it on disconnect, refresh token reuse,
  retention, `grants remove`) deletes its stored token; so does the refresh token lifetime (grants
  without a live refresh token are dropped). An athlete other than the owner keeps at most `OAUTH_MAX_GRANTS_PER_ATHLETE`
  connections. A token opened with an older key of `OAUTH_TOKEN_KEY` is sealed again with the first
  key at its next use (`--doctor` counts the ones still waiting).

| Variable | Meaning |
| --- | --- |
| `MCP_TENANCY` | `multi` enables the mode; needs `MCP_AUTH=oauth`, a network transport and `OAUTH_LOGIN` with `intervals`. |
| `OAUTH_TOKEN_KEY` | Base64 key(s) of 32 bytes (`futureweb-intervals-mcp token-key`), comma-separated: the first encrypts, all decrypt (key rotation: put the new key first, remove the old one when `--doctor` reports no token waiting for it). The server refuses to start in multi-user mode without a key. |
| `OAUTH_TOKEN_KEY_FILE` | Alternative: a file with the key(s), one per line; it must not be readable by group or others (`chmod 600`). `futureweb-intervals-mcp token-key --file <path>` creates one. |
| `OAUTH_ALLOWED_ATHLETES` | The owner plus the friends. `*` (any Intervals.icu athlete) is only accepted together with `OAUTH_ALLOW_ANY_ATHLETE=true`: then anyone with an Intervals.icu account can store a token on your server and use the shared request budget. |
| `OAUTH_TOTP_SECRET` | Required in the multi-user mode when `OAUTH_LOGIN` includes `password` or `apikey`. |
| `OAUTH_TOKEN_RETENTION_DAYS` | Drop an athlete's connection and token after this many days without use (default `0` = only the refresh token lifetime, `OAUTH_REFRESH_TOKEN_TTL`, applies). |
| `OAUTH_MAX_GRANTS_PER_ATHLETE` | Connections kept per athlete (default `5`; the least recently used are revoked). The owner's connections are never evicted, neither by this cap nor by the total of 500. |
| `INTERVALS_OAUTH_OFFER_CHATS` | `true` offers the "Activity comments" checkbox (Intervals.icu `CHATS`; default `false`). |
| `INTERVALS_OAUTH_EXCLUDE_AREAS` | Further scope areas never requested. |
| `MCP_ATHLETE_DAILY_REQUESTS` | Soft budget of Intervals.icu requests per athlete and UTC day (default `1000`, `0` = off). |
| `MCP_APP_REQUESTS_PER_15MIN` | Budget of all OAuth-token connections together per 15 minutes (default `2000`, `0` = off). |
| `MCP_ATHLETE_SHARE_PERCENT` / `MCP_OWNER_RESERVED_PERCENT` | Share of that budget one athlete may use (default `25`; `0` still allows one request per window, `100` means no per-athlete limit) and the share the other athletes leave to the owner (default `20`; `100` blocks every athlete but the owner). `--doctor` warns about both edge values. The owner's API key is not counted; the reserve matters when the owner connects with an Intervals.icu token. |

```bash
futureweb-intervals-mcp token-key --file /etc/intervals-mcp/token.key
MCP_TENANCY=multi
OAUTH_TOKEN_KEY_FILE=/etc/intervals-mcp/token.key
OAUTH_LOGIN=intervals         # or intervals,password together with OAUTH_TOTP_SECRET
OAUTH_ALLOWED_ATHLETES=i123456,i234567,i345678
ATHLETE_ID=i123456
API_KEY=...                   # the owner's key, used only for the owner's own connections
# ATHLETE_TIMEZONE unset: each athlete's Intervals.icu time zone is used
```

**Switching modes.**

* *Before* switching to the multi-user mode, confirm once that the connections of the single-user
  mode are yours: `futureweb-intervals-mcp grants adopt-legacy --owner` (run it with the server's
  environment; a running server picks it up). Connections from before this release do not record
  who signed in; without this step they are refused in the multi-user mode (kept in the file, so you
  can still adopt them). Connections made with this release record the athlete: the owner's become
  owner grants automatically at the first multi-user start, those of other athletes never do.
* The single-user mode refuses to start when `OAUTH_ALLOWED_ATHLETES` names anyone but `ATHLETE_ID`
  (your own other accounts: `OAUTH_OWNER_ACCOUNTS`).
* In the multi-user mode the file is written as format 2, which older releases refuse to read
  instead of serving other athletes' connections with your API key. Back in the single-user mode,
  only your own connections are kept; the others and their tokens are removed at the next write.
* Rolling back to an older release while in the single-user mode works (it reads the file), but it
  drops the recorded athletes when it rewrites refresh tokens: connections created or refreshed
  under the older release need `grants adopt-legacy --owner` again before the next switch to the
  multi-user mode (until then they are refused, never served as the owner's).

**Managing connections.**

```bash
futureweb-intervals-mcp grants list            # athlete, kind, client, created, last used, token stored (never the token)
futureweb-intervals-mcp grants adopt-legacy --owner   # single-user connections without a recorded athlete are yours
futureweb-intervals-mcp grants remove i234567  # all connections and tokens of an athlete
futureweb-intervals-mcp grants remove --grant <id>
futureweb-intervals-mcp grants remove --legacy # the single-user connections without a recorded athlete
futureweb-intervals-mcp grants prune --days 60 # athlete connections unused for 60 days
```

Run the commands **as the service user** with the server's environment (`OAUTH_STATE_FILE`,
`ATHLETE_ID`, e.g. `sudo -u <user> env $(cat /etc/...env) futureweb-intervals-mcp grants list` or
`docker exec -u <uid>` in the container); without it they look at `./oauth_state.json`. As root they
refuse to change a state directory that belongs to another user (that user could plant links for
root to follow; `--allow-root` overrides). They never follow a link for the lock file, which must be
a plain file owned by the server user, and give files they create the owner of the state file. They
lock the state file (`<state file>.lock`); a running server
notices the change and drops removed connections at its next request. Removing a friend completely:
`grants remove <id>`, remove them from `OAUTH_ALLOWED_ATHLETES` and restart, and the friend revokes
the app at Intervals.icu (the token stays valid there until then).

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
may have at most 20 pending sign-ins, and a full table (500) drops the oldest entry of the
busiest address inside the busiest network (IPv6 counted per /48), so a flood of `/authorize`
requests from one address or network - also one sharing your /48 - cannot push out your own;
an attacker with hundreds of separate networks still can.

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
* **Revoke a connection:** `POST /revoke`, `futureweb-intervals-mcp grants remove`, or delete the
  state file to cut off all. A change of the state file by another process is picked up by the
  running server before its next request or write; it never overwrites such a change. Writers lock
  `<state file>.lock` (created next to the state file).
* **Logs:** the server's log lines contain client ids, athlete ids and events, never
  passwords, codes or tokens; client-supplied values are escaped and shortened. uvicorn's
  access log keeps query parameter names but not their values. The reverse proxy's access
  log contains full URLs unless configured as in section 3.
* `get_server_status` shows the sign-in method and whether an Intervals.icu app is configured;
  `--doctor` (on the server) also shows the user name, the allowed athletes, the state file,
  the bind address and the SSE path, which the MCP tool does not reveal to connected clients.

## Security model

In the single-user mode data access uses the one Intervals.icu API key of the deployment, so
this is a single-athlete server: the allowlist decides who may connect, and the consent page decides
which permission classes each connection gets, never more than `MCP_PERMISSIONS`. A refresh
can only narrow a connection's classes, and every tool call is checked against them. The
classes are a server-side guard for the tools; they cannot make an API key with full
account access safe against a compromised host. With the Intervals.icu
sign-in there is no extra password to leak; with the password sign-in it must be as strong
as the API key itself. Everyone on the allowlist sees the owner's data, so to share a server
use the multi-user mode (section 5): each connection then uses the connecting athlete's own
Intervals.icu token, the API key only serves the owner after the owner signed in, and an
athlete id other than the connection's own is refused before any request. The stored tokens are
encrypted with a key outside the state file; anyone with both the state file and the key (or
control of the running server) can act as every connected athlete within their granted scopes.
See [SECURITY.md](../SECURITY.md) for the threat model.
