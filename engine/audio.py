"""Authentic recitation audio and ayah timing module for MP3Quran (Yasser Al-Dosari)."""

from __future__ import annotations

import json
import os
import socket
import ssl
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional, Sequence, Union

from engine.verses import AyahTiming as VerseAyahTiming


DEFAULT_API_BASE_URL = "https://mp3quran.net/api/v3"
DEFAULT_RECITER_ID = 92
DEFAULT_RECITER_NAME = "ياسر الدوسري"
DEFAULT_RIWAYA_NAME = "حفص عن عاصم"
DEFAULT_TIMING_READ_ID = 92
DEFAULT_AUDIO_SERVER = "https://server11.mp3quran.net/yasser/"
RECITER_REUSE_RIGHTS_STATUS = "unverified"

EXPECTED_SURAH_COUNT = 114
EXPECTED_TOTAL_AYAH_COUNT = 6236
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_USER_AGENT = "QuranReelsAutomation/1.0"

# Standard Hafs ayah counts per Surah (1..114)
STANDARD_SURAH_AYAH_COUNTS: dict[int, int] = {
    1: 7, 2: 286, 3: 200, 4: 176, 5: 120, 6: 165, 7: 206, 8: 75, 9: 129, 10: 109,
    11: 123, 12: 111, 13: 43, 14: 52, 15: 99, 16: 128, 17: 111, 18: 110, 19: 98, 20: 135,
    21: 112, 22: 78, 23: 118, 24: 64, 25: 77, 26: 227, 27: 93, 28: 88, 29: 69, 30: 60,
    31: 34, 32: 30, 33: 73, 34: 54, 35: 45, 36: 83, 37: 182, 38: 88, 39: 75, 40: 85,
    41: 54, 42: 53, 43: 89, 44: 59, 45: 37, 46: 35, 47: 38, 48: 29, 49: 18, 50: 45,
    51: 60, 52: 49, 53: 62, 54: 55, 55: 78, 56: 96, 57: 29, 58: 22, 59: 24, 60: 13,
    61: 14, 62: 11, 63: 11, 64: 18, 65: 12, 66: 12, 67: 30, 68: 52, 69: 52, 70: 44,
    71: 28, 72: 28, 73: 20, 74: 56, 75: 40, 76: 31, 77: 50, 78: 40, 79: 46, 80: 42,
    81: 29, 82: 19, 83: 36, 84: 25, 85: 22, 86: 17, 87: 19, 88: 26, 89: 30, 90: 20,
    91: 15, 92: 21, 93: 11, 94: 8, 95: 8, 96: 19, 97: 5, 98: 8, 99: 8, 100: 11,
    101: 11, 102: 8, 103: 3, 104: 9, 105: 5, 106: 4, 107: 7, 108: 3, 109: 6, 110: 3,
    111: 5, 112: 4, 113: 5, 114: 6,
}


class AudioSourceError(ValueError):
    """Raised when recitation audio or timing data fetching, parsing, or validation fails."""


@dataclass(frozen=True)
class ReciterMetadata:
    """Provenance and discovery metadata for an MP3Quran reciter and recitation."""

    provider: str
    api_base_url: str
    reciter_id: int
    reciter_name: str
    riwaya: str
    moshaf_id: int
    timing_read_id: int
    audio_server: str
    available_surahs: tuple[int, ...]
    reuse_rights_status: str = RECITER_REUSE_RIGHTS_STATUS


@dataclass(frozen=True)
class TimingReadMetadata:
    """Identity advertised by MP3Quran for one timing/read resource."""

    read_id: int
    reciter_name: str
    riwaya: str
    audio_server: str
    surah_count: int


@dataclass(frozen=True)
class AyahTiming:
    """Authentic recitation timing boundary for a single canonical ayah in milliseconds."""

    surah_number: int
    ayah_number: int
    start_ms: int
    end_ms: int

    @property
    def start_time_seconds(self) -> float:
        return self.start_ms / 1000.0

    @property
    def end_time_seconds(self) -> float:
        return self.end_ms / 1000.0

    @property
    def duration_seconds(self) -> float:
        return (self.end_ms - self.start_ms) / 1000.0


@dataclass(frozen=True)
class PreAyahSegment:
    """Pre-ayah audio segment (such as ayah=0 or intro/basmala) in milliseconds."""

    surah_number: int
    start_ms: int
    end_ms: int

    @property
    def start_time_seconds(self) -> float:
        return self.start_ms / 1000.0

    @property
    def end_time_seconds(self) -> float:
        return self.end_ms / 1000.0

    @property
    def duration_seconds(self) -> float:
        return (self.end_ms - self.start_ms) / 1000.0


@dataclass(frozen=True)
class SurahAudio:
    """Validated audio file URL and authentic ayah timings for a complete Surah."""

    surah_number: int
    audio_url: str
    timings: tuple[AyahTiming, ...]
    pre_ayah_segment: Optional[PreAyahSegment] = None

    @property
    def number_of_ayahs(self) -> int:
        return len(self.timings)

    def get_ayah_timing(self, ayah_number: int) -> AyahTiming:
        if (
            not isinstance(ayah_number, int)
            or isinstance(ayah_number, bool)
            or ayah_number < 1
            or ayah_number > self.number_of_ayahs
        ):
            raise AudioSourceError(
                f"Ayah number {ayah_number} is out of range for Surah {self.surah_number} (1..{self.number_of_ayahs})."
            )
        return self.timings[ayah_number - 1]

    def get_ayah_timing_range(
        self,
        first_ayah: int,
        last_ayah: int,
    ) -> tuple[AyahTiming, ...]:
        if (
            not isinstance(first_ayah, int)
            or isinstance(first_ayah, bool)
            or not isinstance(last_ayah, int)
            or isinstance(last_ayah, bool)
        ):
            raise AudioSourceError("Ayah numbers must be integers.")
        if first_ayah < 1 or last_ayah > self.number_of_ayahs or first_ayah > last_ayah:
            raise AudioSourceError(
                f"Invalid ayah timing range {first_ayah}..{last_ayah} for Surah {self.surah_number} (1..{self.number_of_ayahs})."
            )
        return self.timings[first_ayah - 1 : last_ayah]


def to_engine_verse_timing(
    audio_timing: AyahTiming,
    arabic_text: str,
    english_text: str,
) -> VerseAyahTiming:
    """Convert an authentic audio timing record into an engine.verses AyahTiming record."""
    if not isinstance(audio_timing, AyahTiming):
        raise AudioSourceError("audio_timing must be an AyahTiming instance.")
    if not isinstance(arabic_text, str) or not arabic_text.strip():
        raise AudioSourceError("arabic_text must be a non-empty string.")
    if not isinstance(english_text, str) or not english_text.strip():
        raise AudioSourceError("english_text must be a non-empty string.")

    return VerseAyahTiming(
        surah=audio_timing.surah_number,
        ayah=audio_timing.ayah_number,
        start_time=audio_timing.start_time_seconds,
        end_time=audio_timing.end_time_seconds,
        arabic_text=arabic_text,
        english_text=english_text,
    )


def build_surah_audio_url(
    surah_number: int,
    audio_server: str = DEFAULT_AUDIO_SERVER,
    available_surahs: Optional[Sequence[int]] = None,
) -> str:
    """Construct a deterministic, validated HTTPS audio URL for a Surah."""
    if (
        not isinstance(surah_number, int)
        or isinstance(surah_number, bool)
        or surah_number < 1
        or surah_number > EXPECTED_SURAH_COUNT
    ):
        raise AudioSourceError(
            f"Surah number {surah_number} is out of range (1..{EXPECTED_SURAH_COUNT})."
        )

    if not isinstance(audio_server, str) or not audio_server.startswith("https://"):
        raise AudioSourceError("Audio server URL must use secure HTTPS scheme.")

    if available_surahs is not None and surah_number not in available_surahs:
        raise AudioSourceError(
            f"Surah {surah_number} is not in the reciter's available surah list."
        )

    return f"{audio_server.rstrip('/')}/{surah_number:03d}.mp3"


def parse_reciter_metadata(
    reciters_payload: Union[str, bytes, dict],
    target_reciter_name: str = DEFAULT_RECITER_NAME,
    target_riwaya: str = DEFAULT_RIWAYA_NAME,
    timing_read_id: int = DEFAULT_TIMING_READ_ID,
    api_base_url: str = DEFAULT_API_BASE_URL,
) -> ReciterMetadata:
    """Parse and validate MP3Quran reciters payload for the required reciter and riwaya."""
    if isinstance(reciters_payload, (str, bytes)):
        try:
            payload = json.loads(reciters_payload)
        except Exception as err:
            raise AudioSourceError(f"Malformed JSON in reciters payload: {err}") from err
    elif isinstance(reciters_payload, dict):
        payload = reciters_payload
    else:
        raise AudioSourceError("Reciters payload must be a JSON string, bytes, or dict.")

    if not isinstance(payload, dict):
        raise AudioSourceError("Reciters payload top-level element must be an object.")

    reciters_list = payload.get("reciters")
    if not isinstance(reciters_list, list):
        raise AudioSourceError("Missing or invalid 'reciters' list in payload.")

    matched_reciter: Optional[dict] = None
    for reciter in reciters_list:
        if isinstance(reciter, dict):
            name = str(reciter.get("name", ""))
            if target_reciter_name in name:
                matched_reciter = reciter
                break

    if matched_reciter is None:
        raise AudioSourceError(
            f"Required reciter '{target_reciter_name}' not found in MP3Quran reciters list."
        )

    reciter_id = matched_reciter.get("id")
    if not isinstance(reciter_id, int) or isinstance(reciter_id, bool):
        raise AudioSourceError("Invalid or missing reciter ID.")

    moshaf_list = matched_reciter.get("moshaf")
    if not isinstance(moshaf_list, list) or not moshaf_list:
        raise AudioSourceError(f"No moshaf entries available for reciter '{target_reciter_name}'.")

    matched_moshaf: Optional[dict] = None
    for moshaf in moshaf_list:
        if isinstance(moshaf, dict):
            moshaf_name = str(moshaf.get("name", ""))
            if target_riwaya in moshaf_name:
                matched_moshaf = moshaf
                break

    if matched_moshaf is None:
        raise AudioSourceError(
            f"Required riwaya '{target_riwaya}' not found for reciter '{target_reciter_name}'."
        )

    moshaf_id = matched_moshaf.get("id")
    if not isinstance(moshaf_id, int) or isinstance(moshaf_id, bool):
        raise AudioSourceError("Invalid or missing moshaf ID.")

    server_url = matched_moshaf.get("server")
    if not isinstance(server_url, str) or not server_url.startswith("https://"):
        raise AudioSourceError("Moshaf server URL must be a valid HTTPS string.")

    surah_list_raw = matched_moshaf.get("surah_list")
    if not isinstance(surah_list_raw, str) or not surah_list_raw.strip():
        raise AudioSourceError("Missing or empty 'surah_list' in moshaf entry.")

    try:
        available_surahs = tuple(int(s.strip()) for s in surah_list_raw.split(",") if s.strip())
    except ValueError as err:
        raise AudioSourceError(f"Malformed 'surah_list' integer sequence: {err}") from err

    reported_surah_total = matched_moshaf.get("surah_total")
    if reported_surah_total is not None and reported_surah_total != len(available_surahs):
        raise AudioSourceError(
            f"Moshaf surah_total ({reported_surah_total}) does not match surah_list length ({len(available_surahs)})."
        )

    return ReciterMetadata(
        provider="MP3Quran",
        api_base_url=api_base_url,
        reciter_id=reciter_id,
        reciter_name=str(matched_reciter.get("name", target_reciter_name)),
        riwaya=str(matched_moshaf.get("name", target_riwaya)),
        moshaf_id=moshaf_id,
        timing_read_id=timing_read_id,
        audio_server=server_url,
        available_surahs=available_surahs,
        reuse_rights_status=RECITER_REUSE_RIGHTS_STATUS,
    )


def parse_timing_reads_payload(
    raw_payload: Union[str, bytes, Sequence, dict],
    *,
    expected_read_id: int = DEFAULT_TIMING_READ_ID,
    expected_reciter_name: str = DEFAULT_RECITER_NAME,
    expected_riwaya: str = DEFAULT_RIWAYA_NAME,
) -> TimingReadMetadata:
    """Resolve and validate the exact MP3Quran timing/read identity.

    The timing provider publishes the recording folder on the read record.  We
    use that same folder for the MP3 download so timing and audio cannot drift
    to independently configured resources.
    """
    if isinstance(raw_payload, (str, bytes)):
        try:
            payload = json.loads(raw_payload)
        except Exception as err:
            raise AudioSourceError(f"Malformed JSON in timing reads payload: {err}") from err
    else:
        payload = raw_payload

    if isinstance(payload, dict):
        reads = payload.get("data") or payload.get("result") or payload.get("reads")
    else:
        reads = payload
    if not isinstance(reads, Sequence) or isinstance(reads, (str, bytes)):
        raise AudioSourceError("Timing reads payload must contain a JSON array.")

    matched: Optional[dict] = None
    for item in reads:
        if not isinstance(item, dict):
            continue
        try:
            read_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        if read_id == expected_read_id:
            matched = item
            break

    if matched is None:
        raise AudioSourceError(f"Timing read {expected_read_id} is absent from the provider catalog.")

    reciter_name = str(matched.get("name", "")).strip()
    riwaya = str(matched.get("rewaya", matched.get("riwaya", ""))).strip()
    audio_server = str(matched.get("folder_url", matched.get("server", ""))).strip()
    try:
        surah_count = int(matched.get("soar_count", matched.get("surah_count")))
    except (TypeError, ValueError) as err:
        raise AudioSourceError("Timing read has an invalid or missing Surah count.") from err

    if expected_reciter_name not in reciter_name:
        raise AudioSourceError(
            f"Timing read {expected_read_id} belongs to '{reciter_name}', not '{expected_reciter_name}'."
        )
    if expected_riwaya not in riwaya:
        raise AudioSourceError(
            f"Timing read {expected_read_id} uses '{riwaya}', not '{expected_riwaya}'."
        )
    if not audio_server.startswith("https://"):
        raise AudioSourceError("Timing read audio folder must use secure HTTPS.")
    if surah_count != EXPECTED_SURAH_COUNT:
        raise AudioSourceError(
            f"Timing read {expected_read_id} covers {surah_count} Surahs, expected {EXPECTED_SURAH_COUNT}."
        )

    return TimingReadMetadata(
        read_id=expected_read_id,
        reciter_name=reciter_name,
        riwaya=riwaya,
        audio_server=f"{audio_server.rstrip('/')}/",
        surah_count=surah_count,
    )


def parse_surah_timing_payload(
    raw_payload: Union[str, bytes, Sequence, dict],
    expected_surah: int,
    audio_server: str = DEFAULT_AUDIO_SERVER,
) -> SurahAudio:
    """Parse and strictly validate MP3Quran authentic ayah timing for a single Surah."""
    if (
        not isinstance(expected_surah, int)
        or isinstance(expected_surah, bool)
        or expected_surah < 1
        or expected_surah > EXPECTED_SURAH_COUNT
    ):
        raise AudioSourceError(
            f"Expected Surah number {expected_surah} is out of range (1..{EXPECTED_SURAH_COUNT})."
        )

    if isinstance(raw_payload, (str, bytes)):
        try:
            payload = json.loads(raw_payload)
        except Exception as err:
            raise AudioSourceError(f"Malformed JSON in timing payload: {err}") from err
    else:
        payload = raw_payload

    if isinstance(payload, dict):
        timing_list = payload.get("data") or payload.get("result") or payload.get("ayahs")
        if not isinstance(timing_list, list):
            raise AudioSourceError("Timing payload object missing timing list.")
    elif isinstance(payload, list):
        timing_list = payload
    else:
        raise AudioSourceError("Timing payload must be a JSON string, list, or dict.")

    expected_ayah_count = STANDARD_SURAH_AYAH_COUNTS[expected_surah]

    pre_ayah_segment: Optional[PreAyahSegment] = None
    canonical_items: list[dict] = []

    for item in timing_list:
        if not isinstance(item, dict):
            raise AudioSourceError(f"Timing entry in Surah {expected_surah} must be an object.")

        ayah_val = item.get("ayah")
        try:
            ayah_num = int(ayah_val)
        except (TypeError, ValueError) as err:
            raise AudioSourceError(
                f"Surah {expected_surah} has non-integer ayah identifier: {ayah_val}"
            ) from err

        if ayah_num == 0:
            start_ms = item.get("start_time")
            end_ms = item.get("end_time")
            try:
                start_ms_int = int(start_ms)
                end_ms_int = int(end_ms)
            except (TypeError, ValueError) as err:
                raise AudioSourceError(
                    f"Surah {expected_surah} ayah=0 has invalid timing values: {start_ms}, {end_ms}"
                ) from err
            if start_ms_int < 0 or end_ms_int <= start_ms_int:
                raise AudioSourceError(
                    f"Surah {expected_surah} ayah=0 timing invalid: {start_ms_int}..{end_ms_int}"
                )
            pre_ayah_segment = PreAyahSegment(
                surah_number=expected_surah,
                start_ms=start_ms_int,
                end_ms=end_ms_int,
            )
        else:
            canonical_items.append(item)

    if len(canonical_items) != expected_ayah_count:
        raise AudioSourceError(
            f"Surah {expected_surah} contains {len(canonical_items)} canonical timing items, expected {expected_ayah_count}."
        )

    parsed_timings: list[AyahTiming] = []
    previous_timing: Optional[AyahTiming] = None

    for ayah_idx, item in enumerate(canonical_items):
        expected_ayah_num = ayah_idx + 1
        ayah_num = int(item["ayah"])

        if ayah_num != expected_ayah_num:
            raise AudioSourceError(
                f"Surah {expected_surah} ayah number mismatch: expected {expected_ayah_num}, got {ayah_num}."
            )

        start_val = item.get("start_time")
        end_val = item.get("end_time")

        try:
            start_ms = int(start_val)
            end_ms = int(end_val)
        except (TypeError, ValueError) as err:
            raise AudioSourceError(
                f"Surah {expected_surah} ayah {expected_ayah_num} has non-integer timing: {start_val}..{end_val}"
            ) from err

        if start_ms < 0:
            raise AudioSourceError(
                f"Surah {expected_surah} ayah {expected_ayah_num} has negative start_time: {start_ms}"
            )

        if end_ms <= start_ms:
            raise AudioSourceError(
                f"Surah {expected_surah} ayah {expected_ayah_num} has invalid non-positive duration: {start_ms}..{end_ms}"
            )

        if previous_timing is not None:
            if start_ms < previous_timing.start_ms:
                raise AudioSourceError(
                    f"Surah {expected_surah} ayah {expected_ayah_num} start_time ({start_ms}) precedes previous start_time ({previous_timing.start_ms})."
                )
            if start_ms < previous_timing.end_ms:
                raise AudioSourceError(
                    f"Surah {expected_surah} ayah {expected_ayah_num} overlaps previous ayah: {start_ms} < {previous_timing.end_ms}."
                )

        current_timing = AyahTiming(
            surah_number=expected_surah,
            ayah_number=expected_ayah_num,
            start_ms=start_ms,
            end_ms=end_ms,
        )
        parsed_timings.append(current_timing)
        previous_timing = current_timing

    audio_url = build_surah_audio_url(expected_surah, audio_server=audio_server)

    return SurahAudio(
        surah_number=expected_surah,
        audio_url=audio_url,
        timings=tuple(parsed_timings),
        pre_ayah_segment=pre_ayah_segment,
    )


def _build_ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _fetch_url_json(
    url: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    user_agent: str = DEFAULT_USER_AGENT,
    max_retries: int = 1,
) -> bytes:
    if not isinstance(url, str) or not url.startswith("https://"):
        raise AudioSourceError("API request URL must use secure HTTPS scheme.")

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "application/json",
        },
    )
    ssl_context = _build_ssl_context()
    attempts = max(1, max_retries + 1)
    last_error: Optional[Exception] = None

    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ssl_context) as resp:
                status_code = getattr(resp, "status", None) or resp.getcode()
                if status_code != 200:
                    raise AudioSourceError(f"HTTP request returned status {status_code}.")
                return resp.read()
        except AudioSourceError:
            raise
        except urllib.error.HTTPError as err:
            last_error = err
            if err.code < 500 or attempt == attempts - 1:
                raise AudioSourceError(f"HTTP error {err.code}: {err.reason}") from err
        except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError, ssl.SSLError) as err:
            last_error = err
            if attempt == attempts - 1:
                raise AudioSourceError(f"Network error while fetching audio/timing data: {err}") from err
        except Exception as err:
            raise AudioSourceError(f"Unexpected error while fetching audio/timing data: {err}") from err

        time.sleep(0.5)

    raise AudioSourceError(f"Failed to fetch data from {url}: {last_error}")


def fetch_reciter_metadata(
    api_base_url: str = DEFAULT_API_BASE_URL,
    target_reciter_name: str = DEFAULT_RECITER_NAME,
    target_riwaya: str = DEFAULT_RIWAYA_NAME,
    timing_read_id: int = DEFAULT_TIMING_READ_ID,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    user_agent: str = DEFAULT_USER_AGENT,
    max_retries: int = 1,
) -> ReciterMetadata:
    """Fetch official reciter list and return verified metadata for Yasser Al-Dosari."""
    url = f"{api_base_url.rstrip('/')}/reciters?language=ar"
    raw_body = _fetch_url_json(url, timeout=timeout, user_agent=user_agent, max_retries=max_retries)
    return parse_reciter_metadata(
        raw_body,
        target_reciter_name=target_reciter_name,
        target_riwaya=target_riwaya,
        timing_read_id=timing_read_id,
        api_base_url=api_base_url,
    )


def fetch_timing_read_metadata(
    timing_read_id: int = DEFAULT_TIMING_READ_ID,
    api_base_url: str = DEFAULT_API_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    user_agent: str = DEFAULT_USER_AGENT,
    max_retries: int = 1,
) -> TimingReadMetadata:
    """Fetch and validate the provider identity for the selected timing read."""
    url = f"{api_base_url.rstrip('/')}/ayat_timing/reads"
    raw_body = _fetch_url_json(url, timeout=timeout, user_agent=user_agent, max_retries=max_retries)
    return parse_timing_reads_payload(raw_body, expected_read_id=timing_read_id)


def fetch_available_timing_soar(
    timing_read_id: int = DEFAULT_TIMING_READ_ID,
    api_base_url: str = DEFAULT_API_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    user_agent: str = DEFAULT_USER_AGENT,
    max_retries: int = 1,
) -> tuple[int, ...]:
    """Fetch list of Surahs with timing coverage for the specified read ID."""
    url = f"{api_base_url.rstrip('/')}/ayat_timing/soar?read={timing_read_id}"
    raw_body = _fetch_url_json(url, timeout=timeout, user_agent=user_agent, max_retries=max_retries)
    try:
        data = json.loads(raw_body)
    except Exception as err:
        raise AudioSourceError(f"Malformed JSON in timing soar response: {err}") from err

    if not isinstance(data, list):
        raise AudioSourceError("Timing soar response must be a JSON array.")

    surah_ids = []
    for item in data:
        if isinstance(item, dict) and "id" in item:
            try:
                surah_ids.append(int(item["id"]))
            except (TypeError, ValueError) as err:
                raise AudioSourceError(f"Malformed Surah id in timing soar list: {err}") from err

    return tuple(surah_ids)


def fetch_surah_timing(
    surah_number: int,
    timing_read_id: int = DEFAULT_TIMING_READ_ID,
    api_base_url: str = DEFAULT_API_BASE_URL,
    audio_server: str = DEFAULT_AUDIO_SERVER,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    user_agent: str = DEFAULT_USER_AGENT,
    max_retries: int = 1,
) -> SurahAudio:
    """Fetch authentic ayah timing for a single Surah from MP3Quran."""
    if (
        not isinstance(surah_number, int)
        or isinstance(surah_number, bool)
        or surah_number < 1
        or surah_number > EXPECTED_SURAH_COUNT
    ):
        raise AudioSourceError(
            f"Surah number {surah_number} is out of range (1..{EXPECTED_SURAH_COUNT})."
        )

    url = f"{api_base_url.rstrip('/')}/ayat_timing?surah={surah_number}&read={timing_read_id}"
    raw_body = _fetch_url_json(url, timeout=timeout, user_agent=user_agent, max_retries=max_retries)
    return parse_surah_timing_payload(
        raw_body,
        expected_surah=surah_number,
        audio_server=audio_server,
    )


def download_surah_audio(
    surah_number: int,
    target_dir: Optional[str] = None,
    audio_server: str = DEFAULT_AUDIO_SERVER,
    timeout: float = 60.0,
    user_agent: str = DEFAULT_USER_AGENT,
    buffer_size: int = 64 * 1024,
) -> str:
    """Stream and download Yasser Al-Dosari MP3 recitation audio for a single Surah to local disk.

    Returns local file path on success. Fails closed with AudioSourceError on failure.
    """
    if (
        not isinstance(surah_number, int)
        or isinstance(surah_number, bool)
        or surah_number < 1
        or surah_number > EXPECTED_SURAH_COUNT
    ):
        raise AudioSourceError(f"Surah number {surah_number} is out of range (1..{EXPECTED_SURAH_COUNT}).")

    url = build_surah_audio_url(surah_number, audio_server=audio_server)
    dest_dir = target_dir or tempfile.gettempdir()
    os.makedirs(dest_dir, exist_ok=True)
    filename = f"yasser_surah_{surah_number:03d}.mp3"
    dest_path = os.path.join(dest_dir, filename)

    ssl_context = _build_ssl_context()
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})

    total_bytes = 0
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_context) as resp:
            status_code = getattr(resp, "status", None) or resp.getcode()
            if status_code != 200:
                raise AudioSourceError(f"Audio download HTTP request returned status {status_code}.")
            with open(dest_path, "wb") as f_out:
                while True:
                    chunk = resp.read(buffer_size)
                    if not chunk:
                        break
                    f_out.write(chunk)
                    total_bytes += len(chunk)

        if total_bytes <= 0:
            if os.path.exists(dest_path):
                os.remove(dest_path)
            raise AudioSourceError("Downloaded audio file is empty (0 bytes).")
    except Exception as err:
        if os.path.exists(dest_path):
            try:
                os.remove(dest_path)
            except OSError:
                pass
        if isinstance(err, AudioSourceError):
            raise
        raise AudioSourceError(f"Failed to download Surah audio: {err}") from err

    return dest_path
