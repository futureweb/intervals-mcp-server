# Method: comparisons

How the performance tools find and compare peak efforts, similar intervals, repeated workouts,
power:HR efficiency, fatigue resistance, histograms and the athlete power, HR and pace curves:
parameters, defaults, windows, rules, caveats and API cost.

## Conventions shared by these tools
- All tools are read-only. They report values computed by Intervals.icu or simple statistics over
  them; nothing is calibrated, interpolated or judged. A missing value is shown as `n/a` (JSON
  `null`), never as 0.
- **Power meters**: different bikes (gear) may carry different power meters that are not
  calibrated against each other, and trainer power can differ from the outdoor meter. Absolute
  watts are only comparable within one gear / power meter; across gear compare % of FTP, HR or RPE.
  The tools show gear and power meter per activity and flag mixed sensors.
- **FTP at the time**: every activity carries the FTP Intervals.icu used for it (`icu_ftp`); % of
  FTP values always use that FTP, not today's.
- **Lists and formats**: comma-separated text parameters (`activity_ids`, `durations`,
  `distances`, `sport_types`, `power_bands`, `curves` of `get_fatigue_resistance`) are split at
  commas; blanks around items are ignored. Activity types are compared case-insensitively. Ranges
  such as `ftp_range` are written `"low-high"` (e.g. `"225-240"`, both ends inclusive). The curve
  tools `get_athlete_power_curves`, `get_hr_curves` and `get_pace_curves` take JSON arrays instead
  (`durations: [5, 60, 300]`, `distances: [1000, 5000]`, `curves: ["90d", "s0"]`).
- **Limits are capped, not rejected** where a parameter says "capped to": a smaller value becomes
  the minimum, a larger value the maximum, and the text output says "capped from ...".
- **Output**: `output_format="text"` is a readable summary; `"json"` returns every field. Text
  outputs end with the number of API calls made (the gear catalog may add one call when it is not
  cached).
- **Sport families** used for defaults: cycling (Ride, VirtualRide, GravelRide, MountainBikeRide,
  EBikeRide, EMountainBikeRide, TrackRide, Velomobile, Handcycle), running (Run, TrailRun,
  VirtualRun), walking (Walk, Hike, Snowshoe), swimming (Swim, OpenWaterSwim), skiing (NordicSki,
  BackcountrySki, VirtualSki, RollerSki, AlpineSki), rowing (Rowing, VirtualRow, Canoeing,
  Kayaking); any other type is its own family.
- **Indoor** means the activity has the trainer flag or its type starts with "Virtual".

## get_best_efforts
Peak efforts of one activity for given durations or distances (peak power, HR, pace).
- **Method**: for every requested duration (seconds) or distance (metres) the Intervals.icu
  best-efforts endpoint returns the best average of the stream over that window and the window's
  sample indices `[start_index, end_index)` (end exclusive). Use it for the peak 5 s / 1 min /
  5 min / 20 min power of a ride, the fastest kilometre of a run (`stream="velocity_smooth"` with
  `distances`) or the highest sustained heart rate.
- **Positions and recording pauses**: positions are elapsed h:mm:ss from the activity's time
  stream; this clock includes recording pauses. Because a duration is elapsed time, an effort
  window can span a recording pause (a time jump > 5 s); such a window is flagged with the paused
  time ("window spans ... of recording pause", JSON `paused_s_in_window`), since the paused seconds
  lie inside it. The window ends with its last sample: its end time is the time of the first sample
  after the window when that sample follows without a pause, else one second after the window's
  last sample, so a pause right after the effort is not counted inside it. When the time stream is
  missing, positions are sample indices. The sample indices can be passed unchanged as
  `start_index` / `end_index` to `get_activity_streams`, `compare_power_streams` or another
  `get_best_efforts` call.
- **Parameters**:
  - `activity_id`: the activity.
  - `stream` (default `watts`): any stream type of the activity, e.g. `watts`, `heartrate`,
    `velocity_smooth` (speed in m/s, shown with the pace in the sport's units: per 100 m for swims,
    per 500 m for rowing, otherwise per km), `cadence`. Units shown: W, bpm, rpm, m/s, m, Nm.
  - `durations` (default `"5,30,60,300,1200,3600"`): comma-separated seconds, positive, duplicates
    ignored. The default also applies when `distances` is given: pass `""` (or null) to evaluate
    distances only. At least one duration or distance is required.
  - `distances`: comma-separated metres, e.g. `"1000,5000"`, evaluated in addition to the durations
    (the time each distance took is shown).
  - `count` (1-20, default 1): number of best non-overlapping efforts per duration or distance,
    ranked #1, #2 ...
  - `exclude_intervals`: ignore samples inside the activity's detected intervals (e.g. the best
    effort outside the structured work).
  - `start_index` / `end_index`: search only from this sample index and stop before that index.
  - `output_format`: text or json.
- **Not available**: a duration longer than the activity (or a stream without data) is reported as
  not available. When every request fails the first error is returned; otherwise a failed request
  appears in its row.
- **API calls**: one per duration or distance, plus one for the time stream, plus one for the
  activity (the sport's pace units) with `velocity_smooth`.

## compare_best_efforts
Best efforts (e.g. 1 / 5 / 20 min power) of several activities side by side.
- **Use**: how peak power developed over a block, comparing races, or checking whether a new bike
  or power meter reads differently.
- **Activities**: from `activity_ids` (one request per id; all ids are fetched, then the filters
  and the limit apply) or from the activities of a date range (`start_date` default 90 days before
  `end_date`, `end_date` default today). Both are narrowed by `sport_types` (case-insensitive),
  `gear_id` and, when given, the dates, then sorted newest first and cut to `limit` (default 10,
  capped to 1-25).
- **Durations**: comma-separated seconds (default `"60,300,1200"`), positive, at most 10 per call
  (each activity needs one request per duration; 25 x 10 = 250 requests at most).
- **Stream**: as in `get_best_efforts` (default `watts`); one best effort (count 1) per activity and
  duration.
- **Output**: a matrix with one row per activity (date, sport, gear name and id, FTP at the time,
  name, id) and one column per duration (best average of the stream), then the best activity per
  duration. A cell `error` means the request failed (not a missing effort); when a request of a
  duration failed, that duration's best is marked INCOMPLETE because the real best may be among the
  failed ones. `n/a` means the activity is shorter than the duration or has no such stream. A note
  reminds that different bikes may carry power meters that are not calibrated against each other.
- **API calls**: one per activity and duration, plus one to list the activities (or one per id),
  plus one for the gear catalog unless cached.

## find_similar_intervals
Activities containing intervals of a given length and intensity (% of FTP), ranked by
comparability.
- **Search**: wraps the Intervals.icu interval search (`activities/interval-search`): activities
  with WORK intervals between `min_secs` and `max_secs` long at `min_intensity`-`max_intensity`
  % of FTP, optionally restricted to a workout `target` (POWER, HR or PACE: the target type of the
  workout, not the sport; case-insensitive) and a repeat count (`min_reps` / `max_reps`, each
  >= 1, min <= max).
- **Bounds**: `min_secs` > 0 and <= `max_secs`; 0 <= `min_intensity` <= `max_intensity` <= 300.
  The endpoint accepts whole percent only, so a fractional lower bound is rounded down and a
  fractional upper bound rounded up (90-105.4 becomes 90-106).
- **Reference activity** (`reference_activity_id`): the window is derived from that activity's
  main work set (main set as in `compare_workouts`, e.g. 3 x 10 min threshold): interval length
  75-125 % of the main set's median interval (at least 1 s) and intensity its % of FTP ± 8
  percentage points (not below 0). Explicitly passed bounds win, each on its own. When the
  reference has no power-based intensity, `min_intensity` and `max_intensity` must be passed.
  Without a reference all four bounds are required. Results are ranked by comparability with the
  reference.
- **Date window**: the API has no date, sport or gear filter, so these are applied here on the
  returned list. With a reference activity and no `start_date` only the 365 days before the
  reference count (default window, shown in the output with the number of older matches outside
  it); an explicit `start_date` allows any range. Without a reference there is no default date
  limit. `end_date` keeps activities up to that day.
- **Sport default**: the sport family of the reference activity, otherwise the family with the most
  results (cycling, running ...). Results of other families are counted in the output ("Also found
  in other sports"), not silently dropped, because % of FTP is sport-specific. `sport_types="all"`
  keeps every sport (the previous cross-sport behaviour); an explicit list keeps exactly those
  activity types.
- **Further filters**: `gear_id`; `ftp_range` (`"low-high"` W, inclusive) keeps activities whose
  FTP at the time lies in the range.
- **Result size**: the API is asked for `limit` x 5 results (at most 100, at least `limit`) so that
  enough remain after the local filters; the output keeps `limit` (default 20, >= 1). When the API
  returned its maximum and the oldest result is newer than the window start, a note says older
  matches are missing and how to narrow the search.
- **Comparability score 0-100**, a weighted mean of:
  - the best matching group of the activity's interval summary (e.g. "3x 10m 250w"; groups between
    0.9 x `min_secs` and 1.1 x `max_secs`): closeness of its interval length to the reference (or
    to the middle of the searched window), weight 2; with a reference also the repetition count
    (weight 1) and the % of FTP on a 15-point scale (weight 2). Without a matching group: 0.3,
    weight 2 ("no interval group of the searched length in the summary");
  - same sport type as the anchor: 1.0, else 0.8 (weight 1);
  - same gear as the anchor (when both have gear): 1.0, else 0.6 with the note "other gear: watts
    from another power meter, compare % FTP only" (weight 1);
  - FTP context: FTP within `ftp_tolerance_pct` (default 5 %) of the anchor's FTP: 1.0, else 0.6
    with "other FTP context" (weight 1).
  The anchor is the reference activity, otherwise the newest result ("newest result (context)").
- **Sorting** (`sort_by`): `comparability` (score, then newest; default with a reference) or
  `date` (newest first; default otherwise).
- **Per activity**: date, sport, name, id, the interval summary, moving time, load, intensity, FTP
  at the time, gear, power meter and compliance, plus the comparability score with its reasons
  (JSON also RPE and feel). Absolute watts of different bikes / power meters are not comparable;
  compare % of FTP across gear.
- **Next steps**: `get_activity_intervals(activity_id)` for the per-interval details (power, HR,
  cadence, timing); `compare_workouts` to compare the same workout over time.
- **API calls**: one (the interval search), plus two for a reference activity (activity and its
  intervals) and one for the gear catalog unless cached.

## get_activity_histogram
Time-in-bucket histogram of one activity.
- **Metrics** (`metric`, case-insensitive): `power` (W, default bucket 25 W), `hr` (bpm, default
  bucket 5 bpm), `pace` and `gap` (grade-adjusted pace). Pace and GAP buckets are speeds in m/s
  set by Intervals.icu and are shown with the matching pace range (min/km), slowest to fastest.
- **Bucket size** (`bucket_size`): positive width in W or bpm, for power and hr only; passing it for
  pace or gap is an error.
- **Output**: seconds and % of the recorded time per bucket, as computed by Intervals.icu; empty
  buckets are left out of the text. Use it to see how polarised a ride was, how much time was spent
  above FTP, or the pace distribution of a run independent of the zone definitions.
- **API calls**: one.

## compare_workouts
Repeated executions of the same workout over time, compared on truly comparable work intervals.
- **Collecting activities** (newest first, `limit` default 8, capped to 1-12):
  - `query` without dates: Intervals.icu name search (case-insensitive text in the name; `"#tag"`
    for an exact tag), newest first, asking for 4 x the limit results (at most 100). When the
    search returns its maximum, a note says older matches may be missing; a name search with
    `start_date` / `end_date` lists that range instead and matches the names locally (same rule).
  - `activity_ids`: one request per id; `query` is then ignored.
  - otherwise the activities of the date range (`start_date` default 90 days before `end_date`,
    `end_date` default today).
  - A name search with a reference activity and no `start_date` keeps the 365 days before the
    reference (shown in the filters; an explicit `start_date` allows any range).
- **Filters**: `sport_types` (comma-separated, case-insensitive; `"all"` keeps every sport; default
  the sport family of the reference activity, otherwise of the newest activity found), `gear_id`,
  the dates, `ftp_range` (`"low-high"` W, FTP at the time, inclusive). A reference activity outside
  the collected list is fetched and added to the selection before the limit is applied.
- **Main set per activity**: only intervals labelled WORK with a duration count. They are split
  into
  - surges / sprints: shorter than 2 min (and shorter than `min_interval_secs` when given): listed,
    never averaged in;
  - excluded, with the reason: outside `min_interval_secs` / `max_interval_secs`, outside
    `min_intensity` / `max_intensity` (% of FTP), or below 70 % of FTP (warm-ups / recoveries
    labelled WORK; this floor is replaced by `min_intensity` when given);
  - the main set: the remaining intervals are grouped greedily by similar length (within 25 % of
    the group median) and intensity (within 8 percentage points of FTP of the group median); the
    group with the largest total time (ties: higher intensity) is the main set;
  - other WORK intervals: the remaining groups (listed, not averaged in).
- **Main-set summary**: count, median interval length, time-weighted means (weights = moving time
  of each interval) of power, NP, HR and cadence, the maximum HR of the intervals, % of FTP at the
  time, W/bpm (time-weighted power / time-weighted HR) and each interval's values. Intervals without
  power are kept but contribute no power.
- **Reference pattern**: count, median length and % of FTP of the main set of
  `reference_activity_id`, otherwise of the newest activity with a main set. An activity is
  comparable when its main set's median length is within 25 % and its intensity within 8
  percentage points of FTP of the pattern, and (with `min_reps` / `max_reps`) its main set has that
  many intervals. Non-comparable activities are listed with the reason; with
  `comparable_only=false` they also appear in the table, marked NOT comparable. Trends always use
  the comparable activities only.
- **Trends** (first -> last over the comparable activities, oldest first): time-weighted power, HR,
  max HR, cadence, W/bpm and, with `include_rpe`, RPE; each with change, % change, range and n.
  Fewer than 3 activities are marked as not reliable, fewer than 2 give no trend. When the
  comparable activities use several gear ids, power and W/bpm are also trended on the reference
  gear only. RPE is always the whole-activity RPE, never per interval. For foot sports cadence is
  shown in steps per minute (2 x the stored per-leg value).
- **Notes**: different gear / power meters (watts not calibrated against each other: compare power
  within one gear, use HR and RPE across gear), mixed indoor and outdoor sessions, unknown power
  meter identity, FTP changes over the period.
- **Output order**: activities oldest first, each with set, power (NP), HR avg/max, cadence, W/bpm,
  FTP, power source (power meter, gear, indoor) and optionally RPE, then the main-set intervals,
  surges, other and excluded intervals.
- **API calls**: one to collect the activities (or one per id), one for a reference outside the
  list, one for the gear catalog unless cached, plus one per activity for its intervals (at most
  12).

## get_power_hr_efficiency
Power-to-heart-rate ratio (W per bpm) per power band across steady intervals over time.
- **Activities**: the date range (`start_date` default 90 days before `end_date`, `end_date`
  default today), filtered by `sport_types` (default `"Ride,GravelRide,VirtualRide"`), `gear_id` and
  `environment` (indoor = trainer / virtual, outdoor = the rest); the newest `limit` (default 30,
  capped to 1-60) are analysed and shown oldest first.
- **Intervals**: every WORK interval of at least `min_interval_secs` (default 300 s; moving time,
  else elapsed time) with both average power and average HR (> 0) goes into the power band of its
  average power. `min_start_minutes` / `max_start_minutes` keep only intervals starting within that
  many minutes from the activity start (e.g. to skip warm-ups or fatigued late intervals).
- **Bands** (`power_bands`, default `"150-200,200-250,250-300"`): comma-separated `"low-high"` in
  W, half-open (low <= W < high), 0 <= low < high, must not overlap.
- **Per activity and band**: number of intervals, time-weighted mean watts and mean HR, and W/bpm
  (mean watts / mean HR).
- **Trend per band**: each activity counts once with its band W/bpm. A trend needs at least
  2 x `min_activities_per_group` (default 3, >= 1) activities with data in the band; the group
  size is the larger of `min_activities_per_group` and a third of those activities. The mean W/bpm
  of the oldest group is compared with the newest group (change in %), and the difference with the
  standard deviation of the per-activity values (day-to-day variation): "larger than the
  day-to-day variation" or "within the day-to-day variation, no meaningful change". Bands without
  enough activities are reported as not reliable.
- **Several bikes / power meters**: trends are computed per gear (activities without gear form
  their own group), so watts of different power meters are never mixed.
- **Caveat**: a higher W/bpm at the same power usually means a lower HR for the same output, but
  heat, fatigue, hydration, cadence, indoor vs outdoor, the interval's position in the ride and
  power meter differences between bikes all move the ratio. This is a statistical comparison of
  W/bpm in steady WORK intervals, not a fitness verdict, and a single activity with a higher W/bpm
  does not show an improvement.
- **API calls**: one to list the activities, one per activity (at most 60) and one for the gear
  catalog unless cached.

## get_fatigue_resistance
Best power fresh vs after the athlete's kJ thresholds (kJ0, kJ1).
- **Background**: besides the normal power curve Intervals.icu keeps two "fatigued" curves built
  only from efforts that started after a configurable amount of work (sport settings `after_kj0` /
  `after_kj1`; in Intervals.icu: Settings -> sport settings -> power -> fatigued curves "after
  kJ"). Literature uses about 10-40 kJ per kg body mass for such thresholds.
- **Sport settings first**: the sport setting whose types include `activity_type` (default Ride,
  case-insensitive) gives the thresholds and the FTP.
  - Neither threshold configured: Intervals.icu then returns the fresh curve as kJ0/kJ1, so no
    pseudo values are shown; the output explains the setting and shows the fresh curve for
    reference.
  - One threshold configured: only that one is compared.
  - No sport setting covers the type: the thresholds are unknown; the kJ0/kJ1 curves are shown as
    Intervals.icu returns them, with that caveat.
- **Curves compared**: with `activity_id` that activity's power curve, fresh and per configured
  threshold (`power-curve.json?fatigue=kj0|kj1`); otherwise the athlete curves of `activity_type`
  for each id in `curves` (comma-separated, default `"42d"`, e.g. `42d`, `90d`, `1y`, `s0`), each
  with its `-kj0` / `-kj1` variant. `curves` is ignored with `activity_id`.
- **Values**: per duration (`durations`, comma-separated seconds, default `"60,300,1200"`) the
  watts of the exact curve point or of the nearest point within 10 % (the point used is shown), and
  the change of each fatigued curve in % relative to the fresh one. A fatigued curve without data at
  these durations is reported as missing ("no efforts after the threshold in this period"), one
  equal to the fresh curve wherever it has data as identical ("either the best efforts all came
  after the threshold or the threshold is too low"); no change is computed for either. `n/a` means
  no usable value near that duration.
- **Threshold suggestion** (`suggest_thresholds`, default true; offered when the sport setting
  exists but not both thresholds are set): a suggestion only, settings are never changed.
  - From body mass (athlete profile weight): kJ0 = 15 kJ/kg, kJ1 = 30 kJ/kg; without a weight from
    the sport setting's FTP: 1.5 h and 3 h at about 55 % of FTP (kJ per hour = FTP x 0.55 x 3.6).
    Rounded to 250 kJ.
  - Limited by the work of recent rides: rides of the same sport family with at least 1 h moving
    time in the last 90 days; their median and 75th percentile kJ are shown. When the 75th
    percentile is below the kJ1 suggestion or the median below kJ0, a second suggestion fits the
    current rides (kJ0 = min(kJ0, median), kJ1 = min(kJ1, 75th percentile), both rounded down to
    250 kJ, kJ1 at least kJ0 + 250), because thresholds above most rides leave the fatigued curves
    almost empty.
- **Related**: `get_long_ride_fatigue_profile` (HR, W/bpm and cadence at matched power within long
  rides; method: fatigue).
- **API calls**: one for the sport settings, then one per activity curve (fresh plus one per
  configured threshold) or one for the athlete curves, plus two (athlete profile, activity list)
  for a threshold suggestion: 2-5 in total.

## get_athlete_power_curves
Best power per duration of the athlete over whole periods.
- **Curves**: `this_season` (Intervals.icu curve `s0`, default true), `last_season` (`s1`, default
  true) and, when both `start_date` and `end_date` are given, a custom range curve
  `r.<start>.<end>` (YYYY-MM-DD, start strictly before end). Both dates or neither; at least one
  curve must be selected.
- **Parameters**: `activity_type` (default Ride; the sport of the curve, e.g. VirtualRide, Run);
  `durations` as a JSON array of integer seconds (default `[5, 15, 30, 60, 120, 300, 600, 1200,
  3600]`; only exact points of the curve are returned, other durations are left out silently);
  `indoor_outdoor` (`"indoor"` / `"outdoor"`, Intervals.icu's indoor filter; omit for both);
  `include_normalised` (default true) adds W/kg and, when different, the activity that set the W/kg
  best.
- **Output**: per curve its label and date range, then per duration the watts, W/kg and the id of
  the activity that set it.
- **API calls**: one.

## get_hr_curves
Highest average heart rate sustained per duration over one or more periods.
- **Parameters**: `activity_type` (default Run); `durations` as a JSON array of integer seconds
  (default `[5, 15, 30, 60, 300, 600, 1200, 1800, 3600]`, exact curve points); `curves` as a JSON
  array of curve ids, e.g. `"90d"`, `"42d"`, `"1y"`, `"s0"` (this season), `"s1"` (last season),
  `"r.2026-01-01.2026-03-01"` (custom range); `start_date` + `end_date` add one more custom range
  curve (both or neither, start strictly before end). The default `["90d"]` is used only when
  neither curves nor a date range are given.
- **Output**: per curve its label and date range, then bpm per duration with the activity that set
  it; durations without data are listed as not available.
- **Use**: together with `get_pace_curves` it shows progress such as the same pace at a lower heart
  rate; or compare the curves of different periods.
- **API calls**: one.

## get_pace_curves
Best time and pace per distance over one or more periods.
- **Parameters**: `activity_type` (default Run; e.g. TrailRun, Swim); `distances` as a JSON array of
  metres (default for runs `[400, 800, 1000, 3000, 5000, 10000, 21097.5, 42195]`, for Swim and
  OpenWaterSwim `[50, 100, 200, 300, 400]`); a requested distance matches a curve point within 1 m,
  otherwise it is listed as not available; `curves` and `start_date` / `end_date` as in
  `get_hr_curves` (default `["90d"]`); `gap=true` uses gradient-adjusted pace instead of raw pace.
- **Output**: per curve its label and date range, then per distance the best time, the pace
  (min/km; min/100 m for Swim and OpenWaterSwim) and the activity that set it.
- **API calls**: one.
