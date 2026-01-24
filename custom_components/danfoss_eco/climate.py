"""Climate entity for Danfoss Eco."""

from __future__ import annotations

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, MAX_TEMP_C, MIN_TEMP_C, MODEL, TEMP_STEP
from .coordinator import EtrvCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    coordinator: EtrvCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([DanfossEcoClimate(coordinator, entry)])


class DanfossEcoClimate(CoordinatorEntity[EtrvCoordinator], ClimateEntity):
    """Representation of a Danfoss Eco thermostat."""

    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.HEAT]
    _attr_hvac_mode = HVACMode.HEAT
    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE
    _attr_min_temp = MIN_TEMP_C
    _attr_max_temp = MAX_TEMP_C
    _attr_target_temperature_step = TEMP_STEP

    def __init__(self, coordinator: EtrvCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = entry.unique_id
        self._attr_name = entry.title

    @property
    def current_temperature(self) -> float | None:
        data = self.coordinator.data or {}
        return data.get("room_temperature")

    @property
    def target_temperature(self) -> float | None:
        data = self.coordinator.data or {}
        return data.get("set_point_temperature")

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.unique_id or self._entry.entry_id)},
            name=self._entry.title,
            manufacturer=MANUFACTURER,
            model=MODEL,
        )

    async def async_set_temperature(self, **kwargs) -> None:
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        await self.coordinator.async_set_temperature(temperature)
