# Method: load

How get_training_load, get_load_projection and get_coach_context compute training load, fitness
(CTL/ATL/form) and the projection or simulation of planned training, which parameters they take
and how to read the results.

## Conventions of the load tools

- Only the Intervals.icu training load of an activity (`icu_training_load`) is used. Device loads
  (custom activity fields such as a Garmin training load or EPOC) have their own scale: they are
  listed separately and never added to it.
- A day without activities is a real rest day with load 0 (it counts in means, SDs and
  monotony). An activity without a load does not add to its day, as in Intervals.icu's own
  fitness model, and is counted separately ("sessions without load").
- A missing wellness value (CTL, ATL, HRV ...) is never replaced by 0.
- Days are local days in the athlete's time zone; "today" is the athlete's today.
- Sport families: Ride, VirtualRide, GravelRide, MountainBikeRide, EBikeRide, EMountainBikeRide,
  TrackRide, Velomobile, Handcycle = cycling; Run, TrailRun, VirtualRun = running; Walk, Hike,
  Snowshoe = walking; Swim, OpenWaterSwim = swimming; NordicSki, BackcountrySki, VirtualSki,
  RollerSki, AlpineSki = skiing; Rowing, VirtualRow, Canoeing, Kayaking = rowing; any other type
  is its own family.
- The metric set (acute/chronic load, ACWR, monotony, strain, deload-like weeks, today's fitness
  recomputed without planned workouts, top sessions, one coaching context) follows the coach
  metrics proposed by morritter in upstream pull request mvilanova/intervals-mcp-server#150. It is
  re-implemented with explicit sample sizes and without verdicts.
- Reference ranges are population heuristics shown as context, not individual limits; no
  assessment is made:
  - acute:chronic workload ratio 0.8-1.3: Gabbett 2016, Br J Sports Med 50:273-280 (team sports;
    debated, e.g. Impellizzeri et al. 2020, Int J Sports Physiol Perform 15:907-913);
  - monotony above 2.0 is commonly cited as high: Foster 1998, Med Sci Sports Exerc 30:1164-1168;
  - deload-like week: 7-day load at or below 80 % of the chronic weekly average (rule of thumb
    adopted from #150);
  - CTL ramp of about 5-8 points per week for build weeks: Friel J. 2015, "Why Ramp Rate Is an
    Important Training Metric", TrainingPeaks ("an increase in CTL of about 5 to 8 points per week
    is about right for most");
  - at least one passive rest day per week: Meeusen et al. 2013, Med Sci Sports Exerc 45:186-205
    (ECSS/ACSM consensus statement on overtraining).

## get_training_load

Acute and chronic load, acute:chronic ratio, monotony, strain and deload-like weeks of the past,
per sport family and per ISO week, with CTL/ATL/form/ramp for context. Read-only.

Parameters:

- `end_date`: last day of the windows, YYYY-MM-DD; default today. It must not lie in the future
  (error; use get_load_projection for planned load). With the default and no activity recorded
  today yet, the windows end yesterday so an untrained morning is not counted as a rest day; the
  output says so. Pass `end_date` = today to include today anyway.
- `acute_days`: acute window, 3-28 days (default 7).
- `chronic_days`: chronic window, 14-120 days (default 28), longer than `acute_days`.
- `weeks`: number of ISO weeks in the weekly table, 1-26 (default 4).
- `athlete_id`: default the configured athlete.
- `output_format`: `text` or `json`.
- `detail_level`:
  - `compact`: windows, ratio, last 7 days (monotony, strain, deload), fitness, the 3 sport
    families with the highest chronic load;
  - `standard` (default): plus all sport families, the ISO-week table and the device loads;
  - `full`: plus the daily loads of the chronic window and the definitions.
  - JSON contains everything at every level except the daily loads (only at `full`); it includes
    the references and definitions.

Method:

- Daily load: sum of the Intervals.icu load of the activities of each local day, zero-filled.
- Acute load = sum over the acute window; chronic load = sum over the chronic window (it contains
  the acute window). Each window reports days, sessions, sessions without load, days with
  activity, days with load 0 and the daily mean; the chronic window also the weekly mean
  (chronic load / chronic days x 7).
- Acute:chronic ratio = acute daily mean / chronic daily mean (coupled rolling averages);
  undefined when the chronic load is 0. Its position against 0.8-1.3 is given (below, inside,
  above). Fewer than 8 days with load in the chronic window: flagged as a small sample.
- Monotony (Foster) = mean / sample SD of the daily load over the last 7 days (always 7 days,
  whatever `acute_days` is), rest days included; undefined when the SD is 0 (no load or identical
  days), with a note. Strain = 7-day load x monotony.
- The 7-day load as a percentage of the chronic weekly mean; deload-like at <= 80 %.
- Per sport family: the same metrics, ordered by chronic load. A family's monotony and strain are
  only kept with at least 3 days with load in the last 7 days. Primary sport = the family with the
  highest 7-day load. With more than one family with load in the last 7 days, a steady low load
  from cross-training raises the total monotony (higher mean, same SD), so the primary sport's own
  monotony is shown next to it.
- ISO weeks (Monday-Sunday, oldest first; the last may be partial): load, sessions, sessions
  without load, hours (moving time), days with activity, rest days (days without any activity),
  load per sport family (n/a when every session of the family lacks a load), monotony (needs at
  least 5 days and 3 days with load), strain (complete weeks only) and, for complete weeks, the
  load as a percentage of the weekly mean of the `chronic_days` ending on that Sunday, with the
  deload-like flag.
- Fitness at the end date: CTL, ATL, form = CTL - ATL (also as % of CTL) and ramp (Intervals.icu
  `rampRate`, otherwise the CTL change over 7 days) from the latest wellness record up to 7 days
  before the end date that has both CTL and ATL. For today: if Intervals.icu's `ctlLoad` of today
  exceeds the load completed today by more than 1, it already counts planned workouts that are not
  done yet; CTL and ATL are then recomputed from yesterday's values with the completed load only
  (source `recomputed_without_planned`, approach of #150).
- Device loads: the custom activity fields whose aggregation policy is `device_load_sum` (see
  intervals://methods/summary; `CUSTOM_AGGREGATE_OVERRIDES` can set it) are summed over the acute
  and the chronic window with the number of values. Activities without a value are not counted.
  The sport settings' field assignments decide where a value counts; values from sports without
  a field assignment are reported as such.
- API: the activity list (one request with a field selection) and the wellness records of the
  last 14 days; custom item definitions and sport settings are cached per process.

## get_load_projection

CTL, ATL and form projected over the planned workouts of the calendar, or a what-if scenario
compared with the calendar plan. Read-only: nothing is written to the calendar.

Parameters:

- `end_date`: last projected day, YYYY-MM-DD, after today and at most 180 days ahead. Default 28
  days ahead; without `end_date` the period is extended to the target day and to the last
  scenario day (up to 180 days). With `end_date`, scenario days and `target_date` must lie within
  it.
- `ctl_days`, `atl_days`: time constants of the model (default 42 and 7, Intervals.icu's
  defaults); 1 <= `atl_days` < `ctl_days` <= 365. The model check shows whether they match the
  athlete's Intervals.icu fitness settings.
- `athlete_id`, `output_format` (`text` or `json`).
- `detail_level`:
  - `compact`: start, today, planned workouts, end values, lowest form and highest ramp, races,
    target, the number of week metrics outside the cited ranges, model check;
  - `standard` (default): plus the ISO weeks and the plan checks with sources; with a scenario
    also the four completed weeks before this one and the week-by-week comparison;
  - `full`: plus every day (and, with a scenario, the proposed sessions).
  - JSON: without a scenario everything, days included. With a scenario, below `full` the days
    and `simulation.sessions` are left out, at `compact` also the week rows (the comparison weeks
    stay); `omitted` lists what was left out.
- `scenario`: a what-if plan of sessions that are NOT in the calendar (format below).
- `target_date`: target day YYYY-MM-DD, after today and not after the last possible day. Without
  it, the next RACE_A event after today is the target day when `target_form` or a scenario is
  given.
- `target_form`: form range wanted at the start of the target day (format below); starts the
  search for the load before the target.
- `taper_days`: number of days before the target that the search varies, 1-42 (default 7).

Model and start:

- CTL and ATL are exponentially weighted averages of the daily Intervals.icu load:
  `value + (load - value) x (1 - exp(-1 / time constant))` per day.
- Start: Intervals.icu's CTL/ATL at the end of the latest of the last 14 days before today that
  has both values (normally yesterday); without one there is nothing to project from (error).
  Days between that record and yesterday use Intervals.icu's `ctlLoad`.
- Today: the load completed today plus the planned workouts of today that are not done yet.
- Planned workouts: calendar events of category WORKOUT from today to the end that are not paired
  with an activity, with their planned Intervals.icu load. Workouts without a planned load add 0
  and are reported (the projection understates those days). Without planned workouts the
  projection shows only the decay of CTL and ATL; the output is labelled PROJECTION WITHOUT
  PLANNED TRAINING or PROJECTION WITHOUT PLANNED LOAD.
- Values of days, weeks, the end and the lowest form are end-of-day values (that day's load
  included), as in Intervals.icu's wellness records. Race days (RACE_A, RACE_B, RACE_C events in
  the period) and the target day are reported at the start of the day (the end of the day
  before), so the race's own load does not count.
- Intervals.icu's own projected CTL/ATL on the end day (from its future wellness records) is shown
  for comparison; in JSON each calendar-plan week also has Intervals.icu's CTL/ATL at its end.
- Model check: the last 14 days of Intervals.icu loads (`ctlLoad`/`atlLoad`) are replayed through
  the model; the largest CTL and ATL differences to the stored values are reported (not possible
  when those wellness records are incomplete). A small difference shows that the time constants
  match.
- With what-if inputs (scenario, `target_date` or `target_form`) the assumptions are listed: the
  model with constant time constants; every listed session is done on its day with exactly its
  load and nothing else is trained (no illness, travel or missed sessions); the calendar mode;
  how loads were estimated and templates split; target values at the start of the day; calendar
  entries in the period that the model ignores (SICK, INJURED, HOLIDAY, FITNESS_DAYS,
  SET_FITNESS: no load change, constant time constants).

Plan statistics per ISO week (projected days only): load, CTL/ATL/form/ramp at the week's last
day, lowest form and its day, sessions (completed, planned and scenario), sessions without load,
estimated sessions, load per sport family, hours (sessions with a duration; n/a when none has
one), sessions without duration, the longest session (and its share of the target event's planned
duration when that calendar event has one; no commonly cited range), rest days (days without any
session; a session without load is not a rest day) and monotony (at least 5 days and 3 days with
load; identical daily loads leave Foster's monotony undefined, which is the most monotonous case
and is flagged). Weeks outside a commonly cited range are listed: CTL ramp above 5-8 per week
(only weeks above 8 are listed, because recovery, taper and transition weeks lie below it by
design), monotony above 2.0 (or identical loads), no rest day in a complete week.

### Scenario format

`scenario` is an object, the same object as JSON text, or a plain list (= `sessions`). Allowed
keys: `calendar`, `sessions`, `weekly`; unknown keys are rejected with the list of allowed keys.
At least one of `sessions` and `weekly` is needed. All dates lie from today to the last possible
day (`end_date`, or 180 days ahead without it). At most 500 sessions in total (template sessions
included). Invalid input returns an error naming the entry, e.g. `weekly[0].days`.

- `calendar`: how the planned workouts of the calendar are used (case-insensitive):
  - `add` (default): the scenario is added on top of the planned workouts;
  - `replace`: planned workouts from the first to the last scenario day are left out (template
    weeks count completely), the others stay;
  - `none`: no planned workout of the calendar is used.
- `sessions`: list of single sessions, each an object with
  - `date` (YYYY-MM-DD, required);
  - `load` (0-1500), or `duration_min` (1-1440) and `intensity_factor` to estimate it:
    load = hours x IF^2 x 100 (the TSS definition for a session at intensity factor IF; for a
    planned IF or HR/pace-based sports an estimate; at most 1500). `intensity_factor` is 0.3-1.5,
    or 30-150 read as percent. With `load` given, `duration_min` is optional and only used for
    hours and the longest session;
  - optional `sport` (activity type such as "Ride" or "Run", for the per-sport split) and `name`
    (texts up to 80 characters).
- `weekly`: list of weekly templates (a single object is accepted), each with
  - `start`: first day YYYY-MM-DD (default the next Monday after today);
  - `weeks`: 1-26; default the length of a per-week `load`/`hours` list, else 1. The template
    must end (start + 7 x weeks - 1) by the last possible day;
  - `load`: weekly load 0-5000, one number for every week or a list with one value per week; or
    `hours` (0-100 per week, number or list) with `intensity_factor` to estimate the weekly load as
    hours x IF^2 x 100 (at most 5000). `load` and `hours` lists must have the same length;
  - `sessions`: sessions per week, 1-7; or `days`: list of weekdays (Mon, Tue, Wed, Thu, Fri,
    Sat, Sun; the first three letters count; no day twice, one session per day: use `sessions`
    entries for doubles). Given both, their counts must agree. Default days by count: 1 = Sat;
    2 = Wed, Sat; 3 = Tue, Thu, Sat; 4 = Tue, Thu, Sat, Sun; 5 = Tue, Wed, Thu, Sat, Sun;
    6 = Tue-Sun; 7 = every day. Neither given: 5 sessions;
  - `long_session_share` (from 1/sessions to 0.95, only with more than one session): the long
    day gets this share of the weekly load and hours, the other days split the rest equally.
    Without it the weekly load and hours are split equally over the session days;
  - `long_day`: one of the session days (default Sat if it is a session day, else Sun, else the
    last session day);
  - `sport` (default "Ride") and `name`.

Examples:

```json
{"sessions": [{"date": "2026-10-18", "load": 180, "sport": "Ride", "name": "Long ride"}]}
```
One extra session on top of the calendar plan.

```json
[{"date": "2026-10-18", "duration_min": 240, "intensity_factor": 0.7}]
```
A plain list = sessions; load estimated as 4 h x 0.7^2 x 100 = 196.

```json
{"calendar": "replace",
 "weekly": [{"start": "2026-10-19", "weeks": 4, "load": [500, 550, 600, 400],
             "days": ["Tue", "Thu", "Sat", "Sun"], "long_session_share": 0.4, "long_day": "Sat",
             "sport": "Ride"}]}
```
Four template weeks replace the planned workouts of 2026-10-19 to 2026-11-15; Saturday carries
40 % of each week's load, the other three days 20 % each.

```json
{"calendar": "none", "weekly": {"weeks": 3, "hours": 8, "intensity_factor": 0.72, "sessions": 5}}
```
Three weeks from next Monday of 8 h at IF 0.72 (weekly load about 415) on Tue, Wed, Thu, Sat and
Sun, ignoring the calendar.

With a scenario the result compares it with the calendar plan: end values and their difference,
lowest form, highest ramp, week by week (load, CTL, form, ramp), race days, plan checks for both,
what the calendar mode dropped (sessions and load), the templates as understood (days, long day,
weekly loads and hours, whether estimated), and the four completed ISO weeks before this one
(mean and maximum weekly load, mean hours and rest days, longest session) for comparison.

### Target day and the search for the load before it

- The target day is `target_date` (with the race event on that day, if any) or, when only
  `target_form` or a scenario is given, the next RACE_A event after today. Its CTL, ATL and form
  (also as % of CTL) are reported at the start of the day (end of the day before) for the
  calendar plan and, with a scenario, for the scenario. With a race event, its category, name and
  planned duration are shown.
- `target_form`: a range of form at the start of the target day: `"5,15"` (also `"5..15"`,
  `"5 to 15"`, `"5;15"`) in form points (CTL - ATL), `"5%,20%"` in percent of CTL, or a list
  `[5, 15]` (points). Both ends in the same unit, low <= high, points within +-200, percent within
  +-100. Without a target day the result says so (pass `target_date` or add a RACE_A event within
  180 days).
- Whether the calendar plan (and the scenario) lands inside the range is reported.
- Search: only the `taper_days` days before the target (from tomorrow at the earliest) are varied;
  everything else stays as planned (on the scenario when one is given, else on the calendar
  plan). The model is first run up to the day before that window. Two one-dimensional grid
  searches: (a) the load planned in the window scaled from 0 to 200 % in 1 % steps (only when the
  window has planned load); (b) a constant weekly load spread evenly over the window (weekly / 7
  per day), from 0 in steps of 5 up to max(1000, CTL x 21 rounded up to a multiple of 50). The
  model is linear in the load, so the matching values form one interval: the result gives its
  first and last value with the form and CTL they produce (and the window load or the load per
  day); when the range is not reached, the highest and lowest form the search can reach. The
  state before the window is included.
- Example: `target_date="2026-11-15", target_form="5%,20%", taper_days=10` asks which load in the
  10 days before 15 November puts the form at 5-20 % of CTL on the morning of that day.

API: the wellness records from 21 days ago to the end, the calendar events from today to the end
and the activities of today (with a scenario: from the Monday four weeks before this week). A
projection or simulation is arithmetic on the plan, not a forecast; no assessment is made.

## get_coach_context

One compact coaching context for an end date (about 2-3k characters of text), recommended as the
first call for a weekly or general review. Read-only. It reuses the methods of
get_training_load, get_intensity_distribution (intervals://methods/intensity) and get_durability
(intervals://methods/durability); the idea of one pre-computed coaching context comes from the
coach report by morritter in upstream pull request #150.

Parameters:

- `end_date`: last day YYYY-MM-DD, default today, not in the future. With the default and no
  activity recorded today yet, the windows end yesterday (as in get_training_load).
- `athlete_id`.
- `output_format`: `text`, or `json` (the full structure at every detail level).
- `detail_level` (text only):
  - `compact`: load, fitness, intensity 7 d and 28 d, method line, recovery markers, today's
    missing wellness fields, durability;
  - `standard` (default): plus per-sport load, the intensity drift, top sessions of 7 days, the
    plan of the next 7 days with the next race, and the coverage;
  - `full`: plus the last 4 ISO weeks and the references.
- `threshold_as`: how power zone Z4 (91-105 % FTP) counts in the intensity distribution:
  `moderate` (three-zone Z2, default) or `high` (Z3), as in get_intensity_distribution.

Contents and method:

- Load: 7-day and 28-day Intervals.icu load with sessions and days with load 0, the 28-day
  weekly mean, acute:chronic ratio (coupled daily means) with its position against 0.8-1.3 and a
  small-sample flag, Foster monotony and strain of the last 7 days, the 7-day load as % of the
  28-day weekly mean (deload-like at <= 80 %); per sport family 7-d/28-d load and ratio, the
  primary sport and its own monotony when several sports are trained.
- Fitness: CTL/ATL/form/ramp at the end date as in get_training_load (today recomputed without
  planned workouts that are not done yet).
- Intensity: three-zone distribution (zone basis auto: power for cycling, HR then pace otherwise)
  of the last 7 and 28 days with polarization index, class, hard sessions and hard days (at least
  10 min in Z3, or IF >= 0.85 on at least 20 min), the drift between the first and second 14 days
  of the 28, and the 28-day split per sport. When the totals combine power and HR/pace zones a
  caveat gives the per-sport split (different thresholds).
- Method line: windows (7 d acute / 28 d chronic, ratio of daily means, coupled), monotony
  definition, zone basis, the `threshold_as` mode and the hard-session rule.
- Recovery markers (numbers only, no verdict): for HRV (ms), resting HR (bpm) and sleep (h) the
  7-day mean with its number of values, against the mean of the 42 days before those 7 days (the
  baseline excludes the compared days; with its n, the SD of the daily values and a small-sample
  flag below 14 values), the difference (absolute and %) and a z-score. A stored 0 of these
  metrics counts as missing.
  - z = (7-day mean - baseline mean) / (SD of the 7-day rolling means whose windows lie in the 90
    days before the 7-day window x sqrt(1 + 7/42)). The SD of 7-day means is the right reference
    for a 7-day mean (overlapping means inside only 42 days underestimate it); the factor
    sqrt(1 + 7/42) adds the uncertainty of the baseline mean (Var(7-d mean - 42-d mean) is about
    spread^2 x (1 + 7/42)). At least 28 such 7-day means are needed, otherwise no z is given.
  - |z| up to about 2 is normal week-to-week variation; in simulations about 7 % of weeks without
    a real change reach |z| > 2.
- Today's wellness completeness (end date today): the usual wellness fields (present on at least
  80 % of the 14 days before today) that are not yet in today's record, grouped into night/morning
  values, daily metrics and day totals (up to 6 names per group in the text, all in JSON
  `today_completeness`). They are not yet available, not normal and not 0; details with
  get_recovery_snapshot.
- Durability over the 28 days: per sport family the median aerobic decoupling of steady sessions
  with the default filter of get_durability (at least 60 min, moving >= 85 % of elapsed, HR,
  variability index <= 1.20, rides with power, device temperature <= 25 °C), a sample note
  (qualifying share, small sample below 8, mixed indoor/outdoor, bikes/shoes or power meters),
  sessions above 5 %, the last 7 days against the window, and the efficiency factor mean with n;
  plus the number of sessions excluded by the quality filter.
- Top sessions of the last 7 days: the 5 with the highest load (ties: higher IF, then newer); if
  the session with the highest IF is not among them it replaces the fifth, so a short hard session
  is not hidden behind long easy ones (approach of #150).
- Plan (end date today only): planned WORKOUT events of the next 7 days (from tomorrow) with
  their number, planned load (workouts without one are counted) and hours, and the next race
  (RACE_A/B/C from today, up to 42 days ahead). NO PLANNED WORKOUTS is stated explicitly (a load
  projection then shows only the decay).
- Coverage of the 28 days: sessions, sessions without load, % of moving time with zones, days
  with HRV, resting HR and sleep.
- Weeks (full): the last 4 ISO weeks with load, rest days, monotony and % of the trailing weekly
  mean.
- Small samples are flagged; missing values are never counted as 0; no verdict or diagnosis.
- API: at most four requests: the activities of the last 57 days, the wellness records of the
  last 98 days and, when the end date is today, the calendar events of the next 42 days and the
  last 14 days of wellness with all fields (completeness). Custom field names come from cached
  definitions only (no extra request).
- Details: get_training_load, get_intensity_distribution, get_durability, get_recovery_snapshot,
  get_load_projection.
