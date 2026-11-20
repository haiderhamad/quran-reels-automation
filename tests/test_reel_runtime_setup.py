"""Exercise the hosted worker's native dependency bootstrap without running apt."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/setup_reel_runtime.sh"


class ReelRuntimeSetupTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.log = self.directory / "apt.log"
        self.log.touch()
        self.ready = self.directory / "ready"
        self.env = dict(os.environ, PATH=f"{self.directory}:/usr/bin:/bin",
                        LOG=str(self.log), READY=str(self.ready), TMPDIR=temp.name)
        self.stub("ffmpeg", """
if [ "$1" = "-filters" ]; then
    echo ' T.. bass A->A bass filter'
    [ -e "$READY" ] && echo ' ... ass V->V libass'
else
    printf '%s\\n' ' V....D libx264 video' ' A..... aac audio'
fi
exit 0
""")
        for name in ("fc-match", "ffprobe", "python3"):
            self.stub(name, 'exit 0')
        self.stub("sudo", """
echo "$*" >> "$LOG"
for arg in "$@"; do
    case $arg in
      Dir::Etc::sourcelist=*) cat "${arg#*=}" > "$READY.sources" ;;
    esac
done
case "$*" in
  *--download-only*)
    if [ -n "$FAIL_DOWNLOAD" ]; then exit 100; fi
    if [ -n "$FAIL_FIRST" ] && [ ! -f "$READY.tried" ]; then
      touch "$READY.tried"
      exit 124
    fi
    ;;
  *--no-download*) touch "$READY" ;;
esac
""")

    def stub(self, name, body):
        file = self.directory / name
        file.write_text("#!/bin/sh\n" + body + "\n")
        file.chmod(0o755)

    def run_setup(self, **overrides):
        return subprocess.run(["bash", str(SCRIPT)], cwd=ROOT, env=self.env | overrides,
                              capture_output=True, text=True, timeout=10)

    def test_worker_and_offline_ci_exercise_the_same_install_and_bundled_fonts(self):
        for workflow in ("generate-reel.yml", "repository-safety.yml"):
            text = (ROOT / ".github/workflows" / workflow).read_text()
            if workflow == "repository-safety.yml":
                text = text.split("  offline-regression:", 1)[1].split("  dashboard:", 1)[0]
            self.assertIn("bash scripts/setup_reel_runtime.sh", text)
            self.assertIn("python3 scripts/fetch_fonts.py --offline", text)
            for package in ("fonts-noto-core", "fonts-noto-extra", "libass-dev"):
                self.assertNotIn(package, text)

    def test_ready_runner_needs_no_package_manager(self):
        self.ready.touch()
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.log.read_text(), "")

    def test_slow_download_retries_another_source_before_install(self):
        result = self.run_setup(FAIL_FIRST="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.log.read_text().splitlines()
        downloads = [c for c in calls if "--download-only" in c]
        self.assertEqual(len(downloads), 2)
        self.assertNotEqual(downloads[0], downloads[1])
        sources = (self.directory / "ready.sources").read_text().splitlines()
        self.assertEqual(len(sources), 3)
        for source in sources:
            self.assertIn("signed-by=/usr/share/keyrings/ubuntu-archive-keyring.gpg", source)
            self.assertIn("https://archive.ubuntu.com/ubuntu", source)
        for c in calls:
            if " update" in c or "--download-only" in c:
                self.assertIn("timeout", c)
            self.assertNotIn("fonts-noto", c)
        self.assertIn("--no-download", calls[-1])

    def test_exhausted_mirrors_do_not_install(self):
        result = self.run_setup(FAIL_DOWNLOAD="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.log.read_text().count("--download-only"), 2)
        self.assertNotIn("--no-download", self.log.read_text())

    def test_post_install_missing_probe_fails_closed(self):
        self.stub("ffprobe", "exit 1")
        result = self.run_setup()
        self.assertIn("--no-download", self.log.read_text())
        self.assertNotEqual(result.returncode, 0)
