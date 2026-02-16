#!/usr/bin/env python3
"""Scan, pair, read state, and set temperature for Danfoss Eco eTRV."""

from __future__ import annotations

import argparse
import asyncio
import curses
import logging
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import xxtea
from bleak import BleakClient, BleakScanner
from bleak.exc import BleakDBusError, BleakError
from bleak_retry_connector import close_stale_connections_by_address

if TYPE_CHECKING:
    from bleak.backends.bluezdbus.client import BleakClientBlueZDBus

logger = logging.getLogger(__name__)

# BLE UUIDs for Danfoss Eco eTRV
# Reference: libetrv, Eco2 (C#), DanfossE2.py3
UUID_BATTERY = "00002a19-0000-1000-8000-00805f9b34fb"
UUID_PIN = "10020001-2749-0001-0000-00805f9b042f"
UUID_TEMPERATURE = "10020005-2749-0001-0000-00805f9b042f"  # Set point + room temp
UUID_SETTINGS = "10020003-2749-0001-0000-00805f9b042f"  # Device settings
UUID_NAME = "10020006-2749-0001-0000-00805f9b042f"  # Device name
UUID_SECRET_KEY = "1002000b-2749-0001-0000-00805f9b042f"

# Handle map (from libetrv) - allows bypassing service discovery
# Map: UUID -> handle (int)
HANDLE_MAP: dict[str, int] = {
    UUID_BATTERY: 0x10,
    UUID_PIN: 0x24,
    UUID_SETTINGS: 0x2A,
    UUID_TEMPERATURE: 0x2D,
    UUID_NAME: 0x30,
    UUID_SECRET_KEY: 0x3F,
}

# Service UUID for Danfoss custom service
UUID_DANFOSS_SERVICE = "10020000-2749-0001-0000-00805f9b042f"

# Advertisement flag for pairing mode (from libetrv)
# If bit 2 (0x4) is set in the first byte of the device name, device is in setup mode
PAIRING_MODE_FLAG = 0x4

MIN_TEMP_C = 10.0
MAX_TEMP_C = 40.0
TEMP_STEP = 0.5
POLL_INTERVAL_SECS = 30
DEFAULT_CONNECT_TIMEOUT = 20.0
DEFAULT_GATT_TIMEOUT = 10.0
CONNECT_ATTEMPT_TIMEOUT = 20.0
CONNECT_RETRY_DELAY = 0.5
CONNECT_BACKOFF_MAX = 8.0
CONNECT_BACKOFF_JITTER = 0.35
CONNECT_REFRESH_DEVICE_EVERY = 12.0
CONNECT_STALE_CLOSE_EVERY = 8.0
KEY_RETRIEVE_TIMEOUT = 150.0
KEY_CONNECT_BUDGET = 140.0
KEY_GATT_TIMEOUT = 10.0


def _is_etrv(name: str | None) -> bool:
    if not name:
        return False
    return name.endswith(";eTRV")


def _reverse_chunks(data: bytes) -> bytes:
    result = bytearray()
    for i in range(0, len(data), 4):
        result += data[i : i + 4][::-1]
    return bytes(result)


def etrv_decode(data: bytes, key: bytes) -> bytes:
    data = _reverse_chunks(data)
    data = xxtea.decrypt(data, key, padding=False)
    data = _reverse_chunks(data)
    return data


def etrv_encode(data: bytes, key: bytes) -> bytes:
    data = _reverse_chunks(data)
    data = xxtea.encrypt(data, key, padding=False)
    data = _reverse_chunks(data)
    return data


def _to_temperature(raw: int) -> float:
    return raw * 0.5


def _from_temperature(value: float) -> int:
    return int(round(value / TEMP_STEP))


async def _send_pin(client: BleakClient, pin: int) -> None:
    pin_bytes = int(pin).to_bytes(4, byteorder="big", signed=False)
    await client.write_gatt_char(UUID_PIN, pin_bytes, response=True)
    await asyncio.sleep(0.5)


async def _connect_bleak(
    address: str, timeout: float = DEFAULT_CONNECT_TIMEOUT
) -> BleakClient:
    """Connect with plain BleakClient path (read_secret_key-style)."""
    # Attempt to clear any stale connection state in BlueZ.
    # This helps with org.bluez.Error.InProgress and similar stuck states.
    try:
        await close_stale_connections_by_address(address)
    except Exception as err:
        logger.debug("close_stale_connections_by_address(%s) failed: %s", address, err)

    # Prime BlueZ's device object cache once. This reduces how often we hit
    # intermittent "Device ... was not found" errors on some adapters.
    ble_device = None
    try:
        ble_device = await BleakScanner.find_device_by_address(
            address, timeout=min(6.0, max(2.0, timeout / 10.0))
        )
    except Exception as err:
        logger.debug("Initial device resolve failed for %s: %s", address, err)
    last_resolve = time.monotonic()
    last_stale_close = time.monotonic()

    deadline = time.monotonic() + timeout
    attempt = 0
    last_err: Exception | None = None

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break

        attempt += 1
        # Fewer, longer attempts are more stable than rapid connect/disconnect loops.
        per_attempt = min(CONNECT_ATTEMPT_TIMEOUT, remaining)
        started = time.monotonic()

        # If we get "not found" style errors, refresh discovery occasionally.
        if ble_device is None and (time.monotonic() - last_resolve) > CONNECT_REFRESH_DEVICE_EVERY:
            try:
                ble_device = await BleakScanner.find_device_by_address(
                    address, timeout=3.0
                )
                last_resolve = time.monotonic()
            except Exception as err:
                logger.debug("Device re-resolve failed for %s: %s", address, err)
                last_resolve = time.monotonic()

        client = BleakClient(ble_device or address, timeout=per_attempt)
        try:
            # Keep this equivalent to scripts/read_secret_key.py behavior.
            # NOTE: On Linux/BlueZ this ultimately maps to org.bluez.Device1.Connect.
            # We pass timeout explicitly so the backend doesn't pick a shorter default.
            await client.connect(timeout=per_attempt)
            logger.debug(
                "Connected to %s on attempt %d in %.2fs",
                address,
                attempt,
                time.monotonic() - started,
            )
            return client
        except Exception as err:
            last_err = err
            logger.debug(
                "Connect attempt %d failed after %.2fs: %s",
                attempt,
                time.monotonic() - started,
                err,
            )
            if isinstance(err, BleakDBusError) and "InProgress" in str(err):
                logger.debug("BlueZ connect still in progress; backing off before retry")
            err_s = str(err).lower()
            if "not found" in err_s:
                ble_device = None
                last_resolve = 0.0
            if ("inprogress" in err_s or "in progress" in err_s or "org.bluez.error.failed" in err_s) and (
                time.monotonic() - last_stale_close
            ) > CONNECT_STALE_CLOSE_EVERY:
                # Clear stuck connection state occasionally during retries.
                try:
                    await close_stale_connections_by_address(address)
                    last_stale_close = time.monotonic()
                except Exception as stale_err:
                    logger.debug("stale close failed during retry: %s", stale_err)
            if client.is_connected:
                await client.disconnect()

            # Exponential backoff with jitter reduces discovery/connect thrash in BlueZ.
            backoff = min(CONNECT_BACKOFF_MAX, CONNECT_RETRY_DELAY * (2 ** max(0, attempt - 1)))
            backoff += random.uniform(0.0, backoff * CONNECT_BACKOFF_JITTER)
            sleep_for = min(backoff, max(0.0, deadline - time.monotonic()))
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)

    raise RuntimeError(
        f"Failed to connect to {address} within {timeout:.1f}s after "
        f"{attempt} attempt(s): {last_err}"
    )


async def _read_gatt_char_with_timeout(
    client: BleakClient, char_specifier: int | str, timeout: float
) -> bytes:
    try:
        return bytes(await client.read_gatt_char(char_specifier, timeout=timeout))
    except TypeError as err:
        if "timeout" in str(err):
            logger.debug(
                "timeout kwarg unsupported by bleak backend, using default timeout"
            )
            return bytes(await client.read_gatt_char(char_specifier))
        raise


async def _read_gatt_char(
    client: BleakClient, char_uuid: str, timeout: float = DEFAULT_GATT_TIMEOUT
) -> bytes:
    """Read a GATT characteristic, trying handle first then UUID fallback."""
    handle = HANDLE_MAP.get(char_uuid)
    if handle is not None:
        try:
            return await _read_gatt_char_with_timeout(client, handle, timeout)
        except BleakError:
            logger.debug("Handle 0x%02X failed, falling back to UUID", handle)
    return await _read_gatt_char_with_timeout(client, char_uuid, timeout)


async def _write_gatt_char(client: BleakClient, char_uuid: str, data: bytes) -> None:
    """Write to a GATT characteristic, trying handle first then UUID fallback."""
    handle = HANDLE_MAP.get(char_uuid)
    if handle is not None:
        try:
            await client.write_gatt_char(handle, data, response=True)
            return
        except BleakError:
            logger.debug("Handle 0x%02X failed, falling back to UUID", handle)
    await client.write_gatt_char(char_uuid, data, response=True)


@dataclass
class ScannedDevice:
    address: str
    name: str | None
    in_pairing_mode: bool = False


def _parse_pairing_mode(name: str | None) -> bool:
    """Check if device is in pairing mode based on advertisement flags.
    
    The advertisement data format is: [Flags][MAC addr][Device type]
    If bit 2 (0x4) of Flags is set, device is in setup/pairing mode.
    """
    if not name or not name.endswith(";eTRV"):
        return False
    try:
        flags = ord(name[0])
        return bool(flags & PAIRING_MODE_FLAG)
    except (IndexError, TypeError):
        return False


async def _scan_devices(timeout: float, show_all: bool) -> list[ScannedDevice]:
    devices = await BleakScanner.discover(timeout=timeout)
    found: list[ScannedDevice] = []
    for device in devices:
        name = device.name or getattr(device, "local_name", None)
        if show_all or _is_etrv(name):
            in_pairing_mode = _parse_pairing_mode(name)
            found.append(ScannedDevice(device.address, name, in_pairing_mode))
    return found


async def scan(timeout: float, show_all: bool) -> None:
    devices = await _scan_devices(timeout, show_all)
    for device in devices:
        mode = " [PAIRING]" if device.in_pairing_mode else ""
        print(f"{device.address} | {device.name}{mode}")


async def _get_secret_key_value(address: str, pin: int = 0) -> str:
    """Read secret key from device. Device must be in pairing mode (timer button pressed)."""
    logger.debug("Reading key from %s with %.1fs budget", address, KEY_RETRIEVE_TIMEOUT)
    client: BleakClient | None = None
    try:
        async with asyncio.timeout(KEY_RETRIEVE_TIMEOUT):
            client = await _connect_bleak(address, timeout=KEY_CONNECT_BUDGET)
            logger.debug("Connected to %s", address)
            await _send_pin(client, pin)
            logger.debug("PIN written to %s", address)
            data = await _read_gatt_char(
                client, UUID_SECRET_KEY, timeout=KEY_GATT_TIMEOUT
            )
            logger.debug("Secret key read from %s", address)
            return bytes(data)[:16].hex()
    except TimeoutError as exc:
        raise RuntimeError(
            f"Key retrieval exceeded {KEY_RETRIEVE_TIMEOUT:.0f}s. Ensure pairing mode is active, "
            "device is close, and no other process is connected."
        ) from exc
    except Exception as e:
        logger.error("Failed to read secret key from %s: %s", address, e)
        raise RuntimeError(
            f"Failed to read secret key: {e}. "
            "Make sure the timer button was pressed to enter pairing mode."
        ) from e
    finally:
        if client and client.is_connected:
            await client.disconnect()


async def get_secret_key(
    address: str, pin: int = 0, wait_for_enter: bool = True
) -> None:
    if wait_for_enter:
        print("Push the timer button on the thermostat, then press Enter...")
        input()
    print(await _get_secret_key_value(address, pin))


async def _read_info_data(
    address: str,
    secret_key: str,
    pin: int,
    skip_battery: bool,
) -> dict[str, object]:
    key = bytes.fromhex(secret_key)
    client = await _connect_bleak(address, timeout=DEFAULT_CONNECT_TIMEOUT)
    try:
        await _send_pin(client, pin)

        temp_raw = await _read_gatt_char(
            client, UUID_TEMPERATURE, timeout=DEFAULT_GATT_TIMEOUT
        )
        name_raw = await _read_gatt_char(
            client, UUID_NAME, timeout=DEFAULT_GATT_TIMEOUT
        )
        battery_raw = None
        if not skip_battery:
            battery_raw = await _read_gatt_char(
                client, UUID_BATTERY, timeout=DEFAULT_GATT_TIMEOUT
            )
    finally:
        if client.is_connected:
            await client.disconnect()

    temp_decoded = etrv_decode(bytes(temp_raw), key)
    name_decoded = etrv_decode(bytes(name_raw), key)

    set_point = _to_temperature(temp_decoded[0])
    room_temp = _to_temperature(temp_decoded[1])
    name = name_decoded.decode("utf-8").rstrip("\0")

    return {
        "battery": battery_raw[0] if battery_raw is not None else None,
        "room_temp": room_temp,
        "set_point": set_point,
        "name": name,
    }


async def read_info(
    address: str,
    secret_key: str,
    pin: int,
    skip_battery: bool,
) -> None:
    info = await _read_info_data(address, secret_key, pin, skip_battery)
    if info["battery"] is not None:
        print(f"battery: {info['battery']}%")
    print(f"room_temp: {info['room_temp']} C")
    print(f"set_point: {info['set_point']} C")
    print(f"name: {info['name']}")


async def _set_temperature_value(
    address: str,
    secret_key: str,
    temperature: float,
    pin: int,
) -> float:
    key = bytes.fromhex(secret_key)
    bounded = max(MIN_TEMP_C, min(MAX_TEMP_C, temperature))

    client = await _connect_bleak(address, timeout=DEFAULT_CONNECT_TIMEOUT)
    try:
        await _send_pin(client, pin)
        temp_raw = await _read_gatt_char(
            client, UUID_TEMPERATURE, timeout=DEFAULT_GATT_TIMEOUT
        )
        decoded = etrv_decode(bytes(temp_raw), key)
        raw = bytearray(decoded)
        raw[0] = _from_temperature(bounded)
        payload = etrv_encode(bytes(raw), key)
        await _write_gatt_char(client, UUID_TEMPERATURE, payload)
    finally:
        if client.is_connected:
            await client.disconnect()
    return bounded


async def set_temperature(
    address: str,
    secret_key: str,
    temperature: float,
    pin: int,
) -> None:
    bounded = await _set_temperature_value(
        address, secret_key, temperature, pin
    )
    if abs(bounded - temperature) > 0.0001:
        print(f"clamped_set_point: {bounded} C")
    print(f"set_point_updated: {bounded} C")


_EXECUTOR = ThreadPoolExecutor(max_workers=1)


def _run_async(coro):
    return asyncio.run(coro)


def _run_async_with_spinner(
    stdscr, coro, label: str, timeout: float | None = None
):
    start = time.monotonic()
    future = _EXECUTOR.submit(_run_async, coro)
    spinner = ["|", "/", "-", "\\"]
    idx = 0
    stdscr.nodelay(True)
    while not future.done():
        if timeout is not None and (time.monotonic() - start) > timeout:
            stdscr.nodelay(False)
            raise TimeoutError(f"{label} timed out after {timeout:.0f}s")
        _message(stdscr, f"{label} {spinner[idx % len(spinner)]}")
        idx += 1
        time.sleep(0.1)
        stdscr.getch()
    stdscr.nodelay(False)
    return future.result()


def _prompt(stdscr, prompt: str) -> str:
    height, width = stdscr.getmaxyx()
    stdscr.move(height - 2, 0)
    stdscr.clrtoeol()
    stdscr.addstr(height - 2, 0, f"{prompt}: ")
    stdscr.refresh()
    curses.echo()
    curses.curs_set(1)
    value = stdscr.getstr(height - 2, len(prompt) + 2, width - len(prompt) - 3)
    curses.noecho()
    curses.curs_set(0)
    return value.decode().strip()


def _message(stdscr, text: str) -> None:
    height, width = stdscr.getmaxyx()
    stdscr.move(height - 1, 0)
    stdscr.clrtoeol()
    stdscr.addstr(height - 1, 0, text[: width - 1])
    stdscr.refresh()


def _draw_list(
    stdscr,
    title: str,
    items: list[str],
    selected: int,
    footer: str,
) -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    stdscr.addstr(0, 0, title[: width - 1])
    for idx, item in enumerate(items):
        row = 2 + idx
        if row >= height - 2:
            break
        if idx == selected:
            stdscr.attron(curses.A_REVERSE)
        stdscr.addstr(row, 0, item[: width - 1])
        if idx == selected:
            stdscr.attroff(curses.A_REVERSE)
    stdscr.addstr(height - 2, 0, footer[: width - 1])
    stdscr.refresh()


def _device_menu(stdscr, device: ScannedDevice, state: dict[str, object]) -> None:
    selected = 0
    while True:
        secret = state.get("secret_key") or ""
        pin = state.get("pin")
        pin_label = "" if pin is None else str(pin)
        header = f"{device.address} | {device.name or '-'}"
        mode = " [PAIRING]" if device.in_pairing_mode else ""
        items = [
            f"Read secret key{mode}",
            "Read info",
            "Set temperature",
            f"Set secret key (current: {secret or '-'})",
            f"Set PIN (current: {pin_label or '-'})",
            "Back",
        ]
        _draw_list(
            stdscr,
            header,
            items,
            selected,
            "Enter=select  Up/Down=move  q=back",
        )
        key = stdscr.getch()
        if key in (ord("q"), 27):
            return
        if key == curses.KEY_UP and selected > 0:
            selected -= 1
            continue
        if key == curses.KEY_DOWN and selected < len(items) - 1:
            selected += 1
            continue
        if key not in (10, 13):
            continue

        if selected == 0:
            _message(stdscr, "Push the timer button on the thermostat...")
            stdscr.refresh()
            try:
                secret_key = _run_async_with_spinner(
                    stdscr,
                    _get_secret_key_value(device.address, pin if isinstance(pin, int) else 0),
                    "Reading secret key",
                    timeout=120.0,
                )
                state["secret_key"] = secret_key
                _message(stdscr, "Secret key read and stored.")
            except Exception as exc:
                _message(stdscr, f"Failed to read key: {exc}")
        elif selected == 1:
            if not secret:
                _message(stdscr, "Secret key missing. Use 'Read secret key' or set it.")
                continue
            try:
                info = _run_async_with_spinner(
                    stdscr,
                    _read_info_data(
                        device.address,
                        str(secret),
                        pin if isinstance(pin, int) else 0,
                        False,
                    ),
                    "Reading info",
                    timeout=120.0,
                )
                battery = info["battery"]
                msg = (
                    f"battery={battery}% "
                    f"room={info['room_temp']}C "
                    f"set={info['set_point']}C "
                    f"name={info['name']}"
                )
                _message(stdscr, msg)
            except Exception as exc:
                _message(stdscr, f"Read failed: {exc}")
        elif selected == 2:
            if not secret:
                _message(stdscr, "Secret key missing. Use 'Read secret key' or set it.")
                continue
            value = _prompt(stdscr, "Set temperature (C)")
            try:
                target = float(value)
                bounded = _run_async_with_spinner(
                    stdscr,
                    _set_temperature_value(
                        device.address,
                        str(secret),
                        target,
                        pin if isinstance(pin, int) else 0,
                    ),
                    "Setting temperature",
                    timeout=120.0,
                )
                _message(stdscr, f"Setpoint updated to {bounded} C")
            except Exception as exc:
                _message(stdscr, f"Set failed: {exc}")
        elif selected == 3:
            value = _prompt(
                stdscr,
                "Secret key (32 hex chars, empty to clear)",
            )
            if value == "":
                state["secret_key"] = ""
                _message(stdscr, "Secret key cleared.")
            else:
                try:
                    bytes.fromhex(value)
                except ValueError:
                    _message(stdscr, "Invalid hex string.")
                else:
                    state["secret_key"] = value
                    _message(stdscr, "Secret key updated.")
        elif selected == 4:
            value = _prompt(stdscr, "PIN (empty to clear)")
            if value == "":
                state["pin"] = None
                _message(stdscr, "PIN cleared.")
            else:
                try:
                    pin_value = int(value)
                    if pin_value < 0 or pin_value > 9999:
                        raise ValueError
                except ValueError:
                    _message(stdscr, "PIN must be 0-9999.")
                else:
                    state["pin"] = pin_value
                    _message(stdscr, "PIN updated.")
        elif selected == 5:
            return


def _tui_main(stdscr) -> None:
    curses.curs_set(0)
    stdscr.keypad(True)
    devices: list[ScannedDevice] = []
    selected = 0
    per_device_state: dict[str, dict[str, object]] = {}
    status = "Press r to scan for devices."

    while True:
        items = [
            f"{device.address} | {device.name or '-'}" for device in devices
        ] or ["<no devices>"]
        _draw_list(
            stdscr,
            "Danfoss Eco TUI",
            items,
            selected,
            f"{status}  Enter=open  r=scan  q=quit",
        )
        key = stdscr.getch()
        if key in (ord("q"), 27):
            return
        if key == ord("r"):
            status = "Scanning..."
            _message(stdscr, status)
            try:
                devices = _run_async_with_spinner(
                    stdscr,
                    _scan_devices(8.0, False),
                    "Scanning",
                    timeout=20.0,
                )
                selected = 0
                status = f"Found {len(devices)} device(s)."
            except Exception as exc:
                status = f"Scan failed: {exc}"
            continue
        if key == curses.KEY_UP and selected > 0:
            selected -= 1
            continue
        if key == curses.KEY_DOWN and selected < max(0, len(items) - 1):
            selected += 1
            continue
        if key in (10, 13) and devices:
            device = devices[selected]
            state = per_device_state.setdefault(
                device.address,
                {"secret_key": "", "pin": None},
            )
            _device_menu(stdscr, device, state)
            status = "Ready."


def tui() -> None:
    curses.wrapper(_tui_main)



@dataclass
class WizardState:
    phase: str = "scanning"
    devices: list[ScannedDevice] = field(default_factory=list)
    selected_idx: int = 0
    selected_device: ScannedDevice | None = None
    secret_key: str = ""
    pin: int | None = None
    info: dict[str, object] | None = None
    error: str = ""
    status_msg: str = ""
    last_poll: float = 0.0


def _wizard_draw_scanning(
    stdscr, state: WizardState, scan_done: bool, spinner_idx: int
) -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()

    spinner = ["|", "/", "-", "\\"]
    if scan_done:
        title = f"Danfoss Eco Wizard - Found {len(state.devices)} device(s)"
    else:
        title = f"Danfoss Eco Wizard - Scanning {spinner[spinner_idx % 4]}"

    stdscr.addstr(0, 0, title[: width - 1], curses.A_BOLD)

    if not state.devices:
        stdscr.addstr(2, 2, "Searching for eTRV devices..."[: width - 3])
    else:
        for idx, dev in enumerate(state.devices):
            row = 2 + idx
            if row >= height - 3:
                break
            mode = " [PAIRING]" if dev.in_pairing_mode else ""
            label = f"{dev.address} | {dev.name or '-'}{mode}"
            if idx == state.selected_idx:
                stdscr.attron(curses.A_REVERSE)
            stdscr.addstr(row, 2, label[: width - 3])
            if idx == state.selected_idx:
                stdscr.attroff(curses.A_REVERSE)

    footer = "[↑↓] Navigate  [Enter] Select  [q] Quit"
    if scan_done:
        footer = "[↑↓] Navigate  [Enter] Select  [r] Rescan  [q] Quit"
    stdscr.addstr(height - 2, 0, footer[: width - 1])

    if state.error:
        stdscr.addstr(height - 1, 0, state.error[: width - 1], curses.A_BOLD)

    stdscr.refresh()


def _wizard_draw_connecting(stdscr, state: WizardState) -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()

    dev = state.selected_device
    if dev is None:
        return
    title = f"Connecting to {dev.name or dev.address}..."
    stdscr.addstr(0, 0, title[: width - 1], curses.A_BOLD)
    stdscr.addstr(2, 2, state.status_msg[: width - 3])

    if state.error:
        stdscr.addstr(height - 1, 0, state.error[: width - 1], curses.A_BOLD)

    stdscr.refresh()


def _wizard_draw_status(stdscr, state: WizardState, spinner_idx: int) -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()

    dev = state.selected_device
    if dev is None:
        return
    title = f"{dev.name or '-'} - {dev.address}"
    stdscr.addstr(0, 0, title[: width - 1], curses.A_BOLD)

    info = state.info or {}
    row = 2

    battery = info.get("battery")
    if battery is not None:
        stdscr.addstr(row, 2, f"Battery:    {battery}%"[: width - 3])
        row += 1

    room_temp = info.get("room_temp")
    if room_temp is not None:
        stdscr.addstr(row, 2, f"Room Temp:  {room_temp}°C"[: width - 3])
        row += 1

    set_point = info.get("set_point")
    if set_point is not None:
        stdscr.addstr(row, 2, f"Setpoint:   {set_point}°C"[: width - 3], curses.A_BOLD)
        row += 1

    name = info.get("name")
    if name:
        stdscr.addstr(row, 2, f"Name:       {name}"[: width - 3])
        row += 1

    row += 1
    elapsed = time.monotonic() - state.last_poll
    next_poll = max(0, POLL_INTERVAL_SECS - elapsed)
    spinner = ["|", "/", "-", "\\"]
    poll_status = f"Next refresh in {int(next_poll)}s {spinner[spinner_idx % 4]}"
    stdscr.addstr(row, 2, poll_status[: width - 3])

    footer = "[+/-] Adjust temp  [r] Refresh  [b] Back  [q] Quit"
    stdscr.addstr(height - 2, 0, footer[: width - 1])

    if state.status_msg:
        stdscr.addstr(height - 1, 0, state.status_msg[: width - 1])
    if state.error:
        stdscr.addstr(height - 1, 0, state.error[: width - 1], curses.A_BOLD)

    stdscr.refresh()


def _wizard_do_connect(stdscr, state: WizardState) -> bool:
    dev = state.selected_device
    if dev is None:
        return False
    state.error = ""

    if not dev.in_pairing_mode:
        state.status_msg = "Push the timer button on the thermostat..."
        _wizard_draw_connecting(stdscr, state)
        stdscr.nodelay(False)
        stdscr.getch()
        stdscr.nodelay(True)

    state.status_msg = "Reading secret key..."
    _wizard_draw_connecting(stdscr, state)

    try:
        secret_key = _run_async_with_spinner(
            stdscr,
            _get_secret_key_value(dev.address, pin=state.pin or 0),
            "Pairing",
            timeout=120.0,
        )
        state.secret_key = secret_key
        state.status_msg = f"Secret key: {secret_key[:8]}..."
        _wizard_draw_connecting(stdscr, state)
    except Exception as exc:
        state.error = f"Pairing failed: {exc}"
        return False

    state.status_msg = "Reading device data..."
    _wizard_draw_connecting(stdscr, state)

    try:
        info = _run_async_with_spinner(
            stdscr,
            _read_info_data(
                dev.address,
                state.secret_key,
                state.pin or 0,
                skip_battery=False,
            ),
            "Reading",
            timeout=120.0,
        )
        state.info = info
        state.last_poll = time.monotonic()
    except Exception as exc:
        state.error = f"Read failed: {exc}"
        return False

    return True


def _wizard_refresh_info(stdscr, state: WizardState) -> bool:
    dev = state.selected_device
    if dev is None:
        return False
    state.error = ""
    state.status_msg = "Refreshing..."

    try:
        info = _run_async_with_spinner(
            stdscr,
            _read_info_data(
                dev.address,
                state.secret_key,
                state.pin or 0,
                skip_battery=False,
            ),
            "Refreshing",
            timeout=120.0,
        )
        state.info = info
        state.last_poll = time.monotonic()
        state.status_msg = "Updated."
        return True
    except Exception as exc:
        state.error = f"Refresh failed: {exc}"
        return False


def _wizard_adjust_temp(stdscr, state: WizardState, delta: float) -> None:
    dev = state.selected_device
    if dev is None:
        return
    current: float = 20.0
    if state.info:
        raw = state.info.get("set_point")
        if isinstance(raw, (int, float)):
            current = float(raw)
    new_temp = max(MIN_TEMP_C, min(MAX_TEMP_C, current + delta))

    state.status_msg = f"Setting to {new_temp}°C..."
    state.error = ""

    try:
        bounded = _run_async_with_spinner(
            stdscr,
            _set_temperature_value(
                dev.address,
                state.secret_key,
                new_temp,
                state.pin or 0,
            ),
            "Setting",
            timeout=120.0,
        )
        if state.info:
            state.info["set_point"] = bounded
        state.status_msg = f"Setpoint: {bounded}°C"
    except Exception as exc:
        state.error = f"Set failed: {exc}"


def _wizard_main(stdscr) -> None:
    curses.curs_set(0)
    stdscr.keypad(True)
    stdscr.nodelay(True)

    state = WizardState()
    scan_future = _EXECUTOR.submit(_run_async, _scan_devices(10.0, False))
    scan_done = False
    spinner_idx = 0

    while True:
        spinner_idx += 1

        if state.phase == "scanning":
            if scan_future and scan_future.done() and not scan_done:
                try:
                    state.devices = scan_future.result()
                    scan_done = True
                except Exception as exc:
                    state.error = f"Scan failed: {exc}"
                    scan_done = True

            _wizard_draw_scanning(stdscr, state, scan_done, spinner_idx)

            key = stdscr.getch()
            if key == ord("q"):
                return
            if key == curses.KEY_UP and state.selected_idx > 0:
                state.selected_idx -= 1
            elif key == curses.KEY_DOWN and state.selected_idx < len(state.devices) - 1:
                state.selected_idx += 1
            elif key == ord("r") and scan_done:
                state.devices = []
                state.selected_idx = 0
                scan_done = False
                state.error = ""
                scan_future = _EXECUTOR.submit(_run_async, _scan_devices(10.0, False))
            elif key in (10, 13) and state.devices:
                state.selected_device = state.devices[state.selected_idx]
                state.phase = "connecting"

            time.sleep(0.1)

        elif state.phase == "connecting":
            stdscr.nodelay(False)
            success = _wizard_do_connect(stdscr, state)
            stdscr.nodelay(True)

            if success:
                state.phase = "status"
            else:
                _wizard_draw_connecting(stdscr, state)
                stdscr.nodelay(False)
                stdscr.addstr(
                    stdscr.getmaxyx()[0] - 2,
                    0,
                    "Press any key to go back...",
                )
                stdscr.refresh()
                stdscr.getch()
                stdscr.nodelay(True)
                state.phase = "scanning"
                state.selected_device = None
                state.secret_key = ""
                state.info = None

        elif state.phase == "status":
            elapsed = time.monotonic() - state.last_poll
            if elapsed >= POLL_INTERVAL_SECS:
                stdscr.nodelay(False)
                _wizard_refresh_info(stdscr, state)
                stdscr.nodelay(True)

            _wizard_draw_status(stdscr, state, spinner_idx)

            key = stdscr.getch()
            if key == ord("q"):
                return
            elif key == ord("b"):
                state.phase = "scanning"
                state.selected_device = None
                state.secret_key = ""
                state.info = None
                state.status_msg = ""
                state.error = ""
            elif key == ord("r"):
                stdscr.nodelay(False)
                _wizard_refresh_info(stdscr, state)
                stdscr.nodelay(True)
            elif key in (ord("+"), ord("=")):
                stdscr.nodelay(False)
                _wizard_adjust_temp(stdscr, state, TEMP_STEP)
                stdscr.nodelay(True)
            elif key in (ord("-"), ord("_")):
                stdscr.nodelay(False)
                _wizard_adjust_temp(stdscr, state, -TEMP_STEP)
                stdscr.nodelay(True)

            time.sleep(0.1)


def wizard() -> None:
    curses.wrapper(_wizard_main)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Danfoss Eco BLE helper. Run without args for all-in-one wizard."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("wizard", help="All-in-one wizard (scan, pair, control).")

    scan_parser = subparsers.add_parser("scan", help="Scan for nearby eTRV devices.")
    scan_parser.add_argument("--timeout", type=float, default=8.0)
    scan_parser.add_argument("--all", action="store_true")

    key_parser = subparsers.add_parser("get-key", help="Read secret key (push timer button first).")
    key_parser.add_argument("address")
    key_parser.add_argument("--pin", type=int, default=0, help="PIN code (default: 0000)")
    key_parser.add_argument(
        "--no-wait-for-enter",
        action="store_true",
        help="Skip interactive Enter prompt before attempting key read.",
    )

    info_parser = subparsers.add_parser("info", help="Read battery/temp/name.")
    info_parser.add_argument("address")
    info_parser.add_argument("secret_key")
    info_parser.add_argument("--pin", type=int, default=0, help="PIN code (default: 0000)")
    info_parser.add_argument("--skip-battery", action="store_true")

    set_parser = subparsers.add_parser("set-temp", help="Set target temperature.")
    set_parser.add_argument("address")
    set_parser.add_argument("secret_key")
    set_parser.add_argument("temperature", type=float)
    set_parser.add_argument("--pin", type=int, default=0, help="PIN code (default: 0000)")

    subparsers.add_parser("tui", help="Legacy interactive terminal UI.")

    return parser.parse_args()


def main() -> None:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s | %(levelname)-5s | %(message)s",
        datefmt="%H:%M:%S",
    )
    
    if len(sys.argv) == 1:
        wizard()
        return
    args = _parse_args()
    if args.command == "wizard":
        wizard()
    elif args.command == "scan":
        asyncio.run(scan(args.timeout, args.all))
    elif args.command == "get-key":
        asyncio.run(
            get_secret_key(
                args.address,
                args.pin,
                wait_for_enter=not args.no_wait_for_enter,
            )
        )
    elif args.command == "info":
        asyncio.run(
            read_info(
                args.address,
                args.secret_key,
                args.pin,
                args.skip_battery,
            )
        )
    elif args.command == "set-temp":
        asyncio.run(
            set_temperature(
                args.address,
                args.secret_key,
                args.temperature,
                args.pin,
            )
        )
    elif args.command == "tui":
        tui()


if __name__ == "__main__":
    main()
