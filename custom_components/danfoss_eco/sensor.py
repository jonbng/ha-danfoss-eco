"""Sensors for Danfoss Eco."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_REPORT_ROOM_TEMPERATURE,
    DEFAULT_REPORT_ROOM_TEMPERATURE,
    DOMAIN,
    MANUFACTURER,
    MODEL,
)
from .coordinator import EtrvCoordinator


@dataclass(frozen=True)
class DanfossSensorDescription:
    key: str
    name: str
    device_class: SensorDeviceClass | None = None
    native_unit: str | None = None


SENSORS: tuple[DanfossSensorDescription, ...] = (
    DanfossSensorDescription(
        key="battery",
        name="Battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit=PERCENTAGE,
    ),
    DanfossSensorDescription(
        key="name",
        name="Reported Name",
    ),
    DanfossSensorDescription(
        key="last_update",
        name="Last Update",
        device_class=SensorDeviceClass.TIMESTAMP,
    ),
)

ROOM_TEMPERATURE_SENSOR = DanfossSensorDescription(
    key="room_temperature",
    name="Room Temperature",
    device_class=SensorDeviceClass.TEMPERATURE,
    native_unit=UnitOfTemperature.CELSIUS,
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    coordinator: EtrvCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[DanfossEcoSensor] = [
        DanfossEcoSensor(coordinator, entry, description) for description in SENSORS
    ]
    if entry.options.get(CONF_REPORT_ROOM_TEMPERATURE, DEFAULT_REPORT_ROOM_TEMPERATURE):
        entities.append(DanfossEcoSensor(coordinator, entry, ROOM_TEMPERATURE_SENSOR))
    async_add_entities(entities)


class DanfossEcoSensor(CoordinatorEntity[EtrvCoordinator], SensorEntity):
    """Representation of a Danfoss Eco sensor."""

    def __init__(
        self,
        coordinator: EtrvCoordinator,
        entry: ConfigEntry,
        description: DanfossSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._entry = entry
        self._attr_unique_id = f"{entry.unique_id}_{description.key}"
        self._attr_name = f"{entry.title} {description.name}"
        self._attr_device_class = description.device_class
        self._attr_native_unit_of_measurement = description.native_unit

    @property
    def native_value(self) -> Any:
        data = self.coordinator.data or {}
        return data.get(self.entity_description.key)

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.unique_id or self._entry.entry_id)},
            name=self._entry.title,
            manufacturer=MANUFACTURER,
            model=MODEL,
        )
