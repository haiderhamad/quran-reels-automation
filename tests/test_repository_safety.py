from __future__ import annotations

import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.check_repository_safety import (
    MAX_TEXT_FILE_BYTES,
    HistoryViolation,
    SafetyCheckError,
    Violation,
    _should_scan_path,
    main,
    scan_all_local_refs,
    scan_repository,
    scan_text,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class RepositorySafetyPatternTests(unittest.TestCase):
    def test_rejects_token_like_powershell_prompt(self) -> None:
        secret = "aA1" + ("B" * 53)
        prompt = 'Read-Host "' + secret + '"'

        violations = scan_text("launcher.ps1", prompt)

        self.assertEqual(len(violations), 1)
        self.assertEqual(
            violations[0].rule,
            "high-entropy credential-like quoted literal",
        )
        self.assertNotIn(secret, violations[0].format())

    def test_rejects_provider_token_signature(self) -> None:
        provider_value = "github_pat_" + ("A1" * 12)

        violations = scan_text("settings.py", 'TOKEN="' + provider_value + '"')

        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].rule, "GitHub token literal")
        self.assertNotIn(provider_value, violations[0].format())

    def test_allows_obvious_fake_provider_token(self) -> None:
        fake_provider_value = "github_pat_" + ("X" * 24)

        self.assertEqual(scan_text("README.md", fake_provider_value), [])

    def test_rejects_sensitive_literal_assignment(self) -> None:
        source = 'R2_SECRET_ACCESS_KEY="' + ("a" * 32) + '"'

        violations = scan_text("settings.env", source)

        self.assertEqual(len(violations), 1)
        self.assertEqual(
            violations[0].rule,
            "credential-like literal assigned to a sensitive setting",
        )

    def test_rejects_generic_secret_names(self) -> None:
        names = (
            "ADMIN_PASSWORD",
            "AWS_SECRET_ACCESS_KEY",
            "CLOUDFLARE_API_TOKEN",
            "DATABASE_PASSWORD",
            "META_APP_SECRET",
            "PASSWORD",
            "SECRET",
            "TOKEN",
        )

        for name in names:
            with self.subTest(name=name):
                source = name + '="sensitive-value-123"'
                self.assertEqual(len(scan_text("settings.py", source)), 1)

    def test_rejects_bcrypt_style_literal_as_a_secret(self) -> None:
        credential_hash = "$2b$12$" + ("aB1" * 10)
        source = 'ADMIN_PASSWORD_HASH="' + credential_hash + '"'

        self.assertEqual(len(scan_text("settings.py", source)), 1)

    def test_rejects_quoted_mapping_key(self) -> None:
        setting_name = "META_ACCESS_" + "TOKEN"
        source = '"' + setting_name + '": "sensitive-value-123"'

        self.assertEqual(len(scan_text("settings.json", source)), 1)

    def test_allows_placeholders_and_secret_references(self) -> None:
        source = "\n".join(
            (
                "R2_SECRET_ACCESS_KEY=replace_me",
                "META_ACCESS_TOKEN=${{ secrets.META_ACCESS_TOKEN }}",
            )
        )

        self.assertEqual(scan_text(".env.example", source), [])

    def test_allows_explicit_uppercase_template_replacement_instruction(self) -> None:
        for setting in ("PEXELS_API_KEY", "PIXABAY_API_KEY", "SUPABASE_SERVICE_ROLE_KEY"):
            with self.subTest(setting=setting):
                self.assertEqual(scan_text(
                    "k8s/worker-secret-template.yaml",
                    f"{setting}: REPLACE_WITH_{setting}",
                ), [])

    def test_allows_exact_runtime_template_instruction(self) -> None:
        self.assertEqual(scan_text(
            "monitoring/grafana-cloud-secret-template.yaml",
            "GRAFANA_CLOUD_ACCESS_POLICY_TOKEN: REPLACE_AT_RUNTIME",
        ), [])
        self.assertTrue(scan_text(
            "monitoring/grafana-cloud-secret-template.yaml",
            "TOKEN: REPLACE_AT_RUNTIME_secret_value",
        ))

    def test_template_instruction_cannot_hide_real_provider_token(self) -> None:
        token = "ghp_" + "A1" * 12
        self.assertTrue(scan_text(
            "k8s/worker-secret-template.yaml", "API_KEY: REPLACE_WITH_" + token,
        ))
        for value in ("replace_with_some_secret", "REPLACE_WITH_KEY-secret", "REPLACE_WITH_"):
            with self.subTest(value=value):
                self.assertTrue(scan_text("k8s/worker-secret-template.yaml", "API_KEY: " + value))

    def test_rejects_upstream_target_in_active_workflow(self) -> None:
        target = "habib-hf" + "/quran-reels-generator"

        violations = scan_text(".github/workflows/deploy.yml", target)

        self.assertEqual(len(violations), 1)
        self.assertIn("prohibited imported deployment target", violations[0].rule)

    def test_allows_upstream_reference_in_documentation(self) -> None:
        target = "Habib-HF" + "/Quran-Reels-Generator"

        self.assertEqual(scan_text("README.md", target), [])

    def test_allows_commented_upstream_workflow_reference(self) -> None:
        target = "Habib-HF" + "/Quran-Reels-Generator"

        self.assertEqual(
            scan_text(".github/workflows/safety.yml", "# retired " + target),
            [],
        )

    def test_rejects_legacy_workflow_filename(self) -> None:
        violations = scan_text(".github/workflows/deploy-hf.yml", "name: retired")

        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].rule, "legacy publish/release workflow is active")

    def test_rejects_legacy_workflow_markers(self) -> None:
        markers = (
            "huggingface_" + "hub",
            "softprops/" + "action-gh-release",
            "Afrasyaab-GH" + "/Quran-Reels-Generator",
        )

        for marker in markers:
            with self.subTest(marker=marker):
                violations = scan_text(".github/workflows/publish.yml", marker)
                self.assertEqual(len(violations), 1)

    def test_allows_disabled_workflow_provenance(self) -> None:
        target = "Habib-HF" + "/Quran-Reels-Generator"

        self.assertEqual(
            scan_text("legacy/deploy-hf.yml.disabled", target),
            [],
        )

    def test_oversized_active_workflow_is_still_scanned(self) -> None:
        self.assertTrue(
            _should_scan_path(
                ".github/workflows/publish.yml",
                MAX_TEXT_FILE_BYTES + 1,
            )
        )
        self.assertFalse(
            _should_scan_path("large-generated-data.txt", MAX_TEXT_FILE_BYTES + 1)
        )


class RepositorySafetyCliTests(unittest.TestCase):
    def test_cli_failure_is_redacted(self) -> None:
        secret = "aA1" + ("B" * 53)
        finding = Violation("settings.py", 4, "credential-like literal")
        stderr = io.StringIO()

        with mock.patch(
            "scripts.check_repository_safety.scan_repository",
            return_value=[finding],
        ), contextlib.redirect_stderr(stderr):
            exit_code = main()

        self.assertEqual(exit_code, 1)
        self.assertNotIn(secret, stderr.getvalue())
        self.assertIn("settings.py:4", stderr.getvalue())

    def test_cli_operational_failure_is_sanitized(self) -> None:
        stderr = io.StringIO()

        with mock.patch(
            "scripts.check_repository_safety.scan_repository",
            side_effect=SafetyCheckError("sensitive internal detail"),
        ), contextlib.redirect_stderr(stderr):
            exit_code = main()

        self.assertEqual(exit_code, 2)
        self.assertNotIn("sensitive internal detail", stderr.getvalue())

    def test_history_cli_failure_is_redacted(self) -> None:
        secret = "aA1" + ("B" * 53)
        finding = HistoryViolation(
            ref="refs/heads/main",
            path="settings.py",
            line=4,
            rule="credential-like literal",
            blob="a" * 40,
        )
        stderr = io.StringIO()

        with mock.patch(
            "scripts.check_repository_safety.scan_all_local_refs",
            return_value=[finding],
        ), contextlib.redirect_stderr(stderr):
            exit_code = main(["--all-local-refs"])

        self.assertEqual(exit_code, 1)
        self.assertNotIn(secret, stderr.getvalue())
        self.assertIn("refs/heads/main:settings.py:4", stderr.getvalue())


class LocalHistorySafetyTests(unittest.TestCase):
    def _git(
        self,
        repository: Path,
        *arguments: str,
        input_bytes: bytes | None = None,
    ) -> bytes:
        return subprocess.run(
            ["git", *arguments],
            cwd=repository,
            input=input_bytes,
            check=True,
            capture_output=True,
        ).stdout

    def _initialize_repository(self, repository: Path) -> None:
        self._git(repository, "init", "-q")
        self._git(repository, "config", "user.name", "Safety Test")
        self._git(repository, "config", "user.email", "safety@example.invalid")

    def test_finds_secret_deleted_from_tip_but_retained_by_parent(self) -> None:
        secret = "aA1" + ("B" * 53)
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir)
            self._initialize_repository(repository)
            settings = repository / "settings.py"
            settings.write_text('TOKEN="' + secret + '"\n', encoding="utf-8")
            self._git(repository, "add", "settings.py")
            self._git(repository, "commit", "-q", "-m", "unsafe parent")
            settings.write_text("TOKEN=os.environ['TOKEN']\n", encoding="utf-8")
            self._git(repository, "add", "settings.py")
            self._git(repository, "commit", "-q", "-m", "safe tip")

            findings = scan_all_local_refs(repository)

        self.assertTrue(findings)
        self.assertNotIn(secret, "\n".join(finding.format() for finding in findings))

    def test_scans_direct_tree_refs_and_binary_non_utf8_blobs(self) -> None:
        secret = "aA1" + ("B" * 53)
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir)
            self._initialize_repository(repository)
            settings = repository / "settings.env"
            settings.write_bytes(b"\xff\x00TOKEN=\"" + secret.encode("ascii") + b"\"\n")
            self._git(repository, "add", "settings.env")
            unsafe_tree = self._git(repository, "write-tree").decode().strip()
            settings.write_text("TOKEN=${TOKEN}\n", encoding="utf-8")
            self._git(repository, "add", "settings.env")
            self._git(repository, "commit", "-q", "-m", "safe root")
            self._git(repository, "update-ref", "refs/codex/test-tree", unsafe_tree)

            findings = scan_all_local_refs(repository)

        self.assertTrue(
            any(finding.ref == "refs/codex/test-tree" for finding in findings)
        )
        self.assertNotIn(secret, "\n".join(finding.format() for finding in findings))


class CurrentRepositorySafetyTests(unittest.TestCase):
    def test_current_repository_is_clean(self) -> None:
        self.assertEqual(scan_repository(REPOSITORY_ROOT), [])

    def test_all_current_local_refs_are_clean(self) -> None:
        self.assertEqual(scan_all_local_refs(REPOSITORY_ROOT), [])


if __name__ == "__main__":
    unittest.main()
