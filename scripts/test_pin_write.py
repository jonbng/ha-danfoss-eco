#!/usr/bin/env python3
"""Try PIN writes against candidate characteristics and test reads."""

from __future__ import annotations

import argparse
import asyncio

from bleak import BleakClient
from bleak.exc import BleakDBusError

PIN_CANDIDATES = [
    "10020001-2749-0001-0000-00805f9b042f",
    "10010001-2749-0001-0000-00805f9b042f",
    "10020007-2749-0001-0000-00805f9b042f",
    "1002000a-2749-0001-0000-00805f9b042f",
]

TEMP_UUID = "10020002-2749-0001-0000-00805f9b042f"
NAME_UUID = "10020003-2749-0001-0000-00805f9b042f"
BATTERY_UUID = "00002a19-0000-1000-8000-00805f9b34fb"


async def _try_pin(address: str, pin: int) -> None:
    pin_bytes = int(pin).to_bytes(4, byteorder="big", signed=False)
    async with BleakClient(address) as client:
        for candidate in PIN_CANDIDATES:
            try:
                await client.write_gatt_char(candidate, pin_bytes, response=True)
                print(f"Wrote PIN to {candidate}")
            except Exception as exc:
                print(f"Write failed for {candidate}: {exc}")
                continue

            # Try reading temp/name/battery to see if authorization works.
            try:
                await client.read_gatt_char(TEMP_UUID)
                await client.read_gatt_char(NAME_UUID)
                await client.read_gatt_char(BATTERY_UUID)
                print(f"Reads succeeded after writing {candidate}")
                return
            except BleakDBusError as exc:
                print(f"Reads failed after {candidate}: {exc}")
            except Exception as exc:
                print(f"Reads failed after {candidate}: {exc}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Test PIN write characteristic.")
    parser.add_argument("address", help="BLE address (e.g. 00:04:2f:63:33:ce)")
    parser.add_argument("--pin", type=int, default=0, help="PIN (0-9999)")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    asyncio.run(_try_pin(args.address, args.pin))


if __name__ == "__main__":
    main()
