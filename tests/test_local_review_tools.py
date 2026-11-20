"""Local review tools must retain source integrity and leave publishing unreachable."""
import argparse
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class ReviewCliTests(unittest.TestCase):
    def test_proposal_is_local_unverified_and_reconstructs_source(self):
        from engine.renderer import RenderTranslation
        from scripts import propose_alignment as cli
        arabic, english = "one two three four five six seven", "each word belongs to a line"
        with patch.object(cli, "read_review_text", return_value=("A", "B", arabic,
                          RenderTranslation("en", english, "", "english_rwwad", "1.0.19"))):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cli.main(["--verse", "2:25"])
            result = json.loads(out.getvalue())
        self.assertEqual(result["status"], "unverified")
        self.assertFalse(result["publication_approved"])
        self.assertEqual(result["verse_key"], "2:25")
        self.assertEqual(" ".join(p["arabic_text"] for p in result["panels"]), arabic)
        self.assertEqual(" ".join(p["english_text"] for p in result["panels"]), english)

    def test_range_validation(self):
        from scripts.review_sources import parse_verse_key
        for value in ("0:1", "2:287", "1:0", "2:20:3"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                parse_verse_key(value)

    def test_existing_output_is_rejected_before_source_access(self):
        from scripts import generate_local_review_reel as cli
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "review.mp4"
            out.write_bytes(b"existing review")
            with patch.object(cli, "read_review_text") as read, self.assertRaises(FileExistsError):
                cli.review((2, 20), out)
            read.assert_not_called()
            self.assertEqual(out.read_bytes(), b"existing review")

    def test_source_mismatch_cannot_fall_back_to_an_unreviewed_layout(self):
        from engine.audio import AyahTiming
        from engine.renderer import RenderTranslation
        from scripts import generate_local_review_reel as cli
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "review.mp4"
            with patch.object(cli, "read_review_text", return_value=("Chapter", "Chapter", "altered",
                    RenderTranslation("en", "altered", "", "english_rwwad", "1.0.19"))), \
                 patch.object(cli.audio, "fetch_timing_read_metadata"), \
                 patch.object(cli.audio, "fetch_surah_timing") as timing, \
                 patch.object(cli.audio, "download_surah_audio") as download:
                timing.return_value.get_ayah_timing.return_value = AyahTiming(2, 20, 273480, 307380)
                with self.assertRaisesRegex(ValueError, "verified"):
                    cli.review((2, 20), out)
                self.assertFalse(out.exists())
                download.assert_not_called()

    def test_wrong_recording_is_rejected_before_any_video_work(self):
        from engine.audio import AyahTiming
        from engine.renderer import RenderTranslation, SUBTITLE_MODE_VERIFIED_PHRASE_PROGRESSIVE
        from scripts import generate_local_review_reel as cli
        with tempfile.TemporaryDirectory() as temp, contextlib.ExitStack() as patches:
            recording = Path(temp) / "recording.mp3"
            recording.write_bytes(b"unrelated recording")
            output = Path(temp) / "review.mp4"
            english = RenderTranslation("en", "test verse", "", "english_rwwad", "1.0.19")
            patches.enter_context(patch.object(cli, "read_review_text", return_value=("A", "B", "text", english)))
            patches.enter_context(patch.object(cli.audio, "fetch_timing_read_metadata"))
            timing = patches.enter_context(patch.object(cli.audio, "fetch_surah_timing"))
            timing.return_value.get_ayah_timing.return_value = AyahTiming(2, 25, 409920, 459120)
            resolve = patches.enter_context(patch.object(cli.renderer, "resolve_render_ayah_subtitle_plan"))
            resolve.return_value.subtitle_mode = SUBTITLE_MODE_VERIFIED_PHRASE_PROGRESSIVE
            render = patches.enter_context(patch.object(cli.renderer, "render_reel"))
            with self.assertRaisesRegex(ValueError, "Recording differs"):
                cli.review((2, 25), output, recording)
            render.assert_not_called()
            self.assertFalse(output.exists())

    def test_changed_read_interval_is_rejected_before_downloading_or_rendering(self):
        from engine.audio import AyahTiming
        from engine.renderer import RenderTranslation
        from scripts import generate_local_review_reel as cli
        with tempfile.TemporaryDirectory() as temp, contextlib.ExitStack() as patches:
            output = Path(temp) / "review.mp4"
            english = RenderTranslation("en", "test verse", "", "english_rwwad", "1.0.19")
            patches.enter_context(patch.object(cli, "read_review_text", return_value=("A", "B", "text", english)))
            patches.enter_context(patch.object(cli.audio, "fetch_timing_read_metadata"))
            timing = patches.enter_context(patch.object(cli.audio, "fetch_surah_timing"))
            timing.return_value.get_ayah_timing.return_value = AyahTiming(2, 20, 273481, 307381)
            download = patches.enter_context(patch.object(cli.audio, "download_surah_audio"))
            ffmpeg = patches.enter_context(patch.object(cli.subprocess, "run"))
            with self.assertRaisesRegex(ValueError, "timing differs"):
                cli.review((2, 20), output)
            download.assert_not_called()
            ffmpeg.assert_not_called()
            self.assertFalse(output.exists())
