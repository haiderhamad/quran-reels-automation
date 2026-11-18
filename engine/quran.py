"""Authentic Arabic Uthmani Quran source module for Quran Foundation Content API v4."""

from __future__ import annotations

import base64
import json
import os
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional, Sequence, Union


DEFAULT_QF_ENV = "production"
PRELIVE_API_BASE_URL = "https://apis-prelive.quran.foundation/content/api/v4"
PRODUCTION_API_BASE_URL = "https://apis.quran.foundation/content/api/v4"
PRELIVE_OAUTH_TOKEN_URL = "https://prelive-oauth2.quran.foundation/oauth2/token"
PRODUCTION_OAUTH_TOKEN_URL = "https://oauth2.quran.foundation/oauth2/token"

DEFAULT_SCRIPT = "uthmani"
DEFAULT_FIELD = "text_uthmani"
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

# Standard Surah metadata: number -> (arabic_name, english_name, english_name_translation)
STANDARD_SURAH_METADATA: dict[int, tuple[str, str, Optional[str]]] = {
    1: ("الفاتحة", "Al-Fatihah", "The Opener"),
    2: ("البقرة", "Al-Baqarah", "The Cow"),
    3: ("آل عمران", "Ali 'Imran", "Family of Imran"),
    4: ("النساء", "An-Nisa", "The Women"),
    5: ("المائدة", "Al-Ma'idah", "The Table Spread"),
    6: ("الأنعام", "Al-An'am", "The Cattle"),
    7: ("الأعراف", "Al-A'raf", "The Heights"),
    8: ("الأنفال", "Al-Anfal", "The Spoils of War"),
    9: ("التوبة", "At-Tawbah", "The Repentance"),
    10: ("يونس", "Yunus", "Jonah"),
    11: ("هود", "Hud", "Hud"),
    12: ("يوسف", "Yusuf", "Joseph"),
    13: ("الرعد", "Ar-Ra'd", "The Thunder"),
    14: ("ابراهيم", "Ibrahim", "Abraham"),
    15: ("الحجر", "Al-Hijr", "The Rocky Tract"),
    16: ("النحل", "An-Nahl", "The Bee"),
    17: ("الإسراء", "Al-Isra", "The Night Journey"),
    18: ("الكهف", "Al-Kahf", "The Cave"),
    19: ("مريم", "Maryam", "Mary"),
    20: ("طه", "Taha", "Ta-Ha"),
    21: ("الأنبياء", "Al-Anbya", "The Prophets"),
    22: ("الحج", "Al-Hajj", "The Pilgrimage"),
    23: ("المؤمنون", "Al-Mu'minun", "The Believers"),
    24: ("النور", "An-Nur", "The Light"),
    25: ("الفرقان", "Al-Furqan", "The Criterion"),
    26: ("الشعراء", "Ash-Shu'ara", "The Poets"),
    27: ("النمل", "An-Naml", "The Ant"),
    28: ("القصص", "Al-Qasas", "The Stories"),
    29: ("العنكبوت", "Al-'Ankabut", "The Spider"),
    30: ("الروم", "Ar-Rum", "The Romans"),
    31: ("لقمان", "Luqman", "Luqman"),
    32: ("السجدة", "As-Sajdah", "The Prostration"),
    33: ("الأحزاب", "Al-Ahzab", "The Combined Forces"),
    34: ("سبإ", "Saba", "Sheba"),
    35: ("فاطر", "Fatir", "Originator"),
    36: ("يس", "Ya-Sin", "Ya Sin"),
    37: ("الصافات", "As-Saffat", "Those who set the Ranks"),
    38: ("ص", "Sad", "The Letter \"Saad\""),
    39: ("الزمر", "Az-Zumar", "The Troops"),
    40: ("غافر", "Ghafir", "The Forgiver"),
    41: ("فصلت", "Fussilat", "Explained in Detail"),
    42: ("الشورى", "Ash-Shuraa", "The Consultation"),
    43: ("الزخرف", "Az-Zukhruf", "The Ornaments of Gold"),
    44: ("الدخان", "Ad-Dukhan", "The Smoke"),
    45: ("الجاثية", "Al-Jathiyah", "The Crouching"),
    46: ("الأحقاف", "Al-Ahqaf", "The Wind-Curved Sandhills"),
    47: ("محمد", "Muhammad", "Muhammad"),
    48: ("الفتح", "Al-Fath", "The Victory"),
    49: ("الحجرات", "Al-Hujurat", "The Rooms"),
    50: ("ق", "Qaf", "The Letter \"Qaf\""),
    51: ("الذاريات", "Adh-Dhariyat", "The Winnowing Winds"),
    52: ("الطور", "At-Tur", "The Mount"),
    53: ("النجم", "An-Najm", "The Star"),
    54: ("القمر", "Al-Qamar", "The Moon"),
    55: ("الرحمن", "Ar-Rahman", "The Beneficent"),
    56: ("الواقعة", "Al-Waqi'ah", "The Inevitable"),
    57: ("الحديد", "Al-Hadid", "The Iron"),
    58: ("المجادلة", "Al-Mujadila", "The Pleading Woman"),
    59: ("الحشر", "Al-Hashr", "The Exile"),
    60: ("الممتحنة", "Al-Mumtahanah", "She that is to be examined"),
    61: ("الصف", "As-Saf", "The Ranks"),
    62: ("الجمعة", "Al-Jumu'ah", "The Congregation, Friday"),
    63: ("المنافقون", "Al-Munafiqun", "The Hypocrites"),
    64: ("التغابن", "At-Taghabun", "The Mutual Disillusion"),
    65: ("الطلاق", "At-Talaq", "The Divorce"),
    66: ("التحريم", "At-Tahrim", "The Prohibition"),
    67: ("الملك", "Al-Mulk", "The Sovereignty"),
    68: ("القلم", "Al-Qalam", "The Pen"),
    69: ("الحاقة", "Al-Haqqah", "The Reality"),
    70: ("المعارج", "Al-Ma'arij", "The Ascending Stairways"),
    71: ("نوح", "Nuh", "Noah"),
    72: ("الجن", "Al-Jinn", "The Jinn"),
    73: ("المزمل", "Al-Muzzammil", "The Enshrouded One"),
    74: ("المدثر", "Al-Muddaththir", "The Cloaked One"),
    75: ("القيامة", "Al-Qiyamah", "The Resurrection"),
    76: ("الانسان", "Al-Insan", "The Man"),
    77: ("المرسلات", "Al-Mursalat", "The Emissaries"),
    78: ("النبإ", "An-Naba", "The Tidings"),
    79: ("النازعات", "An-Nazi'at", "Those who drag forth"),
    80: ("عبس", "'Abasa", "He Frowned"),
    81: ("التكوير", "At-Takwir", "The Overthrowing"),
    82: ("الإنفطار", "Al-Infitar", "The Cleaving"),
    83: ("المطففين", "Al-Mutaffifin", "The Defrauding"),
    84: ("الإنشقاق", "Al-Inshiqaq", "The Sundering"),
    85: ("البروج", "Al-Buruj", "The Mansions of the Stars"),
    86: ("الطارق", "At-Tariq", "The Nightcommer"),
    87: ("الأعلى", "Al-A'la", "The Most High"),
    88: ("الغاشية", "Al-Ghashiyah", "The Overwhelming"),
    89: ("الفجر", "Al-Fajr", "The Dawn"),
    90: ("البلد", "Al-Balad", "The City"),
    91: ("الشمس", "Ash-Shams", "The Sun"),
    92: ("الليل", "Al-Layl", "The Night"),
    93: ("الضحى", "Ad-Duhaa", "The Morning Hours"),
    94: ("الشرح", "Ash-Sharh", "The Relief"),
    95: ("التين", "At-Tin", "The Fig"),
    96: ("العلق", "Al-'Alaq", "The Clot"),
    97: ("القدر", "Al-Qadr", "The Power"),
    98: ("البينة", "Al-Bayyinah", "The Clear Proof"),
    99: ("الزلزلة", "Az-Zalzalah", "The Earthquake"),
    100: ("العاديات", "Al-'Adiyat", "The Courser"),
    101: ("القارعة", "Al-Qari'ah", "The Calamity"),
    102: ("التكاثر", "At-Takathur", "The Rivalry in world increase"),
    103: ("العصر", "Al-'Asr", "The Declining Day"),
    104: ("الهمزة", "Al-Humazah", "The Traducer"),
    105: ("الفيل", "Al-Fil", "The Elephant"),
    106: ("قريش", "Quraysh", "Quraysh"),
    107: ("الماعون", "Al-Ma'un", "The Small kindnesses"),
    108: ("الكوثر", "Al-Kawthar", "The Abundance"),
    109: ("الكافرون", "Al-Kafirun", "The Disbelievers"),
    110: ("النصر", "An-Nasr", "The Divine Support"),
    111: ("المسد", "Al-Masad", "The Palm Fiber"),
    112: ("الإخلاص", "Al-Ikhlas", "The Sincerity"),
    113: ("الفلق", "Al-Falaq", "The Daybreak"),
    114: ("الناس", "An-Nas", "Mankind"),
}


class QuranSourceError(ValueError):
    """Raised when Quran data fetching, parsing, or validation fails."""


@dataclass(frozen=True)
class QuranAyah:
    """A single immutable ayah with authentic Arabic text."""

    global_number: int
    surah_number: int
    ayah_number: int
    arabic_text: str


@dataclass(frozen=True)
class QuranSurah:
    """A complete Surah containing all consecutive immutable ayahs."""

    number: int
    arabic_name: str
    english_name: str
    english_name_translation: Optional[str]
    number_of_ayahs: int
    ayahs: tuple[QuranAyah, ...]


@dataclass(frozen=True)
class QuranSourceMetadata:
    """Source provenance and edition metadata for retrieved Quran data."""

    provider: str
    api_base_url: str
    environment: str
    script: str
    field: str


@dataclass(frozen=True)
class QuranData:
    """Complete, verified Quran dataset containing all 114 Surahs and 6236 Ayahs."""

    surahs: tuple[QuranSurah, ...]
    source: QuranSourceMetadata

    def get_surah(self, surah_number: int) -> QuranSurah:
        if (
            not isinstance(surah_number, int)
            or isinstance(surah_number, bool)
            or surah_number < 1
            or surah_number > len(self.surahs)
        ):
            raise QuranSourceError(
                f"Surah number {surah_number} is out of range (1..{len(self.surahs)})."
            )
        return self.surahs[surah_number - 1]

    def get_ayah(self, surah_number: int, ayah_number: int) -> QuranAyah:
        surah = self.get_surah(surah_number)
        if (
            not isinstance(ayah_number, int)
            or isinstance(ayah_number, bool)
            or ayah_number < 1
            or ayah_number > surah.number_of_ayahs
        ):
            raise QuranSourceError(
                f"Ayah number {ayah_number} is out of range for Surah {surah_number} (1..{surah.number_of_ayahs})."
            )
        return surah.ayahs[ayah_number - 1]

    def get_ayah_range(
        self,
        surah_number: int,
        first_ayah: int,
        last_ayah: int,
    ) -> tuple[QuranAyah, ...]:
        surah = self.get_surah(surah_number)
        if (
            not isinstance(first_ayah, int)
            or isinstance(first_ayah, bool)
            or not isinstance(last_ayah, int)
            or isinstance(last_ayah, bool)
        ):
            raise QuranSourceError("Ayah numbers must be integers.")
        if first_ayah < 1 or last_ayah > surah.number_of_ayahs or first_ayah > last_ayah:
            raise QuranSourceError(
                f"Invalid ayah range {first_ayah}..{last_ayah} for Surah {surah_number} (1..{surah.number_of_ayahs})."
            )
        return surah.ayahs[first_ayah - 1 : last_ayah]

    @property
    def total_surahs(self) -> int:
        return len(self.surahs)

    @property
    def total_ayahs(self) -> int:
        return sum(surah.number_of_ayahs for surah in self.surahs)


def validate_quran_source_for_run(run_kind: str, quran_data: QuranData) -> None:
    """Validate Quran dataset integrity against run semantics.

    Production runs ('scheduled' or 'manual_production') fail closed unless:
    1. quran_data.source.environment == 'production' (strictly require explicitly approved production source; never permit 'prelive' or other sandboxes)
    2. quran_data.total_surahs == 114 (full canonical Surah count)
    3. quran_data.total_ayahs == 6236 (full canonical Ayah count)

    Test runs ('test') may use either production or approved prelive sandboxes.
    """
    clean_kind = (run_kind or "").strip().lower()
    if clean_kind in ("scheduled", "manual_production"):
        if quran_data is None:
            raise QuranSourceError(f"Production {clean_kind} run requires a valid Quran dataset.")
        source = getattr(quran_data, "source", None)
        env = getattr(source, "environment", None)
        total_surahs = getattr(quran_data, "total_surahs", None)
        total_ayahs = getattr(quran_data, "total_ayahs", None)
        if env != "production":
            raise QuranSourceError(
                f"Production {clean_kind} run requires verified production Quran source (env='production'). "
                f"Found env='{env}'."
            )
        if total_surahs != EXPECTED_SURAH_COUNT:
            raise QuranSourceError(
                f"Production {clean_kind} run requires full canonical Quran dataset ({EXPECTED_SURAH_COUNT} Surahs). "
                f"Found {total_surahs} Surahs."
            )
        if total_ayahs != EXPECTED_TOTAL_AYAH_COUNT:
            raise QuranSourceError(
                f"Production {clean_kind} run requires full canonical Quran dataset ({EXPECTED_TOTAL_AYAH_COUNT} Ayahs). "
                f"Found {total_ayahs} Ayahs."
            )
    elif clean_kind == "test":
        if quran_data is None:
            raise QuranSourceError("Test run requires a valid Quran dataset.")
        return
    else:
        raise QuranSourceError(f"Unsupported run_kind '{run_kind}' for Quran source validation.")


# In-memory token cache: (client_id, environment) -> (access_token, expires_at_timestamp)
_TOKEN_CACHE: dict[tuple[str, str], tuple[str, float]] = {}


def _clear_token_cache(client_id: Optional[str] = None, qf_env: Optional[str] = None) -> None:
    """Clear cached token for a client_id/env or all cached tokens."""
    global _TOKEN_CACHE
    if client_id is not None and qf_env is not None:
        _TOKEN_CACHE.pop((client_id, qf_env), None)
    else:
        _TOKEN_CACHE.clear()


def _get_api_base_url(qf_env: str) -> str:
    env = str(qf_env).strip().lower()
    if env == "production":
        return PRODUCTION_API_BASE_URL
    if env == "prelive":
        return PRELIVE_API_BASE_URL
    raise QuranSourceError(f"Invalid Quran Foundation environment '{qf_env}'. Must be 'prelive' or 'production'.")


def _get_oauth_token_url(qf_env: str) -> str:
    env = str(qf_env).strip().lower()
    if env == "production":
        return PRODUCTION_OAUTH_TOKEN_URL
    if env == "prelive":
        return PRELIVE_OAUTH_TOKEN_URL
    raise QuranSourceError(f"Invalid Quran Foundation environment '{qf_env}'. Must be 'prelive' or 'production'.")


def _build_ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _get_access_token(
    client_id: Optional[str] = None,
    client_secret: Optional[str] = None,
    qf_env: str = DEFAULT_QF_ENV,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    user_agent: str = DEFAULT_USER_AGENT,
) -> str:
    """Obtain or retrieve a cached OAuth2 access token for Quran Foundation Content API."""
    resolved_client_id = client_id or os.getenv("QF_CLIENT_ID")
    resolved_client_secret = client_secret or os.getenv("QF_CLIENT_SECRET")

    if not resolved_client_id or not resolved_client_secret:
        raise QuranSourceError(
            "Missing Quran Foundation credentials. Set QF_CLIENT_ID and QF_CLIENT_SECRET environment variables."
        )

    cache_key = (resolved_client_id, qf_env)
    now = time.time()

    if cache_key in _TOKEN_CACHE:
        token, expires_at = _TOKEN_CACHE[cache_key]
        # Reuse cached token until 30 seconds before expiry
        if now < expires_at - 30.0:
            return token

    token_url = _get_oauth_token_url(qf_env)
    creds_bytes = f"{resolved_client_id}:{resolved_client_secret}".encode("utf-8")
    basic_auth = base64.b64encode(creds_bytes).decode("ascii")
    form_data = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "scope": "content",
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        token_url,
        data=form_data,
        headers={
            "User-Agent": user_agent,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "Authorization": f"Basic {basic_auth}",
        },
        method="POST",
    )

    ssl_context = _build_ssl_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_context) as resp:
            status_code = getattr(resp, "status", None) or resp.getcode()
            if status_code != 200:
                raise QuranSourceError(f"OAuth token request failed with status {status_code}.")
            body_bytes = resp.read()
    except urllib.error.HTTPError as err:
        raise QuranSourceError(f"OAuth token request HTTP error {err.code}: {err.reason}") from err
    except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError, ssl.SSLError) as err:
        raise QuranSourceError(f"OAuth token request network error: {err}") from err
    except Exception as err:
        raise QuranSourceError(f"OAuth token request unexpected error: {err}") from err

    try:
        token_payload = json.loads(body_bytes.decode("utf-8"))
    except Exception as err:
        raise QuranSourceError(f"Malformed JSON in OAuth token response: {err}") from err

    if not isinstance(token_payload, dict):
        raise QuranSourceError("OAuth token response must be a JSON object.")

    access_token = token_payload.get("access_token")
    if not isinstance(access_token, str) or not access_token.strip():
        raise QuranSourceError("Missing or invalid 'access_token' in OAuth token response.")

    expires_in_val = token_payload.get("expires_in", 3600)
    try:
        expires_in = float(expires_in_val)
    except (TypeError, ValueError):
        expires_in = 3600.0

    _TOKEN_CACHE[cache_key] = (access_token, now + expires_in)
    return access_token


def parse_quran_payload(
    raw_payload: Union[str, bytes, dict, Sequence],
    qf_env: str = DEFAULT_QF_ENV,
    api_base_url: Optional[str] = None,
) -> QuranData:
    """Parse and strictly validate a complete Quran Foundation Content API verses payload."""
    if isinstance(raw_payload, (str, bytes)):
        try:
            payload = json.loads(raw_payload)
        except Exception as err:
            raise QuranSourceError(f"Malformed JSON payload: {err}") from err
    else:
        payload = raw_payload

    if isinstance(payload, dict):
        verses_list = payload.get("verses")
        if not isinstance(verses_list, list):
            raise QuranSourceError("Missing or invalid 'verses' list in API response.")
    elif isinstance(payload, list):
        verses_list = payload
    else:
        raise QuranSourceError("API payload must be a JSON string, bytes, dict, or list.")

    env_clean = str(qf_env).strip().lower() if qf_env is not None else ""
    is_prelive_env = env_clean == "prelive"

    if is_prelive_env and len(verses_list) == 293:
        # Quran Foundation prelive sandbox dataset provides Surahs 1..2 (293 ayahs)
        expected_total_ayahs = 293
        expected_surah_count = 2
    else:
        # Production and standard validation requires all 6236 canonical ayahs across 114 Surahs
        expected_total_ayahs = EXPECTED_TOTAL_AYAH_COUNT
        expected_surah_count = EXPECTED_SURAH_COUNT

    if len(verses_list) != expected_total_ayahs:
        if is_prelive_env:
            raise QuranSourceError(
                f"Expected exactly 293 or {EXPECTED_TOTAL_AYAH_COUNT} verses in prelive payload, got {len(verses_list)}."
            )
        raise QuranSourceError(
            f"Expected exactly {EXPECTED_TOTAL_AYAH_COUNT} verses in production payload, got {len(verses_list)}."
        )

    surah_ayah_map: dict[int, list[QuranAyah]] = {s: [] for s in range(1, expected_surah_count + 1)}
    seen_verse_keys: set[str] = set()

    for idx, item in enumerate(verses_list):
        if not isinstance(item, dict):
            raise QuranSourceError(f"Verse item at index {idx} must be a JSON object.")

        verse_key = item.get("verse_key")
        if not isinstance(verse_key, str) or not verse_key.strip():
            raise QuranSourceError(f"Verse item at index {idx} missing 'verse_key'.")

        if verse_key in seen_verse_keys:
            raise QuranSourceError(f"Duplicate verse_key '{verse_key}' found in payload.")
        seen_verse_keys.add(verse_key)

        parts = verse_key.split(":")
        if len(parts) != 2:
            raise QuranSourceError(f"Invalid verse_key format '{verse_key}'. Expected 'surah:ayah'.")

        try:
            surah_num = int(parts[0])
            ayah_num = int(parts[1])
        except ValueError as err:
            raise QuranSourceError(f"Non-integer numbers in verse_key '{verse_key}'.") from err

        if surah_num < 1 or surah_num > expected_surah_count:
            raise QuranSourceError(f"Surah number {surah_num} in verse_key '{verse_key}' out of range (1..{expected_surah_count}).")

        expected_ayah_count = STANDARD_SURAH_AYAH_COUNTS[surah_num]
        if ayah_num < 1 or ayah_num > expected_ayah_count:
            raise QuranSourceError(
                f"Ayah number {ayah_num} in verse_key '{verse_key}' out of range (1..{expected_ayah_count})."
            )

        text_uthmani = item.get(DEFAULT_FIELD)
        if not isinstance(text_uthmani, str) or not text_uthmani.strip():
            raise QuranSourceError(f"Missing or empty '{DEFAULT_FIELD}' in verse '{verse_key}'.")

        global_id_val = item.get("id")
        try:
            global_id = int(global_id_val) if global_id_val is not None else idx + 1
        except (TypeError, ValueError):
            global_id = idx + 1

        ayah_obj = QuranAyah(
            global_number=global_id,
            surah_number=surah_num,
            ayah_number=ayah_num,
            arabic_text=text_uthmani,
        )
        surah_ayah_map[surah_num].append(ayah_obj)

    surahs: list[QuranSurah] = []
    for surah_num in range(1, expected_surah_count + 1):
        ayahs_for_surah = surah_ayah_map[surah_num]
        expected_count = STANDARD_SURAH_AYAH_COUNTS[surah_num]

        if len(ayahs_for_surah) != expected_count:
            raise QuranSourceError(
                f"Surah {surah_num} has {len(ayahs_for_surah)} ayahs, expected {expected_count}."
            )

        # Validate consecutive ordering
        for a_idx, ayah in enumerate(ayahs_for_surah):
            expected_ayah_num = a_idx + 1
            if ayah.ayah_number != expected_ayah_num:
                raise QuranSourceError(
                    f"Surah {surah_num} ayah number mismatch: expected {expected_ayah_num}, got {ayah.ayah_number}."
                )

        arabic_name, english_name, translation_name = STANDARD_SURAH_METADATA.get(
            surah_num, (f"سورة {surah_num}", f"Surah {surah_num}", None)
        )

        surah_obj = QuranSurah(
            number=surah_num,
            arabic_name=arabic_name,
            english_name=english_name,
            english_name_translation=translation_name,
            number_of_ayahs=expected_count,
            ayahs=tuple(ayahs_for_surah),
        )
        surahs.append(surah_obj)

    resolved_base_url = api_base_url or _get_api_base_url(qf_env)

    metadata = QuranSourceMetadata(
        provider="Quran Foundation",
        api_base_url=resolved_base_url,
        environment=qf_env,
        script=DEFAULT_SCRIPT,
        field=DEFAULT_FIELD,
    )

    return QuranData(
        surahs=tuple(surahs),
        source=metadata,
    )


def fetch_quran_data(
    client_id: Optional[str] = None,
    client_secret: Optional[str] = None,
    qf_env: Optional[str] = None,
    script: str = DEFAULT_SCRIPT,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    user_agent: str = DEFAULT_USER_AGENT,
    max_retries: int = 1,
) -> QuranData:
    """Fetch complete authentic Arabic Quran dataset from Quran Foundation Content API."""
    resolved_env = (qf_env or os.getenv("QF_ENV", DEFAULT_QF_ENV)).strip().lower()
    resolved_client_id = client_id or os.getenv("QF_CLIENT_ID")
    resolved_client_secret = client_secret or os.getenv("QF_CLIENT_SECRET")

    base_url = _get_api_base_url(resolved_env)
    content_url = f"{base_url.rstrip('/')}/quran/verses/{script}"

    if not content_url.startswith("https://"):
        raise QuranSourceError("Quran Foundation Content API requires a secure HTTPS URL.")

    ssl_context = _build_ssl_context()
    token = _get_access_token(
        client_id=resolved_client_id,
        client_secret=resolved_client_secret,
        qf_env=resolved_env,
        timeout=timeout,
        user_agent=user_agent,
    )

    attempts = max(1, max_retries + 1)
    last_error: Optional[Exception] = None

    for attempt in range(attempts):
        req = urllib.request.Request(
            content_url,
            headers={
                "User-Agent": user_agent,
                "Accept": "application/json",
                "x-auth-token": token,
                "x-client-id": resolved_client_id or "",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ssl_context) as resp:
                status_code = getattr(resp, "status", None) or resp.getcode()
                if status_code != 200:
                    raise QuranSourceError(f"HTTP request returned status {status_code}.")
                raw_body = resp.read()
                return parse_quran_payload(raw_body, qf_env=resolved_env, api_base_url=base_url)
        except urllib.error.HTTPError as err:
            last_error = err
            # Handle 401 Unauthorized by refreshing token once
            if err.code == 401 and attempt == 0:
                _clear_token_cache(resolved_client_id, resolved_env)
                token = _get_access_token(
                    client_id=resolved_client_id,
                    client_secret=resolved_client_secret,
                    qf_env=resolved_env,
                    timeout=timeout,
                    user_agent=user_agent,
                )
                continue
            if err.code < 500 or attempt == attempts - 1:
                raise QuranSourceError(f"HTTP error {err.code}: {err.reason}") from err
        except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError, ssl.SSLError) as err:
            last_error = err
            if attempt == attempts - 1:
                raise QuranSourceError(f"Network error while fetching Quran data: {err}") from err
        except Exception as err:
            raise QuranSourceError(f"Unexpected error while fetching Quran data: {err}") from err

        time.sleep(0.5)

    raise QuranSourceError(f"Failed to fetch Quran data from {content_url}: {last_error}")
