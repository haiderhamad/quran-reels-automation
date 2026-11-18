"""Hermetic qualification runner bounds; no remote store or CV is invoked."""
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from engine.background_candidates import BACKGROUND_SELECTABLE_TARGET, DISCOVERY_VERIFIED_TARGET
from scripts import run_background_qualification as runner


class QualificationRunnerTests(unittest.TestCase):
    def run_worker(self, counts, argv=None):
        store = Mock()
        store.count_selectable_background_assets.side_effect = counts
        with ExitStack() as stack:
            for name in ('auto_load_env_if_needed', '_validate_qualification_env', 'verify_current_safety_model_files'):
                stack.enter_context(patch.object(runner, name))
            stack.enter_context(patch.object(runner.SupabaseConfig, 'from_env'))
            stack.enter_context(patch.object(runner, 'SupabaseStateStore', return_value=store))
            stack.enter_context(patch.object(runner, 'get_background_pool_repository'))
            qualify = stack.enter_context(patch.object(runner, 'qualify_one_claimed_candidate', return_value={'status': 'VERIFIED'}))
            self.assertEqual(runner.main(argv or []), 0)
        return qualify

    def test_default_qualifies_three_until_100_selectable(self):
        self.assertEqual(BACKGROUND_SELECTABLE_TARGET, 100)
        self.assertEqual(DISCOVERY_VERIFIED_TARGET, 100)
        self.assertEqual(self.run_worker([40, 40, 41, 42]).call_count, 3)

    def test_github_target_250_continues_cv_above_local_default_100(self):
        # Verified pool may contain 191 eligible assets. A GitHub job with
        # the default 100 target would silently process zero candidates.
        checks = self.run_worker([191, 191, 192, 193], ["--target-selectable", "250"])
        self.assertEqual(checks.call_count, 3)

    def test_100_selectable_exits_before_claim(self):
        self.run_worker([100]).assert_not_called()

    def test_target_rechecked_before_each_claim(self):
        self.assertEqual(self.run_worker([99, 99, 100]).call_count, 1)

    def test_workflow_default_and_bounded_timeout_preserve_schedule(self):
        text = (Path(__file__).resolve().parents[1] / '.github/workflows/background-qualification.yml').read_text()
        self.assertIn('cron: "37 2,14 * * *"', text)
        self.assertIn('timeout-minutes: 15', text)
        self.assertIn("inputs.max_candidates || '3'", text)
        self.assertIn("inputs.target_selectable || '250'", text)
