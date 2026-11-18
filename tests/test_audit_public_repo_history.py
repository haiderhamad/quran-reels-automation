"""Offline regression checks for redacted, read-only Git publication preflight."""
import unittest
from scripts.audit_public_repo_history import scan_bytes, suspicious_path, SECRET_CLASSES

class PublicRepoAuditTests(unittest.TestCase):
    def test_secret_patterns_never_return_values(self):
        token=b"ghp_" + b"a"*36
        result=scan_bytes("src/settings.ts",b"export const x = '"+token+b"';")
        self.assertIn("github_token",result)
        self.assertTrue(all("ghp" not in label for label in result))
        self.assertIn("provider_secret",scan_bytes("source.py",b"sk-proj-"+b"X"*32))
        self.assertIn("private_key_header",scan_bytes("a.md",b"-----BEGIN " + b"PRIVATE KEY-----"))
    def test_fixture_secrets_need_review_not_automatic_certification(self):
        self.assertIn("provider_secret",scan_bytes("tests/mock.py",b"sb_secret_"+b"t"*25))
        self.assertNotIn("github_token",scan_bytes("README.md",b"ghp_sample"))
    def test_git_history_path_flags(self):
        self.assertEqual(suspicious_path(".env.local"),"private_environment_file")
        self.assertEqual(suspicious_path("dir/prod.pem"),"possibly_private_key_or_database")
        self.assertEqual(suspicious_path(".ai-bridge/current-plan.md"),"local_or_private_directory")
        self.assertIsNone(suspicious_path("apps/dashboard/.env.example"))
        self.assertIsNone(suspicious_path("data/editorial/calm_reels_shortlist_v1.json"))
    def test_binary_skips_text_guessing(self):
        self.assertEqual(scan_bytes("asset.mp4",b"\x00"+b"ghp_"+b"x"*36),set())
    def test_personal_path_is_flagged_without_saving_person_identity(self):
        self.assertEqual(scan_bytes("old.md",b"/" + b"Users/someone/Code/secret"),{"personal_absolute_path"})
        self.assertEqual(scan_bytes("README.md",b"https://quranenc.com/en/home/api/"),set())

if __name__=="__main__":
    unittest.main()
