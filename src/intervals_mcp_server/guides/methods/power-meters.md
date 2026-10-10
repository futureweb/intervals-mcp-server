# Method: power meters

How `compare_power_streams` compares two power streams recorded on the same ride, on one ride
sample by sample or across several rides per bike and power meter, and which conventions and
exclusions apply. The second-power-meter check of `get_activity_report` uses the same comparison.

## compare_power_streams
- Read-only: no calibration, scaling or smoothing is applied to either stream, nothing is written
  and no correction factor is ever derived; the numbers describe the recorded streams.
- **Streams**: a ride can carry two power streams: `watts` from the power meter paired as the
  primary sensor of the head unit and a second one. Intervals.icu stores a second power meter as
  stream type `secondary_power` (shown as "Power2" in `power_field_names`); a custom stream can be
  compared as well (`primary` / `secondary` take any stream type). The two sensors measure at
  different points of the drive train and have their own calibration. Which physical sensor each
  stream belongs to must be taken from the device data shown in the header (device, power meter
  name and serial, primary power field, power fields in the file), not assumed.
- **Alignment**: the streams are sample-aligned (index `i` of both belongs to the same recorded
  sample at `time[i]`). Recording pauses appear as jumps in `time`, so every window-based statistic
  walks the time stream and only uses windows that are contiguous in time (no gap larger than 2 s
  between consecutive samples).

### One ride (`activity_id`)
- API requests: the activity and one streams request (time, primary, secondary). When a stream
  is missing the answer lists the activity's streams.
- `start_index` / `end_index` (end exclusive, as in `get_activity_intervals`) restrict the
  comparison to a sample range; they apply to one activity only.
- **Exclusions**: samples where either stream is missing (null, NaN) or below 10 W (coasting,
  zeros, sensor drop to 0) are excluded; paired samples whose absolute difference exceeds 50 % of
  the primary are counted as outliers and excluded. The counts of each kind are reported.
- **Conventions**: differences are secondary - primary; percentages are relative to the primary.
  `mean_diff_pct` of a group of samples is the difference of the group means relative to the
  primary mean, i.e. `(ratio - 1) x 100`; `median_diff_pct` and `stdev_diff_pct` are over the
  per-sample percentages.
- **Reported**:
  - overall offset in W and % with the ratio secondary/primary;
  - the offset per power band of the primary, `[lo, hi)`: 50-100, 100-150, 150-200, 200-250,
    250-300, 300-400 W;
  - stable windows only: non-overlapping 30 s and 60 s windows without time gaps whose primary
    coefficient of variation is at most 10 % and of which at least 90 % of the samples are usable
    pairs; their window means are compared (robust against a lag);
  - drift: the mean difference per quarter of the elapsed time (a quarter needs at least 30 usable
    pairs), to make a changing offset visible;
  - lag: the shift within ±5 s that maximises the correlation of both streams (at least 60 pairs);
    a positive `best_lag_s` means the secondary lags behind the primary (secondary at t + lag
    matches primary at t); ties go to the smaller lag. All other statistics are computed as
    recorded (lag 0); a non-zero lag widens the per-sample spread and skews the bins, the stable
    windows and best efforts are robust;
  - best efforts of each stream for 5 s, 30 s, 1 min, 5 min, 20 min and 60 min: the highest
    rolling mean over contiguous windows, found independently for each stream (the windows may
    differ); `same_window_diff_pct` compares both streams in the primary's window;
  - the number of samples used and excluded (missing, coasting/zero, outliers).
- `output_format=json`: activity with start times, primary, secondary, device fields and the full
  comparison.

### Several rides (no `activity_id`, or `activity_ids`)
- **Rides**: with `activity_ids` the given activities (comma-separated; duplicates ignored, cut
  to `limit` before any request; the answer names ids beyond the limit); otherwise the activities
  of the date range that carry the second stream (listed in their stream types; for
  `secondary_power` also more than one power field in the file), newest first. `start_date`
  default 179 days before `end_date` (180 days), `end_date` default today. `limit` (default 10,
  1-20): at most this many rides are analysed; older matching rides are counted as not analysed.
- API requests: the activity list with a field selection (or one request per id), the gear list,
  and one streams request per ride.
- **Each ride is compared on its own** (as above); a ride enters the between-ride statistics with
  at least 600 usable pairs (10 min at 1 Hz), otherwise it is listed as excluded with the reason.
- **Groups**: rides are grouped by bike (gear), indoor/outdoor (trainer or virtual), primary power
  meter identity and second power source; groups are not pooled. The primary meter is named from
  the file's device data (power meter name and serial), else from the active PowerMeter components
  of the bike in the gear list, else "not identified" (the rides of such a group may come from
  different meters). The second source is known only by its field name in the file; the activity
  data does not say which device recorded it (pedals, trainer, another crank), so groups assume the
  same device per bike, setting and field.
- **Per group** (each ride counts once): n, mean, median, standard deviation between rides and
  range of the per-ride mean difference (overall, per power bin with at least 60 pairs, stable
  windows), the within-ride per-sample spread, the drift (last minus first quarter, percentage
  points), the outlier share and the lag (rides per best lag, median). Rides further than
  max(3 pp, 3 x MAD) from the group median are listed; fewer than 3 rides in a group are flagged
  as a small sample.
- `detail_level`: `compact` (between-ride summary only), `standard` (default, plus one line per
  ride: date, name, gear, setting, meters, pairs, mean primary W, diff mean/median/sd, stable
  windows, drift, lag, outliers) or `full` (plus the power bins and device data per ride; JSON adds
  each ride's full comparison).
- `output_format=json`: mode, source, primary, secondary, limit, rides not analysed beyond the
  limit, ids beyond the limit, duplicate ids ignored, rides and the between-ride summary.

## Second power meter check in get_activity_report
- Runs the one-ride comparison of `watts` vs `secondary_power` on the streams the report loaded,
  only with at least 300 valid paired samples (both above 0) before and after the exclusions;
  when 99 % or more of the pairs are equal both streams come from the same sensor and are reported
  as identical, never compared. Reports valid pairs, mean difference % and ratio, the stable 60 s
  windows (count and difference) and the lag.
