# Futureweb Intervals MCP usage guide

Which tool for which question, the recommended call order and the conventions of all tools.
Method details: `intervals://methods/<topic>` or `get_guide(topic)`; workout format:
`intervals://workout-syntax`.

## 1. Orientation
- `get_server_status`: version, enabled permission classes, tool set (MCP_TOOLSET), hidden
  tools, API check, custom item counts.
- With `MCP_TOOLSET=core` only the core tools exist; tools outside the set are marked
  "(full tool set)" in descriptions, prompts and guides.
- For a weekly analysis the recommended first call is `get_coach_context` (load, intensity,
  recovery, durability, plan and method in about 2-3k characters). Go to the detail tools below
  only where needed, and ask for `detail_level="compact"` first.

## 2. One activity
- `get_activity_report` first (overview, plan vs execution, power meter check, climbs, fueling,
  weather, W′ balance, data-quality notes in one call). `include_route_history=true` adds earlier
  activities on the same Intervals.icu route.
- Then only where needed: `get_activity_details`, `get_activity_intervals(detail_level="compact")`,
  `analyze_workout_execution` (method: execution), `get_best_efforts`, `analyze_climbs`
  (method: climbs), `compare_power_streams` (method: power-meters), `get_fueling_analysis`
  (method: fueling), `get_activity_data_audit` (source, sensors, streams and fields present;
  method: activity-data).
- Streams: `list_activity_streams` first, then `get_activity_streams` with
  `output_format="full"`, slicing and `downsample` (method: activity-data).

## 3. Recovery and wellness
- `get_recovery_snapshot` (one day with baselines, load, activities and plan), `get_wellness_data`
  (raw records), `get_wellness_trends` (rolling means, baselines, outliers, correlations),
  `get_nutrition_summary` (method: wellness).
- Today's wellness record is filled during the day: sleep, HRV and resting HR arrive after the
  morning sync, values from a sync bridge (sleeping HR, respiration, SpO2, skin temperature,
  readiness ...) later. The wellness, recovery and coach tools name the usual fields that are not
  yet in today's record (JSON `today_completeness`), split into night/morning values, daily
  metrics (device estimates computed once a day, e.g. VO2max, endurance or hill score) and day
  totals (steps, calories). Such a value is "not yet available": never read it as normal, as 0
  or as a change.

## 4. Periods, load and intensity
- `get_coach_context` first, then `get_training_load` (ACWR, monotony, strain, deload-like weeks),
  `get_intensity_distribution` (three zones, polarization index, hard days),
  `get_durability` (decoupling, efficiency factor) - methods: load, intensity, durability.
- `get_load_projection`: CTL/ATL/form over the planned workouts. `scenario` simulates a what-if
  plan that is never written to the calendar; `target_date` (default the next A race) and
  `target_form` give the form on race day and a taper search (method: load).
- Totals and plan: `get_training_summary` (week, month, sport, gear; custom field aggregation:
  method summary), `get_weekly_summary`, `get_plan_compliance`, `get_training_plan`.

## 5. Performance and fatigue
- Curves: `get_athlete_power_curves`, `get_hr_curves`, `get_pace_curves`;
  `compare_best_efforts`, `compare_workouts`, `find_similar_intervals`,
  `get_power_hr_efficiency`, `get_fatigue_resistance` (method: comparisons).
- `get_long_ride_fatigue_profile`: HR, W/bpm, cadence and stamina at matched power before and
  after work thresholds (kJ or kJ/kg) on long rides; `get_submax_test_trends`: Intervals.icu
  submaximal fatigue tests (#SFT), their validity and trend (method: fatigue).
- `compare_power_streams` compares two power meters on one ride, or without `activity_id`
  (or with `activity_ids`) several rides per bike and power meter (method: power-meters).

## 6. Planning and writes
- Read: `get_sport_settings` / `get_training_zones`, `get_events`, `get_training_plan`,
  `get_workout_library`, `get_library_workout`.
- Before writing a workout read `intervals://workout-syntax`, then `preview_workout` /
  `validate_workout`, show the result and write with `add_or_update_event` only after the
  athlete confirmed. Writes need MCP_PERMISSIONS=write and the athlete's explicit request;
  deletions preview first (`delete_events_by_date_range` with `dry_run=true`).

## Conventions
- Dates are YYYY-MM-DD in the athlete's time zone; `athlete_id` defaults to the configured athlete.
- Times are local (time zone name when stored, else the UTC offset) and UTC.
- Run/walk/hike cadence in steps per minute (spm = 2 x the stored per-leg value, shown as
  stored), bike cadence in rpm; temperatures in °C.
- "no value" = null/NaN; a 0 in a device-file field may mean the source field was absent;
  missing values are never counted as 0.
- The Intervals.icu load is never mixed with device loads (custom fields such as a Garmin training
  load are summed on their own scale).
- Custom fields and streams come from the athlete's own definitions (`intervals://custom-items`).
- Most tools accept `output_format="json"` and `detail_level` (`compact`, `standard`, `full`).
- Statistics only: reference ranges are context with their sources, never a verdict or diagnosis.
