"""High cap before long trips, the way home and planned energy/cost (DESIGN §5.1, §5.4, §5.5).

Based on the week of 13–16 October 2026 in the private installation: a short
trip on Wednesday, 176 km on Thursday (away until midnight) and the weekday
floor on Friday morning.
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from ev_charge_planner.planner import DayInfo, Settings, build_slots, plan, remaining_km

TZ = ZoneInfo("Europe/Copenhagen")
EXPENSIVE = 3.0


def oct(day: int) -> date:
    return date(2026, 10, day)


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=TZ)


def prices(cheap: dict[tuple[date, int], float]) -> dict[tuple[date, int], float]:
    out = {(oct(13) + timedelta(days=i), h): EXPENSIVE for i in range(6) for h in range(24)}
    out.update(cheap)
    return out


S = Settings(
    battery_kwh=82,
    efficiency_kwh_per_km=0.224,
    floor_weekday=45,
    floor_weekend=50,
    max_level=90,
    buffer_km=100,
    charge_kw=10.4,
    horizon_days=4,
)
PCT_KM = S.pct_per_km()
DAYS = {
    oct(14): DayInfo(oct(14), km=41.5, away_hours=[(8, 17)]),
    oct(15): DayInfo(oct(15), km=176, away_hours=[(10, 0)]),  # until midnight
}
WED_NIGHT = {(oct(14), h): 1.3 for h in range(5)}


def by_day(p):
    return {r.day: r for r in p.rows}


def max_level_before(p, now, soc, day: date) -> float:
    """Highest SoC from now until the departure on ``day``."""
    charged = {c.start: c.pct for c in p.hours}
    level, top = soc, soc
    for slot in build_slots(now, DAYS, prices({}), prices({}), None, S):
        if slot.departs and slot.start.date() == day:
            return top
        level += charged.get(slot.start, 0.0)
        top = max(top, level)
        level -= slot.use
    return top


def test_no_high_cap_days_before_the_long_trip():
    """Cheap known Wednesday night, Thursday night only slightly cheaper than Friday.

    Wednesday night may charge for Thursday, but only up to the max level: the
    next departure after it is Wednesday's short trip, not the long one.
    """
    pr = prices({**WED_NIGHT, **{(oct(15), h): 1.9 for h in range(10)}, **{(oct(16), h): 2.0 for h in range(8)}})
    p = plan(at(13, 23), 54, DAYS, pr, S)
    assert max_level_before(p, at(13, 23), 54, oct(14)) <= S.max_level + 1e-6
    rows = by_day(p)
    assert rows[oct(15)].soc_departure <= S.max_level + 1e-6
    # Saturday's floor (50 %) is reached without any expensive hour.
    assert rows[oct(17)].soc_departure == pytest.approx(50, abs=0.1)
    assert all(c.raw_price < EXPENSIVE for c in p.hours)
    assert p.unmet == 0


def test_high_cap_right_before_a_long_trip_when_much_cheaper():
    """Thursday night is cheap, Friday night dearer: charge above 90 % before the trip.

    It covers Friday's and Saturday's floors after the trip, so nothing is
    charged at the dearer hours later.
    """
    pr = prices({**{(oct(15), h): 1.0 for h in range(10)}, **{(oct(16), h): 2.5 for h in range(8)}})
    p = plan(at(14, 18), 60, DAYS, pr, S)
    rows = by_day(p)
    thu = rows[oct(15)]
    assert S.max_level < thu.soc_departure <= S.max_level_long_trip + 1e-6
    # Back within the max level after the trip, and Friday's floor needs no dear hour.
    assert thu.soc_departure - 176 * PCT_KM <= S.max_level + 1e-6
    assert p.hours and all(c.start.date() == oct(15) for c in p.hours)
    assert rows[oct(17)].soc_departure == pytest.approx(50, abs=0.1)


def test_high_cap_needs_the_price_advantage():
    """1.0 against 2.5 is 60 % cheaper; with an 80 % threshold the cap stays at 90 %."""
    pr = prices({**{(oct(15), h): 1.0 for h in range(10)}, **{(oct(16), h): 2.5 for h in range(8)}})
    strict = Settings(**{**S.__dict__, "long_trip_advantage_pct": 80})
    rows = by_day(plan(at(14, 18), 60, DAYS, pr, strict))
    assert rows[oct(15)].soc_departure <= S.max_level + 1e-6
    assert rows[oct(16)].charge_hours  # the floor is charged Friday night instead


def test_high_cap_only_for_long_trips():
    pr = prices({**{(oct(15), h): 1.0 for h in range(10)}, **{(oct(16), h): 2.5 for h in range(8)}})
    short = Settings(**{**S.__dict__, "long_trip_km": 200})  # 176 km is not long
    rows = by_day(plan(at(14, 18), 60, DAYS, pr, short))
    assert rows[oct(15)].soc_departure <= S.max_level + 1e-6


def test_remaining_km():
    days = {oct(14): DayInfo(oct(14), km=42, away_hours=[(8, 17)])}
    # Away per calendar: at least the return leg (half the day's km).
    assert remaining_km(at(14, 12), days, car_home=False, distance_km=5) == pytest.approx(21)
    # Far from home: the road distance (1.3 × straight line) is larger.
    assert remaining_km(at(14, 12), days, car_home=False, distance_km=30) == pytest.approx(39)
    # Not in the calendar, location unknown: nothing to subtract.
    assert remaining_km(at(14, 18), days, car_home=None, distance_km=None) == 0
    # A spontaneous trip: only the way home.
    assert remaining_km(at(14, 18), days, car_home=False, distance_km=10) == pytest.approx(13)
    assert remaining_km(at(14, 12), days, car_home=True, distance_km=None) == 0


def test_away_now_takes_the_way_home_from_the_soc():
    """86 % at noon during a 42 km round trip: about 80 % when home."""
    days = {**DAYS, oct(14): DayInfo(oct(14), km=41.5, away_hours=[(8, 17)])}
    p = plan(at(14, 12), 86, days, prices({}), S, car_home=False, distance_km=5.3)
    assert p.remaining_km == pytest.approx(20.75, abs=0.1)
    assert p.soc_home == pytest.approx(86 - 20.75 * PCT_KM, abs=0.1)
    assert by_day(p)[oct(15)].soc_departure == pytest.approx(p.soc_home, abs=0.1)


def test_later_trip_today_is_a_departure():
    """The morning trip has started; the afternoon trip at 14 still needs its requirement."""
    days = {oct(14): DayInfo(oct(14), km=60, away_hours=[(8, 10), (14, 18)])}
    slots = build_slots(at(14, 11), days, prices({}), prices({}), None, S)
    deps = [sl.start for sl in slots if sl.departs and sl.start.date() == oct(14)]
    assert deps == [at(14, 14)]


def test_todays_km_hidden_when_trips_are_done():
    days = {oct(14): DayInfo(oct(14), km=41.5, away_hours=[(8, 17)])}
    rows = by_day(plan(at(14, 20), 70, days, prices({}), S, car_home=True))
    assert rows[oct(14)].km == 0
    rows = by_day(plan(at(14, 12), 70, days, prices({}), S, car_home=False, distance_km=5))
    assert rows[oct(14)].km == 41.5


def test_energy_and_cost_by_price_basis():
    """kWh and cost per hour, split into known and forecast prices."""
    pr = prices({**WED_NIGHT, **{(oct(16), h): 2.0 for h in range(8)}})
    p = plan(at(13, 23), 54, DAYS, pr, S, known_until=oct(14))
    kwh = sum(c.kwh for c in p.hours)
    assert p.kwh_known + p.kwh_forecast == pytest.approx(kwh, abs=0.05)
    assert p.kwh_known == pytest.approx(sum(c.kwh for c in p.hours if c.known), abs=0.05)
    assert p.cost_known == pytest.approx(sum(c.kwh * c.raw_price for c in p.hours if c.known), abs=0.05)
    assert all(c.kwh == pytest.approx(c.pct * S.battery_kwh / 100, abs=0.01) for c in p.hours)
    row = by_day(p)[oct(14)]
    assert row.charge_kwh == pytest.approx(sum(c.kwh for c in row.charge_hours), abs=0.05)
    assert row.cost == pytest.approx(sum(c.kwh * c.raw_price for c in row.charge_hours), abs=0.05)
