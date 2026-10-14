"""Constants for EV Charge Planner (no Home Assistant imports)."""

from __future__ import annotations

from dataclasses import dataclass

DOMAIN = "ev_charge_planner"
PLATFORMS = ["number", "sensor"]

# Config entry data (entity selection)
CONF_SOC_ENTITY = "soc_entity"
CONF_PLUGGED_ENTITY = "plugged_entity"
CONF_PRICE_ADAPTER = "price_adapter"
CONF_PRICE_ENTITY = "price_entity"
CONF_FORECAST_ENTITY = "forecast_entity"
CONF_FORECAST_MAE_ENTITIES = "forecast_mae_entities"
CONF_EFFICIENCY_ENTITY = "efficiency_entity"
CONF_KM_ENTITY = "km_entity"
CONF_AWAY_HOURS_ENTITY = "away_hours_entity"
CONF_AWAY_NIGHTS_ENTITY = "away_nights_entity"
CONF_HOLIDAY_CALENDAR = "holiday_calendar"
CONF_LAST_CALIBRATION_ENTITY = "last_calibration_entity"
CONF_LOCATION_ENTITY = "location_entity"
CONF_CHARGE_SPEED_ENTITY = "charge_speed_entity"
CONF_CALENDAR_ENTITIES = "calendar_entities"

# Options
CONF_DEFAULT_DEPARTURE_HOUR = "default_departure_hour"
CONF_HORIZON_DAYS = "horizon_days"
CONF_EFFICIENCY_FALLBACK = "efficiency_fallback"
CONF_HOME_WORDS = "home_words"
CONF_IGNORE_WORDS = "ignore_words"
CONF_MIN_HOME_MINUTES = "min_home_minutes"
CONF_WAZE_REGION = "waze_region"

WAZE_REGIONS = ["eu", "us", "na", "il", "au"]

DEFAULT_OPTIONS = {
    CONF_DEFAULT_DEPARTURE_HOUR: 8,
    CONF_HORIZON_DAYS: 7,
    CONF_EFFICIENCY_FALLBACK: 0.20,
    CONF_HOME_WORDS: "",
    CONF_IGNORE_WORDS: "teams, zoom, meet.google, webex, online, http://, https://, #ingenbil, #tog",
    CONF_MIN_HOME_MINUTES: 60,
    CONF_WAZE_REGION: "eu",
}

CALENDAR_REFRESH_MINUTES = 15
LEG_CACHE_HOURS = 24  # routed legs are reused this long
LEG_RETRY_MINUTES = 60  # a leg that could not be routed is retried after this
MAX_NEW_LEGS_PER_UPDATE = 20

UPDATE_INTERVAL_MINUTES = 5


@dataclass(frozen=True)
class ParamSpec:
    """A planner parameter exposed as a number entity."""

    key: str  # attribute name on planner.Settings
    default: float
    min: float
    max: float
    step: float
    unit: str | None
    icon: str


PARAMS: tuple[ParamSpec, ...] = (
    ParamSpec("floor_weekday", 50, 0, 100, 1, "%", "mdi:battery-arrow-up"),
    ParamSpec("floor_weekend", 40, 0, 100, 1, "%", "mdi:battery-arrow-up-outline"),
    ParamSpec("max_level", 90, 50, 100, 1, "%", "mdi:battery-lock"),
    ParamSpec("max_level_long_trip", 100, 50, 100, 1, "%", "mdi:battery-high"),
    ParamSpec("long_trip_km", 100, 0, 1000, 10, "km", "mdi:road-variant"),
    ParamSpec("long_trip_advantage_pct", 30, 0, 80, 5, "%", "mdi:cash-minus"),
    ParamSpec("buffer_km", 100, 0, 300, 10, "km", "mdi:map-marker-distance"),
    ParamSpec("battery_kwh", 75, 10, 200, 0.5, "kWh", "mdi:car-battery"),
    ParamSpec("charge_kw", 11, 1, 50, 0.1, "kW", "mdi:ev-station"),
    ParamSpec("calibration_soc", 100, 50, 100, 1, "%", "mdi:battery-sync"),
    ParamSpec("calibration_interval_days", 0, 0, 90, 1, "d", "mdi:calendar-sync"),
    ParamSpec("calibration_early_days", 0, 0, 90, 1, "d", "mdi:calendar-arrow-left"),
    ParamSpec("calibration_price_threshold", 0, 0, 10, 0.05, None, "mdi:cash-check"),
    ParamSpec("calibration_escalation_pct_per_week", 10, 0, 100, 1, "%", "mdi:trending-up"),
    ParamSpec("forecast_min_accuracy", 90, 50, 100, 1, "%", "mdi:crystal-ball"),
    ParamSpec("forecast_far_factor", 1.25, 1, 3, 0.05, None, "mdi:chart-bell-curve"),
)
