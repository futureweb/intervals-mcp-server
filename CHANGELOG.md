# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [1.0.0b1] - 2026-10-09

First public beta of the Futureweb fork. Based on upstream
[mvilanova/intervals-mcp-server](https://github.com/mvilanova/intervals-mcp-server) at `cb1fbca`.

### Added (today's wellness completeness)
- `get_wellness_data`, `get_recovery_snapshot` and `get_coach_context` say when today's wellness
  record (athlete's time zone) is still incomplete: one line names the usual fields (a value on
  at least 80 % of the 14 previous days; a stored 0 is a placeholder) that today's record does not
  have yet, by display name, with the record's last update in local time, e.g. "Today 2026-10-10
  is incomplete (last updated 09:46 local): not yet available: sleeping HR, respiration, SpO2, …
  Treat them as missing, not as normal". JSON has `today_completeness` (date, exists,
  updated_local, missing_usual_fields, note). The wellness request of `get_wellness_data` and
  `get_recovery_snapshot` starts 14 days before today instead of making a second request;
  `get_coach_context` makes one extra request for today (custom field names only from cached
  definitions). Today's missing values stay out of baselines, 7-day means and z-scores.
- `get_wellness_data` prints its "Wellness Data:" heading once instead of once more per day.

### Fixed (review findings: writes and API)
- `delete_events_by_date_range` works in two steps: new `categories` (default `WORKOUT`), `dry_run`
  (default `true`: only lists what matches), `confirm_ids` (required with `dry_run=false`: the ids
  from the preview the athlete confirmed) and `include_paired` (default `false`) parameters. Only
  confirmed ids that still match are deleted, one `DELETE` per id (404 = already gone); events added
  after the preview and confirmed ids that no longer match are reported, never deleted. At most 31
  days and 100 ids, `end_date` not before `start_date`, only events that start in the range; plan
  phases and fitness-model events cannot be range-deleted (class stays `destructive`).
- Workouts are validated before they are written (`add_or_update_event`, `add_events_bulk`,
  `create_library_workout`); with errors nothing is sent, warnings are listed in the answer. An
  empty `workout_doc` (`{}`, no steps) is ignored and never wipes a planned workout. Text-only
  workouts (strength, yoga) use the new `description` parameter of `add_or_update_event` or a
  workout_doc with text steps only; on update a `description` (whatever it looks like, e.g. a bullet
  list) or a workout_doc without timed steps replaces a structured (timed) workout only with
  `replace_workout=true`. New errors:
  open-ended (lap-press / free-ride without duration) steps, duration and distance on one step,
  non-whole or non-positive durations, units that do not fit the target kind (HR in `%ftp` ...),
  value plus range, implausible absolute paces (walking and hiking up to 40:00/km), step labels
  with words that Intervals.icu would read as workout syntax (`2m`, `2-3m.`, `85%`, `Z2`, `Z2/Z3`,
  `3x`, `ramp` unless the step is a ramp ...; surrounding punctuation does not hide them; line
  breaks are never rendered), description or comment lines that would
  become steps, repeats or warm-up/cool-down sections; the description is followed by a blank line. Bulk
  entries get the same type checks (no crash on `"70"`, no per-character string steps), at most
  100 entries, blank names and non-string colours are refused, `created_count` counts the events
  the API returned.
- `add_or_update_note` reads the event first and only updates notes (it never turns a workout into
  a note); moving an event keeps its time of day; negative `moving_time`/`distance` and blank names
  or messages are refused; an empty description or comment on update is ignored (new
  `clear_description` for notes and activities, `clear_comments` for wellness empty them on
  purpose; a clear flag together with new text is refused); dates must be `YYYY-MM-DD`; events and notes created without a date use today in the
  athlete's time zone.
- Sport inference matches whole words and no longer defaults to Ride: a workout or race whose name
  does not name exactly one sport needs `workout_type`; notes, sick, holiday and injury days are
  created without a sport; `TARGET` is no longer offered by `add_or_update_event` (it had no target
  fields).
- `update_sport_settings`: `threshold_pace` needs its unit (`"4:30/km"`, `"7:15/mi"`, `"1:45/100m"`,
  `"4.17 m/s"`; a bare number is refused as ambiguous; 0.35-10 m/s), the answer shows pace and m/s, and
  `recalcHrZones=false` is sent explicitly. `update_custom_item` refuses empty updates and merges
  `content` into the current content; `delete_custom_item` reads the item first and names it.
- `add_event_from_library` also copies the planned load (`icu_training_load`, `joules`) and says when
  the library workout's steps are not in its text (file imports).
- Overwriting write tools (`add_or_update_event`, `add_or_update_note`, `update_activity`,
  `update_wellness`) carry `destructiveHint: true`; their class stays `write`.
- Ids are confined to one URL path segment everywhere (`seg()` in every URL, and the tool guard
  refuses `*_id` / `*_ids` arguments such as `a/b` or `i123/intervals`).
- API client: error messages keep the status code and the reason Intervals.icu gives (API key
  redacted), HTML error pages keep their status, empty transport errors name their type; GET is
  retried after timeouts and dropped connections (POST/PUT/DELETE are not), and a write that failed
  in transit says it may still have been applied.
- One tool call makes at most 300 API requests and runs at most 120 s (`MCP_TOOL_MAX_REQUESTS`,
  `MCP_TOOL_TIMEOUT_S`) and says when a limit cut its result; `compare_best_efforts` takes at most
  10 durations.
- Tool results are capped (`MCP_MAX_OUTPUT_CHARS`, default 100000) with a note instead of silent
  truncation (JSON stays valid: the large lists are cut by the same fraction, chronological lists
  keep their newest items, a paged list gets the matching `next_offset`, and `truncated` says what
  was kept); `get_activity_streams` pages by size (default `max_points` 2000, at most 20000) and
  its JSON output is one valid JSON object with `next_start_index`; `get_wellness_data` pages by
  day with the `start_date` to continue.
- "Today" and all default date ranges use the athlete's time zone (profile `timezone`, looked up
  via `/profile` and cached for a day, or `ATHLETE_TIMEZONE`), not the server clock (UTC in Docker);
  `tzdata` is a dependency so time zones also work on Windows.
- Caches (athlete, sport settings, custom items, gear) expire (10 / 30 minutes), are keyed by API
  key, drop after the writes that change them (`update_sport_settings` also refreshes the profile),
  and never store a failed fetch; `get_training_zones` has `refresh`; gear and profile errors are
  shown instead of "no gear" / "no settings".
- API errors are no longer reported as missing data (`get_activity_report`,
  `analyze_workout_execution`, `get_recovery_snapshot`, nutrition trends, `get_activity_intervals`);
  `compare_best_efforts` marks failed cells and incomplete bests; `find_similar_intervals` and
  `compare_workouts` say when the API result was cut at its limit, and a name search with dates
  lists that range instead of the newest matches.
- `get_weekly_summary` works with the athlete alias `0`; interval and stream labels use the custom
  definitions of the activity's owner.

### Fixed (review findings: analytics)
- `analyze_workout_execution` / `get_activity_report` / `get_activity_intervals` (plan vs
  execution): a planned step can span any number of consecutive intervals, so runs with device
  auto-laps (e.g. every 1 km) are no longer pushed to the end of the activity ("before the plan",
  steps "not executed"); a long unmatched block before or after the plan is no longer cheap
  (ANA-1). Device auto-laps are recognised (most of the time in laps of one distance or
  duration): a step boundary inside an auto-lap is placed at the intensity change (least-squares
  change point between the two targets), a step without a lap of its own between two matched steps
  is found at its two intensity changes, an overrun of an auto-lapped step is reported as "longer
  than planned" instead of "before the plan", and the boundary is only moved with that evidence;
  lap presses are never moved, and an auto-lap boundary the samples cannot place is kept with its
  durations not judged (ANA-2, second review R26-1/2/5). With auto-laps the text carries a caveat
  and JSON `alignment_confidence` (high / medium / low) with `alignment_notes` and `auto_laps`
  (R26-4). Laps of the length of a planned step (30/30 s, 3/3 min, 1 km / 1 km, hill repeats)
  are lap presses unless the intensity changes clearly inside several of them (device auto-laps
  of 1 km on 1 km repeats); only device auto-laps are ever merged for the alignment; an internal
  error or an exhausted time budget of the plan comparison falls back to the interval analysis
  with a note (also in the compact report) and a logged warning instead of failing the tool.
  Open-ended targets (top zone, a %/W range with a start only; a start-only zone is that
  zone) are lower bounds in adherence, time in target and the alignment, shown as "352 W or more";
  zone watts are floored like the zone table (ANA-3, R26-11). Distance steps are matched and
  flagged on distance; the plan clock restarts at the actual end of every step without duration,
  and estimated planned totals are labelled (ANA-12, R26-8). Very many laps are merged pairwise for
  the alignment and the analysis runs off the event loop (R26-6). Easy aerobic run steps are work; rest = recovery zone
  (Z1) or a step between two clearly harder steps (ANA-11). A paired event with `"workout_doc":
  null` (race, note) no longer crashes the analysis (API-6).
- Pw:HR drift has the Intervals.icu decoupling sign (positive = HR rose relative to power; exact
  intervals keep Intervals' own value) and the convention is stated (ANA-4).
- NP of split or merged steps uses the 30 s rolling mean over the whole activity (on a 1 s grid:
  recording pauses as 0 W, sparser sampling held) and stream speeds are distance / moving time, as Intervals.icu computes
  interval values (ANA-10, R26-7).
- `get_activity_report` key finding compares work steps in the unit of their targets (pace, HR or
  W) instead of labelling pace/HR targets as watts, clearly different targets listed apart (ANA-7,
  R26-10).
- `get_best_efforts`: the end index is exclusive; a recording pause right after the window is no
  longer counted inside it, and the window ends with its last sample (ANA-5).
- Correlations, the weight trend and the nutrition weight change treat a stored 0 of a
  physiological metric as no value, like the trend statistics (ANA-6).
- Power zone watt bounds are floored like Intervals.icu (FTP 234: Z1 <= 128 W, Z2 129-175 W)
  (ANA-8). `icu_intensity` is always read as percent, so a tiny value is no IF > 1 (ANA-9).
- `get_coach_context` recovery markers compare the 7-day means with the 42 days before them; z
  divides by the SD of the 7-day means over the 90 days before (times sqrt(1 + 7/42) for the
  baseline mean) instead of the SD of single days; |z| up to about 2 is stated as normal variation
  (ANA-14, R26-3).
- `get_training_summary`: HR time in zones is reported (plain-list zone times were dropped), the
  sweet-spot bucket is listed apart from Z1-Z7 (API-5); the load line says that the total takes
  power, else HR, else pace per activity and that the per-method sums overlap, and CTL/ATL are read
  at the end of each week or month (ANA-15, R26-9); a missing ATL no longer breaks the text (ANA-16).
- `get_intensity_distribution` / `get_training_load` / `get_coach_context`: the pace basis uses GAP
  zone times where Intervals.icu does (`use_gap_zone_times`) (API-18).
- Robustness and units (ANA-16): `get_recovery_snapshot` with a baseline mean of 0, NaN values in
  the performance tools, pace of swims per 100 m (execution analysis and best efforts).

### Added (phase 6: data quality, fueling, context)
- `get_activity_data_audit` (new, read-only): provenance and data quality of one activity - source and
  file (Garmin Connect sync with the Garmin activity id, upload of a Garmin export, the Garmin
  Intervals Bridge's upload mode, Strava stubs that the API returns empty), upload delay after the end
  and analysis time (re-analysed later), custom fields without a value whose definition changed after
  the last analysis (the API only gives the last change time), filtered
  duplicates (not in the activity list while a listed activity starts within 2 min, same Garmin
  activity named), recording stops and gaps, FIT laps vs Intervals.icu intervals and what manual
  interval edits mean, device and sensor identity (power meter name/serial, battery, estimated power,
  HR sensor not exposed), streams usual on recent activities of the sport but missing, per-stream
  coverage (dropouts, zeros, empty streams, streams not returned), the sport's custom fields with
  value / zero placeholder / no value / absent and the device-file fields the bridge could fill.
  Without `activity_id`: per-sport coverage of a period (sources, power, HR, GPS, weather, custom
  streams, expected fields). Three requests for one activity, one for a period.
- `get_fueling_analysis` (new, read-only): carbs used (Intervals.icu estimate) and ingested in g and
  g/h, ingested share of used, kcal and kJ, fluid intake, sodium and sweat loss from custom fields
  found by units and name (no vendor list); a 0 in a device-file field (e.g. a Garmin sweat loss) is a
  zero placeholder, shown as "0 stored" and left out of totals, differences and statistics; rates per
  moving hour; intake per hour only when a custom stream carries it. Period mode for sessions of at
  least `min_minutes`: per sport family, and within each family by duration and intensity bucket,
  with sample sizes, logging coverage (logged, stored 0, not logged) and Spearman correlations per
  family with n (computed from 8 sessions with intake logged, never pooled across sports); notes that
  used vs ingested is no 1:1 energy deficit; no targets.
- `get_activity_report` and `get_activity_details`: fueling line, weather line (temperature range,
  feels-like, wind in km/h from the m/s Intervals.icu stores, compass direction, head/tailwind share,
  clouds, rain in mm/h, device sensor next to the weather) and W′ balance (W′ and power-model W′, max
  depletion, lowest W′bal; in the report from the `w_bal` stream, requested with the other streams at
  no extra cost: time below 75/50/25 % of W′, dips below 50 %, the interval that ended lowest). When
  W′bal falls below 0 (depletion above W′) the ride exceeded the W′/CP model with the FTP and W′ set
  for it: this is flagged as a model mismatch (`model_mismatch`) and no depletion percentages or
  threshold times are presented as physiology. Compact views carry one short "Context:" line
  (carb rates, sweat, temperature, wind, W′bal minimum); standard and full the full lines. JSON
  sections `fueling`, `weather`, `w_prime`, `provenance`. The report's data-quality notes name the
  source and freshness, recording stops, manual interval edits, zero placeholders and fields changed
  after the analysis (compact: short forms, source and edits only in the "Data:" line); Strava stubs
  get a clear message instead of an empty analysis.
- `get_activity_report(include_route_history=True)`: earlier activities on the same Intervals.icu
  route (activity list filtered by `route_id`, the latest 16 activities on the route, plus the route
  name; two requests; the text says when older activities were not loaded) with
  time, power, W/kg, HR, weather and start/end pairs such as stamina; rank by moving time and
  differences to the median of comparable activities (same sport family, distance within 5 %,
  elevation gain within 10 %).
- `get_durability(temperature_source=...)`: the heat filter can use the activity's weather or
  feels-like temperature instead of the device sensor; sessions list device and weather temperature
  (and feels-like when it drives the filter).
- Fixed: the activity summary printed the wind speed (stored in m/s) as km/h.

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

### Added (phase 6: plan simulation)
- `get_load_projection` simulates what-if plans without writing anything (`scenario`): single
  sessions with a load, or with `duration_min` and `intensity_factor` (load estimated as
  hours x IF² x 100 and flagged as an estimate), and weekly templates (`weekly`: start, weeks,
  weekly load or a list per week, or hours with an IF; sessions or weekdays, long day and its
  share, sport). `calendar` adds them to the planned workouts, replaces the planned workouts
  inside the scenario's span or ignores the calendar. Scenario and calendar plan are compared
  day by day (full), per ISO week (load per sport, CTL, ATL, form, ramp) and at the end; the four
  completed weeks before are summarised for comparison.
- Target day (`target_date`, default the next RACE_A within 180 days): CTL, ATL and form at the
  start of the day for the calendar plan and the scenario; with `target_form` (points or percent
  of CTL) a grid search over the load of the last `taper_days` days (percent of the planned load,
  or a constant weekly load) that puts the form into the range, with the CTL that goes with it.
  Assumptions are listed (time constants 42/7 d, sessions done as listed, no illness).
- Plan statistics per ISO week for the calendar plan and the scenario: sessions, hours, longest
  session (and its share of the race's planned duration), rest days and monotony; weeks with a
  CTL ramp above 5-8 per week (Friel 2015), monotony above 2.0 (Foster 1998) or no rest day
  (Meeusen et al. 2013) are listed as outside the commonly cited range, nothing more. Identical
  daily loads (monotony undefined, maximal) are flagged too; weeks without durations say "hours n/a".
- `get_load_projection` reports race days at the start of the day (before the race's own load),
  like the target day; days, weeks, the end and the lowest form are labelled as end-of-day values
  (JSON `value_basis`, races `basis`).

### Security (review findings)
- OAuth: refreshing a token with a narrower scope (for example only `mcp`) keeps the grant's
  permission scopes; a token without any `intervals:*` scope is read-only and never falls back
  to the server-wide `MCP_PERMISSIONS`.
- `/register` (dynamic client registration, reachable without credentials) limits `client_name`
  to 100 printable characters, `redirect_uris` to 10 and the whole metadata to 8 KB; client-supplied
  values are escaped and clipped in log lines, and the server logs through a plain stream handler
  instead of the SDK's rich handler (whose rendering time grows quadratically with long tokens).
- OAuth consent page: the form only accepts a submission from the browser that opened it. The page
  sets an HttpOnly consent cookie (`__Host-` prefixed on https) and the form carries an HMAC bound
  to it and to the sign-in request; a POST whose `Origin` or `Sec-Fetch-Site` names another site is
  refused. Previously another web page could submit the consent (and pick all permissions) in the
  athlete's browser when the Intervals.icu sign-in was enabled. The pages now send
  `Referrer-Policy: same-origin` (no form-action CSP, which would block the redirects).
- The consent form body is limited to 16 KB / 50 fields; `/register` accepts at most 10
  registrations per client address and hour, and a client in the middle of its consent is no
  longer evicted from the 50-client table.
- Pending sign-ins and Intervals.icu sign-ins in progress are capped per client address (IPv4
  address or IPv6 /64, 20 each), and a full table drops the oldest entry of the busiest address
  inside the busiest network (IPv6 per /48); a flood of `/authorize` requests from one address or
  network (also one sharing the athlete's /48) can no longer push out the athlete's own pending
  sign-in.
- Client metadata documents: bounded cache (256 documents, rejected ones evicted first), one
  shared fetch per document, at most 10 fetches per minute for unknown client ids (pinned ids,
  ids accepted before and ids holding a refresh token - loaded from the state file at startup -
  are exempt, so random client ids cannot keep ChatGPT's document from being fetched), a known
  document is kept for up to a day while its host is unreachable, and client ids with a query
  string, percent-encoding, dot or empty segments, control characters or more than 512
  characters are refused (an invalid URL gave HTTP 500). A document's redirect URIs must stay on
  its own host, another allowlisted host or loopback.
- `OAUTH_CLIENT_HOSTS` and `OAUTH_REDIRECT_HOSTS` accept `host/path` entries (exactly that path)
  and `host/path/` entries (every path below it) in addition to hosts; redirect URIs of
  dynamically registered clients may not contain a query string.
- `/token` and `/revoke` accept only `application/x-www-form-urlencoded` bodies with each
  parameter once (RFC 6749); a `multipart/form-data` body, which the SDK would have parsed,
  could otherwise skip the client assertion check.
- Refresh tokens: a rotated refresh token presented again within `OAUTH_REFRESH_REUSE_GRACE`
  seconds (default 120) gets the same answer again once that answer is stored (retry after a
  lost response, concurrent refreshes), so a grant never forks into parallel chains; presented later it revokes the whole
  grant (RFC 9700 reuse detection; `OAUTH_REFRESH_REUSE_REVOKE=false` only refuses the request).
  A client whose metadata document declares `private_key_jwt` (ChatGPT) must send its client
  assertion with every token request (`OAUTH_REQUIRE_PRIVATE_KEY_JWT`, default `true`; verified
  from the production journal that ChatGPT signs its code and refresh requests). Verified
  assertions are logged at INFO. A refresh narrowed to permission scopes keeps `mcp`.
- Sign-in: the PBKDF2 password check runs in a worker thread (its own pool of 4) instead of
  blocking the event loop for 0.3 s per attempt; an attempt is counted before the check, so a
  concurrent burst from one address gets no more checks than the limit; failed password /
  API-key sign-ins also count against a global budget (`OAUTH_LOGIN_GLOBAL_RATE_LIMIT`, default
  500 per 15 minutes; with TOTP it never pauses a sign-in, so others cannot lock the athlete
  out); the per-address limit groups IPv6 addresses by /64 and its table is bounded; an
  authenticator code is only used up when the password or API key was right.
- `get_server_status` no longer tells connected clients the OAuth user name, password source,
  allowed athletes, state file path, bind address, port or SSE path (a secret path is a
  credential); `--doctor` on the server still shows them.
- Logging: uvicorn's access log keeps query parameter names but drops their values (the
  Intervals.icu callback code, the sign-in request id, SSE session ids); request bodies are no
  longer logged at DEBUG and Intervals.icu error bodies are shortened; httpx's per-request INFO
  lines are off unless `FASTMCP_LOG_LEVEL=DEBUG`. The documentation no longer claims that no
  log contains codes (the reverse proxy's does unless configured, see `docs/REMOTE_ACCESS.md`).
- Docker base images are pinned by digest (Dependabot updates them).

### Fixed (review findings: operations)
- OAuth state file: written in a worker thread and only committed to memory once the write
  succeeded (a full disk no longer loses the refresh token or authorization code of the request,
  the client can retry); the directory is fsynced after the rename; the server checks at startup
  that the directory is writable.
- A state file that cannot be used (not JSON, wrong structure, written by a newer version) stops
  the server with a one-line error and is never overwritten or moved; entries that the current
  SDK cannot read are kept in the file unchanged instead of crashing the server. The format stays
  version 1; existing files load unchanged.
- New command line front end (`futureweb-intervals-mcp`, also used by
  `python src/intervals_mcp_server/server.py`): `--version` and `--help` work with a broken
  configuration, unknown flags are refused, `--doctor` lists every configuration problem
  (permissions, transport, port range, log level, path settings, OAuth settings, state file)
  without starting anything, and a configuration error at startup is reported in one line
  (exit code 2) instead of a traceback. A network transport without `API_KEY` / `ATHLETE_ID`
  logs a warning.
- `FASTMCP_PORT` must be 1-65535, `FASTMCP_LOG_LEVEL` a known level and the path settings must
  start with `/`.
- `MCP_PUBLIC_URL` with a path: the protected resource metadata is served once, at the path the
  SDK advertises, with the permission scopes (previously a second document without them was
  added at the root), and the consent form posts to the prefixed path.
- An Intervals.icu sign-in whose request was denied or expired while Intervals.icu answered
  shows the "expired" page instead of HTTP 500.
- Docker image: runs the `futureweb-intervals-mcp` console script from the installed package
  (no second copy of the sources), keeps the OAuth state in `/data` (mount a volume), and has a
  health check for the network transports.
- Release workflow: the GitHub release is created only after the image was pushed, one run per
  tag at a time, and the tag must also match `__version__` (a test checks it against
  `pyproject.toml`).
- Dependencies: floors raised to the security-updated versions (`mcp>=1.30`, `httpx>=0.28.1`,
  `starlette>=1.7`, `python-multipart>=0.0.32`), direct imports (`starlette`, `uvicorn`, `anyio`)
  declared, and the unused `mcp[cli]` extra (typer) dropped.

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
