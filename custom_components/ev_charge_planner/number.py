"""Planner parameters as number entities (restored across restarts)."""

from __future__ import annotations

from homeassistant.components.number import NumberMode, RestoreNumber
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, PARAMS, ParamSpec
from .coordinator import PlannerCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: PlannerCoordinator = entry.runtime_data
    async_add_entities(PlannerNumber(coordinator, spec) for spec in PARAMS)


class PlannerNumber(RestoreNumber):
    """One planner parameter."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: PlannerCoordinator, spec: ParamSpec) -> None:
        self.coordinator = coordinator
        self.spec = spec
        self._attr_translation_key = spec.key
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{spec.key}"
        self._attr_native_min_value = spec.min
        self._attr_native_max_value = spec.max
        self._attr_native_step = spec.step
        self._attr_native_unit_of_measurement = spec.unit
        self._attr_icon = spec.icon
        self._attr_native_value = spec.default
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, coordinator.entry.entry_id)})

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_number_data()) is not None and last.native_value is not None:
            self._attr_native_value = last.native_value
        self.coordinator.params[self.spec.key] = float(self._attr_native_value)
        await self.coordinator.async_request_refresh()

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        self.coordinator.params[self.spec.key] = float(value)
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()
