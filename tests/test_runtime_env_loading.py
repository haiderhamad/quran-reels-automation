"""Regression tests for safe local environment loading."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.build_verified_background_pool import auto_load_env_if_needed


class TestRuntimeEnvLoading(unittest.TestCase):
    def test_loads_usable_values_without_overwriting_process_env(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env.local"
            env_path.write_text(
                "\n".join(
                    (
                        "QF_CLIENT_ID=local-client-id",
                        "QF_CLIENT_SECRET=\"local-client-secret\"",
                        "QF_ENV=[SENSITIVE]",
                        "IGNORED_EMPTY=",
                        "IGNORED_PLACEHOLDER=placeholder_secret",
                    )
                ),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"QF_CLIENT_ID": "process-client-id"},
                clear=True,
            ):
                auto_load_env_if_needed([env_path])
                self.assertEqual(os.environ["QF_CLIENT_ID"], "process-client-id")
                self.assertEqual(os.environ["QF_CLIENT_SECRET"], "local-client-secret")
                self.assertNotIn("QF_ENV", os.environ)
                self.assertNotIn("IGNORED_EMPTY", os.environ)
                self.assertNotIn("IGNORED_PLACEHOLDER", os.environ)


if __name__ == "__main__":
    unittest.main()
