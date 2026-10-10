# Workout syntax

How to describe a planned workout for `add_or_update_event`, `add_events_bulk`,
`create_library_workout`, `validate_workout`, `preview_workout`, and as `planned_workout_doc` for
the analysis tools. Check every workout with `validate_workout` (or `preview_workout`) and show the
result to the athlete before writing it.

## workout_doc

```json
{
  "description": "High-intensity workout for increasing VO2 max",
  "steps": [
    {"power": {"value": 80, "units": "%ftp"}, "duration": 900, "warmup": true},
    {"reps": 2, "text": "Intervals", "steps": [
      {"power": {"value": 110, "units": "%ftp"}, "distance": 500, "text": "Hard"},
      {"power": {"value": 80, "units": "%ftp"}, "duration": 90, "text": "Recovery"}
    ]},
    {"power": {"value": 80, "units": "%ftp"}, "duration": 600, "cooldown": true}
  ]
}
```

- `steps` (list, required for a structured workout) and an optional `description` (shown above
  the steps; its lines must not look like steps, repeats or "Warmup"/"Cooldown" headers).
- A workout_doc without timed steps (only a description or only text steps) is a text-only
  workout (strength, yoga: an exercise list). On an update it never silently replaces a
  structured (timed) workout: that needs `replace_workout=true`.
- An empty workout_doc (`{}` or no steps) is ignored on writes: it never clears a planned workout.

## Step properties

- `duration`: whole seconds (positive), e.g. `{"duration": 1800}`.
- `distance`: metres (positive), e.g. `{"distance": 5000}`. Give `duration` OR `distance`, never
  both (the distance would be dropped).
- Every step needs a duration or a distance, except a pure text/comment step (`{"text": "..."}`
  without a target). Open-ended steps (until lap press, free ride without duration) cannot be
  written as Intervals.icu workout text and are refused.
- `warmup: true` / `cooldown: true` on single steps (not on repeat blocks) mark warm-up and
  cool-down; a missing warm-up or cool-down is a warning.
- Targets: one of `power`, `hr`, `pace` (and optionally `cadence`), each an object with `units`
  and either `value` or a range `start` + `end` (never both):
  - Percentage of FTP: `{"power": {"value": 80, "units": "%ftp"}}`
  - Absolute power: `{"power": {"value": 200, "units": "w"}}`
  - Power zone: `{"power": {"value": 2, "units": "power_zone"}}`; % of MMP: `%mmp`
  - Heart rate: `{"hr": {"value": 75, "units": "%hr"}}` (% of max HR),
    `{"hr": {"value": 85, "units": "%lthr"}}`, zone `{"hr": {"value": 2, "units": "hr_zone"}}`
  - Pace: % of threshold pace `{"pace": {"value": 80, "units": "%pace"}}`, zone
    `{"pace": {"value": 2, "units": "pace_zone"}}`
  - Absolute pace: `MINS_KM` / `MINS_MILE` in seconds (335) or decimal minutes (5.583) per km/mile,
    never m.ss (5.35 means 5.35 minutes = 5:21/km, not 5:35); `SECS_100M`, `SECS_100Y`, `SECS_500M`
    in seconds:
    `{"pace": {"value": 335, "units": "MINS_KM"}}` -> "5:35/km Pace"
  - Cadence: `{"cadence": {"value": 90, "units": "rpm"}}` (`cadence` is accepted as units too)
  - Units must fit the kind (an HR target in `%ftp` is refused). Plausible ranges are checked
    (e.g. %ftp 20-250, w 0-2500, %hr 30-110, %lthr 30-130, zones 1-8, cadence 20-140; absolute
    paces 2:20-40:00/km, 0:50-6:00/100m). Pace targets on rides, power targets on runs and
    swim/row pace units on runs are warnings.
- Ranges: `{"power": {"start": 80, "end": 90, "units": "%ftp"}}`.
- Ramps (gradual change, e.g. for ERG): `{"ramp": true, "power": {"start": 80, "end": 90, "units": "%ftp"}}`
  (a ramp needs a start/end range).
- Repeats: `reps` with nested `steps` (no nested repeats; a repeat block has no duration,
  distance or target of its own):
  `{"reps": 3, "steps": [{"power": {"value": 110, "units": "%ftp"}, "distance": 500, "text": "Hard"}, {"power": {"value": 80, "units": "%ftp"}, "duration": 90, "text": "Recovery"}]}`
- Free ride (no ERG control, needs a duration, optional suggested power):
  `{"freeride": true, "duration": 1200, "power": {"value": 80, "units": "%ftp"}}`
- Max effort: `{"maxeffort": true, "duration": 30}`.
- Labels: `{"text": "Warmup"}`. A step's text becomes the cue at the start of its line, as in
  Intervals.icu's builder: `{"text": "Sprint", "distance": 40, "hr": {"value": 5, "units": "hr_zone"}}`
  -> "- Sprint 40mtr Z5 HR". Labels must be plain words: durations (2m, 30s), distances (400mtr),
  % values, watts, rpm/bpm, zones (Z2), repeat counts (3x), m:ss, line breaks and the keywords
  ramp / freeride / max effort / hidepower are refused, because Intervals.icu would read them as
  part of the step.
- Optional on the doc: `target` (AUTO, POWER, HR, PACE: which target the device follows).

## Native workout text (`description`)

Instead of a workout_doc, `description` may hold plain text ("Squats 5x5, Deadlifts 3x5") or native
Intervals.icu workout text, sent as-is, e.g.:

```
Warmup
- 10m 55%
Main set 3x
- 10m 90%
- 5m 55%
Cooldown
- 10m 50%
```

It is not validated by this server; prefer a workout_doc for structured workouts.

## add_or_update_event

- Creating needs a `name` (not blank); the date defaults to today (athlete's time zone), the
  category to WORKOUT. Workouts and races need a sport: pass `workout_type` (Ride, Run, Swim, Walk,
  Row, WeightTraining ...) unless the name names exactly one sport ("Easy run" -> Run).
- Categories: WORKOUT, RACE_A, RACE_B, RACE_C, NOTE, HOLIDAY, SICK, INJURED (notes and
  holiday/sick/injured days have no sport). Weekly TARGET events are not offered.
- Updates (`event_id` given) are partial: only the parameters you pass change; moving an event to
  another day keeps its time of day. `workout_doc` REPLACES the planned workout; `description`
  replaces the event text (an empty string is ignored, never clears it).
- The workout is validated first (same checks as `validate_workout`): with errors nothing is
  written; warnings are listed in the answer. A new event is refused when that day already has
  the same event (`allow_duplicate=true` creates it anyway); `dry_run=true` returns the exact
  request without writing. After the write the answer reports what Intervals.icu stored and
  parsed (steps parsed vs sent, parse warnings); details: write safety in `intervals://guide`.

## add_events_bulk entries

A list of at most 100 objects, all validated first (if ANY entry is invalid nothing is sent and all
errors are returned); keys that do not apply to an entry's category are rejected:

- `name` (required, not blank), `start_date` (required, YYYY-MM-DD), `category` "WORKOUT"
  (default) or "NOTE".
- WORKOUT: `workout_type` (REQUIRED here, not inferred), `workout_doc` (as above) OR
  `description` (native text), `moving_time` (s, >= 0), `distance` (m, >= 0).
- NOTE: `description` (required), `color` (colour name, default green).

```json
[
  {"name": "Easy run", "start_date": "2025-01-06", "workout_type": "Run", "moving_time": 2700},
  {"name": "Rest day", "start_date": "2025-01-07", "category": "NOTE", "description": "Full rest"}
]
```

Before the bulk request the events of the dates are read: an entry whose day already has the same
event (same category and sport with the same name or the same non-trivial workout), or that
repeats an earlier entry exactly, is refused and not written (`allow_duplicate=true` skips the
check); two entries with the same day, sport and name but different content are both created. The
answer is compact JSON with "created" (input index, status, event id, date, name and short warnings
from the read-back; `detail_level="full"` adds what was stored), "refused" (index, the existing
event or the earlier entry), "errors" (index and all problems per invalid entry) and
"created_count". If the request itself fails, an error string is returned and events may have been
created. `dry_run=true` returns the bulk request that would be sent.

## create_library_workout

Same `workout_doc` format and checks; the steps are rendered into the library workout's text
(`description` is used when the workout_doc has no steps). A folder or plan is needed (by name,
case-insensitive, or id; optional when the library has exactly one folder).
