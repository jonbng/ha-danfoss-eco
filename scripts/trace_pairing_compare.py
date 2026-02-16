#!/usr/bin/env python3
"""Compare libetrv and eco_tool pairing runs with Bluetooth traces.

This helper runs both tools in sequence, wrapping each run with:
- btmon capture (binary HCI snoop + text log)
- dbus-monitor capture for BlueZ method calls/signals/errors
- timestamped stdout/stderr logs for the tool process

Use this to identify where time is lost: connect, PIN write, or key read.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import TextIO


ROOT = Path(__file__).resolve().parents[2]
LIBETRV_DIR = ROOT / "libetrv"
ECO_SCRIPT_DIR = ROOT / "ha-danfoss-eco" / "scripts"


def ts() -> str:
    """Return a compact wall-clock timestamp with milliseconds."""
    now = datetime.now().astimezone()
    return now.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def write_line(fp: TextIO, line: str) -> None:
    fp.write(f"{ts()} {line}\n")
    fp.flush()


class StreamPump(threading.Thread):
    """Copy one process stream to file with timestamps."""

    def __init__(self, stream, out_fp: TextIO, prefix: str) -> None:
        super().__init__(daemon=True)
        self._stream = stream
        self._out_fp = out_fp
        self._prefix = prefix

    def run(self) -> None:
        for raw in iter(self._stream.readline, b""):
            line = raw.decode(errors="replace").rstrip("\n")
            write_line(self._out_fp, f"[{self._prefix}] {line}")


@dataclass
class MonitorSet:
    btmon_proc: subprocess.Popen[bytes]
    dbus_proc: subprocess.Popen[bytes]
    btmon_log_fp: TextIO
    dbus_log_fp: TextIO
    btmon_snoop_path: str


@dataclass
class RunResult:
    name: str
    start_ts: str
    end_ts: str
    duration_sec: float
    exit_code: int | None
    command: list[str]
    cwd: str
    output_log: str
    btmon_log: str
    btmon_snoop: str
    dbus_log: str
    bluepy_helper_pid: int | None = None
    bluepy_helper_info: str | None = None
    bluepy_strace_prefix: str | None = None


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _read_proc_cmdline(pid: int) -> str:
    data = _read_text(Path(f"/proc/{pid}/cmdline"))
    # cmdline is NUL separated
    return data.replace("\x00", " ").strip()


def _read_proc_status(pid: int) -> str:
    return _read_text(Path(f"/proc/{pid}/status"))


def _get_ppid(pid: int) -> int | None:
    status = _read_proc_status(pid)
    for line in status.splitlines():
        if line.startswith("PPid:"):
            try:
                return int(line.split(":", 1)[1].strip())
            except ValueError:
                return None
    return None


def _is_descendant(pid: int, ancestor_pid: int) -> bool:
    """Return True if pid is a descendant of ancestor_pid."""
    cur = pid
    for _ in range(64):  # hard limit to avoid loops
        if cur == ancestor_pid:
            return True
        ppid = _get_ppid(cur)
        if ppid is None or ppid <= 1:
            return False
        cur = ppid
    return False


def _find_bluepy_helper_pid(tool_pid: int) -> int | None:
    """Find bluepy-helper pid that belongs to tool_pid."""
    proc_dir = Path("/proc")
    for entry in proc_dir.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == tool_pid:
            continue
        cmd = _read_proc_cmdline(pid)
        if not cmd:
            continue
        if "bluepy-helper" not in cmd:
            continue
        # Prefer helpers spawned by the tool process
        if _is_descendant(pid, tool_pid):
            return pid
    return None


def _write_bluepy_helper_info(run_dir: Path, helper_pid: int) -> str:
    info_path = run_dir / "bluepy_helper_info.json"
    info = {
        "pid": helper_pid,
        "cmdline": _read_proc_cmdline(helper_pid),
        "status": _read_proc_status(helper_pid),
    }
    # Best-effort exe link
    try:
        info["exe"] = os.readlink(f"/proc/{helper_pid}/exe")
    except Exception:
        info["exe"] = None
    info_path.write_text(json.dumps(info, indent=2), encoding="utf-8")
    return str(info_path)


def _start_strace_attach(
    run_dir: Path, helper_pid: int, require_sudo: bool
) -> tuple[subprocess.Popen[bytes] | None, str | None]:
    prefix = run_dir / "strace-bluepy-helper"
    cmd = [
        "strace",
        "-ff",
        "-ttt",
        "-s",
        "256",
        "-o",
        str(prefix),
        "-p",
        str(helper_pid),
    ]
    if require_sudo:
        cmd = ["sudo", "-n", *cmd]
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
        return proc, str(prefix)
    except Exception:
        return None, None


def _trace_bluepy_helper(
    run_dir: Path,
    tool_pid: int,
    stop_event: threading.Event,
    require_sudo: bool,
    output_fp: TextIO,
    state: dict[str, object],
) -> None:
    """Background thread: wait for bluepy-helper and attach strace."""
    deadline = time.monotonic() + 20.0
    helper_pid: int | None = None
    while not stop_event.is_set() and time.monotonic() < deadline:
        helper_pid = _find_bluepy_helper_pid(tool_pid)
        if helper_pid is not None:
            break
        time.sleep(0.05)

    if helper_pid is None:
        write_line(output_fp, "[bluepy] bluepy-helper not found (no strace attached)")
        return

    write_line(output_fp, f"[bluepy] found bluepy-helper pid={helper_pid}")
    state["bluepy_helper_pid"] = helper_pid
    state["bluepy_helper_info"] = _write_bluepy_helper_info(run_dir, helper_pid)

    strace_proc, prefix = _start_strace_attach(run_dir, helper_pid, require_sudo=require_sudo)
    if strace_proc is None:
        write_line(output_fp, "[bluepy] failed to start strace attach")
        return

    state["bluepy_strace_prefix"] = prefix
    write_line(output_fp, f"[bluepy] strace attached (prefix={prefix})")

    # Wait until either stop requested or traced process exits.
    while not stop_event.is_set():
        if strace_proc.poll() is not None:
            break
        time.sleep(0.1)

    # Stop strace if still running.
    if strace_proc.poll() is None:
        try:
            strace_proc.send_signal(signal.SIGINT)
            strace_proc.wait(timeout=2)
        except Exception:
            try:
                strace_proc.kill()
            except Exception:
                pass


class SudoKeepAlive(threading.Thread):
    """Periodically refresh sudo timestamp while captures are running."""

    def __init__(self, interval_sec: int = 30) -> None:
        super().__init__(daemon=True)
        self._interval_sec = interval_sec
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.wait(self._interval_sec):
            subprocess.run(
                ["sudo", "-n", "true"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )


def ensure_sudo_ready() -> None:
    """Prompt once for sudo credentials for background monitor commands."""
    print()
    print("sudo is required for btmon/dbus-monitor capture.")
    print("You may be prompted for your password now.")
    rc = subprocess.run(["sudo", "-v"], check=False).returncode
    if rc != 0:
        raise RuntimeError("sudo authentication failed; cannot start privileged monitors")
    # Validate non-interactive sudo works before spawning background monitors.
    rc = subprocess.run(["sudo", "-n", "true"], check=False).returncode
    if rc != 0:
        raise RuntimeError(
            "sudo timestamp could not be reused non-interactively. "
            "Check sudoers tty settings or run without --sudo-monitors."
        )


def start_monitors(run_dir: Path, sudo: bool) -> MonitorSet:
    """Start btmon and dbus-monitor for one run."""
    btmon_snoop = run_dir / "hci.snoop"
    btmon_text = run_dir / "btmon.log"
    dbus_text = run_dir / "dbus.log"

    btmon_log_fp = btmon_text.open("w", encoding="utf-8")
    dbus_log_fp = dbus_text.open("w", encoding="utf-8")

    btmon_cmd = ["btmon", "-w", str(btmon_snoop)]
    if sudo:
        btmon_cmd = ["sudo", btmon_cmd[0], *btmon_cmd[1:]]

    # Capture both calls into BlueZ and BlueZ signals/errors.
    dbus_cmd = [
        "dbus-monitor",
        "--system",
        "type='method_call',destination='org.bluez'",
        "type='signal',sender='org.bluez'",
        "type='error',sender='org.bluez'",
    ]
    if sudo:
        dbus_cmd = ["sudo", dbus_cmd[0], *dbus_cmd[1:]]

    write_line(btmon_log_fp, f"[monitor] starting: {' '.join(btmon_cmd)}")
    write_line(dbus_log_fp, f"[monitor] starting: {' '.join(dbus_cmd)}")

    btmon_proc = subprocess.Popen(
        btmon_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=None,
    )
    dbus_proc = subprocess.Popen(
        dbus_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=None,
    )

    StreamPump(btmon_proc.stdout, btmon_log_fp, "btmon").start()
    StreamPump(dbus_proc.stdout, dbus_log_fp, "dbus").start()

    return MonitorSet(
        btmon_proc=btmon_proc,
        dbus_proc=dbus_proc,
        btmon_log_fp=btmon_log_fp,
        dbus_log_fp=dbus_log_fp,
        btmon_snoop_path=str(btmon_snoop),
    )


def stop_proc(proc: subprocess.Popen[bytes], name: str, log_fp: TextIO) -> None:
    """Try graceful stop, then hard kill."""
    if proc.poll() is not None:
        write_line(log_fp, f"[monitor] {name} exited with {proc.returncode}")
        return
    try:
        proc.send_signal(signal.SIGINT)
        write_line(log_fp, f"[monitor] sent SIGINT to {name} pid={proc.pid}")
        proc.wait(timeout=3)
        write_line(log_fp, f"[monitor] {name} stopped with {proc.returncode}")
    except Exception as exc:
        write_line(log_fp, f"[monitor] graceful stop failed for {name}: {exc}")
        try:
            proc.kill()
            proc.wait(timeout=2)
            write_line(log_fp, f"[monitor] killed {name} pid={proc.pid}")
        except Exception as kill_exc:
            write_line(log_fp, f"[monitor] hard kill failed for {name}: {kill_exc}")


def run_tool_with_traces(
    name: str,
    command: list[str],
    cwd: Path,
    run_dir: Path,
    use_sudo_monitors: bool,
    *,
    strace_bluepy_helper: bool = True,
) -> RunResult:
    run_dir.mkdir(parents=True, exist_ok=True)
    output_log = run_dir / "tool.log"
    output_fp = output_log.open("w", encoding="utf-8")

    write_line(output_fp, f"[meta] run={name}")
    write_line(output_fp, f"[meta] cwd={cwd}")
    write_line(output_fp, f"[meta] cmd={' '.join(command)}")

    monitors = start_monitors(run_dir, sudo=use_sudo_monitors)

    # Let monitors initialize before running tool.
    time.sleep(0.3)

    start_wall = ts()
    start_mono = time.monotonic()
    write_line(output_fp, "[meta] tool starting")

    proc = subprocess.Popen(
        command,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=None,  # inherit terminal; tools can prompt for Enter
        preexec_fn=os.setsid,
    )
    out_pump = StreamPump(proc.stdout, output_fp, "stdout")
    err_pump = StreamPump(proc.stderr, output_fp, "stderr")
    out_pump.start()
    err_pump.start()

    bluepy_state: dict[str, object] = {}
    bluepy_stop = threading.Event()
    bluepy_thread: threading.Thread | None = None
    if name == "libetrv" and strace_bluepy_helper:
        # Attach strace to bluepy-helper (best effort). Requires sudo to ptrace.
        bluepy_thread = threading.Thread(
            target=_trace_bluepy_helper,
            args=(run_dir, proc.pid, bluepy_stop, use_sudo_monitors, output_fp, bluepy_state),
            daemon=True,
        )
        bluepy_thread.start()

    try:
        exit_code = proc.wait()
    except KeyboardInterrupt:
        write_line(output_fp, "[meta] interrupted by user, stopping tool")
        try:
            os.killpg(proc.pid, signal.SIGINT)
            exit_code = proc.wait(timeout=3)
        except Exception:
            os.killpg(proc.pid, signal.SIGKILL)
            exit_code = proc.wait(timeout=2)
    finally:
        if bluepy_thread is not None:
            bluepy_stop.set()
            bluepy_thread.join(timeout=3)

    end_mono = time.monotonic()
    end_wall = ts()
    write_line(output_fp, f"[meta] tool exited with code={exit_code}")
    write_line(output_fp, f"[meta] duration_sec={end_mono - start_mono:.3f}")

    # Stop monitors after tool exits to include trailing Bluetooth events.
    time.sleep(0.5)
    stop_proc(monitors.btmon_proc, "btmon", monitors.btmon_log_fp)
    stop_proc(monitors.dbus_proc, "dbus-monitor", monitors.dbus_log_fp)

    monitors.btmon_log_fp.close()
    monitors.dbus_log_fp.close()
    output_fp.close()

    return RunResult(
        name=name,
        start_ts=start_wall,
        end_ts=end_wall,
        duration_sec=round(end_mono - start_mono, 3),
        exit_code=exit_code,
        command=command,
        cwd=str(cwd),
        output_log=str(output_log),
        btmon_log=str(run_dir / "btmon.log"),
        btmon_snoop=monitors.btmon_snoop_path,
        dbus_log=str(run_dir / "dbus.log"),
        bluepy_helper_pid=bluepy_state.get("bluepy_helper_pid") if bluepy_state else None,
        bluepy_helper_info=bluepy_state.get("bluepy_helper_info") if bluepy_state else None,
        bluepy_strace_prefix=bluepy_state.get("bluepy_strace_prefix") if bluepy_state else None,
    )


def prompt_for_button(run_name: str) -> None:
    print()
    print(f"=== {run_name} ===")
    print("Press the thermostat timer button now to enter pairing mode.")
    input("When ready, press Enter to start capture + command...")


def default_run_root(base: Path | None) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    parent = base or (ROOT / "ha-danfoss-eco" / "scripts" / "trace_runs")
    return parent / f"pairing-compare-{stamp}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run libetrv + eco_tool pairing with btmon/dbus captures and "
            "timestamped logs for side-by-side analysis."
        )
    )
    parser.add_argument("address", help="BLE MAC address (example: 00:04:2F:63:33:CE)")
    parser.add_argument("--pin", type=int, default=0, help="PIN code (default: 0)")
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="Python executable to run child tools (default: current interpreter)",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Parent directory for trace output (default: scripts/trace_runs)",
    )
    parser.add_argument(
        "--sudo-monitors",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Run btmon/dbus-monitor via sudo (default: true)",
    )
    parser.add_argument(
        "--skip-libetrv",
        action="store_true",
        help="Skip libetrv run",
    )
    parser.add_argument(
        "--skip-eco",
        action="store_true",
        help="Skip eco_tool run",
    )
    parser.add_argument(
        "--strace-bluepy-helper",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Attach strace to bluepy-helper during libetrv run (default: true)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    sudo_keepalive: SudoKeepAlive | None = None
    if args.sudo_monitors:
        ensure_sudo_ready()
        sudo_keepalive = SudoKeepAlive(interval_sec=30)
        sudo_keepalive.start()

    run_root = default_run_root(Path(args.output_dir) if args.output_dir else None)
    run_root.mkdir(parents=True, exist_ok=True)

    print(f"Output directory: {run_root}")
    print(
        "Note: this script runs tool subprocesses (not direct imports) so behavior "
        "matches your real CLI usage."
    )

    results: list[RunResult] = []

    try:
        if not args.skip_libetrv:
            prompt_for_button("libetrv retrieve_key")
            libetrv_cmd = [
                args.python,
                "-m",
                "libetrv.cli",
                "device",
                args.address,
                "retrieve_key",
                "--wait_seconds=0",
            ]
            results.append(
                run_tool_with_traces(
                    name="libetrv",
                    command=libetrv_cmd,
                    cwd=LIBETRV_DIR,
                    run_dir=run_root / "01-libetrv",
                    use_sudo_monitors=args.sudo_monitors,
                    strace_bluepy_helper=args.strace_bluepy_helper,
                )
            )

        if not args.skip_eco:
            prompt_for_button("eco_tool get-key")
            eco_cmd = [
                args.python,
                "eco_tool.py",
                "get-key",
                args.address,
                "--pin",
                str(args.pin),
                "--no-wait-for-enter",
            ]
            results.append(
                run_tool_with_traces(
                    name="eco_tool",
                    command=eco_cmd,
                    cwd=ECO_SCRIPT_DIR,
                    run_dir=run_root / "02-eco_tool",
                    use_sudo_monitors=args.sudo_monitors,
                    strace_bluepy_helper=False,
                )
            )
    finally:
        if sudo_keepalive is not None:
            sudo_keepalive.stop()

    summary_path = run_root / "summary.json"
    summary = {
        "created_at": ts(),
        "address": args.address,
        "pin": args.pin,
        "python": args.python,
        "sudo_monitors": args.sudo_monitors,
        "results": [asdict(r) for r in results],
    }
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print()
    print("Done. Summary:")
    for result in results:
        status = "ok" if result.exit_code == 0 else f"exit={result.exit_code}"
        print(f"- {result.name}: {result.duration_sec:.2f}s ({status})")
    print(f"- summary: {summary_path}")


if __name__ == "__main__":
    main()
