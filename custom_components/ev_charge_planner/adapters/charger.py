"""Charging speed from a charger or charging integration entity.

E.g. EV Smart Charging's ``number.<name>_charging_speed`` (unit ``%/h``), or a
charger's power sensor in kW or W.
"""

from __future__ import annotations

PCT_PER_HOUR_UNITS = {"%/h", "%"}
KW_UNITS = {"kW"}
W_UNITS = {"W"}


def charge_kw(value: object, unit: str | None, battery_kwh: float) -> float | None:
    """Charging power in kW from an entity state and its unit, or None if not usable.

    ``%/h`` (percentage of the battery per hour) is converted with the battery
    capacity. A state without a unit is taken as kW.
    """
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    unit = (unit or "").strip()
    if unit in PCT_PER_HOUR_UNITS:
        return number * battery_kwh / 100 if battery_kwh > 0 else None
    if unit in W_UNITS:
        return number / 1000
    if unit in KW_UNITS or not unit:
        return number
    return None
