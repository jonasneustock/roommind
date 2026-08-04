"""Schedule utilities for resolving future target temperatures."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

from homeassistant.util import dt as dt_util

from ..const import (
    DEFAULT_COMFORT_COOL,
    DEFAULT_COMFORT_HEAT,
    DEFAULT_ECO_COOL,
    DEFAULT_ECO_HEAT,
    TargetTemps,
)

_LOGGER = logging.getLogger(__name__)


def find_active_block(schedule_blocks: dict, ts: float) -> dict[str, Any] | None:
    """Find the schedule block active at the given timestamp.

    Returns the block's ``data`` dict, or None if no block matches.
    Caller must check ``schedule_blocks is None`` before calling.
    """
    dt = dt_util.as_local(datetime.fromtimestamp(ts, tz=UTC))
    day_name = dt.strftime("%A").lower()
    current_time = dt.time()
    day_blocks = schedule_blocks.get(day_name, [])
    for block in day_blocks:
        from_raw = block.get("from", "00:00:00")
        to_raw = block.get("to", "00:00:00")
        from_time = from_raw if hasattr(from_raw, "hour") else datetime.strptime(str(from_raw), "%H:%M:%S").time()
        to_time = to_raw if hasattr(to_raw, "hour") else datetime.strptime(str(to_raw), "%H:%M:%S").time()
        if from_time <= current_time < to_time:
            return dict(block.get("data", {}))
    return None


def resolve_target_at_time(
    ts: float,
    schedule_blocks: dict | None,
    override_until: float | None,
    override_temp: float | None,
    vacation_until: float | None,
    vacation_temp: float | None,
    comfort_temp: float,
    eco_temp: float,
    presence_away: bool = False,
    block_temp_converter: Callable[[float], float] | None = None,
    presence_away_action: str = "eco",
    schedule_off_action: str = "eco",
) -> float | None:
    """Resolve what the target temp would be at a specific timestamp.

    Returns None when the action is "off" (devices should be turned off).
    """
    # 1. Override
    if override_temp is not None and (override_until is None or ts < override_until):
        return float(override_temp)
    # 2. Vacation
    if vacation_until is not None and ts < vacation_until and vacation_temp is not None:
        return float(vacation_temp)
    # 2.5 Presence
    if presence_away:
        if presence_away_action == "off":
            return None
        return eco_temp
    # 3. Schedule blocks
    if schedule_blocks is None:
        return comfort_temp
    data = find_active_block(schedule_blocks, ts)
    if data is not None:
        block_temp = data.get("temperature")
        if block_temp is not None:
            try:
                val = float(block_temp)
                return block_temp_converter(val) if block_temp_converter else val
            except (ValueError, TypeError):
                pass
        return comfort_temp
    # Not in any block → eco or off
    if schedule_off_action == "off":
        return None
    return eco_temp


def _is_boolean_sensor_on(hass: HomeAssistant, entity_id: str | None) -> bool:
    """Return whether a boolean-like entity is on/true."""
    if not entity_id:
        return False
    state = hass.states.get(entity_id)
    return state is not None and str(state.state).lower() in ("on", "true", "1", "yes")


def _constrain_heat_cool_band(heat: float | None, cool: float | None) -> TargetTemps:
    """Keep heat/cool targets from crossing after price adjustments."""
    if heat is not None and cool is not None and heat > cool:
        midpoint = round((heat + cool) / 2, 1)
        heat = midpoint
        cool = midpoint
    return TargetTemps(heat=heat, cool=cool)


def _apply_electricity_price_adjustment(
    targets: TargetTemps,
    slot: str,
    electricity_cheap: bool,
    electricity_cheap_delta: float,
    electricity_cheap_slots: str,
    electricity_expensive: bool = False,
    electricity_expensive_delta: float = 0.0,
    electricity_expensive_slots: str = "both",
) -> TargetTemps:
    """Adjust targets for cheap/expensive electricity in the active slot.

    Expensive energy takes precedence when both sensors are active: heating
    targets are lowered and cooling targets are raised to reduce demand.
    Cheap energy does the inverse to pre-condition the room.
    """
    if electricity_expensive and electricity_expensive_delta > 0 and electricity_expensive_slots in ("both", slot):
        heat = None if targets.heat is None else round(float(targets.heat) - electricity_expensive_delta, 1)
        cool = None if targets.cool is None else round(float(targets.cool) + electricity_expensive_delta, 1)
        return _constrain_heat_cool_band(heat, cool)

    if not electricity_cheap or electricity_cheap_delta <= 0 or electricity_cheap_slots not in ("both", slot):
        return targets
    heat = None if targets.heat is None else round(float(targets.heat) + electricity_cheap_delta, 1)
    cool = None if targets.cool is None else round(float(targets.cool) - electricity_cheap_delta, 1)
    return _constrain_heat_cool_band(heat, cool)


def is_electricity_cheap(hass: HomeAssistant, room: dict) -> bool:
    """Return whether the room's cheap-electricity boolean sensor is on."""
    return _is_boolean_sensor_on(hass, room.get("electricity_cheap_sensor"))


def is_electricity_expensive(hass: HomeAssistant, room: dict) -> bool:
    """Return whether the room's expensive-electricity boolean sensor is on."""
    return _is_boolean_sensor_on(hass, room.get("electricity_expensive_sensor"))


def resolve_targets_at_time(
    ts: float,
    schedule_blocks: dict | None,
    override_until: float | None,
    override_heat: float | None,
    override_cool: float | None,
    vacation_until: float | None,
    vacation_temp: float | None,
    comfort_heat: float,
    comfort_cool: float,
    eco_heat: float,
    eco_cool: float,
    presence_away: bool = False,
    block_temp_converter: Callable[[float], float] | None = None,
    presence_away_action: str = "eco",
    schedule_off_action: str = "eco",
    presence_clears_override: bool = False,
    electricity_cheap: bool = False,
    electricity_cheap_delta: float = 0.0,
    electricity_cheap_slots: str = "both",
    electricity_expensive: bool = False,
    electricity_expensive_delta: float = 0.0,
    electricity_expensive_slots: str = "both",
) -> TargetTemps:
    """Resolve dual heat/cool target temps at a specific timestamp.

    Returns TargetTemps(heat, cool). None values mean "force off".
    """
    # 1. Override — split heat/cool dead-band (skipped when presence-away suppresses it)
    if (override_heat is not None or override_cool is not None) and (override_until is None or ts < override_until):
        if not (presence_away and presence_clears_override):
            return TargetTemps(heat=override_heat, cool=override_cool)
    # 2. Vacation — heat setback, cooling stays at eco_cool
    if vacation_until is not None and ts < vacation_until and vacation_temp is not None:
        t = float(vacation_temp)
        return TargetTemps(heat=t, cool=max(t, eco_cool))
    # 2.5 Presence
    if presence_away:
        if presence_away_action == "off":
            return TargetTemps(heat=None, cool=None)
        return _apply_electricity_price_adjustment(
            TargetTemps(heat=eco_heat, cool=eco_cool),
            "eco",
            electricity_cheap,
            electricity_cheap_delta,
            electricity_cheap_slots,
            electricity_expensive,
            electricity_expensive_delta,
            electricity_expensive_slots,
        )
    # 3. Schedule blocks
    if schedule_blocks is None:
        return _apply_electricity_price_adjustment(
            TargetTemps(heat=comfort_heat, cool=comfort_cool),
            "comfort",
            electricity_cheap,
            electricity_cheap_delta,
            electricity_cheap_slots,
            electricity_expensive,
            electricity_expensive_delta,
            electricity_expensive_slots,
        )
    data = find_active_block(schedule_blocks, ts)
    if data is not None:
        heat_temp_raw = data.get("heat_temperature")
        cool_temp_raw = data.get("cool_temperature")
        if heat_temp_raw is not None or cool_temp_raw is not None:
            h = comfort_heat
            c = comfort_cool
            if heat_temp_raw is not None:
                try:
                    val = float(heat_temp_raw)
                    h = block_temp_converter(val) if block_temp_converter else val
                except (ValueError, TypeError):
                    pass
            if cool_temp_raw is not None:
                try:
                    val = float(cool_temp_raw)
                    c = block_temp_converter(val) if block_temp_converter else val
                except (ValueError, TypeError):
                    pass
            return _apply_electricity_price_adjustment(
                TargetTemps(heat=h, cool=c),
                "comfort",
                electricity_cheap,
                electricity_cheap_delta,
                electricity_cheap_slots,
                electricity_expensive,
                electricity_expensive_delta,
                electricity_expensive_slots,
            )
        block_temp = data.get("temperature")
        if block_temp is not None:
            try:
                val = float(block_temp)
                t = block_temp_converter(val) if block_temp_converter else val
                return _apply_electricity_price_adjustment(
                    TargetTemps(heat=t, cool=t),
                    "comfort",
                    electricity_cheap,
                    electricity_cheap_delta,
                    electricity_cheap_slots,
                    electricity_expensive,
                    electricity_expensive_delta,
                    electricity_expensive_slots,
                )
            except (ValueError, TypeError):
                pass
        return _apply_electricity_price_adjustment(
            TargetTemps(heat=comfort_heat, cool=comfort_cool),
            "comfort",
            electricity_cheap,
            electricity_cheap_delta,
            electricity_cheap_slots,
            electricity_expensive,
            electricity_expensive_delta,
            electricity_expensive_slots,
        )
    # Not in any block → eco or off
    if schedule_off_action == "off":
        return TargetTemps(heat=None, cool=None)
    return _apply_electricity_price_adjustment(
        TargetTemps(heat=eco_heat, cool=eco_cool),
        "eco",
        electricity_cheap,
        electricity_cheap_delta,
        electricity_cheap_slots,
        electricity_expensive,
        electricity_expensive_delta,
        electricity_expensive_slots,
    )


def resolve_schedule_index(
    hass: HomeAssistant,
    room: dict,
    *,
    schedules_key: str = "schedules",
    selector_key: str = "schedule_selector_entity",
) -> int:
    """Return the 0-based index of the active schedule, or -1 if none.

    This is the single source of truth for schedule selector resolution,
    used by both the coordinator and schedule_utils helpers.

    Supports custom key names for reuse with different schedule types
    (e.g. cover schedules).
    """
    schedules = room.get(schedules_key, [])
    if not schedules:
        return -1

    selector_entity = room.get(selector_key, "")
    if not selector_entity:
        return 0

    state = hass.states.get(selector_entity)
    if state is None or state.state in ("unavailable", "unknown"):
        return 0

    if selector_entity.startswith("input_boolean."):
        return 1 if state.state == "on" else 0

    if selector_entity.startswith("input_number."):
        try:
            idx = int(float(state.state)) - 1  # 1-based → 0-based
        except (ValueError, TypeError):
            return 0
        if 0 <= idx < len(schedules):
            return idx
        return -1

    # Fallback for unknown entity domains
    return 0


def get_active_schedule_entity(
    hass: HomeAssistant,
    room: dict,
) -> str | None:
    """Return the entity_id of the currently active schedule, or None."""
    schedules = room.get("schedules", [])
    idx = resolve_schedule_index(hass, room)
    if 0 <= idx < len(schedules):
        return schedules[idx].get("entity_id", "") or None
    return None


async def read_schedule_blocks(
    hass: HomeAssistant,
    schedule_entity_id: str,
    cache: dict[str, dict] | None = None,
) -> dict | None:
    """Read weekly schedule blocks via schedule.get_schedule service.

    When ``cache`` is provided, successful reads are stored under the entity ID
    so subsequent failures fall back to the last good blocks. Without a cache,
    transient service failures cause the caller's target resolution to silently
    revert to comfort_temp (see #308).
    """
    if not schedule_entity_id or not schedule_entity_id.startswith("schedule."):
        return None

    blocks: dict | None = None
    error: Exception | None = None
    try:
        response = await hass.services.async_call(
            "schedule",
            "get_schedule",
            {"entity_id": schedule_entity_id},
            blocking=True,
            return_response=True,
        )
        if response and schedule_entity_id in response:
            result = response[schedule_entity_id]
            if isinstance(result, dict):
                blocks = dict(result)
    except Exception as err:  # noqa: BLE001
        error = err

    if blocks is not None:
        if cache is not None:
            cache[schedule_entity_id] = blocks
        return blocks

    cached = cache.get(schedule_entity_id) if cache is not None else None
    if cached is not None:
        _LOGGER.debug(
            "schedule.get_schedule unavailable for %s; using cached blocks (error=%r)",
            schedule_entity_id,
            error,
        )
        return cached

    _LOGGER.warning(
        "schedule.get_schedule unavailable for %s and no cached blocks; "
        "target will fall back to comfort/eco (error=%r)",
        schedule_entity_id,
        error,
    )
    return None


def make_target_resolver(
    schedule_blocks: dict | None,
    room: dict,
    settings: dict,
    hass: HomeAssistant | None = None,
    presence_away: bool = False,
    mold_prevention_delta: float = 0.0,
) -> Callable[[float], TargetTemps]:
    """Create a sync target resolver function (schedule blocks pre-fetched).

    Returns TargetTemps with None values for timestamps where devices should
    be turned off.
    """
    comfort_heat = room.get("comfort_heat", room.get("comfort_temp", DEFAULT_COMFORT_HEAT))
    comfort_cool = room.get("comfort_cool", DEFAULT_COMFORT_COOL)
    eco_heat = room.get("eco_heat", room.get("eco_temp", DEFAULT_ECO_HEAT))
    eco_cool = room.get("eco_cool", DEFAULT_ECO_COOL)
    override_until = room.get("override_until")
    override_heat = room.get("override_heat")
    override_cool = room.get("override_cool")
    vacation_until = settings.get("vacation_until")
    vacation_temp = settings.get("vacation_temp")
    presence_away_action = settings.get("presence_away_action", "eco")
    schedule_off_action = settings.get("schedule_off_action", "eco")
    presence_clears_override = bool(settings.get("presence_clears_override", False))
    electricity_cheap = bool(hass is not None and is_electricity_cheap(hass, room))
    electricity_cheap_delta = float(room.get("electricity_cheap_delta", 0.0) or 0.0)
    electricity_cheap_slots = room.get("electricity_cheap_slots", "both")
    electricity_expensive = bool(hass is not None and is_electricity_expensive(hass, room))
    electricity_expensive_delta = float(room.get("electricity_expensive_delta", 0.0) or 0.0)
    electricity_expensive_slots = room.get("electricity_expensive_slots", "both")

    converter: Callable[[float], float] | None = None
    if hass is not None:
        from .temp_utils import ha_temp_to_celsius

        _hass = hass
        converter = lambda v: ha_temp_to_celsius(_hass, v)  # noqa: E731

    def resolver(ts: float) -> TargetTemps:
        targets = resolve_targets_at_time(
            ts,
            schedule_blocks,
            override_until,
            override_heat,
            override_cool,
            vacation_until,
            vacation_temp,
            comfort_heat,
            comfort_cool,
            eco_heat,
            eco_cool,
            presence_away=presence_away,
            block_temp_converter=converter,
            presence_away_action=presence_away_action,
            schedule_off_action=schedule_off_action,
            presence_clears_override=presence_clears_override,
            electricity_cheap=electricity_cheap,
            electricity_cheap_delta=electricity_cheap_delta,
            electricity_cheap_slots=electricity_cheap_slots,
            electricity_expensive=electricity_expensive,
            electricity_expensive_delta=electricity_expensive_delta,
            electricity_expensive_slots=electricity_expensive_slots,
        )
        if targets.heat is None and targets.cool is None:
            return targets
        return TargetTemps(
            heat=targets.heat + mold_prevention_delta if targets.heat is not None else None,
            cool=targets.cool if targets.cool is not None else None,
        )

    return resolver
