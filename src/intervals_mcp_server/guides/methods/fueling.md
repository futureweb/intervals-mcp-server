# Method: fueling

How get_fueling_analysis reads carbohydrate, energy, fluid, sodium and sweat values of one
activity or of the long sessions of a period, and which statistics it gives. No intake targets or
prescriptions.

## get_fueling_analysis

Read-only. Two modes:

- One activity (`activity_id` given): one request for the activity and, only when a custom
  stream carries intake over time, one request for that stream. The custom item definitions come
  from the per-process cache (the activity's own athlete first, else `athlete_id` or the
  configured athlete).
- A period (no `activity_id`): one request for the activity list with a field selection.

Parameters:

- `activity_id`: one activity, e.g. i123456789; omit for the period mode. With it, `start_date`,
  `end_date`, `sport_types` and `min_minutes` are ignored.
- `start_date`: first day YYYY-MM-DD (period mode); default 89 days before `end_date` (90 days).
  The period must not exceed 366 days.
- `end_date`: last day YYYY-MM-DD (period mode); default today.
- `sport_types`: comma-separated activity types (period mode), e.g. "Ride,GravelRide"
  (case-insensitive exact types); default all.
- `min_minutes`: minimum moving time in minutes for a session to count (period mode; default 90,
  0 = every session; not negative).
- `athlete_id`, `output_format` (`text` or `json`).
- `detail_level`:
  - one activity: `compact` leaves out the line naming the fueling custom fields that do not
    exist and the notes; `standard` and `full` show them. JSON always has everything;
  - period: `compact` = the overall line (with more than one sport family) and one line per sport
    family; `standard` (default) adds the duration and intensity buckets, the correlations and the
    15 most recent sessions; `full` lists every session. JSON leaves out the session rows only at
    `compact`.

Values:

- `carbs_used`: Intervals.icu's estimate of the carbohydrate burnt, not a measurement.
- `carbs_ingested`: what the athlete logged, one total per activity without timestamps. Null =
  not logged; a stored 0 stays 0 and is counted separately ("0 g stored").
- Ingested share of used = ingested / used x 100. Used minus ingested is not an energy deficit
  1:1, because muscle and liver glycogen, fat oxidation and food eaten before the session
  contribute.
- Energy: `calories` (kcal, from the device or Intervals.icu) and `icu_joules` (mechanical work
  from power, shown in kJ).
- Fluid intake, sodium and sweat loss exist only as custom activity fields. They are found
  generically by the units and the words of the definition's name and code (nothing is tied to a
  vendor):
  - sweat loss: a volume unit (ml, l, cl, dl, fl oz, oz) and a word sweat/perspiration, or
    loss/lost/deficit together with fluid/hydration/water;
  - sodium: a word sodium/salt/electrolyte(s) and the unit mg or g;
  - fluid intake: a volume unit and a word such as intake, ingested, consumed, drank, drink,
    drunk, fluid, bottle, water, hydration, beverage;
  - carbohydrate custom fields (carb, carbohydrate, cho, gel with the unit g) are named as present
    but not used in the figures.
  Amounts are converted to ml or mg. Fluid minus sweat loss (ml) is given when both exist.
- A 0 in a custom field filled from the device file (FIT source or script, e.g. a Garmin sweat
  loss) is a zero placeholder: Intervals.icu stores 0 when the file lacks the source, so it is
  reported as "0 stored" and left out of totals, differences and statistics. A 0 in a manual field
  stays 0.
- Rates are per moving hour (elapsed time when there is no moving time).
- Missing values are "not logged", never 0.

One activity:

- The figures above with g/h, ml/h and mg/h, the IF (Intervals.icu intensity) and the weather
  temperature (JSON).
- Intake over time: when the activity has a custom stream whose definition names carbohydrate or
  fluid intake, the intake per hour of the session (hour 1, 2, ...), read as cumulative values
  (never decreasing) or as single intake events, as detected. Otherwise the note says that
  Intervals.icu stores intake as one total without timestamps, so its distribution over the
  session cannot be shown.

Period:

- Sessions of at least `min_minutes` moving time, newest first, with their figures.
- Overall and per sport family: sessions, intake logged / 0 g stored / not logged, sessions with
  carbs used, device-file zero placeholders left out, and the distribution (n, median, range,
  interquartile range from 4 values) of ingested g/h (sessions with a logged value above 0 only),
  used g/h, ingested % of used, sweat loss ml/h and fluid ml/h.
- Per sport family also by moving time (< 2 h, 2-3 h, 3-4 h, >= 4 h) and by intensity factor
  (IF < 0.65, 0.65-0.75, 0.75-0.85, >= 0.85; Intervals.icu intensity, from power on rides and from
  HR or pace elsewhere), with sample sizes.
- Spearman rank correlation (ties get the mean rank) of ingested g/h with duration and with IF,
  per sport family, only from 8 sessions with intake logged on; association only, not causation.
- Buckets and correlations never mix sport families.
