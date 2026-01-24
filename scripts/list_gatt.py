#!/usr/bin/env python3
"""List GATT services/characteristics with handles."""

from __future__ import annotations

import argparse
import asyncio

from bleak import BleakClient


async def _list(address: str) -> None:
    async with BleakClient(address) as client:
        if hasattr(client, "get_services"):
            services = await client.get_services()
        else:
            services = client.services
        for service in services:
            print(f"Service {service.uuid}")
            for char in service.characteristics:
                handle = getattr(char, "handle", None)
                props = ",".join(char.properties)
                print(f"  Char {char.uuid} handle={handle} props={props}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="List BLE GATT characteristics.")
    parser.add_argument("address", help="BLE address (e.g. 00:04:2f:63:33:ce)")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    asyncio.run(_list(args.address))


if __name__ == "__main__":
    main()
