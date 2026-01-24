#!/usr/bin/env python3
"""Scan, pair, read state, and set temperature for Danfoss Eco eTRV."""

from __future__ import annotations

import argparse
import asyncio
import curses
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import xxtea
from bleak import BleakClient, BleakScanner

UUID_BATTERY = "00002a19-0000-1000-8000-00805f9b34fb"
UUID_PIN = "10020001-2749-0001-0000-00805f9b042f"
UUID_TEMPERATURE = "10020002-2749-0001-0000-00805f9b042f"
UUID_NAME = "10020003-2749-0001-0000-00805f9b042f"
UUID_SECRET_KEY = "1002000b-2749-0001-0000-00805f9b042f"

MIN_TEMP_C = 10.0
MAX_TEMP_C = 40.0
TEMP_STEP = 0.5


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


async def _maybe_pair(client: BleakClient, pair: bool) -> None:
    if pair and hasattr(client, "pair"):
        await client.pair()


async def _send_pin(client: BleakClient, pin: int | None) -> None:
    if pin is None:
        return
    pin_bytes = int(pin).to_bytes(4, byteorder="big", signed=False)
    await client.write_gatt_char(UUID_PIN, pin_bytes, response=True)
    await asyncio.sleep(0.2)


@dataclass
class ScannedDevice:
    address: str
    name: str | None


async def _scan_devices(timeout: float, show_all: bool) -> list[ScannedDevice]:
    devices = await BleakScanner.discover(timeout=timeout)
    found: list[ScannedDevice] = []
    for device in devices:
        name = device.name or getattr(device, "local_name", None)
        if show_all or _is_etrv(name):
            found.append(ScannedDevice(device.address, name))
    return found


async def scan(timeout: float, show_all: bool) -> None:
    devices = await _scan_devices(timeout, show_all)
    for device in devices:
        print(f"{device.address} | {device.name}")


async def _get_secret_key_value(address: str, pair: bool) -> str:
    async with BleakClient(address) as client:
        await _maybe_pair(client, pair)
        data = await asyncio.wait_for(
            client.read_gatt_char(UUID_SECRET_KEY), timeout=10.0
        )
        return bytes(data)[:16].hex()


async def get_secret_key(address: str, pair: bool) -> None:
    print(await _get_secret_key_value(address, pair))


async def _read_info_data(
    address: str,
    secret_key: str,
    pin: int | None,
    pair: bool,
    skip_battery: bool,
) -> dict[str, object]:
    key = bytes.fromhex(secret_key)
    async with BleakClient(address) as client:
        await _maybe_pair(client, pair)
        await _send_pin(client, pin)

        temp_raw = await asyncio.wait_for(
            client.read_gatt_char(UUID_TEMPERATURE), timeout=10.0
        )
        name_raw = await asyncio.wait_for(
            client.read_gatt_char(UUID_NAME), timeout=10.0
        )
        battery_raw = None
        if not skip_battery:
            battery_raw = await asyncio.wait_for(
                client.read_gatt_char(UUID_BATTERY), timeout=10.0
            )

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
    pin: int | None,
    pair: bool,
    skip_battery: bool,
) -> None:
    info = await _read_info_data(address, secret_key, pin, pair, skip_battery)
    if info["battery"] is not None:
        print(f"battery: {info['battery']}%")
    print(f"room_temp: {info['room_temp']} C")
    print(f"set_point: {info['set_point']} C")
    print(f"name: {info['name']}")


async def _set_temperature_value(
    address: str,
    secret_key: str,
    temperature: float,
    pin: int | None,
    pair: bool,
) -> float:
    key = bytes.fromhex(secret_key)
    bounded = max(MIN_TEMP_C, min(MAX_TEMP_C, temperature))

    async with BleakClient(address) as client:
        await _maybe_pair(client, pair)
        await _send_pin(client, pin)
        temp_raw = await asyncio.wait_for(
            client.read_gatt_char(UUID_TEMPERATURE), timeout=10.0
        )
        decoded = etrv_decode(bytes(temp_raw), key)
        raw = bytearray(decoded)
        raw[0] = _from_temperature(bounded)
        payload = etrv_encode(bytes(raw), key)
        await asyncio.wait_for(
            client.write_gatt_char(UUID_TEMPERATURE, payload, response=True),
            timeout=10.0,
        )
    return bounded


async def set_temperature(
    address: str,
    secret_key: str,
    temperature: float,
    pin: int | None,
    pair: bool,
) -> None:
    bounded = await _set_temperature_value(
        address, secret_key, temperature, pin, pair
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
        pair = state.get("pair", False)
        pin_label = "" if pin is None else str(pin)
        header = f"{device.address} | {device.name or '-'}"
        items = [
            "Read secret key (pairing mode)",
            "Read info",
            "Set temperature",
            f"Set secret key (current: {secret or '-'})",
            f"Set PIN (current: {pin_label or '-'})",
            f"Toggle pair before connect (current: {'on' if pair else 'off'})",
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
            try:
                secret_key = _run_async_with_spinner(
                    stdscr,
                    _get_secret_key_value(device.address, True),
                    "Reading secret key",
                    timeout=20.0,
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
                        pin if isinstance(pin, int) else None,
                        bool(pair),
                        False,
                    ),
                    "Reading info",
                    timeout=20.0,
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
                        pin if isinstance(pin, int) else None,
                        bool(pair),
                    ),
                    "Setting temperature",
                    timeout=20.0,
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
            state["pair"] = not bool(pair)
        elif selected == 6:
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
                {"secret_key": "", "pin": None, "pair": False},
            )
            _device_menu(stdscr, device, state)
            status = "Ready."


def tui() -> None:
    curses.wrapper(_tui_main)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Danfoss Eco BLE helper (scan, key, info, set-temp, tui)."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_parser = subparsers.add_parser("scan", help="Scan for nearby eTRV devices.")
    scan_parser.add_argument("--timeout", type=float, default=8.0)
    scan_parser.add_argument("--all", action="store_true")

    key_parser = subparsers.add_parser("get-key", help="Read secret key (pairing mode).")
    key_parser.add_argument("address")
    key_parser.add_argument("--pair", action="store_true")

    info_parser = subparsers.add_parser("info", help="Read battery/temp/name.")
    info_parser.add_argument("address")
    info_parser.add_argument("secret_key")
    info_parser.add_argument("--pin", type=int, default=None)
    info_parser.add_argument("--pair", action="store_true")
    info_parser.add_argument("--skip-battery", action="store_true")

    set_parser = subparsers.add_parser("set-temp", help="Set target temperature.")
    set_parser.add_argument("address")
    set_parser.add_argument("secret_key")
    set_parser.add_argument("temperature", type=float)
    set_parser.add_argument("--pin", type=int, default=None)
    set_parser.add_argument("--pair", action="store_true")

    subparsers.add_parser("tui", help="Interactive terminal UI.")

    return parser.parse_args()


def main() -> None:
    if len(sys.argv) == 1:
        tui()
        return
    args = _parse_args()
    if args.command == "scan":
        asyncio.run(scan(args.timeout, args.all))
    elif args.command == "get-key":
        asyncio.run(get_secret_key(args.address, args.pair))
    elif args.command == "info":
        asyncio.run(
            read_info(
                args.address,
                args.secret_key,
                args.pin,
                args.pair,
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
                args.pair,
            )
        )
    elif args.command == "tui":
        tui()


if __name__ == "__main__":
    main()
