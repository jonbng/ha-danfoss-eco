"""Device access for Danfoss Eco eTRV."""

from __future__ import annotations

from datetime import datetime, timezone

from ..const import (
    MIN_TEMP_C,
    MAX_TEMP_C,
    UUID_BATTERY,
    UUID_NAME,
    UUID_SECRET_KEY,
    UUID_TEMPERATURE,
)
from .client import EtrvBleClient
from .structs import BatteryData, NameData, TemperatureData


class EtrvDevice:
    """High-level device operations."""

    def __init__(self, client: EtrvBleClient) -> None:
        self._client = client

    async def async_read_state(self) -> dict[str, object]:
        # Battery is standard BLE characteristic (0x2A19) - NOT XXTEA encrypted
        # Must read it separately with decode=False
        battery_raw = await self._client.async_read(
            UUID_BATTERY,
            decode=False,
            send_pin=False,  # Battery doesn't require PIN
        )
        battery = BatteryData.from_bytes(battery_raw).battery

        # Temperature and Name are Danfoss custom characteristics - XXTEA encrypted
        results = await self._client.async_read_many(
            [UUID_TEMPERATURE, UUID_NAME],
            decode=True,
            send_pin=True,
        )
        temp = TemperatureData.from_bytes(results[UUID_TEMPERATURE])
        name = NameData.from_bytes(results[UUID_NAME]).name
        return {
            "battery": battery,
            "room_temperature": temp.room_temperature,
            "set_point_temperature": temp.set_point,
            "name": name,
            "last_update": datetime.now(timezone.utc),
            "raw_temperature": temp,
        }

    async def async_set_temperature(self, temperature: float) -> None:
        bounded = max(MIN_TEMP_C, min(MAX_TEMP_C, temperature))
        raw = await self._client.async_read(
            UUID_TEMPERATURE,
            decode=True,
            send_pin=True,
        )
        temp = TemperatureData.from_bytes(raw)
        payload = temp.with_set_point(bounded)
        await self._client.async_write(
            UUID_TEMPERATURE,
            payload,
            encode=True,
            send_pin=True,
        )

    async def async_get_secret_key(self) -> str:
        return await self._client.async_get_secret_key(UUID_SECRET_KEY)
