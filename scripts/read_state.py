#!/usr/bin/env python3
"""Read battery/temperature/name from a thermostat using a known key."""

from __future__ import annotations

import argparse
import asyncio
import struct

import xxtea
from bleak import BleakClient
from bleak.exc import BleakDBusError

HANDLER_BATTERY = 0x10
HANDLER_PIN = 0x24
HANDLER_TEMPERATURE = 0x2D
HANDLER_NAME = 0x30

BATTERY_UUID = "00002a19-0000-1000-8000-00805f9b34fb"
PIN_UUID = "10020001-2749-0001-0000-00805f9b042f"
TEMP_UUID = "10020002-2749-0001-0000-00805f9b042f"
NAME_UUID = "10020003-2749-0001-0000-00805f9b042f"


async def _find_char_by_handle(client: BleakClient, handle: int):
    if hasattr(client, "get_services"):
        services = await client.get_services()
    else:
        services = client.services
    for service in services:
        for char in service.characteristics:
            if getattr(char, "handle", None) == handle:
                return char
    return None


def _reverse_chunks(data: bytes) -> bytes:
    result = bytearray()
    for i in range(0, len(data), 4):
        result += data[i : i + 4][::-1]
    return bytes(result)


def _decode(data: bytes, key: bytes) -> bytes:
    data = _reverse_chunks(data)
    data = xxtea.decrypt(data, key, padding=False)
    data = _reverse_chunks(data)
    return data


def _to_temperature(raw: int) -> float:
    return raw * 0.5


async def _read_state(
    address: str,
    secret_key: str,
    pin: int | None,
    battery_uuid: str | None,
    temp_uuid: str | None,
    name_uuid: str | None,
    pin_uuid: str | None,
    skip_battery: bool,
    pair: bool,
    debug: bool,
) -> None:
    key = bytes.fromhex(secret_key)
    async with BleakClient(address) as client:
        if pair and hasattr(client, "pair"):
            await client.pair()
        pin_char = pin_uuid or PIN_UUID or await _find_char_by_handle(client, HANDLER_PIN)
        battery_char = battery_uuid or BATTERY_UUID or await _find_char_by_handle(client, HANDLER_BATTERY)
        temp_char = temp_uuid or TEMP_UUID or await _find_char_by_handle(client, HANDLER_TEMPERATURE)
        name_char = name_uuid or NAME_UUID or await _find_char_by_handle(client, HANDLER_NAME)

        missing = [
            label
            for label, char in [
                ("battery", battery_char),
                ("temperature", temp_char),
                ("name", name_char),
            ]
            if char is None
        ]
        if missing:
            raise RuntimeError(f"Missing characteristics by handle: {', '.join(missing)}")

        if pin is not None:
            if pin_char is None:
                raise RuntimeError("PIN characteristic handle not found")
            pin_bytes = int(pin).to_bytes(4, byteorder="big", signed=False)
            await client.write_gatt_char(pin_char, pin_bytes, response=True)
            await asyncio.sleep(0.2)

        try:
            temp_raw = await client.read_gatt_char(temp_char)
            name_raw = await client.read_gatt_char(name_char)
            battery_raw = None
            if not skip_battery:
                battery_raw = await client.read_gatt_char(battery_char)
        except BleakDBusError as exc:
            raise RuntimeError(
                "Read failed. Device may require PIN on a different write UUID. "
                "Try scripts/test_pin_write.py to locate the correct PIN characteristic."
            ) from exc

    battery = battery_raw[0] if battery_raw is not None else None
    temp_decoded = _decode(bytes(temp_raw), key)
    name_decoded = _decode(bytes(name_raw), key)

    if debug:
        print(f"raw_temp: {bytes(temp_raw).hex()}")
        print(f"dec_temp: {temp_decoded.hex()}")
        print(f"raw_name: {bytes(name_raw).hex()}")
        print(f"dec_name: {name_decoded.hex()}")

    set_point = _to_temperature(temp_decoded[0])
    room_temp = _to_temperature(temp_decoded[1])
    name = name_decoded.decode("utf-8").rstrip("\0")

    if battery is not None:
        print(f"battery: {battery}%")
    print(f"room_temp: {room_temp} C")
    print(f"set_point: {set_point} C")
    print(f"name: {name}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read Danfoss Eco state via BLE.")
    parser.add_argument("address", help="BLE address (e.g. 00:04:2f:63:33:ce)")
    parser.add_argument("secret_key", help="Secret key (32 hex chars)")
    parser.add_argument("--pin", type=int, default=None, help="Optional PIN (0-9999)")
    parser.add_argument("--battery-uuid", help="Battery characteristic UUID override")
    parser.add_argument("--temp-uuid", help="Temperature characteristic UUID override")
    parser.add_argument("--name-uuid", help="Name characteristic UUID override")
    parser.add_argument("--pin-uuid", help="PIN characteristic UUID override")
    parser.add_argument(
        "--skip-battery", action="store_true", help="Skip battery read"
    )
    parser.add_argument("--pair", action="store_true", help="Attempt BLE pairing first")
    parser.add_argument("--debug", action="store_true", help="Print raw/decoded bytes")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    asyncio.run(
        _read_state(
            args.address,
            args.secret_key,
            args.pin,
            args.battery_uuid,
            args.temp_uuid,
            args.name_uuid,
            args.pin_uuid,
            args.skip_battery,
            args.pair,
            args.debug,
        )
    )


if __name__ == "__main__":
    main()
