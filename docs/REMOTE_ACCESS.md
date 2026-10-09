# Remote access with OAuth (ChatGPT, Claude, other remote MCP clients)

Remote MCP clients such as ChatGPT connectors or Claude's custom connectors only
support "no authentication" or OAuth 2.1 with dynamic client registration, PKCE
(S256) and public clients. The server ships an optional, single-user OAuth 2.1
authorization server for exactly that. It is off by default (`MCP_AUTH=none`);
nothing changes unless you enable it.

## What you get

* One user: a username (default `athlete`) and one password.
* Standard endpoints served by the MCP SDK: `/.well-known/oauth-authorization-server`,
  `/.well-known/oauth-protected-resource`, `/authorize`, `/token`, `/register`, `/revoke`.
* A minimal login page served by this project: `GET`/`POST /oauth/login`.
* Bearer tokens enforced on the MCP transport (`/sse` + `/messages/`, or `/mcp`).
* Access tokens live in memory (default 1 h); clients and refresh tokens (default 30 days)
  are persisted in `OAUTH_STATE_FILE` (mode 0600).

## Configuration

| Variable | Meaning |
| --- | --- |
| `MCP_AUTH` | `none` (default) or `oauth`. |
| `MCP_PUBLIC_URL` | Public base URL clients use, e.g. `https://mcp.example.com`. Required with `oauth`; it is the OAuth issuer and resource identifier. `http://` is only accepted for `localhost`/`127.0.0.1`. |
| `OAUTH_PASSWORD` or `OAUTH_PASSWORD_HASH` | The login password, plain or hashed (exactly one of them). |
| `OAUTH_USERNAME` | Login name, default `athlete`. |
| `OAUTH_STATE_FILE` | JSON file for registered clients and refresh tokens, default `./oauth_state.json`. Keep it on a persistent volume. |
| `OAUTH_ACCESS_TOKEN_TTL` / `OAUTH_REFRESH_TOKEN_TTL` | Seconds, defaults 3600 and 2592000. |
| `OAUTH_LOGIN_RATE_LIMIT` | Failed logins per client IP per 15 minutes before `429`, default 5. |

Create a hash instead of storing the plain password:

```bash
python -m intervals_mcp_server.auth hash-password          # prompts, or reads stdin
OAUTH_PASSWORD_HASH='pbkdf2_sha256$600000$...$...'         # quote it: it contains "$"
```

Minimal `.env` for a public deployment:

```bash
MCP_TRANSPORT=sse            # or http
FASTMCP_HOST=127.0.0.1       # the reverse proxy talks to this
MCP_AUTH=oauth
MCP_PUBLIC_URL=https://mcp.example.com
OAUTH_PASSWORD_HASH='pbkdf2_sha256$...'
OAUTH_STATE_FILE=/var/lib/intervals-mcp/oauth_state.json
```

## Reverse proxy

TLS terminates at the proxy; the server itself speaks plain HTTP on localhost.
The proxy must

* forward `/.well-known/`, `/authorize`, `/token`, `/register`, `/revoke`,
  `/oauth/login` and the MCP paths (`/sse` and `/messages/`, or `/mcp`) unchanged;
* send `X-Forwarded-Proto: https` and `X-Forwarded-For` (uvicorn trusts them from
  127.0.0.1, which is what the login rate limit keys on);
* not buffer or time out the SSE stream (nginx: `proxy_buffering off;` and a long
  `proxy_read_timeout`).

`MCP_PUBLIC_URL` must be exactly the origin the client is given (scheme, host, port).
Use a dedicated hostname without a path prefix; with a prefix such as
`https://mcp.example.com/secret` the proxy would have to strip `/secret` for every
route except `/.well-known/...`, which must stay as is.

## What a client does on first use

1. ChatGPT / Claude is given `https://mcp.example.com/sse` (or `/mcp`), gets `401`
   with a `WWW-Authenticate: Bearer resource_metadata=...` header and reads both
   metadata documents.
2. It registers itself via `POST /register` (public client, `token_endpoint_auth_method`
   `none`) and receives a `client_id`.
3. It opens `/authorize` in your browser; you land on `/oauth/login`, enter
   username and password and are redirected back to the client with a code.
4. The client exchanges the code (with its PKCE verifier) at `/token` for an access
   token and a refresh token and uses the access token as a bearer token.
5. When the access token expires it uses the refresh token; each use rotates it.

The login link is valid for 10 minutes, the code for 5 minutes and both are single use.

## Operations

* **Restart:** access tokens are gone (clients refresh silently); registered clients
  and refresh tokens survive via the state file. Deleting the file forces every client
  to register and log in again.
* **Rotate the password:** change `OAUTH_PASSWORD(_HASH)` and restart. Existing
  refresh tokens stay valid until they expire; delete the state file to cut them off,
  or revoke through `/revoke`.
* **Logs** contain client ids and events only, never passwords, codes or tokens.
* `/register` is reachable without credentials by design; the client table is capped
  (oldest idle registrations are evicted) so it cannot grow unbounded.

## Security model

The server uses one Intervals.icu API key. Everyone who can log in sees and, depending
on `MCP_PERMISSIONS`, changes that athlete's data, so the OAuth password protects the
athlete's data and must be as strong as the API key itself. Give the server its own
hostname, keep `MCP_PERMISSIONS` as small as possible, and do not share the login.
Multiple athletes need separate deployments (separate API key, password and state file);
this is not a multi-user server.
