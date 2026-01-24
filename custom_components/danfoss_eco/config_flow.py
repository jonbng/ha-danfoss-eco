"""Config flow for Danfoss Eco integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components import bluetooth
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback

from .ble.client import EtrvBleClient, EtrvBleError
from .ble.device import EtrvDevice
from .const import (
    CONF_PIN,
    CONF_POLL_INTERVAL,
    CONF_REPORT_ROOM_TEMPERATURE,
    CONF_SECRET_KEY,
    CONF_SETPOINT_DEBOUNCE,
    CONF_STAY_CONNECTED,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_REPORT_ROOM_TEMPERATURE,
    DEFAULT_SETPOINT_DEBOUNCE,
    DEFAULT_STAY_CONNECTED,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


def _matches_etrv(service_info: bluetooth.BluetoothServiceInfoBleak) -> bool:
    name = service_info.name or ""
    return name.endswith(";eTRV")


class DanfossEcoConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Danfoss Eco."""

    VERSION = 1

    def __init__(self) -> None:
        self._address: str | None = None
        self._name: str | None = None

    async def async_step_bluetooth(
        self, discovery_info: bluetooth.BluetoothServiceInfoBleak
    ) -> config_entries.FlowResult:
        """Handle Bluetooth discovery."""
        if not _matches_etrv(discovery_info):
            return self.async_abort(reason="not_etrv")

        address = discovery_info.address
        await self.async_set_unique_id(address)
        self._abort_if_unique_id_configured()

        self._address = address
        self._name = discovery_info.name or address
        self.context["title_placeholders"] = {"name": self._name}
        return await self.async_step_pair()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Handle a user-initiated flow."""
        errors: dict[str, str] = {}

        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address)
            self._abort_if_unique_id_configured()
            self._address = address
            self._name = user_input.get("name") or address
            return await self.async_step_pair()

        devices = await self._async_discovered_devices()
        if not devices:
            errors["base"] = "no_devices"

        address_selector: Any
        if devices:
            address_selector = vol.In(devices)
        else:
            address_selector = str

        data_schema = vol.Schema({vol.Required(CONF_ADDRESS): address_selector})

        return self.async_show_form(
            step_id="user",
            data_schema=data_schema,
            errors=errors,
        )

    async def async_step_pair(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Connect and exchange keys during setup."""
        errors: dict[str, str] = {}

        if user_input is not None:
            pin = user_input.get(CONF_PIN)
            assert self._address is not None
            try:
                secret_key = await self._async_get_secret_key(self._address, pin)
            except EtrvBleError as err:
                _LOGGER.warning("Pairing failed: %s", err)
                errors["base"] = "pairing_failed"
            else:
                return self.async_create_entry(
                    title=self._name or self._address,
                    data={
                        CONF_ADDRESS: self._address,
                        CONF_SECRET_KEY: secret_key,
                        CONF_PIN: pin,
                    },
                )

        schema = vol.Schema(
            {
                vol.Optional(CONF_PIN): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=0, max=9999),
                )
            }
        )

        return self.async_show_form(
            step_id="pair",
            data_schema=schema,
            errors=errors,
            description_placeholders={"name": self._name or self._address or ""},
        )

    async def _async_discovered_devices(self) -> dict[str, str]:
        devices: dict[str, str] = {}
        for service_info in bluetooth.async_discovered_service_info(
            self.hass, connectable=True
        ):
            if not _matches_etrv(service_info):
                continue
            address = service_info.address
            name = service_info.name or address
            devices[address] = f"{name} ({address})"
        return devices

    async def _async_get_secret_key(self, address: str, pin: int | None) -> str:
        client = EtrvBleClient(
            self.hass,
            address,
            secret_key=None,
            pin=pin,
            stay_connected=False,
        )
        device = EtrvDevice(client)
        try:
            return await device.async_get_secret_key()
        finally:
            await client.async_disconnect()

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        return DanfossEcoOptionsFlow(config_entry)


class DanfossEcoOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow."""

    def __init__(self, entry: config_entries.ConfigEntry) -> None:
        self._entry = entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = self._entry.options
        data_schema = vol.Schema(
            {
                vol.Required(
                    CONF_POLL_INTERVAL,
                    default=options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL.seconds),
                ): vol.All(vol.Coerce(int), vol.Range(min=1)),
                vol.Required(
                    CONF_STAY_CONNECTED,
                    default=options.get(CONF_STAY_CONNECTED, DEFAULT_STAY_CONNECTED),
                ): bool,
                vol.Required(
                    CONF_REPORT_ROOM_TEMPERATURE,
                    default=options.get(
                        CONF_REPORT_ROOM_TEMPERATURE, DEFAULT_REPORT_ROOM_TEMPERATURE
                    ),
                ): bool,
                vol.Required(
                    CONF_SETPOINT_DEBOUNCE,
                    default=options.get(
                        CONF_SETPOINT_DEBOUNCE, DEFAULT_SETPOINT_DEBOUNCE
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=1)),
            }
        )

        return self.async_show_form(step_id="init", data_schema=data_schema)
