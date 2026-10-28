"""Forecast price adjustment (DESIGN §5.0): a forecast price gets a safety margin, so a known price weighs more."""

from dataclasses import replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from ev_charge_planner.planner import (
    DayInfo,
    HourPrice,
    Settings,
    adjust_prices,
    calibrate_fallback,
    fallback_margins,
    fallback_offsets,
    forecast_disagreement,
    forecast_margins,
    merge_prices,
    plan,
)

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
# Margins and adjusted prices
# --------------------------------------------------------------------------- #


def test_margin_per_lead_day_and_far_factor():
    prices = prices_for(oct(9), 8, {})
    m = forecast_margins(prices, oct(9), settings())
    # Known mean = 1.30 → accuracy = 1 − MAE / 1.30 < 90 % → margin = MAE
    assert oct(9) not in m  # known date is never adjusted
    assert m[oct(10)] == pytest.approx(0.21)
    assert m[oct(13)] == pytest.approx(0.36)
    # Lead 5+ uses the day-4 MAE × forecast_far_factor (1.25)
    assert m[oct(14)] == pytest.approx(0.36 * 1.25)
    assert m[oct(16)] == m[oct(14)]


def test_accurate_forecast_or_no_mae_is_not_adjusted():
    prices = prices_for(oct(9), 4, {})
    assert forecast_margins(prices, oct(9), settings(mae=(0.05,))) == {}  # 96 % ≥ 90 %
    assert forecast_margins(prices, oct(9), settings(mae=())) == {}
    assert forecast_margins(prices, None, settings()) == {}


def test_margin_cap_and_negative_prices():
    prices = prices_for(oct(9), 2, {})
    m = forecast_margins(prices, oct(9), settings(mae=(2.0,)))
    assert m[oct(10)] == pytest.approx(1.30)  # at most the mean known price
    adj = adjust_prices({(oct(10), 1): 0.8, (oct(10), 2): -0.1, (oct(9), 1): 0.8}, {oct(10): 0.2})
    assert adj[(oct(10), 1)] == pytest.approx(1.0)  # 0.8 + 0.2
    assert adj[(oct(10), 2)] == pytest.approx(0.1)  # a negative price is made dearer too
    assert adj[(oct(9), 1)] == 0.8  # known date untouched


def test_disagreement_leaves_out_the_usual_difference():
    """The fallback is usually 0.5 lower at 03; only a date that differs more than that counts."""
    forecast = [hp(10, 3, 1.5, "forecast"), hp(11, 3, 1.5, "forecast"), hp(12, 3, 1.5, "forecast")]
    fallback = [hp(10, 3, 1.0, "fallback"), hp(11, 3, 1.0, "fallback"), hp(12, 3, 0.4, "fallback"), hp(13, 3, 0.9, "fallback")]
    d = forecast_disagreement(fallback, forecast, tz=TZ)
    # offset[3] = (0.5 + 0.5 + 1.1) / 3 = 0.7
    assert d[oct(10)] == pytest.approx(0.2) and d[oct(11)] == pytest.approx(0.2)
    assert d[oct(12)] == pytest.approx(0.4)
    assert oct(13) not in d  # no forecast that date
    assert forecast_disagreement(fallback, [], tz=TZ) == {}


def test_disagreement_counts_even_when_the_forecast_is_accurate():
    prices = prices_for(oct(9), 4, {})
    accurate = settings(mae=(0.05,))
    assert forecast_margins(prices, oct(9), accurate, {oct(10): 0.4}) == {oct(10): pytest.approx(0.4)}
    # The larger of MAE and disagreement
    m = forecast_margins(prices, oct(9), settings(), {oct(10): 0.1, oct(11): 0.5, oct(9): 0.9})
    assert m[oct(10)] == pytest.approx(0.21) and m[oct(11)] == pytest.approx(0.5)
    assert oct(9) not in m  # known date


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #

# Friday 9/10 03 (known) 0.74, Saturday 10/10 03 (forecast) 0.65 and a clearly
# cheaper Sunday 03 (forecast 0.40). Saturday: 150 km = 40 % + 100 km buffer
# 27 % → 67 % at departure 09.
NIGHTS = prices_for(oct(9), 8, {(oct(9), 3): 0.74, (oct(10), 3): 0.65, (oct(11), 3): 0.40})
TRIP_SAT = {oct(10): DayInfo(oct(10), km=150, away_hours=[(9, 18)])}
EVENING_THU = datetime(2026, 10, 8, 20, 0, tzinfo=TZ)


def hours_of(row):
    return [ch.start.hour for ch in row.charge_hours]


def test_known_hour_beats_slightly_cheaper_forecast():
    """0.65 forecast at 84 % accuracy gets the day-1 MAE 0.21 ⇒ 0.86 > 0.74 known ⇒ Friday 03 gets the full hour."""
    s = replace(settings(), cheap_fill_advantage_pct=0)  # Saturday 03 is also 43 % below its later hours
    rate = s.charge_pct_per_hour()
    p = plan(EVENING_THU, 60, TRIP_SAT, NIGHTS, s, known_until=oct(9))
    rows = by_day(p)
    assert hours_of(rows[oct(9)]) == [3] and rows[oct(9)].charge_pct == pytest.approx(rate, abs=0.01)
    assert hours_of(rows[oct(10)]) == [3] and rows[oct(10)].charge_pct < rate - 1
    assert rows[oct(10)].forecast_margin == pytest.approx(0.21)
    assert rows[oct(9)].forecast_margin == 0.0
    sat = rows[oct(10)].charge_hours[0]
    assert sat.raw_price == 0.65 and sat.price == pytest.approx(0.86)
    # Friday 03 is known and before Friday's departure → handed over now.
    assert p.target_soc == round(60 + rate) and p.charge_until == datetime(2026, 10, 9, 8, 0, tzinfo=TZ)


def test_without_adjustment_planner_waits_for_forecast():
    """Same week, forecast trusted: Saturday 03 gets the full hour, Friday 03 the rest."""
    s = settings(mae=())
    rate = s.charge_pct_per_hour()
    p = plan(EVENING_THU, 60, TRIP_SAT, NIGHTS, s, known_until=oct(9))
    rows = by_day(p)
    assert hours_of(rows[oct(10)]) == [3] and rows[oct(10)].charge_pct == pytest.approx(rate, abs=0.01)
    assert not rows[oct(10)].charge_hours[0].known
    assert rows[oct(9)].charge_pct < rate - 1
    assert p.target_soc < round(60 + rate)


def test_disagreeing_forecasts_make_the_planner_charge_now():
    """Forecast trusted, but the two forecasts disagree about Saturday: its margin 0.2 ⇒ 0.85 > 0.74 known."""
    s = settings(mae=())
    rate = s.charge_pct_per_hour()
    p = plan(EVENING_THU, 60, TRIP_SAT, NIGHTS, s, known_until=oct(9), disagreement={oct(10): 0.2})
    rows = by_day(p)
    assert rows[oct(9)].charge_pct == pytest.approx(rate, abs=0.01)
    assert rows[oct(10)].forecast_margin == pytest.approx(0.2)


def test_known_day_hour_beats_forecast():
    """Friday 10:00, tomorrow not yet published: a known 0.739 at noon beats a
    0.664 forecast tonight at 84 % accuracy (adjusted 0.874), and a known 1.30
    beats a forecast 1.30."""
    now = datetime(2026, 10, 9, 10, 0, tzinfo=TZ)
    prices = prices_for(oct(9), 8, {(oct(9), 12): 0.739, (oct(10), 3): 0.664, (oct(11), 3): 0.40})
    s = settings()
    rate = s.charge_pct_per_hour()
    p = plan(now, 50, TRIP_SAT, prices, s, known_until=oct(9))
    rows = by_day(p)
    assert p.next_charge is not None and p.next_charge.start.hour == 12 and p.next_charge.known
    assert rows[oct(9)].charge_hours[0].pct == pytest.approx(rate, abs=0.01)
    # The rest is charged tonight at a known 1.30 rather than a forecast 1.30.
    assert hours_of(rows[oct(9)]) == [12, 23]
    assert p.charge_until == datetime(2026, 10, 10, 0, 0, tzinfo=TZ)
    assert p.target_soc == round(50 + rows[oct(9)].charge_pct)

    trusted = plan(now, 50, TRIP_SAT, prices, settings(mae=()), known_until=oct(9))
    assert hours_of(by_day(trusted)[oct(9)]) == [12]
    assert trusted.target_soc == round(50 + rate)


# --------------------------------------------------------------------------- #
# Fallback forecast after the forecast ends
# --------------------------------------------------------------------------- #


def hp(day: int, hour: int, price: float, source: str) -> HourPrice:
    return HourPrice(datetime(2026, 10, day, hour, tzinfo=TZ), price, source)


def test_fallback_is_calibrated_per_hour_of_day():
    """The fallback lacks e.g. the evening peak tariff: each hour of day gets its own mean difference."""
    forecast = [hp(10, 3, 1.5, "forecast"), hp(11, 3, 1.1, "forecast"), hp(10, 18, 4.0, "forecast")]
    fallback = [
        hp(10, 3, 1.0, "fallback"),  # diff 0.5
        hp(11, 3, 0.8, "fallback"),  # diff 0.3 → mean 0.4 at 03
        hp(10, 18, 2.0, "fallback"),  # diff 2.0 at 18
        hp(12, 3, 0.9, "fallback"),
        hp(12, 18, 2.1, "fallback"),
        hp(12, 12, 1.0, "fallback"),  # no overlap at 12 → overall mean (0.5 + 0.3 + 2.0) / 3
    ]
    out = {(p.start.day, p.start.hour): p for p in calibrate_fallback(fallback, forecast, tz=TZ)}
    assert set(out) == {(12, 3), (12, 18), (12, 12)}  # hours the forecast covers are left out
    assert out[(12, 3)].price == pytest.approx(1.3)
    assert out[(12, 18)].price == pytest.approx(4.1)
    assert out[(12, 12)].price == pytest.approx(1.0 + 2.8 / 3)
    assert out[(12, 3)].source == "fallback"
    merged = merge_prices(calibrate_fallback(fallback, forecast, tz=TZ), forecast, tz=TZ)
    assert merged[(oct(10), 3)] == 1.5 and merged[(oct(12), 3)] == pytest.approx(1.3)


def test_fallback_offsets_are_kept_for_later():
    """The offsets of one update calibrate a later fallback when the forecast is missing (e.g. after a restart)."""
    forecast = [hp(10, 3, 1.5, "forecast"), hp(10, 18, 4.0, "forecast")]
    offsets = fallback_offsets([hp(10, 3, 1.0, "fallback"), hp(10, 18, 2.0, "fallback")], forecast, tz=TZ)
    assert offsets[3] == pytest.approx(0.5) and offsets[18] == pytest.approx(2.0)
    assert offsets[12] == pytest.approx(1.25)  # overall mean
    assert fallback_offsets([hp(12, 3, 0.9, "fallback")], [], tz=TZ) is None
    out = calibrate_fallback([hp(12, 3, 0.9, "fallback"), hp(12, 18, 2.1, "fallback")], [], tz=TZ, offsets=offsets)
    assert [round(p.price, 3) for p in out] == [1.4, 4.1]


def test_fallback_without_overlap_is_unchanged():
    fallback = [hp(12, 3, 0.9, "fallback")]
    assert calibrate_fallback(fallback, [], tz=TZ) == fallback
    assert calibrate_fallback(fallback, [hp(10, 3, 1.5, "forecast")], tz=TZ) == fallback


def test_fallback_margin_grows_per_day_and_ignores_min_accuracy():
    prices = prices_for(oct(9), 8, {})
    fb = [(oct(13), 3), (oct(14), 3), (oct(15), 3), (oct(9), 3)]
    # Accurate forecast (no date margins), but the fallback hours still get one.
    s = settings(mae=(0.05, 0.06, 0.07, 0.08))
    assert forecast_margins(prices, oct(9), s) == {}
    m = fallback_margins(prices, fb, oct(9), s)
    assert (oct(9), 3) not in m  # known date
    assert m[(oct(13), 3)] == pytest.approx(0.08 * 1.25)  # lead 4
    assert m[(oct(14), 3)] == pytest.approx(0.08 * 1.25**2)  # lead 5
    assert m[(oct(15), 3)] == pytest.approx(0.08 * 1.25**3)
    assert fallback_margins(prices, fb, oct(9), settings(mae=())) == {}
    assert fallback_margins(prices, fb, None, s) == {}
    assert fallback_margins(prices, [(oct(14), 3)], oct(9), settings(mae=(2.0,)))[(oct(14), 3)] == pytest.approx(1.30)


def test_hour_margin_takes_precedence_over_date_margin():
    adj = adjust_prices({(oct(14), 3): 1.0, (oct(14), 4): 1.0}, {oct(14): 0.2}, {(oct(14), 3): 0.5})
    assert adj[(oct(14), 3)] == pytest.approx(1.5)
    assert adj[(oct(14), 4)] == pytest.approx(1.2)


def test_plan_weighs_fallback_hours_by_their_uncertainty():
    """A slightly cheaper fallback hour loses to a forecast hour once its uncertainty is priced in."""
    s = settings(mae=(0.05, 0.06, 0.07, 0.08))
    prices = prices_for(oct(9), 8, {(oct(12), 3): 1.00, (oct(14), 3): 0.95})
    trip = {oct(15): DayInfo(oct(15), km=150, away_hours=[(9, 18)])}
    now = datetime(2026, 10, 9, 20, 0, tzinfo=TZ)
    without = by_day(plan(now, 50, trip, prices, s, known_until=oct(9)))
    assert hours_of(without[oct(14)]) == [3]
    rows = by_day(plan(now, 50, trip, prices, s, known_until=oct(9), fallback_hours={(oct(14), h) for h in range(24)}))
    assert hours_of(rows[oct(12)]) == [3]
    sun = [ch for ch in rows[oct(14)].charge_hours if ch.start.hour == 3]
    assert not sun or sun[0].price == pytest.approx(0.95 + 0.08 * 1.25**2)
