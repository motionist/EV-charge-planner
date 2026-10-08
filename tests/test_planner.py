"""Planner core tests.

The first scenario is the week of 8–15 October 2026 from the private
installation the planner was ported from (Polestar 2, 82 kWh, 0.219 kWh/km,
Danish all-in prices). Expected values were confirmed on the original
Jinja implementation and its dashboard.
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from ev_charge_planner.planner import DayInfo, Settings, plan
from ev_charge_planner.planner.core import build_timeline, target_for

TZ = ZoneInfo("Europe/Copenhagen")
EXPENSIVE = 3.0


def make_prices(spec: dict[date, dict[str, tuple[float, int] | None]]) -> dict[tuple[date, int], float]:
    """Hourly prices: every hour EXPENSIVE except the given cheapest night/day hour."""
    prices: dict[tuple[date, int], float] = {}
    for d, parts in spec.items():
        for h in range(24):
            prices[(d, h)] = EXPENSIVE
        for part in ("night", "day"):
            if parts.get(part):
                p, h = parts[part]
                prices[(d, h)] = p
    return prices


def oct(day: int) -> date:
    return date(2026, 10, day)


SETTINGS = Settings(
    battery_kwh=82,
    efficiency_kwh_per_km=0.219,
    floor_weekday=55,
    floor_weekend=41,
    max_level=90,
    buffer_km=100,
)

WEEK_DAYS = {
    oct(8): DayInfo(oct(8), km=49.6, away_hours=[(8, 17)]),
    oct(12): DayInfo(oct(12), km=49.6, away_hours=[(8, 17)]),
    oct(13): DayInfo(oct(13), km=49.6, away_hours=[(8, 17)]),
    oct(14): DayInfo(oct(14), km=49.6, away_hours=[(8, 17)]),
    oct(15): DayInfo(oct(15), km=179.3, away_hours=[(8, 23)]),
}

WEEK_PRICES = make_prices(
    {
        oct(8): {"night": (1.461, 2)},
        oct(9): {"night": (1.02, 4), "day": (0.74, 12)},
        oct(10): {"night": (0.78, 4), "day": (0.91, 13)},
        oct(11): {"night": (1.04, 0), "day": (1.27, 13)},
        oct(12): {"night": (1.59, 3)},
        oct(13): {"night": (2.31, 3)},
        oct(14): {"night": (1.40, 5)},
        oct(15): {"night": (1.25, 5)},
    }
)


def by_day(p):
    return {r.day: r for r in p.rows}


def test_week_day_window_and_long_trip_top_up():
    now = datetime(2026, 10, 8, 14, 0, tzinfo=TZ)
    p = plan(now, 70, WEEK_DAYS, WEEK_PRICES, SETTINGS)
    rows = by_day(p)

    # Friday: no night charge, but the 0.74 day window (cheapest of the week)
    # charges to the 90 % cap.
    assert rows[oct(9)].night_target is None
    assert rows[oct(9)].day_target == 90
    assert rows[oct(9)].day_window.best_hour == 12

    # Saturday–Wednesday: nothing to do.
    for d in (10, 11, 12, 13, 14):
        assert rows[oct(d)].night_target is None, d

    # Thursday: 179 km (48 %) + 100 km buffer (27 %) → top up to 75 %
    # because the 90 % cap from Friday could not cover the whole week.
    assert rows[oct(15)].night_target == 75
    assert rows[oct(15)].soc_departure == 75

    # The next charge is Friday's day window.
    assert p.next_charge is not None and p.next_charge.kind == "day" and p.next_charge.day == oct(9)
    assert p.target_soc == 90


def test_night_mode_uses_current_night():
    """At 00:30 the night in progress belongs to today (no jump at midnight)."""
    now = datetime(2026, 10, 8, 0, 30, tzinfo=TZ)
    events, first, night_mode = build_timeline(now, WEEK_DAYS, WEEK_PRICES, SETTINGS)
    assert night_mode and first == oct(8)
    first_opp = next(i for i, e in enumerate(events) if e.kind == "opp")
    assert events[first_opp].opp.day == oct(8)
    # Friday night (1.02) is cheaper than tonight (1.461): only Thursday's floor
    # (55 %) and its trip + buffer (13 + 27 = 40 %) must be covered → 55 %.
    assert target_for(events, first_opp, WEEK_DAYS, SETTINGS) == 55
    p = plan(now, 69, WEEK_DAYS, WEEK_PRICES, SETTINGS)
    assert by_day(p)[oct(8)].night_target is None


def test_buffer_respected_and_capped():
    days = {oct(9): DayInfo(oct(9), km=120, away_hours=[(8, 18)])}
    # Friday night 1.0, every later night cheaper (0.9): only Friday is covered.
    prices = make_prices({oct(9) + timedelta(days=i): {"night": (1.0 if i == 0 else 0.9, 3)} for i in range(7)})
    now = datetime(2026, 10, 8, 20, 0, tzinfo=TZ)
    p = plan(now, 30, days, prices, SETTINGS)
    # 120 km = 32 %, + 27 % buffer = 59 % (> floor 55)
    assert by_day(p)[oct(9)].night_target == 59

    days_long = {oct(9): DayInfo(oct(9), km=300, away_hours=[(6, 22)])}
    p = plan(now, 30, days_long, prices, SETTINGS)
    # 300 km = 80 % + 27 % → capped at max level 90 %
    assert by_day(p)[oct(9)].night_target == 90


def test_away_overnight_charges_full_and_skips_away_night():
    days = {
        oct(10): DayInfo(oct(10), km=150, away_hours=[(9, 24)], away_overnight=True),
        oct(11): DayInfo(oct(11), km=150, away_hours=[(0, 18)]),
    }
    prices = make_prices({oct(9) + timedelta(days=i): {"night": (1.0, 3)} for i in range(7)})
    now = datetime(2026, 10, 9, 20, 0, tzinfo=TZ)
    p = plan(now, 40, days, prices, SETTINGS)
    rows = by_day(p)
    assert rows[oct(10)].night_target == 90
    assert rows[oct(11)].night is not None and rows[oct(11)].night.available is False


def test_calibration_early_grab():
    s = Settings(**{**SETTINGS.__dict__})
    s.calibration_interval_days = 28
    s.calibration_early_days = 15
    s.calibration_price_threshold = 0.8
    s.calibration_soc = 100
    s.last_calibration = datetime(2026, 9, 20, 3, 0, tzinfo=TZ)
    now = datetime(2026, 10, 8, 20, 0, tzinfo=TZ)
    p = plan(now, 60, {}, WEEK_PRICES, s)
    rows = by_day(p)
    # Saturday night 0.78 ≤ 0.8 and nothing later is cheaper → calibrate there,
    # not on Friday night (1.02, above the threshold).
    assert rows[oct(10)].calibration is True
    assert rows[oct(10)].night_target == 100
    assert rows[oct(9)].calibration is False
