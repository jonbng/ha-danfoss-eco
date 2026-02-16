#!/usr/bin/env python3
"""Summarize btmon/dbus traces from trace_pairing_compare.py runs.

Usage:
  python3 analyze_trace_run.py /path/to/pairing-compare-YYYYMMDD-HHMMSS

Outputs a short, human-readable summary of:
- BlueZ DBus connect attempts and how long until errors
- btmon MGMT connect failures
- whether ATT write/read happened (PIN write + key read)
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


TS_RE = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})")


def parse_ts(line: str) -> datetime | None:
    m = TS_RE.match(line)
    if not m:
        return None
    return datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S.%f")


@dataclass
class DbusConnectAttempt:
    connect_ts: datetime
    error_ts: datetime | None
    error_name: str | None


@dataclass
class TraceSummary:
    label: str
    dbus_connect_attempts: list[DbusConnectAttempt]
    dbus_start_discovery_count: int
    dbus_stop_discovery_count: int
    dbus_services_resolved_signal_count: int
    btmon_connect_failed_count: int
    btmon_att_write_req_count: int
    btmon_att_read_req_count: int


def read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def summarize_dbus(lines: list[str]) -> list[DbusConnectAttempt]:
    attempts: list[DbusConnectAttempt] = []
    pending_idx: int | None = None

    for line in lines:
        # Match connect attempts regardless of minor formatting differences.
        if "interface=org.bluez.Device1" in line and "member=Connect" in line:
            ts = parse_ts(line)
            if ts is None:
                continue
            attempts.append(DbusConnectAttempt(connect_ts=ts, error_ts=None, error_name=None))
            pending_idx = len(attempts) - 1
            continue

        if "error_name=org.bluez.Error." in line and pending_idx is not None:
            ts = parse_ts(line)
            if ts is None:
                continue
            m = re.search(r"error_name=(org\\.bluez\\.Error\\.[^\\s]+)", line)
            err_name = m.group(1) if m else "org.bluez.Error"
            attempts[pending_idx].error_ts = ts
            attempts[pending_idx].error_name = err_name
            pending_idx = None

    return attempts


def count_dbus_markers(lines: list[str]) -> tuple[int, int, int]:
    start = sum("interface=org.bluez.Adapter1; member=StartDiscovery" in l for l in lines)
    stop = sum("interface=org.bluez.Adapter1; member=StopDiscovery" in l for l in lines)
    services_resolved = sum("string \"ServicesResolved\"" in l for l in lines)
    return start, stop, services_resolved


def summarize_btmon(lines: list[str]) -> tuple[int, int, int]:
    connect_failed = sum("MGMT Event: Connect Failed" in l for l in lines)
    att_write_req = sum("ATT: Write Request" in l for l in lines)
    att_read_req = sum("ATT: Read Request" in l for l in lines)
    return connect_failed, att_write_req, att_read_req


def summarize_trace_folder(folder: Path, label: str) -> TraceSummary:
    dbus_lines = read_lines(folder / "dbus.log")
    btmon_lines = read_lines(folder / "btmon.log")
    start_disc, stop_disc, services_resolved = count_dbus_markers(dbus_lines)
    return TraceSummary(
        label=label,
        dbus_connect_attempts=summarize_dbus(dbus_lines),
        dbus_start_discovery_count=start_disc,
        dbus_stop_discovery_count=stop_disc,
        dbus_services_resolved_signal_count=services_resolved,
        btmon_connect_failed_count=summarize_btmon(btmon_lines)[0],
        btmon_att_write_req_count=summarize_btmon(btmon_lines)[1],
        btmon_att_read_req_count=summarize_btmon(btmon_lines)[2],
    )


def print_summary(summary: TraceSummary) -> None:
    print(f"\n== {summary.label} ==")
    print(f"dbus Device1.Connect attempts: {len(summary.dbus_connect_attempts)}")
    if summary.dbus_start_discovery_count or summary.dbus_stop_discovery_count:
        print(
            "dbus discovery churn: "
            f"StartDiscovery={summary.dbus_start_discovery_count}, "
            f"StopDiscovery={summary.dbus_stop_discovery_count}, "
            f"ServicesResolved(signals)={summary.dbus_services_resolved_signal_count}"
        )
    for idx, a in enumerate(summary.dbus_connect_attempts, start=1):
        if a.error_ts is None:
            print(f"  {idx}. {a.connect_ts} -> (no error captured)")
            continue
        dur = (a.error_ts - a.connect_ts).total_seconds()
        print(
            f"  {idx}. {a.connect_ts.time()} -> {a.error_ts.time()} "
            f"({dur:.3f}s) {a.error_name}"
        )

    print(f"btmon MGMT Connect Failed: {summary.btmon_connect_failed_count}")
    print(f"btmon ATT Write Request:   {summary.btmon_att_write_req_count}")
    print(f"btmon ATT Read Request:    {summary.btmon_att_read_req_count}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Summarize BLE trace run logs.")
    p.add_argument("run_dir", help="pairing-compare-* folder path")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    run_dir = Path(args.run_dir).expanduser().resolve()
    if not run_dir.exists():
        raise SystemExit(f"Run dir not found: {run_dir}")

    lib = summarize_trace_folder(run_dir / "01-libetrv", "libetrv")
    eco = summarize_trace_folder(run_dir / "02-eco_tool", "eco_tool")
    print_summary(lib)
    print_summary(eco)

    # Quick interpretation hint.
    lib_att = lib.btmon_att_write_req_count > 0 and lib.btmon_att_read_req_count > 0
    eco_att = eco.btmon_att_write_req_count > 0 and eco.btmon_att_read_req_count > 0
    print("\n== Interpretation ==")
    if lib_att and not eco_att:
        print("- libetrv reached ATT (PIN write + key read); eco_tool did not reach ATT.")
    elif lib_att and eco_att:
        print("- Both reached ATT; compare timing in tool.log for latency differences.")
    else:
        print("- Neither reached ATT; investigate adapter state / pairing mode timing.")


if __name__ == "__main__":
    main()

