"""Coordinator: collects inputs from Home Assistant and runs the planner."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .adapters import prices as price_adapters
from .adapters.trips import build_days
from .const import (
    CONF_AWAY_HOURS_ENTITY,
    CONF_AWAY_NIGHTS_ENTITY,
    CONF_DEFAULT_DEPARTURE_HOUR,
    CONF_EFFICIENCY_ENTITY,
    CONF_EFFICIENCY_FALLBACK,
    CONF_FORECAST_ENTITY,
    CONF_FORECAST_MAE_ENTITIES,
    CONF_HOLIDAY_CALENDAR,
    CONF_HORIZON_DAYS,
    CONF_KM_ENTITY,
    CONF_LAST_CALIBRATION_ENTITY,
    CONF_PLUGGED_ENTITY,
    CONF_PRICE_ADAPTER,
    CONF_PRICE_ENTITY,
    CONF_SOC_ENTITY,
    DEFAULT_OPTIONS,
    DOMAIN,
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

        settings = Settings(
            efficiency_kwh_per_km=efficiency,
            default_departure_hour=int(conf[CONF_DEFAULT_DEPARTURE_HOUR]),
            horizon_days=int(conf[CONF_HORIZON_DAYS]),
            last_calibration=last_cal,
            forecast_mae=self._forecast_mae(),
            **self.params,
        )

        holidays = await self._holidays(now.date(), settings.horizon_days)
        days = build_days(
            self._text_state(CONF_KM_ENTITY),
            self._text_state(CONF_AWAY_HOURS_ENTITY),
            self._text_state(CONF_AWAY_NIGHTS_ENTITY),
            holidays,
        )
        prices, known_until = self._prices(tz)
        return plan(now, soc, days, prices, settings, known_until=known_until)
