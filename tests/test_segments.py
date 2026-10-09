"""
Unit tests for intervals_mcp_server.utils.segments.

A synthetic 1 Hz ride (flat start, climb, recording stop, descent, stationary pause,
small bump, flat tail, with null/NaN holes and custom streams) is built
programmatically and the detected segments, pauses, summary and text report are
checked against it.
"""

import json
import random
from collections.abc import Callable
from typing import Any

from intervals_mcp_server.utils.segments import (
    SEGMENT_KEYS,
    detect_segments,
    format_segments,
    segments_to_json,
)

STREAM_DEFS = {
    "Stamina": {"code": "Stamina", "name": "Garmin Stamina", "units": "point", "description": "Stamina"},
}

BASE_ALTITUDE = 500.0
FLAT_SECS = 300  # idx 0-299, time 0-299
CLIMB_SECS = 600  # idx 300-899, time 300-899: +200 m over 3 km
CLIMB_GAIN_M = 200.0
CLIMB_SPEED = 5.0
GAP_SECS = 120  # recording stop between idx 899 (t=899) and idx 900 (t=1019)
DESCENT_SECS = 400  # idx 900-1299: -200 m over 4 km
DESCENT_SPEED = 10.0
STATIONARY_SECS = 200  # idx 1300-1499, velocity 0
BUMP_SECS = 60  # idx 1500-1559 up, 1560-1619 down
TAIL_SECS = 200  # idx 1620-1819
TOTAL_SAMPLES = FLAT_SECS + CLIMB_SECS + DESCENT_SECS + STATIONARY_SECS + 2 * BUMP_SECS + TAIL_SECS
CLIMB_START, CLIMB_END = FLAT_SECS, FLAT_SECS + CLIMB_SECS
DESCENT_END = CLIMB_END + DESCENT_SECS
STATIONARY_START, STATIONARY_END = DESCENT_END, DESCENT_END + STATIONARY_SECS

Value = int | float | Callable[[int], int | float]


def _value(spec: Value, position: int) -> int | float:
    """Constant or per-sample value of a phase."""
    return spec(position) if callable(spec) else spec


class _Ride:
    """Builds sample-aligned 1 Hz streams phase by phase."""

    def __init__(self) -> None:
        names = ("time", "altitude", "distance", "velocity_smooth", "watts", "heartrate", "cadence")
        self.columns: dict[str, list[Any]] = {name: [] for name in names}
        self._time = 0
        self._altitude = BASE_ALTITUDE
        self._distance = 0.0

    def add(  # pylint: disable=too-many-arguments
        self,
        seconds: int,
        *,
        climb_m: float = 0.0,
        speed: float = 8.0,
        watts: Value = 180,
        hr: int = 120,
        cadence: Value = 80,
    ) -> None:
        """Append ``seconds`` samples with a constant altitude rate and speed."""
        rate = climb_m / seconds
        for position in range(seconds):
            row = (
                self._time, self._altitude, self._distance, speed,
                _value(watts, position), hr, _value(cadence, position),
            )
            for name, value in zip(self.columns, row, strict=True):
                self.columns[name].append(value)
            self._time += 1
            self._altitude += rate
            self._distance += speed

    def gap(self, seconds: int) -> None:
        """Recording stop: the time jump between the last and the next sample becomes ``seconds``."""
        self._time += seconds - 1

    def streams(self, altitude_type: str = "altitude") -> list[dict[str, Any]]:
        """Streams in API shape plus two custom streams (Stamina falling 100 -> 0, RearGear)."""
        count = len(self.columns["time"])
        streams: list[dict[str, Any]] = [
            {"type": altitude_type if name == "altitude" else name, "data": list(values)}
            for name, values in self.columns.items()
        ]
        streams.append(
            {
                "type": "Stamina",
                "name": "Garmin Stamina",
                "custom": True,
                "data": [100 - 100 * index / (count - 1) for index in range(count)],
            }
        )
        streams.append(
            {"type": "RearGear", "custom": True, "data": [17 if index % 50 else None for index in range(count)]}
        )
        return streams


def _variable_power(position: int) -> int:
    return 150 if (position // 60) % 2 == 0 else 350


def _cadence_with_zeros(position: int) -> int:
    return 0 if position % 10 == 0 else 70


def _build_ride(bump_m: float = 5.0) -> _Ride:
    ride = _Ride()
    ride.add(FLAT_SECS, speed=8.0, watts=180, hr=120, cadence=80)
    ride.add(
        CLIMB_SECS, climb_m=CLIMB_GAIN_M, speed=CLIMB_SPEED,
        watts=_variable_power, hr=150, cadence=_cadence_with_zeros,
    )
    ride.gap(GAP_SECS)
    ride.add(DESCENT_SECS, climb_m=-CLIMB_GAIN_M, speed=DESCENT_SPEED, watts=100, hr=130, cadence=0)
    ride.add(STATIONARY_SECS, speed=0.0, watts=0, hr=100, cadence=0)
    ride.add(BUMP_SECS, climb_m=bump_m, speed=5.0)
    ride.add(BUMP_SECS, climb_m=-bump_m, speed=5.0)
    ride.add(TAIL_SECS, speed=8.0)
    return ride


def _stream(streams: list[dict[str, Any]], stream_type: str) -> list[Any]:
    return next(stream for stream in streams if stream["type"] == stream_type)["data"]


def _ride_streams(altitude_type: str = "altitude", bump_m: float = 5.0) -> list[dict[str, Any]]:
    """The synthetic ride with null, NaN and "NaN" holes punched into a few streams."""
    streams = _build_ride(bump_m).streams(altitude_type)
    altitude = _stream(streams, altitude_type)
    altitude[10], altitude[20], altitude[30], altitude[600] = None, float("nan"), "NaN", None
    watts = _stream(streams, "watts")
    watts[400], watts[401] = None, float("nan")
    _stream(streams, "heartrate")[5] = "NaN"
    return streams


def _segments_of(result: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [segment for segment in result["segments"] if segment["type"] == kind]


def test_detects_the_climb_with_expected_metrics():
    """
    The 200 m / 3 km climb is found once, with gain, grade, VAM, speed, power
    (NP above average for the alternating 150/350 W blocks), HR and non-zero cadence.
    """
    result = detect_segments(_ride_streams())
    assert "error" not in result
    assert result["altitude_source"] == "altitude"
    assert result["samples"] == TOTAL_SAMPLES
    climbs = _segments_of(result, "climb")
    assert len(climbs) == 1
    climb = climbs[0]
    assert tuple(climb) == SEGMENT_KEYS
    assert abs(climb["start_index"] - CLIMB_START) <= 5
    assert abs(climb["end_index"] - CLIMB_END) <= 5
    assert climb["start_time"] == climb["start_index"]  # 1 Hz, no gap before the climb
    assert climb["end_time"] == climb["end_index"] - 1
    assert climb["duration_s"] == climb["end_time"] - climb["start_time"]
    assert abs(climb["elevation_gain_m"] - CLIMB_GAIN_M) <= 5
    assert climb["elevation_loss_m"] <= 1
    assert abs(climb["distance_m"] - 3000) <= 50
    assert 6.3 <= climb["avg_grade_pct"] <= 6.8
    assert 6.0 <= climb["max_grade_pct"] <= 7.5
    assert 1150 <= climb["vam_m_per_h"] <= 1250
    assert 4.8 <= climb["avg_speed_m_s"] <= 5.1
    assert 240 <= climb["avg_watts"] <= 252
    assert climb["max_watts"] == 350
    assert climb["normalized_power"] > climb["avg_watts"] + 20
    assert 148 <= climb["avg_hr"] <= 150
    assert climb["max_hr"] == 150
    assert climb["avg_cadence_nonzero"] == 70


def test_descent_other_sections_and_ignored_bump():
    """
    The descent is found, the 5 m bump is not a climb, the flat start and the tail
    are "other" sections, and the segments are sorted and do not overlap.
    """
    result = detect_segments(_ride_streams())
    segments = result["segments"]
    assert [segment["type"] for segment in segments] == ["other", "climb", "descent", "other"]
    descent = segments[2]
    assert abs(descent["start_index"] - CLIMB_END) <= 5
    assert abs(descent["end_index"] - DESCENT_END) <= 6
    assert abs(descent["elevation_loss_m"] - CLIMB_GAIN_M) <= 5
    assert -5.3 <= descent["avg_grade_pct"] <= -4.7
    assert descent["max_grade_pct"] <= -4.5
    assert descent["vam_m_per_h"] is None
    assert descent["avg_cadence_nonzero"] is None  # cadence is 0 throughout
    assert descent["avg_watts"] == 100
    for previous, following in zip(segments, segments[1:], strict=False):
        assert previous["end_index"] <= following["start_index"]
    assert segments[0]["start_index"] == 0
    assert segments[-1]["end_index"] == TOTAL_SAMPLES
    tail = segments[-1]
    assert tail["start_index"] <= STATIONARY_END
    assert tail["elevation_gain_m"] <= 6
    # A 15 m bump exceeds the hysteresis but not the minimum gain: still no extra segment.
    bigger = detect_segments(_ride_streams(bump_m=15.0))
    assert bigger["summary"]["climbs"] == 1
    assert bigger["summary"]["descents"] == 1


def test_pauses_and_summary():
    """
    The 120 s recording stop and the 200 s stationary run are reported with their
    positions and durations; the summary counts and totals match the segments.
    """
    result = detect_segments(_ride_streams())
    pauses = result["pauses"]
    assert [(p["kind"], p["start_index"], p["end_index"], p["duration_s"]) for p in pauses] == [
        ("recording_stop", CLIMB_END - 1, CLIMB_END, GAP_SECS),
        ("stationary", STATIONARY_START, STATIONARY_END, STATIONARY_SECS - 1),
    ]
    assert pauses[0]["start_time"] == CLIMB_END - 1
    assert pauses[0]["end_time"] == CLIMB_END - 1 + GAP_SECS
    summary = result["summary"]
    climb = _segments_of(result, "climb")[0]
    assert summary["climbs"] == 1
    assert summary["descents"] == 1
    assert summary["total_climb_gain_m"] == climb["elevation_gain_m"]
    assert summary["total_descent_loss_m"] == _segments_of(result, "descent")[0]["elevation_loss_m"]
    assert summary["pause_time_s"] == GAP_SECS + STATIONARY_SECS - 1
    assert set(summary["longest_climb"]) == {
        "start_index", "gain_m", "distance_m", "avg_grade_pct", "vam_m_per_h",
    }
    assert summary["longest_climb"]["start_index"] == climb["start_index"]
    assert summary["steepest_climb"]["vam_m_per_h"] == climb["vam_m_per_h"]


def test_custom_and_extra_stream_statistics():
    """
    Custom streams always get per-segment range statistics; extra stream types are
    added when present and silently skipped when absent.
    """
    result = detect_segments(_ride_streams(), extra_stream_types=("heartrate", "not_there"))
    climb = _segments_of(result, "climb")[0]
    assert set(climb["streams"]) == {"Stamina", "RearGear", "heartrate"}
    stamina = climb["streams"]["Stamina"]
    assert stamina["samples"] == climb["end_index"] - climb["start_index"]
    assert stamina["first"] > stamina["last"]
    assert stamina["delta"] < 0
    gear = climb["streams"]["RearGear"]
    assert gear["non_null"] < gear["samples"]
    assert gear["min"] == gear["max"] == 17
    assert climb["streams"]["heartrate"]["max"] == 150
    default = detect_segments(_ride_streams())
    assert set(_segments_of(default, "climb")[0]["streams"]) == {"Stamina", "RearGear"}


def test_fixed_altitude_is_preferred_when_it_has_data():
    """
    A numeric fixed_altitude stream wins over altitude; one without numeric data
    falls back to altitude.
    """
    streams = _ride_streams()
    flat = {"type": "fixed_altitude", "data": [BASE_ALTITUDE] * TOTAL_SAMPLES}
    result = detect_segments([*streams, flat])
    assert result["altitude_source"] == "fixed_altitude"
    assert result["summary"]["climbs"] == 0
    assert result["summary"]["descents"] == 0
    assert [segment["type"] for segment in result["segments"]] == ["other"]
    empty = {"type": "fixed_altitude", "data": [None, "NaN", float("nan")] * (TOTAL_SAMPLES // 3)}
    fallback = detect_segments([*streams, empty])
    assert fallback["altitude_source"] == "altitude"
    assert fallback["summary"]["climbs"] == 1


def test_missing_streams_give_an_error_dict():
    """
    Without altitude or time an error dict names the missing stream; nothing raises.
    """
    streams = _ride_streams()
    no_altitude = detect_segments([s for s in streams if s["type"] != "altitude"])
    assert set(no_altitude) == {"error"}
    assert "altitude" in no_altitude["error"]
    no_time = detect_segments([s for s in streams if s["type"] != "time"])
    assert "time" in no_time["error"]
    nothing = detect_segments([])
    assert "time" in nothing["error"]
    assert "altitude" in nothing["error"]
    assert format_segments(no_altitude).startswith("Segment detection not possible")


def test_noisy_profile_is_still_one_climb_and_one_descent():
    """
    Barometric noise of +-2 m does not split the climb or the descent.
    """
    streams = _ride_streams()
    altitude = _stream(streams, "altitude")
    noise = random.Random(7)
    for index, value in enumerate(altitude):
        if isinstance(value, float):
            altitude[index] = value + noise.uniform(-2.0, 2.0)
    result = detect_segments(streams)
    assert result["summary"]["climbs"] == 1
    assert result["summary"]["descents"] == 1
    assert abs(_segments_of(result, "climb")[0]["elevation_gain_m"] - CLIMB_GAIN_M) <= 5


def test_format_segments_report():
    """
    The report carries the header, the key numbers of each segment, labelled custom
    stream statistics, the pauses and honours max_segments.
    """
    result = detect_segments(_ride_streams())
    text = format_segments(result, STREAM_DEFS)
    climb = _segments_of(result, "climb")[0]
    assert text.startswith(f"Segments (altitude from altitude, {TOTAL_SAMPLES} samples, 4 listed): 1 climbs")
    assert f"Longest climb: idx {climb['start_index']}, +{climb['elevation_gain_m']} m" in text
    assert "[2] Climb " in text
    assert f"(idx {climb['start_index']}-{climb['end_index']}): +{climb['elevation_gain_m']} m" in text
    assert f"avg grade {climb['avg_grade_pct']} %" in text
    assert f"VAM {climb['vam_m_per_h']} m/h" in text
    assert f"NP {climb['normalized_power']} W, max 350 W" in text
    assert "HR avg 150, max 150 bpm; cadence 70 rpm; speed 18." in text
    assert "  Garmin Stamina [Stamina] (point): start " in text
    assert "  RearGear: start 17" in text
    assert "[3] Descent" in text
    assert "cadence n/a rpm" in text
    assert "Pauses (2, 319 s total):" in text
    assert "  recording stop 14:59-16:59 (idx 899-900): 120 s" in text
    assert "  stationary 23:39-26:58 (idx 1300-1500): 199 s" in text
    short = format_segments(result, STREAM_DEFS, max_segments=1)
    assert "[2]" not in short
    assert "... 3 more segments not shown" in short


def test_format_segments_writes_missing_values_as_na_and_long_times():
    """
    None values render as n/a and times beyond an hour as h:mm:ss.
    """
    segment = dict.fromkeys(SEGMENT_KEYS)
    segment.update(
        type="other", start_index=0, end_index=2, start_time=3723, end_time=7322, duration_s=3599,
        streams={},
    )
    result = {
        "altitude_source": "altitude", "samples": 2, "segments": [segment], "pauses": [],
        "summary": {"climbs": 0, "descents": 0, "pause_time_s": 0},
    }
    text = format_segments(result)
    assert "[1] Other 1:02:03-2:02:02 (idx 0-2): +n/a m / -n/a m over n/a, avg grade n/a %" in text
    assert "power avg n/a W, NP n/a W, max n/a W; HR avg n/a, max n/a bpm; cadence n/a rpm; speed n/a" in text
    assert "VAM" not in text
    assert text.endswith("Pauses: none detected")


def test_segments_to_json_is_serialisable():
    """
    The JSON view is a deep copy without NaN or infinity.
    """
    result = detect_segments(_ride_streams())
    payload = segments_to_json(result)
    assert payload is not result
    dumped = json.dumps(payload, allow_nan=False)
    assert json.loads(dumped)["summary"]["climbs"] == 1
    assert segments_to_json({"x": float("nan"), "y": [1.0, float("inf"), (2, 3)]}) == {
        "x": None, "y": [1.0, None, [2, 3]],
    }
