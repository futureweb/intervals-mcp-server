<p align="center"><img src="docs/assets/icon-futureweb-128.png" alt="Futureweb Intervals MCP icon" width="128" height="128"></p>

# Futureweb Intervals MCP

**Advanced Intervals.icu MCP server for ChatGPT, Claude and every MCP client: Garmin-enriched
metrics, every custom field and stream, recovery insights and endurance performance analysis.**

[![CI](https://github.com/futureweb/intervals-mcp-server/actions/workflows/ci.yml/badge.svg)](https://github.com/futureweb/intervals-mcp-server/actions/workflows/ci.yml)
[![License: GPL-3.0](https://img.shields.io/badge/license-GPL--3.0-blue.svg)](LICENSE)
![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)
![Status: public beta](https://img.shields.io/badge/status-1.0.0b1%20public%20beta-orange)
[![Garmin Intervals Bridge](https://img.shields.io/badge/companion-Garmin%20Intervals%20Bridge-6f42c1)](https://github.com/futureweb/garmin-intervals-bridge)

A [Model Context Protocol](https://modelcontextprotocol.io) server that lets AI assistants read
and analyse your [Intervals.icu](https://intervals.icu) training data the way a coach would:
activities with every custom field and stream, intervals, wellness against personal baselines,
thresholds and zones, planned-versus-executed workouts, climbs, dual power meters, best efforts,
efficiency and fatigue resistance, nutrition and weight, training summaries and the calendar.
It runs where you run it, talks only to the Intervals.icu API and exposes nothing that writes
unless you enable it.

> **Continuation of [mvilanova/intervals-mcp-server](https://github.com/mvilanova/intervals-mcp-server).**
> The original project is no longer actively developed (last commit on 2 August 2026, 29
> pull requests left open). This community-maintained fork carries it on: the useful
> open pull requests were reviewed and merged with their authors credited, the bugs fixed, and the
> server rebuilt around coaching analysis, Garmin data and safe remote access. Not affiliated with
> Intervals.icu or Garmin.

**Companion project:** the [Garmin Intervals Bridge](https://github.com/futureweb/garmin-intervals-bridge)
puts back the metrics Garmin strips from the files it sends to Intervals.icu (stamina, training
effect, recovery time, VO₂max, running dynamics, sleep and HRV details …). This MCP is built to
read all of it, but works just as well without the bridge.

## Contents

- [Highlights](#highlights)
- [Works with the Garmin Intervals Bridge](#works-with-the-garmin-intervals-bridge)
- [Tools](#tools)
- [Quick start](#quick-start)
- [Connect an AI client](#connect-an-ai-client)
- [Sharing the server with friends](#sharing-the-server-with-friends)
- [Configuration](#configuration)
- [Permissions and security](#permissions-and-security)
- [Project status and roadmap](#project-status-and-roadmap)
- [Documentation](#documentation)
- [Development](#development)
- [Credits and license](#credits-and-license)

## Highlights

- **Every custom item, resolved dynamically.** Custom activity fields, interval fields, streams and
  wellness fields are read from your own definitions and reported with name, code, value and
  units. Nothing device-specific is hard-coded; null, NaN, zero and absent stay distinct, fields
  that do not belong to the sport are kept apart, and device loads are never mixed with the
  Intervals.icu load.
- **Streams at full resolution.** Any stream as summary, CSV or JSON, sliced by index or time,
  downsampled and paged, plus per-interval statistics of any stream (for example the stamina drop
  of each interval).
- **Coaching analysis instead of raw dumps.** One-call activity report, planned versus executed
  per step (also for deleted events and rides extended beyond the plan), climbs and descents,
  second power meter check, best efforts, similar intervals, repeated workouts over time,
  power-to-heart-rate efficiency, fatigue resistance, training load (acute:chronic ratio,
  monotony, strain), three-zone intensity distribution with polarization index, aerobic
  durability, load projection over the plan, recovery snapshot with 42-day baselines, wellness
  trends and correlations, nutrition and weight trends, weekly and monthly summaries.
  Statistics with their sample sizes; the interpretation stays with the coach.
- **Token-efficient.** `detail_level` (`compact`, `standard`, `full`) and `output_format="json"`
  on the heavy tools, nine ready-made coaching prompts and two MCP resources.
- **Safe remote access.** Built-in OAuth 2.1 server with **"Continue with Intervals.icu"**
  sign-in, a consent page with per-connection permissions, client metadata documents with
  `private_key_jwt` (ChatGPT), PKCE and RFC 9207. Streamable HTTP (`/mcp`) and SSE from one
  process.
- **Read-only by default.** Tools are grouped into permission classes (`read`, `write`,
  `destructive`, `admin`) enforced on the server; only `read` is active unless you enable more.
- **Tested.** More than 400 tests on synthetic data, ruff, mypy, CodeQL, pinned GitHub Actions,
  build and Docker smoke tests on every pull request.

## Works with the Garmin Intervals Bridge

```
Garmin device ──► Garmin Connect ──► official sync ──► Intervals.icu activity (filtered FIT)
                        │                                     ▲
                        └── Garmin Intervals Bridge ──────────┘  restores custom fields, streams, wellness
                                                              │
                                              Futureweb Intervals MCP (this project)
                                                              │
                                                 ChatGPT / Claude / any MCP client
```

The [Garmin Intervals Bridge](https://github.com/futureweb/garmin-intervals-bridge) writes the
metrics Garmin filters out into the custom activity fields, custom streams and wellness fields
you defined in Intervals.icu. The MCP reads those definitions at run time, so every restored value
shows up in the tools automatically:

| Restored by the bridge | Where the MCP shows it |
| --- | --- |
| Training effect, EPOC, Garmin training load, recovery time, VO₂max, performance condition, stamina start/end, sweat loss, temperatures | `get_activity_details`, `get_activity_report`, `get_training_summary`, `get_training_load` (device loads apart from the Intervals.icu load), `get_fueling_analysis` (sweat loss per hour), `get_activity_data_audit` (which fields still hold a zero placeholder or no value) |
| Stamina and potential stamina, second power meter, grade-adjusted speed, gear selection, running dynamics streams | `get_activity_streams`, `get_activity_intervals`, `analyze_workout_execution`, `analyze_climbs`, `compare_power_streams` |
| Night SpO₂, respiration, sleeping HR, HRV details, Body Battery, sleep stages and stress, readiness, nutrition | `get_wellness_data`, `get_recovery_snapshot`, `get_wellness_trends`, `get_nutrition_summary` |

The MCP never contacts Garmin; the bridge is optional and other devices or sync tools that fill
custom items get the same treatment. Worked examples and notes on reading the device metrics
correctly: [docs/GARMIN_BRIDGE.md](docs/GARMIN_BRIDGE.md).

## Tools

67 tools; 52 of them only read. Write tools are marked ✎ (`write`), ✖ (`destructive`) or
⚙ (`admin`) and are hidden unless their class is enabled. Most tools accept `output_format="json"`
and `detail_level` (`compact` first). Tools never take an API key: credentials come only from
the server environment.

The catalogue is kept small for the AI client: short descriptions that say when to use a tool,
a description and (where the values are fixed) an enum for every parameter, and text results
without a duplicate structured copy. Method details and the workout format live in MCP resources
(`intervals://methods/<topic>`, `intervals://workout-syntax`) and in `get_guide(topic)` for clients
that only call tools. `tools/list` is about 22.7k tokens at `MCP_PERMISSIONS=read,write`
(was 42.0k; cl100k); see [Tool sets](#tool-sets) for a smaller set.

**Activity analysis**

| Tool | What it does |
| --- | --- |
| `get_activity_report` | Complete analysis in one call: overview with fueling (carbs used/ingested per hour, sweat loss, energy), weather (temperature, feels-like, wind, head/tailwind share) and W′ balance (max depletion, time below 75/50/25 % of W′), plan vs execution or intervals, second power meter check, climbs, data-quality notes (source, upload and analysis times, recording stops, zero placeholders); `include_route_history` compares earlier activities on the same Intervals.icu route (time, power, W/kg, HR, weather, stamina); `detail_level` compact (core numbers, one context line and key findings, about 2-2.5k characters), standard or full |
| `get_activity_data_audit` | Provenance and data quality of one activity: source and file (Garmin sync, Garmin export or bridge upload, Strava stub), upload and analysis times, filtered duplicates, recording stops and gaps, FIT laps vs intervals and manual edits, device and sensor identity, streams usual for the sport but missing, per-stream coverage, custom fields with value / zero placeholder / none and what the Garmin Intervals Bridge could fill; without an activity, the per-sport data coverage of a period |
| `analyze_workout_execution` | Planned vs executed per step (duration, target adherence, time in range, HR response, fade, Pw:HR drift with the Intervals.icu decoupling sign, stamina). A step may span any number of laps; with device auto-laps step boundaries inside laps are placed at the intensity change (lap presses are kept), steps without a lap of their own are found between their neighbours, and a caveat with `alignment_confidence` says how far per-step results can be trusted. Open-ended targets (top zone) are lower bounds, distance steps are compared on distance. Steps are capped at their planned duration, longer intervals are split for the analysis, and riding beyond the plan is reported separately with its load and extra efforts. Tolerances for start shift, pauses and step length; `planned_workout_doc` for deleted events; matching events suggested read-only |
| `analyze_climbs` | Climbs, descents and pauses with power, NP, HR, VAM and custom streams per segment; grade smoothed over a distance window (raw grade optional), grade confidence high/medium/low per segment (no grade below the minimum horizontal distance, raw included), real pauses vs slow movement, sport profiles for riding, running and hiking, data-quality flags |
| `compare_power_streams` | Sample-aligned comparison of two power meters: offset, power bands, stable windows, drift, lag; without `activity_id` (date range or `activity_ids`) every ride with a second power stream is compared on its own and summarised per bike, indoor/outdoor, primary meter and second power source with n, median, between-ride SD and range (overall, per power band, stable windows), drift, lag, outliers and rides far from the median; never a correction factor |
| `get_best_efforts` | Best efforts of one activity for durations or distances, with their elapsed-time windows (end index exclusive); windows across a recording pause are flagged |
| `get_activity_histogram` | Time distribution of power, heart rate, pace or GAP |

**Activities and streams**

| Tool | What it does |
| --- | --- |
| `get_activities` | List with sport, gear and power meter filters, sorting, paging, compact or JSON output; without `start_date` the 30 days before `end_date` (or today) |
| `get_activity_details` | Summary, thresholds used (FTP, eFTP, LTHR, zones), device and power meter, running dynamics, every custom field with units, fueling, weather, W′ balance and source/freshness |
| `get_activity_intervals` | Intervals and groups, custom interval fields, per-interval statistics of any stream, optionally the planned step type next to the Intervals.icu type |
| `list_activity_streams`, `get_activity_streams` | Discover and fetch any stream: summary, CSV or JSON, slicing, downsampling, paging (2000 samples per page by default) |
| `get_activity_messages`, `add_activity_message` ✎, `update_activity` ✎ | Notes and comments, RPE, feel, name, description (`dry_run` shows the exact request) |

**Performance over time**

| Tool | What it does |
| --- | --- |
| `compare_best_efforts` | Best efforts across activities (ids, date range, sport, gear) side by side; ids are de-duplicated and capped to the limit before any request |
| `find_similar_intervals` | Activities with comparable intervals, from a reference activity or a given length and intensity; same sport family by default, ranked by comparability, with gear and power meter context; with a reference the 365 days before it by default (window shown, `start_date` for any range) |
| `compare_workouts` | Repeated workouts over time, comparing only comparable work intervals (reference activity, sport family, length, intensity, reps, FTP range); the reference activity is always fetched and always the pattern, with a name search the 365 days before it are listed; surges kept apart, time-weighted means, power, HR, cadence and whole-activity RPE trends, gear and power meter flags |
| `get_power_hr_efficiency` | Watts per heartbeat per power band and bike over time, with minimum sample sizes and filters for gear, indoor/outdoor and interval position |
| `get_fatigue_resistance` | Best power fresh vs after the athlete's kJ thresholds; without configured thresholds it explains the setting and suggests values instead of showing pseudo results |
| `get_long_ride_fatigue_profile` | HR, W/bpm, cadence and Garmin stamina at matched power in steady segments before vs after work thresholds (default 750 / 1,500 kJ, or kJ/kg), for given rides or the long rides of a period; prior work at every threshold and climb (kJ, kJ/kg, kJ and time above FTP, efforts above FTP), climbs after N hours with stamina at start and end, median and range across rides, method stated, small samples flagged |
| `get_submax_test_trends` | Submaximal fatigue tests detected by Intervals.icu (#SFT) with a validity filter (target tolerance, CV, ignored, detected inside a regular workout, hard riding before, optional HR recovery) and reasons for every excluded test; HR at the end, efficiency factor, HRRc and HR course trended per sport family and test type (power and pace never pooled) and ISO week over the valid tests only |
| `get_training_load` | Acute and chronic load, acute:chronic ratio, Foster monotony and strain (rest days as 0), deload-like weeks, per sport with the primary sport, ISO week table, CTL/ATL/form/ramp; device loads kept separate; reference ranges with sources, no verdict |
| `get_intensity_distribution` | Three-zone distribution from power, HR or pace zones (mapping by zone count; GAP zone times where Intervals.icu uses them), polarization index after Treff et al. 2019, class, hard sessions and days, drift between the halves, per sport and week, zone coverage, a caveat when totals mix power and HR zones |
| `get_durability` | Aerobic decoupling of steady long sessions after a quality filter (excluded sessions per reason), median and count above 5 %, recent vs window with a stability band, efficiency factor trend; qualifying share per sport, fewer than 8 sessions flagged, mixed indoor/outdoor, bikes or power meters pointed out; heat filter on the device sensor or the activity's weather / feels-like temperature (`temperature_source`), sessions list both temperatures |
| `get_load_projection` | CTL, ATL and form projected over the planned workouts (42/7-day model), missing planned loads reported, race days, Intervals.icu's own projection and a model check for comparison; says prominently when nothing is planned. What-if simulation (`scenario`): sessions or weekly templates that are not in the calendar (load given or estimated as hours x IF² x 100), added to or replacing the plan, compared with the calendar plan; form at a target day or the next RACE_A and a grid search for the load of the last days that reaches a given form range; per-week plan statistics (ramp, monotony, longest session, rest days) with commonly cited ranges and sources. Nothing is written |
| `get_athlete_power_curves`, `get_hr_curves`, `get_pace_curves` | Season and date-range curves |
| `get_training_summary` | Totals per week, month, sport or gear with the Intervals.icu load (power, else HR, else pace per activity) and the overlapping per-method sums, time in power zones (sweet spot apart) and HR zones, CTL/ATL at the end of each week or month; custom fields aggregated by units and meaning (sums only where they make sense, otherwise mean, median, range or change); an explicit `detail_level` also selects the JSON parts (without it the JSON is complete) |
| `get_weekly_summary`, `get_plan_compliance` | Weekly review and planned-vs-done overview |

**Wellness and recovery**

| Tool | What it does |
| --- | --- |
| `get_recovery_snapshot` | Today and the previous days, 42-day baselines, recent load and planned sessions in one call; names the usual wellness fields today's record does not have yet (not yet available, not normal) |
| `get_coach_context` | Recommended first call for a weekly analysis: overview in about 2-2.5k characters with load, fitness, intensity distribution (per-sport split when zone bases mix), HRV / resting HR / sleep 7-day means against the 42 days before them (z against the week-to-week spread of the prior 90 days), durability, top sessions, the plan of the next 7 days, today's not-yet-available wellness fields and a method line (windows, coupled ACWR, `threshold_as`, hard-session rule); an explicit `detail_level` also selects the JSON parts (without it the JSON is complete) |
| `get_wellness_trends` | Rolling means, baselines, outliers, week-over-week changes, correlations, eFTP per sport; requested period, lookback and baseline window stated separately, small samples flagged |
| `get_nutrition_summary` | Intake, device burn, energy balance on logged days, weight trend, training load per day |
| `get_fueling_analysis` | Fueling of one activity or the long sessions of a period: carbs used (Intervals.icu estimate) and ingested per moving hour, ingested share of used, energy, fluid intake, sodium and sweat loss from custom fields found by units and name (device-file zeros treated as placeholders); per sport family, and within it per duration and intensity, with sample sizes, logging coverage and Spearman correlations from 8 sessions; no targets |
| `get_wellness_data`, `update_wellness` ✎ | Daily records (`include_all_fields` adds every custom wellness field; without `start_date` the 30 days before `end_date` or today), with a line naming the usual fields today's record does not have yet (night/morning values, daily metrics such as VO2max or endurance score, and day totals apart); subjective scores (`dry_run` shows the exact request) |

**Athlete, gear, calendar and workouts**

| Tool | What it does |
| --- | --- |
| `get_athlete_profile`, `get_sport_settings`, `get_training_zones` | Profile, per-sport thresholds, zones with absolute ranges |
| `update_sport_settings` ⚙ | Validated change of FTP, LTHR, max HR or threshold pace (pace with its unit, e.g. `4:30/km`); `dry_run` shows the request with the current values |
| `get_gear_list`, `get_gear_details` | Bikes, shoes and components with mileage and maintenance reminders |
| `get_events`, `get_event_by_id`, `get_training_plan` | Calendar with the full workout document; plan phases, weekly targets, races |
| `validate_workout`, `preview_workout` | Check and render a workout document before writing it |
| `add_or_update_event` ✎, `add_or_update_note` ✎, `add_events_bulk` ⚙, `delete_event` ✖, `delete_events_by_date_range` ✖ | Calendar changes: workouts are validated before they are written, `dry_run` returns the exact request, a new event is refused when that day already has the same one (`allow_duplicate`), the answer reads back what Intervals.icu stored and parsed (steps parsed vs sent, parse warnings); deletions name what was deleted; the range delete previews by default |
| `get_workout_library`, `get_library_workout`, `create_library_workout` ✎, `add_event_from_library` ✎, `delete_library_workout` ✖ | Workout library (dry run, duplicate check when scheduling, read-back, deletions name what was deleted) |

**Custom items and server**

| Tool | What it does |
| --- | --- |
| `get_custom_items`, `get_custom_item_by_id`, `create_custom_item` ⚙, `update_custom_item` ⚙, `delete_custom_item` ✖ | Custom field, stream and chart definitions (`create_custom_item` and `update_custom_item` with `dry_run`; update: the content after the merge) |
| `get_server_status` | Version, enabled permissions, tool set, hidden tools, transport and sign-in mode, API check (also `--doctor`) |
| `get_guide` | Usage guide, workout syntax and method guides (the resources below) for clients that only call tools |

**Prompts:** `recovery_check`, `workout_deep_dive`, `weekly_training_review`,
`training_load_review`, `performance_progression`, `long_ride_climbing_analysis`,
`nutrition_weight_trend`, `power_meter_comparison`, `workout_planning_validation`, `race_week`
(taper and form on race day, fueling plan from history, weather, logistics), `fueling_review`,
`plan_health_check` (load projection and what-if scenario of the planned weeks) and
`coach_handoff` (compact summary for another coach or session).
**Resources:** `intervals://guide` (how to use the tools), `intervals://workout-syntax` (the
structured workout format), `intervals://methods/<topic>` (how an analysis works: activity-data,
execution, climbs, power-meters, load, intensity, durability, summary, comparisons, fatigue,
wellness, fueling), `intervals://custom-items` (your custom item definitions).
The server also sends short instructions in the MCP `initialize` answer (start with
`get_coach_context` or `get_activity_report`, compact first, missing values are not normal,
writes only on request after a preview), and its icon and website in `serverInfo`. The HTTP
transports serve the icon without authentication at `/favicon.ico`, `/favicon.png`, `/icon.png`,
`/icon.svg` and `/apple-touch-icon.png` (also used on the sign-in page).

### Tool sets

`MCP_TOOLSET=full` (default) registers every tool of the enabled permission classes.
`MCP_TOOLSET=core` registers a curated set of 23 tools for clients with a small tool budget (about
9.4k tokens at `MCP_PERMISSIONS=read,write`); `MCP_PERMISSIONS` still applies inside the set:

| Purpose | Core tools |
| --- | --- |
| Orientation | `get_server_status`, `get_guide` |
| Weekly review, load and plan | `get_coach_context`, `get_training_summary`, `get_load_projection`, `get_events`, `get_event_by_id`, `get_training_plan` |
| One activity | `get_activities`, `get_activity_report`, `get_activity_details`, `get_activity_intervals`, `get_best_efforts`, `get_fueling_analysis` |
| Recovery | `get_recovery_snapshot`, `get_wellness_data`, `get_wellness_trends` |
| Planning | `get_sport_settings`, `validate_workout`, `preview_workout` |
| Writes (`write` class) | `add_or_update_event` ✎, `update_wellness` ✎, `update_activity` ✎ |

`get_server_status` and `--doctor` show the active tool set and how many tools it leaves out; tool
descriptions, prompts and guides mark the tools outside the set "(full tool set)".
After a server update that changes tools or tool sets, refresh the tool list in the client
(ChatGPT: the connector's refresh in the app settings; Claude: reconnect the connector).

**Output conventions:** start times are shown local with the timezone name when Intervals.icu
stores one, otherwise with the UTC offset derived from the local and UTC start, plus UTC; run,
walk and hike cadence in steps per minute (`spm`, 2 x the per-leg value Intervals.icu stores,
which is shown as stored), bike cadence in rpm; temperatures in °C (a temperature custom field
without units takes the unit its sibling temperature fields agree on); missing values are `n/a`,
never 0.

## Quick start

Requirements: Python 3.12+, [uv](https://github.com/astral-sh/uv), an Intervals.icu API key
(Settings → Developer Settings) and your athlete ID (`i123456`).

```bash
git clone https://github.com/futureweb/intervals-mcp-server.git
cd intervals-mcp-server
uv sync --locked
cp .env.example .env                       # set API_KEY and ATHLETE_ID
uv run futureweb-intervals-mcp --doctor    # checks configuration and API access
uv run futureweb-intervals-mcp             # starts the server on stdio
```

Without cloning:

```bash
uvx --from git+https://github.com/futureweb/intervals-mcp-server futureweb-intervals-mcp --version
```

Docker: tagged releases publish `ghcr.io/futureweb/intervals-mcp-server` (`latest` only for
final versions, beta tags such as `1.0.0b1` explicitly):

```bash
docker run --rm -i -e API_KEY=... -e ATHLETE_ID=i123456 ghcr.io/futureweb/intervals-mcp-server:1.0.0b1
```

With `MCP_AUTH=oauth` mount a volume on `/data` (the image keeps `OAUTH_STATE_FILE` there), e.g.
`-v intervals-mcp:/data`; otherwise every re-created container disconnects all clients.

## Connect an AI client

### Claude Desktop and Claude Code (local, stdio)

```json
{
  "mcpServers": {
    "intervals-icu": {
      "command": "uv",
      "args": ["--directory", "/path/to/intervals-mcp-server", "run", "futureweb-intervals-mcp"],
      "env": { "API_KEY": "your-api-key", "ATHLETE_ID": "i123456", "MCP_PERMISSIONS": "read" }
    }
  }
}
```

Claude Code: `claude mcp add intervals-icu -- uv --directory /path/to/intervals-mcp-server run futureweb-intervals-mcp`.

### ChatGPT, Claude.ai and other remote clients (OAuth)

Remote clients need an HTTPS endpoint. Run the server behind a TLS reverse proxy with OAuth:

```bash
MCP_TRANSPORT=http+sse FASTMCP_HOST=127.0.0.1 FASTMCP_PORT=8001 \
MCP_AUTH=oauth MCP_PUBLIC_URL=https://mcp.example.com \
INTERVALS_OAUTH_CLIENT_ID=... INTERVALS_OAUTH_CLIENT_SECRET=... \
MCP_PERMISSIONS=read,write \
uv run futureweb-intervals-mcp
```

1. Choose the sign-in: without further settings you sign in with your Intervals.icu **API key**
   (optionally plus an authenticator code, `OAUTH_TOTP_SECRET`). For **Continue with Intervals.icu**
   create an OAuth app at <https://intervals.icu/oauth/apply> with the redirect URL
   `https://mcp.example.com/oauth/intervals/callback`; a server password is the third option.
2. In ChatGPT (developer mode) add a connection with the URL `https://mcp.example.com/mcp` and
   authentication **OAuth**; leave client id and secret empty. Claude.ai: *Add custom connector*
   with the same URL.
3. On the consent page choose the permissions for this connection and sign in. With
   Intervals.icu only the athletes in `OAUTH_ALLOWED_ATHLETES` (default `ATHLETE_ID`) can sign in.
4. After server updates use **Refresh** on the ChatGPT connection to reload the tools.

Everything about the OAuth server, the reverse proxy (Apache and nginx examples) and the
security model: [docs/REMOTE_ACCESS.md](docs/REMOTE_ACCESS.md). Clients without OAuth can use a
secret endpoint path instead (`FASTMCP_SSE_PATH=/mcp-<random>/sse`).

## Sharing the server with friends

By default the server is **single-user** (`MCP_TENANCY=single`): every tool call uses the
server's `API_KEY` and `ATHLETE_ID`. Whoever is allowed to connect sees *your* data, so in this
mode the server refuses to start when `OAUTH_ALLOWED_ATHLETES` names anyone but you (your own
other Intervals.icu accounts can be listed in `OAUTH_OWNER_ACCOUNTS`).

To let a few friends use the same deployment with **their own** Intervals.icu data, switch to the
optional **multi-user mode**:

| | `MCP_TENANCY=single` (default) | `MCP_TENANCY=multi` |
| --- | --- | --- |
| Data access | the server's `API_KEY` for every connection | each connection with its own credential |
| Friends sign in with | – (they would see your data) | "Continue with Intervals.icu" (the only way for non-owners) |
| Their Intervals.icu token | used for the identity check, never stored | stored **encrypted** (AES-256-GCM) per connection |
| Your own connections | API key | API key, only after *you* signed in (Intervals.icu as `ATHLETE_ID`, or password / API key plus authenticator code) |
| `athlete_id` arguments | any athlete the key can read | only the connection's own athlete (`0` = own); anything else is refused before a request |
| Caches, request budgets | one | per connection / per athlete, fair share of the app limit |

**Setup** (details in [docs/REMOTE_ACCESS.md](docs/REMOTE_ACCESS.md#5-multi-user-mode-sharing-the-server)):

```bash
# 1. While still in single-user mode: confirm once that your existing connections are yours
futureweb-intervals-mcp grants list
futureweb-intervals-mcp grants adopt-legacy --owner
# 2. A key for the stored tokens (mode 0600, owned by the service user; back it up separately)
futureweb-intervals-mcp token-key --file /etc/intervals-mcp/token.key
# 3. Settings, then restart and check
MCP_TENANCY=multi
OAUTH_TOKEN_KEY_FILE=/etc/intervals-mcp/token.key
OAUTH_LOGIN=intervals                     # you sign in with Intervals.icu as ATHLETE_ID (or add OAUTH_TOTP_SECRET for a password)
OAUTH_ALLOWED_ATHLETES=i123456,i234567,i345678   # you plus your friends
futureweb-intervals-mcp --doctor
```

Run the `grants` and `token-key` commands as the service user with the server's environment
(`OAUTH_STATE_FILE`, `ATHLETE_ID`), e.g. `docker exec` in the container. Connections from the
single-user mode that were not adopted are refused in the multi-user mode, never treated as yours.

Your friends add the same connector URL in ChatGPT or Claude, choose the permissions on the
consent page and approve the Intervals.icu app. The Intervals.icu permissions requested match
the classes they choose (`read` asks for `ACTIVITY`, `WELLNESS`, `CALENDAR`, `LIBRARY` and
`SETTINGS` read access; `write` and higher add the write scopes the tools need). Activity comments
need Intervals.icu's `CHATS` permission, which also covers private chats: it is never requested
unless you set `INTERVALS_OAUTH_OFFER_CHATS=true` *and* the athlete ticks "Activity comments" on
the consent page; otherwise the two comment tools say that the permission is missing.

**What is stored.** Per connection the state file keeps the athlete id, the MCP client, creation
and last-use dates, the granted Intervals.icu scopes and the athlete's Intervals.icu access token,
encrypted with the key from `OAUTH_TOKEN_KEY` / `OAUTH_TOKEN_KEY_FILE` (without the key the token
cannot be read; keep the key apart from backups of the state file). At most
`OAUTH_MAX_GRANTS_PER_ATHLETE` connections (default 5) are kept per athlete. Data is not cached on
disk; in-memory caches expire within minutes and are separated per connection.

**Privacy.** Wellness data such as HRV, sleep, resting heart rate or weight is health data (in the
EU a special category under Art. 9 GDPR), and as the operator you are responsible for it. Invite
only people who explicitly agree, tell them in a short note what is stored (above), who can see it
(you: the state file and the server logs with athlete ids, tool names, request paths and errors,
never tokens or data bodies), how long logs are kept (e.g. `journalctl --vacuum-time=14d`) and how
to leave. Keep the deployment patched. If the key or the state file may have leaked, put a new key
first in `OAUTH_TOKEN_KEY`, and ask your friends to revoke the app at Intervals.icu.

**Deletion.** A connection's stored token is deleted when the MCP client revokes the connection on
disconnect (`/revoke`; not every client does), after `OAUTH_REFRESH_TOKEN_TTL` without use (default
30 days; `OAUTH_TOKEN_RETENTION_DAYS` makes it shorter), or with the `grants` command. To remove a
friend completely:

1. `futureweb-intervals-mcp grants remove <athlete id>` (a running server drops the connections at
   its next request);
2. remove the athlete from `OAUTH_ALLOWED_ATHLETES` and restart;
3. the friend revokes the app in their Intervals.icu settings (the server cannot revoke one token
   without disconnecting all of the athlete's connections; until then the deleted token would still
   be valid at Intervals.icu);
4. optionally purge old log lines (`journalctl --vacuum-time=...`).

**Limits.** All athletes share the request limits of the one Intervals.icu OAuth app: every
athlete has a daily soft budget (`MCP_ATHLETE_DAILY_REQUESTS`, default 1000), all together a
15-minute budget (`MCP_APP_REQUESTS_PER_15MIN`, default 2000) of which one athlete may use at most
`MCP_ATHLETE_SHARE_PERCENT` (default 50) and the others leave `MCP_OWNER_RESERVED_PERCENT` (default
20) to you; retries count. The per-call limit applies on top. `get_server_status` shows the mode
and the calling connection's own athlete, scopes and budget, never other users. Switching back to
`single` drops the other athletes' connections and tokens at the next write; your own connections
keep working in both modes.

## Configuration

Environment variables; a `.env` file in the working directory is loaded automatically
([.env.example](.env.example)).

| Variable | Default | Meaning |
| --- | --- | --- |
| `API_KEY` | – | Intervals.icu API key (required) |
| `ATHLETE_ID` | – | Athlete ID, `i123456` or `123456` (required) |
| `MCP_PERMISSIONS` | `read` | Enabled tool classes, e.g. `read,write` or `all` |
| `MCP_TOOLSET` | `full` | `full` (every tool of the enabled classes) or `core` (curated set, see [Tool sets](#tool-sets)) |
| `CUSTOM_UNITS_OVERRIDES` | – | Display units per custom item code, e.g. `Stamina=%,RecoveryTime=h` |
| `CUSTOM_AGGREGATE_OVERRIDES` | – | Aggregation per custom field code in summaries (`sum`, `device_load_sum`, `trend`, `mean`, `none`), e.g. `TrainingLoad=device_load_sum` |
| `MCP_TRANSPORT` | `stdio` | `stdio`, `sse`, `http` or `http+sse` (`/mcp` and `/sse` in one process) |
| `FASTMCP_HOST` / `FASTMCP_PORT` | `127.0.0.1` / `8000` | Bind address of the HTTP transports |
| `FASTMCP_SSE_PATH` / `FASTMCP_MESSAGE_PATH` | `/sse` / `/messages/` | SSE endpoint paths |
| `FASTMCP_ALLOWED_HOSTS` / `FASTMCP_ALLOWED_ORIGINS` | host of `MCP_PUBLIC_URL` | Public Host headers accepted behind a reverse proxy (DNS rebinding protection); required for a public endpoint without OAuth |
| `MCP_AUTH` | `none` | `oauth` enables the built-in OAuth 2.1 server |
| `MCP_PUBLIC_URL` | – | Public base URL, required with `MCP_AUTH=oauth` |
| `INTERVALS_OAUTH_CLIENT_ID` / `INTERVALS_OAUTH_CLIENT_SECRET` | – | Intervals.icu OAuth app for "Continue with Intervals.icu" |
| `OAUTH_LOGIN` | `intervals` with an app, else `password` if set, else `apikey` | Sign-in method(s): `intervals`, `password`, `apikey` |
| `OAUTH_TOTP_SECRET` | – | Authenticator code as second factor for password and API-key sign-in (`python -m intervals_mcp_server.auth totp-secret`) |
| `OAUTH_ALLOWED_ATHLETES` | `ATHLETE_ID` | Athletes allowed to sign in (`*` only with `MCP_TENANCY=multi` and `OAUTH_ALLOW_ANY_ATHLETE=true`) |
| `MCP_TENANCY` | `single` | `multi`: every connection uses its own Intervals.icu credential ([Sharing the server with friends](#sharing-the-server-with-friends)) |
| `OAUTH_TOKEN_KEY` / `OAUTH_TOKEN_KEY_FILE` | – | Multi-user mode (required): key(s) that encrypt the stored Intervals.icu tokens (`futureweb-intervals-mcp token-key`) |
| `MCP_ATHLETE_DAILY_REQUESTS` / `MCP_APP_REQUESTS_PER_15MIN` | `1000` / `2000` | Multi-user mode: request budget per athlete and day, and of all athletes together per 15 minutes (`0` = off) |
| `MCP_ATHLETE_SHARE_PERCENT` / `MCP_OWNER_RESERVED_PERCENT` | `50` / `20` | Multi-user mode: share of the 15-minute budget one athlete may use, and the share kept for the owner |
| `INTERVALS_OAUTH_OFFER_CHATS` | `false` | Multi-user mode: offer "Activity comments" (Intervals.icu `CHATS`, which also covers private chats) on the consent page |
| `OAUTH_MAX_GRANTS_PER_ATHLETE` | `5` | Multi-user mode: connections kept per athlete (the least recently used are revoked) |
| `OAUTH_OWNER_ACCOUNTS` | – | Your own other Intervals.icu accounts; the only other athletes the single-user mode accepts on the allowlist |
| `OAUTH_PASSWORD_HASH` / `OAUTH_USERNAME` | – / `athlete` | Password sign-in (hash: `python -m intervals_mcp_server.auth hash-password`) |
| `OAUTH_STATE_FILE` | `./oauth_state.json` | Registered clients and refresh token digests |
| `INTERVALS_API_BASE_URL` | `https://intervals.icu/api/v1` | API base URL |
| `ATHLETE_TIMEZONE` | athlete profile | Time zone for "today" and default date ranges, e.g. `Europe/Vienna`; `server` uses the server clock. Unset: the `timezone` of the Intervals.icu athlete profile (looked up once a day) |
| `MCP_TOOL_MAX_REQUESTS` / `MCP_TOOL_TIMEOUT_S` | `300` / `120` | Limits of one tool call (Intervals.icu requests, seconds); a tool that hits them stops and says its result is incomplete |
| `MCP_MAX_OUTPUT_CHARS` | `100000` | Largest tool result; longer text is cut with a note on how to get the rest, in JSON the largest lists are cut (`truncated` says what was kept) |

Further OAuth options (client and redirect host allowlists, token lifetimes, rate limit) are
listed in [docs/REMOTE_ACCESS.md](docs/REMOTE_ACCESS.md).

## Permissions and security

| Class | Tools | Enable with |
| --- | --- | --- |
| `read` | everything that only reads | default |
| `write` | `add_or_update_event`, `add_or_update_note`, `add_activity_message`, `update_activity`, `update_wellness`, `create_library_workout`, `add_event_from_library` | `MCP_PERMISSIONS=read,write` |
| `destructive` | `delete_event`, `delete_events_by_date_range`, `delete_custom_item`, `delete_library_workout` | `MCP_PERMISSIONS=read,write,destructive` |
| `admin` | `create_custom_item`, `update_custom_item`, `add_events_bulk`, `update_sport_settings` | `MCP_PERMISSIONS=all` |

Tools of a disabled class are not registered at all. With OAuth, each connection additionally
gets only the classes granted on the consent page (`intervals:read`, `intervals:write`, …);
other tools are hidden from it and refused if called.

Write tools that can replace existing values (`add_or_update_event`, `add_or_update_note`,
`update_activity`, `update_wellness`) carry the MCP hint `destructiveHint: true`, so clients ask
before running them; their class stays `write`. Nothing is created, paired, renamed or deleted
automatically: `delete_events_by_date_range` first only lists what matches; it deletes only with
`dry_run=false` and the confirmed ids from that list (`confirm_ids`), touches only the named
categories (default planned workouts), at most 31 days, never workouts already paired with an
activity unless asked. Empty values from a client never wipe existing text or workouts.

Every write is predictable and verifiable (details: write safety in `intervals://guide`):

- `dry_run=true` on every create/update tool (including `create_custom_item`) returns the exact
  request (method, path, body after all defaults and merges, as compact JSON) and the validation
  result; no write request is sent (the server refuses every non-GET request during a dry run).
- Creating an event (single, bulk or from the library) first reads the events of its date (one
  request over the date range) and refuses a duplicate (same category and sport with the same name,
  or the same non-trivial workout or text) unless `allow_duplicate=true`; a brick day or an AM/PM
  pair with different names is not a duplicate. The bulk tool decides per entry, lists the refused
  ones and creates planned double sessions (same name, different content).
- After every event or library workout write the answer reads back what Intervals.icu stored and
  parsed: date, name, category, sport, duration, load, steps parsed vs sent and parse warnings
  (notes: date, name, category, text length).
- `delete_event`, `delete_library_workout` and `delete_custom_item` read the object first and name
  what was deleted; a missing id deletes nothing.

- Credentials never appear in logs or tool output; in the single-user mode the Intervals.icu
  sign-in token is used for the identity check only and never stored (in the multi-user mode it
  is stored encrypted, see above).
- Never expose the HTTP transports without OAuth or a secret path, and always behind TLS.
- In the single-user mode one deployment serves one athlete's API key and the sign-in allowlist
  decides who may connect; share a deployment only in the multi-user mode.

Details and how to report a vulnerability: [SECURITY.md](SECURITY.md).

## Project status and roadmap

`1.0.0b1` is the first public beta of this fork. Development happens in reviewed pull requests;
`main` is protected and every change runs the full CI.

**Done**
- Custom fields, custom streams and interval statistics for any device data
  (also offered upstream as [mvilanova/intervals-mcp-server#153](https://github.com/mvilanova/intervals-mcp-server/pull/153))
- Coaching tools, permission classes, upstream fixes and merged community pull requests
- Performance analytics, execution analysis for deleted and extended workouts, detail levels,
  one-call activity report, prompts and resources
- OAuth with "Continue with Intervals.icu", per-connection permissions, client metadata
  documents, streamable HTTP and SSE in one process
- Analytics quality: extended rides split correctly, only comparable intervals compared,
  custom field aggregation by meaning, clear wellness periods, minimum sample sizes for
  efficiency trends, honest fatigue resistance, smoothed climb grades, compact report
- Training load and intensity: acute:chronic ratio, monotony and strain, three-zone distribution
  with polarization index, aerobic durability, load projection and a weekly coach context

**Next**
- PyPI package and the first tagged release with a GHCR image
- Optional multi-athlete mode that uses each athlete's own Intervals.icu OAuth token
- Migration to MCP SDK v2 once it is stable for the transports used here

## Documentation

| Document | Content |
| --- | --- |
| [docs/REMOTE_ACCESS.md](docs/REMOTE_ACCESS.md) | OAuth server, Intervals.icu sign-in, ChatGPT and Claude setup, reverse proxy, operations |
| [docs/GARMIN_BRIDGE.md](docs/GARMIN_BRIDGE.md) | Working with the Garmin Intervals Bridge, worked examples, reading device metrics |
| [docs/FEATURE_COMPARISON.md](docs/FEATURE_COMPARISON.md) | Comparison with other Intervals.icu MCP servers |
| [docs/UPSTREAM_AUDIT.md](docs/UPSTREAM_AUDIT.md) | Every open upstream pull request and what happened to it |
| [docs/RELEASE_CHECKLIST.md](docs/RELEASE_CHECKLIST.md) | Release process and gates |
| [CHANGELOG.md](CHANGELOG.md) | Changes per version |
| [SECURITY.md](SECURITY.md) | Security policy and vulnerability reporting |

## Development

```bash
uv sync --all-extras --locked
uv run --locked pytest                 # synthetic fixtures, no credentials needed
uv run --locked ruff check .
uv run --locked mypy src tests
uv run --locked --with pylint pylint --disable=C0301 $(git ls-files '*.py')   # advisory
```

CI runs ruff, mypy and pytest on Python 3.12 and 3.13, builds and imports the wheel and sdist,
builds and smoke-tests the Docker image, and CodeQL scans the code. All GitHub Actions are pinned
to commit SHAs; Dependabot keeps them and the dependencies current. Releases are built from tags
([docs/RELEASE_CHECKLIST.md](docs/RELEASE_CHECKLIST.md)); beta tags become GitHub pre-releases.

Contributions are welcome: see [CONTRIBUTING.md](CONTRIBUTING.md). Never put real athlete data,
API keys or hostnames into issues, fixtures or logs.

## Credits and license

Original project by [Marc Vilanova](https://github.com/mvilanova) and contributors:
[mvilanova/intervals-mcp-server](https://github.com/mvilanova/intervals-mcp-server). This fork
integrates community pull requests by
[arnold-maderthaner](https://github.com/arnold-maderthaner)
([#140](https://github.com/mvilanova/intervals-mcp-server/pull/140),
[#142](https://github.com/mvilanova/intervals-mcp-server/pull/142) to
[#147](https://github.com/mvilanova/intervals-mcp-server/pull/147)),
[biochaos](https://github.com/biochaos) ([#131](https://github.com/mvilanova/intervals-mcp-server/pull/131)) and
[kokostitiahah](https://github.com/kokostitiahah) ([#149](https://github.com/mvilanova/intervals-mcp-server/pull/149)), and fixes reported and
the training load and intensity metrics proposed by [morritter](https://github.com/morritter)
([#150](https://github.com/mvilanova/intervals-mcp-server/pull/150));
thank you. Maintained by [Futureweb](https://www.futureweb.at), together with the
[Garmin Intervals Bridge](https://github.com/futureweb/garmin-intervals-bridge).

Licensed under the GNU General Public License v3.0, see [LICENSE](LICENSE). Intervals.icu and
Garmin are trademarks of their respective owners and are used only to describe compatibility.
