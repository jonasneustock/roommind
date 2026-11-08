"""Tests for schedule comfort classification, archival and ranking."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from custom_components.roommind.services.schedule_comfort import build_comfort_ranking, classify_schedule_comfort
from custom_components.roommind.utils.history_store import HistoryStore

_START = datetime(2026, 11, 1, tzinfo=UTC).timestamp()


@pytest.mark.parametrize(
    ("temp", "heat", "cool", "expected"),
    [
        (19, 20, 24, 0),
        (18.99, 20, 24, -1),
        (25, 20, 24, 0),
        (25.01, 20, 24, 1),
        (27, 20, None, 0),
        (15, None, 24, 0),
        (26, None, 24, 1),
        (20, None, None, None),
        (None, 20, 24, None),
        (float("nan"), 20, 24, None),
    ],
)
def test_classify_schedule_comfort(temp, heat, cool, expected):
    assert classify_schedule_comfort(temp, heat, cool) == expected


def _sample(comfort: int | None) -> dict:
    return {
        "schedule_comfort_valid": int(comfort is not None),
        "schedule_comfort_cold": int(comfort == -1),
        "schedule_comfort_warm": int(comfort == 1),
    }


def test_weighted_ranking_clips_range_and_leaves_out_gaps(tmp_path):
    store = HistoryStore(str(tmp_path))
    store.record("office", _sample(-1), _START - 60)
    store.record("office", _sample(1), _START + 120)
    store.record("office", _sample(None), _START + 840)
    store.record("office", _sample(0), _START + 1020)
    store.record("kitchen", _sample(0), _START)
    store.record("legacy", {"room_temp": 15, "heat_target": 20}, _START)

    result = build_comfort_ranking(store, ["legacy", "kitchen", "office"], _START, _START + 1080, UTC)
    office, kitchen, legacy = result["rooms"]
    assert office["area_id"] == "office"
    assert (office["valid_seconds"], office["cold_seconds"], office["warm_seconds"]) == (360, 120, 180)
    assert office["miss_pct"] == pytest.approx(83.33)
    assert office["days"][0]["start_ts"] == _START
    assert office["days"][0]["end_ts"] == _START + 1080
    assert kitchen["miss_pct"] == 0
    assert legacy["miss_pct"] is None
    assert legacy["valid_seconds"] == 0


def test_rotation_keeps_miss_fraction_when_average_temperature_masks_it(tmp_path, monkeypatch):
    store = HistoryStore(str(tmp_path))
    store.record("office", {"mode": "heating", "room_temp": 18, "heat_target": 20, **_sample(-1)}, _START)
    store.record("office", {"mode": "idle", "room_temp": 22, "heat_target": 20, **_sample(0)}, _START + 180)
    store.record("office", {"mode": "idle", "room_temp": 17, "heat_target": 20}, _START + 360)
    store.record("office", {"mode": "idle", **_sample(1)}, _START + 540)
    monkeypatch.setattr("custom_components.roommind.utils.history_store.time.time", lambda: _START + 72 * 3600)
    store.rotate("office")

    archived = store.read_history("office")
    assert float(archived[0]["room_temp"]) == 20
    assert float(archived[0]["schedule_comfort_cold"]) == 0.5
    assert float(archived[1]["schedule_comfort_valid"]) == 0.5

    result = build_comfort_ranking(store, ["office"], _START, _START + 600, UTC)
    assert result["rooms"][0]["valid_seconds"] == 450
    assert result["rooms"][0]["cold_seconds"] == 150
    assert result["rooms"][0]["warm_seconds"] == 150


def test_daily_drilldown_respects_local_midnight_and_dst(tmp_path):
    store = HistoryStore(str(tmp_path))
    zone = ZoneInfo("Europe/Berlin")
    midnight = datetime(2026, 10, 25, tzinfo=zone).timestamp()
    next_midnight = datetime(2026, 10, 26, tzinfo=zone).timestamp()
    store.record("office", _sample(-1), midnight - 60)
    result = build_comfort_ranking(store, ["office"], midnight - 120, next_midnight, zone)

    previous, fallback_day = result["rooms"][0]["days"]
    assert previous["date"] == "2026-10-24"
    assert previous["cold_seconds"] == 60
    assert fallback_day["date"] == "2026-10-25"
    assert fallback_day["cold_seconds"] == 120
    assert fallback_day["end_ts"] - fallback_day["start_ts"] == 25 * 3600
