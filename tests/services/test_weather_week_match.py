"""Weather week comparisons use clock hours, observed samples and existing history."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.roommind.services.weather_week_match import (
    find_weather_week_match,
    read_weather_week_match,
)
from custom_components.roommind.utils.history_store import HistoryStore

MONDAY = datetime(2026, 10, 26, tzinfo=UTC)


def _hour_rows(start, temperatures):
    return [
        {"timestamp": start.timestamp() + hour * 3600 + minute * 60, "outdoor_temp": temperature}
        for hour, temperature in enumerate(temperatures)
        if temperature is not None
        for minute in range(0, 60, 5)
    ]


def test_closest_profile_wins_even_when_another_week_has_same_average():
    current = [8, 7, 6, 7, 8, 10, 12, 13, 14, 15]
    too_different = list(reversed(current))
    closest = [value + 0.5 for value in current]
    rows = (
        _hour_rows(MONDAY, [*current, 99])
        + _hour_rows(MONDAY - timedelta(weeks=1), too_different)
        + _hour_rows(MONDAY - timedelta(weeks=2), [*closest, -99])
    )

    match = find_weather_week_match(rows, MONDAY + timedelta(hours=10, minutes=53))

    assert match["status"] == "matched"
    assert match["current"] == {
        "start_ts": MONDAY.timestamp(),
        "end_ts": (MONDAY + timedelta(hours=10)).timestamp(),
    }
    assert match["comparison"] == {
        "start_ts": (MONDAY - timedelta(weeks=2)).timestamp(),
        "end_ts": (MONDAY - timedelta(weeks=2) + timedelta(hours=10)).timestamp(),
    }
    assert match["matched_hours"] == match["expected_hours"] == 10
    assert match["weather_mae"] == 0.5
    assert match["outdoor_profile"][-1] == {
        "ts": (MONDAY + timedelta(hours=9)).timestamp(),
        "current_temp": 15,
        "comparison_temp": 15.5,
    }


def test_requires_actual_overlap_and_keeps_missing_intervals_visible():
    current = [5] * 9 + [None]
    too_few_paired = [5] * 7 + [None, None, 5]
    supported = [5] * 8 + [None, 5]
    rows = (
        _hour_rows(MONDAY, current)
        + _hour_rows(MONDAY - timedelta(weeks=1), too_few_paired)
        + _hour_rows(MONDAY - timedelta(weeks=2), supported)
    )
    match = find_weather_week_match(rows, MONDAY + timedelta(hours=10))

    assert match["comparison"]["start_ts"] == (MONDAY - timedelta(weeks=2)).timestamp()
    assert match["matched_hours"] == 8
    assert match["outdoor_profile"][-2]["comparison_temp"] is None
    assert match["outdoor_profile"][-1]["current_temp"] is None


def test_equal_matches_prefer_coverage_then_recency():
    rows = (
        _hour_rows(MONDAY, [4] * 10)
        + _hour_rows(MONDAY - timedelta(weeks=1), [4] * 8)
        + _hour_rows(MONDAY - timedelta(weeks=2), [4] * 10)
        + _hour_rows(MONDAY - timedelta(weeks=3), [4] * 10)
    )
    match = find_weather_week_match(rows, MONDAY + timedelta(hours=10))

    assert match["comparison"]["start_ts"] == (MONDAY - timedelta(weeks=2)).timestamp()
    assert match["matched_hours"] == 10


@pytest.mark.parametrize(
    ("hours", "current", "status"),
    [
        (5, [6] * 5, "insufficient_current"),
        (10, [6] * 7, "insufficient_current"),
        (10, [6] * 8, "no_comparable_week"),
    ],
)
def test_reports_insufficient_current_data_separately(hours, current, status):
    result = find_weather_week_match(_hour_rows(MONDAY, current), MONDAY + timedelta(hours=hours))
    assert result["status"] == status
    assert result["comparison"] is None
    assert result["weather_mae"] is None


def test_duplicate_room_samples_and_invalid_values_do_not_create_hourly_coverage():
    single = {"timestamp": MONDAY.timestamp(), "outdoor_temp": 7}
    bad = [
        {"timestamp": MONDAY.timestamp() + 300, "outdoor_temp": "nan"},
        {"timestamp": "nan", "outdoor_temp": 7},
        {"timestamp": "bad", "outdoor_temp": 7},
        {"timestamp": MONDAY.timestamp() + 600},
    ]
    match = find_weather_week_match([single] * 12 + bad, MONDAY + timedelta(hours=6))

    assert match["status"] == "insufficient_current"


def test_matches_local_clock_hours_across_spring_forward():
    zone = ZoneInfo("Europe/Berlin")
    start = datetime(2026, 3, 23, tzinfo=zone)
    previous = start - timedelta(weeks=1)
    # Current Sunday 02:00 does not exist. Match all the other completed clock hours.
    now = datetime(2026, 3, 29, 8, 45, tzinfo=zone)
    current_hours = int((now.replace(minute=0).timestamp() - start.timestamp()) / 3600)
    current_rows = _hour_rows(start, [3] * current_hours)
    previous_rows = _hour_rows(previous, [4] * (current_hours + 1))

    match = find_weather_week_match(current_rows + previous_rows, now)

    assert match["comparison"]["end_ts"] == datetime(2026, 3, 22, 8, tzinfo=zone).timestamp()
    assert match["time_zone"] == "Europe/Berlin"
    assert match["matched_hours"] == match["expected_hours"] == current_hours
    assert match["weather_mae"] == 1
    assert not any(
        (local := datetime.fromtimestamp(point["ts"], zone)).day == 29 and local.hour == 2
        for point in match["outdoor_profile"]
    )


def test_fall_back_combines_both_occurrences_of_the_same_wall_hour():
    zone = ZoneInfo("Europe/Berlin")
    start = datetime(2026, 10, 19, tzinfo=zone)
    previous = start - timedelta(weeks=1)
    now = datetime(2026, 10, 25, 6, tzinfo=zone)
    current_hours = int((now.timestamp() - start.timestamp()) / 3600)
    current = [4] * current_hours
    current[146:148] = [2, 6]  # Two occurrences of Sunday 02:00.

    match = find_weather_week_match(
        _hour_rows(start, current) + _hour_rows(previous, [4] * (current_hours - 1)),
        now,
    )

    assert match["matched_hours"] == match["expected_hours"] == current_hours - 1
    assert match["weather_mae"] == 0
    assert match["outdoor_profile"][146]["current_temp"] == 4


def test_reads_history_and_detail_from_different_rooms(tmp_path, monkeypatch):
    store = HistoryStore(str(tmp_path))
    previous = MONDAY - timedelta(weeks=1)
    now = MONDAY + timedelta(hours=6)
    for row in _hour_rows(MONDAY, [5] * 6):
        store.record("living", row, row["timestamp"])
    for row in _hour_rows(previous, [4] * 6):
        store.record("bedroom", row, row["timestamp"])
    monkeypatch.setattr("custom_components.roommind.utils.history_store.time.time", now.timestamp)
    store.rotate("bedroom")

    match = read_weather_week_match(store, ("living", "bedroom"), now)

    assert match["status"] == "matched"
    assert match["weather_mae"] == 1
