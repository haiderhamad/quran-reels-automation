"""Source-bound eligible fixtures for publication transport tests."""
from types import SimpleNamespace
from engine.alignments import normalized_sha256
from functools import lru_cache
from unittest.mock import patch


@lru_cache(maxsize=1)
def legacy_authorization_catalog():
    """Grant only the synthetic ranges exercised by the offline transport tests."""
    from engine.production_authorization import ProductionAuthorizationCatalog

    rows = []
    for surah, lower, upper in ((1, 1, 3), (2, 21, 21), (94, 1, 3)):
        for first in range(lower, upper + 1):
            for last in range(first, upper + 1):
                identity = len(rows) + 1
                rows.append({
                    "policy_version": "calm-inviting-v1",
                    "surah_number": surah,
                    "passage_first_ayah": first,
                    "passage_last_ayah": last,
                    "authorization_ids": [identity],
                    "approval_bases": ["LEGACY_CATALOG"],
                    "candidate_ayah_keys": [f"{surah}:{ayah}" for ayah in range(first, last + 1)],
                    "authorization_fingerprint": f"{identity:032x}",
                })
    return ProductionAuthorizationCatalog.from_rpc_rows('calm-inviting-v1', rows)


def mock_live_authorization(test_case):
    """Mock the external catalog read while retaining all actual safety checks."""
    catalog = legacy_authorization_catalog()
    from engine.supabase_state import SupabaseConfig
    config_patcher = patch('engine.supabase_state.SupabaseConfig.from_env',
        return_value=SupabaseConfig(url='https://fixture.supabase.co', service_role_key='fixture'))
    config_patcher.start()
    test_case.addCleanup(config_patcher.stop)
    source_patcher = patch('engine.reel_publication_policy._trusted_source_hashes',
        side_effect=fixture_source_hashes)
    source_patcher.start()
    test_case.addCleanup(source_patcher.stop)
    for target in ('engine.reel_publication_policy.load_production_authorization_catalog',
                   'engine.job.load_production_authorization_catalog'):
        patcher = patch(target, return_value=catalog)
        patcher.start()
        test_case.addCleanup(patcher.stop)
    return catalog


def authorized_run_request(*args, **kwargs):
    from engine.main import RunRequest
    kwargs.setdefault('production_authorization', legacy_authorization_catalog())
    return RunRequest(*args, **kwargs)


def authorized_run_job(*args, **kwargs):
    from engine.job import run_job
    kwargs.setdefault('production_authorization', legacy_authorization_catalog())
    return run_job(*args, **kwargs)

# Synthetic labels for the public tests. They are never loaded by the worker.
ARABIC = ("علامة تجريبية " * 9).strip()
ENGLISH = "Synthetic caption for this unit test."


def source_labels(surah, ayah):
    if (surah, ayah) == (2, 21):
        return ARABIC, ENGLISH
    if surah in (1, 94) and 1 <= ayah <= 4:
        return f"نموذج {surah} {ayah}", f"Sample {surah}, item {ayah}."
    raise ValueError(f"No fixture for {surah}:{ayah}")


def fixture_source_hashes(surah, ayah, language):
    arabic, english = source_labels(surah, ayah)
    translation = english if language == "en" else f"Deutsch: {english}"
    return normalized_sha256(arabic), normalized_sha256(translation)


def eligible_record_evidence(surah=2, first=21, last=21):
    texts = [source_labels(surah, ayah) for ayah in range(first, last + 1)]
    caption, subtitles = [], []
    for number, (arabic, english) in enumerate(texts, first):
        key = f'{surah}:{number}'
        ah, eh = normalized_sha256(arabic), normalized_sha256(english)
        caption.append({'verse_key': key, 'arabic_text': arabic, 'arabic_normalized_sha256': ah,
            'translations': {'en': {'text': english, 'normalized_sha256': eh,
                'translation_key': 'english_rwwad', 'translation_version': '1.0.19'}}})
        subtitles.append({'verse_key': key, 'arabic_source_hash': ah, 'english_source_hash': eh})
    return {'caption_ayahs': caption, 'subtitle_ayahs': subtitles,
        'production_authorization': legacy_authorization_catalog().require_range(surah, first, last).snapshot()}


def eligible_record():
    return SimpleNamespace(surah_number=2, first_ayah=21, last_ayah=21, run_kind='scheduled',
        surah_name_ar='البقرة', surah_name_en='Al-Baqarah', provenance=eligible_record_evidence())


def eligible_meta_request(**kwargs):
    from engine.publisher import PublishRequest, build_reel_caption_from_record
    record = eligible_record()
    kwargs['editorial_record'] = record
    kwargs['caption'] = build_reel_caption_from_record(record)
    return PublishRequest(**kwargs)


def eligible_buffer_request(*args, **kwargs):
    kwargs.update(dict(zip(("run_id", "caption", "public_media_url", "save_to_draft", "source"), args)))
    from engine.buffer_publisher import BufferPublishRequest, build_buffer_facebook_caption
    record = eligible_record()
    kwargs['editorial_record'] = record
    kwargs['caption'] = build_buffer_facebook_caption(record)
    return BufferPublishRequest(**kwargs)
