"""Coordinator for Danfoss Eco devices."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .ble.client import EtrvBleClient, EtrvBleError
from .ble.device import EtrvDevice
from .const import (
    CONF_PIN,
    CONF_POLL_INTERVAL,
    CONF_SECRET_KEY,
    CONF_SETPOINT_DEBOUNCE,
    CONF_STAY_CONNECTED,
    DEFAULT_PIN,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_SETPOINT_DEBOUNCE,
    DEFAULT_STAY_CONNECTED,
)

_LOGGER = logging.getLogger(__name__)


class EtrvCoordinator(DataUpdateCoordinator[dict[str, object]]):
    """Handle polling and commands for a Danfoss Eco device."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        options = entry.options
        poll_interval = options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL.seconds)
        update_interval = timedelta(seconds=poll_interval)

        super().__init__(
            hass,
            _LOGGER,
            name=entry.title,
            update_interval=update_interval,
        )

        self.entry = entry
        self._client = EtrvBleClient(
            hass,
            entry.unique_id or entry.data["address"],
            entry.data.get(CONF_SECRET_KEY),
            options.get(CONF_PIN, entry.data.get(CONF_PIN, DEFAULT_PIN)),
            options.get(CONF_STAY_CONNECTED, DEFAULT_STAY_CONNECTED),
        )
        self._device = EtrvDevice(self._client)
        self._pending_setpoint: float | None = None

        debounce_seconds = options.get(CONF_SETPOINT_DEBOUNCE, DEFAULT_SETPOINT_DEBOUNCE)
        self._debouncer = Debouncer(
            hass,
            _LOGGER,
            cooldown=debounce_seconds,
            immediate=False,
            function=self._async_apply_setpoint,
        )

    async def async_disconnect(self) -> None:
        await self._client.async_disconnect()

    async def _async_update_data(self) -> dict[str, object]:
        try:
            return await self._device.async_read_state()
        except EtrvBleError as err:
            raise UpdateFailed(str(err)) from err
        except Exception as err:  # pragma: no cover - safety net
            raise UpdateFailed(f"Unexpected error: {err}") from err

    async def async_set_temperature(self, temperature: float) -> None:
        self._pending_setpoint = temperature
        await self._debouncer.async_call()

    async def _async_apply_setpoint(self) -> None:
        if self._pending_setpoint is None:
            return
        try:
            await self._device.async_set_temperature(self._pending_setpoint)
        except EtrvBleError as err:
            raise UpdateFailed(str(err)) from err
        finally:
            self._pending_setpoint = None
        await self.async_request_refresh()

    async def async_config_entry_first_refresh(self) -> None:
        try:
            await super().async_config_entry_first_refresh()
        except UpdateFailed as err:
            raise ConfigEntryNotReady from err
