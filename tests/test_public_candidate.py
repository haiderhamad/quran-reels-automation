"""Public installation boundaries independent of any provider account."""
from hashlib import sha256
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github/workflows"


class PublicInstallationTests(unittest.TestCase):
    def test_public_pull_request_checks_cannot_receive_provider_credentials(self):
        ci = (WORKFLOWS / "repository-safety.yml").read_text()
        self.assertIn("pull_request:", ci)
        self.assertIn("contents: read", ci)
        self.assertNotRegex(ci, r"secrets[.\[]|pull_request_target|secrets: inherit")
        self.assertIn("check_repository_safety.py --all-local-refs", ci)

    def test_credential_workflows_require_opt_in_and_trusted_private_default_branch(self):
        for path in WORKFLOWS.glob("*.yml"):
            source = path.read_text()
            if "secrets." not in source:
                continue
            with self.subTest(workflow=path.name):
                self.assertNotIn("pull_request", source)
                self.assertIn("github.event.repository.private", source)
                self.assertIn("github.event.repository.default_branch", source)
                self.assertIn("== 'true'", source)

    def test_worker_and_readback_share_scoped_installation_owned_reference(self):
        for name in ("generate-reel.yml", "verify-existing-facebook-reel.yml"):
            steps = (WORKFLOWS / name).read_text().split("\n      - ")
            readers = [s for s in steps if "secrets.ROWWAD_REFERENCE_B2_KEY }}" in s]
            self.assertEqual(len(readers), 1)
            self.assertIn("ROWWAD_REFERENCE_OBJECT_KEY", readers[0])
            self.assertIn("ROWWAD_REFERENCE_GZIP_SHA256", readers[0])
            self.assertIn("python3 -m scripts.prepare_rowwad_reference --public-if-unconfigured", readers[0])
            self.assertIn("QURAN_REELS_TRANSLATION_CACHE_DIR", readers[0])

    def test_no_operator_schedule_or_review_approval_bootstrapped(self):
        migrations = ROOT / "supabase/migrations"
        cron = next(migrations.glob("*_supabase_cron_scheduler.sql")).read_text()
        self.assertNotRegex(cron.lower(), r"select\s+cron.schedule\(")
        self.assertNotIn(".vercel.app", cron)
        self.assertIn("scheduler_origin", cron)
        self.assertRegex(cron, r"revoke all on function public\.invoke_publishing_scheduler\(\) from public, anon, authenticated, service_role;")
        registry = next(migrations.glob("*_owner_v1_reviewed_passage_claim_and_receipts.sql")).read_text()
        self.assertNotRegex(registry.lower(), r"insert into public\.calm_reels_v1_reviewed_passages")

    def test_catalog_identity_and_code_only_assets(self):
        self.assertEqual(sha256((ROOT / "data/editorial/calm_reels_shortlist_v1.json").read_bytes()).hexdigest(),
                         "df3eea4aefd469c838e3c9c31c58603122b8c8eaab50f0d36ef10d90a181666d")
        self.assertFalse((ROOT / "scripts/prepare_public_release.py").exists())
        self.assertFalse((ROOT / "alignments/candidates").exists())
        self.assertFalse((ROOT / "alignments/english_rwwad").exists())
        self.assertFalse((WORKFLOWS / "private-reviewed-fatiha-qa.yml").exists())


class OperatorSourceHandoffTests(unittest.TestCase):
    def test_package_and_verify_from_independent_synthetic_provider_responses(self):
        import json
        from tempfile import TemporaryDirectory
        from unittest.mock import patch
        from engine import translation as tr
        from scripts.package_rowwad_reference import package
        from scripts.verify_operator_sources import verify
        from tests.test_prepare_rowwad_reference import make_valid_complete_dataset
        data = make_valid_complete_dataset()
        digest = tr._cache_body_sha256(tr._cache_body_for_data(data))
        with TemporaryDirectory() as home:
            path = Path(home)
            body = tr._cache_body_for_data(data)
            meta = dict(body["source"], key=data.source.translation_key)
            (path / "metadata.json").write_text(json.dumps({"translations": [meta]}))
            for surah in body["surahs"]:
                (path / f'{surah["number"]:03d}.json').write_text(json.dumps({"result": surah["ayahs"]}))
            output = path / "authorized.gz"
            with self.assertRaisesRegex(ValueError, "differs"):
                package(path, output)
            self.assertFalse(output.exists())
            with patch.dict(tr.PINNED_TRANSLATION_BODY_HASHES, {("english_rwwad", "1.0.19"): digest}):
                report = package(path, output)
                checked = verify(output, None, path)
                for name in ("gzip_sha256", "body_sha256", "ayahs", "surahs", "version"):
                    self.assertEqual(report[name], checked[name])
                self.assertEqual(output.stat().st_mode & 0o777, 0o600)
                with self.assertRaises(FileExistsError):
                    package(path, output)
                with self.assertRaisesRegex(ValueError, "missing"):
                    verify(output, "2:25", path)


class BlankEnvironmentExamplesTests(unittest.TestCase):
    def test_empty_environment_value_cannot_consume_the_next_assignment(self):
        from scripts.check_repository_safety import scan_text
        field = "EXAMPLE_" + "API_KEY"
        content = f"{field}=\nOTHER_FIELD=value\n"
        self.assertEqual(scan_text(".env.example", content), [])

    def test_following_real_assignment_is_still_detected(self):
        from scripts.check_repository_safety import scan_text
        field = "SERVICE_" + "API_KEY"
        content = f"{field}=\n{field}=this-is-a-real-config-value\n"
        issues = scan_text("apps/dashboard/.env.example", content)
        self.assertEqual([issue.line for issue in issues], [2])
