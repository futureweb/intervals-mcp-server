# Method: summary

How get_training_summary, get_weekly_summary and get_plan_compliance build period totals, the
custom-field aggregation policy and the matching of planned workouts with activities.

## get_training_summary

Activity-based totals of a period grouped by ISO week, month, sport, gear or in total. Read-only.
Compared with get_weekly_summary (one call to Intervals.icu's athlete summary) it adds grouping
by month, sport or gear, elevation, separate power/HR/pace loads, custom field aggregates (e.g. a
device training load, kept apart from the Intervals.icu load) and gear splits.

Parameters:

- `start_date` (required) and `end_date` (default today), YYYY-MM-DD; the start must not lie after
  the end. There is no maximum period; long periods return many groups.
- `group_by`: `week` (default; ISO weeks from Monday, labelled "Monday date (ISO year-week)"),
  `month` (YYYY-MM), `sport` (activity type), `gear` ("gear name (id)" or "no gear") or `total`
  (one group).
- `sport_types`: comma-separated activity types to include, e.g. "Ride,GravelRide"
  (case-insensitive exact types); default all.
- `include_gear`: show the per-gear split inside each group (text at standard and full; it is
  left out when a group has only activities without gear). Default true.
- `athlete_id`, `output_format` (`text` or `json`; JSON always contains every field, whatever
  `detail_level` and `include_gear` are: the groups with their summary and the fitness at their
  end, the overall summary and the fitness at the period end).
- `detail_level` (text):
  - `compact`: per group totals, loads, fitness at the end, sessions and time per sport and the
    device loads;
  - `standard` (default): plus time in zones, the per-sport and per-gear lines, feel/RPE,
    sessions of 3 h or more, the longest session, trainer sessions, the custom field aggregates
    (fields without a meaningful aggregate are only named) and the start-to-end changes of paired
    fields;
  - `full`: plus the aggregation reason of every custom field, and the fields without an
    aggregate as items with their number of values.
- With more than one group a TOTAL block follows.

Per group and in total:

- Sessions, moving and elapsed time, distance, elevation gain.
- Intervals.icu training load (`icu_training_load`; per activity Intervals.icu takes the power
  load, else the HR load, else the pace load) and the sums per method (`power_load`, `hr_load`,
  `pace_load`) over the activities that have them. They overlap: an activity with power also has
  an HR load, so they are not added.
- Time-weighted intensity: Intervals.icu intensity (`icu_intensity`, %) weighted by moving time,
  over the activities that have one.
- CTL, ATL, form (CTL - ATL) and ramp rate from the latest wellness record with CTL up to the
  group's last day: the Sunday of an ISO week or the last day of a month, capped at the period
  end; for sport, gear and total the period end.
- Time in power zones per zone (the sweet-spot bucket SS is listed apart because it overlaps
  Z3/Z4, so the zones add up) and in HR zones.
- Per sport (activity type) and per gear: sessions, moving time, distance, load.
- Feel distribution (count per feel value), mean RPE (`icu_rpe`), sessions of 3 h or more moving,
  the longest session, trainer sessions.
- Missing values are skipped, never counted as 0 (sums add only what exists; means use only the
  values that exist).
- API: the activity list (one request with every custom activity field in the field selection)
  and the wellness records of the period; custom item definitions, the gear list and the sport
  settings are cached per process.

### Custom field aggregation policy

Numeric custom activity fields (text and select fields are never aggregated) are aggregated over
the activities of each group by a generic policy derived from the field definition, its units and
the words of its name and code (camelCase and snake_case are split; "VO2 Max" also counts as
"vo2"). Nothing is tied to a vendor. The definition's own `aggregate` (SUM, MAX, MIN, AVERAGE) is
one input, but it is chosen for one activity's intervals and is often SUM for values that must
never be added across activities (percentages, ground contact time in ms, vertical oscillation in
cm, balance, scores).

Policies:

- `sum`: additive quantities (energy, volume, distance, time, counts): total and mean per session.
- `device_load_sum`: device training loads (training load, EPOC, TRIMP, impact load): summed but
  labelled as a device scale, separate from the Intervals.icu load and never added to it
  (get_training_load lists them the same way).
- `trend`: estimates and states (VO2max, performance condition, recovery time, detected
  thresholds, fitness age, predictions): latest value with its date, first value, change from
  first to latest, mean, median and range; never summed.
- `mean`: per-activity (intensive) values (percentages such as stamina, scores, training
  effects, running dynamics, heart rate, power, cadence, temperatures): mean, median, minimum
  and maximum; never summed, even when the definition says SUM. A definition aggregate of MIN or
  MAX makes that the headline value (e.g. the minimum temperature).
- `none`: no aggregate (text/select fields, and numbers without units or a recognisable meaning,
  where a sum would not be meaningful).

The rules, in this order (the first that applies wins):

1. An operator override from the server setting `CUSTOM_AGGREGATE_OVERRIDES` (see below).
2. A non-numeric field: `none`.
3. A load word in the name or code (load, epoc, trimp, tss, strain) and units other than %:
   `device_load_sum`.
4. A state phrase (recovery + time, fitness + age, performance + condition, vo2, vo2max,
   predicted, prediction, ftp, lthr, ltp, detected, threshold): `trend`.
5. A temperature (word temperature or temp, or units °C/°F): `mean` (headline MIN or MAX when the
   definition says so).
6. A per-activity word (effect, stride, cadence, gct, contact, oscillation, ratio, balance,
   flight, stamina, score, hr, heart, power, speed, pace, effectiveness): `mean`.
7. Intensive units (%, percent, point(s), bpm, W, watts, rpm, spm, ms, cm, mm, ratio, index,
   score, /min, breaths/min, m/s, km/h, kph, mph, ml/kg/min, ml/min/kg, ml/kg, W/kg, kg, lb,
   mmol/l, Nm, deg, °): `mean` (headline MIN or MAX when the definition says so).
8. Additive units (kcal, cal, kJ, J; ml, l, fl oz, oz; m, km, mi, ft; s, min, h and their
   spellings; steps, reps, count, strokes, laps), or no units and a sum word (calories, kcal,
   energy, distance, duration, steps, sweat, fluid, elapsed, moving, count), with a definition
   aggregate of SUM or none: `sum`.
9. A definition aggregate AVERAGE: `mean`; MIN or MAX: `mean` with that headline.
10. Additive units with any other definition aggregate: `sum`.
11. Otherwise `none`.

When the definition says SUM but the policy is not a sum, the reason says that the definition's
SUM is not applied across activities.

`CUSTOM_AGGREGATE_OVERRIDES` is an environment variable of the server (set by the operator, not a
tool parameter): comma-separated `Code=policy` pairs with the custom field code and one of
`sum`, `device_load_sum`, `trend`, `mean`, `none` (case-insensitive; an unknown policy is
ignored), e.g. `CUSTOM_AGGREGATE_OVERRIDES="TrainingLoad=device_load_sum,Stamina=mean"`. An
override takes precedence over every rule; its reason is "operator override".

Values:

- Missing values (null, NaN) are skipped. A stored 0 counts as a value and is reported
  (`zero_values`), except for `trend` fields on a never-negative scale (no negative value among
  them), where a stored 0 is the device's "no value" and is left out (reported as "stored 0 left
  out as 'no value'").
- Sport settings decide where a value counts (the field assignments, `activity_field_ids`, of the
  Intervals.icu sport settings):
  - the sport lists the field: the value counts;
  - the sport lists fields but not this one: the value is ignored and counted (e.g. running
    dynamics stored as 0 on rides);
  - the sport lists no fields (e.g. gravel rides without own settings) while the field is
    assigned to some sport: the lists of the other sports of its family stand in (GravelRide
    follows Ride); when no sport of the family has a list, every value counts. There only real
    non-zero values count, reported as "from sports without field assignment" with those sports;
    a stored 0 is a placeholder and ignored;
  - the field is assigned to no sport at all: the values on sports without a field list count
    (a stored 0 included); sports that list other fields still exclude it.
- Paired fields named "<stem> at start" / "<stem> at end" (also begin/beginning/initial and
  finish/final) with the same stem, e.g. stamina: over the activities with both values the median
  and mean start-to-end change and the largest drop.

## get_weekly_summary

A quick per-week overview from Intervals.icu's athlete summary (`athlete-summary.json`, one
request). Read-only.

- Parameters: `start_date` and `end_date` (both required, YYYY-MM-DD, start not after end) and
  `athlete_id`. Text output only.
- One block per ISO week (the week's Monday with the ISO week label), oldest first: sessions,
  moving time, distance and training load; one line per sport category with sessions (categories
  without sessions are left out); time in HR zones (non-zero zones only; the summary's
  `timeInZones` are HR zones, Z1..Z7 plus a trailing slot, verified against live responses: a
  week's values equalled the sum of the activities' HR zone times); CTL (fitness), ATL (fatigue),
  form and ramp rate at the END of the week, or of the latest day with data for the current week
  (shown as "as of").
- Sessions without heart rate data (e.g. strength) are still counted; in live data
  WeightTraining activities were reported under the "Workout" category.
- With an API key the endpoint may also return rows of other athletes (followed or coached):
  only the requested athlete's rows are kept (rows without an athlete id stay; the alias "0" is
  resolved to the real id).
- For grouping by month, sport or gear, separate power/HR/pace loads and custom fields use
  get_training_summary.

## get_plan_compliance

Planned workouts against executed activities in a date range. Read-only, two requests (the
calendar events and the activities of the range). Text output only.

- Parameters: `start_date` and `end_date` (both required, YYYY-MM-DD, start not after end) and
  `athlete_id`.
- Planned workouts are calendar events of category WORKOUT, in order of day.
- Matching: a workout is linked to the activity named by its `paired_activity_id` (returned by
  the live API, not in the OpenAPI Event schema); otherwise to the activity whose
  `paired_event_id` is the workout's id. An activity is counted for one workout only: a second
  workout pointing to the same activity stays unlinked. A workout whose `paired_activity_id`
  names an activity outside the fetched range counts as completed without actuals.
- Completed: each pair shows planned vs actual duration (moving time) and Intervals.icu load
  with the deviation in percent (n/a when a value is missing).
- Missed: unlinked workouts before today. Upcoming: unlinked workouts of today or later (not
  missed). "Today" is the athlete's local date.
- Unplanned: activities not linked to any workout of the range. Activities whose
  `paired_event_id` names a workout outside the range are listed separately as "completed,
  planned outside range" (neither unplanned nor counted in the completion).
- Completion = completed / (completed + missed); n/a when nothing was due.
- NOTE events that set training availability, allowed sports or a maximum training time are
  listed as well.
