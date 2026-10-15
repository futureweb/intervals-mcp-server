# Method: workout execution

How `analyze_workout_execution` compares a planned workout with the executed activity (alignment
of planned steps with the actual intervals, per-step metrics, tolerances), and how the same plan
matching is used by `get_activity_intervals(include_planned_types=true)` and
`get_activity_report`.

## analyze_workout_execution
- Read-only; nothing is paired, split or changed on Intervals.icu. Every metric is computed from
  recorded samples only, time-weighted by the sample spacing; nothing is interpolated.
- **Plan source**, in this order: the given `planned_workout_doc` (a workout document with
  `steps`, same format as `add_or_update_event`, e.g. when the calendar event no longer exists),
  the given `event_id`, or the event paired with the activity (`paired_event_id`). The event is
  loaded with its targets resolved. The content of a deleted event is never reconstructed; pass
  `planned_workout_doc` instead. An event without workout steps, or one that cannot be loaded, is
  named and the intervals are analysed alone.
- **API requests**: the activity, its intervals, its streams (time, watts, heartrate, cadence,
  velocity_smooth, distance, altitude when present, plus every custom stream), the event when
  needed and, for suggestions, the WORKOUT events around the activity's day. Custom definitions
  and sport settings are cached. An API error is reported as INCOMPLETE DATA (retry later), never
  shown as missing data.
- **Plan expansion**: repeats are expanded; targets are resolved to W, bpm or pace from the
  activity's thresholds (FTP, LTHR, max HR, threshold pace, power/HR/pace zones). Open-ended targets
  (top zone, a %/W range given by its start only) are lower bounds, not point targets.
- **Work and rest steps**: steps whose target lies in the recovery zone (Z1) count as rest;
  without zone settings the default Intervals.icu Z1 tops are used (55 % FTP, 77.5 % of threshold
  speed, 80 % LTHR). A step up to 65 % FTP / 80 % threshold speed / 85 % LTHR that sits between two
  clearly harder steps (target midpoint at least 10 % higher) is a recovery between efforts and
  counts as rest (no length limit), e.g. 4 min at 60 % FTP between threshold efforts. Easy aerobic
  steps otherwise count as work.
- **Alignment**: an order-preserving sequence alignment (Needleman-Wunsch style) of the planned
  steps with the intervals Intervals.icu detected, by order, planned duration (or distance) and
  target intensity measured on the planned part of the span; interval names are never used. A step
  may match one interval or any number of consecutive intervals of about the same intensity (an
  effort split by laps, e.g. 1 km device auto-laps on a run, or by a stop); it absorbs a further
  interval only while the intervals after it do not yet cover its planned duration. Lap presses are
  kept as step boundaries. Extra intervals before the first or after the last planned step are
  cheap when short (the cost grows with their duration), so riding before or after the workout
  does not distort the pairing.
- **Device auto-laps** (most laps of one distance or duration: within ±3 % of the typical lap, at
  least 60 % of the time, at least 4 laps): a step boundary inside a lap is placed where the
  intensity changes; a step without a lap of its own between two matched steps is found at its two
  intensity changes; an overrun is reported as longer than planned. Step targets closer than 3 %
  are not told apart inside a lap. A boundary the samples cannot place is kept and the durations
  there are not judged. When one step runs longer than planned and the adjacent step is
  correspondingly short, the boundary is moved on the continuous timeline (time carried over) when
  the moved samples fit the receiving step at least as well, instead of reporting a pair of
  opposite false deviations.
- **Alignment confidence** (caveat line; JSON `alignment_confidence` with `alignment_notes`):
  `high` when the laps follow the plan (lap presses, a workout on the device, detected efforts);
  `medium` when step boundaries inside device auto-laps were placed at intensity changes; `low`
  when, with auto-laps, boundaries cannot be placed from the samples or planned steps were not
  found (shorter than an auto-lap or not executed).
- **Very many laps or long plans**: adjacent auto-laps are merged pairwise for the matching (at
  most 600 laps, fewer for long plans so that steps x laps stays below 30,000; never below 100 or
  twice the number of steps); boundaries are refined on the samples afterwards. When the alignment
  exceeds its 20 s run-time budget the plan comparison is skipped with a note and the intervals are
  shown without the plan.
- **Steps capped at their planned duration**: when the matched span is longer than the planned
  duration plus the tolerance, it is split logically (analysis only). The planned part is evaluated
  against the plan from the samples; the remainder of the last planned step plus everything after
  it is reported as **additional training after the plan** (e.g. a cool-down continued for the
  ride home, extra sprints) with its own metrics, kJ share, estimated load and the extra efforts it
  contains (intervals at or above 90 % FTP, up to 5 listed), and is not counted against the plan;
  it is reported separately when it lasts at least 120 s. The remainder of an earlier step is extra time inside the plan.
  Riding before the first step is reported the same way. Steps without a duration (distance) and
  lap-press steps restart the plan clock at their actual end.
- **Lap-press steps** (`until_lap_press`, "Press lap" in the workout text): the step ends when the
  athlete presses the lap button; its duration or distance is only the planning estimate. The press
  closes exactly one lap, so the step is that lap, preceded only by full device auto-laps. A full
  auto-lap is a lap of the auto-lap length, at most 0.5 % below the device setting: a lap closed by a
  press is usually shorter. Without a detected auto-lap layout, a lap of a usual auto-lap distance
  also counts (200 m to 20 km, or 0.1 to 10 miles, up to 1.5 % over), when two laps have it. A single
  such lap, or inside the plan any one lap, can be the step's first lap when it brings the length
  nearer to the estimate (e.g. a double press). Devices whose auto-laps keep fixed distance markers
  instead of restarting at a press are not handled well: see the known limitations below.
  - **Never across a press**: the step never reaches across another lap: a press, a step lap, the
    lap that starts the workout. Riding before the workout start and after the last press is
    reported as riding before the plan or additional training after it. With lap-press steps the
    riding before or after the plan costs its fixed part once per block, so a long ride home in
    many auto-laps is not moved into the plan. The first step takes all full auto-laps back to the
    workout start; a lap well off its target there counts as riding before the plan.
  - **Length**: in the alignment any length costs little; lengths nearer the estimate win ties.
  - **Ending on a full auto-lap** costs extra: much more when a shorter lap that could be the press
    follows through laps that fit the target (the press came later), less otherwise (a press at
    about the auto-lap length, or no press). Then the row says "ended at a full device auto-lap".
  - **Fixed boundaries**: the step's lap boundaries are never moved, split or extended afterwards.
  - **No length deviation**: it is never capped or reported as shorter/longer than planned.
  - **Row text**: "ended by lap after <actual> (planning estimate <estimate>)", in elapsed time,
    and with a recording pause also the moving time. "Ended with the recording" when it is the last
    step and the activity ends there.
  - **JSON `lap_press`**: `ended_by` (`lap`, `auto-lap` or `end of recording`), `elapsed_s`,
    `moving_time_s`, `estimate_s` or `estimate_m`, and `actual_m`.
  - **Metrics**: target adherence, time in target and the other metrics are evaluated as usual.
  - **Following steps**: they are judged from the actual lap (start shift and durations), so a
    longer or shorter lap-press step never shifts the timed steps after it.
  - **Plan total**: marked "≈" with the planning estimates it includes (JSON `lap_press_steps`,
    `lap_press_estimate_s`, `lap_press_estimate_m`).
  - **Known limitations** (lap boundaries are inferred from lap lengths; Intervals.icu does not
    expose the lap trigger):
    - Auto-laps that keep fixed distance markers instead of restarting at a press or step lap: a
      lap-press step can be cut at a marker, and its remainder goes to the next step or shows as
      additional training.
    - Workout not run on the device (auto-laps only, no press): the step ends at some auto-lap near
      a fitting length and the neighbouring timed steps can be off.
    - Two lap-press steps with an extra lap inside one of them (a double press, or a lap split by
      Intervals.icu): the steps can be split at the wrong lap. In the first or last step the extra
      lap is reported as riding before or after the plan.
    - Riding before the workout that ends exactly on an auto-lap at the first step's intensity: no
      lap marks the workout start, and the riding counts as part of the first step.
    - Very short auto-laps (about 200 m or less at riding speed, where laps overshoot their distance)
      or more than about 600 laps (merged pairwise for the matching, never across a lap that may
      close a lap-press step): boundaries can be off and a short step can be missed.
- **Per step**: planned vs actual duration on moving time (recording pauses excluded; distance
  steps on distance), the target range (resolved to W, bpm or pace), the actual average, below / in
  / above target with the offset from the exact range, time within the target range (±5 %), HR at
  start and end, the HR drop in the first minute after work steps, cadence, power/speed fade, Pw:HR
  drift for work steps of 10 min or more (Intervals.icu decoupling sign: positive = HR rose
  relative to power), the change of every custom stream (e.g. stamina; clock counters and
  other-sport streams are left out except in full) and notes on clear deviations (short, too long,
  off target, paused, shifted). The Intervals.icu interval type is kept and shown next to the
  planned step type when they differ.
- **Without a plan** the same metrics are reported per detected interval. For an unpaired activity
  without a plan (`suggest_matches`, default true) up to three planned WORKOUT events of the
  activity's day and the days before and after are suggested, scored by same sport (+0.4), planned
  vs actual moving time (ratio x 0.4), name similarity (x 0.2), another day (-0.2) and already
  paired with another activity (-0.3). Read-only: nothing is paired or changed.
- The header adds the Intervals.icu compliance, RPE, feel and load, and (not in compact) the
  device/custom fields assigned to the sport.
- Parameters:
  - `activity_id`: the activity.
  - `event_id`: planned workout (event) to compare against; default the paired event.
  - `planned_workout_doc`: workout document with `steps` to compare against.
  - `suggest_matches` (default true): candidate events for unpaired activities without a plan.
  - `output_format`: `text` or `json` (activity, event, plan_source, match_candidates,
    thresholds, custom_fields, summary, rows, extension, pre_plan, hidden_streams, load_errors).
  - `detail_level`: `compact` (one line per step plus the summary), `standard` (default) or `full`
    (also clock counter and other-sport custom streams).
  - `duration_tolerance_pct` (default 10): how much longer or shorter, in % of the step and at least
    30 s, a step may be before it is split (longer) or flagged as short; 0-100.
  - `start_tolerance_s` (default 120): steps starting more than this many seconds away from the plan
    timeline are flagged.
  - `pause_tolerance_s` (default 60): recording pauses inside a step up to this many seconds in
    total are not flagged; durations are always compared on moving time.

## Plan matching in get_activity_intervals (include_planned_types)
- The planned steps come from `planned_workout_doc` or the event paired with the activity (one
  extra request); they are expanded and resolved as above.
- The same alignment as `analyze_workout_execution`, but without streams: interval durations are
  the intervals' moving time (elapsed time when missing) and the default tolerances apply (10 %,
  at least 30 s). Each interval gets the planned step it belongs to; the Intervals.icu WORK /
  RECOVERY type is kept as stored and marked "type differs from the plan" when the planned step is
  work and the interval is not WORK, or the planned step is rest and the interval is not RECOVERY.
- A lap-press step is open: its intervals get `lap_press`, `estimate_s` / `estimate_m` and the
  text "until lap press, ended by lap after <actual> (planning estimate <estimate>)", never
  `beyond_plan_s`.
- A matched span (one or several consecutive intervals) longer than the planned duration plus the
  tolerance gets `beyond_plan_s` on each of its intervals, counted as in `analyze_workout_execution`:
  after the last matched step as additional training after the plan, earlier as extra time inside
  the plan. When the span ends with a device auto-lap that runs into the next step,
  `boundary_inside_auto_lap` says so (the samples would be needed to place the boundary; use
  `analyze_workout_execution`). Intervals without a planned step are marked as before the plan,
  after the plan (additional training) or extra inside the plan; JSON `plan_position` is
  `planned_step`, `before_plan`, `after_plan`, `extra_inside_plan` or `no_plan`.

## Plan in get_activity_report
- The report runs the same analysis as `analyze_workout_execution` on the streams it loaded once,
  with the same tolerance parameters. Plan source: `planned_workout_doc`, else the event paired
  with the activity (there is no `event_id` parameter). Without intervals there is no execution
  section; without a plan the intervals are listed instead.
- Compact key findings summarise the plan: steps executed of planned, work steps vs the planned
  targets in the unit of the targets (power, HR or pace; clearly different targets, e.g. an easy
  block and strides, listed separately), work steps within ±5 % and inside the exact range, the
  mean time in target, steps with clear deviations, and the additional training after the plan
  (duration, work share, extra efforts). Lap-press steps are named with their actual length and
  planning estimate (length set by the lap press, not judged; their targets are judged). When the
  plan comparison was skipped, the finding says so instead of "0/0 steps".
