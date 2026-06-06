#!/usr/bin/env python3
"""Offline sweep for stale ccstack runtime artifacts.

Scans a workspace state directory and:

1. Deletes any ``pids/*.pid`` file whose recorded pid is no longer running.
2. Rewrites any ``doctor-runs/*.json`` whose ``doctor_state`` is
   ``spawned`` or ``running`` but whose recorded ``pid`` is dead,
   flipping it to ``host_failed`` (or ``cancelled`` if the file
   already records a ``cancelled_at`` timestamp) so the UI no longer
   shows it as an in-flight run.

Idempotent: running it multiple times converges to a clean state.

Usage::

    python3 scripts/cancel-stale.py ~/.ccstack/state/<workspace-name>
    python3 scripts/cancel-stale.py ~/.ccstack/state/<workspace-name> --dry-run
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


def pid_is_running(pid: Any) -> bool:
    """Return True iff ``pid`` is a positive integer pointing at a live process.

    Matches ``ccstack.pid_is_running`` so the sweep agrees with the runtime.
    """
    try:
        numeric_pid = int(pid)
    except (TypeError, ValueError):
        return False
    if numeric_pid <= 0:
        return False
    try:
        os.kill(numeric_pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # process exists, we just cannot signal it
        return True
    return True


def read_json_file(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def write_json_file(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def sweep_pid_dir(pid_dir: Path, dry_run: bool, verbose: bool) -> List[str]:
    deleted: List[str] = []
    if not pid_dir.is_dir():
        return deleted
    for entry in sorted(pid_dir.glob("*.pid")):
        try:
            raw = entry.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        try:
            pid_value: Optional[int] = int(raw) if raw else None
        except ValueError:
            pid_value = None
        if pid_value is None:
            if verbose:
                print(f"[stale-pid] empty/invalid file: {entry}")
            if not dry_run:
                entry.unlink(missing_ok=True)
            deleted.append(str(entry))
            continue
        if not pid_is_running(pid_value):
            if verbose:
                print(f"[stale-pid] dead pid {pid_value}: {entry}")
            if not dry_run:
                entry.unlink(missing_ok=True)
            deleted.append(str(entry))
        elif verbose:
            print(f"[stale-pid] alive pid {pid_value}: {entry}")
    return deleted


def sweep_doctor_runs(doctor_dir: Path, dry_run: bool, verbose: bool) -> List[str]:
    rewritten: List[str] = []
    if not doctor_dir.is_dir():
        return rewritten
    for entry in sorted(doctor_dir.glob("*.json")):
        state = read_json_file(entry)
        if not state:
            continue
        current = str(state.get("doctor_state") or "")
        if current not in {"spawned", "running"}:
            if verbose:
                print(f"[doctor-run] terminal ({current}): {entry}")
            continue
        pid = state.get("pid")
        host_pid = state.get("host_pid")
        if pid_is_running(pid) or (host_pid not in (None, "") and pid_is_running(host_pid)):
            if verbose:
                print(f"[doctor-run] alive pid={pid} host_pid={host_pid}: {entry}")
            continue
        # Orphan — rewrite as host_failed (or honor an in-progress cancel).
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        if state.get("cancelled_at"):
            state["doctor_state"] = "cancelled"
            state["status"] = "doctor cancelled"
            state.setdefault("returncode", -15)
            state["doctor_outcome"] = "cancelled"
        else:
            state["doctor_state"] = "host_failed"
            state["status"] = "doctor host failed"
            state.setdefault(
                "error",
                "ccstack runner exited without recording completion (offline sweep)",
            )
            if state.get("returncode") is None:
                state["returncode"] = -1
            state["doctor_outcome"] = "host_failed"
        state.setdefault("swept_at", now_iso)
        if verbose:
            print(f"[doctor-run] orphan, rewriting -> {state['doctor_state']}: {entry}")
        if not dry_run:
            write_json_file(entry, state)
        rewritten.append(str(entry))
    return rewritten


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Sweep stale ccstack pid files and orphaned doctor-run state. "
            "Pass the workspace state dir (e.g. ~/.ccstack/state/<name>)."
        )
    )
    parser.add_argument(
        "state_dir",
        type=Path,
        help="Workspace state directory (contains pids/ and doctor-runs/).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing.",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print one line per scanned file.",
    )
    args = parser.parse_args(argv)

    state_dir: Path = args.state_dir.expanduser().resolve()
    if not state_dir.is_dir():
        print(f"error: not a directory: {state_dir}", file=sys.stderr)
        return 2

    deleted_pids = sweep_pid_dir(state_dir / "pids", args.dry_run, args.verbose)
    rewritten_runs = sweep_doctor_runs(
        state_dir / "doctor-runs", args.dry_run, args.verbose
    )

    suffix = " (dry-run)" if args.dry_run else ""
    print(
        f"swept {len(deleted_pids)} stale pid file(s) and "
        f"{len(rewritten_runs)} orphan doctor run(s) under {state_dir}{suffix}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
