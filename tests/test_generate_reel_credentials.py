"""Static and regression test suite for Generate Reel Quran Foundation credential routing."""

from __future__ import annotations

import os
from pathlib import Path
import unittest
from unittest.mock import patch

from engine.quran import (
    QuranSourceError,
    fetch_quran_data,
    validate_quran_source_for_run,
)
from tests.test_quran_source_gate import (
    make_full_production_quran,
    make_prelive_sandbox_quran,
)


class TestGenerateReelCredentialRouting(unittest.TestCase):
    """Verifies strict fail-closed QF credential routing in generate-reel.yml."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow_path = (
            Path(__file__).resolve().parent.parent
            / ".github"
            / "workflows"
            / "generate-reel.yml"
        )
        cls.assertTrue(
            cls.workflow_path.exists(),
            f"Workflow file missing: {cls.workflow_path}",
        )
        cls.workflow_text = cls.workflow_path.read_text(encoding="utf-8")

    # -------------------------------------------------------------------------
    # 1. Static Workflow Contract Verifications
    # -------------------------------------------------------------------------

    def test_repository_qf_env_secret_not_referenced(self) -> None:
        """The repository secret QF_ENV must not be used as a runtime selector."""
        self.assertNotIn(
            "secrets.QF_ENV",
            self.workflow_text,
            "generate-reel.yml must not reference secrets.QF_ENV; runtime env derives strictly from run_kind.",
        )

    def test_no_unsafe_ternary_fallback_expressions(self) -> None:
        """Workflow must not use condition && secret_a || secret_b fallbacks."""
        for line in self.workflow_text.splitlines():
            if "QF_CLIENT" in line or "QF_ENV" in line:
                self.assertNotIn(
                    "||",
                    line,
                    f"Unsafe fallback expression detected in line: {line}",
                )
                self.assertNotIn(
                    "&&",
                    line,
                    f"Unsafe ternary expression detected in line: {line}",
                )

    def test_run_semantics_validation_step_exists(self) -> None:
        """A validation step must enforce supported run_kind inputs before execution."""
        self.assertIn("name: Validate run semantics", self.workflow_text)
        self.assertIn("test|scheduled|manual_production)", self.workflow_text)
        self.assertIn("exit 1", self.workflow_text)

    def test_test_execution_step_credential_isolation(self) -> None:
        """Test execution step references strictly prelive credentials and QF_ENV=prelive."""
        step_marker = "name: Run Quran Reel generation job (test run)"
        self.assertIn(step_marker, self.workflow_text)
        test_block = self.workflow_text.split(step_marker)[1].split("- name:")[0]

        self.assertIn("if: inputs.run_kind == 'test'", test_block)
        self.assertIn("QF_CLIENT_ID: ${{ secrets.QF_CLIENT_ID }}", test_block)
        self.assertIn("QF_CLIENT_SECRET: ${{ secrets.QF_CLIENT_SECRET }}", test_block)
        self.assertIn("QF_ENV: prelive", test_block)

        # Must not leak or reference production secrets in the test step
        self.assertNotIn("QF_PRODUCTION_CLIENT_ID", test_block)
        self.assertNotIn("QF_PRODUCTION_CLIENT_SECRET", test_block)

    def test_production_execution_step_credential_isolation(self) -> None:
        """Production execution step references strictly production credentials and QF_ENV=production."""
        step_marker = "name: Run Quran Reel generation job (production run)"
        self.assertIn(step_marker, self.workflow_text)
        prod_block = self.workflow_text.split(step_marker)[1]

        self.assertIn("if: inputs.run_kind == 'scheduled' || inputs.run_kind == 'manual_production'", prod_block)
        self.assertIn("QF_CLIENT_ID: ${{ secrets.QF_PRODUCTION_CLIENT_ID }}", prod_block)
        self.assertIn("QF_CLIENT_SECRET: ${{ secrets.QF_PRODUCTION_CLIENT_SECRET }}", prod_block)
        self.assertIn("QF_ENV: production", prod_block)

        # Must not leak or reference prelive secrets in the production step
        self.assertNotIn("secrets.QF_CLIENT_ID", prod_block)
        self.assertNotIn("secrets.QF_CLIENT_SECRET", prod_block)

    # -------------------------------------------------------------------------
    # 2. Runtime Credential Behavior & Fail-Closed Emulation
    # -------------------------------------------------------------------------

    def test_missing_credentials_fails_closed(self) -> None:
        """When the selected credential pair is missing/empty, execution fails closed."""
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(QuranSourceError) as ctx:
                fetch_quran_data(client_id=None, client_secret=None, qf_env="production")
            self.assertIn("Missing Quran Foundation credentials", str(ctx.exception))

            with self.assertRaises(QuranSourceError) as ctx:
                fetch_quran_data(client_id=None, client_secret=None, qf_env="prelive")
            self.assertIn("Missing Quran Foundation credentials", str(ctx.exception))

    def test_scheduled_run_with_prelive_dataset_fails_closed(self) -> None:
        """If scheduled run somehow received prelive data, production gate fails closed."""
        prelive_quran = make_prelive_sandbox_quran()
        with self.assertRaises(QuranSourceError) as ctx:
            validate_quran_source_for_run("scheduled", prelive_quran)
        self.assertIn("requires verified production Quran source (env='production')", str(ctx.exception))

    def test_manual_production_run_with_prelive_dataset_fails_closed(self) -> None:
        """If manual_production run somehow received prelive data, production gate fails closed."""
        prelive_quran = make_prelive_sandbox_quran()
        with self.assertRaises(QuranSourceError) as ctx:
            validate_quran_source_for_run("manual_production", prelive_quran)
        self.assertIn("requires verified production Quran source (env='production')", str(ctx.exception))

    def test_test_run_with_prelive_dataset_is_permitted(self) -> None:
        """Test runs are explicitly permitted to use the approved prelive sandbox."""
        prelive_quran = make_prelive_sandbox_quran()
        # Must not raise
        validate_quran_source_for_run("test", prelive_quran)

    def test_production_run_with_production_dataset_passes(self) -> None:
        """Production runs with complete production dataset pass the gate."""
        prod_quran = make_full_production_quran()
        # Must not raise
        validate_quran_source_for_run("scheduled", prod_quran)
        validate_quran_source_for_run("manual_production", prod_quran)


if __name__ == "__main__":
    unittest.main()
