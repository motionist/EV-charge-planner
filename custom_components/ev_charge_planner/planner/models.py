"""Data model for the EV charge planner.

Pure Python – must not import Home Assistant so it can be unit tested on its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass(frozen=True)
class HourPrice:
    """All-in price for the hour starting at ``start`` (timezone-aware, local)."""

    start: datetime
    price: float
    source: str = "known"  # known | forecast | fallback


@dataclass
class DayInfo:
    """Planned driving for one date."""

    day: date
    km: float = 0.0
    # Hour intervals [start, end) when the car is away from home.
    away_hours: list[tuple[int, int]] = field(default_factory=list)
    # The car is away the night *after* this date.
    away_overnight: bool = False
    holiday: bool = False


@dataclass
class Settings:
    """Planner parameters (see docs/DESIGN.md §6)."""

    battery_kwh: float = 75.0
    efficiency_kwh_per_km: float = 0.20
    floor_weekday: float = 50.0
    floor_weekend: float = 40.0
    max_level: float = 90.0
    buffer_km: float = 100.0
    # High cap before long trips (DESIGN §5.1): the home stay right before a
    # departure of at least long_trip_km may charge above max_level, up to
    # max_level_long_trip, in an hour at least long_trip_advantage_pct cheaper
    # than the best hour within max_level.
    max_level_long_trip: float = 100.0
    long_trip_km: float = 100.0
    long_trip_advantage_pct: float = 30.0
    charge_kw: float = 11.0  # charging power at the home charger
    # Departure on a date without away hours (and return after an away night).
    default_departure_hour: int = 8
    horizon_days: int = 7
    # Longest period handed over to the charger integration at once.
    handover_max_hours: int = 24
    calibration_soc: float = 100.0
    calibration_interval_days: float = 0.0  # 0 disables calibration
    calibration_early_days: float = 0.0
    calibration_price_threshold: float = 0.0
    calibration_escalation_pct_per_week: float = 10.0
    last_calibration: datetime | None = None
    # Forecast price adjustment (DESIGN §5.0). forecast_mae[i] is the
    # forecast's mean absolute error for lead day i + 1; empty disables it.
    forecast_mae: tuple[float, ...] = ()
    forecast_min_accuracy: float = 90.0  # %
    forecast_far_factor: float = 1.25  # MAE multiplier beyond the last lead day

    def pct_per_km(self) -> float:
        """Battery percentage used per km."""
        if self.battery_kwh <= 0:
            return 0.0
        return self.efficiency_kwh_per_km / self.battery_kwh * 100.0

    def buffer_pct(self) -> float:
        """Buffer after the planned trips, in percent of the battery."""
        return self.buffer_km * self.pct_per_km()

    def charge_pct_per_hour(self) -> float:
        """Battery percentage gained per full hour of charging."""
        if self.battery_kwh <= 0:
            return 0.0
        return self.charge_kw / self.battery_kwh * 100.0


@dataclass
class ChargeHour:
    """A planned charging hour."""

    start: datetime
    pct: float  # battery percentage charged in this hour
    kwh: float  # energy into the battery in this hour (charging losses not included)
    price: float | None  # adjusted price used for the decision
    raw_price: float | None  # price as reported by the price source
    known: bool  # True for a known (day-ahead) price, False for a forecast
    calibration: bool = False


@dataclass
class PlanRow:
    """Plan for one date."""

    day: date
    day_type: str  # weekday | weekend | holiday
    km: float
    floor: float
    departure_hour: int | None  # None when the car does not depart from home
    requirement: float | None  # SoC needed at departure
    charge_pct: float  # planned charging on this date (all hours)
    charge_kwh: float
    cost: float | None  # raw price × kWh of the hours with a price
    charge_hours: list[ChargeHour]
    soc_departure: float | None  # expected SoC at departure
    forecast_factor: float = 1.0  # accuracy factor of this date's prices
    calibration: bool = False
    away_overnight: bool = False
    status: str = "not_needed"  # charge | covered | not_needed


@dataclass
class Plan:
    """Planner result.

    ``target_soc`` and ``charge_until`` are the hand-over to the charger
    integration: charge to ``target_soc`` in the cheapest hours before
    ``charge_until``. They only include hours with known prices before the next
    departure; the rest of ``hours`` is the expected plan.
    """

    target_soc: int
    charge_until: datetime
    charge_now: float  # percentage handed over
    hours: list[ChargeHour]
    rows: list[PlanRow]
    unmet: float = 0.0  # percentage that cannot be charged in time
    calibration: bool = False
    # km still to drive before the car is home again, and the SoC expected then
    remaining_km: float = 0.0
    soc_home: float | None = None
    # Planned energy and cost by price basis (raw prices, charging losses not included)
    kwh_known: float = 0.0
    cost_known: float = 0.0
    kwh_forecast: float = 0.0
    cost_forecast: float = 0.0

    @property
    def next_charge(self) -> ChargeHour | None:
        """The first planned charging hour."""
        return self.hours[0] if self.hours else None
