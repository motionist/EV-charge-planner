"""Planner core tests (hourly price plan, DESIGN §5).

The first scenario is the week of 8–15 October 2026 from the private
installation the planner was ported from (Polestar 2, 82 kWh, 0.219 kWh/km,
10.4 kW ≈ 12.7 %/h, Danish all-in prices).
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from ev_charge_planner.planner import DayInfo, Settings, build_slots, plan, requirement

TZ = ZoneInfo("Europe/Copenhagen")
EXPENSIVE = 3.0


def oct(day: int) -> date:
    return date(2026, 10, day)


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=TZ)


def make_prices(first: date, ndays: int, cheap: dict[tuple[date, int], float]) -> dict[tuple[date, int], float]:
    """Every hour EXPENSIVE except the given (date, hour) prices."""
    prices = {(first + timedelta(days=i), h): EXPENSIVE for i in range(ndays) for h in range(24)}
    prices.update(cheap)
    return prices


SETTINGS = Settings(
    battery_kwh=82,
    efficiency_kwh_per_km=0.219,
    floor_weekday=55,
    floor_weekend=41,
    max_level=90,
    buffer_km=100,
    charge_kw=10.4,
    max_level_long_trip=90,  # high cap off here; see test_high_cap.py
)
RATE = SETTINGS.charge_pct_per_hour()  # ≈ 12.68 %/h

WEEK_DAYS = {
    oct(8): DayInfo(oct(8), km=49.6, away_hours=[(8, 17)]),
    oct(12): DayInfo(oct(12), km=49.6, away_hours=[(8, 17)]),
    oct(13): DayInfo(oct(13), km=49.6, away_hours=[(8, 17)]),
    oct(14): DayInfo(oct(14), km=49.6, away_hours=[(8, 17)]),
    oct(15): DayInfo(oct(15), km=179.3, away_hours=[(8, 23)]),
}

WEEK_PRICES = make_prices(
    oct(8),
    9,
    {
        (oct(8), 2): 1.461,
        (oct(9), 4): 1.02,
        (oct(9), 12): 0.74,
        (oct(10), 4): 0.78,
        (oct(10), 13): 0.91,
        (oct(11), 0): 1.04,
        (oct(11), 13): 1.27,
        (oct(12), 3): 1.59,
        (oct(13), 3): 2.31,
        (oct(14), 5): 1.40,
        (oct(15), 5): 1.25,
    },
)


def by_day(p):
    return {r.day: r for r in p.rows}


def hours_of(row):
    return [ch.start.hour for ch in row.charge_hours]


def test_requirement():
    # Floor, trips + buffer, max level before an away night – capped at max.
    assert requirement(oct(9), None, SETTINGS) == 55
    assert requirement(oct(10), None, SETTINGS) == 41
    assert requirement(oct(15), WEEK_DAYS[oct(15)], SETTINGS) == pytest.approx(179.3 * 0.219 / 82 * 100 + 100 * 0.219 / 82 * 100)
    assert requirement(oct(9), DayInfo(oct(9), km=400), SETTINGS) == 90
    assert requirement(oct(9), DayInfo(oct(9), away_overnight=True), SETTINGS) == 90


def test_week_cheapest_hours_and_long_trip():
    p = plan(at(8, 18), 70, WEEK_DAYS, WEEK_PRICES, SETTINGS)
    rows = by_day(p)

    # Friday noon (0.74, cheapest of the week) and Saturday 04 (0.78) charge
    # to the 90 % cap; nothing is charged at the dearer hours in between.
    assert hours_of(rows[oct(9)]) == [12] and rows[oct(9)].charge_pct == pytest.approx(RATE, abs=0.01)
    assert hours_of(rows[oct(10)]) == [4] and rows[oct(10)].charge_pct == pytest.approx(90 - 70 - RATE, abs=0.01)
    assert rows[oct(11)].soc_departure == 90 and rows[oct(11)].status == "covered"

    # 90 % cannot cover Monday–Thursday incl. the 179 km trip + buffer
    # (74.6 %): Thursday 05 (1.25) charges a full hour, the rest Wednesday 05
    # (1.40) – not the dearer Monday/Tuesday nights.
    assert hours_of(rows[oct(15)]) == [5] and rows[oct(15)].charge_pct == pytest.approx(RATE, abs=0.01)
    assert hours_of(rows[oct(14)]) == [5]
    assert rows[oct(12)].charge_hours == [] and rows[oct(13)].charge_hours == []
    assert rows[oct(15)].soc_departure == pytest.approx(74.6, abs=0.1)
    assert p.unmet == 0

    # Hand-over: nothing to charge before Friday's departure at 08.
    assert p.target_soc == 70 and p.charge_now == 0
    assert p.charge_until == at(9, 8)
    assert p.next_charge is not None and p.next_charge.start == at(9, 12)


def test_never_above_max_level():
    p = plan(at(8, 18), 70, WEEK_DAYS, WEEK_PRICES, SETTINGS)
    level = 70.0
    for slot, ch in ((s, {c.start: c.pct for c in p.hours}.get(s.start, 0.0)) for s in build_slots(at(8, 18), WEEK_DAYS, WEEK_PRICES, WEEK_PRICES, None, SETTINGS)):
        level += ch
        assert level <= 90 + 1e-6
        level -= slot.use


FAST = Settings(**{**SETTINGS.__dict__, "charge_kw": 50, "horizon_days": 3})  # ≈ 61 %/h: one hour suffices
TRIPS = {oct(9): DayInfo(oct(9), km=100, away_hours=[(8, 18)]), oct(10): DayInfo(oct(10), km=100, away_hours=[(8, 18)])}
TRIP_PCT = 100 * 0.219 / 82 * 100  # 26.7 %, also the 100 km buffer


def test_charges_only_to_the_next_cheaper_hour():
    """Saturday 03 is cheaper: Friday 03 only charges for Friday."""
    prices = make_prices(oct(8), 4, {(oct(9), 3): 1.0, (oct(10), 3): 0.5})
    p = plan(at(8, 20), 40, TRIPS, prices, FAST)
    rows = by_day(p)
    assert hours_of(rows[oct(9)]) == [3] and rows[oct(9)].charge_pct == pytest.approx(15, abs=0.01)  # → floor 55
    # Saturday 03 covers Saturday's trip + buffer and Sunday's floor (41 %).
    assert hours_of(rows[oct(10)]) == [3] and rows[oct(10)].soc_departure == pytest.approx(41 + TRIP_PCT, abs=0.1)
    # Friday 03 is a known price before Friday's departure → handed over.
    assert p.target_soc == 55 and p.charge_until == at(9, 8)


def test_tops_up_when_nothing_cheaper_comes():
    """Saturday is dearer: Friday 03 charges for Saturday and Sunday too, up to 90 %."""
    prices = make_prices(oct(8), 4, {(oct(9), 3): 1.0, (oct(10), 3): 1.5})
    p = plan(at(8, 20), 40, TRIPS, prices, FAST)
    rows = by_day(p)
    assert rows[oct(9)].soc_departure == pytest.approx(90, abs=0.01)
    # Sunday's floor (41 %) needs 4.4 % more than 90 % can carry: Saturday 03.
    assert hours_of(rows[oct(10)]) == [3]
    assert rows[oct(10)].charge_pct == pytest.approx(41 - (90 - 2 * TRIP_PCT), abs=0.01)
    assert rows[oct(11)].soc_departure == pytest.approx(41, abs=0.01)
    assert p.target_soc == 90


def test_long_trip_capped_and_unmet():
    days = {oct(9): DayInfo(oct(9), km=300, away_hours=[(8, 22)])}
    prices = make_prices(oct(8), 4, {(oct(9), 3): 1.0})
    p = plan(at(8, 20), 30, days, prices, FAST)
    # 300 km = 80 % + 27 % buffer → capped at 90 %
    assert by_day(p)[oct(9)].requirement == 90 and p.target_soc == 90 and p.unmet == 0

    # Only one hour at 12.7 %/h left before departure: the rest is unmet.
    slow = Settings(**{**SETTINGS.__dict__, "horizon_days": 2})
    p = plan(at(9, 7), 30, days, prices, slow)
    assert p.target_soc == round(30 + RATE)
    assert p.unmet == pytest.approx(90 - 30 - RATE, abs=0.1)


def test_away_overnight_charges_full_and_skips_away_hours():
    days = {
        oct(10): DayInfo(oct(10), km=150, away_hours=[(9, 24)], away_overnight=True),
        oct(11): DayInfo(oct(11), km=150, away_hours=[(0, 18)]),
    }
    prices = make_prices(oct(9), 5, {(oct(10), 3): 1.0})
    p = plan(at(9, 20), 40, days, prices, FAST)
    rows = by_day(p)
    assert rows[oct(10)].requirement == 90 and rows[oct(10)].soc_departure == pytest.approx(90, abs=0.01)
    # Sunday: the car comes home at 18 – no departure from home, nothing charged while away.
    assert rows[oct(11)].requirement is None and rows[oct(11)].departure_hour is None
    assert all(h >= 18 for h in hours_of(rows[oct(11)]))


def test_current_hour_is_partial_and_handover_ends_at_departure():
    """At 07:30 only half an hour is left before the 08:00 departure."""
    days = {oct(8): DayInfo(oct(8), km=49.6, away_hours=[(8, 17)])}
    prices = make_prices(oct(8), 3, {})
    p = plan(at(8, 7, 30), 40, days, prices, SETTINGS)
    first = p.hours[0]
    assert first.start == at(8, 7) and first.pct == pytest.approx(RATE / 2, abs=0.01)
    assert p.charge_until == at(8, 8)
    assert p.target_soc == round(40 + RATE / 2)
    assert p.unmet == pytest.approx(55 - 40 - RATE / 2, abs=0.1)  # floor 55 not reachable


def test_handover_stops_at_last_known_price():
    """Tomorrow's prices are not known yet: hand over today's hours only."""
    days = {oct(10): DayInfo(oct(10), km=150, away_hours=[(9, 18)])}
    prices = make_prices(oct(9), 4, {(oct(9), 13): 0.5, (oct(10), 3): 0.9})
    p = plan(at(9, 10), 50, days, prices, SETTINGS, known_until=oct(9))
    assert p.charge_until == at(10, 0)
    rows = by_day(p)
    assert hours_of(rows[oct(9)]) == [13] and p.target_soc == round(50 + RATE)
    assert rows[oct(10)].charge_hours and not rows[oct(10)].charge_hours[0].known


def test_calibration_early_grab():
    s = Settings(
        **{
            **SETTINGS.__dict__,
            "calibration_interval_days": 28,
            "calibration_early_days": 15,
            "calibration_price_threshold": 0.8,
            "calibration_soc": 100,
            "last_calibration": at(8, 3) - timedelta(days=18),
        }
    )
    prices = make_prices(oct(8), 8, {(oct(9), 3): 0.78, (oct(10), 3): 0.85})
    p = plan(at(8, 20), 60, {}, prices, s)
    rows = by_day(p)
    # Friday 03 (0.78) ≤ 0.8 and nothing later is cheaper → calibrate before
    # Friday's departure: charge to 100 % in the cheapest hours (03 first).
    assert p.calibration and p.target_soc == 100
    assert rows[oct(9)].calibration and 3 in hours_of(rows[oct(9)])
    assert p.charge_until == at(9, 8)

    # Not yet 15 days since the last calibration → no calibration.
    s2 = Settings(**{**s.__dict__, "last_calibration": at(8, 3) - timedelta(days=10)})
    assert plan(at(8, 20), 60, {}, prices, s2).calibration is False


def test_dst_change_hours_are_continuous():
    """25 October 2026 has 25 hours (02:00 twice); every hour appears once in UTC."""
    s = Settings(**{**SETTINGS.__dict__, "horizon_days": 2})
    prices = make_prices(oct(24), 4, {})
    slots = build_slots(at(24, 20), {}, prices, prices, None, s)
    starts = [sl.start.astimezone(ZoneInfo("UTC")) for sl in slots]
    assert all((b - a).total_seconds() == 3600 for a, b in zip(starts, starts[1:], strict=False))
    assert [sl.start.hour for sl in slots if sl.start.date() == oct(25)].count(2) == 2
    assert sum(1 for sl in slots if sl.departs) == 2  # 25/10 and 26/10 at 08, once each
