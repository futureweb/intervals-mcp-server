# Method: intensity

How get_intensity_distribution turns the time in zones of each activity into the three-zone
model, the polarization index, the distribution class and hard sessions/days (the same method
gives the intensity part of get_coach_context).

## get_intensity_distribution

Read-only, one API call (the activity list with a field selection). Statistics of the recorded
zone times only; no assessment. The metric set and the edge cases of the polarization index follow
the coach metrics proposed by morritter in upstream pull request mvilanova/intervals-mcp-server#150;
the zone mapping here depends on the number of zones of the athlete's zone model.

Parameters:

- `start_date`: first day YYYY-MM-DD; default 27 days before `end_date` (4 weeks). The period must
  not exceed 366 days and the start must not lie after the end.
- `end_date`: last day YYYY-MM-DD; default today.
- `zone_basis`: `auto` (default), `power`, `hr` or `pace` (case-insensitive). `auto` uses power
  for cycling and heart rate, then pace, then power for every other sport, falling back to the
  next basis with usable data. A forced basis uses only that one; activities without it are left
  out (and counted in the coverage).
- `sport_types`: comma-separated activity types to include, e.g. "Ride,GravelRide"
  (case-insensitive exact types); default all.
- `threshold_as`: how power zone Z4 (91-105 % FTP, threshold work) counts: `moderate` (default,
  three-zone Z2) or `high` (three-zone Z3). Only power models with 6 or 7 zones are affected.
- `athlete_id`, `output_format` (`text` or `json`).
- `detail_level`:
  - `compact`: the period (shares, hours, PI, class, hard sessions/days), zone bases and coverage,
    the hard-session rule, every sport family, the drift between the halves;
  - `standard` (default): plus the ISO weeks, the zone mapping, the class rules, the hard-session
    rule with its source and the polarization index source;
  - `full`: plus every session (minutes in Z1/Z2/Z3, basis, zone count, IF, why it is hard, or
    why it has no zones). JSON contains the sessions only at `full`; it always includes the zone
    mapping, the class rules and the references.

Three-zone model:

- Z1 below the first lactate/ventilatory threshold, Z2 between the thresholds, Z3 above the
  second (Seiler & Kjerland 2006, Scand J Med Sci Sports 16:49-56). Threshold work (power Z4,
  91-105 % FTP) is middle-zone work by default.
- Zone times come from what Intervals.icu stores per activity: power `icu_zone_times` (entries
  Z1..Zn with seconds; the sweet-spot bucket SS overlaps Z3/Z4 and is ignored), HR
  `icu_hr_zone_times`, pace `pace_zone_times`, or the grade-adjusted `gap_zone_times` when the
  activity says Intervals.icu shows them (`use_gap_zone_times`, as raw pace understates the
  effort on hilly runs). An activity whose zone times are missing or add up to 0 has no zones for
  that basis.
- Mapping of the athlete's zones to Z1 | Z2 | Z3 by basis and number of zones:
  - power, 7 zones (Coggan, % of FTP): Z1-Z2 (<= 75 %) | Z3-Z4 (76-105 %) | Z5-Z7 (> 105 %);
    with `threshold_as="high"`: Z1-Z2 | Z3 | Z4-Z7;
  - power, 6 zones: Z1-Z2 | Z3-Z4 | Z5-Z6; with `high`: Z1-Z2 | Z3 | Z4-Z6;
  - HR, 7 zones (Intervals.icu / Friel, % of LTHR): Z1-Z2 (< ~90 %) | Z3-Z4 (90-99 %) |
    Z5a-Z5c (>= LTHR);
  - pace, 7 zones (% of threshold pace): Z1-Z2 | Z3-Z4 | Z5a-Z5c;
  - any basis, 5 zones (e.g. %HRmax or Seiler's five zones): Z1-Z2 | Z3 | Z4-Z5;
  - any basis, 3 zones: taken as they are.
  - Other zone counts have no documented mapping: such activities are left out and reported
    ("<basis> zone model with N zones has no three-zone mapping").
- Missing zone data is never counted as easy time.

Statistics per block (period, sport family, ISO week, half):

- Z1/Z2/Z3 shares of the summed zone time (%), hours in total and per zone, sessions with zones,
  and the share of the time per zone basis.
- Polarization index after Treff et al. 2019 (Front Physiol 10:707): PI = log10(Z1 / Z2 x Z3 x
  100) with zone fractions. Undefined with no zone time, with Z3 below 1 % (the log is undefined
  for Z3 = 0 and such a distribution is not polarized) and with Z1 = 0. Z2 = 0 is replaced by
  0.01, as proposed by Treff et al., and noted.
- Class (rules of #150): Base: Z3 < 1 % and Z1 >= Z2; Polarized: Z1 > Z3 > Z2 and PI > 2.0;
  Pyramidal: Z1 > Z2 > Z3; Threshold: Z2 largest; HIT: Z3 largest; any other order (e.g.
  Z1 > Z3 > Z2 with PI <= 2.0) is reported as Pyramidal. The zone order is shown, e.g.
  "Z1 > Z3 > Z2" (ties with "=").
- Hard session: at least 10 min in three-zone Z3, or an intensity factor of at least 0.85 on a
  session of at least 20 min moving time (IF from Intervals.icu's `icu_intensity` in percent).
  Source: IF bands after Allen & Coggan (Training and Racing with a Power Meter): above 0.85 is
  beyond endurance-paced training. Hard days = days with at least one hard session. Sessions
  with neither zone data nor IF are unclassified and counted.
- Per sport family (ordered by moving time) also its own coverage.
- ISO weeks are clipped to the period (partial weeks show their number of days).
- Drift: the first half of the period (the first days // 2 days) against the second half: change
  of each zone share in percentage points, PI change and whether the class changed. Not available
  for periods shorter than 2 days or when one half has no zone data.
- Coverage: sessions, sessions with usable zones, moving hours, moving hours with zones and their
  share, excluded sessions by reason.
- When a total combines different zone bases (power for rides, HR or pace for other sports) a
  caveat says so: the thresholds are not the same, compare the per-sport split.
