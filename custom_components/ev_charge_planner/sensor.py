"""Sensors: target SoC, next charge, charge-until time and the full plan."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import PlannerCoordinator
from .planner import ChargeHour


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: PlannerCoordinator = entry.runtime_data
    async_add_entities(
        [
            TargetSocSensor(coordinator),
            NextChargeSensor(coordinator),
            ChargeUntilSensor(coordinator),
            PlanSensor(coordinator),
        ]
    )


def _hour(ch: ChargeHour) -> dict[str, Any]:
    return {
        "start": ch.start.isoformat(),
        "pct": ch.pct,
        "kwh": ch.kwh,
        "price": ch.raw_price,
        "price_adjusted": ch.price,
        "known": ch.known,
        "calibration": ch.calibration,
    }


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
    """Target SoC to hand over – feed this to EV Smart Charging or similar.

    Charge to this level in the cheapest hours before ``charge_until``.
    """

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
        if not p:
            return {}
        return {
            "charge_until": p.charge_until.isoformat(),
            "charge_now": p.charge_now,
            "calibration": p.calibration,
            "unmet": p.unmet,
            "remaining_km": p.remaining_km,
            "soc_home": p.soc_home,
            "charge_kw": round(self.coordinator.charge_kw, 2) if self.coordinator.charge_kw else None,
            "charge_speed_source": self.coordinator.charge_speed_source,
        }


class NextChargeSensor(_Base):
    """Start of the first planned charging hour (known or forecast price)."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: PlannerCoordinator) -> None:
        super().__init__(coordinator, "next_charge_start")

    @property
    def native_value(self) -> datetime | None:
        p = self.coordinator.data
        return p.next_charge.start if p and p.next_charge else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        p = self.coordinator.data
        return _hour(p.next_charge) if p and p.next_charge else {}


class ChargeUntilSensor(_Base):
    """End of the hand-over: the next departure or the last known price hour.

    Use it as the completion time of EV Smart Charging or a similar charger
    integration.
    """

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:clock-end"

    def __init__(self, coordinator: PlannerCoordinator) -> None:
        super().__init__(coordinator, "charge_until")

    @property
    def native_value(self) -> datetime | None:
        p = self.coordinator.data
        return p.charge_until if p else None


class PlanSensor(_Base):
    """The full plan as attributes (planned hours and one row per date) for dashboards."""

    _attr_icon = "mdi:calendar-clock"
    # Large and changing every update: keep them out of the recorder.
    _unrecorded_attributes = frozenset({"hours", "rows", "totals", "trips", "trip_source", "missing_legs"})

    def __init__(self, coordinator: PlannerCoordinator) -> None:
        super().__init__(coordinator, "plan")

    @property
    def native_value(self) -> str | None:
        p = self.coordinator.data
        if not p:
            return None
        return p.next_charge.start.date().isoformat() if p.next_charge else "none"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        p = self.coordinator.data
        if not p:
            return {}
        rows = [
            {
                "date": r.day.isoformat(),
                "type": r.day_type,
                "km": round(r.km, 1),
                "floor": r.floor,
                "departure_hour": r.departure_hour,
                "requirement": r.requirement,
                "charge_pct": r.charge_pct,
                "charge_kwh": r.charge_kwh,
                "cost": r.cost,
                "charge_hours": [ch.start.hour for ch in r.charge_hours],
                "charge_known": all(ch.known for ch in r.charge_hours),
                "soc_departure": r.soc_departure,
                "forecast_factor": r.forecast_factor,
                "calibration": r.calibration,
                "away_overnight": r.away_overnight,
                "status": r.status,
            }
            for r in p.rows
        ]
        totals = {
            "kwh": round(p.kwh_known + p.kwh_forecast, 2),
            "cost": round(p.cost_known + p.cost_forecast, 2),
            "kwh_known": p.kwh_known,
            "cost_known": p.cost_known,
            "kwh_forecast": p.kwh_forecast,
            "cost_forecast": p.cost_forecast,
        }
        trips = {
            d.isoformat(): {"km": t.km, "away_hours": [list(a) for a in t.away], "away_overnight": t.away_overnight, "stops": t.stops}
            for d, t in sorted(self.coordinator.trips.items())
        }
        return {
            "hours": [_hour(ch) for ch in p.hours],
            "rows": rows,
            "totals": totals,
            "trip_source": self.coordinator.trip_source,
            "trips": trips,
            "missing_legs": self.coordinator.missing_legs,
        }
