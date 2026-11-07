"""Compare like-for-like local week-to-date outdoor temperature profiles."""

from __future__ import annotations

import math
from collections.abc import Iterable, Iterator
from datetime import date, datetime, time, timedelta, tzinfo
from statistics import fmean
from typing import Literal, TypedDict

from ..utils.history_store import HISTORY_MAX_AGE, HistoryStore

_BUCKET_SECONDS = 300
_MIN_BUCKETS_PER_HOUR = 6
_MIN_MATCHED_HOURS = 6
_MIN_OVERLAP = 0.8
_MAX_PRIOR_WEEKS = HISTORY_MAX_AGE // (7 * 24 * 3600)


class WeekPeriod(TypedDict):
    start_ts: float
    end_ts: float


class OutdoorProfilePoint(TypedDict):
    ts: float
    current_temp: float | None
    comparison_temp: float | None


class WeatherWeekMatch(TypedDict):
    status: Literal["matched", "insufficient_current", "no_comparable_week"]
    current: WeekPeriod
    comparison: WeekPeriod | None
    time_zone: str
    weather_mae: float | None
    matched_hours: int
    expected_hours: int
    outdoor_profile: list[OutdoorProfilePoint]


def read_weather_week_match(
    history_store: HistoryStore | None,
    area_ids: tuple[str, ...],
    now: datetime,
) -> WeatherWeekMatch:
    """Read existing room history on the executor and compute the match."""
    if history_store is None:
        return find_weather_week_match((), now)

    def rows() -> Iterator[dict]:
        start_ts = now.timestamp() - HISTORY_MAX_AGE
        for area_id in area_ids:
            for read in (history_store.read_history, history_store.read_detail):
                try:
                    yield from read(area_id, start_ts=start_ts, end_ts=now.timestamp())
                except FileNotFoundError:
                    # Room history can be deleted between the existence check and open.
                    continue

    return find_weather_week_match(rows(), now)


def find_weather_week_match(rows: Iterable[dict], now: datetime) -> WeatherWeekMatch:
    """Find the closest historical week using paired, completed local clock hours."""
    zone = now.tzinfo
    if zone is None:
        raise ValueError("Weather comparison requires a timezone-aware datetime")

    monday = _monday(now)
    start = datetime.combine(monday, time.min, zone)
    elapsed_hours = now.weekday() * 24 + now.hour
    expected = _elapsed_hour_starts(start, elapsed_hours)
    current_period = _week_period(start, elapsed_hours)

    result: WeatherWeekMatch = {
        "status": "insufficient_current",
        "current": current_period,
        "comparison": None,
        "time_zone": str(zone),
        "weather_mae": None,
        "matched_hours": 0,
        "expected_hours": len(expected),
        "outdoor_profile": [],
    }
    minimum_hours = max(_MIN_MATCHED_HOURS, math.ceil(len(expected) * _MIN_OVERLAP))
    if len(expected) < minimum_hours:
        return result

    profiles = _hourly_profiles(rows, zone, now.timestamp() - HISTORY_MAX_AGE, current_period["end_ts"])
    current = profiles.get(monday, {})
    if len(expected.keys() & current.keys()) < minimum_hours:
        return result

    result["status"] = "no_comparable_week"
    best: tuple[float, int, int] | None = None
    for weeks_ago in range(1, _MAX_PRIOR_WEEKS + 1):
        candidate_start = start - timedelta(weeks=weeks_ago)
        candidate = profiles.get(candidate_start.date(), {})
        paired = expected.keys() & current.keys() & candidate.keys()
        if len(paired) < minimum_hours:
            continue

        score = fmean(abs(current[hour] - candidate[hour]) for hour in paired)
        # Among equal errors, prefer more coverage and then the most recent week.
        rank = (score, -len(paired), weeks_ago)
        if best is None or rank < best:
            best = rank
    if best is None:
        return result

    score, negative_coverage, weeks_ago = best
    match_start = start - timedelta(weeks=weeks_ago)
    comparison = profiles[match_start.date()]
    result["status"] = "matched"
    result["comparison"] = _week_period(match_start, elapsed_hours)
    result["weather_mae"] = round(score, 2)
    result["matched_hours"] = -negative_coverage
    result["outdoor_profile"] = [
        {
            "ts": timestamp,
            "current_temp": round(current[hour], 2) if hour in current else None,
            "comparison_temp": round(comparison[hour], 2) if hour in comparison else None,
        }
        for hour, timestamp in expected.items()
    ]
    return result


def _monday(value: datetime) -> date:
    return value.date() - timedelta(days=value.weekday())


def _week_period(start: datetime, hours: int) -> WeekPeriod:
    return {"start_ts": start.timestamp(), "end_ts": (start + timedelta(hours=hours)).timestamp()}


def _elapsed_hour_starts(start: datetime, hours: int) -> dict[int, float]:
    result: dict[int, float] = {}
    for hour in range(hours):
        local = start + timedelta(hours=hour)
        timestamp = local.timestamp()
        # Spring-forward hours that never existed are not missing observations.
        if datetime.fromtimestamp(timestamp, start.tzinfo).hour == local.hour:
            result[hour] = timestamp
    return result


def _hourly_profiles(
    rows: Iterable[dict], zone: tzinfo, start_ts: float, end_ts: float
) -> dict[date, dict[int, float]]:
    buckets: dict[int, tuple[float, float]] = {}
    for row in rows:
        try:
            timestamp = float(row["timestamp"])
            temperature = float(row["outdoor_temp"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (start_ts <= timestamp < end_ts and math.isfinite(temperature)):
            continue
        # The coordinator records the same outdoor signal in each room. One
        # observation per five-minute bucket gives each time interval equal weight.
        bucket = int(timestamp // _BUCKET_SECONDS)
        if bucket not in buckets or timestamp >= buckets[bucket][0]:
            buckets[bucket] = (timestamp, temperature)

    hours: dict[tuple[date, int], list[float]] = {}
    for bucket, (_, temperature) in buckets.items():
        local = datetime.fromtimestamp(bucket * _BUCKET_SECONDS, zone)
        key = (_monday(local), local.weekday() * 24 + local.hour)
        # During fall-back, both occurrences contribute to the same local hour.
        hours.setdefault(key, []).append(temperature)

    profiles: dict[date, dict[int, float]] = {}
    for (week, hour), temperatures in hours.items():
        if len(temperatures) >= _MIN_BUCKETS_PER_HOUR:
            profiles.setdefault(week, {})[hour] = fmean(temperatures)
    return profiles
