#!/usr/bin/env python3
"""Probe readable characteristics in the custom eTRV service."""

from __future__ import annotations

import argparse
import asyncio
import binascii
import string

from bleak import BleakClient

CUSTOM_SERVICE_UUID = "10020000-2749-0001-0000-00805f9b042f"


def _printable(data: bytes) -> str:
    text = "".join(chr(b) if chr(b) in string.printable else "." for b in data)
    return text


async def _probe(address: str) -> None:
    async with BleakClient(address) as client:
        if hasattr(client, "get_services"):
            services = await client.get_services()
        else:
            services = client.services

        for service in services:
            if str(service.uuid).lower() != CUSTOM_SERVICE_UUID:
                continue
            print(f"Service {service.uuid}")
            for char in service.characteristics:
                if "read" not in char.properties:
                    continue
                try:
                    data = await client.read_gatt_char(char)
                except Exception as exc:
                    print(f"  {char.uuid} handle={getattr(char, 'handle', None)} read_error={exc}")
                    continue
                payload = bytes(data)
                hexval = binascii.hexlify(payload).decode()
                printable = _printable(payload)
                print(
                    f"  {char.uuid} handle={getattr(char, 'handle', None)} "
                    f"len={len(payload)} hex={hexval} ascii={printable}"
                )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Probe readable characteristics for eTRV custom service."
    )
    parser.add_argument("address", help="BLE address (e.g. 00:04:2f:63:33:ce)")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    asyncio.run(_probe(args.address))


if __name__ == "__main__":
    main()
