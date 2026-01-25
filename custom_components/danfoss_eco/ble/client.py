"""BLE client for Danfoss Eco devices."""

from __future__ import annotations

import asyncio
from typing import Iterable

from bleak import BleakClient
from bleak.exc import BleakError
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    close_stale_connections_by_address,
    establish_connection,
)
from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant

from ..const import UUID_PIN
from .crypto import etrv_decode, etrv_encode

# Default timeout for BLE connection attempts (seconds)
DEFAULT_CONNECT_TIMEOUT = 15.0


class EtrvBleError(Exception):
    """Error communicating with the device."""


class EtrvBleTimeoutError(EtrvBleError):
    """Timeout connecting to the device."""


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

    async def _ensure_connected(
        self, send_pin: bool, timeout: float | None = None
    ) -> BleakClient:
        if self._client and self._client.is_connected:
            if send_pin:
                await self._send_pin_once()
            return self._client

        # Clear any stale connections to this address before attempting to connect.
        # This prevents "already_in_progress" errors from lingering connection attempts.
        await close_stale_connections_by_address(self._address)

        ble_device = bluetooth.async_ble_device_from_address(
            self._hass, self._address, connectable=True
        )
        if ble_device is None:
            raise EtrvBleError(f"No connectable device found for {self._address}")

        try:
            if timeout is not None:
                async with asyncio.timeout(timeout):
                    client = await establish_connection(
                        BleakClientWithServiceCache,
                        ble_device,
                        self._address,
                    )
            else:
                client = await establish_connection(
                    BleakClientWithServiceCache,
                    ble_device,
                    self._address,
                )
        except TimeoutError as exc:
            raise EtrvBleTimeoutError(
                f"Timeout connecting to {self._address}"
            ) from exc
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
        await self._client.write_gatt_char(UUID_PIN, pin_bytes, response=True)
        await asyncio.sleep(0.2)
        self._pin_sent = True

    async def async_read(
        self,
        char_uuid: str,
        *,
        decode: bool = True,
        send_pin: bool = True,
        timeout: float | None = None,
    ) -> bytes:
        async with self._lock:
            client = await self._ensure_connected(send_pin=send_pin, timeout=timeout)
            data = await client.read_gatt_char(char_uuid)
            if decode:
                if self._secret is None:
                    raise EtrvBleError("Secret key missing for decode")
                data = etrv_decode(bytes(data), self._secret)
            if not self._stay_connected:
                await self.async_disconnect()
            return bytes(data)

    async def async_read_many(
        self,
        char_uuids: Iterable[str],
        *,
        decode: bool = True,
        send_pin: bool = True,
    ) -> dict[str, bytes]:
        async with self._lock:
            client = await self._ensure_connected(send_pin=send_pin)
            results: dict[str, bytes] = {}
            for char_uuid in char_uuids:
                data = await client.read_gatt_char(char_uuid)
                if decode:
                    if self._secret is None:
                        raise EtrvBleError("Secret key missing for decode")
                    data = etrv_decode(bytes(data), self._secret)
                results[char_uuid] = bytes(data)
            if not self._stay_connected:
                await self.async_disconnect()
            return results

    async def async_write(
        self,
        char_uuid: str,
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
            await client.write_gatt_char(char_uuid, payload, response=True)
            if not self._stay_connected:
                await self.async_disconnect()

    async def async_get_secret_key(
        self, char_uuid: str, timeout: float | None = DEFAULT_CONNECT_TIMEOUT
    ) -> str:
        """Read the secret key from the device.

        Args:
            char_uuid: UUID of the secret key characteristic.
            timeout: Connection timeout in seconds. Defaults to DEFAULT_CONNECT_TIMEOUT.

        Returns:
            The secret key as a hex string.

        Raises:
            EtrvBleTimeoutError: If the connection times out.
            EtrvBleError: If the connection fails for other reasons.
        """
        data = await self.async_read(
            char_uuid, decode=False, send_pin=True, timeout=timeout
        )
        return data[:16].hex()
