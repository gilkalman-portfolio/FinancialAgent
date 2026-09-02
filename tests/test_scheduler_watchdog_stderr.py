"""
Test: run_scheduler_watchdog.py captures the scheduler child process's
stderr to a rotating log file instead of discarding it to DEVNULL.

Background (2026-08-26): 5 same-day returncode=1 scheduler crashes had no
available traceback — the watchdog launched scheduler.py with
stderr=subprocess.DEVNULL, and scheduler.py's own loguru sink can't record
an exception that kills the process before any handler runs. See CLAUDE.md
Incident Archive 2026-08-26.

run_scheduler_watchdog.py is a standalone watchdog script (an infinite
`while True` loop calling subprocess.run + time.sleep, no class or
dependency injection), so these tests exercise its two testable units
directly: _open_stderr_log() (rotation / header / open-failure fallback)
and main()'s wiring (subprocess.run must receive a real open file for
stderr=, not DEVNULL, and it must be closed afterward). The main() test is
forced to exit after exactly one loop iteration via a mocked subprocess.run
that returns returncode=0 (main()'s pre-existing "exited cleanly, stop"
path) — this doesn't test that branch itself, just uses it as a clean exit
so the test doesn't loop/sleep forever.

Run:
    python -m pytest tests/test_scheduler_watchdog_stderr.py -v
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

import run_scheduler_watchdog as watchdog


@pytest.fixture(autouse=True)
def _isolated_stderr_log(tmp_path, monkeypatch):
    """Every test gets its own STDERR_LOG path so tests can't see each
    other's rotation state or touch the worktree's real logs/ directory."""
    log_path = tmp_path / "scheduler_stderr.log"
    monkeypatch.setattr(watchdog, "STDERR_LOG", log_path)
    return log_path


# ── _open_stderr_log(): header, rotation, open-failure fallback ───────────────

def test_opens_fresh_file_and_writes_attempt_header(_isolated_stderr_log):
    f = watchdog._open_stderr_log(attempt=1)
    try:
        assert f is not subprocess.DEVNULL
        assert not f.closed
    finally:
        f.close()

    content = _isolated_stderr_log.read_text(encoding="utf-8")
    assert "attempt #1" in content


def test_child_stderr_written_through_the_returned_handle_is_readable(_isolated_stderr_log):
    """Sanity check that the returned object is a real, on-disk file — not
    an in-memory buffer — since subprocess needs a real OS file descriptor
    to redirect the child's stderr into."""
    f = watchdog._open_stderr_log(attempt=1)
    f.write("Traceback (most recent call last):\nRuntimeError: boom\n")
    f.close()

    content = _isolated_stderr_log.read_text(encoding="utf-8")
    assert "RuntimeError: boom" in content


def test_rotates_when_existing_log_exceeds_max_bytes(_isolated_stderr_log, monkeypatch):
    monkeypatch.setattr(watchdog, "STDERR_MAX_BYTES", 100)  # tiny threshold for the test
    _isolated_stderr_log.write_text("x" * 200, encoding="utf-8")

    f = watchdog._open_stderr_log(attempt=2)
    f.close()

    backup = _isolated_stderr_log.with_suffix(_isolated_stderr_log.suffix + ".1")
    assert backup.exists(), "expected the oversized log to be rotated to a .1 backup"
    assert backup.read_text(encoding="utf-8") == "x" * 200
    assert "x" * 200 not in _isolated_stderr_log.read_text(encoding="utf-8"), \
        "expected a fresh file after rotation, not the old oversized content"


def test_does_not_rotate_when_under_threshold(_isolated_stderr_log, monkeypatch):
    monkeypatch.setattr(watchdog, "STDERR_MAX_BYTES", 10_000_000)
    _isolated_stderr_log.write_text("small", encoding="utf-8")

    f = watchdog._open_stderr_log(attempt=2)
    f.close()

    backup = _isolated_stderr_log.with_suffix(_isolated_stderr_log.suffix + ".1")
    assert not backup.exists()
    assert "small" in _isolated_stderr_log.read_text(encoding="utf-8")


def test_open_failure_falls_back_to_devnull_and_logs_error():
    """The task's explicit requirement: a Popen stderr-redirect setup
    failure must not be silently swallowed."""
    with patch.object(watchdog, "RotatingFileHandler", side_effect=OSError("disk full")), \
         patch.object(watchdog.logging, "error") as mock_log_error:
        result = watchdog._open_stderr_log(attempt=1)

    assert result is subprocess.DEVNULL
    assert mock_log_error.called, "expected the fallback to be logged, not silent"


# ── main(): subprocess.run is wired to a real file, not DEVNULL ───────────────

def test_main_passes_open_file_as_stderr_and_closes_it_after(monkeypatch):
    captured = {}
    fake_proc = MagicMock(returncode=0)

    def fake_run(*args, **kwargs):
        stderr_arg = kwargs.get("stderr")
        captured["stderr"] = stderr_arg
        captured["was_open_during_call"] = (stderr_arg is not subprocess.DEVNULL
                                             and not stderr_arg.closed)
        return fake_proc

    monkeypatch.setattr(watchdog, "_send_telegram", lambda msg: None)
    monkeypatch.setattr(subprocess, "run", fake_run)

    watchdog.main()

    stderr_arg = captured["stderr"]
    assert stderr_arg is not subprocess.DEVNULL, \
        "expected stderr to be redirected to a real log file, not discarded"
    assert captured["was_open_during_call"] is True, \
        "the file must still be open while subprocess.run is using it"
    assert stderr_arg.closed, \
        "expected the stderr file handle to be closed after the process exits"
