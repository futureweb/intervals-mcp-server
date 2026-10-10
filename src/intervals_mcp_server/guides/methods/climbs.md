# Method: climbs

How `analyze_climbs` (and the climb section of `get_activity_report`) splits an activity into
climbs, descents, sections in between and pauses from its streams, and how each segment is
evaluated.

## analyze_climbs
- For activities without defined intervals (tours, mountain hikes, trail runs) or whenever the
  terrain rather than the laps structures the effort. Read-only; 2 API requests (the activity and
  all its streams; custom stream definitions cached). Thresholds are explicit parameters, nothing
  is interpolated, and device sitting/standing or similar classifications are not used.
- **Data**: the time, altitude (the corrected `fixed_altitude` when available, else `altitude`),
  distance, speed, power, HR and cadence streams plus every custom stream. Without time or altitude
  there is no segmentation. When Intervals.icu returns no streams although the activity lists
  stream types, the file was not retained (e.g. a duplicate upload): look with `get_activities`
  for another activity with the same start time.
- **Detection**: the altitude profile is smoothed with a centred moving average against
  GPS/barometer noise, then walked with a hysteresis of 8 m: a rising run continues while the
  altitude has not dropped more than 8 m below its running maximum and ends at that maximum;
  falling runs are the mirror image. A rising run is a climb when its gain reaches
  `min_climb_gain_m` and its average grade `min_grade_pct`; a falling run is a descent when its
  loss reaches `min_descent_loss_m` and its (negative) grade the minimum grade. With a distance
  stream the ends of a run are trimmed to where the grade over the grade window reaches the
  minimum grade, so a noisy flat approach is not part of a climb. Sections between climbs and
  descents ("other") are reported when longer than 120 s.
- **Sport defaults** (`sport` = the activity type; explicit parameters override them):

  | Profile | Types | Stationary speed | Smoothing | Grade window | Min grade distance | Plausible grade | Raw grade window |
  |---|---|---|---|---|---|---|---|
  | foot | Run, TrailRun, VirtualRun, Walk, Hike, Snowshoe, BackcountrySki | 0.3 m/s | 15 samples | 50 m | 30 m | 60 % | 10 m |
  | bike | types containing "Ride", Velomobile, Handcycle | 0.5 m/s | 9 samples | 100 m | 50 m | 25 % | 20 m |
  | default | everything else | 0.5 m/s | 9 samples | 100 m | 50 m | 40 % | 20 m |

  The raw grade window is never shorter than the minimum grade distance.
- **Per segment**: start/end time and sample indices (end exclusive; a recording stop inside a
  segment counts towards its duration, one right after its last sample does not), duration and
  moving time, distance, elevation gain/loss, average grade and the steepest grade over the grade
  window (smoothed; with `show_raw_grade` also the grade of the unsmoothed altitude), VAM and speed
  on moving time (recording stops and real pauses excluded), average and normalized power (30 s
  rolling window on the time stream, at least 60 s of power data), max power, average/max HR,
  cadence (non-zero samples; steps per minute on foot), and the start/end/min/max/mean/delta of
  every custom stream (clock counters and other-sport streams left out) plus the standard streams
  of `extra_stream_types`.
- **Grade quality**: no grade is computed over tiny horizontal distances: below
  `min_grade_distance_m` the grade, raw grade included, is "not determinable" (GPS distance on
  slow, steep terrain is too short and would turn into extreme percentages). Grades above the
  plausible limit of the sport are flagged as data-quality problems and excluded from the steepest
  climb. Every segment gets a grade confidence with its reasons, counted in the summary: `low` for
  no distance stream, a distance below the minimum, an average grade above the plausible limit or
  GPS speed near zero while moving for more than 25 % of the time; `medium` for a horizontal
  distance below 3 x the minimum, a steepest window above the plausible limit, a GPS speed below
  2 x the stationary speed, or pauses / recording stops above 25 % of the segment time; otherwise
  `high`.
- **Pauses**: stretches below the stationary speed are pause candidates, classified in chunks of
  about 120 s. Real pauses (no vertical or horizontal progress, little stepping) and recording
  stops (time jumps > 5 s) count as pause time; very slow movement with vertical progress (at
  least 100 m/h and 3 m), horizontal progress of at least 0.15 m/s or a stepping share of at least
  60 % (scrambling, steep climbing with GPS speed near zero) is kept as moving time. Pauses
  shorter than `pause_min_secs` are not reported; the text lists up to 20. A device moving-time
  counter stream is shown for comparison but not treated as truth.
- The sum of the climbs' gain is not the activity's total elevation gain (Intervals.icu computes
  that separately from the whole profile; the header shows the Intervals.icu totals).
- Parameters:
  - `min_climb_gain_m` (default 30): minimum elevation gain of a climb, metres.
  - `min_descent_loss_m` (default 30): minimum elevation loss of a descent, metres.
  - `min_grade_pct` (default 2): minimum average grade of climbs and descents, percent.
  - `pause_min_secs` (default 60): minimum length of a reported stationary period, seconds.
  - `extra_stream_types`: comma-separated standard stream types evaluated per segment in addition
    to the custom streams, e.g. "temp,respiration,left_right_balance".
  - `max_segments` (default 40): segments printed in text output (JSON has all).
  - `output_format`: `text` or `json` (activity with Intervals.icu totals, thresholds, result with
    altitude source, segments, pauses, slow movement, summary, settings and hidden streams).
  - `show_raw_grade` (default false): also the grade from the unsmoothed altitude over short
    windows of at least `min_grade_distance_m`.
  - `grade_window_m`: distance window of the steepest grade; default 50 m for foot sports, 100 m
    otherwise.
  - `min_grade_distance_m`: no grade below this horizontal distance; default 30 m for foot sports,
    50 m otherwise.
  - `stationary_speed_m_s`: speed below which a stretch is a pause candidate; default 0.3 m/s for
    foot sports, 0.5 m/s otherwise.

## Climbs in get_activity_report
- The report runs the same detection with the sport defaults and the default thresholds on the
  streams it already loaded, by default only when the activity has no intervals or more than 500 m
  of elevation gain (`include_climbs` forces or suppresses it). It lists the climb/descent counts,
  the gain inside climbs, pause time and slow movement kept as moving, and the 6 largest climbs by
  gain (all in full) with time, gain, distance, grade, VAM, power, NP, HR and quality flags.
