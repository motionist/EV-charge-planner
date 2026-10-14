"""Problem sensor: on while information is missing or the user should act (DESIGN §5.6)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import PlannerCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: PlannerCoordinator = entry.runtime_data
    async_add_entities([ProblemSensor(coordinator)])


class ProblemSensor(CoordinatorEntity[PlannerCoordinator], BinarySensorEntity):
    """On while there is an issue; the issues are attributes (also those not notified)."""

    _attr_has_entity_name = True
    _attr_translation_key = "problem"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: PlannerCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_problem"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, coordinator.entry.entry_id)})

    @property
    def available(self) -> bool:
        # Stays available when an update fails, e.g. while the battery level is missing.
        return True

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.alerts.active)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"issues": self.coordinator.alerts.current()}
