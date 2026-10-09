# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [1.0.0b1] - 2026-10-09

First public beta of the Futureweb fork. Based on upstream
[mvilanova/intervals-mcp-server](https://github.com/mvilanova/intervals-mcp-server) at `cb1fbca`.

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
- Remote hardening: `FASTMCP_MESSAGE_PATH`, secret-path deployment guide, built-in single-user
  OAuth 2.1 authorization server (`MCP_AUTH=oauth`) for ChatGPT/Claude, `docs/REMOTE_ACCESS.md`.
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
- Pylint runs non-blocking in CI; ruff, mypy and pytest are required.
