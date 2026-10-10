# Method: durability

How get_durability selects steady sessions and summarises Intervals.icu's aerobic decoupling and
efficiency factor (the same method, with the default filter, gives the durability part of
get_coach_context).

## get_durability

Read-only, one API call (the activity list with a field selection). Statistics only, no verdict:
heat, hydration, fatigue, terrain and pacing all affect decoupling. The quality filter, the 5 %
threshold and the recent-versus-window comparison with a stability band follow the coach metrics
proposed by morritter in upstream pull request mvilanova/intervals-mcp-server#150. Interval-level
watts per heartbeat per power band and per bike is a different view: get_power_hr_efficiency.

Parameters:

- `start_date`: first day YYYY-MM-DD; default 41 days before `end_date` (6 weeks). The period must
  not exceed 366 days.
- `end_date`: last day YYYY-MM-DD; default today.
- `sport_types`: comma-separated activity types, e.g. "Ride,VirtualRide" (case-insensitive exact
  types); default every cycling and running type. With a filter, the sport families of the given
  types are analysed (cycling and running when none of them belongs to a known family).
- `min_minutes`: minimum moving time in minutes (default 60, above 0).
- `max_vi`: maximum variability index (normalized / average power; default 1.20, at least 1).
- `max_temp_c`: maximum average temperature in °C (default 25); null = no limit. A missing
  temperature (typical indoors) passes.
- `drift_threshold_pct`: decoupling in % above which sessions are counted (default 5, above 0).
- `recent_days`: recent window compared with the whole period (default 7, at least 1).
- `environment`: `indoor` (trainer rides and Virtual* activity types) or `outdoor`; default both.
- `temperature_source`: which temperature `max_temp_c` checks: `device` (default; the device
  sensor's average, which reads body and sun heat as well), `weather` (the weather Intervals.icu
  attaches to outdoor activities along the track) or `feels_like` (its feels-like temperature).
  The session lists show the device and weather temperatures (plus feels-like when it is used).
- `athlete_id`, `output_format` (`text` or `json`).
- `detail_level`:
  - `compact`: the filter, counts of considered and excluded sessions by reason, and per sport
    family the decoupling summary and the efficiency factor;
  - `standard` (default): plus the qualifying sessions (date, type, name, id, decoupling,
    minutes, variability index, efficiency factor, temperatures, indoor) and the reference with
    the stability bands;
  - `full`: plus the excluded sessions with their reason.
  - JSON follows the same levels: session lists only at standard and full, excluded sessions only
    at full.

Values used (computed by Intervals.icu per activity):

- Aerobic decoupling (`decoupling`): the drift of power:HR (or pace:HR for runs without power)
  between the first and the second half of the session, in %. Negative values (HR drifting down)
  are kept. Each session reports its basis (power:HR when it has a variability index, else
  pace:HR).
- Efficiency factor (`icu_efficiency_factor`): normalized power / average HR.

Quality filter for the decoupling (the first failed criterion is the session's exclusion reason):

1. `short`: moving time below `min_minutes`;
2. `pauses`: moving time below 85 % of the elapsed time (long stops);
3. `environment`: excluded by the indoor/outdoor filter;
4. `heat`: average temperature (per `temperature_source`) above `max_temp_c`;
5. `no_hr`: no average heart rate;
6. `no_power`: a ride without power (no variability index); runs without power skip the
   variability check (pace:HR);
7. `variable`: variability index above `max_vi` (not steady);
8. `no_decoupling`: no decoupling value from Intervals.icu.

Per sport family (cycling, running, or the families of `sport_types`):

- Decoupling of the qualifying sessions: n, median, mean, range, quartiles (from 4 values,
  inclusive method), the count and share above `drift_threshold_pct`, the number of indoor
  sessions and the sessions per basis.
- Reference: aerobic decoupling below 5 % over a steady long session is commonly read as good
  aerobic endurance (Friel, The Cyclist's Training Bible; Allen & Coggan, Training and Racing with
  a Power Meter).
- Sample: sessions considered and the qualifying share (e.g. 6 of 19); fewer than 8 qualifying
  sessions are flagged as a small sample, not reliable; a mix among the qualifying sessions is
  pointed out (indoor and outdoor, several bikes/shoes = gear ids, several power meters).
- Recent versus window: the median of the last `recent_days` against the median of the whole
  period, difference in percentage points, direction "lower" / "higher" outside a +-1 pp
  stability band, else "within band". Needs at least 2 recent and 3 window values and a recent
  window shorter than the period; otherwise only the recent n is shown.
- Efficiency factor: sessions of the family with at least 20 min moving, heart rate and power
  with a variability index up to `max_vi` (indoor/outdoor filter applied; no temperature or pause
  check). Window mean and n, the mean of the last `recent_days` and n, the change in % with
  direction outside a +-2 % band (needs 2 recent and 3 window values), small sample below 8. EF
  depends on the power meter: with several gear ids the text suggests comparing per gear with
  get_power_hr_efficiency.
