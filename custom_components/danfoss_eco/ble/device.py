"""Device access for Danfoss Eco eTRV."""

from __future__ import annotations

from datetime import datetime, timezone

from ..const import (
    HANDLER_BATTERY,
    HANDLER_NAME,
    HANDLER_TEMPERATURE,
    HANDLER_SECRET_KEY,
    MIN_TEMP_C,
    MAX_TEMP_C,
)
from .client import EtrvBleClient
from .structs import BatteryData, NameData, TemperatureData


class EtrvDevice:
    """High-level device operations."""

    def __init__(self, client: EtrvBleClient) -> None:
        self._client = client

    async def async_read_state(self) -> dict[str, object]:
        results = await self._client.async_read_many(
            [HANDLER_BATTERY, HANDLER_TEMPERATURE, HANDLER_NAME],
            decode=True,
            send_pin=True,
        )
        battery = BatteryData.from_bytes(results[HANDLER_BATTERY]).battery
        temp = TemperatureData.from_bytes(results[HANDLER_TEMPERATURE])
        name = NameData.from_bytes(results[HANDLER_NAME]).name
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
            HANDLER_TEMPERATURE,
            decode=True,
            send_pin=True,
        )
        temp = TemperatureData.from_bytes(raw)
        payload = temp.with_set_point(bounded)
        await self._client.async_write(
            HANDLER_TEMPERATURE,
            payload,
            encode=True,
            send_pin=True,
        )

    async def async_get_secret_key(self) -> str:
        return await self._client.async_get_secret_key(HANDLER_SECRET_KEY)
