"""Render a reviewed Arabic/English verse to a local MP4 on a synthetic background."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

from engine import audio, renderer
from engine.subtitles import SubtitleConfig
from scripts.fetch_fonts import install_bundled_fonts
from scripts.review_sources import parse_verse_key, read_review_text

# The read-92 recording used for the approved Al-Baqarah panels.
BAQARAH_AUDIO_SHA256 = "d390ae18bf320e6073e4736a72e8dd4a24ea363a80ab4696281e2b990d2aeb25"
REVIEWED_INTERVALS = {(2, 20): (273480, 307380), (2, 25): (409920, 459120),
                      (2, 35): (707420, 725980)}


def review(verse: tuple[int, int], output: Path, recording: Path | None = None) -> dict:
    if output.suffix.lower() != ".mp4":
        raise ValueError("Choose a new .mp4 output file.")
    if output.exists() or output.is_symlink():
        raise FileExistsError(output)
    if recording is not None and verse[0] != 2:
        raise ValueError("A local recording is supported only for the pinned read-92 Al-Baqarah MP3.")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".reel-review-", dir=output.parent) as stage:
        work = Path(stage)
        surah_ar, surah_en, arabic, english = read_review_text(verse, work)
        read = audio.fetch_timing_read_metadata()
        timing = audio.fetch_surah_timing(verse[0], audio_server=read.audio_server).get_ayah_timing(verse[1])
        interval = (timing.start_ms, timing.end_ms)
        if verse in REVIEWED_INTERVALS and interval != REVIEWED_INTERVALS[verse]:
            raise ValueError("Source timing differs from the reviewed read-92 interval.")
        fonts = work / "fonts"
        install_bundled_fonts(str(fonts))
        config = renderer.RenderConfig(fonts_dir=str(fonts), preset="ultrafast", subtitle_config=SubtitleConfig(
            arabic_font_size=160, translation_font_size=40, translation_max_lines=3,
            arabic_line_spacing=1.1, arabic_translation_gap_px=60))
        ayah = renderer.resolve_render_ayah_subtitle_plan(
            renderer.RenderAyah(*verse, arabic, english.text, english.footnotes, *interval, (english,)), config)
        if ayah.subtitle_mode != renderer.SUBTITLE_MODE_VERIFIED_PHRASE_PROGRESSIVE:
            raise ValueError("No source-verified English panel record for this verse.")

        if recording is None:
            recording = Path(audio.download_surah_audio(verse[0], target_dir=str(work), audio_server=read.audio_server))
        if verse[0] == 2:
            with recording.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != BAQARAH_AUDIO_SHA256:
                    raise ValueError("Recording differs from the reviewed read-92 Al-Baqarah MP3.")
        background = work / "synthetic.mp4"
        duration = timing.duration_seconds + 2
        subprocess.run([renderer.resolve_ffmpeg_binary(), "-nostdin", "-v", "error", "-f", "lavfi",
                        "-i", "color=c=0x18352f:s=1080x1920:r=30", "-t", str(duration),
                        "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
                        str(background)], check=True, timeout=180)
        staged = work / "review.mp4"
        request = renderer.RenderRequest(
            verse[0], surah_ar, surah_en, (ayah,), str(recording),
            (renderer.RenderBackground(str(background), duration, 0),), str(staged), seed=1)
        measured = renderer.preflight_subtitle_layout(request, config)
        result = renderer.render_reel(request, config)
        os.link(staged, output)  # Same filesystem, atomic, never overwrites another review.
        return {"verse": "%d:%d" % verse, "output": str(output), "duration_seconds": result.duration_seconds,
                "panels": len(measured.panels), "footnote_font_size": config.footnote_font_size,
                "background": "synthetic", "publication_approved": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verse", required=True, type=parse_verse_key)
    parser.add_argument("--output", type=Path, required=True, help="New local MP4 path.")
    parser.add_argument("--audio", type=Path, help="Optional pinned read-92 Al-Baqarah MP3.")
    args = parser.parse_args(argv)
    print(json.dumps(review(args.verse, args.output, args.audio), indent=2))


if __name__ == "__main__":
    main()
