"""Danfoss Eco integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant

from .ble import close_stale_connections_by_address
from .const import DOMAIN
from .coordinator import EtrvCoordinator

PLATFORMS: list[Platform] = [Platform.CLIMATE, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Danfoss Eco from a config entry."""
    address = entry.unique_id or entry.data.get(CONF_ADDRESS, "")

    # Clear any stale BLE connections before attempting to connect.
    # This prevents "already_in_progress" errors from lingering connections.
    if address:
        await close_stale_connections_by_address(address)

    coordinator = EtrvCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    coordinator: EtrvCoordinator | None = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if coordinator is not None:
        await coordinator.async_disconnect()
    return unload_ok
