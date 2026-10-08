"""Sensors: target SoC, next charge window and the full plan."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import PlannerCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: PlannerCoordinator = entry.runtime_data
    async_add_entities(
        [
            TargetSocSensor(coordinator),
            NextChargeSensor(coordinator, "next_charge_start", 0),
            NextChargeSensor(coordinator, "next_charge_end", 1),
            PlanSensor(coordinator),
        ]
    )


class _Base(CoordinatorEntity[PlannerCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: PlannerCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_translation_key = key
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.entry_id)},
            name=coordinator.entry.title,
            manufacturer="EV Charge Planner",
        )


class TargetSocSensor(_Base):
    """Target SoC for the next charge – feed this to EV Smart Charging or similar."""

    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_icon = "mdi:battery-charging-high"

    def __init__(self, coordinator: PlannerCoordinator) -> None:
        super().__init__(coordinator, "target_soc")

    @property
    def native_value(self) -> int | None:
        return self.coordinator.data.target_soc if self.coordinator.data else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        p = self.coordinator.data
        nc = p.next_charge if p else None
        return {
            "next_charge_kind": nc.kind if nc else None,
            "next_charge_date": nc.day.isoformat() if nc else None,
            "next_charge_best_hour": nc.best_hour if nc else None,
            "next_charge_price": nc.price if nc else None,
        }


class NextChargeSensor(_Base):
    """Start (index 0) or end (index 1) of the next charging window."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: PlannerCoordinator, key: str, index: int) -> None:
        super().__init__(coordinator, key)
        self._index = index

    @property
    def native_value(self):
        p = self.coordinator.data
        win = PlannerCoordinator.window_datetimes(p.next_charge if p else None, dt_util.DEFAULT_TIME_ZONE)
        return win[self._index] if win else None


class PlanSensor(_Base):
    """The full plan as attributes (one row per date) for dashboards."""

    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator: PlannerCoordinator) -> None:
        super().__init__(coordinator, "plan")

    @property
    def native_value(self) -> str | None:
        p = self.coordinator.data
        if not p:
            return None
        return p.next_charge.day.isoformat() if p.next_charge else "none"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        p = self.coordinator.data
        if not p:
            return {}
        rows = []
        for r in p.rows:
            rows.append(
                {
                    "date": r.day.isoformat(),
                    "type": r.day_type,
                    "km": round(r.km, 1),
                    "floor": r.floor,
                    "night_price": r.night.price if r.night else None,
                    "night_hour": r.night.best_hour if r.night else None,
                    "night_available": r.night.available if r.night else None,
                    "night_target": r.night_target,
                    "day_price": r.day_window.price if r.day_window else None,
                    "day_hour": r.day_window.best_hour if r.day_window else None,
                    "day_target": r.day_target,
                    "soc_departure": r.soc_departure,
                    "calibration": r.calibration,
                    "away_overnight": r.away_overnight,
                    "status": r.status,
                }
            )
        return {"rows": rows}
