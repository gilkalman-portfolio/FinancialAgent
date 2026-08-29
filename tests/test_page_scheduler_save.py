"""
Test: page_scheduler.py's "Save settings" handler preserves config keys
it doesn't itself edit, instead of replacing scheduler_config.json wholesale.

Background (2026-08-26): the Save handler built `new_cfg` as a fresh dict
literal listing only the ~18 keys this form edits, then overwrote
scheduler_config.json with it. Any key outside that literal — auto_watchlist,
momentum_enabled, gap_scanner settings, weekly_rotation_time, and 20+ others
covering entire subsystems — was silently wiped the next time anyone clicked
"Save settings" on the Scheduler dashboard page. Fixed by spreading the
already-loaded `cfg` dict into `new_cfg` before applying this form's edits.

Run:
    .venv\\Scripts\\python.exe -m pytest tests/test_page_scheduler_save.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

sys.path.insert(0, str(Path(__file__).parent.parent))


def _run_scheduler_page(cfg_path):
    from unittest.mock import patch

    import _pages_modules.page_scheduler as page_scheduler

    with patch.object(page_scheduler, "_CFG_FILE", __import__("pathlib").Path(cfg_path)), \
         patch.object(page_scheduler, "get_sectors", return_value=["Technology", "Healthcare"]):
        page_scheduler.render()


@pytest.fixture
def cfg_file(tmp_path):
    path = tmp_path / "scheduler_config.json"
    path.write_text(json.dumps({
        "enabled": False,
        "times": ["08:30", "16:30"],
        # Keys this form never edits — must survive a Save round-trip.
        "auto_watchlist": True,
        "momentum_enabled": True,
        "momentum_interval_minutes": 30,
        "gap_scanner": {"enabled": True, "threshold_pct": 3},
        "weekly_rotation_time": "08:15",
    }))
    return path


def test_save_settings_preserves_untouched_keys(cfg_file):
    at = AppTest.from_function(_run_scheduler_page, args=(str(cfg_file),))
    at.run(timeout=30)
    assert not at.exception

    # Flip a form-editable field so the save actually changes something.
    at.toggle[0].set_value(True)
    at.run(timeout=30)

    at.button[0].click()
    at.run(timeout=30)
    assert not at.exception

    saved = json.loads(cfg_file.read_text())

    # Edited field took the new value.
    assert saved["enabled"] is True

    # Untouched keys — including a nested dict — survived unchanged.
    assert saved["auto_watchlist"] is True
    assert saved["momentum_enabled"] is True
    assert saved["momentum_interval_minutes"] == 30
    assert saved["gap_scanner"] == {"enabled": True, "threshold_pct": 3}
    assert saved["weekly_rotation_time"] == "08:15"
