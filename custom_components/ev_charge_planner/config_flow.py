"""Config flow: pick the entities the planner reads; options for time windows."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from .adapters.prices import ADAPTER_ENERGI_DATA_SERVICE, ADAPTER_GENERIC, ADAPTER_NORDPOOL
from .const import (
    CONF_AWAY_HOURS_ENTITY,
    CONF_AWAY_NIGHTS_ENTITY,
    CONF_DAY_END_HOUR,
    CONF_DAY_START_HOUR,
    CONF_EFFICIENCY_ENTITY,
    CONF_EFFICIENCY_FALLBACK,
    CONF_FORECAST_ENTITY,
    CONF_HOLIDAY_CALENDAR,
    CONF_HORIZON_DAYS,
    CONF_KM_ENTITY,
    CONF_LAST_CALIBRATION_ENTITY,
    CONF_NIGHT_END_HOUR,
    CONF_PLUGGED_ENTITY,
    CONF_PRICE_ADAPTER,
    CONF_PRICE_ENTITY,
    CONF_SOC_ENTITY,
    DEFAULT_OPTIONS,
    DOMAIN,
)

TEXT_DOMAINS = ["input_text", "text", "sensor"]


def _entity(domain: str | list[str]) -> selector.EntitySelector:
    return selector.EntitySelector(selector.EntitySelectorConfig(domain=domain))


USER_SCHEMA = vol.Schema(
    {
        vol.Required("name", default="EV Charge Planner"): str,
        vol.Required(CONF_SOC_ENTITY): _entity("sensor"),
        vol.Optional(CONF_PLUGGED_ENTITY): _entity("binary_sensor"),
        vol.Required(CONF_PRICE_ADAPTER, default=ADAPTER_ENERGI_DATA_SERVICE): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[ADAPTER_ENERGI_DATA_SERVICE, ADAPTER_NORDPOOL, ADAPTER_GENERIC],
                translation_key=CONF_PRICE_ADAPTER,
            )
        ),
        vol.Required(CONF_PRICE_ENTITY): _entity("sensor"),
        vol.Optional(CONF_FORECAST_ENTITY): _entity("sensor"),
        vol.Optional(CONF_EFFICIENCY_ENTITY): _entity("sensor"),
        vol.Optional(CONF_KM_ENTITY): _entity(TEXT_DOMAINS),
        vol.Optional(CONF_AWAY_HOURS_ENTITY): _entity(TEXT_DOMAINS),
        vol.Optional(CONF_AWAY_NIGHTS_ENTITY): _entity(TEXT_DOMAINS),
        vol.Optional(CONF_HOLIDAY_CALENDAR): _entity("calendar"),
        vol.Optional(CONF_LAST_CALIBRATION_ENTITY): _entity(["input_datetime", "sensor"]),
    }
)


def _hour() -> selector.NumberSelector:
    return selector.NumberSelector(selector.NumberSelectorConfig(min=0, max=24, step=1, mode=selector.NumberSelectorMode.BOX))


class EvChargePlannerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            name = user_input.pop("name")
            return self.async_create_entry(title=name, data=user_input)
        return self.async_show_form(step_id="user", data_schema=USER_SCHEMA)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return EvChargePlannerOptionsFlow()


class EvChargePlannerOptionsFlow(OptionsFlow):
    """Time windows, horizon and efficiency fallback."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        cur = {**DEFAULT_OPTIONS, **self.config_entry.options}
        schema = vol.Schema(
            {
                vol.Required(CONF_NIGHT_END_HOUR, default=cur[CONF_NIGHT_END_HOUR]): _hour(),
                vol.Required(CONF_DAY_START_HOUR, default=cur[CONF_DAY_START_HOUR]): _hour(),
                vol.Required(CONF_DAY_END_HOUR, default=cur[CONF_DAY_END_HOUR]): _hour(),
                vol.Required(CONF_HORIZON_DAYS, default=cur[CONF_HORIZON_DAYS]): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=1, max=7, step=1, mode=selector.NumberSelectorMode.BOX)
                ),
                vol.Required(CONF_EFFICIENCY_FALLBACK, default=cur[CONF_EFFICIENCY_FALLBACK]): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=0.05, max=0.5, step=0.001, unit_of_measurement="kWh/km", mode=selector.NumberSelectorMode.BOX)
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
