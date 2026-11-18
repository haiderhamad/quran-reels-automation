"""Audit background assets under production visual configuration and composite safety scanning.

Evaluates whether the 12 source-verified background assets remain safe after full FFmpeg
rendering using authoritative Supabase app_config visual parameters (Arabic font 170,
English font 50, gaps, shadows, transitions, letterboxing) and 4 FPS visual safety scanning.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.background_pool import (
    BackgroundCandidate,
    VerifiedBackgroundRecord,
    make_provider,
)
from engine.backgrounds import download_background
from engine.job import build_render_config_from_stored_config
from engine.quran import QuranData
from engine.renderer import (
    RenderAyah,
    RenderBackground,
    RenderConfig,
    RenderRequest,
    RenderTranslation,
    render_reel,
    resolve_render_ayah_subtitle_plan,
)
from engine.safety import Detection, SafetyConfig, SafetyScanResult, scan_video
from engine.supabase_state import SupabaseConfig, SupabaseStateStore
from engine.translation import TranslationData
from scripts.build_verified_background_pool import auto_load_env_if_needed


PHASE_5_COHORT = (
    ("pexels", "12328908"),
    ("pexels", "7681529"),
    ("pexels", "7681534"),
    ("pexels", "32079443"),
    ("pexels", "15499818"),
    ("pexels", "33897599"),
    ("pexels", "39029761"),
    ("pexels", "7830744"),
    ("pixabay", "191093"),
    ("pixabay", "219843"),
    ("pixabay", "296183"),
    ("pixabay", "367134"),
)


@dataclass(frozen=True)
class DetectionSummary:
    detection_type: str
    confidence: float
    timestamp_seconds: float
    frame_index: int


@dataclass(frozen=True)
class AssetAuditResult:
    provider: str
    provider_asset_id: str
    provider_variant_id: str
    content_sha256: str
    duration_seconds: float
    width: int
    height: int
    classification: str  # PRODUCTION_CONFIG_PASS | REPRODUCIBLE_FAIL | NONDETERMINISTIC
    attempts: int
    run_1_verdict: str
    run_1_reason: Optional[str]
    run_1_frames: int
    run_2_verdict: Optional[str] = None
    run_2_reason: Optional[str] = None
    run_2_frames: Optional[int] = None
    detections: tuple[DetectionSummary, ...] = ()


def fetch_audit_cohort_from_supabase(
    store: SupabaseStateStore,
) -> list[dict[str, Any]]:
    """Fetch the exact 12 Phase 5 cohort assets from Supabase regardless of current status."""
    all_assets = store._request(
        "GET",
        "background_assets",
        params={"select": "*"},
    )
    asset_map = {(a["provider"], a["provider_asset_id"]): a for a in all_assets}
    cohort_rows = []
    for prov, aid in PHASE_5_COHORT:
        if (prov, aid) in asset_map:
            cohort_rows.append(asset_map[(prov, aid)])
        else:
            raise ValueError(f"Cohort asset {prov}:{aid} not found in Supabase background_assets!")
    return cohort_rows


def fetch_verified_assets_from_supabase(
    store: SupabaseStateStore,
) -> list[dict[str, Any]]:
    """Fetch verified background assets from Supabase."""
    return fetch_audit_cohort_from_supabase(store)


def run_composite_test_for_asset(
    asset_row: Mapping[str, Any],
    quran_data: QuranData,
    translation_data: TranslationData,
    render_config: RenderConfig,
    safety_config: Optional[SafetyConfig] = None,
    test_duration_cap_seconds: float = 15.0,
) -> tuple[bool, Optional[str], int, Sequence[Detection]]:
    """Download asset, render 2:21 composite video with production visual config, and scan."""
    provider_name = asset_row["provider"]
    asset_id = asset_row["provider_asset_id"]
    variant_id = asset_row["provider_variant_id"]
    expected_sha = asset_row["content_sha256"]

    provider = make_provider(provider_name)
    asset_info = provider.get_asset(asset_id)
    candidate = provider.resolve_variant(asset_info, variant_id)

    resolved_safety_config = safety_config or SafetyConfig(
        animal_detection_required=True,
    )

    with tempfile.TemporaryDirectory() as tmp_dir:
        downloaded = download_background(candidate, target_dir=tmp_dir)
        if downloaded.content_sha256 != expected_sha:
            raise ValueError(
                f"SHA mismatch for {provider_name}:{asset_id}: "
                f"expected {expected_sha}, downloaded {downloaded.content_sha256}"
            )

        q_ayah = quran_data.get_ayah(2, 21)
        tr_ayah = translation_data.get_ayah(2, 21)
        surah_obj = quran_data.get_surah(2)

        duration = min(candidate.duration_seconds, test_duration_cap_seconds)
        footnotes_str = (
            "; ".join(tr_ayah.footnotes)
            if getattr(tr_ayah, "footnotes", None) and isinstance(tr_ayah.footnotes, (list, tuple))
            else (tr_ayah.footnotes if isinstance(getattr(tr_ayah, "footnotes", None), str) else "")
        )
        ayah = RenderAyah(
            surah_number=2,
            ayah_number=21,
            arabic_text=q_ayah.arabic_text,
            english_text=tr_ayah.translation_text,
            footnotes=footnotes_str,
            start_ms=0,
            end_ms=int(duration * 1000),
            translations=(
                RenderTranslation(
                    "en",
                    tr_ayah.translation_text,
                    footnotes_str,
                    translation_data.source.translation_key,
                    translation_data.source.version,
                ),
            ),
        )
        resolved_ayah = resolve_render_ayah_subtitle_plan(ayah, render_config)

        # Generate silent audio track for subtitle timing
        dummy_audio = os.path.join(tmp_dir, "dummy_audio.aac")
        cmd = [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=44100:cl=stereo",
            "-t",
            str(resolved_ayah.duration_seconds),
            "-c:a",
            "aac",
            dummy_audio,
        ]
        subprocess.run(cmd, capture_output=True, check=True)

        output_mp4 = os.path.join(tmp_dir, "composite_render.mp4")
        req = RenderRequest(
            surah_number=2,
            arabic_surah_name=surah_obj.arabic_name,
            english_surah_name=surah_obj.english_name,
            ayahs=(resolved_ayah,),
            surah_audio_path=dummy_audio,
            backgrounds=(
                RenderBackground(
                    local_path=downloaded.local_path,
                    source_duration_seconds=candidate.duration_seconds,
                    pexels_video_id=candidate.pexels_video_id or 0,
                ),
            ),
            output_path=output_mp4,
            seed=0,
        )

        render_reel(req, config=render_config)
        scan_res = scan_video(output_mp4, config=resolved_safety_config)

        all_detections = []
        all_detections.extend(scan_res.person_detections)
        all_detections.extend(scan_res.face_detections)
        all_detections.extend(scan_res.animal_detections)

        return (
            scan_res.safe,
            scan_res.failure_reason,
            scan_res.frames_scanned,
            all_detections,
        )


def audit_background_pool_render_robustness(
    store: Optional[SupabaseStateStore] = None,
    quran_data: Optional[QuranData] = None,
    translation_data: Optional[TranslationData] = None,
    render_config: Optional[RenderConfig] = None,
    safety_config: Optional[SafetyConfig] = None,
    test_duration_cap_seconds: float = 15.0,
) -> list[AssetAuditResult]:
    """Audit the 12 background assets using authoritative production visual config."""
    supabase_store = store or SupabaseStateStore(SupabaseConfig.from_env())

    cohort_rows = fetch_audit_cohort_from_supabase(supabase_store)
    print(f"Loaded {len(cohort_rows)} cohort assets from Supabase.")

    if render_config is not None:
        resolved_render_config = render_config
    else:
        stored_config = supabase_store.load_config()
        resolved_render_config = build_render_config_from_stored_config(stored_config)

    sub = resolved_render_config.subtitle_config
    print("Effective Production Visual Config:")
    print(f"  arabic_font_size: {sub.arabic_font_size}")
    print(f"  translation_font_size: {sub.translation_font_size}")
    print(f"  arabic_translation_gap_px: {sub.arabic_translation_gap_px}")
    print(f"  translation_stack_gap_px: {sub.translation_stack_gap_px}")
    print(f"  transition_duration_ms: {sub.transition_duration_ms}")
    print(f"  subtitle_lead_ms: {sub.subtitle_lead_ms}")
    print(f"  text_outline: {sub.text_outline}")
    print(f"  text_shadow: {sub.text_shadow}")
    print(f"  header_style: {sub.header_style}")
    print(f"  subtitle_backing: {sub.subtitle_backing}")

    resolved_quran = quran_data
    if resolved_quran is None:
        from engine.quran import fetch_quran_data
        resolved_quran = fetch_quran_data()

    resolved_translation = translation_data
    if resolved_translation is None:
        from engine.translation import fetch_translation_data_for_language
        resolved_translation = fetch_translation_data_for_language("en")

    results: list[AssetAuditResult] = []

    for idx, asset_row in enumerate(cohort_rows, 1):
        provider = asset_row["provider"]
        asset_id = asset_row["provider_asset_id"]
        variant_id = asset_row["provider_variant_id"]
        sha = asset_row["content_sha256"]
        val = asset_row.get("value", {}) or {}
        duration = float(val.get("duration_seconds") or 0.0)
        width = int(val.get("width") or val.get("file_width") or 1080)
        height = int(val.get("height") or val.get("file_height") or 1920)

        print(
            f"[{idx}/{len(cohort_rows)}] Auditing {provider}:{asset_id} (variant={variant_id})...",
            flush=True,
        )

        # Attempt 1
        safe_1, reason_1, frames_1, dets_1 = run_composite_test_for_asset(
            asset_row=asset_row,
            quran_data=resolved_quran,
            translation_data=resolved_translation,
            render_config=resolved_render_config,
            safety_config=safety_config,
            test_duration_cap_seconds=test_duration_cap_seconds,
        )

        if safe_1:
            print(f"  -> Run 1: PASS ({frames_1} frames)", flush=True)
            results.append(
                AssetAuditResult(
                    provider=provider,
                    provider_asset_id=asset_id,
                    provider_variant_id=variant_id,
                    content_sha256=sha,
                    duration_seconds=duration,
                    width=width,
                    height=height,
                    classification="PRODUCTION_CONFIG_PASS",
                    attempts=1,
                    run_1_verdict="SAFE",
                    run_1_reason=None,
                    run_1_frames=frames_1,
                )
            )
        else:
            print(
                f"  -> Run 1: FAIL (reason: {reason_1}) - running repeatability check...",
                flush=True,
            )
            # Repeatability check (Attempt 2)
            safe_2, reason_2, frames_2, dets_2 = run_composite_test_for_asset(
                asset_row=asset_row,
                quran_data=resolved_quran,
                translation_data=resolved_translation,
                render_config=resolved_render_config,
                safety_config=safety_config,
                test_duration_cap_seconds=test_duration_cap_seconds,
            )

            det_summaries = tuple(
                DetectionSummary(
                    detection_type=d.detection_type,
                    confidence=float(d.confidence),
                    timestamp_seconds=float(d.timestamp_seconds),
                    frame_index=int(d.frame_index),
                )
                for d in (dets_2 if not safe_2 else dets_1)
            )

            if not safe_2:
                cls_name = (
                    "REPRODUCIBLE_SAME_SIGNATURE"
                    if reason_1 == reason_2
                    else "REPEATED_REJECTION_DIFFERENT_SIGNATURE"
                )
                print(f"  -> Run 2: FAIL (reason: {reason_2}) -> {cls_name}", flush=True)
                results.append(
                    AssetAuditResult(
                        provider=provider,
                        provider_asset_id=asset_id,
                        provider_variant_id=variant_id,
                        content_sha256=sha,
                        duration_seconds=duration,
                        width=width,
                        height=height,
                        classification=cls_name,
                        attempts=2,
                        run_1_verdict="REJECT",
                        run_1_reason=reason_1,
                        run_1_frames=frames_1,
                        run_2_verdict="REJECT",
                        run_2_reason=reason_2,
                        run_2_frames=frames_2,
                        detections=det_summaries,
                    )
                )
            else:
                print(f"  -> Run 2: PASS -> NONDETERMINISTIC_INVESTIGATE", flush=True)
                results.append(
                    AssetAuditResult(
                        provider=provider,
                        provider_asset_id=asset_id,
                        provider_variant_id=variant_id,
                        content_sha256=sha,
                        duration_seconds=duration,
                        width=width,
                        height=height,
                        classification="NONDETERMINISTIC_INVESTIGATE",
                        attempts=2,
                        run_1_verdict="REJECT",
                        run_1_reason=reason_1,
                        run_1_frames=frames_1,
                        run_2_verdict="SAFE",
                        run_2_reason=None,
                        run_2_frames=frames_2,
                        detections=det_summaries,
                    )
                )

    return results


def main() -> None:
    auto_load_env_if_needed()
    parser = argparse.ArgumentParser(
        description="Audit background pool render robustness using production app_config."
    )
    parser.add_argument(
        "--output-json",
        type=str,
        default=".local/audits/phase5c_production_config_render_robustness_audit.json",
        help="Path to output JSON audit report.",
    )
    args = parser.parse_args()

    results = audit_background_pool_render_robustness()

    pass_count = sum(1 for r in results if r.classification == "PRODUCTION_CONFIG_PASS")
    fail_count = sum(1 for r in results if r.classification == "REPRODUCIBLE_FAIL")
    nondet_count = sum(
        1 for r in results if r.classification == "NONDETERMINISTIC / INVESTIGATE"
    )

    print("\n================ AUDIT SUMMARY ================")
    print(f"Total cohort assets audited: {len(results)}")
    print(f"PRODUCTION_CONFIG_PASS: {pass_count}")
    print(f"REPRODUCIBLE_FAIL: {fail_count}")
    print(f"NONDETERMINISTIC: {nondet_count}")

    out_path = Path(args.output_json)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    report_dict = {
        "audited_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "total_audited": len(results),
        "pass_count": pass_count,
        "fail_count": fail_count,
        "nondeterministic_count": nondet_count,
        "results": [
            {
                "provider": r.provider,
                "provider_asset_id": r.provider_asset_id,
                "provider_variant_id": r.provider_variant_id,
                "content_sha256": r.content_sha256,
                "duration_seconds": r.duration_seconds,
                "dimensions": f"{r.width}x{r.height}",
                "classification": r.classification,
                "attempts": r.attempts,
                "run_1_verdict": r.run_1_verdict,
                "run_1_reason": r.run_1_reason,
                "run_1_frames": r.run_1_frames,
                "run_2_verdict": r.run_2_verdict,
                "run_2_reason": r.run_2_reason,
                "run_2_frames": r.run_2_frames,
                "detections": [
                    {
                        "type": d.detection_type,
                        "confidence": d.confidence,
                        "timestamp": d.timestamp_seconds,
                        "frame_index": d.frame_index,
                    }
                    for d in r.detections
                ],
            }
            for r in results
        ],
    }
    out_path.write_text(json.dumps(report_dict, indent=2), encoding="utf-8")
    print(f"Saved audit report to {out_path}")


if __name__ == "__main__":
    main()
