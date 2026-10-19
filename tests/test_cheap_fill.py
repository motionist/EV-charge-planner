"""Cheap fill: fill up when an hour is much cheaper than the next 48 hours (DESIGN §5.1a)."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from ev_charge_planner.planner import DayInfo, Settings, plan

TZ = ZoneInfo("Europe/Copenhagen")
NORMAL = 2.0


def oct(day: int) -> date:
    return date(2026, 10, day)


def at(day: int, hour: int) -> datetime:
    return datetime(2026, 10, day, hour, tzinfo=TZ)


def prices(cheap: dict[tuple[date, int], float]) -> dict[tuple[date, int], float]:
    out = {(oct(19) + timedelta(days=i), h): NORMAL for i in range(6) for h in range(24)}
    out.update(cheap)
    return out


S = Settings(
    battery_kwh=82,
    efficiency_kwh_per_km=0.22,
    floor_weekday=30,
    floor_weekend=30,
    max_level=90,
    buffer_km=0,
    charge_kw=11,
    horizon_days=4,
    max_level_long_trip=90,
)
# One short trip; the floors are covered by the current SoC, so the planner
# alone would not charge at all.
DAYS = {oct(20): DayInfo(oct(20), km=30, away_hours=[(8, 17)])}
CHEAP_NIGHT = {(oct(20), h): 1.0 for h in range(2, 6)}  # 50 % below the next 48 h


def by_day(p):
    return {r.day: r for r in p.rows}


def test_fills_to_the_max_level_in_a_much_cheaper_hour():
    p = plan(at(19, 22), 50, DAYS, prices(CHEAP_NIGHT), S)
    fill = [c for c in p.hours if c.cheap_fill]
    assert fill and all(c.raw_price == 1.0 for c in fill)
    assert by_day(p)[oct(20)].soc_departure == pytest.approx(S.max_level, abs=0.1)
    assert all(c.start.date() == oct(20) for c in p.hours)


def test_fill_replaces_dearer_charging_for_later_trips():
    """A later trip was charged at normal price; the fill makes that redundant."""
    days = {**DAYS, oct(21): DayInfo(oct(21), km=150, away_hours=[(8, 20)])}
    p = plan(at(19, 22), 50, days, prices(CHEAP_NIGHT), S)
    assert all(c.raw_price == 1.0 for c in p.hours)
    assert p.unmet == 0


def test_no_fill_with_a_small_price_difference():
    cheap = {(oct(20), h): 1.8 for h in range(2, 6)}  # only 10 % cheaper
    p = plan(at(19, 22), 50, DAYS, prices(cheap), S)
    assert not any(c.cheap_fill for c in p.hours)
    assert by_day(p)[oct(20)].soc_departure < S.max_level


def test_zero_disables_the_fill():
    off = Settings(**{**S.__dict__, "cheap_fill_advantage_pct": 0})
    p = plan(at(19, 22), 50, DAYS, prices(CHEAP_NIGHT), off)
    assert not any(c.cheap_fill for c in p.hours)
    assert by_day(p)[oct(20)].soc_departure < S.max_level


def test_no_fill_without_prices_for_the_whole_window():
    """Prices end the day after the cheap night: the comparison is incomplete."""
    pr = {k: v for k, v in prices(CHEAP_NIGHT).items() if k[0] <= oct(20)}
    p = plan(at(19, 22), 50, DAYS, pr, S)
    assert not any(c.cheap_fill for c in p.hours)


def test_never_above_the_max_level():
    p = plan(at(19, 22), 85, DAYS, prices(CHEAP_NIGHT), S)
    assert sum(c.pct for c in p.hours) == pytest.approx(S.max_level - 85, abs=0.01)


def test_waits_for_a_cheaper_hour_within_the_window():
    """Monday night is cheap, but Tuesday night is cheaper still: fill then."""
    cheaper = {(oct(21), h): 0.6 for h in range(2, 6)}
    p = plan(at(19, 22), 50, DAYS, prices({**CHEAP_NIGHT, **cheaper}), S)
    fill = [c for c in p.hours if c.cheap_fill]
    assert fill and all(c.start.date() == oct(21) for c in fill)


def test_a_whole_cheap_night_fills_not_only_its_cheapest_hour():
    night = {(oct(20), 2): 0.6, (oct(20), 3): 0.55, (oct(20), 4): 0.5, (oct(20), 5): 0.6}
    p = plan(at(19, 22), 50, DAYS, prices(night), S)
    assert by_day(p)[oct(20)].soc_departure == pytest.approx(S.max_level, abs=0.1)
    assert len([c for c in p.hours if c.cheap_fill]) >= 3
