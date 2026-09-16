"""
Test: run_ibkr_worker_watchdog.py detects a stopped IB Gateway Docker
container and restarts it before launching the worker.

Background (2026-09-16): a host BSOD SIGKILLed the gateway container
(docker inspect showed ExitCode=137). Docker Desktop came back up on the
next login but never honored the container's `restart: unless-stopped`
policy — a known Docker Desktop gap after a host-level crash rather than a
clean daemon restart. The worker then spent 3.5 days in a silent
ConnectionRefusedError retry loop with 10 open positions getting zero
automated stop/exit monitoring, and nothing surfaced it until someone
happened to look. See CLAUDE.md Incident Archive 2026-09-16.

Run:
    python -m pytest tests/test_ibkr_worker_watchdog_gateway_guard.py -v
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import run_ibkr_worker_watchdog as watchdog


def _fake_run(running_output: str, start_returncode: int = 0, start_stderr: str = ""):
    """Build a fake subprocess.run that answers `docker ps` then `docker start`."""
    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["docker", "ps"]:
            return MagicMock(stdout=running_output, returncode=0)
        if cmd[:2] == ["docker", "start"]:
            return MagicMock(returncode=start_returncode, stderr=start_stderr)
        raise AssertionError(f"unexpected command: {cmd}")
    return fake_run


def test_container_already_running_does_nothing():
    fake_run = _fake_run(running_output=watchdog.GATEWAY_CONTAINER + "\n")
    with patch.object(subprocess, "run", side_effect=fake_run) as mock_run, \
         patch.object(watchdog, "_send_telegram") as mock_telegram:
        watchdog._ensure_gateway_container_running()

    # Only the `docker ps` check should fire — no start attempt, no alert.
    assert mock_run.call_count == 1
    mock_telegram.assert_not_called()


def test_container_stopped_is_started_and_reported():
    fake_run = _fake_run(running_output="")  # empty = not running
    with patch.object(subprocess, "run", side_effect=fake_run) as mock_run, \
         patch.object(watchdog, "_send_telegram") as mock_telegram:
        watchdog._ensure_gateway_container_running()

    assert mock_run.call_count == 2
    start_cmd = mock_run.call_args_list[1].args[0]
    assert start_cmd == ["docker", "start", watchdog.GATEWAY_CONTAINER]
    mock_telegram.assert_called_once()
    assert "auto-started" in mock_telegram.call_args.args[0]


def test_container_stopped_and_start_fails_still_reported():
    fake_run = _fake_run(running_output="", start_returncode=1, start_stderr="no such container")
    with patch.object(subprocess, "run", side_effect=fake_run), \
         patch.object(watchdog, "_send_telegram") as mock_telegram:
        watchdog._ensure_gateway_container_running()

    mock_telegram.assert_called_once()
    assert "auto-start failed" in mock_telegram.call_args.args[0]
    assert "no such container" in mock_telegram.call_args.args[0]


def test_docker_cli_missing_is_swallowed_not_raised():
    """If Docker Desktop itself isn't installed/running, this check must
    never block the worker from launching."""
    with patch.object(subprocess, "run", side_effect=FileNotFoundError("docker not found")), \
         patch.object(watchdog, "_send_telegram") as mock_telegram, \
         patch.object(watchdog.logging, "warning") as mock_warn:
        watchdog._ensure_gateway_container_running()  # must not raise

    mock_telegram.assert_not_called()
    assert mock_warn.called


def test_main_loop_checks_gateway_before_each_launch(monkeypatch, tmp_path):
    """Wiring check: main()'s loop calls the guard before spawning the worker."""
    monkeypatch.setattr(watchdog, "LOG_DIR", tmp_path)
    monkeypatch.setattr(watchdog, "WORKER_PID_FILE", tmp_path / "ibkr_worker.pid")
    sentinel = tmp_path / "stop_ibkr_worker.flag"
    monkeypatch.setattr(watchdog, "STOP_SENTINEL", sentinel)
    monkeypatch.setattr(watchdog, "_send_telegram", lambda msg: None)
    monkeypatch.setattr(watchdog, "_kill_orphaned_worker", lambda: None)
    monkeypatch.setattr(watchdog, "_rotate_worker_log", lambda path: None)

    calls = []
    monkeypatch.setattr(watchdog, "_ensure_gateway_container_running", lambda: calls.append(1))

    fake_proc = MagicMock(returncode=0, pid=12345)

    def fake_popen(*args, **kwargs):
        sentinel.write_text("stop")  # exit the loop after exactly one iteration
        return fake_proc

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    watchdog.main()

    assert calls == [1], "expected the gateway guard to run exactly once, before the single launch"
