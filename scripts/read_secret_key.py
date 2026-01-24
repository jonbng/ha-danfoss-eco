#!/usr/bin/env python3
"""Read the secret key from a thermostat in pairing mode."""

from __future__ import annotations

import argparse
import asyncio
import binascii

from bleak import BleakClient

HANDLER_SECRET_KEY = 0x3F
CUSTOM_SERVICE_PREFIX = "10020000-2749-0001-0000-00805f9b042f"
DEFAULT_SECRET_UUID = "1002000b-2749-0001-0000-00805f9b042f"


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


async def _read_key(address: str, uuid: str | None, pair: bool) -> None:
    async with BleakClient(address) as client:
        if pair and hasattr(client, "pair"):
            await client.pair()
        if uuid is None:
            uuid = DEFAULT_SECRET_UUID
        if uuid:
            data = await client.read_gatt_char(uuid)
            print(bytes(data)[:16].hex())
            return

        # Attempt to discover by reading all readable characteristics in custom service
        if hasattr(client, "get_services"):
            services = await client.get_services()
        else:
            services = client.services

        candidates: list[tuple[str, bytes]] = []
        for service in services:
            if str(service.uuid).lower() != CUSTOM_SERVICE_PREFIX:
                continue
            for char in service.characteristics:
                if "read" not in char.properties:
                    continue
                try:
                    data = await client.read_gatt_char(char)
                except Exception:
                    continue
                payload = bytes(data)
                if len(payload) == 16:
                    candidates.append((str(char.uuid), payload))

        if not candidates:
            raise RuntimeError(
                "No 16-byte readable characteristics found in custom service."
            )

        if len(candidates) > 1:
            print("Multiple 16-byte candidates found. Re-run with --uuid:")
            for uuid, payload in candidates:
                print(f"  {uuid}  {binascii.hexlify(payload).decode()}")
            return

        print(candidates[0][1].hex())


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read Danfoss Eco secret key.")
    parser.add_argument("address", help="BLE address (e.g. 00:04:2f:63:33:ce)")
    parser.add_argument("--uuid", help="Characteristic UUID to read (optional)")
    parser.add_argument("--pair", action="store_true", help="Attempt BLE pairing first")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    asyncio.run(_read_key(args.address, args.uuid, args.pair))


if __name__ == "__main__":
    main()
