"""Regression tests for the owner-approved Mac / GitHub Actions execution split.

No network or database access; these checks protect the operational boundary
without treating an environment variable as a security credential.
"""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import os
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from engine.job import JobResult, main


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


class WorkerRoleBoundariesTests(TestCase):
    def test_local_cli_never_generates_or_publishes_a_reel(self):
        import subprocess
        import sys

        for arguments in (
            ["--run-kind", "test"],
            ["--run-kind", "scheduled", "--schedule-slot-id", "morning"],
            ["--run-kind", "manual_production"],
        ):
            with self.subTest(arguments=arguments):
                environment = dict(os.environ)
                environment.pop("GITHUB_ACTIONS", None)
                result = subprocess.run(
                    [sys.executable, "-m", "engine.job", *arguments],
                    cwd=ROOT, env=environment, capture_output=True, text=True,
                    timeout=20,
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn("REEL_WORKER_GITHUB_ACTIONS_ONLY", result.stderr)

    def test_github_reel_cli_delegates_to_existing_worker(self):
        result = JobResult(
            status="SKIPPED", slot="test", date="2026-10-30",
            run_id="fixture", skipped_reason="SAFE_TEST_ONLY",
        )
        with (
            patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}),
            patch("engine.job.run_job", return_value=result) as runner,
            redirect_stdout(StringIO()),
        ):
            self.assertEqual(main(["--run-kind", "test"]), 0)
            runner.assert_called_once()
            self.assertEqual(runner.call_args.args[0].run_kind, "test")

    def test_github_one_prescan_and_two_cv_qualification_runs_daily(self):
        checks = (
            ("background-discovery.yml", 'cron: "17 1 * * *"', "discover_background_candidates.py"),
            ("background-qualification.yml", 'cron: "37 2,14 * * *"', "run_background_qualification.py"),
        )
        for filename, cron, script in checks:
            with self.subTest(workflow=filename):
                content = (WORKFLOWS / filename).read_text(encoding="utf-8")
                self.assertIn(cron, content)
                self.assertEqual(content.count("    - cron:"), 1)
                self.assertIn("actions/setup-python@", content)
                self.assertIn("python3", content)
                self.assertIn("scripts/" + script, content)
                self.assertIn("vars.QURAN_REELS_BACKGROUND_ENABLED == 'true'", content)
                self.assertNotIn("vars.QURAN_REELS_PRODUCTION_ENABLED", content)
                self.assertIn("  contents: read", content)
        qualification = (WORKFLOWS / "background-qualification.yml").read_text()
        self.assertIn(
            "if: ${{ github.event.repository.private && "
            "github.ref == format('refs/heads/{0}', github.event.repository.default_branch) && "
            "vars.QURAN_REELS_BACKGROUND_ENABLED == 'true' && "
            "vars.BACKGROUND_QUALIFICATION_ENABLED != 'false' }}",
            qualification,
        )
        self.assertIn("scripts/fetch_cv_models.py", qualification)
        self.assertIn('default: "250"', qualification)
        self.assertIn("inputs.target_selectable || '250'", qualification)
        self.assertIn("inputs.max_candidates || '3'", qualification)
        discovery = (WORKFLOWS / "background-discovery.yml").read_text()
        self.assertIn(
            "if: ${{ github.event.repository.private && "
            "github.ref == format('refs/heads/{0}', github.event.repository.default_branch) && "
            "vars.QURAN_REELS_BACKGROUND_ENABLED == 'true' && "
            "vars.BACKGROUND_DISCOVERY_ENABLED != 'false' }}",
            discovery,
        )
        self.assertIn("inputs.max_candidates || '20'", discovery)
        self.assertNotIn('cron: "17 1,13 * * *"', discovery)
        self.assertNotIn("engine.job", qualification)

    def test_reel_production_remains_github_only_and_buffer_only(self):
        source = (WORKFLOWS / "generate-reel.yml").read_text()
        self.assertIn("python3 -u -m engine.job", source)
        self.assertIn("PUBLISHER_BACKEND: ${{ vars.PUBLISHER_BACKEND || 'buffer' }}", source)
        self.assertIn("BUFFER_FACEBOOK_ENABLED", source)
        self.assertIn("BUFFER_INSTAGRAM_ENABLED", source)
        self.assertIn("vars.QURAN_REELS_PRODUCTION_ENABLED == 'true'", source)
        self.assertNotIn("QURAN_REELS_BACKGROUND_ENABLED", source)
        scheduler = (WORKFLOWS / "quran-publishing-scheduler.yml").read_text()
        self.assertIn("vars.QURAN_REELS_PRODUCTION_ENABLED == 'true'", scheduler)
        self.assertNotIn("QURAN_REELS_BACKGROUND_ENABLED", scheduler)
        self.assertIn("workflow_dispatch:", scheduler)
        self.assertNotIn("  schedule:", scheduler)  # Supabase pg_cron is the sole recurring publisher.
        self.assertIn("uses: ./.github/workflows/generate-reel.yml", scheduler)
        self.assertNotIn("docker build", source)
        self.assertNotIn("kubectl", source)

    def test_diagrams_do_not_route_mac_into_reel_generation(self):
        for name in ("architecture.mmd", "execution-backends.mmd"):
            text = (ROOT / "docs" / "diagrams" / name).read_text()
            self.assertIn("GitHub Actions", text)
            self.assertIn("Mac", text)
            self.assertIn("CV", text)
            self.assertIn("Buffer", text)
        diagram = (ROOT / "docs/diagrams/execution-backends.mmd").read_text()
        self.assertNotIn("mac_cv --> gh_reel", diagram)
        self.assertNotIn("mac_discovery --> gh_reel", diagram)
        self.assertIn("pool -->|verified source hash| gh_reel", diagram)
