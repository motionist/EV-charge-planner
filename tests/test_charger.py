"""Charging speed from a charger/charging integration entity."""

import pytest

from ev_charge_planner.adapters.charger import charge_kw


def test_percent_per_hour_uses_the_battery_capacity():
    # EV Smart Charging: 12.7 %/h of an 82 kWh battery ≈ 10.4 kW.
    assert charge_kw("12.7", "%/h", 82) == pytest.approx(10.414)


def test_kw_and_w():
    assert charge_kw("11", "kW", 82) == 11
    assert charge_kw("7400", "W", 82) == pytest.approx(7.4)
    assert charge_kw("11", None, 82) == 11  # no unit: kW


@pytest.mark.parametrize("value", ["unavailable", "unknown", None, "0", "-3"])
def test_unusable_values(value):
    assert charge_kw(value, "%/h", 82) is None


def test_unknown_unit():
    assert charge_kw("16", "A", 82) is None
