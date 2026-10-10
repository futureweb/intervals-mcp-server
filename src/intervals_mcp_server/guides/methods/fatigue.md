# Method: fatigue

How the long-ride fatigue profile (HR, W/bpm, cadence and stamina at matched power before and after
work thresholds) and the submaximal fatigue test trend work: parameters, selection, method,
statistics, caveats, sources and API cost. Both tools are read-only and report statistics with
sample sizes; nothing is calibrated or judged.

## get_long_ride_fatigue_profile
How the athlete rides the same power before and after a given amount of work: heart rate at
matched power (drift), watts per heartbeat, cadence and Garmin stamina / potential stamina, in
steady segments of a power band, split into phases by work thresholds. Each threshold crossing and
each climb carries the prior work that led to it, so 1,500 kJ of easy riding can be told apart from
1,500 kJ with many hard efforts.

### Parameters
- `activity_ids`: comma-separated activity ids. Duplicates are ignored and the list is cut to
  `limit` before any request (ids beyond the limit are named in the output, not fetched). Given
  rides are analysed whatever work they reached; an unreached threshold is shown as "not reached".
- `start_date` / `end_date`: the period searched when no ids are given; `start_date` default 180
  days before `end_date`, `end_date` default today.
- `sport_types` (default `"Ride,GravelRide"`): activity types of the period search
  (case-insensitive).
- `limit` (default 6, capped to 1-12): rides analysed, newest first; rides of the period beyond the
  limit are counted in the output.
- `power_bands`: comma-separated, non-overlapping bands in W as `"low-high"` (half-open,
  low <= W < high), e.g. `"180-200"` or `"170-190,190-210"`. Default: 75-85 % of the FTP of the
  newest ride, rounded outward to 5 W (an error asks for `power_bands` when that ride has no FTP).
- `work_thresholds` (default `"750,1500"`): 1 to 3 positive values (sorted, duplicates removed) in
  `threshold_unit`.
- `threshold_unit`: `kj` (absolute kJ, default) or `kj_per_kg` (kJ per kg body mass, converted per
  ride with its body mass; rides without a body mass are skipped).
- `min_segment_secs` (default 120, at least 90: the first 60 s of a segment are HR settling time):
  minimum steady segment length.
- `climb_after_hours` (default 2, >= 0): climbs starting at or after this many hours are marked late.
- `min_climb_gain_m` (default 100, > 0): minimum elevation gain of a climb.
- `detail_level`: `compact` = per ride the totals, the change lines per band and the number of
  climbs (late ones counted), plus the across-ride statistics; `standard` (default) adds the
  threshold crossings with their prior work and stamina, the phase summaries per band, every climb,
  the method and the background sources; `full` adds every steady segment with its prior work.
  In JSON, `compact` drops the segments and keeps only late climbs, `standard` drops the segments,
  `full` keeps everything.
- `output_format`: text or json.

### Ride selection and body mass
- Without ids: the activities of the period and sports, newest first, keeping only long rides
  whose total work (Intervals.icu `icu_joules`) reaches the highest threshold (in kJ/kg: the highest
  threshold x the ride's body mass).
- Body mass: the activity's weight, else the athlete profile weight (fetched once when any ride
  lacks one), else unknown (kJ/kg values are then not shown).
- Streams per ride: time, watts, heartrate, cadence, temp, altitude, fixed_altitude, distance,
  velocity_smooth, plus Garmin stamina and potential stamina when the ride has them (a custom stream
  whose type or custom item name contains "stamina"; "potential" in it marks potential stamina).
- Rides without streams, without time or power, or without body mass for kJ/kg thresholds are
  listed as skipped with the reason.

### Method
- **Work**: cumulative sum of power x time on the time stream (1 Hz recordings: the sum of the
  samples, as Intervals.icu's `icu_joules`); a recording pause (time jump > 5 s) adds the sample
  once, never the pause. **kJ above FTP** is the sum of the power above FTP (`max(0, W - FTP)`),
  matching Intervals.icu's `icu_joules_above_ftp`. An **effort above FTP** is a run of at least 60 s
  in which the 30 s rolling power stays at or above FTP. Without FTP, work above FTP is `n/a`.
- **Steady segments**: maximal runs in which the trailing 30 s rolling power (full windows, no time
  gap > 2 s) stays inside the band widened by 20 % of its limits; a segment counts when it lasts at
  least `min_segment_secs`, its mean power lies in the band (low <= W < high), at most 10 % of its
  samples are coasting (< 10 W or missing) and heart rate covers at least 90 % of the measured part.
  The first 10 minutes of the ride are left out (warm-up, HR not settled) and segments are cut at
  the work thresholds, so a segment never spans two phases.
- **Segment metrics** are taken over the segment minus its first 60 s (HR lags behind power
  changes): time-weighted mean power, HR, W/bpm (mean power / mean HR), non-zero cadence and
  temperature; stamina and potential stamina at the segment start and end.
- **Phases**: before the first threshold (the reference), between the thresholds and after the
  last one, labelled e.g. `0-750 kJ`, `750-1500 kJ`, `>= 1500 kJ`. Phase values are time-weighted
  over its segments. A phase with fewer than 3 segments or less than 10 measured minutes is a small
  sample.
- **Changes** are later phase minus reference: HR in bpm, W/bpm in %, cadence in rpm, power in W
  (HR also follows the power within the band, so the power difference is shown too) and
  temperature. A change is not comparable when one of the phases has no segments; small samples
  and cadence differences above 15 rpm (climbing vs flat, gearing) are flagged.
- **Threshold crossings**: where each threshold is reached (elapsed time), with the prior work:
  kJ, kJ/kg, kJ above FTP and its share of the work, time above FTP, number of efforts above FTP,
  and stamina / potential stamina at that point.
- **Climbs** come from the same climb detection as `analyze_climbs` (method: climbs; smoothed
  altitude with hysteresis, at least `min_climb_gain_m`): start time, before / after `climb_after_hours`, duration, gain,
  average grade, average power and NP, HR, W/bpm, cadence, stamina and potential stamina at the
  start and end, and the prior work before the climb.
- **Across rides** (two or more rides): per band and later phase, the per-ride changes against the
  reference phase as median [min..max] with n, for HR, W/bpm, cadence and power; each ride counts
  once and needs data in both phases. Fewer than 3 rides, or more than half of the rides with a
  small phase sample, make the row a small sample, not reliable; rides with small phase samples and
  with cadence shifts are counted. With at least 4 rides they are also split at the median share of
  work above FTP done before the threshold (lower vs higher share, HR and W/bpm per group): context,
  not a predictor.
- When most phases are small samples, the output suggests a wider power band, a shorter
  `min_segment_secs` or more rides.

### Caveats
- Heart rate at a given power also follows heat, hydration, fuelling, sleep and the day's form, and
  outdoor steady segments differ in terrain and cadence; the changes are statistics of this ride /
  these rides, not a fitness verdict.
- Work above FTP before a point is context (riders often go harder on good days), not a predictor.
- Several bikes / power meters are flagged (absolute power bands are not calibrated against each
  other), as are mixed indoor and outdoor rides.

### Background
- Intervals.icu fatigued power curves after kJ0 / kJ1: forum thread "Fatigue resistance",
  https://forum.intervals.icu/t/fatigue-resistance/4396 (see also `get_fatigue_resistance`,
  method: comparisons).
- kJ per kg thresholds: "Power curve after kj/kg",
  https://forum.intervals.icu/t/power-curve-after-kj-kg/93688.
- Work above FTP and the "good-day" effect: "Three ways field data fooled me about durability",
  https://forum.intervals.icu/t/three-ways-field-data-fooled-me-about-durability-1-350-climbs-33-amateurs/132461.

### API calls
One to list the activities (or one per id), one for the athlete profile when a ride lacks a body
mass, one stream request per ride, plus the gear catalog and the custom stream definitions unless
cached.

## get_submax_test_trends
Submaximal fatigue tests detected by Intervals.icu (#SFT): validity of each test and the trend of
the valid ones over the weeks.

### What Intervals.icu detects
- Intervals.icu detects a steady effort at a configured target early in an activity (sport settings
  `sft_*`: type POWER or PACE, duration, target in % of FTP or threshold pace, tolerance, maximum
  coefficient of variation, latest start), tags the activity #SFT and stores the test on the
  activity (`submax_fatigue_test`): start and end index, average watts (or m/s), target, CV, HR at
  the end of the test (`final_bpm`), HR recovery after the test (`hrrc`, with its recovery window
  `end_index_hrrc`, when the athlete eased off), efficiency factor (average power / final HR), RPE,
  time to exhaustion and an ignore flag. The configured test per sport is shown in the output.
- A detection is not automatically a benchmark: the same minutes can be the first part of a
  threshold interval. Every test therefore passes a validity filter; only valid tests feed the
  trend and excluded tests are listed with their reasons.
- HRRc counts only with a recovery window: a value of 0 without one means "not measured", never
  0 bpm.
- Sources: "Automatic Submaximal Fatigue Testing",
  https://forum.intervals.icu/t/automatic-submaximal-fatigue-testing/132525; HRRc:
  https://forum.intervals.icu/t/heart-rate-recovery-hrrc/387.

### Parameters
- `start_date` / `end_date`: period (`start_date` default 180 days before `end_date`, `end_date`
  default today).
- `sport_types`: comma-separated activity types (case-insensitive); default all.
- `limit` (default 30, capped to 1-60): tests, newest first; older tests beyond the limit are
  counted.
- `tolerance_pct` (0-50): allowed deviation of the average from the target in %; default the
  test's own tolerance (5 % when the test has none).
- `require_recovery`: exclude tests without an HR recovery part.
- `check_context` (default true): check the intervals and streams around each POWER test (two
  extra API calls per test).
- `detail_level`: `compact` = header, test settings, counts per exclusion reason and the trends of
  HR at the end, efficiency factor and HRRc (no per-test lines, no stream metrics, no weekly
  table; JSON test rows reduced to id, date, average, target, final HR, EF, HRRc, valid and reason
  codes); `standard` (default) = every test with its values and VALID / EXCLUDED status, all trend
  metrics and the per-ISO-week table; `full` also shows why the context of a test was not checked.
- `output_format`: text or json.

### Validity (reasons for exclusion)
- `ignored`: marked as ignored in Intervals.icu.
- `incomplete`: test without type, target or average.
- `off_target`: average outside the target tolerance.
- `not_steady`: coefficient of variation above the test's limit.
- `inside_workout` (with `check_context`): part of a longer work interval (a WORK interval at least
  60 s longer than the test holds 80 % of the test window), or the effort continued after the test
  (mean power in the next 60 s at least 80 % of the target).
- `hard_before` (with `check_context`): mean power in the 5 min before the test at least 90 % of the
  target.
- `no_recovery` (with `require_recovery`): no HR recovery part.

### Context check (POWER tests with time and power streams)
- Records HR at the start and end of the test and its rise, the mean power 5 min before and 60 s
  after the test and, when the minute after is easy (power below 50 % of the target), the HR drop
  60 s after the test (HR at the end minus the mean HR 55-65 s later). Pace tests and tests without
  usable streams are not checked (noted with `detail_level="full"`).

### Trend
- Valid tests are grouped per sport family and test type: power tests (W, efficiency factor in
  W/bpm) and pace tests (m/s, efficiency factor in m/s per bpm) are never pooled.
- Metrics: HR at the end of the test (Intervals.icu), efficiency factor, HRRc (Intervals.icu), HR
  rise during the test (stream) and HR drop 60 s after the test (stream, easy minute only). Each
  with n, first -> last, change, mean, SD (two or more tests) and the least-squares slope per week
  over the weeks covered (three or more tests, not all on the same day); fewer than 4 tests are a
  small sample, not reliable.
- Per ISO week: number of tests and the means of HR at the end, EF, HRRc, average and target.
- Notes per group: targets differ by more than 1 % (HR at the end is then not like for like; the
  efficiency factor partly accounts for it), different bikes / shoes (power meters), mixed indoor
  and outdoor tests.
- When no test is valid, the output describes a benchmark test: a stand-alone steady effort at the
  configured target, early in the activity and not part of a longer interval, followed by about a
  minute of easy riding for the HR recovery.

### Caveats
- HR at the end of the test depends on the target (FTP changes), heat, fatigue and the power meter;
  the trend is a statistic with n, not a fitness verdict.

### API calls
One for the sport settings, one for the activity list, plus two (intervals and streams) per power
test with `check_context`.
