# Method: wellness

How the wellness tools read the daily Intervals.icu wellness records (`get_wellness_data`,
`get_recovery_snapshot`, `get_wellness_trends`, `get_nutrition_summary`): defaults, baselines,
z-scores, outliers, correlations, weight trend, calorie balance and the check of today's record.

All four tools are read-only and report numbers only: no readiness verdict, no diagnosis. A
wellness record is one day (its id is the date in the athlete's time zone); any value can be
missing and whole days can be absent. Missing values are never filled in or counted as 0.

## get_wellness_data

- `start_date`: first day, default 30 days before today (counted from today, not from
  `end_date`: with only a past `end_date`, pass `start_date` too).
- `end_date`: last day, default today (athlete's time zone).
- `include_all_fields=false` lists the standard sections as stored: training metrics (ctl, atl,
  rampRate, ctlLoad, atlLoad), sport-specific info (eFTP per sport), vital signs (weight,
  restingHR, hrv, hrvSDNN, avgSleepingHR, spO2, systolic/diastolic, respiration, bloodGlucose,
  lactate, vo2max, bodyFat, abdomen, baevskySI), sleep and recovery (sleep time, sleepQuality,
  sleepScore, readiness), menstrual tracking, subjective scores (soreness, fatigue, stress, mood,
  motivation, injury), nutrition and hydration (kcalConsumed, carbohydrates, protein, fatTotal,
  hydrationVolume, hydration), steps, comments and the locked status.
- `include_all_fields=true` adds "Other Fields": every further field of the record; custom
  wellness fields (custom items of type INPUT_FIELD) are labelled with the display name and units
  of the athlete's custom item definitions.
- One wellness request for the range. When the range includes today it is widened to the 14 days
  before today for the completeness check (those extra days are not listed). The custom item
  definitions are loaded (cached per athlete) for `include_all_fields` or when custom fields are
  missing today.
- Paging: days are added in date order until the answer would exceed the output budget of one
  tool result (below MCP_MAX_OUTPUT_CHARS, default 100000 characters; a year with all fields is
  about 1 MB). Then a note says "output stopped after N of M days (through D). Continue with
  start_date=D+1 (end_date=...)".
- Today's completeness: see "Today's record" below; at most 15 names per group, then "and N more
  (all: get_recovery_snapshot detail_level=full)".
- To change subjective scores or the comment use `update_wellness` (only on the athlete's request).

## get_recovery_snapshot

- `date_str`: the day, default today. `days_back` (0-14, default 3): how many days before it are
  listed too; a day without record says "no wellness record".
- `baseline_metrics`: comma-separated wellness field codes for the baseline block, default
  `hrv,restingHR,avgSleepingHR,sleepScore,readiness,respiration,spO2`; native or custom codes.
- `detail_level`:
  - `compact`: native values, fitness, subjective scores, missing values, baselines, activities
    and planned events; no custom field dump; at most 8 names per group in today's completeness
    line.
  - `standard` (default): plus up to 20 custom wellness fields per day ("+N more
    (detail_level=full)"); 15 names per group.
  - `full`: every custom field with a value and every missing name.
- `output_format=json`: `date`, `today_completeness`, `days` (per day: date, updated, locked,
  native, fitness, subjective, custom, missing, preliminary), `baselines` (the full trend
  statistics per metric without the day series), `activities` and `planned_events` (raw),
  `load_errors`.
- Per day: the record's last update time and locked flag; native values RHR (bpm), HRV (ms), HRV
  SDNN, sleeping HR, sleep (h), sleep score, readiness, SpO2 (%), respiration (/min), weight (kg),
  body fat (%), steps, kcal consumed, hydration (l); CTL, ATL, form (CTL - ATL) and ramp rate as
  stored by Intervals.icu; subjective scores (sleepQuality, soreness, fatigue, stress, mood,
  motivation, injury; 1-4 scales, 1 = best); custom wellness fields with a value (e.g. Body
  Battery, training readiness, sleep stages, respiration or skin temperature written by a sync
  bridge), formatted with their definition; and "missing": the native values above that the
  record does not have.
- Device composite scores (readiness, Body Battery ...) are derived values, not independent
  measurements.
- Baselines: a separate request of the 42 + days_back days ending at `date_str` with only the
  baseline fields. Per metric: the latest value (most recent day with a value in that window; its
  date is shown and may be before `date_str`), the trailing 7-day mean, and the baseline of the
  42 calendar days ending at the last day of the window (mean, median, sample SD and n, available
  values only); latest vs baseline as difference, percent of the baseline mean and z-score
  (difference / SD). "not enough data" when there is no latest value or fewer than 3 baseline
  values; fewer than 14 values are flagged as a small sample. A stored 0 of a physiological
  metric counts as missing (as in get_wellness_trends).
- Activities from `date_str - days_back` to `date_str`: local start, type, name, id, moving time,
  Intervals.icu load (with power/HR/pace loads), intensity (IF %), feel (1-5), RPE (1-10) and up
  to 12 numeric custom activity fields with a non-zero value ("device fields").
- Planned events of `date_str`: category and sport, name, event id, planned duration and load,
  paired activity.
- Today is tagged "(today, preliminary)" (JSON `preliminary=true`): its aggregates (steps,
  calories, burn) can still be incomplete. Today's completeness line: see "Today's record".
- Requests: wellness of the listed days (widened by 14 days for today), baseline wellness, custom
  item definitions (cached), activities, events. An activity or event request that fails is
  reported as "could not be loaded", never as "none".

## get_wellness_trends

- `start_date`: default 41 days before `end_date` (a 42-day period); `end_date`: default today.
- `metrics`: comma-separated, at most 16, default
  `hrv,restingHR,avgSleepingHR,sleepScore,readiness,respiration,spO2,weight`. Native codes such as
  hrv, hrvSDNN, restingHR, avgSleepingHR, respiration, spO2, sleepSecs, sleepScore, readiness,
  weight, bodyFat, vo2max, steps, kcalConsumed, ctl, atl, rampRate; any custom wellness field by
  its code; per sport from the wellness sportInfo `eftp_<Type>`, `wPrime_<Type>`, `pMax_<Type>`
  (e.g. `eftp_Ride`, `eftp_Run`).
- `windows`: rolling windows in days (2-365), default `7,14,42`.
- `correlations`: comma-separated pairs `a:b` or `a:b:lag_days`, e.g.
  `hrv:readiness,restingHR:sleepScore:1`.
- Units: HRV ms, resting/sleeping HR bpm, respiration breaths/min, SpO2 %, readiness and sleep
  score /100, weight kg, body fat %, sleepSecs s, VO2max ml/kg/min, eFTP and Pmax W, W' J; custom
  fields with the units of their definition (e.g. a temperature deviation in °C).
- Data: one wellness request from `start_date - 42 days` to `end_date` (lookback for the rolling
  windows and the baseline). The output states the requested period, the fetched range and the
  baseline window separately.
- Missing values: null, NaN and non-numbers are no value, a day without record is missing,
  nothing is interpolated. For hrv, hrvSDNN, restingHR, avgSleepingHR, respiration, spO2, weight,
  bodyFat, sleepSecs, sleepScore, readiness and vo2max a stored 0 is a placeholder: it counts as
  missing and is reported ("N stored 0 treated as missing"). For other fields 0 is a value.
- Per metric:
  - period: days, days with values, days missing (requested period only);
  - latest: the last day with a value in the period;
  - period statistics: mean, median, min, max, sample SD, p25, p75 (inclusive quartiles; needs 2
    values);
  - personal baseline: the 42 calendar days ending at `end_date` (lookback included): mean,
    median, SD and n; fewer than 14 values are flagged "small sample, not reliable";
  - latest vs baseline: difference, percent of the baseline mean (n/a for a mean of 0), z-score
    = difference / baseline SD;
  - rolling means at `end_date`: trailing mean of the available values of each window (calendar
    days, lookback included) with n;
  - last 7 days vs previous 7 days: means of the last 7 calendar days ending at `end_date` and of
    the 7 before, difference and percent;
  - outliers: days of the period whose z-score against the period mean and SD is at least 2.5 in
    absolute value; only with 7 or more values;
  - day table: the last 14 days of the period with the value and the 7-day trailing mean (the
    first window when 7 is not requested); JSON `series` has every day with all windows.
- Weight (when `weight` is among the metrics): over the requested period, windows of 7, 14 and 28
  days (the last N calendar days up to the last record): n, mean, first and last weight, change
  (needs 2 weighed days) and slope by least-squares regression in kg per week (needs 3).
- Correlations: over the requested period; `a` on a day is paired with `b` lag_days later
  (negative: earlier); only days where both have a value; Pearson r and Spearman rho from 10
  pairs on; fewer than 30 pairs are flagged as a small sample ("indicative only"). A correlation
  is a statistical association, not a cause.
- JSON: `start`, `end`, `windows` (requested, fetched, baseline), `metrics` (full results with
  `series`), `weight`, `correlations`.

## get_nutrition_summary

- `start_date`: default 27 days before `end_date` (a 28-day period); `end_date`: default today.
- `windows`: comma-separated windows in days (2-365), default `7,14,28`.
- `burn_field` (default `GarminTotalCalories`), `active_field` (`GarminActiveCalories`) and
  `balance_field` (`GarminKcalBalance`): codes of custom wellness fields with the device's total
  daily burn, active burn and computed balance (the defaults are the codes a Garmin sync bridge
  writes; the athlete's codes are listed by get_custom_items).
- `include_training_load` (default true): Intervals.icu training load per day (sum of the
  activities' load per local start day); text: the last 7 days with activities and the total of
  the range; JSON `training_load_per_day`. A failed activity request is reported.
- Days: the calendar from the first to the last day with a wellness record in the range; the
  windows are the last N days of it. The text lists the last 7 days, JSON every day.
- A day is logged when kcalConsumed is a number above 0. Unlogged days (also days without a
  record) are left out of every intake and balance statistic and counted as days without
  intake: an unlogged day is never 0 kcal.
- Per day: kcal consumed, carbohydrates, protein and fat (g), burn, active burn, balance and
  weight (a stored 0 weight is missing).
- Balance: the device balance field when present, otherwise kcal consumed minus burn on logged
  days. Window balance statistics use logged days only.
- Per window: days, logged days, days without intake, intake total and mean over logged days,
  carbohydrate/protein/fat means over logged days, burn mean (days with a burn value), balance
  total and mean over logged days, weight change (last minus first weighed day).
- Weight trend with the same windows: n, mean, first and last weight, change and least-squares
  slope in kg per week (as in get_wellness_trends).
- Caveats: the burn is a device estimate of the daily expenditure, not a measurement; a calorie
  balance is not a measurement of fat change; the most recent day can still be incomplete (device
  totals are updated during the day).

## Today's record (all wellness tools)

- Today's record (athlete's time zone) fills during the day: sleep, HRV and resting HR arrive
  after the morning sync, other device values (sleeping HR, respiration, SpO2, custom fields of a
  sync bridge ...) later. When a tool covers today it names the usual fields that today's record
  does not have yet, with the record's last update time (local).
- Usual field: a value on at least 80 % of the 14 days before today (12 of 14 days).
- No value: null, NaN, empty text, and a stored 0, except for signed fields (a negative value in
  those 14 days, or deviation, delta or change in the code, name or description, e.g. a skin
  temperature deviation of 0.0 °C), where 0 is a real value.
- Not checked: metadata (id, date, updated, locked ...) and the values Intervals.icu computes
  itself (ctl, atl, rampRate, ctlLoad, atlLoad, sportInfo, menstrualPhasePredicted ...).
- The missing fields are grouped by when they usually arrive (JSON `group`): `night_morning`
  (values of the night and the morning: sleep, HRV, resting HR, skin temperature, readiness ...)
  first, then `daily_metric` (device estimates computed once a day and delivered with a device
  sync: VO2max, Garmin Endurance Score, Hill Score / Strength / Endurance, Fitness Age, predicted
  race times, acute training load, daily goals ...) and `day_total` (steps, calories ..., normally complete in
  the evening) last; each group on its own line.
- Read such a field as "not yet available": not normal, not 0, not a change.
- JSON `today_completeness`: date, exists, updated, updated_local, reference_days, usual_fields,
  missing_usual_fields (field, name, group) and a note. Without enough history the note says that
  completeness is unknown.
