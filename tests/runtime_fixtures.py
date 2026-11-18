"""Generated input and detector fixtures for offline retry tests."""
from __future__ import annotations

from typing import Optional
from engine.audio import (
    DEFAULT_AUDIO_SERVER, DEFAULT_RECITER_NAME, DEFAULT_RIWAYA_NAME, DEFAULT_TIMING_READ_ID,
    AyahTiming, PreAyahSegment, SurahAudio, TimingReadMetadata, build_surah_audio_url,
)
from engine.backgrounds import BackgroundCandidate
from engine.quran import QuranAyah, QuranData, QuranSourceMetadata, QuranSurah, STANDARD_SURAH_AYAH_COUNTS, STANDARD_SURAH_METADATA
from tests.editorial_fixtures import source_labels
from engine.safety import SafetyScanResult
from engine.translation import TranslationAyah, TranslationData, TranslationSourceMetadata, TranslationSurah


def make_mock_quran_data(environment: str = "production") -> QuranData:
    """Create a synthetic QuranData fixture with 114 Surahs and 6236 Ayahs."""
    ayahs_94 = tuple(QuranAyah(6179 + i, 94, i, source_labels(94, i)[0]) for i in range(1, 5))
    ayahs_94 += tuple(QuranAyah(6179+i,94,i,f"نص 94:{i}") for i in range(5,9))
    surah_94 = QuranSurah(
        number=94,
        arabic_name="الشرح",
        english_name="Ash-Sharh",
        english_name_translation="The Relief",
        number_of_ayahs=8,
        ayahs=ayahs_94,
    )

    # Surah 1 with 7 ayahs for range tests
    ayahs_1 = tuple(
        QuranAyah(global_number=i, surah_number=1, ayah_number=i, arabic_text=f"آية {i}")
        for i in range(1, 8)
    )
    surah_1 = QuranSurah(
        number=1,
        arabic_name="الفاتحة",
        english_name="Al-Fatihah",
        english_name_translation="The Opening",
        number_of_ayahs=7,
        ayahs=ayahs_1,
    )

    all_surahs = []
    global_id = 1
    for s_num in range(1, 115):
        count = STANDARD_SURAH_AYAH_COUNTS[s_num]
        if s_num == 1:
            all_surahs.append(surah_1)
        elif s_num == 94:
            all_surahs.append(surah_94)
        else:
            ayahs = tuple(
                QuranAyah(global_id + i, s_num, i + 1, f"نص {s_num}:{i + 1}")
                for i in range(count)
            )
            arabic_name, english_name, english_trans = STANDARD_SURAH_METADATA.get(
                s_num, (f"سورة {s_num}", f"Surah {s_num}", None)
            )
            all_surahs.append(
                QuranSurah(
                    number=s_num,
                    arabic_name=arabic_name,
                    english_name=english_name,
                    english_name_translation=english_trans,
                    number_of_ayahs=count,
                    ayahs=ayahs,
                )
            )
        global_id += count

    return QuranData(
        surahs=tuple(all_surahs),
        source=QuranSourceMetadata(
            "quran_foundation",
            "https://apis.quran.foundation",
            environment,
            "text_uthmani",
            "text_uthmani",
        ),
    )


def make_mock_timing_read() -> TimingReadMetadata:
    return TimingReadMetadata(
        read_id=DEFAULT_TIMING_READ_ID,
        reciter_name=DEFAULT_RECITER_NAME,
        riwaya=DEFAULT_RIWAYA_NAME,
        audio_server=DEFAULT_AUDIO_SERVER,
        surah_count=114,
    )


def make_mock_translation_data(with_footnotes_on_94_2: bool = False) -> TranslationData:
    """Create a minimal mock TranslationData matching Surah 94."""
    fn_94_2 = "Some footnote explanation" if with_footnotes_on_94_2 else ""
    ayahs_94 = tuple(TranslationAyah(94, i, source_labels(94, i)[1], fn_94_2 if i == 2 else "") for i in range(1, 5))
    ayahs_94 += tuple(TranslationAyah(94,i,f"Translation {i}","") for i in range(5,9))
    surah_94 = TranslationSurah(
        number=94,
        number_of_ayahs=8,
        ayahs=ayahs_94,
    )

    ayahs_1 = tuple(
        TranslationAyah(surah_number=1, ayah_number=i, english_text=f"Ayah translation {i}", footnotes="")
        for i in range(1, 8)
    )
    surah_1 = TranslationSurah(1, 7, ayahs_1)

    all_surahs = [surah_1] + [
        TranslationSurah(i, 1, (TranslationAyah(i, 1, "Text", ""),))
        for i in range(2, 94)
    ] + [surah_94] + [
        TranslationSurah(i, 1, (TranslationAyah(i, 1, "Text", ""),))
        for i in range(95, 115)
    ]
    meta = TranslationSourceMetadata(
        provider="quranenc",
        api_base_url="https://quranenc.com/api/v1",
        translation_key="english_rwwad",
        language_iso_code="en",
        version="1.0.19",
        last_update="1234567890",
        title="Rowwad Translation Center",
        description="English Translation of the meanings of the Noble Quran",
    )
    return TranslationData(
        source=meta,
        surahs=tuple(all_surahs),
    )


def make_mock_surah_audio_94() -> SurahAudio:
    """Create a synthetic timing mock for Surah 94 with ~16.5s total duration for ayahs 1..3."""
    timings = (
        AyahTiming(surah_number=94, ayah_number=1, start_ms=3500, end_ms=9000),    # 5.5s
        AyahTiming(surah_number=94, ayah_number=2, start_ms=9000, end_ms=14500),   # 5.5s
        AyahTiming(surah_number=94, ayah_number=3, start_ms=14500, end_ms=20000),  # 5.5s  -> Total 1..3 = 16.5s
        AyahTiming(surah_number=94, ayah_number=4, start_ms=20000, end_ms=25000),  # 5.0s
    )
    return SurahAudio(
        surah_number=94,
        audio_url=build_surah_audio_url(94),
        timings=timings,
        pre_ayah_segment=PreAyahSegment(94, 0, 3500),
    )


def make_mock_candidate(
    video_id: int,
    duration_seconds: float = 15.0,
    requires_center_crop: bool = False,
) -> BackgroundCandidate:
    return BackgroundCandidate(
        pexels_video_id=video_id,
        query="nature",
        pexels_page_url=f"https://www.pexels.com/video/{video_id}/",
        videographer_name="Nature Videographer",
        videographer_url="https://www.pexels.com/@nature",
        duration_seconds=duration_seconds,
        source_width=1080,
        source_height=1920,
        video_file_id=video_id * 10,
        file_url=f"https://pexels.com/v/{video_id}.mp4",
        file_type="video/mp4",
        fps=30.0,
        file_width=1080,
        file_height=1920,
        requires_center_crop=requires_center_crop,
    )


def make_mock_safety_result(safe: bool = True, failure_reason: Optional[str] = None) -> SafetyScanResult:
    return SafetyScanResult(
        safe=safe,
        frames_scanned=10,
        duration_seconds=15.0,
        person_detections=(),
        face_detections=(),
        failure_reason=failure_reason,
    )
