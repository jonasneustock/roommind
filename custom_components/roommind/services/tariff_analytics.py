"""Compare observed electricity tariffs with logged room heating activity."""

from __future__ import annotations

import math
from itertools import pairwise
from typing import TypedDict

from ..const import MODE_COOLING, MODE_HEATING, MODE_IDLE
from ..utils.history_store import HISTORY_MAX_AGE, HistoryStore
from .analytics_service import _safe_float
from .analytics_stats import MAX_SAMPLE_GAP_S

# An extra two hours permits a 90-day calendar selection across a clock change.
MAX_TARIFF_RANGE_S = HISTORY_MAX_AGE + 7200


class TariffSegment(TypedDict):
    start_ts: float
    end_ts: float
    price: float | None
    unit: str | None
    heating: bool


def build_tariff_period(
    store: HistoryStore | None,
    area_id: str,
    start_ts: float,
    end_ts: float,
    cutoffs: tuple[float, float] | None,
) -> dict:
    """Read one range plus adjacent samples so boundary intervals can be clipped."""
    rows: list[dict] = []
    if store is not None:
        read_start, read_end = start_ts - MAX_SAMPLE_GAP_S, end_ts + MAX_SAMPLE_GAP_S
        # Rotation appends to the archive before trimming detail. Reading
        # detail first can produce duplicates but avoids missing those samples.
        detail = store.read_detail(area_id, start_ts=read_start, end_ts=read_end)
        rows = store.read_history(area_id, start_ts=read_start, end_ts=read_end) + detail
    return summarize_tariff_period(rows, start_ts, end_ts, cutoffs)


def summarize_tariff_period(
    rows: list[dict],
    start_ts: float,
    end_ts: float,
    cutoffs: tuple[float, float] | None,
) -> dict:
    """Weight logged samples by elapsed time and keep unlike units separate."""
    observations: dict[float, dict] = {}
    for row in rows:
        ts = _finite_float(row.get("timestamp", ""))
        if ts is not None:
            observations[ts] = row

    segments: list[TariffSegment] = []
    monitored_seconds = heating_seconds = 0.0
    units: dict[str | None, dict] = {}

    for ts, next_ts in pairwise(sorted(observations)):
        start, end = max(ts, start_ts), min(next_ts, end_ts)
        if end <= start or next_ts - ts > MAX_SAMPLE_GAP_S:
            continue
        row = observations[ts]
        mode = row.get("mode")
        if mode not in (MODE_IDLE, MODE_HEATING, MODE_COOLING):
            continue

        seconds = end - start
        heating = mode == MODE_HEATING
        price = _finite_float(row.get("electricity_price", ""))
        unit = (row.get("electricity_price_unit") or None) if price is not None else None
        monitored_seconds += seconds
        if heating:
            heating_seconds += seconds

        if price is not None:
            summary = units.setdefault(
                unit,
                {
                    "unit": unit,
                    "priced_heating_seconds": 0.0,
                    "weighted_price_sum": 0.0,
                    "band_seconds": {"cheap": 0.0, "typical": 0.0, "expensive": 0.0},
                },
            )
            if heating:
                summary["priced_heating_seconds"] += seconds
                summary["weighted_price_sum"] += price * seconds
                if cutoffs is not None:
                    low, high = cutoffs
                    band = "cheap" if price <= low else "expensive" if price >= high else "typical"
                    summary["band_seconds"][band] += seconds

        if (
            segments
            and segments[-1]["end_ts"] == start
            and segments[-1]["price"] == price
            and segments[-1]["unit"] == unit
            and segments[-1]["heating"] == heating
        ):
            segments[-1]["end_ts"] = end
        else:
            segments.append({"start_ts": start, "end_ts": end, "price": price, "unit": unit, "heating": heating})

    for summary in units.values():
        weighted_price = summary.pop("weighted_price_sum")
        seconds = summary["priced_heating_seconds"]
        summary["average_tariff"] = weighted_price / seconds if seconds else None

    return {
        "start_ts": start_ts,
        "end_ts": end_ts,
        "monitored_seconds": monitored_seconds,
        "heating_seconds": heating_seconds,
        "units": list(units.values()),
        "segments": segments,
    }


def _finite_float(value: str) -> float | None:
    result = _safe_float(value)
    return result if result is not None and math.isfinite(result) else None
