# Garmin Intervals Bridge integration

The Futureweb Intervals MCP reads **only** from Intervals.icu. It never talks to Garmin
Connect. What makes it useful for Garmin athletes is that Intervals.icu can hold far more
than the standard fields: custom activity fields, custom streams, custom interval fields and
custom wellness fields. The [Garmin Intervals Bridge](https://github.com/futureweb/garmin-intervals-bridge)
fills those custom items with the metrics Garmin strips from the files it sends to partners,
and this MCP exposes everything that is there, with the names, units and codes you defined.

The bridge is **optional**. Without it the MCP is a complete Intervals.icu MCP server; with
it the same tools automatically show the additional data.

## How the pieces fit together

```
Garmin device ──► Garmin Connect ──► official sync ──► Intervals.icu activity (filtered FIT)
                        │                                     ▲
                        └── Garmin Intervals Bridge ──────────┘  adds custom fields/streams
                                                              │
                                              Futureweb Intervals MCP (read-only by default)
                                                              │
                                                     ChatGPT / Claude / any MCP client
```

- The normal Garmin → Intervals.icu sync stays switched on. Nothing is duplicated.
- The bridge finds the activity the official sync created and writes the missing values into
  the custom activity fields and streams **you** defined (it reads your definitions, it does not
  invent fields). Daily wellness values go to native wellness fields first and to private
  `Garmin…` custom wellness fields for the rest.
- The MCP resolves every custom item against `/athlete/{id}/custom-item` at run time: a field
  shows up with its display name, technical code, value and units as soon as it exists in the
  account. No FIT upload, no manual export, no device-specific code in the MCP.

## What the MCP shows when the bridge is active

| Data | Where it lives in Intervals.icu | MCP tool |
| --- | --- | --- |
| Training Effect (aerobic/anaerobic), EPOC, training load, recovery time, VO₂max, performance condition, stamina at start/end/minimum, sweat loss, temperatures, execution score … | custom activity fields (keys of the activity payload) | `get_activity_details` (section *Custom Activity Fields*), JSON via `output_format="json"` |
| Stamina, potential stamina, grade-adjusted speed, gear selection, battery, running dynamics streams … | custom streams | `list_activity_streams`, `get_activity_streams`, `get_activity_intervals(stream_types="custom")`, `analyze_climbs`, `analyze_workout_execution` |
| Custom interval fields | keys of each interval | `get_activity_intervals` |
| Sleeping HR, respiration, SpO₂, HRV detail, Body Battery, sleep stages, sleep stress, skin temperature, training readiness, endurance/hill scores, race predictions, calories … | custom wellness fields (`INPUT_FIELD`) | `get_wellness_data(include_all_fields=True)`, `get_recovery_snapshot`, `get_wellness_trends`, `get_nutrition_summary` |

Values are reported exactly as stored. `null`/`NaN` is "no value"; a `0` in a field filled
from the device file is listed under *Zero values in device-file fields* because Intervals.icu
stores `0` for both a real zero and an absent source field. Garmin loads (e.g. a custom
*Training Load* field) are always kept apart from the Intervals.icu training load.

### Units

The MCP uses the units of your custom item definitions. For definitions without units or with
unspecific ones (community items often say `point`), set display units per code:

```
CUSTOM_UNITS_OVERRIDES=Stamina=%,PotentialStamina=%,RecoveryTime=h
```

Overridden units are marked `units_source: "override"` in JSON output.

## Worked examples

All examples use placeholder IDs; outputs are abbreviated. Nothing here requires an API key
of its own: the tools use the key configured for the server.

### 1. Road bike with power and a second power meter

```
get_activity_details(activity_id="i000000001")
```
```
Thresholds used for this activity (snapshot stored with the activity):
- Power: FTP 234 W (icu_ftp, setting at the time), eFTP 227 W (icu_rolling_ftp, Intervals.icu estimate)
- Power/device source: device GARMIN EDGE_1040, power meter SHIMANO FC-R9200P, serial 1234, power fields power, Power2

Custom Activity Fields:
- Aerobic Effect [AerobicEffect]: 3.5
- Training Load [TrainingLoad]: 129.6
- Performance Condition [PerformanceCondition]: -1
- Stamina at end [Staminaatend]: 78 %
```
```
compare_power_streams(activity_id="i000000001")            # watts vs secondary_power
```
```
Primary 'watts' vs secondary 'secondary_power'
Device data: device GARMIN EDGE_1040, power meter ...; power fields in file: power, Power2
Overall (n 4210 valid pairs): mean 179.3 W vs 174.4 W, diff -4.9 W (-2.7 %), ratio 0.973
Bins: 100-150 W: ... | 150-200 W: ... | 200-250 W: ...
Stable 30 s windows: 55 windows, mean diff -2.6 %
Lag: best 0 s | Drift per quarter: -2.4 / -2.8 / -2.7 / -2.9 %
```
The comparison never calibrates anything; which physical sensor is which comes from the
device data in the header, not from assumptions.

### 2. Stamina across the intervals of a structured session

```
get_activity_intervals(activity_id="i000000001", stream_types="Stamina,PotentialStamina,secondary_power")
```
```
[2] Interval 2 (WORK)
Duration: 751 seconds ...
Stream Metrics (samples 1022-1742):
  Garmin Stamina [Stamina] (%): start 96, end 88, min 87, max 96, mean 91, delta -8, samples 721/721
  Garmin Potential Stamina [PotentialStamina] (%): start 96, end 89, ...
  Power2 [secondary_power] (W): start 257, end 216, mean 213.8, ...
```
Groups (e.g. 3 × 12 min) are evaluated over all member intervals, so the stamina drop of the
whole block is one line. For sample-level work use
`get_activity_streams(..., stream_types="time,watts,heartrate,Stamina", output_format="full", start_index=1022, end_index=1742)`.

### 3. Training effect, recovery time, VO₂max and sweat loss over time

```
get_training_summary(start_date="2026-09-01", end_date="2026-09-30", group_by="week")
```
Each week lists the Intervals.icu load split (power / HR / pace) and, separately, the numeric
custom fields aggregated as their definition prescribes (`SUM` for a load, `MAX` for a
training effect, mean otherwise):

```
Custom fields (e.g. device loads, kept separate from Intervals.icu load):
  Training Load [TrainingLoad] sum 512.3 (n 5); Aerobic Effect [AerobicEffect] max 4.1 (n 5);
  Recovery Time [RecoveryTime] max 38.5 h (n 5); Sweat loss [Sweatloss] sum 2890 ml (n 5)
```

### 4. Running dynamics

```
get_activity_details(activity_id="i000000002")      # a run
```
```
Running Dynamics:
Pace: 7:24/km (2.250 m/s)
GAP (grade adjusted pace): 7:20/km (2.271 m/s)
Cadence: 73.6 rpm as stored (x2 = 147 steps/min)
Ground contact time: 312.7 ms
Vertical oscillation: 85.9 mm
Vertical ratio: 9.28 %
Step length: 937.8 mm
```
`get_activity_intervals` prints the same block per interval; `list_activity_streams` shows
the dynamics streams (standard `stance_time`, `vertical_oscillation`, `step_length` and any
custom ones such as `GarminGCT`), and `analyze_climbs(..., extra_stream_types="stance_time,step_length")`
evaluates them per uphill/downhill segment.

### 5. Wellness with sleeping HR, respiration, HRV and sleep stress

```
get_recovery_snapshot(days_back=3)
```
```
2026-10-09 (today, preliminary) | updated 2026-10-09T12:07:56Z | locked no
  RHR 50 bpm | HRV 41 ms | sleeping HR 47 bpm | sleep 7.4 h | sleep score 89 | readiness 92 | SpO2 96 % | respiration 15.5 /min
  CTL 64.3 | ATL 68.3 | form -4 | ramp 3.88
  custom: Garmin Deep Sleep 85 min; Garmin REM Sleep 121 min; Garmin Sleep Stress Avg 21; Garmin HRV 7-day Average 37 ms; Garmin Skin Temperature Deviation -0.3 °C; ...
  missing: weight, kcal consumed
Baselines (last 42 days ...):
  hrv: latest 41 (2026-10-09) | 7d mean 40.3 | 42d baseline mean 39.1, median 39, sd 4.2 (n 41) | vs baseline +1.9 (+4.9%, z +0.45)
```
```
get_wellness_trends(metrics="hrv,restingHR,GarminSleepStressAvg,GarminSleepRespirationAvg", correlations="hrv:readiness")
```
Trends report rolling means, baselines, outliers and week-over-week changes; correlations are
labelled as statistical associations, never as causes. The MCP does not compute a readiness
verdict: the interpretation stays with the coach.

## Without the bridge

Everything above still works with the standard Intervals.icu data: the custom sections are
simply empty or shorter, `list_activity_streams` shows the standard streams, and the wellness
tools use the native fields. Athletes with other devices or other sync tools that write
custom items get the same treatment, because the MCP reads the definitions, not the vendor.
