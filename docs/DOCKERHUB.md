# Futureweb Intervals MCP

Self-hosted [Model Context Protocol](https://modelcontextprotocol.io) server for
[Intervals.icu](https://intervals.icu): lets ChatGPT, Claude and other MCP clients read and analyze
your training the way a coach would (activities with every custom field and stream, intervals,
wellness against personal baselines, training load, planned versus executed workouts, the
calendar). Works with the [Garmin Intervals Bridge](https://github.com/futureweb/garmin-intervals-bridge)
but does not need it.

Source, documentation and issues: <https://github.com/futureweb/intervals-mcp-server>

This image is the same multi-arch image (linux/amd64, linux/arm64) as
`ghcr.io/futureweb/intervals-mcp-server`; both are built and published by the release workflow
of the GitHub repository.

## Tags

| Tag | Meaning |
| --- | --- |
| `X.Y.Z` (e.g. `1.0.0`) | a final release |
| `X.Y` | the newest final release of that minor version |
| `latest` | the newest final release |
| `X.Y.ZbN`, `X.Y.ZrcN` (e.g. `1.0.0b1`) | a beta or release candidate, only under its own tag |

## Run (stdio, for local MCP clients)

`1.0.0b1` is the current beta; from 1.0.0 on `latest` points to the newest final release.

```bash
docker run --rm -i -e API_KEY=your-api-key -e ATHLETE_ID=i123456 futurewebat/futureweb-intervals-mcp:1.0.0b1
```

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "intervals-icu": {
      "command": "docker",
      "args": ["run", "--rm", "-i", "-e", "API_KEY", "-e", "ATHLETE_ID", "-e", "MCP_PERMISSIONS",
               "futurewebat/futureweb-intervals-mcp:1.0.0b1"],
      "env": { "API_KEY": "your-api-key", "ATHLETE_ID": "i123456", "MCP_PERMISSIONS": "read" }
    }
  }
}
```

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `API_KEY` | – | Intervals.icu API key (Settings → Developer Settings), required |
| `ATHLETE_ID` | – | Athlete ID, e.g. `i123456`, required |
| `MCP_PERMISSIONS` | `read` | Enabled tool classes: `read`, `read,write` or `all` |
| `MCP_TOOLSET` | `full` | `full` or `core` (curated coaching set) |
| `ATHLETE_TIMEZONE` | athlete profile | Time zone for "today", e.g. `Europe/Vienna` |
| `MCP_TRANSPORT` | `stdio` | `stdio`, `sse`, `http` or `http+sse` |

Every variable is described in the
[README](https://github.com/futureweb/intervals-mcp-server#configuration).

## Remote clients (ChatGPT, Claude.ai)

The network transports (`MCP_TRANSPORT=http+sse`) listen on `127.0.0.1` inside the container
unless `FASTMCP_HOST=0.0.0.0` is set. Publish the port on the host loopback only
(`-p 127.0.0.1:8000:8000`) and put a TLS reverse proxy in front of it; for remote clients enable
the built-in OAuth server (`MCP_AUTH=oauth`) and mount a volume on `/data` for its state
(`-v intervals-mcp:/data`). Setup, proxy examples and the security model:
[docs/REMOTE_ACCESS.md](https://github.com/futureweb/intervals-mcp-server/blob/main/docs/REMOTE_ACCESS.md).

The container runs as an unprivileged user (uid 10001) and talks only to the Intervals.icu API.

## License

GPL-3.0-only. Community-maintained continuation of
[mvilanova/intervals-mcp-server](https://github.com/mvilanova/intervals-mcp-server). Not affiliated
with Intervals.icu or Garmin.
