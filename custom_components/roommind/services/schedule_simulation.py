"""Replay two heating schedules using the same learned model and room history."""

from __future__ import annotations

import math
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import time as wall_time
from functools import partial
from typing import TYPE_CHECKING

from homeassistant.util import dt as dt_util

from ..const import (
    DEFAULT_COMFORT_COOL,
    DEFAULT_COMFORT_HEAT,
    DEFAULT_ECO_COOL,
    DEFAULT_ECO_HEAT,
    MAX_TARGET_TEMP,
    MIN_TARGET_TEMP,
    MODE_COOLING,
    MODE_HEATING,
    MODE_IDLE,
    TargetTemps,
)
from ..control.analytics_simulator import simulate_prediction
from ..control.mpc_controller import (
    MIN_ACTIVE_UPDATES,
    MIN_IDLE_UPDATES,
    PLAN_DT_MINUTES,
    check_acs_can_heat,
    get_can_heat_cool,
    is_mpc_active,
)
from ..control.thermal_model import RoomModelManager
from ..managers.cover_manager import compute_shading_factor
from ..utils.schedule_utils import (
    find_active_block,
    get_active_schedule_entity,
    read_schedule_blocks,
    resolve_targets_at_time,
)
from ..utils.temp_utils import ha_temp_to_celsius

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from ..control.thermal_model import ThermalEKF
    from ..coordinator import RoomMindCoordinator
    from ..store import RoomMindStore
    from ..utils.history_store import HistoryStore

_STEP_SECONDS = int(PLAN_DT_MINUTES * 60)
_MIN_BLOCKS = 12
_MAX_SECONDS = 48 * 3600
_MAX_SHIFT_MINUTES = 180
_COMFORT_TOLERANCE_C = 1.0


class ScheduleSimulationError(ValueError):
    """A comparison cannot be run for this room or range."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ScheduleChanges:
    start_shift_minutes: int = 0
    overnight_heat: float | None = None
    daytime_heat: float | None = None

    def __post_init__(self) -> None:
        if abs(self.start_shift_minutes) > _MAX_SHIFT_MINUTES:
            raise ScheduleSimulationError("invalid_input", "Heating start shift must be within 180 minutes.")
        for target in (self.overnight_heat, self.daytime_heat):
            if target is not None and not MIN_TARGET_TEMP <= target <= MAX_TARGET_TEMP:
                raise ScheduleSimulationError("invalid_input", "Heating targets must be between 5 and 35 °C.")


@dataclass(frozen=True)
class _ReplayBlock:
    ts: int
    room_temp: float
    outdoor_temp: float
    solar: float
    observed_mode: str
    tariff: tuple[float, str] | None


def _finite(value: object) -> float | None:
    try:
        number = float(str(value))
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _read_replay_segments(history: HistoryStore, area_id: str, start: int, end: int) -> list[list[_ReplayBlock]]:
    rows = history.read_history(area_id, None, start, end) + history.read_detail(area_id, None, start, end)
    blocks: dict[int, _ReplayBlock] = {}
    open_blocks = set()
    for row in rows:
        ts = _finite(row.get("timestamp"))
        if ts is None:
            continue
        block_ts = int(ts // _STEP_SECONDS) * _STEP_SECONDS
        if row.get("window_open") in ("True", "true", "1"):
            open_blocks.add(block_ts)
            continue
        room = _finite(row.get("room_temp"))
        outdoor = _finite(row.get("outdoor_temp"))
        if room is None or outdoor is None:
            continue
        solar = _finite(row.get("solar_irradiance")) or 0.0
        blind = _finite(row.get("blind_position"))
        if blind is not None:
            solar *= compute_shading_factor([round(blind)])
        price = _finite(row.get("electricity_price"))
        prior = blocks.get(block_ts)
        tariff = (price, str(row.get("electricity_price_unit") or "")) if price is not None else None
        blocks[block_ts] = _ReplayBlock(
            block_ts, room, outdoor, solar, str(row.get("mode", "")), tariff or (prior.tariff if prior else None)
        )

    segments: list[list[_ReplayBlock]] = []
    for ts, block in sorted(blocks.items()):
        if ts in open_blocks or not start <= ts < end:
            continue
        if not segments or ts != segments[-1][-1].ts + _STEP_SECONDS:
            segments.append([])
        segments[-1].append(block)
    return [segment for segment in segments if len(segment) >= _MIN_BLOCKS]


def _clock(value: wall_time | str) -> wall_time:
    return value if isinstance(value, wall_time) else wall_time.fromisoformat(value)


def _day_seconds(value: wall_time) -> int:
    return value.hour * 3600 + value.minute * 60 + value.second


def _shift_first_blocks(blocks: dict, minutes: int) -> dict:
    """Shift the first comfort block of each day within its original day."""
    if not minutes:
        return blocks
    shifted: dict = {}
    for day, entries in blocks.items():
        ordered = sorted(entries, key=lambda block: _clock(block["from"]))
        if ordered:
            first = ordered[0]
            start = _day_seconds(_clock(first["from"]))
            end = _day_seconds(_clock(first["to"]))
            latest = max(start, end - _STEP_SECONDS)
            seconds = min(latest, max(0, start + minutes * 60))
            hours, rest = divmod(seconds, 3600)
            mins, secs = divmod(rest, 60)
            ordered[0] = {**first, "from": wall_time(hours, mins, secs)}
        shifted[day] = ordered
    return shifted


@dataclass(frozen=True)
class _Schedule:
    blocks: dict
    settings: dict
    room: dict
    can_cool: bool
    convert_temperature: Callable[[float], float]

    def target(self, ts: int, changes: ScheduleChanges) -> TargetTemps:
        targets = resolve_targets_at_time(
            ts,
            self.blocks,
            None,
            None,
            None,
            None,
            None,
            self.room.get("comfort_heat", self.room.get("comfort_temp", DEFAULT_COMFORT_HEAT)),
            self.room.get("comfort_cool", DEFAULT_COMFORT_COOL),
            self.room.get("eco_heat", self.room.get("eco_temp", DEFAULT_ECO_HEAT)),
            self.room.get("eco_cool", DEFAULT_ECO_COOL),
            block_temp_converter=self.convert_temperature,
            schedule_off_action=self.settings.get("schedule_off_action", "eco"),
        )
        heat, cool = targets.heat, targets.cool if self.can_cool else None
        if changes.daytime_heat is not None and find_active_block(self.blocks, ts) is not None:
            heat = changes.daytime_heat
        elif changes.overnight_heat is not None:
            local = dt_util.as_local(dt_util.utc_from_timestamp(ts))
            entries = self.blocks.get(local.strftime("%A").lower(), [])
            if (
                not entries
                or local.time() < min(_clock(block["from"]) for block in entries)
                or local.time() >= max(_clock(block["to"]) for block in entries)
            ):
                heat = changes.overnight_heat
        # Keep the existing cooling target when a proposed heat target meets it.
        if heat is not None and cool is not None:
            heat = min(heat, cool)
        return TargetTemps(heat, cool)


def _score(
    modes: list[str], temperatures: list[float], reference: list[TargetTemps], blocks: list[_ReplayBlock]
) -> tuple[dict, Counter[tuple[float, str]]]:
    cold = warm = heating = 0
    tariffs: Counter[tuple[float, str]] = Counter()
    for mode, temp, target, block in zip(modes, temperatures, reference, blocks, strict=True):
        if target.heat is not None and temp < target.heat - _COMFORT_TOLERANCE_C:
            cold += 1
        elif target.cool is not None and temp > target.cool + _COMFORT_TOLERANCE_C:
            warm += 1
        if mode == MODE_HEATING:
            heating += 1
            if block.tariff is not None:
                tariffs[block.tariff] += PLAN_DT_MINUTES
    return {
        "too_cold_minutes": cold * PLAN_DT_MINUTES,
        "too_warm_minutes": warm * PLAN_DT_MINUTES,
        "heating_minutes": heating * PLAN_DT_MINUTES,
    }, tariffs


def _summarize_tariffs(tariffs: Counter[tuple[float, str]]) -> dict:
    minutes = tariffs.total()
    units = {unit for _, unit in tariffs}
    average = sum(price * duration for (price, _), duration in tariffs.items()) / minutes if len(units) == 1 else None
    return {
        "tariff_minutes": minutes,
        "average_heating_tariff": average,
        "tariff_unit": next(iter(units)) if len(units) == 1 else "",
    }


def _compare(
    segments: list[list[_ReplayBlock]],
    baseline: _Schedule,
    changes: ScheduleChanges,
    estimator: ThermalEKF,
    acs_can_heat: bool,
    mpc_active: bool,
) -> dict:
    candidate = _Schedule(
        _shift_first_blocks(baseline.blocks, changes.start_shift_minutes),
        baseline.settings,
        baseline.room,
        baseline.can_cool,
        baseline.convert_temperature,
    )
    unchanged = ScheduleChanges()
    totals: dict[str, Counter] = {"current": Counter(), "proposed": Counter()}
    tariffs: dict[str, Counter[tuple[float, str]]] = {"current": Counter(), "proposed": Counter()}
    comfort_minutes = 0
    model = estimator.get_model()
    for blocks in segments:
        reference = [baseline.target(block.ts, unchanged) for block in blocks]
        proposal = [candidate.target(block.ts, changes) for block in blocks]
        comfort_minutes += PLAN_DT_MINUTES * sum(t.heat is not None or t.cool is not None for t in reference)
        initial_mode = blocks[0].observed_mode
        if initial_mode not in (MODE_HEATING, MODE_COOLING):
            initial_mode = MODE_IDLE
        for scenario, targets in (("current", reference), ("proposed", proposal)):
            modes: list[str] = []
            temps = simulate_prediction(
                model=model,
                estimator=estimator,
                target_forecast=[
                    {"target_temp": t.heat, "heat_target": t.heat, "cool_target": t.cool} for t in targets
                ],
                outdoor_series=[block.outdoor_temp for block in blocks],
                current_temp=blocks[0].room_temp,
                window_open=False,
                mpc_active=mpc_active,
                room_config=baseline.room,
                settings=baseline.settings,
                all_points=[],
                solar_series=[block.solar for block in blocks],
                acs_can_heat=acs_can_heat,
                heating_system_type=baseline.room.get("heating_system_type", ""),
                previous_mode=initial_mode,
                modes=modes,
            )
            scores, by_tariff = _score(modes, temps, reference, blocks)
            totals[scenario].update(scores)
            tariffs[scenario].update(by_tariff)
    return {
        **{key: {**totals[key], **_summarize_tariffs(tariffs[key])} for key in totals},
        "comfort_minutes": comfort_minutes,
        "tariff_minutes": PLAN_DT_MINUTES * sum(block.tariff is not None for segment in segments for block in segment),
        "heating_by_tariff": [
            {
                "price": price,
                "unit": unit,
                "current": tariffs["current"][price, unit],
                "proposed": tariffs["proposed"][price, unit],
            }
            for price, unit in sorted(tariffs["current"].keys() | tariffs["proposed"].keys())
        ],
    }


async def build_schedule_comparison(
    hass: HomeAssistant,
    store: RoomMindStore,
    coordinator: RoomMindCoordinator | None,
    area_id: str,
    start_ts: float,
    end_ts: float,
    changes: ScheduleChanges,
) -> dict:
    """Compare today's configured schedules over up to 48 hours of recorded weather."""
    if not (math.isfinite(start_ts) and math.isfinite(end_ts) and 0 <= start_ts < end_ts <= time.time() + 60):
        raise ScheduleSimulationError("invalid_input", "Select a past start and end time.")
    end = int(min(time.time(), end_ts) // _STEP_SECONDS) * _STEP_SECONDS
    start = max(math.ceil(start_ts / _STEP_SECONDS) * _STEP_SECONDS, end - _MAX_SECONDS)
    if end - start < _MIN_BLOCKS * _STEP_SECONDS:
        raise ScheduleSimulationError("history_missing", "Select at least one hour of history.")

    room = store.get_room(area_id)
    if not room or room.get("is_outdoor") or not room.get("temperature_sensor"):
        raise ScheduleSimulationError("room_unavailable", "Choose a room with a temperature sensor and heating.")
    acs_can_heat = check_acs_can_heat(hass, room)
    can_heat, can_cool = get_can_heat_cool(room, acs_can_heat=acs_can_heat)
    if not can_heat:
        raise ScheduleSimulationError("room_unavailable", "Choose a room with a temperature sensor and heating.")
    history = coordinator.history_store if coordinator is not None else None
    if coordinator is None or history is None:
        raise ScheduleSimulationError("history_missing", "No room history is available yet.")
    manager = coordinator._model_manager
    if area_id not in manager.get_room_ids():
        raise ScheduleSimulationError("model_untrained", "RoomMind needs more heating history for this room.")
    idle_count, heating_count, _ = manager.get_mode_counts(area_id)
    if idle_count < MIN_IDLE_UPDATES or heating_count < MIN_ACTIVE_UPDATES:
        raise ScheduleSimulationError("model_untrained", "RoomMind needs more heating history for this room.")

    # Snapshot before awaiting I/O, including a concurrent reset of the live model.
    snapshot = RoomModelManager.from_dict({area_id: manager.get_estimator(area_id).to_dict()})
    settings = store.get_settings()
    entity = get_active_schedule_entity(hass, room)
    blocks = await read_schedule_blocks(hass, entity) if entity else None
    if not blocks or not any(blocks.values()):
        raise ScheduleSimulationError("schedule_missing", "Choose a room with a readable weekly schedule.")

    segments = await hass.async_add_executor_job(_read_replay_segments, history, area_id, start, end)
    if not segments:
        raise ScheduleSimulationError(
            "history_missing", "No uninterrupted hour of indoor and outdoor history was found."
        )

    first = segments[0][0]
    mpc = is_mpc_active(snapshot, area_id, can_heat, can_cool, first.room_temp, first.outdoor_temp, settings=settings)
    estimator = snapshot.get_estimator(area_id)
    simulated_room = {key: value for key, value in room.items() if not key.startswith("override_")}
    schedule = _Schedule(blocks, settings, simulated_room, can_cool, partial(ha_temp_to_celsius, hass))
    summary = await hass.async_add_executor_job(_compare, segments, schedule, changes, estimator, acs_can_heat, mpc)
    return {
        **summary,
        "start_ts": start,
        "end_ts": end,
        "simulated_minutes": PLAN_DT_MINUTES * sum(len(segment) for segment in segments),
        "controller": "mpc" if mpc else "bangbang",
    }
