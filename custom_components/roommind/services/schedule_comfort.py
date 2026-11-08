"""Rank rooms against the scheduled target observed at each sample."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, tzinfo
from itertools import pairwise
from math import isfinite
from typing import TYPE_CHECKING

from ..const import HISTORY_WRITE_CYCLES, UPDATE_INTERVAL
from ..utils.history_store import HISTORY_BUCKET_SECONDS

if TYPE_CHECKING:
    from ..utils.history_store import HistoryStore

SCHEDULE_COMFORT_TOLERANCE_K = 1.0
_DETAIL_SAMPLE_SECONDS = HISTORY_WRITE_CYCLES * UPDATE_INTERVAL


def classify_schedule_comfort(temp: float | None, heat: float | None, cool: float | None) -> int | None:
    """Return -1 for too cold, 1 for too warm and 0 when within the allowed band."""
    heat = _finite_number(heat)
    cool = _finite_number(cool)
    if temp is None or not isfinite(temp) or (heat is None and cool is None):
        return None
    if heat is not None and temp < heat - SCHEDULE_COMFORT_TOLERANCE_K:
        return -1
    if cool is not None and temp > cool + SCHEDULE_COMFORT_TOLERANCE_K:
        return 1
    return 0


def _finite_number(value: object) -> float | None:
    if not isinstance(value, (str, int, float)):
        return None
    try:
        result = float(value)
    except (ValueError, OverflowError):
        return None
    return result if isfinite(result) else None


@dataclass
class _ComfortTime:
    valid: float = 0
    cold: float = 0
    warm: float = 0

    def add(self, seconds: float, valid: float, cold: float, warm: float) -> None:
        self.valid += seconds * valid
        self.cold += seconds * cold
        self.warm += seconds * warm

    def as_dict(self) -> dict[str, float | None]:
        return {
            "valid_seconds": round(self.valid, 2),
            "cold_seconds": round(self.cold, 2),
            "warm_seconds": round(self.warm, 2),
            "miss_pct": round(100 * (self.cold + self.warm) / self.valid, 2) if self.valid else None,
        }


def _by_timestamp(rows: list[dict]) -> dict[float, dict]:
    """Deduplicate repeated rows while preserving the most recently written one."""
    result: dict[float, dict] = {}
    for row in rows:
        ts = _finite_number(row.get("timestamp"))
        if ts is not None:
            result[ts] = row
    return result


def _room_summary(
    store: HistoryStore, area_id: str, start_ts: float, end_ts: float, zone: tzinfo
) -> tuple[dict, float]:
    detail = _by_timestamp(store.read_detail(area_id, None, start_ts - _DETAIL_SAMPLE_SECONDS, end_ts))
    history = _by_timestamp(store.read_history(area_id, None, start_ts - HISTORY_BUCKET_SECONDS, end_ts))
    # Rotation may briefly leave archived and detail rows overlapping.
    detail_start = min(detail, default=end_ts)
    samples = {ts: (row, HISTORY_BUCKET_SECONDS) for ts, row in history.items() if ts < detail_start}
    samples.update({ts: (row, _DETAIL_SAMPLE_SECONDS) for ts, row in detail.items()})
    totals = _ComfortTime()
    days: dict[str, dict] = {}
    for ts, next_ts in pairwise([*sorted(samples), end_ts]):
        row, max_width = samples[ts]
        valid = _finite_number(row.get("schedule_comfort_valid")) or 0.0
        cold = _finite_number(row.get("schedule_comfort_cold")) or 0.0
        warm = _finite_number(row.get("schedule_comfort_warm")) or 0.0
        if not 0 < valid <= 1 or not 0 <= cold <= valid or not 0 <= warm <= valid - cold + 1e-9:
            continue
        cursor = max(ts, start_ts)
        stop = min(next_ts, ts + max_width, end_ts)
        while cursor < stop:
            local_date = datetime.fromtimestamp(cursor, zone).date()
            midnight = datetime.combine(local_date, time.min, zone).timestamp()
            next_midnight = datetime.combine(local_date + timedelta(days=1), time.min, zone).timestamp()
            boundary = min(stop, next_midnight)
            seconds = boundary - cursor
            day = days.setdefault(
                local_date.isoformat(),
                {"start_ts": max(start_ts, midnight), "end_ts": min(end_ts, next_midnight), "time": _ComfortTime()},
            )
            day["time"].add(seconds, valid, cold, warm)
            totals.add(seconds, valid, cold, warm)
            cursor = boundary

    return {
        "area_id": area_id,
        **totals.as_dict(),
        "days": [
            {"date": date, "start_ts": data["start_ts"], "end_ts": data["end_ts"], **data["time"].as_dict()}
            for date, data in days.items()
        ],
    }, (totals.cold + totals.warm) / totals.valid if totals.valid else -1


def build_comfort_ranking(
    store: HistoryStore | None,
    area_ids: list[str],
    start_ts: float,
    end_ts: float,
    zone: tzinfo,
) -> dict:
    """Return a worst-first ranking for all rooms and daily drill-down data."""
    rows = [] if store is None else [_room_summary(store, area_id, start_ts, end_ts, zone) for area_id in area_ids]
    rows.sort(key=lambda item: (-item[1], -item[0]["valid_seconds"], item[0]["area_id"]))
    return {"start_ts": start_ts, "end_ts": end_ts, "rooms": [row for row, _ in rows]}
