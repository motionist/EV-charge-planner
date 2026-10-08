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
    night_end_hour: int = 8
    day_start_hour: int = 8
    day_end_hour: int = 17
    horizon_days: int = 7
    calibration_soc: float = 100.0
    calibration_interval_days: float = 0.0  # 0 disables calibration
    calibration_early_days: float = 0.0
    calibration_price_threshold: float = 0.0
    calibration_escalation_pct_per_week: float = 10.0
    last_calibration: datetime | None = None

    def pct_per_km(self) -> float:
        """Battery percentage used per km."""
        if self.battery_kwh <= 0:
            return 0.0
        return self.efficiency_kwh_per_km / self.battery_kwh * 100.0

    def buffer_pct(self) -> float:
        """Buffer after the planned trips, in percent of the battery."""
        return self.buffer_km * self.pct_per_km()


@dataclass
class Opportunity:
    """A period in which the car can charge (a night or a home day window)."""

    kind: str  # "night" | "day"
    day: date
    price: float | None  # cheapest hour in the window, None if unknown
    best_hour: int | None
    start_hour: int
    end_hour: int
    available: bool = True


@dataclass
class PlanRow:
    """Plan for one date."""

    day: date
    day_type: str  # weekday | weekend | holiday
    km: float
    floor: float
    night: Opportunity | None
    night_target: int | None  # set when the plan charges this night
    day_window: Opportunity | None
    day_target: int | None  # set when the plan charges in the day window
    soc_departure: float  # expected SoC when leaving this date
    calibration: bool = False
    away_overnight: bool = False
    status: str = "not_needed"  # charge | covered | not_needed


@dataclass
class Plan:
    """Planner result."""

    target_soc: int
    next_opportunity: Opportunity | None
    next_charge: Opportunity | None
    rows: list[PlanRow]
