"""
Tests for scheduler.run_llm_universe_curation() — the weekly job wrapper.

Safety property under test: this feature must be a strict no-op unless BOTH
the global scheduler is enabled AND llm_universe_curation_enabled is
explicitly true. Existing deployments with an unmodified scheduler_config.json
must see zero behavior change.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import scheduler  # noqa: E402


class TestDisabledByDefault:
    def test_missing_flag_is_a_no_op(self):
        """A scheduler_config.json without the new key at all (every existing
        deployment, until someone opts in) must not call the curator."""
        with patch.object(scheduler, "load_config", return_value={"enabled": True}), \
             patch("src.llm_universe_curator.run_weekly_curation") as run:
            scheduler.run_llm_universe_curation()
        run.assert_not_called()

    def test_flag_true_but_scheduler_globally_disabled_is_a_no_op(self):
        cfg = {"enabled": False, "llm_universe_curation_enabled": True}
        with patch.object(scheduler, "load_config", return_value=cfg), \
             patch("src.llm_universe_curator.run_weekly_curation") as run:
            scheduler.run_llm_universe_curation()
        run.assert_not_called()

    def test_flag_explicitly_false_is_a_no_op(self):
        cfg = {"enabled": True, "llm_universe_curation_enabled": False}
        with patch.object(scheduler, "load_config", return_value=cfg), \
             patch("src.llm_universe_curator.run_weekly_curation") as run:
            scheduler.run_llm_universe_curation()
        run.assert_not_called()


class TestEnabled:
    def test_runs_and_sends_telegram_on_changes(self):
        cfg = {"enabled": True, "llm_universe_curation_enabled": True, "telegram": True}
        result = {"week_of": "2026-08-10", "curated": ["AAA", "BBB"], "removed": ["ZZZ"], "ok": True}
        with patch.object(scheduler, "load_config", return_value=cfg), \
             patch("src.llm_universe_curator.run_weekly_curation", return_value=result), \
             patch.object(scheduler, "init_db"), \
             patch.object(scheduler, "TelegramNotifier") as tg:
            scheduler.run_llm_universe_curation()
        tg.return_value.send_message.assert_called_once()
        assert "2026-08-10" in tg.return_value.send_message.call_args[0][0]

    def test_failure_result_does_not_send_telegram_or_raise(self):
        cfg = {"enabled": True, "llm_universe_curation_enabled": True, "telegram": True}
        result = {"week_of": "2026-08-10", "curated": [], "removed": [], "ok": False}
        with patch.object(scheduler, "load_config", return_value=cfg), \
             patch("src.llm_universe_curator.run_weekly_curation", return_value=result), \
             patch.object(scheduler, "init_db"), \
             patch.object(scheduler, "TelegramNotifier") as tg:
            scheduler.run_llm_universe_curation()  # must not raise
        tg.return_value.send_message.assert_not_called()

    def test_exception_inside_is_caught_not_raised(self):
        cfg = {"enabled": True, "llm_universe_curation_enabled": True}
        with patch.object(scheduler, "load_config", return_value=cfg), \
             patch("src.llm_universe_curator.run_weekly_curation", side_effect=RuntimeError("boom")), \
             patch.object(scheduler, "init_db"):
            scheduler.run_llm_universe_curation()  # must not raise
