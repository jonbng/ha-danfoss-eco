"""BLE client for Danfoss Eco devices."""

from __future__ import annotations

import asyncio
import logging
import random
import time
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

from ..const import HANDLE_MAP, UUID_PIN
from .crypto import EtrvDecodeError, etrv_decode, etrv_encode

_LOGGER = logging.getLogger(__name__)

DEFAULT_CONNECT_TIMEOUT = 90.0
DEFAULT_GATT_TIMEOUT = 90.0

# Connection hardening (mirrors scripts/eco_tool.py approach):
# - Avoid rapid connect/disconnect thrash in BlueZ
# - Add backoff + jitter between attempts
# - Re-run stale connection cleanup during retries
CONNECT_ATTEMPT_TIMEOUT = 20.0
CONNECT_RETRY_DELAY = 0.5
CONNECT_BACKOFF_MAX = 8.0
CONNECT_BACKOFF_JITTER = 0.35
CONNECT_STALE_CLOSE_EVERY = 8.0


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

        deadline = time.monotonic() + (timeout if timeout is not None else DEFAULT_CONNECT_TIMEOUT)
        attempt = 0
        last_err: Exception | None = None
        last_stale_close = 0.0

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break

            attempt += 1

            # Periodically clear stale connections; BlueZ can get stuck with
            # org.bluez.Error.InProgress / lingering connection attempts.
            if (time.monotonic() - last_stale_close) > CONNECT_STALE_CLOSE_EVERY:
                try:
                    await close_stale_connections_by_address(self._address)
                    last_stale_close = time.monotonic()
                except Exception as err:
                    _LOGGER.debug(
                        "close_stale_connections_by_address(%s) failed: %s",
                        self._address,
                        err,
                    )

            ble_device = bluetooth.async_ble_device_from_address(
                self._hass, self._address, connectable=True
            )
            if ble_device is None:
                # If HA doesn't currently have a connectable BLEDevice, back off and retry.
                last_err = EtrvBleError(f"No connectable device found for {self._address}")
            else:
                per_attempt = min(CONNECT_ATTEMPT_TIMEOUT, remaining)
                started = time.monotonic()
                try:
                    async with asyncio.timeout(per_attempt + 5.0):
                        # We manage our own backoff/retry loop; keep the connector
                        # to a single attempt to avoid compounding timeouts.
                        client = await establish_connection(
                            BleakClientWithServiceCache,
                            ble_device,
                            self._address,
                            max_attempts=1,
                        )
                    _LOGGER.debug(
                        "Connected to %s on attempt %d in %.2fs",
                        self._address,
                        attempt,
                        time.monotonic() - started,
                    )

                    self._client = client
                    self._pin_sent = False
                    if send_pin:
                        await self._send_pin_once()
                    return client
                except TimeoutError as exc:
                    last_err = exc
                except BleakError as exc:
                    last_err = exc

            # Exponential backoff with jitter to reduce adapter thrash.
            backoff = min(CONNECT_BACKOFF_MAX, CONNECT_RETRY_DELAY * (2 ** max(0, attempt - 1)))
            backoff += random.uniform(0.0, backoff * CONNECT_BACKOFF_JITTER)
            sleep_for = min(backoff, max(0.0, deadline - time.monotonic()))
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)

        raise EtrvBleTimeoutError(
            f"Timeout connecting to {self._address} after {attempt} attempt(s): {last_err}"
        )

    async def _send_pin_once(self) -> None:
        if self._pin_sent:
            return
        if self._client is None:
            return
        pin_bytes = int(self._pin).to_bytes(4, byteorder="big", signed=False)
        _LOGGER.debug(
            "Sending PIN %s to %s (bytes: %s)",
            self._pin,
            self._address,
            pin_bytes.hex(),
        )
        try:
            await self._client.write_gatt_char(UUID_PIN, pin_bytes, response=True)
        except BleakError as exc:
            _LOGGER.error("Failed to write PIN to %s: %s", self._address, exc)
            raise EtrvBleError(f"Failed to send PIN: {exc}") from exc
        await asyncio.sleep(0.5)
        self._pin_sent = True
        _LOGGER.debug("PIN sent successfully to %s", self._address)

    async def _read_gatt_char_with_timeout(
        self, client: BleakClient, char_specifier: int | str, timeout: float
    ) -> bytes:
        """Read GATT char, trying with timeout kwarg first, then without for compatibility.
        
        Some Bleak backends accept a `timeout` kwarg; older ones raise TypeError, so we
        fall back gracefully.
        """
        try:
            return bytes(await client.read_gatt_char(char_specifier, timeout=timeout))
        except TypeError as err:
            if "timeout" in str(err):
                # Older backend - timeout kwarg not supported
                _LOGGER.debug(
                    "timeout kwarg not supported by backend, "
                    "falling back to default timeout"
                )
                return bytes(await client.read_gatt_char(char_specifier))
            raise

    async def _read_gatt_char(
        self, client: BleakClient, char_uuid: str, timeout: float = DEFAULT_GATT_TIMEOUT
    ) -> bytes:
        """Read a GATT characteristic, trying handle first then UUID fallback."""
        handle = HANDLE_MAP.get(char_uuid)
        if handle is not None:
            try:
                _LOGGER.debug("Reading handle 0x%02X for %s (timeout=%.1fs)", handle, char_uuid[-8:], timeout)
                result = await self._read_gatt_char_with_timeout(client, handle, timeout)
                _LOGGER.debug("Handle 0x%02X read success (%d bytes)", handle, len(result))
                return result
            except BleakError as err:
                _LOGGER.debug(
                    "Handle 0x%02X read failed: %s, falling back to UUID",
                    handle,
                    err,
                )
        _LOGGER.debug("Reading UUID %s (timeout=%.1fs)", char_uuid[-8:], timeout)
        result = await self._read_gatt_char_with_timeout(client, char_uuid, timeout)
        _LOGGER.debug("UUID read success (%d bytes)", len(result))
        return result

    async def _write_gatt_char(
        self, client: BleakClient, char_uuid: str, data: bytes
    ) -> None:
        """Write to a GATT characteristic, trying handle first then UUID fallback."""
        handle = HANDLE_MAP.get(char_uuid)
        if handle is not None:
            try:
                await client.write_gatt_char(handle, data, response=True)
                return
            except BleakError:
                _LOGGER.debug(
                    "Handle 0x%02X write failed for %s, falling back to UUID",
                    handle,
                    char_uuid,
                )
        await client.write_gatt_char(char_uuid, data, response=True)

    async def async_read(
        self,
        char_uuid: str,
        *,
        decode: bool = True,
        send_pin: bool = True,
        timeout: float | None = None,
        gatt_timeout: float = DEFAULT_GATT_TIMEOUT,
    ) -> bytes:
        async with self._lock:
            client = await self._ensure_connected(send_pin=send_pin, timeout=timeout)
            data = await self._read_gatt_char(client, char_uuid, timeout=gatt_timeout)
            if decode:
                if self._secret is None:
                    raise EtrvBleError("Secret key missing for decode")
                try:
                    data = etrv_decode(bytes(data), self._secret)
                except EtrvDecodeError as exc:
                    raise EtrvBleError(str(exc)) from exc
            if not self._stay_connected:
                await self.async_disconnect()
            return bytes(data)

    async def async_read_many(
        self,
        char_uuids: Iterable[str],
        *,
        decode: bool = True,
        send_pin: bool = True,
        gatt_timeout: float = DEFAULT_GATT_TIMEOUT,
    ) -> dict[str, bytes]:
        async with self._lock:
            client = await self._ensure_connected(send_pin=send_pin)
            results: dict[str, bytes] = {}
            for char_uuid in char_uuids:
                data = await self._read_gatt_char(client, char_uuid, timeout=gatt_timeout)
                if decode:
                    if self._secret is None:
                        raise EtrvBleError("Secret key missing for decode")
                    try:
                        data = etrv_decode(data, self._secret)
                    except EtrvDecodeError as exc:
                        raise EtrvBleError(str(exc)) from exc
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
            await self._write_gatt_char(client, char_uuid, payload)
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
