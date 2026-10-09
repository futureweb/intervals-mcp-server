# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [1.0.0b1] - 2026-10-09

First public beta of the Futureweb fork. Based on upstream
[mvilanova/intervals-mcp-server](https://github.com/mvilanova/intervals-mcp-server) at `cb1fbca`.

### Added (phase 6: fatigue, tests, sensors)
- `get_long_ride_fatigue_profile` (new, read-only): fatigue resistance at submaximal power for one ride or
  the long rides of a period (rides reaching the highest threshold). Steady segments of a power band
  (default 75-85 % FTP; 30 s rolling power within the band ±20 %, segment mean inside the band, at least
  `min_segment_secs`, <= 10 % coasting, HR on >= 90 % of the samples, first 10 min and each segment's
  first 60 s of HR left out, segments cut at the thresholds) are split into phases by work (default
  750 / 1,500 kJ, `threshold_unit="kj_per_kg"` scales by body mass from the activity or the profile;
  kJ/kg is always shown) and compared with the phase before the first threshold: HR at matched power,
  W/bpm, cadence, temperature, Garmin stamina / potential stamina. Each threshold crossing, segment and
  climb carries the prior work: kJ, kJ/kg, kJ above FTP (as Intervals.icu `icu_joules_above_ftp`), time
  above FTP and efforts above FTP (30 s power >= FTP for >= 60 s), so easy and hard kJ can be told apart;
  climbs (from `analyze_climbs`' detection) after `climb_after_hours` are marked with stamina at start
  and end. Across rides: median, range and n of the changes per phase, rides split by their share of
  work above FTP (from 4 rides); a row is flagged as a small sample with fewer than 3 rides or when more
  than half of its rides have small phase samples, and cadence changes of more than 15 rpm between
  phases (terrain, gearing) are flagged. `activity_ids` are de-duplicated and cut to `limit` before any
  request; dropped ids are named. Context: the forum threads
  [Fatigue resistance](https://forum.intervals.icu/t/fatigue-resistance/4396),
  [Power curve after kj/kg](https://forum.intervals.icu/t/power-curve-after-kj-kg/93688) and
  [Three ways field data fooled me about durability](https://forum.intervals.icu/t/three-ways-field-data-fooled-me-about-durability-1-350-climbs-33-amateurs/132461)
  (work above FTP is shown as context, not as a predictor).
- `get_submax_test_trends` (new, read-only): the submaximal fatigue tests Intervals.icu detects
  (`submax_fatigue_test` on the activity, sport settings `sft_*`;
  [announcement](https://forum.intervals.icu/t/automatic-submaximal-fatigue-testing/132525)) over a
  period with a validity filter: average within the target tolerance (default the test's own), CV
  within its limit, not ignored and - from the intervals and streams around the test - not part of a
  longer work interval, not continued after the test window and not preceded by hard riding; a
  detection inside a regular workout is listed with its reason and never used as a benchmark. HRRc 0
  without a recovery window counts as not measured; `require_recovery` makes recovery mandatory. Valid
  tests are trended per sport family and test type - power (W, W/bpm) and pace (m/s, m/s per bpm) tests
  are never pooled - (HR at the end, efficiency factor, HRRc, HR rise, HR drop in an easy minute after
  the test) with n, change, slope per week, SD and an ISO week table; differing targets, bikes and
  indoor/outdoor are pointed out.
- `compare_power_streams` several-rides mode: without `activity_id` (date range, default 180 days, or
  `activity_ids`, de-duplicated and cut to `limit` before any request) every ride carrying the second
  power stream is compared on its own and summarised per bike, indoor/outdoor, primary power meter (from
  the file's device data or the bike's PowerMeter gear components) and second power source (its field
  name in the file; the device is not in the activity data, which the output says per ride):
  n, mean, median, between-ride SD and range of the offset overall, per power band and in stable
  windows, the within-ride spread, drift between the first and last quarter, lag counts, outlier share
  and rides far from the group median; power bands and groups with fewer than 3 rides are flagged as
  small samples; rides with fewer than 10 min of usable pairs are excluded with the reason. No correction factor is derived or applied. New optional parameters `activity_ids`,
  `start_date`, `end_date`, `limit`, `detail_level`, `athlete_id`.

### Changed (phase 5: coach test feedback)
- `get_training_summary` / `get_training_load` device loads: a sport without its own field list
  (e.g. GravelRide) follows the field lists of its sport family (Ride); real non-zero values count
  there and are reported as "from sports without field assignment", a stored 0 stays a placeholder.
  Explicit exclusions in a sport's settings stay excluded (running dynamics, a bike "stride" on
  rides), counted separately from zero placeholders.
- `find_similar_intervals` with `reference_activity_id` and no `start_date` keeps the 365 days
  before the reference (window and older matches shown; `start_date` allows any range);
  `compare_workouts` applies the same window to a name search anchored on a reference.
- `get_activity_intervals` with a plan shows the planned step on every interval line (compact,
  standard, JSON) and, for an interval longer than its step, the planned part and the time beyond
  the plan, consistent with `analyze_workout_execution`; unplanned intervals say whether they lie
  before, inside or after the plan.
- `analyze_climbs`: grade confidence high/medium/low per segment with reasons (horizontal distance,
  GPS speed, pauses, implausible grades) in text and JSON, counted in the summary; no raw grade
  below the minimum horizontal distance ("not determinable"); short distances in metres.
- Units and times: foot-sport cadence in steps per minute (2 x the stored per-leg value, labelled
  as stored) everywhere it is printed, bike cadence in rpm, no running dynamics for rides;
  temperatures with °C or n/a (a temperature custom field without units takes the unit of its
  sibling fields); local start times with the timezone name or the UTC offset derived from local
  vs UTC (JSON `utc_offset`).
- `get_load_projection` says "PROJECTION WITHOUT PLANNED TRAINING" (or "WITHOUT PLANNED LOAD") in
  the header at every detail level; `get_coach_context` flags "NO PLANNED WORKOUTS".
- `get_durability`: qualifying share per sport (e.g. 6 of 15), fewer than 8 qualifying sessions
  flagged as a small sample, mixed indoor/outdoor, bikes/shoes or power meters pointed out.
- `get_coach_context`: method line (windows, coupled daily-mean ACWR, monotony, zone basis,
  `threshold_as` - new optional parameter - and the hard-session rule), a caveat with the per-sport
  Z1/Z2/Z3 split when totals mix power and HR zones; the compact intensity distribution carries the
  caveat and the hard-session rule. `get_coach_context` is the recommended first call for weekly
  analyses in `intervals://guide`, `weekly_training_review` and `training_load_review`.
- Also fixes [mvilanova/intervals-mcp-server#134](https://github.com/mvilanova/intervals-mcp-server/issues/134):
  `get_activities` no longer adds activities from the 60 days before `start_date` when the range
  holds fewer named activities than the limit; every result lies in the requested local dates.
- Also fixes [mvilanova/intervals-mcp-server#132](https://github.com/mvilanova/intervals-mcp-server/issues/132):
  a leaf step's text is rendered as the cue at the start of the workout line
  (`- Sprint 40mtr intensity=active Z5 HR`) as in Intervals.icu's builder syntax.

### Added (phase 4: training load and intensity)
- Training load and intensity metrics as separate read-only tools, after the coach report proposed
  by [morritter](https://github.com/morritter) in upstream
  [#150](https://github.com/mvilanova/intervals-mcp-server/pull/150) (metric set, edge cases and
  filters adopted; re-implemented as reusable tools with sample sizes and without verdicts):
  - `get_training_load`: acute and chronic Intervals.icu load (default 7 / 28 days, configurable),
    acute:chronic ratio (coupled daily means), Foster monotony and strain over the last 7 days with
    rest days as 0, the 7-day load as a share of the chronic weekly mean (deload-like at <= 80 %), the
    same per sport family with the primary sport and its own monotony (cross-training floor), ISO
    week table with rest days, weekly monotony/strain and deload-like weeks, CTL/ATL/form/ramp (today
    recomputed without planned but not yet done workouts), device loads summed separately on their
    own scale. With the default end date and no activity today the windows end yesterday.
  - `get_intensity_distribution`: three-zone model from power, HR or pace zones with a documented
    mapping by zone count (power 7: Z1-Z2 | Z3-Z4 | Z5-Z7 with threshold work in the middle zone as in
    Seiler's model, `threshold_as="high"` for the variant of #150; HR/pace 7: Z1-Z2 | Z3-Z4 | Z5-Z7, 5 and 3
    zones), `zone_basis` auto (power for cycling, HR then pace otherwise), polarization index after
    Treff et al. 2019 (Z3 < 1 %, Z1 = 0, Z2 = 0 edge cases), class with zone order, hard sessions
    and days (>= 10 min in Z3 or IF >= 0.85 on >= 20 min), drift between the halves of the period,
    per sport family and ISO week, coverage of sessions and moving time with zones.
  - `get_durability`: aerobic decoupling of steady long sessions with a quality filter (duration,
    pauses, HR, power/variability index, temperature, indoor/outdoor) and excluded sessions per
    reason, median and quartiles per sport, count above 5 %, last 7 days vs window with a +/- 1 pp
    band, efficiency factor trend with a +/- 2 % band and a note when several bikes are involved.
  - `get_load_projection`: CTL/ATL/form projected day by day over the planned WORKOUT loads with
    the 42/7-day model (configurable), planned workouts without load reported, races, Intervals.icu's
    own projection for comparison and a model check against the stored values.
  - `get_coach_context`: compact weekly overview (about 2k characters) of load, fitness, intensity
    (7 and 28 days), recovery markers against 42-day baselines (numbers only), durability, top
    sessions and the plan of the next 7 days; three API calls.
- Prompt `training_load_review`; `weekly_training_review` and the `intervals://guide` resource
  mention the new tools.

### Added (sign-in options)
- OAuth sign-in with the Intervals.icu API key (`OAUTH_LOGIN=apikey`), the default without an
  Intervals.icu app and without a password: OAuth works with zero extra configuration. The entered key is
  compared in constant time with the configured one and never stored or logged.
- Optional TOTP second factor (`OAUTH_TOTP_SECRET`, RFC 6238, single-use codes) for the password and
  API-key sign-ins; `python -m intervals_mcp_server.auth totp-secret` creates the secret and the
  `otpauth://` URI; status shows the second factor.

### Added (client integration)
- MCP tool annotations derived from the permission class (`readOnlyHint`, `destructiveHint`,
  `openWorldHint`), so clients such as ChatGPT run read-only tools without asking and request a
  confirmation for writes, deletions and admin tools.

### Security
- Dependencies updated: MCP SDK 1.30 (minimum 1.28.1), Starlette 1.7, python-multipart 0.0.32,
  cryptography 50, anyio 4.15, PyJWT 2.15 and others (Dependabot advisories).
- `FASTMCP_ALLOWED_HOSTS` / `FASTMCP_ALLOWED_ORIGINS`: the SDK's DNS rebinding protection now
  accepts the public host of `MCP_PUBLIC_URL` and configured hosts behind a reverse proxy.

### Changed (phase 3: analytics quality)
- `analyze_workout_execution` / `get_activity_report`: planned steps are capped at their planned
  (moving) duration; a longer interval is split logically (analysis only) so the planned part is
  evaluated from the samples and the remainder of the last step plus everything after the plan
  is reported as additional training (sample range, kJ share, estimated load, extra efforts);
  riding before the plan and extra time inside the plan are reported separately. Steps can match
  up to three consecutive same-intensity intervals (lap splits); matching never uses names.
  New optional `duration_tolerance_pct`, `start_tolerance_s`, `pause_tolerance_s`, `detail_level`.
  Per-step deviation notes, exact-range hits and mean time in target instead of a single
  "in target" count; the Intervals.icu interval type is kept and shown next to the planned type;
  clock counters, duplicate and other-sport custom streams are left out of step statistics.
- `get_activity_report`: `detail_level` compact (core numbers, up to five key findings,
  sport-assigned custom fields, data quality) / standard / full; the second-power-meter check
  needs a second stream with enough valid samples and reports identical streams; gear streams
  whose values are positions are not presented as tooth counts.
- `compare_workouts`: compares only comparable work intervals (main set by duration and
  intensity, surges under 2 min and warm-ups/recoveries labelled WORK are listed but never
  averaged), time-weighted means, avg/max HR per interval, reference pattern
  (`reference_activity_id` or newest activity), sport-family default (`sport_types="all"` for the
  old behaviour), filters for gear, interval length, intensity, reps and FTP range, separate
  power / HR / cadence / W/bpm / whole-activity RPE trends, gear and power-meter flags.
- `get_training_summary`: custom fields are aggregated by units and meaning (additive values
  summed, device loads summed as a device scale, estimates as latest/change/range, percentages,
  running dynamics, scores and temperatures as mean/median/range, unknown fields not aggregated),
  values from sports without the field are ignored, start/end pairs (stamina) report the typical
  change, `CUSTOM_AGGREGATE_OVERRIDES`, `detail_level`.
- `get_wellness_trends`: requested period, fetched lookback and baseline window are stated
  separately and statistics cover the requested period only; native metrics carry units; a stored
  0 of physiological metrics is missing; small baselines and correlations are flagged.
- `analyze_climbs`: sport profiles (foot vs bike) for smoothing, grade window, minimum horizontal
  distance and plausible grade; flagged segments; real pauses vs very slow movement; VAM on
  moving time; device moving-time counter shown for comparison only; optional raw grade and
  `grade_window_m`, `min_grade_distance_m`, `stationary_speed_m_s`.
- `find_similar_intervals`: optional `reference_activity_id` (window derived from its main set),
  sport-family default (`sport_types="all"` keeps the cross-sport search), comparability score with
  gear / power meter / FTP context, `ftp_range`, `ftp_tolerance_pct`, `sort_by`.
- `get_power_hr_efficiency`: trends need `min_activities_per_group` independent activities per
  group and are compared with the day-to-day variation, time-weighted band means, per-gear trends,
  non-overlapping bands, filters `gear_id`, `environment`, `min_start_minutes`, `max_start_minutes`.
- `get_fatigue_resistance`: reads the sport settings first; without kJ thresholds no fatigued
  values are shown, the configuration is explained and thresholds are suggested (read-only);
  missing and identical fatigued curves are detected.
- `get_best_efforts`: positions are elapsed time (the clock includes recording pauses) and
  windows spanning a pause are flagged; `get_activity_intervals`: optional planned step types;
  `get_training_plan`: unreadable plan reported as unknown; `get_library_workout`: never fails on
  malformed steps.

### Added (phase 2: analytics, hardening)
- `get_activity_report`: one-call compact analysis (overview, plan vs execution, power meter
  check, climbs, data-quality notes).
- `analyze_workout_execution`: `planned_workout_doc` for deleted events, detection of training
  beyond the plan (reported as additional training, not as poor compliance), read-only match
  suggestions for unpaired activities.
- Performance analytics: `get_best_efforts`, `compare_best_efforts`, `find_similar_intervals`,
  `get_activity_histogram`, `compare_workouts`, `get_power_hr_efficiency`, `get_fatigue_resistance`.
- `get_training_plan` (ATP phases, weekly targets, races, fitness-model events),
  `get_library_workout`, `update_sport_settings` (admin), gear maintenance reminders,
  `get_activities(power_meter=...)`.
- `detail_level` (compact / standard / full) for activity details, intervals and the recovery
  snapshot; custom fields are separated into "assigned to this sport" (from the sport settings)
  and others; interval stream statistics limited to the requested streams.
- Remote hardening: `FASTMCP_MESSAGE_PATH`, secret-path deployment guide, built-in OAuth 2.1
  authorization server (`MCP_AUTH=oauth`) for ChatGPT/Claude, `docs/REMOTE_ACCESS.md`:
  "Continue with Intervals.icu" sign-in restricted to allowlisted athletes (password sign-in as
  alternative), consent page with per-connection permission scopes (`intervals:read`, ...)
  enforced on `tools/list` and `tools/call`, Client ID Metadata Documents with `private_key_jwt`,
  RFC 9207 `iss`, audience-bound tokens, redirect host allowlist for dynamic registration, and
  `MCP_TRANSPORT=http+sse` serving `/mcp` and `/sse` from one process.
- Eight coaching prompts and two MCP resources.
- CI: actions pinned to commit SHAs, build and Docker smoke jobs on every PR, PEP 440 pre-release
  detection and explicit GHCR tags in the release workflow.

### Added
- Custom activity fields, custom interval fields and every custom stream (e.g. the metrics the
  Garmin Intervals Bridge restores) with names, codes, values and units in
  `get_activity_details`, `get_activity_intervals` and the new `list_activity_streams`.
- `get_activity_streams`: any stream type or `all`, full sample resolution as CSV or JSON,
  index/time slicing, downsampling and paging.
- Athlete tools: `get_athlete_profile`, `get_sport_settings` (zones with absolute ranges,
  eFTP estimates, default gear), `get_training_zones`; thresholds snapshot per activity.
- Coaching tools: `get_recovery_snapshot`, `get_wellness_trends` (rolling means, baselines,
  outliers, correlations, eFTP per sport), `get_nutrition_summary`, `get_training_summary`
  (week/month/sport/gear with separate load sources and custom field aggregates),
  `analyze_workout_execution` (planned vs executed, per-step metrics), `analyze_climbs`
  (climb/descent/pause segmentation), `compare_power_streams` (dual power meter comparison),
  `validate_workout` / `preview_workout`, `get_gear_details`, `get_server_status`.
- Merged upstream community PRs: weekly summary and plan compliance (#142), HR and pace
  curves (#147), workout library (#145), bulk calendar events (#146), update_activity (#144),
  update_wellness with 1-4 scale labels (#143), indoor flag (#131), FASTMCP host/port
  handling (#140), tags/sub_type (#149).
- Permission classes `read` / `write` / `destructive` / `admin` enforced at tool registration
  (`MCP_PERMISSIONS`, default read-only); `get_server_status` and `--doctor` show the
  configuration; `--version` flag; console script `futureweb-intervals-mcp`.
- MCP prompts `recovery_check`, `workout_analysis`, `weekly_planning`.
- `output_format="json"` on activities, details, intervals, events and all new tools;
  `get_activities` filters (sport, gear), sorting, pagination and compact output.
- Display unit overrides for custom items (`CUSTOM_UNITS_OVERRIDES`).
- Retry with back-off on HTTP 429/5xx; custom item definition cache.
- CI (ruff, mypy, pytest on Python 3.12/3.13), release workflow, Dockerfile with non-root
  user, security policy, issue and PR templates, documentation (`docs/`).

### Fixed
- Security: identifiers from tool arguments can no longer leave their URL path segment (e.g.
  `../athlete/i1` or `1?oldest=...` reached other endpoints, including the raw athlete object);
  secret fields such as `icu_api_key` are removed from every API response.
- The shared HTTP client was closed whenever any MCP session ended (FastMCP runs the lifespan per
  session), breaking requests of other sessions in flight; it now closes after the last session.
- POST requests are no longer retried after 500/502/503/504: Intervals.icu may already have created the
  event, so a retry could duplicate it. POST is retried only on 429; GET/PUT/DELETE as before.
- `add_or_update_event` / `add_or_update_note` updates are partial: only passed fields are sent, so an
  update no longer clears the description, moves the event to today or turns a NOTE into a WORKOUT;
  new optional `category` (e.g. `RACE_A`); a name is only required when creating (reported in
  mvilanova/intervals-mcp-server#150 by morritter).
- Absolute pace targets (`MINS_KM`, `MINS_MILE`, `SECS_100M`, `SECS_100Y`, `SECS_500M`) are written as
  `5:35/km Pace` instead of bare numbers Intervals.icu cannot parse (format from #150 by morritter).
- `get_event_by_id` used the wrong endpoint (`/event/` instead of `/events/`) and returned 404.
- Events reported `Type: Other`; category and sport are now shown, planned time/load and the
  paired activity included, the workout document rendered.
- Readiness and subjective wellness scores were printed with a wrong `/10` scale.
- Activity start times mixed UTC and local; both are shown with the timezone.
- Intervals.icu load and device loads were indistinguishable; labels now say which is which.
- Null, NaN (also the API's `"NaN"` string), zero and absent custom fields are distinguished.
- Package build included only one module (`tool.hatch.build` include list).

### Changed
- Package renamed to `futureweb-intervals-mcp`; project metadata points to the fork.
- Pylint runs as an advisory CI job (only error-class messages fail it); ruff, mypy and pytest are required.
