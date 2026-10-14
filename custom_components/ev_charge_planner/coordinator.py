"""Coordinator: collects inputs from Home Assistant and runs the planner."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.location import distance

from .adapters import calendar as cal_adapter
from .adapters import prices as price_adapters
from .adapters.charger import charge_kw as charge_kw_from_state
from .adapters.trips import build_days, parse_dates
from .const import (
    CALENDAR_REFRESH_MINUTES,
    CONF_AWAY_HOURS_ENTITY,
    CONF_AWAY_NIGHTS_ENTITY,
    CONF_CALENDAR_ENTITIES,
    CONF_CHARGE_SPEED_ENTITY,
    CONF_DEFAULT_DEPARTURE_HOUR,
    CONF_EFFICIENCY_ENTITY,
    CONF_EFFICIENCY_FALLBACK,
    CONF_FORECAST_ENTITY,
    CONF_FORECAST_MAE_ENTITIES,
    CONF_HOLIDAY_CALENDAR,
    CONF_HOME_WORDS,
    CONF_HORIZON_DAYS,
    CONF_IGNORE_WORDS,
    CONF_KM_ENTITY,
    CONF_LAST_CALIBRATION_ENTITY,
    CONF_LOCATION_ENTITY,
    CONF_MIN_HOME_MINUTES,
    CONF_PLUGGED_ENTITY,
    CONF_PRICE_ADAPTER,
    CONF_PRICE_ENTITY,
    CONF_SOC_ENTITY,
    CONF_WAZE_REGION,
    DEFAULT_OPTIONS,
    DOMAIN,
    LEG_CACHE_HOURS,
    LEG_RETRY_MINUTES,
    MAX_NEW_LEGS_PER_UPDATE,
    PARAMS,
    UPDATE_INTERVAL_MINUTES,
)
from .planner import Plan, Settings, merge_prices, plan

_LOGGER = logging.getLogger(__name__)


class PlannerCoordinator(DataUpdateCoordinator[Plan]):
    """Runs the planner whenever inputs or parameters change."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=UPDATE_INTERVAL_MINUTES),
            config_entry=entry,
        )
        self.entry = entry
        # Parameter values; number entities restore and update these.
        self.params: dict[str, float] = {p.key: p.default for p in PARAMS}
        # Charging power used in the last update and where it came from.
        self.charge_kw: float | None = None
        self.charge_speed_source: str = "manual"
        # Calendar trips: events (refreshed every CALENDAR_REFRESH_MINUTES), routed
        # legs (cached), and the trips/status of the last update for the sensors.
        self._events: list[cal_adapter.CalendarEvent] | None = None
        self._events_at: datetime | None = None
        self._legs: dict[tuple[str, str], tuple[cal_adapter.Leg | None, datetime]] = {}
        self._waze_warned = False
        self.trips: dict[date, cal_adapter.DayTrips] = {}
        self.trip_source: str = "text helpers"
        self.missing_legs: list[str] = []

    # ------------------------------------------------------------------ inputs

    @property
    def _conf(self) -> dict[str, Any]:
        return {**DEFAULT_OPTIONS, **self.entry.data, **self.entry.options}

    def _input_entities(self) -> list[str]:
        keys = (
            CONF_SOC_ENTITY,
            CONF_PLUGGED_ENTITY,
            CONF_PRICE_ENTITY,
            CONF_FORECAST_ENTITY,
            CONF_EFFICIENCY_ENTITY,
            CONF_KM_ENTITY,
            CONF_AWAY_HOURS_ENTITY,
            CONF_AWAY_NIGHTS_ENTITY,
            CONF_LAST_CALIBRATION_ENTITY,
            CONF_LOCATION_ENTITY,
            CONF_CHARGE_SPEED_ENTITY,
        )
        return [e for k in keys if (e := self._conf.get(k))] + list(self._conf.get(CONF_FORECAST_MAE_ENTITIES) or [])

    @callback
    def async_start_listeners(self) -> CALLBACK_TYPE:
        """Refresh when any input entity changes."""

        @callback
        def _changed(_event: Event) -> None:
            self.hass.async_create_task(self.async_request_refresh())

        return async_track_state_change_event(self.hass, self._input_entities(), _changed)

    def _state(self, key: str) -> Any:
        entity_id = self._conf.get(key)
        return self.hass.states.get(entity_id) if entity_id else None

    def _float_state(self, key: str) -> float | None:
        st = self._state(key)
        try:
            return float(st.state) if st else None
        except (TypeError, ValueError):
            return None

    def _text_state(self, key: str) -> str | None:
        st = self._state(key)
        return st.state if st and st.state not in ("unknown", "unavailable") else None

    async def _holidays(self, start: date, days: int) -> set[date]:
        cal = self._conf.get(CONF_HOLIDAY_CALENDAR)
        if not cal:
            return set()
        try:
            resp = await self.hass.services.async_call(
                "calendar",
                "get_events",
                {"start_date_time": dt_util.start_of_local_day(), "duration": {"days": days + 1}},
                target={"entity_id": cal},
                blocking=True,
                return_response=True,
            )
        except Exception as err:  # noqa: BLE001 – holidays are optional
            _LOGGER.debug("Could not read holiday calendar %s: %s", cal, err)
            return set()
        out: set[date] = set()
        for ev in (resp or {}).get(cal, {}).get("events", []):
            d = dt_util.parse_date(str(ev.get("start", ""))[:10])
            if d:
                out.add(d)
        return out

    def _forecast_mae(self) -> tuple[float, ...]:
        """MAE per lead day from the configured sensors, in order; stops at the first unavailable one."""
        out: list[float] = []
        for entity_id in self._conf.get(CONF_FORECAST_MAE_ENTITIES) or []:
            st = self.hass.states.get(entity_id)
            try:
                out.append(float(st.state))
            except (AttributeError, TypeError, ValueError):
                break
        return tuple(out)

    def _prices(self, tz):
        """Merged hourly prices and the last date with known prices."""
        adapter = self._conf.get(CONF_PRICE_ADAPTER, price_adapters.ADAPTER_ENERGI_DATA_SERVICE)
        st = self._state(CONF_PRICE_ENTITY)
        attrs = dict(st.attributes) if st else {}
        fallback, known = [], []
        if adapter == price_adapters.ADAPTER_ENERGI_DATA_SERVICE:
            known, fallback = price_adapters.energi_data_service(attrs, tz)
        elif adapter == price_adapters.ADAPTER_NORDPOOL:
            known = price_adapters.nordpool(attrs, tz)
        else:
            known = price_adapters.from_list(attrs.get("prices") or attrs.get("raw_today") or [], tz, "known")
        fst = self._state(CONF_FORECAST_ENTITY)
        forecast = price_adapters.open_spot_forecast(dict(fst.attributes), tz) if fst else []
        known_until = max((hp.start.astimezone(tz).date() for hp in known), default=None)
        return merge_prices(fallback, forecast, known, tz=tz), known_until

    def _location(self) -> tuple[bool | None, float | None]:
        """Whether the car is home (None if unknown) and its straight-line distance from home in km."""
        plugged = self._state(CONF_PLUGGED_ENTITY)
        if plugged is not None and plugged.state == "on":
            return True, 0.0
        st = self._state(CONF_LOCATION_ENTITY)
        if st is None or st.state in ("unknown", "unavailable"):
            return None, None
        if st.state == "home":
            return True, 0.0
        lat, lon = st.attributes.get("latitude"), st.attributes.get("longitude")
        if lat is None or lon is None:
            return False, None
        meters = distance(self.hass.config.latitude, self.hass.config.longitude, lat, lon)
        return False, meters / 1000 if meters is not None else None

    def _charge_kw(self) -> tuple[float, str]:
        """Charging power: from the charge speed entity when usable, else the parameter."""
        manual = float(self.params["charge_kw"])
        st = self._state(CONF_CHARGE_SPEED_ENTITY)
        if st is None:
            return manual, "manual"
        kw = charge_kw_from_state(st.state, st.attributes.get("unit_of_measurement"), float(self.params["battery_kwh"]))
        if kw is None:
            _LOGGER.debug("Charge speed entity %s not usable (%s), using %s kW", st.entity_id, st.state, manual)
            return manual, "manual (entity unavailable)"
        return kw, st.entity_id

    # ------------------------------------------------------------- calendar

    def _rules(self) -> cal_adapter.CalendarRules:
        conf = self._conf
        return cal_adapter.CalendarRules(
            home_words=cal_adapter.split_words(conf.get(CONF_HOME_WORDS)),
            ignore_words=cal_adapter.split_words(conf.get(CONF_IGNORE_WORDS)),
            min_home_minutes=float(conf[CONF_MIN_HOME_MINUTES]),
        )

    async def _calendar_events(self, now: datetime, days: int) -> list[cal_adapter.CalendarEvent]:
        """Events of the configured calendars, read again every CALENDAR_REFRESH_MINUTES."""
        if self._events is not None and self._events_at and now - self._events_at < timedelta(minutes=CALENDAR_REFRESH_MINUTES):
            return self._events
        raw: list[dict] = []
        for cal in self._conf.get(CONF_CALENDAR_ENTITIES) or []:
            try:
                resp = await self.hass.services.async_call(
                    "calendar",
                    "get_events",
                    {"start_date_time": dt_util.start_of_local_day(), "duration": {"days": days + 1}},
                    target={"entity_id": cal},
                    blocking=True,
                    return_response=True,
                )
            except Exception as err:  # noqa: BLE001 – keep the last events
                _LOGGER.warning("Could not read calendar %s: %s", cal, err)
                return self._events or []
            raw.extend((resp or {}).get(cal, {}).get("events", []))
        self._events = cal_adapter.parse_events(raw, now.tzinfo)
        self._events_at = now
        return self._events

    def _place(self, location: str) -> str:
        if location == cal_adapter.HOME:
            return f"{self.hass.config.latitude},{self.hass.config.longitude}"
        return location

    async def _route(self, needed: set[tuple[str, str]], now: datetime) -> None:
        """Route the legs that are not cached (Waze Travel Time), a limited number per update."""
        todo = []
        for key in sorted(needed):
            cached = self._legs.get(key)
            if cached is None:
                todo.append(key)
                continue
            leg, at = cached
            ttl = timedelta(hours=LEG_CACHE_HOURS) if leg is not None else timedelta(minutes=LEG_RETRY_MINUTES)
            if now - at >= ttl:
                todo.append(key)
        if not todo:
            return
        if not self.hass.services.has_service("waze_travel_time", "get_travel_times"):
            if not self._waze_warned:
                _LOGGER.warning("Calendar trips need the Waze Travel Time integration to calculate km and travel time")
                self._waze_warned = True
            return
        region = self._conf[CONF_WAZE_REGION]
        for a, b in todo[:MAX_NEW_LEGS_PER_UPDATE]:
            leg = None
            try:
                resp = await self.hass.services.async_call(
                    "waze_travel_time",
                    "get_travel_times",
                    {"origin": self._place(a), "destination": self._place(b), "region": region, "units": "metric"},
                    blocking=True,
                    return_response=True,
                )
                routes = [r for r in (resp or {}).get("routes", []) if r.get("duration") is not None and r.get("distance") is not None]
                if routes:
                    best = min(routes, key=lambda r: float(r["duration"]))
                    leg = cal_adapter.Leg(km=float(best["distance"]), minutes=float(best["duration"]))
            except Exception as err:  # noqa: BLE001 – a leg that fails is retried later
                _LOGGER.debug("Could not route %s → %s: %s", a, b, err)
            self._legs[(a, b)] = (leg, now)

    async def _calendar_days(self, now: datetime, days: int, holidays: set[date]):
        events = await self._calendar_events(now, days)
        rules = self._rules()
        cars = cal_adapter.car_events(events, rules)
        await self._route(cal_adapter.legs_needed(cars), now)
        legs = {k: leg for k, (leg, _at) in self._legs.items() if leg is not None}
        trips, missing = cal_adapter.plan_trips(cars, legs, rules)
        self.trips = trips
        self.trip_source = "calendar"
        self.missing_legs = sorted(f"{a} → {b}".replace(cal_adapter.HOME, "home") for a, b in missing)
        nights = parse_dates(self._text_state(CONF_AWAY_NIGHTS_ENTITY))
        return cal_adapter.build_days(trips, nights, holidays)

    # ------------------------------------------------------------------ update

    async def _async_update_data(self) -> Plan:
        now = dt_util.now()
        tz = now.tzinfo
        conf = self._conf

        soc = self._float_state(CONF_SOC_ENTITY)
        if soc is None:
            raise UpdateFailed("State of charge is unavailable")

        efficiency = self._float_state(CONF_EFFICIENCY_ENTITY) or float(conf[CONF_EFFICIENCY_FALLBACK])
        last_cal = None
        if (st := self._state(CONF_LAST_CALIBRATION_ENTITY)) is not None:
            last_cal = dt_util.parse_datetime(st.state)
            if last_cal is not None and last_cal.tzinfo is None:
                last_cal = last_cal.replace(tzinfo=tz)

        self.charge_kw, self.charge_speed_source = self._charge_kw()
        settings = Settings(
            efficiency_kwh_per_km=efficiency,
            default_departure_hour=int(conf[CONF_DEFAULT_DEPARTURE_HOUR]),
            horizon_days=int(conf[CONF_HORIZON_DAYS]),
            last_calibration=last_cal,
            forecast_mae=self._forecast_mae(),
            **{**self.params, "charge_kw": self.charge_kw},
        )

        holidays = await self._holidays(now.date(), settings.horizon_days)
        if conf.get(CONF_CALENDAR_ENTITIES):
            days = await self._calendar_days(now, settings.horizon_days, holidays)
        else:
            self.trips, self.trip_source, self.missing_legs = {}, "text helpers", []
            days = build_days(
                self._text_state(CONF_KM_ENTITY),
                self._text_state(CONF_AWAY_HOURS_ENTITY),
                self._text_state(CONF_AWAY_NIGHTS_ENTITY),
                holidays,
            )
        prices, known_until = self._prices(tz)
        car_home, distance_km = self._location()
        return plan(
            now,
            soc,
            days,
            prices,
            settings,
            known_until=known_until,
            car_home=car_home,
            distance_km=distance_km,
        )
