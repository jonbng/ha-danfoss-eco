#!/usr/bin/env python3
"""Scan for nearby Danfoss Eco thermostats."""

from __future__ import annotations

import argparse
import asyncio
from typing import Iterable

from bleak import BleakScanner


def _is_etrv(name: str | None) -> bool:
    if not name:
        return False
    return name.endswith(";eTRV")


async def _scan(timeout: float, show_all: bool) -> None:
    devices = await BleakScanner.discover(timeout=timeout)
    for device in devices:
        name = device.name or getattr(device, "local_name", None)
        if show_all or _is_etrv(name):
            print(f"{device.address} | {name}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scan for Danfoss Eco BLE devices.")
    parser.add_argument("--timeout", type=float, default=8.0, help="Scan duration in seconds.")
    parser.add_argument(
        "--all", action="store_true", help="Show all BLE devices (not just eTRV)."
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    asyncio.run(_scan(args.timeout, args.all))


if __name__ == "__main__":
    main()
