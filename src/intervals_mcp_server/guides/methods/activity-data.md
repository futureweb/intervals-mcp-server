# Method: activity data

How the activity tools read activities, details, intervals, streams, custom fields and custom
streams, the conventions they share (time, sample indices, cadence, missing values), and what the
one-call activity report and the data audit contain.

## Conventions shared by the activity tools
- **Start times** are given local (time zone name when stored, else the UTC offset) and UTC; JSON
  records carry `start_time_local`, `start_time_utc`, `timezone` and `utc_offset`.
- **Streams are sample-aligned**: every stream of an activity has the same length and index `i`
  of each stream belongs to the same recorded sample; `time[i]` is its offset in seconds from the
  activity start. Recording pauses appear as jumps in `time`, so use the time column, not the
  index, as the timestamp.
- **Sample ranges are end-exclusive**: the `start_index` / `end_index` of an interval (from
  `get_activity_intervals`) cover the samples `start_index .. end_index - 1` and can be passed
  unchanged to `get_activity_streams`, `compare_power_streams` and `get_best_efforts`;
  `start_time` / `end_time` are the matching `time` values. Time bounds select
  `start_time <= time < end_time`.
- **Best-effort windows** (`get_best_efforts`): Intervals.icu returns the best average of a stream
  over each duration (seconds) or distance (metres) with the window's sample indices
  `[start_index, end_index)`, end exclusive, usable as `start_index` / `end_index` in the other
  tools. Positions are shown as elapsed h:mm:ss from the time stream (the clock includes recording
  pauses; sample indices when the time stream is missing). The window ends with its last sample,
  so a pause right after the effort is not counted inside it; a window that spans a recording
  pause (time jump > 5 s) is flagged with the paused seconds, because the duration is elapsed
  time. Durations longer than the activity are reported as not available. Cost: one API call per
  duration or distance plus one for the time stream (and one for the sport's pace units with
  `velocity_smooth`).
- **Cadence follows the sport**: Intervals.icu stores the cadence of foot sports (Run, TrailRun,
  VirtualRun, Walk, Hike, Snowshoe) per leg. The tools show steps per minute (spm = 2 x the stored
  value) and label the stored value, e.g. "147 spm (74 rpm as stored)"; JSON adds
  `average_cadence_spm`. Other sports show rpm. Running dynamics are only shown for foot sports.
- **Missing values**: "no value" / `n/a` / `null` mean null or NaN on Intervals.icu and are never
  counted as 0 (a missing temperature is `n/a`, never 0 °C). Statistics use recorded numeric
  samples only; nothing is interpolated or resampled. A 0 in a custom field filled from the
  device file can be a placeholder (Intervals.icu stores 0 when the file lacks the source field).
- **Custom fields and custom streams** come from the athlete's own custom item definitions
  (`intervals://custom-items`), read dynamically; nothing is hard-coded. The definitions of the
  activity's owner are used (relevant when a coach reads an athlete), falling back to the
  configured athlete; they are cached per process. "Assigned to the sport" means the custom fields
  listed in the sport settings of the activity type. Device metrics written into custom fields
  include, for example, aerobic/anaerobic training effect, training load, EPOC, recovery time,
  VO2max, performance condition, stamina at start/end and sweat loss; custom streams include, for
  example, Stamina, PotentialStamina, performance condition, GarminGASpeed (grade adjusted speed),
  FrontGear / RearGear (gear selection) and battery.
- **Loads**: the Intervals.icu training load is never mixed with device loads from custom fields
  (those keep their own scale).
- **Units** of the standard streams: time s; watts, raw_watts, fixed_watts, secondary_power W;
  heartrate bpm; cadence rpm (as stored); distance, altitude, fixed_altitude m; velocity_smooth
  m/s; grade_smooth %; temp, core_temperature, skin_temperature °C; torque Nm; respiration
  breaths/min; hrv ms; latlng deg; left_right_balance %. Custom streams take their units from the
  athlete's definition.

## get_activities
- Lists the athlete's activities in a date range. Read-only.
- `start_date`: first day YYYY-MM-DD; default 30 days before today (also when only `end_date` is
  given, so pass `start_date` too for an older window). `end_date`: last day, default today. Only
  activities whose local start date lies inside the range are listed (upstream #134); an activity
  without a local start date is kept as returned by the API.
- `limit` (default 10): activities per page. `include_unnamed` (default false): activities without
  a name (or named "Unnamed") are hidden unless true.
- **Classic listing** (no filter, `sort_by=date_desc`, `offset=0`, `detail_level=summary`,
  `output_format=text`): one summary block per activity (name, id, type, sub-type, local and UTC
  start, tags, description, distance, duration and moving time, elevation, power, Intervals.icu
  load, intensity and FTP, heart rate, cadence, feel x/5 and RPE x/10, environment, CTL/ATL and
  Intervals.icu power/HR/pace loads, device and power meter, gear name and id). The API is asked
  for 3 x limit activities when unnamed ones are hidden; when that request came back full but fewer
  than `limit` named activities remain, the whole range is fetched once more. When fewer than
  `limit` are found a note says so (nothing outside the range is added; the number of hidden
  unnamed activities is named).
- **Paged listing** (any of `sport_types`, `gear_id`, `power_meter`, another `sort_by`, an `offset`,
  `detail_level=compact` or `output_format=json`): the whole range is fetched once (compact/json
  with a field selection), filtered, sorted and cut to the page. Header "Activities a-b of total";
  footer "Next page: offset=n" when more follow.
  - `sport_types`: comma-separated activity types, case-insensitive, e.g. "Ride,GravelRide".
  - `gear_id`: exact gear id, e.g. "b12472159" (see `get_gear_list`).
  - `power_meter`: case-insensitive part of the power meter name from the device file, e.g.
    "Rally" or "Shimano"; activities without power meter data are excluded.
  - `sort_by`: `date_desc` (default, newest first) and `date_asc` by local start; `distance`,
    `moving_time` and `load` (Intervals.icu training load) largest first (missing counts as 0).
  - `offset`: matching activities to skip.
  - `detail_level`: `summary` (text block per activity) or `compact` (one line: local start, id,
    type, name, moving time, km, elevation gain, load with the power/HR/pace load split, average
    power and NP, HR, feel/RPE, gear name, power meter, trainer).
  - `output_format=json`: `start_date`, `end_date`, `total`, `offset`, `limit`, `next_offset` and
    `activities` records with explicit units: id, name, type, sub_type, start times,
    moving_time_s, elapsed_time_s, distance_m, elevation_gain_m, training_load_intervals,
    power_load, hr_load, pace_load, intensity_pct, average_power_w, weighted_avg_power_w,
    average_hr_bpm, max_hr_bpm, average_cadence, average_cadence_spm, average_speed_m_s, feel, rpe,
    gear_id, gear_name, trainer, device_name, power_meter, tags, ftp_w, paired_event_id,
    compliance_pct.
- Gear names are resolved from the athlete's gear list (cached per athlete).

## get_activity_details
- One activity in full: one API request plus cached gear, custom item and sport settings lookups.
  Read-only.
- Standard view: the summary (time, distance, elevation, Intervals.icu load, power, HR, cadence,
  feel/RPE, FTP and thresholds used, device and power meter), then fueling (carbs used/ingested per
  hour, sweat loss, fluid), weather (temperature, wind, head/tailwind share, rain) and W′ balance,
  the source with upload/analysis freshness, and a note for Strava stubs (the API returns no data
  for activities imported from Strava).
- `include_custom_fields` (default true): lists every custom activity field the athlete has
  defined that has a value on this activity, with display name, technical code, value and units
  (for select fields also the option label); "no value" means null/NaN on Intervals.icu.
- `include_all_fields` (default false): also lists every other non-empty field of the raw payload
  that is not part of the standard summary, e.g. time in zones, running dynamics, power meter
  details, available stream types. Same as `detail_level=full`.
- `detail_level`: `compact` (about 8 lines: name, time, distance, elevation, gear; Intervals.icu
  load with power/HR/pace load, IF, NP, average power, HR, cadence; feel, RPE, compliance, FTP used,
  eFTP, LTHR, device, power meter and power fields; one context line with fueling rates, weather
  and W′bal; the custom fields assigned to the sport (up to 14); a data line with source, edited
  intervals, sync error, stream count with custom streams and analysis time), `standard`
  (default, the full summary) or `full` (standard plus every other payload field).
- `output_format=json`: the raw activity (without the skyline chart), start times, gear name,
  every custom field with value / units / status, fueling, weather, W′ balance, provenance (source
  and freshness), `detail_level` and a thresholds snapshot (icu_ftp, icu_rolling_ftp, icu_pm_ftp,
  icu_pm_cp, icu_w_prime, icu_pm_w_prime, icu_pm_p_max, lthr, athlete_max_hr, icu_resting_hr,
  threshold_pace, icu_weight, power/HR/pace zones, power_meter, power_meter_serial,
  power_field_names, device_name).

## get_activity_intervals
- The intervals Intervals.icu detected (or the athlete edited) with power, heart rate, cadence,
  speed, intensity and environmental data, plus the interval groups. Read-only.
- API requests: the intervals and the activity itself (its sport decides how cadence is shown,
  its owner whose custom definitions label the fields); one more for the streams with
  `stream_types`; one more for the paired event with `include_planned_types` (none when
  `planned_workout_doc` is given). When the activity cannot be loaded, cadence is shown as stored
  (per leg for runs) and the labels come from the configured athlete; a note says so.
- Without intervals or groups the answer points to `analyze_climbs` (automatic segmentation into
  climbs, descents and pauses) and `get_activity_streams` (raw samples).
- `include_custom_fields` (default true): custom interval fields defined by the athlete, per
  interval with display name, technical code, value and units.
- `stream_types`: comma-separated stream types evaluated per interval between its `start_index`
  and `end_index` (start, end, min, max, mean, delta and the number of non-null samples), e.g.
  "Stamina,PotentialStamina,secondary_power". This yields per-interval values for any stream
  Intervals.icu does not summarise itself: custom streams such as stamina (delta = stamina drop
  during the interval), performance condition or grade adjusted speed, a second power meter
  (secondary_power), gear selection and so on. `custom` = every custom stream of the activity plus
  secondary_power; `all` = every stream (both case-insensitive); other names are passed as given
  (custom codes are case-sensitive). Default: no stream metrics. Use `list_activity_streams` to see
  what exists. Groups are evaluated over all their member intervals. Statistics come from the
  recorded samples only; missing samples are never interpolated.
- `detail_level`: `compact` (one line per interval with the key numbers; custom streams as
  start→end with the minimum), `standard` (default, the full block per interval) or `full`
  (standard plus every custom stream when `stream_types` is not given, i.e. `stream_types="custom"`).
- `output_format=json`: activity type, a cadence note for foot sports, the raw intervals with
  `average_cadence_spm`, custom fields with a value (or 0), `stream_metrics` per stream (name,
  units, samples, non_null, first, last, min, max, mean, delta) and, with planned types,
  `planned_step` and `plan_position`; the groups; a note.
- `include_planned_types` (default false): shows the planned step (type) matched to each interval
  from the paired event or `planned_workout_doc`; the Intervals.icu WORK/RECOVERY type is kept as
  stored and marked when it differs from the plan. An interval longer than its planned step (plus
  the duration tolerance of `analyze_workout_execution`) shows the planned duration and the time
  beyond the plan on its line and in JSON (`planned_step.beyond_plan_s`). `planned_workout_doc` (a
  workout document with `steps`) implies it. The matching method is in
  `intervals://methods/execution`.

## get_activity_streams
- Any stream the activity has, by its technical type: the standard streams (time, watts,
  heartrate, cadence, altitude, distance, velocity_smooth, temp, torque, left_right_balance, hrv,
  respiration, secondary_power, ...) and every custom stream the athlete has defined, addressed by
  its code (e.g. Stamina, PotentialStamina, GarminGASpeed, FrontGear, RearGear). Use
  `list_activity_streams` to discover what an activity offers. Read-only; one request for the
  streams, one more for the activity when custom streams are present (their labels come from the
  owner's definitions).
- `stream_types`: comma-separated types; default
  `time,watts,heartrate,cadence,altitude,distance,velocity_smooth`; `all` fetches every stream of
  the activity (here `custom` does the same: every stream, not only the custom ones).
- `output_format`:
  - `summary` (default): per stream the label and type, value type, number of data points, units
    (with a units note where needed), custom yes/no, description, statistics (start, end, min,
    max, mean, delta, non-null/total samples) and a preview (all values up to 10, else the first
    and last 5). The slicing parameters do not apply.
  - `full`: CSV table with one row per sample (index, time, one column per stream; empty cell = no
    value).
  - `json`: ONE JSON object with the same selection as arrays (null = no value), the activity's
    sample count, selected and returned samples, resolution, `next_start_index`, a note, the
    stream labels, `index_start` / `index_end` and `downsample`.
  - The time stream is always included in full/json output.
- `start_index` (default 0; same indices as `get_activity_intervals`) and `end_index` (stop before;
  default the end; end-exclusive like the intervals' `end_index`) select samples in full/json.
  `start_time` / `end_time` (seconds since start) keep samples with `start_time <= time < end_time`;
  they narrow the index range and are ignored when the time stream is missing or has samples
  without a value.
- `downsample` (default 1 = full sample resolution): keep every n-th recorded sample in full/json;
  samples are skipped, never averaged.
- `max_points` (default 2000, at most 20000; a 24 h ride has about 86,400 samples): maximum
  samples in full/json. Longer selections, and selections too large for one tool result (many
  streams), are cut; the response names the cut and how to continue (`start_index=<next>`, fewer
  stream_types or a larger downsample), never silently.

## list_activity_streams
- Lists the standard and the custom streams present on the activity (custom: defined by the
  athlete, e.g. streams a device records such as stamina, potential stamina, performance
  condition, grade adjusted speed, gear selection, battery) with display name, technical type,
  units and description, plus the power fields of the file. Ends with how to request samples
  (`get_activity_streams(..., output_format='full')`) and per-interval statistics
  (`get_activity_intervals(stream_types=...)`). Read-only.
- `include_stats` (default false): also downloads the streams and reports per stream the sample
  count, non-null count and start/end/min/max/mean (not for latlng). The streams are also
  downloaded when the activity payload lists no stream types.

## get_activity_messages
- The messages (notes, comments) of one activity with author, time (local when the API provides
  it, else UTC), type and content. Read-only.

## add_activity_message
- Write tool (permission class write): posts one message (`content`, plain text, not blank) to the
  activity's thread on Intervals.icu (POST /activity/{id}/messages) and returns its id. Existing
  messages are not changed. Use only when the athlete asks.

## update_activity
- Write tool that replaces values (permission class write, marked as overwriting): PUT
  /activity/{id} with only the fields that are passed; all other activity values stay untouched.
  Use only when the athlete asks.
- `rpe`: rate of perceived exertion, integer 1-10 (1 = very easy, 10 = maximal), sent as `icu_rpe`.
- `feel`: how the athlete felt, integer 1-5 (1 = Strong, 2 = Good, 3 = Normal, 4 = Poor, 5 = Weak).
- `name`: new name; replaces the current one; a blank name is refused.
- `description`: new description; REPLACES the current text (read it first with
  `get_activity_details` to extend it). An empty string is ignored, so a placeholder never wipes
  the text. `clear_description=true` empties it on purpose; passing both a description and
  `clear_description` is refused.
- At least one of rpe, feel, name or description (or clear_description) must be given.
- Activities imported from Strava cannot be updated via the API; the API returns an error.
- The answer lists the changed fields with the values the API returned (the summary formatter
  prefers `perceived_exertion` over `icu_rpe` and could otherwise show a stale RPE), then the
  activity summary.

## get_activity_report
- Complete compact analysis of one activity in a single call. Loads the activity, its intervals,
  one set of streams (time, power, HR, cadence, speed, distance, altitude, second power meter,
  every custom stream, and `w_bal` when the activity has power) and, when paired or provided, the
  planned workout: 3 API requests, 4 with a paired event (`api_calls` in JSON), two more with
  `include_route_history`; custom definitions, sport settings and the gear list are cached
  lookups. For a Strava stub only the note that the API has no data for it is returned. Read-only.
- Sections:
  - **Overview**: key numbers, thresholds, device data and the custom fields assigned to the sport
    (all fields in full), fueling (carbs used/ingested per hour, sweat loss, energy), weather
    (temperature, feels-like, wind, head/tailwind share) and W′ balance (max depletion, time below
    75/50/25 % of W′ from the w_bal stream; W′bal below 0 is flagged as a W′/CP model mismatch),
    as one short context line in compact.
  - **Plan vs execution** when a plan exists (paired event or `planned_workout_doc`): the analysis
    of `analyze_workout_execution` (steps capped at their planned duration, everything after the
    plan reported separately as additional training); see `intervals://methods/execution`.
  - **Intervals** otherwise: one line per interval with the custom streams (standard: at most 30
    lines; full: every line and every stream).
  - **Second power meter check** (watts vs secondary_power, no calibration) only with at least 300
    valid paired samples (both above 0, five minutes at 1 Hz); identical streams (99 % of the
    samples equal: the same sensor recorded twice) are reported as such and never compared.
    Reports valid pairs, mean difference % and ratio, stable 60 s windows and lag; method in
    `intervals://methods/power-meters`.
  - **Climbs**: by default only when the activity has no intervals or more than 500 m of
    elevation gain; `include_climbs` true/false forces or suppresses it. The 6 largest climbs (all
    in full) with the climb/descent/pause summary; method in `intervals://methods/climbs`.
  - **Same route** with `include_route_history`: earlier activities of the same sport family on
    the same Intervals.icu route (up to 15; time, power, W/kg, HR, weather, stamina); activities
    whose distance differs by more than 5 % or elevation gain by more than 10 % are listed but
    left out of the statistics. Routes need GPS.
  - **Data quality**: source, upload/analysis times, recording stops and zero placeholders (full
    audit: `get_activity_data_audit`), unknown device or power meter identity, only one power
    stream, no streams (file not retained?), no intervals, compliance 0 without a paired workout,
    gear streams whose values are gear positions rather than tooth counts, counter and other-sport
    custom streams left out of the statistics. An API error is named as such, never shown as
    missing data.
- `detail_level`: `compact` (core numbers, up to 5 key findings, the custom fields assigned to the
  sport, data-quality flags), `standard` (default, the full analysis without raw stream dumps, at
  most 30 interval lines) or `full` (everything, all custom fields and streams). Key findings: plan
  steps executed with work vs planned targets and time in target (or the main work set without a
  plan), start/end field pairs (e.g. stamina), the Intervals.icu load with device loads on their
  own scale, the second power meter offset, the climbs.
- `duration_tolerance_pct` (default 10), `start_tolerance_s` (default 120), `pause_tolerance_s`
  (default 60): as in `analyze_workout_execution`.
- `output_format=json`: activity, detail_level, plan_source, key_findings, execution (summary,
  extension, pre_plan, hidden streams; the rows except in compact), intervals (without a plan),
  power_check, climbs, fueling, weather, w_prime, provenance, route_history, notes, api_calls.

## get_activity_data_audit
- Provenance and data quality of one activity, or the data coverage of a period. Facts and
  counts, no assessment. Read-only.
- **One activity** (`activity_id`): source (Garmin Connect sync, file upload, Strava stub, other
  syncs), upload and analysis times (freshness; fields without a value whose definition was
  changed or created after the last analysis), filtered duplicates (an activity missing from the
  list of its day while a listed one starts within two minutes), recording stops and time not
  recorded, FIT laps vs Intervals.icu intervals and hand edits, sensors (device, power meter name
  and serial, power fields, second power stream, HR, GPS), streams vs those usually present on
  recent activities of the sport (the 28 days before; the sport family when fewer than 3) with
  sample coverage, custom fields with value / zero placeholder / no value / absent (and those
  usually filled but missing here), device-file fields the Garmin Intervals Bridge could fill,
  context data present (weather, route, carbs, W′ depletion) and analysis issues. API requests:
  the activity with its intervals, the activity list of the 28 days up to its day (field
  selection) and, from `standard` on, its streams; definitions and sport settings are cached.
- **A period** (no `activity_id`): per activity type how many activities have power (meter named,
  second power), HR, GPS, weather, edited intervals, carbs used / ingested, custom streams and each
  expected custom field with value / 0 (real or placeholder) / no value; Strava stubs are counted.
  `start_date` default 27 days before `end_date` (28 days), at most 366 days; `end_date` default
  today; `sport_types` comma-separated filter. One activity list request.
- `detail_level`: `compact` (no stream download, shorter lists; period: only fields with gaps),
  `standard` (default) or `full` (coverage of every stream, each recording gap, every field).
- `athlete_id` selects the athlete of the period; for one activity the activity's owner is used.
