"""Adapter tests (pure Python)."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from ev_charge_planner.adapters import prices, trips
from ev_charge_planner.planner import merge_prices

TZ = ZoneInfo("Europe/Copenhagen")


def test_legacy_trip_helpers():
    days = trips.build_days(
        "2026-10-08=49.6;2026-10-15=179.3;",
        "2026-10-08=8-17;2026-10-15=8-12;2026-10-15=14-23",
        "2026-10-10",
        holidays=[date(2026, 12, 25)],
    )
    assert days[date(2026, 10, 8)].km == 49.6
    assert days[date(2026, 10, 15)].away_hours == [(8, 12), (14, 23)]
    assert days[date(2026, 10, 10)].away_overnight is True
    assert days[date(2026, 12, 25)].holiday is True


def test_away_hours_until_midnight():
    away = trips.parse_away_hours("2026-10-17=18-0;2026-10-17=10-15;2026-10-18=5-3")
    assert away == {date(2026, 10, 17): [(18, 24), (10, 15)]}


def test_energi_data_service_known_and_forecast():
    attrs = {
        "raw_today": [{"hour": datetime(2026, 10, 8, 0, tzinfo=TZ), "price": 1.663}],
        "raw_tomorrow": [{"hour": "2026-10-09T00:00:00+02:00", "price": 1.1}],
        "forecast": [{"hour": datetime(2026, 10, 10, 0, tzinfo=TZ), "price": 1.145}],
    }
    known, fallback = prices.energi_data_service(attrs, TZ)
    assert [round(p.price, 3) for p in known] == [1.663, 1.1]
    assert fallback[0].source == "fallback"


def test_open_spot_forecast_compact_is_averaged_per_hour():
    start = int(datetime(2026, 10, 9, 3, 0, tzinfo=TZ).timestamp())
    attrs = {"predictions": {"interval_minutes": 15, "s": [start, start + 900, start + 1800, start + 2700], "t": [1.0, 1.2, 1.4, 1.6]}}
    out = prices.open_spot_forecast(attrs, TZ)
    assert len(out) == 1 and abs(out[0].price - 1.3) < 1e-9 and out[0].start.hour == 3


def test_merge_prefers_later_layers():
    fallback = [prices.HourPrice(datetime(2026, 10, 9, 3, tzinfo=TZ), 2.0, "fallback")]
    forecast = [prices.HourPrice(datetime(2026, 10, 9, 3, tzinfo=TZ), 1.5, "forecast")]
    known = [prices.HourPrice(datetime(2026, 10, 9, 3, tzinfo=TZ), 1.2, "known")]
    assert merge_prices(fallback, forecast, known, tz=TZ)[(date(2026, 10, 9), 3)] == 1.2
    assert merge_prices(fallback, forecast, tz=TZ)[(date(2026, 10, 9), 3)] == 1.5
