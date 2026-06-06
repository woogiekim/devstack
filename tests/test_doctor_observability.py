"""Offline tests for Bug A (cancel), Bug B (heartbeat), Bug C (sweep).

These tests do NOT require ``CCSTACK_SMOKE_ENABLED=1`` and operate against
temporary directories only. They lock down the doctor observability contract
introduced for the "stuck doctor" UX fix.
"""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Pick the renamed devstack file when present (post git mv);
# fall back to ccstack for any in-progress checkout.
DEVSTACK_PATH = ROOT / "devstack" if (ROOT / "devstack").exists() else ROOT / "ccstack"
CCSTACK_PATH = DEVSTACK_PATH  # legacy alias for older tests
SWEEP_SCRIPT = ROOT / "scripts" / "cancel-stale.py"


def load_ccstack():
    """Load the single-file ccstack script as a module."""
    loader = SourceFileLoader("ccstack_module_doctor_obs", str(CCSTACK_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


ccstack = load_ccstack()


class StatusLabelClassTests(unittest.TestCase):
    """Bug A — cancelled state must have a label, class, and friendly UI copy."""

    def test_cancelled_label(self):
        self.assertEqual(ccstack.doctor_status_label("cancelled"), "doctor cancelled")

    def test_cancelled_class_is_warn(self):
        # warn is intentionally distinct from "fail" — cancellation is
        # user-initiated, not a host failure.
        self.assertEqual(ccstack.doctor_status_class("cancelled"), "warn")

    def test_existing_states_unchanged(self):
        self.assertEqual(ccstack.doctor_status_label("running"), "doctor running")
        self.assertEqual(ccstack.doctor_status_label("completed"), "doctor completed")
        self.assertEqual(ccstack.doctor_status_class("completed"), "ok")
        self.assertEqual(ccstack.doctor_status_class("host_failed"), "fail")
        self.assertEqual(ccstack.doctor_status_class("running"), "running")


class CancelDoctorRunTests(unittest.TestCase):
    """Bug A — cancel_doctor_run must rewrite state and stop a live runner."""

    def _make_workspace(self, root: Path) -> object:
        state_dir = root / "state"
        (state_dir / "doctor-runs").mkdir(parents=True, exist_ok=True)

        class _Workspace:
            pass

        ws = _Workspace()
        ws.state_dir = state_dir
        return ws

    def _write_state(self, ws, run_id: str, state: dict) -> Path:
        path = ccstack.doctor_state_path(ws, run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state) + "\n", encoding="utf-8")
        return path

    def test_cancel_unknown_run_returns_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = self._make_workspace(Path(tmp))
            self.assertFalse(ccstack.cancel_doctor_run(ws, "nonexistent"))

    def test_cancel_terminal_run_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = self._make_workspace(Path(tmp))
            run_id = "doctor_test_done"
            self._write_state(ws, run_id, {"doctor_state": "completed", "returncode": 0})
            self.assertFalse(ccstack.cancel_doctor_run(ws, run_id))
            state = json.loads(ccstack.doctor_state_path(ws, run_id).read_text())
            self.assertEqual(state["doctor_state"], "completed")

    def test_cancel_running_with_live_pid_signals_and_rewrites_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = self._make_workspace(Path(tmp))
            # Spawn a child shell that traps SIGTERM and exits 0.
            child = subprocess.Popen(
                [sys.executable, "-c", "import time, signal; signal.signal(signal.SIGTERM, lambda *a: (_ for _ in ()).throw(SystemExit(0))); time.sleep(30)"],
                start_new_session=True,
            )
            try:
                run_id = "doctor_test_live"
                self._write_state(
                    ws,
                    run_id,
                    {
                        "doctor_state": "running",
                        "pid": child.pid,
                        "host_pid": child.pid,
                        "returncode": None,
                    },
                )
                result = ccstack.cancel_doctor_run(ws, run_id)
                self.assertTrue(result)
                # The child should be reaped within a couple of seconds.
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:  # pragma: no cover
                    self.fail("cancel_doctor_run did not terminate the child")
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=2)
            state = json.loads(ccstack.doctor_state_path(ws, run_id).read_text())
            self.assertEqual(state["doctor_state"], "cancelled")
            self.assertEqual(state["status"], "doctor cancelled")
            self.assertEqual(state["returncode"], -15)
            self.assertIn("cancelled_at", state)
            self.assertEqual(state["doctor_outcome"], "cancelled")

    def test_cancel_running_with_dead_pid_still_flips_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = self._make_workspace(Path(tmp))
            run_id = "doctor_test_dead"
            # 999999 is essentially never live on macOS/Linux.
            self._write_state(
                ws,
                run_id,
                {
                    "doctor_state": "running",
                    "pid": 999999,
                    "returncode": None,
                },
            )
            result = ccstack.cancel_doctor_run(ws, run_id)
            self.assertFalse(result)  # no signal sent
            state = json.loads(ccstack.doctor_state_path(ws, run_id).read_text())
            self.assertEqual(state["doctor_state"], "cancelled")
            self.assertEqual(state["returncode"], -15)

    def test_force_cancel_uses_sigkill_returncode(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = self._make_workspace(Path(tmp))
            run_id = "doctor_test_force"
            self._write_state(
                ws,
                run_id,
                {"doctor_state": "running", "pid": 999999, "returncode": None},
            )
            ccstack.cancel_doctor_run(ws, run_id, force=True)
            state = json.loads(ccstack.doctor_state_path(ws, run_id).read_text())
            self.assertEqual(state["returncode"], -9)


class HeartbeatLoopTests(unittest.TestCase):
    """Bug B — heartbeat thread must stamp state while running, stop on signal."""

    def test_heartbeat_writes_runtime_and_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state_path.write_text(
                json.dumps({"doctor_state": "running"}) + "\n", encoding="utf-8"
            )
            stop_event = threading.Event()
            thread = threading.Thread(
                target=ccstack._doctor_heartbeat_loop,
                args=(state_path, time.time(), stop_event),
                kwargs={"interval": 0.2},
                daemon=True,
            )
            thread.start()
            time.sleep(0.6)  # let at least 2 heartbeats land
            stop_event.set()
            thread.join(timeout=2)
            state = json.loads(state_path.read_text())
            self.assertIn("last_heartbeat_at", state)
            self.assertIn("runtime_seconds", state)
            self.assertGreaterEqual(state["runtime_seconds"], 0)

    def test_heartbeat_stops_when_state_is_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state_path.write_text(
                json.dumps({"doctor_state": "completed", "runtime_seconds": 42})
                + "\n",
                encoding="utf-8",
            )
            stop_event = threading.Event()
            thread = threading.Thread(
                target=ccstack._doctor_heartbeat_loop,
                args=(state_path, time.time(), stop_event),
                kwargs={"interval": 0.2},
                daemon=True,
            )
            thread.start()
            time.sleep(0.6)
            stop_event.set()
            thread.join(timeout=2)
            state = json.loads(state_path.read_text())
            # Terminal state must not be overwritten by the heartbeat loop.
            self.assertEqual(state["doctor_state"], "completed")
            self.assertEqual(state["runtime_seconds"], 42)


class SweepScriptTests(unittest.TestCase):
    """Bug C — cancel-stale.py removes dead .pid files and rewrites orphans."""

    def _run_sweep(self, state_dir: Path, *flags: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SWEEP_SCRIPT), str(state_dir), *flags],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_sweep_deletes_dead_pid_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            pid_dir = state_dir / "pids"
            pid_dir.mkdir()
            dead_file = pid_dir / "apps_x.pid"
            dead_file.write_text("999999\n")
            # Live pid: this process itself.
            live_file = pid_dir / "apps_y.pid"
            live_file.write_text(f"{os.getpid()}\n")

            result = self._run_sweep(state_dir, "--verbose")
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertFalse(dead_file.exists(), "dead pid file should be removed")
            self.assertTrue(live_file.exists(), "live pid file should be preserved")

    def test_sweep_dry_run_makes_no_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            pid_dir = state_dir / "pids"
            pid_dir.mkdir()
            dead_file = pid_dir / "apps_x.pid"
            dead_file.write_text("999999\n")

            result = self._run_sweep(state_dir, "--dry-run")
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertTrue(dead_file.exists(), "dry-run must NOT delete files")

    def test_sweep_rewrites_orphan_doctor_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            doctor_dir = state_dir / "doctor-runs"
            doctor_dir.mkdir()
            orphan = doctor_dir / "doctor_test_orphan.json"
            orphan.write_text(
                json.dumps(
                    {
                        "doctor_state": "running",
                        "pid": 999999,
                        "returncode": None,
                    }
                )
                + "\n"
            )
            live = doctor_dir / "doctor_test_live.json"
            live.write_text(
                json.dumps(
                    {
                        "doctor_state": "running",
                        "pid": os.getpid(),
                        "returncode": None,
                    }
                )
                + "\n"
            )

            result = self._run_sweep(state_dir)
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            orphan_state = json.loads(orphan.read_text())
            self.assertEqual(orphan_state["doctor_state"], "host_failed")
            self.assertIn("error", orphan_state)
            live_state = json.loads(live.read_text())
            self.assertEqual(
                live_state["doctor_state"], "running", "live doctor run must not be swept"
            )

    def test_sweep_missing_state_dir_errors(self):
        result = self._run_sweep(Path("/nonexistent/ccstack/state"))
        self.assertNotEqual(result.returncode, 0)


class DoctorStateActionDispatchTests(unittest.TestCase):
    """Smoke check that the UI dispatch table now contains doctor_cancel."""

    def test_cancel_action_branch_present(self):
        source = CCSTACK_PATH.read_text(encoding="utf-8")
        # Both the dispatch keyword and the helper must exist post-fix.
        self.assertIn('action == "doctor_cancel"', source)
        self.assertIn("execute_doctor_cancel", source)
        # And the JS side must POST to that action.
        self.assertIn('"doctor_cancel"', source)


if __name__ == "__main__":
    unittest.main()
