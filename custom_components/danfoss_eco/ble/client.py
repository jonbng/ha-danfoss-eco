"""BLE client for Danfoss Eco devices."""

from __future__ import annotations

import asyncio
from typing import Iterable

from bleak import BleakClient
from bleak.exc import BleakError
from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant

from ..const import HANDLER_PIN
from .crypto import etrv_decode, etrv_encode


class EtrvBleError(Exception):
    """Error communicating with the device."""


class EtrvBleClient:
    """Simple BLE client wrapper for eTRV devices."""

    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        secret_key: str | None,
        pin: int | None,
        stay_connected: bool,
    ) -> None:
        self._hass = hass
        self._address = address
        self._secret = bytes.fromhex(secret_key) if secret_key else None
        self._pin = pin if pin is not None else 0
        self._stay_connected = stay_connected
        self._client: BleakClient | None = None
        self._pin_sent = False
        self._lock = asyncio.Lock()

    async def async_disconnect(self) -> None:
        if self._client and self._client.is_connected:
            await self._client.disconnect()
        self._client = None
        self._pin_sent = False

    async def _ensure_connected(self, send_pin: bool) -> BleakClient:
        if self._client and self._client.is_connected:
            if send_pin:
                await self._send_pin_once()
            return self._client

        ble_device = bluetooth.async_ble_device_from_address(
            self._hass, self._address, connectable=True
        )
        if ble_device is None:
            raise EtrvBleError(f"No connectable device found for {self._address}")

        client = BleakClient(ble_device)
        try:
            await client.connect()
        except BleakError as exc:
            raise EtrvBleError(f"Failed to connect to {self._address}") from exc

        self._client = client
        self._pin_sent = False
        if send_pin:
            await self._send_pin_once()
        return client

    async def _send_pin_once(self) -> None:
        if self._pin_sent:
            return
        if self._client is None:
            return
        pin_bytes = int(self._pin).to_bytes(4, byteorder="big", signed=False)
        await self._client.write_gatt_char(HANDLER_PIN, pin_bytes, response=True)
        self._pin_sent = True

    async def async_read(
        self,
        handler: int,
        *,
        decode: bool = True,
        send_pin: bool = True,
    ) -> bytes:
        async with self._lock:
            client = await self._ensure_connected(send_pin=send_pin)
            data = await client.read_gatt_char(handler)
            if decode:
                if self._secret is None:
                    raise EtrvBleError("Secret key missing for decode")
                data = etrv_decode(bytes(data), self._secret)
            if not self._stay_connected:
                await self.async_disconnect()
            return bytes(data)

    async def async_read_many(
        self,
        handlers: Iterable[int],
        *,
        decode: bool = True,
        send_pin: bool = True,
    ) -> dict[int, bytes]:
        async with self._lock:
            client = await self._ensure_connected(send_pin=send_pin)
            results: dict[int, bytes] = {}
            for handler in handlers:
                data = await client.read_gatt_char(handler)
                if decode:
                    if self._secret is None:
                        raise EtrvBleError("Secret key missing for decode")
                    data = etrv_decode(bytes(data), self._secret)
                results[handler] = bytes(data)
            if not self._stay_connected:
                await self.async_disconnect()
            return results

    async def async_write(
        self,
        handler: int,
        data: bytes,
        *,
        encode: bool = True,
        send_pin: bool = True,
    ) -> None:
        async with self._lock:
            client = await self._ensure_connected(send_pin=send_pin)
            payload = data
            if encode:
                if self._secret is None:
                    raise EtrvBleError("Secret key missing for encode")
                payload = etrv_encode(data, self._secret)
            await client.write_gatt_char(handler, payload, response=True)
            if not self._stay_connected:
                await self.async_disconnect()

    async def async_get_secret_key(self, handler: int) -> str:
        data = await self.async_read(handler, decode=False, send_pin=False)
        return data[:16].hex()
