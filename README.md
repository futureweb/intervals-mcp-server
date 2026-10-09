# Futureweb Intervals MCP

**Advanced Intervals.icu MCP server with Garmin-enriched metrics, full custom streams, recovery
insights and endurance performance analysis.**

[![CI](https://github.com/futureweb/intervals-mcp-server/actions/workflows/ci.yml/badge.svg)](https://github.com/futureweb/intervals-mcp-server/actions/workflows/ci.yml)
[![License: GPL-3.0](https://img.shields.io/badge/license-GPL--3.0-blue.svg)](LICENSE)
![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)

A [Model Context Protocol](https://modelcontextprotocol.io) server that lets ChatGPT, Claude
and other MCP clients read and analyse your [Intervals.icu](https://intervals.icu) data the way
a coach would: activities with every custom field and stream, intervals, wellness with personal
baselines, thresholds and zones, planned-vs-executed workouts, climbs, dual power meters,
nutrition and weight trends, weekly and monthly summaries, and the calendar.

> Community-maintained fork of [mvilanova/intervals-mcp-server](https://github.com/mvilanova/intervals-mcp-server).
> Not affiliated with Intervals.icu or Garmin. Works with any standard Intervals.icu account;
> the optional [Garmin Intervals Bridge](https://github.com/futureweb/garmin-intervals-bridge)
> adds the Garmin metrics that are otherwise filtered out. All training data and credentials
> stay under your control: the server runs where you run it and talks only to the
> Intervals.icu API.

## What sets it apart

- **Every custom item, resolved dynamically.** Custom activity fields, interval fields, streams
  and wellness fields are read from your account's definitions and shown with name, code,
  value and units: training effect, EPOC, recovery time, VO₂max, performance condition,
  stamina, sweat loss, running dynamics, gear selection, Body Battery, sleep stages … whatever
  is there. Nothing device-specific is hard-coded; null, NaN, zero and absent are distinguished.
- **Streams at full resolution.** Any stream as summary, CSV or JSON, sliced by index or time,
  downsampled and paged; per-interval statistics of custom streams (e.g. stamina drop per interval).
- **Coaching analysis, no black boxes.** Recovery snapshot with 42-day baselines, wellness
  trends and correlations, planned-vs-executed workout analysis, climb/descent segmentation,
  dual power meter comparison, nutrition/calorie/weight trends, training summaries with the
  Intervals.icu load kept apart from device loads. Statistics only; the interpretation is yours.
- **Safe by default.** Tools are grouped into permission classes (`read`, `write`,
  `destructive`, `admin`); only `read` is exposed unless you enable more. Secrets are never
  logged. See [SECURITY.md](SECURITY.md).
- **Built on the community's work.** Includes the best upstream pull requests (weekly summary
  and plan compliance, HR/pace curves, workout library, bulk calendar events, update tools)
  with their authors credited in the history; see [docs/UPSTREAM_AUDIT.md](docs/UPSTREAM_AUDIT.md)
  and [docs/FEATURE_COMPARISON.md](docs/FEATURE_COMPARISON.md).

## Quick start

Requirements: Python 3.12+, [uv](https://github.com/astral-sh/uv), an Intervals.icu API key
(Intervals.icu → Settings → Developer Settings) and your athlete ID (`i123456`).

```bash
git clone https://github.com/futureweb/intervals-mcp-server.git
cd intervals-mcp-server
uv sync --locked
cp .env.example .env                       # fill API_KEY and ATHLETE_ID
uv run futureweb-intervals-mcp --doctor    # checks configuration and API access
uv run futureweb-intervals-mcp             # starts the server on stdio
```

Run without cloning (from git until the package is on PyPI):

```bash
uvx --from git+https://github.com/futureweb/intervals-mcp-server futureweb-intervals-mcp --version
```

Docker (image published by the release workflow to `ghcr.io/futureweb/intervals-mcp-server`):

```bash
docker run --rm -i -e API_KEY=... -e ATHLETE_ID=i123456 ghcr.io/futureweb/intervals-mcp-server:latest
```

## Configuration

All settings are environment variables (a `.env` file in the working directory is loaded
automatically; see [.env.example](.env.example)).

| Variable | Default | Meaning |
| --- | --- | --- |
| `API_KEY` | – | Intervals.icu API key (required) |
| `ATHLETE_ID` | – | Athlete ID, `i123456` or `123456` (required) |
| `MCP_PERMISSIONS` | `read` | Enabled tool classes, e.g. `read,write` or `all` |
| `CUSTOM_UNITS_OVERRIDES` | – | Display units per custom item code, e.g. `Stamina=%,RecoveryTime=h` |
| `MCP_TRANSPORT` | `stdio` | `stdio`, `sse` or `http` |
| `FASTMCP_HOST` / `FASTMCP_PORT` | `127.0.0.1` / `8000` | Bind address for `sse`/`http` |
| `FASTMCP_SSE_PATH` / `FASTMCP_MESSAGE_PATH` | `/sse` / `/messages/` | Endpoint paths; a secret path segment turns the URL into a credential |
| `MCP_AUTH` | `none` | `oauth` enables the built-in single-user OAuth 2.1 server for remote clients (see [docs/REMOTE_ACCESS.md](docs/REMOTE_ACCESS.md)) |
| `MCP_PUBLIC_URL`, `OAUTH_PASSWORD` / `OAUTH_PASSWORD_HASH` | – | Required with `MCP_AUTH=oauth` |
| `INTERVALS_API_BASE_URL` | `https://intervals.icu/api/v1` | API base URL |

### Permission classes

| Class | Tools | Enable with |
| --- | --- | --- |
| `read` | everything that only reads (activities, streams, wellness, analysis, curves, calendar, library, status) | default |
| `write` | `add_or_update_event`, `add_or_update_note`, `add_activity_message`, `update_activity`, `update_wellness`, `create_library_workout`, `add_event_from_library` | `MCP_PERMISSIONS=read,write` |
| `destructive` | `delete_event`, `delete_events_by_date_range`, `delete_custom_item`, `delete_library_workout` | `MCP_PERMISSIONS=read,write,destructive` |
| `admin` | `create_custom_item`, `update_custom_item`, `add_events_bulk`, `update_sport_settings` | `MCP_PERMISSIONS=all` |

Tools of a disabled class are not registered at all; `get_server_status` lists what is enabled
and hidden.

## Connecting a client

### Claude Desktop / Claude Code (stdio)

`claude_desktop_config.json` (macOS: `~/Library/Application Support/Claude/`, Windows:
`%APPDATA%\Claude\`):

```json
{
  "mcpServers": {
    "intervals-icu": {
      "command": "uv",
      "args": ["--directory", "/path/to/intervals-mcp-server", "run", "futureweb-intervals-mcp"],
      "env": { "API_KEY": "your_key", "ATHLETE_ID": "i123456", "MCP_PERMISSIONS": "read,write" }
    }
  }
}
```

On Windows use double backslashes in paths and the full path to `uv.exe` (`where.exe uv`).
Claude Code: `claude mcp add intervals-icu -- uv --directory /path/to/intervals-mcp-server run futureweb-intervals-mcp`.

### ChatGPT and other remote clients (SSE / HTTP)

```bash
MCP_TRANSPORT=sse FASTMCP_HOST=127.0.0.1 FASTMCP_PORT=8000 uv run futureweb-intervals-mcp
```

ChatGPT custom connectors support only "no authentication" or OAuth, so two protections are
built in and documented in [docs/REMOTE_ACCESS.md](docs/REMOTE_ACCESS.md):

1. **Secret path** (immediate, works with every client): set `FASTMCP_SSE_PATH=/mcp-<random>/sse`
   and `FASTMCP_MESSAGE_PATH=/mcp-<random>/messages/`, let the reverse proxy forward only that
   prefix and deny everything else. The URL then acts as a credential (it never appears in
   certificate transparency logs, unlike a hostname).
2. **Built-in OAuth 2.1 server** (`MCP_AUTH=oauth`): dynamic client registration, PKCE, a login
   page with the configured password; ChatGPT and Claude run the OAuth flow on first use.

Keep the server bound to localhost behind a TLS-terminating reverse proxy that forwards
`X-Forwarded-Proto`, and never expose the SSE/HTTP transport directly on a public interface.
One Intervals.icu API key serves everyone who can log in: this is a single-user deployment.
See [SECURITY.md](SECURITY.md).

## Tools

All tools accept `athlete_id` / `api_key` overrides and most accept `output_format="json"`.

**Activities & streams**
- `get_activities` – list with sport/gear filters, sorting, pagination, compact or JSON output
- `get_activity_details` – summary, thresholds used (FTP, eFTP, LTHR, zones, power meter), running dynamics, every custom field with units
- `get_activity_intervals` – intervals and groups, custom interval fields, per-interval statistics of any stream (`stream_types="custom"`)
- `list_activity_streams` / `get_activity_streams` – discover and fetch any stream (summary, CSV, JSON; slicing, downsampling, paging)
- `get_activity_messages`, `add_activity_message`\*, `update_activity`\* (RPE, feel, name, description)

**Analysis**
- `get_activity_report` – one-call compact analysis: overview, plan vs execution (or intervals), second power meter check, climbs, data notes (3–4 API calls)
- `analyze_workout_execution` – planned vs executed per step (duration, target adherence, time in range, HR response, fade, drift, stamina); accepts `planned_workout_doc` for deleted events, reports additional training after the plan separately and suggests matching events for unpaired activities (read-only)
- `analyze_climbs` – climbs, descents and pauses from the streams with power, NP, HR, VAM, grade and custom streams per segment
- `compare_power_streams` – sample-aligned comparison of two power meters (offset, bands, stable windows, drift, lag, best efforts)
- `get_training_summary` – totals per week/month/sport/gear with separate load sources, time in zones, feel/RPE, custom field aggregates
- `get_weekly_summary`, `get_plan_compliance` – quick weekly review and planned-vs-done overview
- `get_athlete_power_curves`, `get_hr_curves`, `get_pace_curves`

**Performance analytics**
- `get_best_efforts` – best efforts of one activity per duration/distance with time windows
- `compare_best_efforts` – best efforts across activities (ids, date range, sport, gear)
- `find_similar_intervals` – activities with intervals of a given duration and intensity (optionally reps, target type, sport, gear, dates)
- `get_activity_histogram` – power / HR / pace / GAP time distribution
- `compare_workouts` – the same workout type over weeks (work intervals, NP, HR, Pw:HR, load)
- `get_power_hr_efficiency` – W/bpm per power band over time
- `get_fatigue_resistance` – fresh vs fatigued (after kJ) power curves

**Wellness & recovery**
- `get_wellness_data` – daily records; `include_all_fields=True` adds every custom wellness field with its name and units
- `get_recovery_snapshot` – today and the previous days, baselines, recent activities and planned sessions in one call
- `get_wellness_trends` – rolling means, baselines, outliers, week-over-week, correlations, eFTP per sport
- `get_nutrition_summary` – intake, device burn, balance (logged days only), weight trend, training load per day
- `update_wellness`\* – subjective scores and comments

**Athlete, thresholds, gear**
- `get_athlete_profile`, `get_sport_settings`, `get_training_zones`, `update_sport_settings`\*\*\*
- `get_gear_list`, `get_gear_details` (components, mileage, maintenance reminders)

**Calendar & workouts**
- `get_events`, `get_event_by_id` (with the full workout document), `get_training_plan` (phases, weekly targets, races), `add_or_update_event`\*, `add_or_update_note`\*, `add_events_bulk`\*\*\*, `delete_event`\*\*, `delete_events_by_date_range`\*\*
- `validate_workout`, `preview_workout` – check a workout document before writing it
- `get_workout_library`, `get_library_workout`, `create_library_workout`\*, `add_event_from_library`\*, `delete_library_workout`\*\*

**Custom items & server**
- `get_custom_items`, `get_custom_item_by_id`, `create_custom_item`\*\*\*, `update_custom_item`\*\*\*, `delete_custom_item`\*\*
- `get_server_status` – version, permissions, hidden tools, API check, custom item counts (also `--doctor` on the command line)

\* write · \*\* destructive · \*\*\* admin

Most tools accept `output_format="json"`; `get_activity_details`, `get_activity_intervals` and
`get_recovery_snapshot` also take `detail_level="compact" | "standard" | "full"` for token-efficient answers.

**Prompts** (reusable workflows for MCP clients that support them): `recovery_check`,
`workout_deep_dive`, `weekly_training_review`, `performance_progression`,
`long_ride_climbing_analysis`, `nutrition_weight_trend`, `power_meter_comparison`,
`workout_planning_validation`. **Resources:** `intervals://guide` (how to use the tools),
`intervals://custom-items` (the athlete's custom item definitions).

## Garmin Intervals Bridge

Since Garmin started filtering the FIT files it sends to partners, Intervals.icu no longer
receives Garmin's own metrics. The [Garmin Intervals Bridge](https://github.com/futureweb/garmin-intervals-bridge)
restores them into the custom fields and streams you define; this MCP then shows them
everywhere. The bridge is optional and the MCP never contacts Garmin itself. Details and
worked examples: [docs/GARMIN_BRIDGE.md](docs/GARMIN_BRIDGE.md).

## Development

```bash
uv sync --all-extras --locked
uv run --locked pytest -q          # tests use synthetic fixtures, no credentials needed
uv run --locked ruff check .
uv run --locked mypy src tests
uv run --locked --with pylint pylint --disable=C0301 $(git ls-files '*.py')   # advisory
```

CI runs ruff, mypy and pytest on Python 3.12 and 3.13; releases are built from tags
([docs/RELEASE_CHECKLIST.md](docs/RELEASE_CHECKLIST.md)). Contributions: [CONTRIBUTING.md](CONTRIBUTING.md).
Please never put real athlete data, API keys or hostnames into issues, fixtures or logs.

## Credits and license

Original project by [Marc Vilanova](https://github.com/mvilanova) and contributors. This fork
integrates community pull requests by arnold-maderthaner (#140, #142–#147), biochaos (#131)
and kokostitiahah (#149); thank you. Licensed under the GNU General Public License v3.0, see
[LICENSE](LICENSE). Intervals.icu and Garmin are trademarks of their respective owners and are
used only to describe compatibility.
