"""Forecast price adjustment (DESIGN §5.0): a known price weighs more than a forecast."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from ev_charge_planner.planner import DayInfo, Settings, adjust_prices, forecast_factors, plan

TZ = ZoneInfo("Europe/Copenhagen")
BASE = 1.30  # flat all-in price of every hour not given below


def oct(day: int) -> date:
    return date(2026, 10, day)


def prices_for(first: date, ndays: int, cheap: dict[tuple[date, int], float]) -> dict[tuple[date, int], float]:
    out = {(first + timedelta(days=i), h): BASE for i in range(ndays) for h in range(24)}
    out.update(cheap)
    return out


def settings(mae=(0.21, 0.27, 0.28, 0.36), min_accuracy=90.0) -> Settings:
    return Settings(
        battery_kwh=82,
        efficiency_kwh_per_km=0.219,
        floor_weekday=55,
        floor_weekend=41,
        max_level=90,
        buffer_km=100,
        forecast_mae=mae,
        forecast_min_accuracy=min_accuracy,
    )


def by_day(p):
    return {r.day: r for r in p.rows}


# --------------------------------------------------------------------------- #
# Factors and adjusted prices
# --------------------------------------------------------------------------- #


def test_factor_per_lead_day_and_far_factor():
    prices = prices_for(oct(9), 8, {})
    f = forecast_factors(prices, oct(9), settings())
    # Known mean = 1.30 → accuracy = 1 − MAE / 1.30
    assert oct(9) not in f  # known date is never adjusted
    assert f[oct(10)] == pytest.approx(1 - 0.21 / 1.30)
    assert f[oct(13)] == pytest.approx(1 - 0.36 / 1.30)
    # Lead 5+ uses the day-4 MAE × forecast_far_factor (1.25)
    assert f[oct(14)] == pytest.approx(1 - 0.36 * 1.25 / 1.30)
    assert f[oct(16)] == f[oct(14)]


def test_accurate_forecast_or_no_mae_is_not_adjusted():
    prices = prices_for(oct(9), 4, {})
    assert forecast_factors(prices, oct(9), settings(mae=(0.05,))) == {}  # 96 % ≥ 90 %
    assert forecast_factors(prices, oct(9), settings(mae=())) == {}
    assert forecast_factors(prices, None, settings()) == {}


def test_factor_floor_and_negative_prices():
    prices = prices_for(oct(9), 2, {})
    f = forecast_factors(prices, oct(9), settings(mae=(2.0,)))  # accuracy < 0
    assert f[oct(10)] == 0.3
    adj = adjust_prices({(oct(10), 1): 0.8, (oct(10), 2): -0.1, (oct(9), 1): 0.8}, {oct(10): 0.8})
    assert adj[(oct(10), 1)] == pytest.approx(1.0)  # 0.8 ÷ 0.8
    assert adj[(oct(10), 2)] == pytest.approx(-0.08)  # negative: × 0.8, not more attractive
    assert adj[(oct(9), 1)] == 0.8  # known date untouched


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #

# Friday 9/10 night (known) 0.74, Saturday 10/10 night (forecast) 0.65 and a
# clearly cheaper Sunday night (forecast 0.40 ⇒ adjusted ≈ 0.51) that ends the
# coverage. Saturday: 150 km = 40 % + 100 km buffer 27 % → 67 %.
NIGHTS = prices_for(oct(9), 8, {(oct(9), 3): 0.74, (oct(10), 3): 0.65, (oct(11), 3): 0.40})
TRIP_SAT = {oct(10): DayInfo(oct(10), km=150, away_hours=[(9, 18)])}
EVENING_THU = datetime(2026, 10, 8, 20, 0, tzinfo=TZ)


def test_known_night_beats_slightly_cheaper_forecast():
    """0.65 forecast at 84 % accuracy ⇒ 0.77 > 0.74 known ⇒ Friday covers Saturday."""
    p = plan(EVENING_THU, 60, TRIP_SAT, NIGHTS, settings(), known_until=oct(9))
    rows = by_day(p)
    assert rows[oct(9)].night_target == 67
    assert rows[oct(10)].night_target is None
    sat = rows[oct(10)].night
    assert sat.raw_price == 0.65 and sat.price == pytest.approx(0.65 / (1 - 0.21 / sat_mean()))
    assert rows[oct(9)].night.factor == 1.0


def sat_mean() -> float:
    known = [p for (d, _h), p in NIGHTS.items() if d <= oct(9)]
    return sum(known) / len(known)


def test_without_adjustment_planner_waits_for_forecast():
    """Same week, forecast trusted: Friday only covers Friday, Saturday charges."""
    p = plan(EVENING_THU, 60, TRIP_SAT, NIGHTS, settings(mae=()), known_until=oct(9))
    rows = by_day(p)
    assert rows[oct(9)].night_target is None  # 60 % ≥ Friday's floor, nothing else before Saturday night
    assert rows[oct(10)].night_target == 67


def test_known_day_window_beats_forecast_night():
    """Friday 10:00, tomorrow not yet published: a known 0.739 at noon beats a
    0.664 forecast tonight at 84 % accuracy (adjusted 0.79)."""
    now = datetime(2026, 10, 9, 10, 0, tzinfo=TZ)
    prices = prices_for(oct(9), 8, {(oct(9), 12): 0.739, (oct(10), 3): 0.664, (oct(11), 3): 0.40})
    p = plan(now, 50, TRIP_SAT, prices, settings(), known_until=oct(9))
    assert p.next_charge is not None
    assert p.next_charge.kind == "day" and p.next_charge.day == oct(9)
    assert p.target_soc == 67

    trusted = plan(now, 50, TRIP_SAT, prices, settings(mae=()), known_until=oct(9))
    assert trusted.next_charge.kind == "night" and trusted.next_charge.day == oct(10)
