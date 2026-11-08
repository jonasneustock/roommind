"""Regression tests for observed tariff coverage and archive preservation."""

from __future__ import annotations

import csv
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.roommind.const import DOMAIN
from custom_components.roommind.control.electricity_tariff import read_current_electricity_price
from custom_components.roommind.services.tariff_analytics import build_tariff_period, summarize_tariff_period
from custom_components.roommind.utils.history_store import HistoryStore
from custom_components.roommind.websocket_api import websocket_get_tariff_analytics


def _sample(ts: float, mode: str = "heating", price: float | None = None, unit: str = "EUR/kWh") -> dict:
    return {
        "timestamp": str(ts),
        "mode": mode,
        "electricity_price": "" if price is None else str(price),
        "electricity_price_unit": unit,
    }


def test_weighting_clips_boundaries_counts_coverage_and_keeps_units_apart() -> None:
    report = summarize_tariff_period(
        [
            _sample(100, price=-0.1),
            _sample(200, price=0.3),
            _sample(400, price=0.2),
            _sample(500),
            _sample(600, price=25, unit="cent/kWh"),
            _sample(700, mode="idle", price=25, unit="cent/kWh"),
            _sample(800, price=0.2),
            _sample(2000, price=0.9),
        ],
        150,
        2100,
        (0.0, 0.3),
    )
    assert report["monitored_seconds"] == 650
    assert report["heating_seconds"] == 550
    eur, cents = report["units"]
    assert eur == {
        "unit": "EUR/kWh",
        "priced_heating_seconds": 350,
        "average_tariff": pytest.approx(75 / 350),
        "band_seconds": {"cheap": 50, "typical": 100, "expensive": 200},
    }
    assert cents["priced_heating_seconds"] == 100
    assert cents["average_tariff"] == 25
    assert report["segments"][0] == {
        "start_ts": 150,
        "end_ts": 200,
        "price": -0.1,
        "unit": "EUR/kWh",
        "heating": True,
    }
    assert report["segments"][3]["price"] is None
    assert report["segments"][-1]["end_ts"] == 800


def test_duplicate_samples_prefer_detail_and_merge_equal_observations() -> None:
    report = summarize_tariff_period(
        [
            _sample(0, price=0.4),
            _sample(60, price=0.2),
            _sample(0, price=0.2),
            _sample(120, mode="idle", price=0.2),
        ],
        0,
        110,
        None,
    )
    assert report["heating_seconds"] == 110
    assert report["units"][0]["average_tariff"] == pytest.approx(0.2)
    assert len(report["segments"]) == 1
    assert report["segments"][0]["end_ts"] == 110


def test_current_price_reader_works_without_forecast_and_rejects_unavailable() -> None:
    hass = MagicMock()
    hass.states.get.return_value = SimpleNamespace(state="-0.0568", attributes={"unit_of_measurement": "EUR/kWh"})
    assert read_current_electricity_price(hass, "sensor.price") == (-0.0568, "EUR/kWh")
    hass.states.get.return_value.state = "unavailable"
    assert read_current_electricity_price(hass, "sensor.price") == (None, "EUR/kWh")
    assert read_current_electricity_price(hass, "") == (None, None)


def test_rotation_keeps_exact_prices_and_mode_boundaries_with_old_history(tmp_path) -> None:
    store = HistoryStore(str(tmp_path))
    base = int((time.time() - 3 * 86400) // 300) * 300
    archive = tmp_path / "living_history.csv"
    with archive.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["timestamp", "mode"])
        writer.writeheader()
        writer.writerow({"timestamp": base - 360, "mode": "heating"})
        writer.writerow({"timestamp": base - 180, "mode": "heating"})
    store.record("living", _sample(base + 20, "idle", 0.12345), base + 20)
    store.record("living", _sample(base + 200, "heating", 0.23456), base + 200)
    store.record("living", _sample(base + 380, "heating", 0.23456), base + 380)
    store.rotate("living")

    rows = store.read_history("living")
    assert rows[0]["electricity_price"] == ""
    assert [(float(row["timestamp"]), row["electricity_price"], row["mode"]) for row in rows[2:]] == [
        (base + 20, "0.12345", "idle"),
        (base + 200, "0.23456", "heating"),
        (base + 380, "0.23456", "heating"),
    ]
    report = build_tariff_period(store, "living", base - 360, base + 380, (0.13, 0.23))
    assert report["heating_seconds"] == 560
    assert report["units"][0]["priced_heating_seconds"] == 180
    assert report["units"][0]["average_tariff"] == pytest.approx(0.23456)


@pytest.mark.asyncio
async def test_websocket_applies_same_cutoffs_to_both_periods(hass, store, tmp_path) -> None:
    await store.async_load()
    await store.async_save_room("living", {})
    history = HistoryStore(str(tmp_path))
    history.record("living", _sample(100, price=0.2), 100)
    history.record("living", _sample(200, price=0.2), 200)
    history.record("living", _sample(300, price=0.4), 300)
    history.record("living", _sample(400, price=0.4), 400)
    hass.data[DOMAIN] = {"store": store, "coordinator": SimpleNamespace(history_store=history)}
    msg = {
        "id": 1,
        "type": "roommind/analytics/tariffs",
        "area_id": "living",
        "current": {"start_ts": 100.0, "end_ts": 200.0},
        "comparison": {"start_ts": 300.0, "end_ts": 400.0},
        "cheap_max": 0.2,
        "expensive_min": 0.4,
    }
    conn = MagicMock()
    await websocket_get_tariff_analytics.__wrapped__(hass, conn, msg)
    conn.send_error.assert_not_called()
    current, comparison = conn.send_result.call_args.args[1]["periods"]
    assert current["units"][0]["band_seconds"] == {"cheap": 100, "typical": 0, "expensive": 0}
    assert comparison["units"][0]["band_seconds"] == {"cheap": 0, "typical": 0, "expensive": 100}
