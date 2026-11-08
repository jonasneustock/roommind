"""Tests for the historical heating schedule comparison."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, time
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.util import dt as dt_util

from custom_components.roommind.const import MODE_HEATING, MODE_IDLE, TargetTemps
from custom_components.roommind.control.thermal_model import RoomModelManager
from custom_components.roommind.services.schedule_simulation import (
    ScheduleChanges,
    ScheduleSimulationError,
    _read_replay_segments,
    _ReplayBlock,
    _Schedule,
    _score,
    _shift_first_blocks,
    _summarize_tariffs,
    build_schedule_comparison,
)

AREA_ID = "office"


def _monday() -> int:
    return int(datetime(2025, 1, 6, tzinfo=dt_util.DEFAULT_TIME_ZONE).timestamp())


def _block(start, end, heat=20):
    return {"from": start, "to": end, "data": {"heat_temperature": heat}}


def _row(ts, **fields):
    return {"timestamp": str(ts), "outdoor_temp": "2", "room_temp": "17", "mode": MODE_IDLE, **fields}


def test_first_block_shift_stays_within_day_and_does_not_mutate_schedule():
    schedule = {"monday": [_block("07:00:00", "07:30:00"), _block("12:00:00", "17:00:00")]}
    assert _shift_first_blocks(schedule, -30)["monday"][0]["from"] == time(6, 30)
    assert _shift_first_blocks(schedule, 90)["monday"][0]["from"] == time(7, 25)
    assert _shift_first_blocks(schedule, 90)["monday"][1] == schedule["monday"][1]
    assert schedule["monday"][0]["from"] == "07:00:00"


def test_overnight_and_daytime_edits_preserve_daytime_gaps():
    schedule = _Schedule(
        {"monday": [_block("07:00:00", "10:00:00"), _block("12:00:00", "17:00:00")]},
        {"schedule_off_action": "off"},
        {"comfort_cool": 24.0},
        True,
        float,
    )
    changes = ScheduleChanges(overnight_heat=16.0, daytime_heat=25.0)
    assert schedule.target(_monday(), changes) == TargetTemps(16.0, None)
    assert schedule.target(_monday() + 8 * 3600, changes) == TargetTemps(24.0, 24.0)
    assert schedule.target(_monday() + 11 * 3600, changes) == TargetTemps(None, None)
    assert schedule.target(_monday() + 18 * 3600, changes) == TargetTemps(16.0, None)


def test_history_replay_excludes_gaps_and_open_windows():
    history = MagicMock()
    history.read_history.return_value = [
        _row(
            _monday() + i * 300,
            window_open="True" if i == 14 else "False",
            electricity_price="-0.1" if i == 0 else "",
            electricity_price_unit="EUR/kWh",
        )
        for i in range(30)
        if i != 15
    ]
    history.read_detail.return_value = [_row(_monday() + 30, outdoor_temp="3")]
    segments = _read_replay_segments(history, AREA_ID, _monday(), _monday() + 30 * 300)
    assert [len(segment) for segment in segments] == [14, 14]
    assert segments[0][0].outdoor_temp == 3.0
    assert segments[0][0].tariff == (-0.1, "EUR/kWh")
    assert segments[1][0].ts == _monday() + 16 * 300


def test_comfort_threshold_is_strict_and_tariffs_count_only_during_heating():
    reference = [TargetTemps(20.0, 24.0)] * 4
    blocks = [
        _ReplayBlock(_monday() + 300 * i, 19, 1, 0, MODE_IDLE, (price, "EUR/kWh"))
        for i, price in enumerate((-0.1, 0.3, 0.3, 0.9))
    ]
    metrics, tariffs = _score(
        [MODE_HEATING, MODE_HEATING, MODE_IDLE, MODE_IDLE], [18.99, 19.0, 25.0, 25.01], reference, blocks
    )
    assert metrics == {"too_cold_minutes": 5, "too_warm_minutes": 5, "heating_minutes": 10}
    assert _summarize_tariffs(tariffs) == {
        "tariff_minutes": 10,
        "average_heating_tariff": pytest.approx(0.1),
        "tariff_unit": "EUR/kWh",
    }
    assert _summarize_tariffs(Counter({(0.1, "EUR/kWh"): 5, (0.4, "USD/kWh"): 5}))["average_heating_tariff"] is None


@pytest.fixture
def replay_dependencies():
    hass = MagicMock()
    hass.config.units.temperature_unit = "°C"
    hass.async_add_executor_job = AsyncMock(side_effect=lambda func, *args: func(*args))
    hass.services.async_call = AsyncMock(
        return_value={"schedule.office": {"monday": [_block("07:00:00", "18:00:00", 21)]}}
    )
    store = MagicMock()
    store.get_room.return_value = {
        "temperature_sensor": "sensor.office",
        "schedules": [{"entity_id": "schedule.office"}],
        "devices": [{"type": "trv", "entity_id": "climate.office"}],
        "climate_mode": "heat_only",
        "eco_heat": 15,
        "override_heat": 30.0,
    }
    store.get_settings.return_value = {"control_mode": "bangbang"}
    coordinator = MagicMock()
    manager = coordinator._model_manager = RoomModelManager()
    estimator = manager.get_estimator(AREA_ID)
    estimator._x[2] = 6.0
    estimator._n_heating = 20
    estimator._n_idle = 60
    coordinator.history_store.read_history.return_value = [
        _row(_monday() + i * 300, electricity_price="0.3", electricity_price_unit="EUR/kWh") for i in range(8 * 12)
    ]
    coordinator.history_store.read_detail.return_value = []
    return hass, store, coordinator, manager


@pytest.mark.asyncio
async def test_replay_uses_same_reference_and_cannot_mutate_live_state(replay_dependencies):
    hass, store, coordinator, manager = replay_dependencies
    live = manager.to_dict()
    room = store.get_room.return_value.copy()
    baseline = await build_schedule_comparison(
        hass, store, coordinator, AREA_ID, _monday(), _monday() + 8 * 3600, ScheduleChanges()
    )
    result = await build_schedule_comparison(
        hass, store, coordinator, AREA_ID, _monday(), _monday() + 8 * 3600, ScheduleChanges(-60, 18.0, 21.0)
    )
    assert baseline["current"] == baseline["proposed"] == result["current"]
    assert result["simulated_minutes"] == result["comfort_minutes"] == result["tariff_minutes"] == 480
    assert result["proposed"]["heating_minutes"] > result["current"]["heating_minutes"]
    assert result["proposed"]["too_cold_minutes"] < result["current"]["too_cold_minutes"]
    assert result["proposed"]["average_heating_tariff"] == pytest.approx(0.3)
    assert manager.to_dict() == live
    assert store.get_room.return_value == room
    assert [call.args[:2] for call in hass.services.async_call.await_args_list] == [("schedule", "get_schedule")] * 2


@pytest.mark.asyncio
async def test_unknown_room_estimator_is_not_created(replay_dependencies):
    hass, store, coordinator, manager = replay_dependencies
    manager.remove_room(AREA_ID)
    with pytest.raises(ScheduleSimulationError, match="more heating history"):
        await build_schedule_comparison(
            hass, store, coordinator, AREA_ID, _monday(), _monday() + 8 * 3600, ScheduleChanges()
        )
    assert manager.get_room_ids() == []


@pytest.mark.parametrize(
    "changes", [{"start_shift_minutes": 181}, {"daytime_heat": float("nan")}, {"overnight_heat": 0}]
)
def test_invalid_edits_are_rejected(changes):
    with pytest.raises(ScheduleSimulationError):
        ScheduleChanges(**changes)
