# Feature comparison: Futureweb Intervals MCP vs. other Intervals.icu MCP servers

Date: 2026-10-09

## 1. Scope, method, licences

**Our project.** `futureweb/intervals-mcp-server` (GPL-3.0-only, Python, FastMCP via `mcp[cli]`), fork of
`mvilanova/intervals-mcp-server`. Positioning: Garmin-enriched metrics, full custom streams, recovery insights
and endurance performance analysis for practical coaching, not full API coverage.

Inventory of our tools at the time of the audit (36; the release adds `analyze_workout_execution`, `analyze_climbs`, `compare_power_streams`, `get_recovery_snapshot`, `get_wellness_trends`, `get_nutrition_summary`, `get_training_summary`, `validate_workout`, `preview_workout`, `get_server_status` = 46 tools, three prompts and the permission classes described in the README):

| Module | Tools |
|---|---|
| activities (8) | `get_activities`, `get_activity_details`, `get_activity_intervals`, `get_activity_streams`, `list_activity_streams`, `get_activity_messages`, `add_activity_message`, `update_activity` |
| athlete (3) | `get_athlete_profile`, `get_sport_settings`, `get_training_zones` (read-only, `output_format` text or json) |
| custom_items (5) | `get_custom_items`, `get_custom_item_by_id`, `create_custom_item`, `update_custom_item`, `delete_custom_item` |
| events (7) | `get_events`, `get_event_by_id`, `add_or_update_event`, `add_or_update_note`, `add_events_bulk`, `delete_event`, `delete_events_by_date_range` |
| gear (2) | `get_gear_list`, `get_gear_details` |
| curves (3) | `get_athlete_power_curves`, `get_hr_curves`, `get_pace_curves` |
| training_review (2) | `get_weekly_summary` (end-of-week CTL/ATL/form, HR-zone time, per-sport split), `get_plan_compliance` (planned vs. executed, missed/unplanned/upcoming) |
| wellness (2) | `get_wellness_data`, `update_wellness` |
| workout_library (4) | `get_workout_library`, `create_library_workout`, `add_event_from_library`, `delete_library_workout` |

Distinctive today: custom activity/interval/wellness fields and custom streams (e.g. Garmin stamina,
performance condition, second power meter) are resolved against the athlete's custom item definitions
(`utils/custom_fields.py`, `utils/streams.py`); `get_activity_streams` offers CSV/JSON, index/time slicing,
`downsample` and `max_points`; `get_activity_intervals` evaluates any stream per interval. Transports: stdio,
SSE, streamable HTTP (`server_setup.py`). No MCP resources, prompts, tool annotations or destructive-tool gating.
Tests: 16 files, 187 test functions.

**In progress (treated as "in progress" below):** `utils/power_compare.py` (dual power meter comparison,
present with tests), `utils/wellness_stats.py` (trends, baselines, outliers, lagged correlations, weight slope,
nutrition balance, present with tests), `utils/segments.py` (climb/descent segmentation, present with tests) and the planned tools `get_recovery_snapshot`, `get_wellness_trends`,
`get_nutrition_summary`, `get_training_summary`, `analyze_workout_execution`, `analyze_climbs`,
`compare_power_streams`, `validate_workout`/`preview_workout`, JSON output and permission classes
(read/write/destructive/admin).

**Method.** For each project: GitHub page and `api.github.com/repos` metadata (licence, `pushed_at`), raw README,
the git tree, and 2-4 source files plus at least one test file read via raw.githubusercontent.com. Statements that
rest on the README alone are marked "(from README only)". Links in the matrix point to the file that was read.

**Reachability and licences (all eight reachable on 2026-10-09):**

| Project | Licence | Language | Last push | Stars |
|---|---|---|---|---|
| hhopke/intervals-icu-mcp | MIT | Python | 2026-10-08 | 89 |
| ricardocabral/icuvisor | MIT | Go | 2026-10-09 | 35 |
| eddmann/intervals-icu-mcp | MIT | Python | 2025-11-06 | 36 |
| eoinoconn/intervals-mcp-server | GPL-3.0 | Python | 2026-05-29 | 12 |
| teoruiz/intervals-mcp | MIT | Go + TypeScript | 2026-10-09 | 2 |
| rbrands/intervals-icu-sync | MIT | Python | 2026-10-05 | 19 |
| andiarenaleandro-ux/intervals-icu-mcp | MIT | Python | 2026-08-31 | 0 |
| derrix060/intervals-mcp | MIT | Go | 2026-03-05 | 3 |

**Licence rule applied in this document.** GPL-3.0 code (eoinoconn, same licence family as ours) may be adapted
with attribution. MIT code may be integrated with attribution (keep the MIT notice next to the integrated code).
No code is copied from a project with an incompatible or missing licence; none of the eight falls into that
category. Go/TypeScript code is never copied, only the algorithm or design is adapted ("adopt idea").

## 2. Project profiles

### hhopke/intervals-icu-mcp (MIT)
Purpose: full read/write Intervals.icu server with structured workout generation; 211 commits since 2026-03,
actively maintained. Transport: stdio default, `--transport http|sse|streamable-http`, bind `127.0.0.1`.
70 tools prefixed `icu_` in 11 groups (activities 13, analysis 8, messages 2, athlete 5, wellness 3,
calendar 12, curves 3, library 8, gear 6, sport settings 5, custom items 5), 4 resources, 9 prompts.
Notable: best efforts, interval search, 4 histograms, fitness chart (daily CTL/ATL from wellness, projected
days), annual training plan reader, bulk create/duplicate events, apply training plan, file downloads, stream
upload, multi-athlete `icu_list_athletes` with `access`/`can_write`. Security: `INTERVALS_ICU_DELETE_MODE`
safe/full/none decides at startup which delete tools are *registered*; every tool carries
readOnlyHint/destructiveHint/idempotentHint/openWorldHint; no HTTP auth (docs recommend tunnel/proxy).
Tests: 31 files (pytest + respx), including `test_delete_mode.py` and transport integration. Module layout and
prompt names are identical to eddmann's project, i.e. a continuation of that code base (lineage not verified
via fork metadata). Output: compact JSON envelope `{data, metadata[, analysis]}`.

### ricardocabral/icuvisor (MIT)
Purpose: local Go binary ("MCP connector") for Intervals.icu with an analysis layer; 1,173 commits since
2026-05, pushed today. Transport: local binary for the MCP client; optional hosted HTTPS connector
(from README only). 73 catalogued tools in 9 groups (activities, analyzers, coach, custom-items, events,
fitness, settings, wellness, workout-library, meta), 13 prompts, 5 resources (workout syntax, event
categories, custom item schemas, analysis formulas, athlete profile). Notable: analyzer family
(`get_climb_segments`, `analyze_trend/distribution/correlation/efforts_delta`, `compute_zone_energy/zone_time/
load_balance/baseline/compliance_rate/workout_progression/training_monotony`), `get_today`,
`get_data_quality_report`, `validate_workout` (offline), `get_extended_metrics` (running dynamics, DFA a1 with
provenance), `propose/apply_annual_training_plan`, terse/`include_full` responses with `_meta`.
Security: `ICUVISOR_DELETE_MODE` safe/full/none (capability `CanWrite/CanDelete`), `ICUVISOR_TOOLSET`
compact/core/full, per-tool `Requirement` read/write/delete, gating at registration, no `confirm` arguments by
policy, coach-mode threat model, OS keychain credential store (file present, not reviewed).
Tests: `_test.go` beside nearly every file, golden files, adversarial catalog matrix (63/71/49 tools in
safe/full/none), schema snapshots, tool-routing benchmark.

### eddmann/intervals-icu-mcp (MIT)
Purpose: general Intervals.icu server; 2 commits, last push 2025-11-06 (stale). Transport: stdio (Claude
Desktop launches `uv run`/`docker run -i`). 48 tools in 9 groups (hyphenated names, e.g. `get-best-efforts`,
`search-intervals`, `get-power/hr/pace/gap-histogram`, `get-fitness-summary`), 1 resource
(`intervals-icu://athlete/profile`), 6 prompts. Security: none; delete tools for activities, events, gear and
sport settings are always registered; `ConfigMiddleware` only validates credentials. Tests: one file with 3
tests (respx), shallow. Output: JSON via `ResponseBuilder`.

### eoinoconn/intervals-mcp-server (GPL-3.0)
Purpose: fork of our upstream (mvilanova) tuned for coaching conversations and a Render deployment; 153
commits, last push 2026-05-29. Transport: stdio and streamable HTTP (`MCP_TRANSPORT=http`, `/mcp`, binds
`0.0.0.0:8000`; README warns the URL is public and unauthenticated). 26 tools in 5 groups; 1 resource
`intervals-icu://guide` (concepts, load metric definitions, recommended tool workflows). Notable:
`get_training_summary` (4 concurrent calls; per-week planned/completed/wellness/CTL/ATL/TSB, `ac_ratio`,
compact JSON, "first call in any coaching conversation"), `get_activity_histogram`, `compact=True` on
`get_activities`, `ToolAnnotations(title, readOnlyHint, destructiveHint)` on every tool. Security: annotations
only, no gating. Tests: 16 files; `test_training_summary.py` has 47 tests, `test_tool_annotations.py` checks all
27 annotations. Same package name and module layout as ours, so code transfers with little friction.

### teoruiz/intervals-mcp (MIT)
Purpose: deliberately small read-only server plus CLI; 15 commits, created 2026-06, pushed today.
Transport: stdio, local HTTP on `127.0.0.1:8080`, remote via Cloudflare Worker. 8 tools, flat list:
`today_context`, `list_recent_activities`, `get_activity`, `get_recovery`, `list_wellness`, `list_calendar`,
`search`, `fetch` (search/fetch pair for ChatGPT-style connectors). Notable: `today_context` gathers
activities, wellness, athlete summary and events in parallel and adds nutrition totals (carbs used/ingested,
net estimate) and explicit availability notes; Garmin running-dynamics streams (`GarminGCT`, `GarminVO`,
`GarminStepLength`, ...) averaged per activity and per interval. Security: read-only by construction (only
GET), GitHub OAuth with user allowlist that fails closed when empty, PKCE, dynamic client registration,
proxy headers stripped; egress deny-by-default (from README only). Tests: Go tests with a fake client (9 in
`insights`), worker security tests, CI. No tool annotations.

### rbrands/intervals-icu-sync (MIT)
Purpose: GenAI coaching toolkit (scripts + prompts + Joe-Friel coaching logic) with an MCP front end; 236
commits, pushed 2026-10-05. Transport: local SSE; hosted Streamable HTTP + SSE on Azure App Service with a
self-built OAuth 2.0 provider. 9 local tools (`prepare_week_data`, `get_coach_input`, `get_fueling_analysis`,
`get_latest_metrics`, `get_activity_streams_sampled`, `list_library_workouts`, `save/validate/upload_week_plan`),
3 resources, 8 prompts (German default). Notable: fueling analysis (carbs/h, fueling ratio, duration bands,
flags), fueling planner (g/h targets per ride type), W'bal (Skiba), training-readiness traffic light.
Security: hosted tokens are Fernet-encrypted payloads that embed athlete id and API key; auth code is
stateless (replayable within 10 min); `upload_week_plan(clear=True)` deletes all WORKOUT events in range,
`dry_run` previews. Tests: 14 unittest files, credential-free via dry runs.

### andiarenaleandro-ux/intervals-icu-mcp (MIT)
Purpose: single-athlete "physiological analysis" server; 12 commits, last push 2026-08-31, 0 stars.
Transport: stdio. 48 tools in 10 groups, including .fit parsing, aerodynamics (CdA), SQLite "memory" and
analytics (`analyze_session`: CCI = HR / %FTP per work interval, HRV z-score correction factor, freshness
matrix, flags; `compare_sessions` trend labels). Security: none (writes and `delete_event` unconditional).
Tests: none ("No automated tests or CI" in README). The analytics are self-defined heuristics.

### derrix060/intervals-mcp (MIT)
Purpose: generate every Intervals.icu endpoint as a tool from the OpenAPI spec at startup (144 tools); 7
commits, last push 2026-03-05. Transport: stdio (inferred from client config). Tools named by `operationId`,
grouped by OpenAPI tag, `INTERVALS_INCLUDE_TAGS`/`INTERVALS_EXCLUDE_TAGS`. Security: none beyond tag
filtering; all HTTP methods including DELETE are exposed; responses are raw JSON without truncation.
Tests: one table-driven test (`formatValue`).

## 3. Feature matrix

Legend: Unser MCP = present / in progress / missing (tool); Empfehlung = adopt idea / adapt code / skip;
Aufwand = S/M/L; Priorität = P1..P3 from a coaching perspective.

### Athlete & training management

| Feature | Unser MCP | Vergleichsprojekt | Empfehlung | Aufwand | Priorität |
|---|---|---|---|---|---|
| Profile / sport settings | present, read-only (`get_athlete_profile`, `get_sport_settings`, `get_training_zones`) | hhopke 5 sport-settings tools incl. update/apply/delete; icuvisor update/create/delete ([tools.golden.json](https://github.com/ricardocabral/icuvisor/blob/main/cmd/gendocs/testdata/tools.golden.json)) | skip (keep read-only; thresholds change in the UI) | S | P3 |
| Power/HR/pace curves | present (3 tools, periods, GAP) | all; icuvisor `get_best_efforts` by sport and bucket | skip | - | P3 |
| Best efforts & interval comparison across activities | missing | hhopke `icu_get_best_efforts` (per activity), `icu_search_intervals` (type/duration/intensity, API-backed) ([activity_analysis.py](https://github.com/hhopke/intervals-icu-mcp/blob/main/src/intervals_icu_mcp/tools/activity_analysis.py)); icuvisor `analyze_efforts_delta` vs. baseline | adopt idea: `get_best_efforts` wrapper + "same interval across activities" comparison feeding `analyze_workout_execution` | M | P1 |
| Power/HR/pace/GAP histograms | missing | eoinoconn `get_activity_histogram(histogram_type, bucket_size)` on `/activity/{id}/power-histogram`, `hr-histogram`, `pace-histogram` ([activities.py](https://github.com/eoinoconn/intervals-mcp-server/blob/develop/src/intervals_mcp_server/tools/activities.py)); hhopke/eddmann 4 separate tools incl. GAP | adapt code (GPL, same package layout); one tool, text rendering of buckets | S | P2 |
| Activity search by tags/gear/sport | missing (`get_activities`: dates, limit, unnamed) | hhopke `icu_search_activities` by name/tag ([activities.py](https://github.com/hhopke/intervals-icu-mcp/blob/main/src/intervals_icu_mcp/tools/activities.py)); icuvisor paginated index + cursor; andiarenaleandro `get_activities_by_sport` (from README only) | adopt idea: local filters `sport_type`, `tags`, `gear_id`, `name_contains` plus cursor | S | P2 |
| CTL/ATL/form history & annual training plan | partial (`get_weekly_summary` end-of-week values; no ATP) | hhopke `icu_get_fitness_chart` (wellness `ctl/atl/rampRate/ctlLoad/atlLoad`, <=365 d, `is_projected`) and `icu_get_annual_training_plan` (PLAN/TARGET/NOTE events) ([athlete.py](https://github.com/hhopke/intervals-icu-mcp/blob/main/src/intervals_icu_mcp/tools/athlete.py), [periodization.py](https://github.com/hhopke/intervals-icu-mcp/blob/main/src/intervals_icu_mcp/tools/periodization.py)); icuvisor `get_fitness`, `get_fitness_projection`, `propose/apply_annual_training_plan` | adopt idea: daily fitness series with downsampling (M); ATP reader (S) | M | P2 |
| Workout library & plans | present (4 tools) | hhopke 8 tools (folders, bulk create, update); eoinoconn `get_workout`, `update_workout`; icuvisor `apply_training_plan`, `get_planning_context` | skip for now; `update_library_workout` later | S | P3 |
| Weekly/monthly summaries | present (`get_weekly_summary`, text); `get_training_summary` in progress | eoinoconn `get_training_summary` (planned + completed + wellness per week, `ac_ratio`, 47 tests) ([training_summary.py](https://github.com/eoinoconn/intervals-mcp-server/blob/develop/src/intervals_mcp_server/tools/training_summary.py)); icuvisor `get_training_summary` | adapt code (GPL) for our JSON `get_training_summary`, keep text weekly summary | M | P1 |
| Planned vs executed comparison | present (`get_plan_compliance`) | icuvisor `compute_compliance_rate`, `get_today.completion_load_evidence` (target > 0, exactly one linked activity) ([get_today.go](https://github.com/ricardocabral/icuvisor/blob/main/internal/tools/get_today.go)); eoinoconn `compliance_pct` | skip (ours is more complete); add per-event load delta to the recovery snapshot | S | P3 |

### Coaching & analytics

| Feature | Unser MCP | Vergleichsprojekt | Empfehlung | Aufwand | Priorität |
|---|---|---|---|---|---|
| Today context / recovery snapshot | in progress (`get_recovery_snapshot`) | teoruiz `today_context` (parallel fetch of activities, wellness, athlete-summary, events; nutrition totals; availability notes) ([service.go](https://github.com/teoruiz/intervals-mcp/blob/main/internal/insights/service.go)); icuvisor `get_today` (athlete-local date, `_meta.as_of/timezone`, planned vs. completed load evidence); hhopke `icu_get_fitness_summary` (TSB/ramp thresholds + recommendations) | adopt idea: factual aggregation with athlete timezone and "no data" notes; no score, no recommendation text | M | P1 |
| Multi-day wellness baselines | in progress (`wellness_stats.py`: trailing means, baselines, outliers, lagged correlations) | icuvisor `compute_baseline`, `analyze_trend`, `analyze_correlation`, `analyze_distribution` with refuse-on-missing-data policy and coverage metadata ([compute_training_monotony.go](https://github.com/ricardocabral/icuvisor/blob/main/internal/tools/compute_training_monotony.go) shows the pattern); rbrands `avg_7d`/`trend_7d` | keep ours; adopt idea: report expected vs. received days and missing dates explicitly | S | P1 |
| Workout execution analysis | in progress (`analyze_workout_execution`) | icuvisor `compute_workout_progression` (user-supplied ordered activity IDs); andiarenaleandro `analyze_session` (per-interval HR drift = first vs. last work interval) ([analytics.py](https://github.com/andiarenaleandro-ux/intervals-icu-mcp/blob/main/server/tools/analytics.py)); hhopke intervals with targets | keep ours; adopt idea: target-vs-actual per step, simple HR drift between work intervals; no CCI/HRV factors | M | P1 |
| Power/HR/pace efficiency comparisons | partial (EF/decoupling from API in `get_activity_details`) | icuvisor `get_extended_metrics` (`pw_hr`, decoupling, zone times, provenance per field) ([get_extended_metrics.go](https://github.com/ricardocabral/icuvisor/blob/main/internal/tools/get_extended_metrics.go)); andiarenaleandro EF by zone | adopt idea: EF/decoupling trend over activities of one type using API fields only | M | P2 |
| Climb/descent segmentation | in progress (`utils/segments.py`, `analyze_climbs`) | icuvisor `get_climb_segments`: 1 m resampling, grade >= 3 %, gain >= 30 m, gap <= 100 m, bridged loss <= 5 m, VAM, avg HR/power, `data_quality`, max 100 segments; descents not reported ([climb_segments.go](https://github.com/ricardocabral/icuvisor/blob/main/internal/analysis/climb_segments.go)) | adapt algorithm with attribution (MIT, Go -> Python); add descents and per-segment stamina drop | M | P1 |
| Dual power meter comparison | in progress (`power_compare.py`, 12 tests) | none of the eight projects | keep (differentiator) | - | P1 |
| Garmin stamina / potential stamina / performance condition | present (custom fields/streams resolved by definition; `get_activity_intervals(stream_types="Stamina")`) | none expose stamina; teoruiz Garmin running dynamics per activity/interval ([streams.go](https://github.com/teoruiz/intervals-mcp/blob/main/internal/intervals/streams.go)); icuvisor running dynamics with `source_kind` provenance | keep; adopt idea: running-dynamics aggregation per interval for runners | S | P2 |
| Nutrition / calorie / weight trends | in progress (`nutrition_summary`, `weight_trend` in `wellness_stats.py`) | teoruiz `Nutrition` block (carbs used/ingested, net estimate); icuvisor `update_activity` carb intake + `fueling_review` prompt ([catalog.go](https://github.com/ricardocabral/icuvisor/blob/main/internal/prompts/catalog.go)) | keep; adopt idea: per-activity carbs_used vs. carbs_ingested line | S | P2 |
| Fueling & hydration analysis | missing | rbrands `fueling_analysis.py`: carbs/h, fueling ratio, duration bands (<1.5 h none, 1.5-2 h optional, >=2 h required), flags for long rides ([fueling_analysis.py](https://github.com/rbrands/intervals-icu-sync/blob/main/scripts/fueling_analysis.py)); icuvisor hydration in `update_wellness` (test name only) | adopt idea: `get_fueling_summary` from activity fields with configurable g/h bands (cite rbrands); no planner | M | P2 |
| Long-term endurance / hill performance | missing (curves only) | icuvisor `analyze_efforts_delta`, `compute_workout_progression`, `get_performance_potential` (summarises upstream fields, "without estimating hidden thresholds") ([performance_potential.go](https://github.com/ricardocabral/icuvisor/blob/main/internal/analysis/performance_potential.go)); andiarenaleandro `compare_sessions` | adopt idea: repeated-climb comparison on top of `analyze_climbs` (same segment over months: time, VAM, W/kg, HR, stamina drop) | L | P2 |

### MCP usability

| Feature | Unser MCP | Vergleichsprojekt | Empfehlung | Aufwand | Priorität |
|---|---|---|---|---|---|
| MCP resources | missing | eoinoconn `intervals-icu://guide` ([guide.py](https://github.com/eoinoconn/intervals-mcp-server/blob/develop/src/intervals_mcp_server/resources/guide.py)); hhopke 4 (profile, workout syntax, event categories, custom item schemas) ([server.py](https://github.com/hhopke/intervals-icu-mcp/blob/main/src/intervals_icu_mcp/server.py)); icuvisor 5 (+ analysis formulas) | adapt code (GPL guide) and add `custom-item-schemas` (ours is dynamic) and `workout-syntax` | S | P1 |
| Reusable MCP prompts | missing | icuvisor 13 (`weekly_review`, `fueling_review`, `race_week_taper`, `coaching_handoff`, ...); hhopke 9; eddmann 6; rbrands 8 | adopt idea: 4-6 prompts that name the tool order (weekly review, recovery check, ride analysis, plan week) | S | P2 |
| Structured JSON output | partial (`output_format=json` on profile/settings/zones/events/streams); planned project-wide | hhopke/eddmann compact envelope `{data, metadata[, analysis]}` ([response_builder.py](https://github.com/hhopke/intervals-icu-mcp/blob/main/src/intervals_icu_mcp/response_builder.py)); icuvisor output schemas + `_meta` (server_version, as_of, timezone, source); teoruiz typed structs | adopt idea: one envelope with `_meta` (as_of, timezone, athlete_id, source endpoints); text stays default | M | P1 |
| Compact token-efficient output | present (text summaries, custom-field lines, stream summary mode) | icuvisor terse default + `include_full`, null stripping with `_meta.missing` ([shape.go](https://github.com/ricardocabral/icuvisor/blob/main/internal/response/shape.go)) and `ICUVISOR_TOOLSET` compact/core/full to shrink the catalog; hhopke light/full tool pairs; eoinoconn `compact=True` | adopt idea: `include_full` convention and an env-selected tool tier (core vs. full) | M | P2 |
| Pagination / downsampling of streams | present (index/time windows, `downsample`, `max_points`, CSV/JSON) | rbrands `max_points=300` + time/distance windows ([mcp_server.py](https://github.com/rbrands/intervals-icu-sync/blob/main/scripts/mcp_server.py)); hhopke uniform step thinning; icuvisor activity cursor pagination | keep; add cursor on `get_activities` | S | P3 |
| Diagnostics / healthcheck tools | missing | icuvisor `get_data_quality_report` (7 sections, severity, recommendations) ([get_data_quality_report.go](https://github.com/ricardocabral/icuvisor/blob/main/internal/tools/get_data_quality_report.go)), `icuvisor_check_server_version`, `icuvisor_list_advanced_capabilities`; teoruiz `config doctor`; hhopke `verify_setup` prompt + startup log of delete mode and tool count | adopt idea: `check_setup` (credentials, athlete, custom items and Garmin-bridge fields detected, permission level) + data-coverage report | S | P1 |
| Tool description quality | present (long docstrings, explicit WRITE/DELETE markers) | icuvisor first-sentence summaries, `toolchecks` for confusable names and schema stability, routing benchmark ([catalog_tiers_test.go](https://github.com/ricardocabral/icuvisor/blob/main/internal/tools/catalog_tiers_test.go)); hhopke docstrings steer to the light tool first | adopt idea: "use when / do not use when" first line and a test that every tool has title + annotations | S | P2 |

### Security

| Feature | Unser MCP | Vergleichsprojekt | Empfehlung | Aufwand | Priorität |
|---|---|---|---|---|---|
| Read-only default | missing (all writes and deletes always registered) | teoruiz read-only by construction ([server.go](https://github.com/teoruiz/intervals-mcp/blob/main/internal/mcpserver/server.go)); icuvisor `ICUVISOR_DELETE_MODE=none` = read-only; hhopke `none` still keeps writes | adopt idea: env-selected level, default "write" (non-destructive), `read` available for shared/remote setups | M | P1 |
| Permission classes | in progress (read/write/destructive/admin) | icuvisor `Requirement` read/write/delete + toolset tiers ([registry.go](https://github.com/ricardocabral/icuvisor/blob/main/internal/tools/registry.go)); eoinoconn `ToolAnnotations(title, readOnlyHint, destructiveHint)` with full test ([test_tool_annotations.py](https://github.com/eoinoconn/intervals-mcp-server/blob/develop/tests/test_tool_annotations.py)); hhopke 4 hints per tool | adapt code (eoinoconn annotations + test, GPL) and icuvisor's design | M | P1 |
| Destructive tool gating server-side | missing | hhopke conditional registration by delete mode; safe mode deletes only events dated tomorrow or later in server TZ, empty folders only, `skipped[{id, reason, hint}]` envelope ([tools.md](https://github.com/hhopke/intervals-icu-mcp/blob/main/docs/tools.md), [test_delete_mode.py](https://github.com/hhopke/intervals-icu-mcp/blob/main/tests/test_delete_mode.py)); icuvisor registration gate, no `confirm` arguments, pre-delete echo, date-range cap ([adversarial_test.go](https://github.com/ricardocabral/icuvisor/blob/main/internal/safety/adversarial_test.go)); rbrands `dry_run` | adapt pattern: register deletes only at level destructive/admin; future-only guard for calendar deletes; echo deleted records | M | P1 |
| Remote auth | missing (SSE/HTTP without auth) | teoruiz Cloudflare Worker with GitHub OAuth allowlist (fails closed), PKCE, stripped proxy headers ([security.ts](https://github.com/teoruiz/intervals-mcp/blob/main/worker/src/security.ts)); rbrands own OAuth provider (API key inside encrypted token, replayable code) ([oauth_provider.py](https://github.com/rbrands/intervals-icu-sync/blob/main/webservice/oauth_provider.py)); hhopke/eoinoconn: no auth, docs recommend tunnel/proxy | adopt idea: bind `127.0.0.1` by default, document tunnel/proxy, refuse `0.0.0.0` unless explicitly set; OAuth deferred | S | P2 |

## 4. Recommended additions (prioritised)

1. **Permission classes with server-side destructive gating (P1, M).** Introduce an env level
   `read < write < destructive < admin`. Tools declare their class once; registration (not the handler)
   decides whether a tool exists, so no prompt can reach an unregistered tool (hhopke/icuvisor pattern). Add
   `readOnlyHint`/`destructiveHint`/`title` annotations and a test that fails when a tool lacks them (adapt
   eoinoconn's `test_tool_annotations.py`, GPL). Calendar deletes at level `destructive` only touch future
   events unless `admin`; deletes return the deleted records.
2. **`get_training_summary` as JSON (P1, M).** eoinoconn's implementation (GPL, same package layout) already
   merges athlete-summary, activities, wellness and events per week with `ac_ratio` and 47 tests. Adapt it with
   attribution, keep our text `get_weekly_summary`, and add custom wellness fields (Garmin bridge) per week.
3. **Recovery snapshot / today context (P1, M).** Follow teoruiz and icuvisor: parallel fetch, athlete-local
   date, explicit "no wellness record for this date" notes, planned-vs-completed load evidence, nutrition
   totals. Report numbers and baselines from `wellness_stats.py`; do not emit a readiness verdict.
4. **MCP resources: usage guide, custom item schemas, workout syntax (P1, S).** A guide resource
   (adapt eoinoconn's `guide.py`) tells the client which tool to call first and how load metrics are defined; a
   dynamic `custom-item-schemas` resource exposes the athlete's Garmin fields, which is our unique input.
5. **`check_setup` diagnostics tool (P1, S).** Verify credentials and athlete id, list detected custom
   items/streams (stamina, performance condition, second power meter), report the active permission level and
   transport, and log tool count + level at startup (hhopke `_emit_startup_log`). Extend later with a
   data-coverage report in the style of icuvisor's `get_data_quality_report`.
6. **Climb segmentation thresholds and data-quality block (P1, M, in progress).** Reuse icuvisor's
   validated parameters (3 % grade, 30 m gain, 100 m gap, 5 m bridged loss, 1 m resampling) and its
   `data_quality` block as the baseline for `utils/segments.py`; add descents and stamina/HR drift per
   segment, which icuvisor does not provide.
7. **Best efforts and cross-activity interval comparison (P1, M).** Wrap the best-efforts and interval
   search endpoints (as hhopke/eddmann do) and add a local "same workout, N executions" comparison so that
   `analyze_workout_execution` can show progression of identical steps.
8. **Uniform JSON envelope with `_meta` (P1, M).** When `output_format=json`, return
   `{data, _meta{as_of, timezone, athlete_id, sources, units}}` for every tool (hhopke's compact
   `separators=(",", ":")`, icuvisor's `_meta` fields). Text remains the default to keep token use low.
9. **Histograms (P2, S).** One `get_activity_histogram(histogram_type, bucket_size)` adapted from eoinoconn,
   rendered as a compact text table with zone shares.
10. **Activity search filters (P2, S).** `sport_type`, `tags`, `gear_id`, `name_contains` and a cursor on
    `get_activities`; filtering is local on the list payload we already fetch.
11. **Fueling summary (P2, M).** Carbs/h and fueling ratio from `carbs_ingested`/`carbs_used`/duration per
    activity with configurable bands (rbrands' bands as documented defaults, cited); long-ride flags only;
    no planner and no race-day prescriptions.
12. **Prompts (P2, S).** Four to six prompt templates that fix the tool order (weekly review, recovery check,
    ride analysis with climbs and power-meter check, plan next week). icuvisor's `coaching_handoff` (compact
    source-labelled Markdown for a new conversation) is worth copying as an idea.

### Deliberately not adopted

- **Dynamic OpenAPI tool generation** (derrix060): 144 tools, raw JSON passthrough, all DELETE endpoints
  exposed; contradicts the focus on curated coaching tools and token economy.
- **Proprietary readiness formulas and traffic lights** (rbrands `training_readiness.py` weights and vetoes,
  hhopke TSB/ramp recommendation thresholds): hand-set constants without published validation; we report
  trends and baselines, the coach decides.
- **AI/heuristic performance scores** (andiarenaleandro CCI, HRV z-score "correction factor", freshness
  matrix, cardiac-suppression flags): unvalidated, single-author metrics.
- **Mixing Garmin exercise load with Intervals.icu load**: Garmin training effect/EPOC stay custom fields
  shown as-is next to Intervals load; they are never summed or averaged together.
- **Local SQLite "memory"/agent notes, .fit parsing, aerodynamics/CdA tools** (andiarenaleandro): outside the
  Intervals.icu data model.
- **Write/delete tools for sport settings, gear and activities, stream upload, bulk manual activities**
  (hhopke, eddmann): high blast radius, low coaching value.
- **Hosted multi-tenant OAuth** (rbrands): API key embedded in bearer tokens and replayable auth codes; if
  remote access is needed, follow teoruiz's allowlist-in-front-of-container model instead.
- **Fully self-built annual-plan proposer** (icuvisor `propose_annual_training_plan`): planning stays with the
  coach; we only read ATP events.

## 5. Security patterns worth adopting

### hhopke/intervals-icu-mcp
- **Registration-time gate.** `src/intervals_icu_mcp/server.py` reads
  `_DELETE_MODE = load_config().intervals_icu_delete_mode` once and registers delete tools conditionally
  (`if _DELETE_MODE == "full": mcp.tool(name="icu_delete_activity", ...)` and
  `if _DELETE_MODE in ("safe", "full"): mcp.tool(name="icu_delete_event", ...)`). Comment in source: the
  safety floor sits outside the model's reach, no parameter can summon an unregistered tool.
- **Three modes with a documented table** (`docs/tools.md`): `safe` (default, 67 tools: event deletes only
  for dates strictly after today in server TZ, gear and library workouts, empty folders), `full` (70),
  `none` (62). Rationale for the one-day buffer: absorbs server-vs-athlete timezone skew; Docker defaults to
  UTC, so `TZ` must be set.
- **Skip envelope instead of silent refusal.** Delete tools return `deleted`, `deleted_count`, `skipped`,
  `skipped_count`, each skipped entry with `id`, `reason` (`past_event`, `folder_not_empty`),
  `start_date_local`, `hint`.
- **Annotations on every tool**: `readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint` passed in
  `mcp.tool(name=..., annotations=...)`.
- **Credential validation per call** in `middleware.py` (`ConfigMiddleware.on_call_tool` raises `ToolError`
  "credentials not configured" before any handler runs).
- **Tests**: `tests/test_delete_mode.py` reloads the server module per mode and asserts the exact registered
  set (`assert "icu_delete_activity" not in names`), plus validation of invalid values
  (`"INTERVALS_ICU_DELETE_MODE must be one of"`).
- **Remote deployment doc**: no built-in auth, bind `127.0.0.1` by default, `0.0.0.0` only inside a container;
  recommends Tailscale/Cloudflare Tunnel, authenticating reverse proxy or SSH tunnel.

### ricardocabral/icuvisor
- **Capability object** (`internal/safety/mode.go`): `ICUVISOR_DELETE_MODE` -> `Capability{CanWrite(),
  CanDelete(), Mode()}`; `CanDelete` only in `full`; empty or unknown values resolve to `safe`
  ("misconfiguration never unlocks deletes"); the resolved mode is logged once at startup.
- **Per-tool requirement** (`internal/tools/registry.go`): `Tool{Requirement: read|write|delete, Toolset:
  compact|core|full}`; `RequiresWrite()` is true for write and delete. Unmarked tools default to the `full` tier
  so new tools "do not silently expand core".
- **Gate order is fixed** (`docs/threat-models/coach-mode.md`): process-global delete mode, then toolset tier,
  then coach ACL; any deny is final, a later gate cannot re-enable a tool. `athlete_id` is a selector, never a
  credential; no silent fallback to the default athlete; malformed and unknown IDs yield the same error class.
- **Registrar** (`internal/mcp/registrar_tools.go`): `safeRegistrar.AddTool` applies capability, toolset and
  coach-mode filters before `server.AddTool`; `ReadOnlyHint: true` is derived from `!tool.RequiresWrite()`.
- **No model-controlled confirmation.** `internal/safety/adversarial_test.go` walks every registered schema
  and fails on any key or value named `confirm`; `TestAdversarialStaticCatalogMatrix` pins the registered
  counts per mode (63 safe / 71 full / 49 none). Delete tools reject a `confirm` argument
  (`delete_tools_test.go`).
- **Delete responses echo the record** (`internal/tools/delete_common.go`): `deleted_id`, `status`,
  `_meta{operation, resource_type, source_endpoint, confirmation_status:
  "upstream_delete_succeeded_post_delete_unverified", deleted: <pre-delete snapshot>}`; the date-range
  delete has a span cap and uses the athlete's timezone.
- **Discoverability of gated tools**: `icuvisor_list_advanced_capabilities` lists hidden tools with their
  requirement and the env var to enable them, and states that the LLM "cannot bypass" the gates at call time.
- **Toolset tiers** (`internal/safety/toolset.go`): `ICUVISOR_TOOLSET` compact/core/full, unknown -> core, to
  keep the default catalog small for weaker clients.
- Credential store via OS keychain (`internal/credstore/oskeychain.go`) and hosted connector: files/README
  present, not reviewed in detail.

### teoruiz/intervals-mcp (remote access)
- Allowlist that fails closed: `isAllowedGitHubUser` returns `allowlist.size > 0 && allowlist.has(login)`;
  signed OAuth state (HMAC, constant-time verify), `HttpOnly`/`SameSite=Lax` 600 s cookie; inbound
  `authorization`, `cookie`, `x-forwarded-*` headers stripped before proxying; upstream fixed to
  `http://localhost/mcp` with `redirect: "manual"` (`worker/src/security.ts`).

### Proposed mapping for our permission classes
`INTERVALS_MCP_PERMISSION=read|write|destructive|admin` (default `write`): `read` registers only `get_*`/`list_*`;
`write` adds messages, wellness, events, notes, library creates and `update_activity`; `destructive` adds
`delete_event`, `delete_events_by_date_range` (future dates only, athlete timezone, skip envelope) and
`delete_library_workout`; `admin` adds custom item create/update/delete and lifts the date guard. Every tool
carries `title`, `readOnlyHint`, `destructiveHint`; a test pins the registered set per level, and `check_setup`
reports the active level.
