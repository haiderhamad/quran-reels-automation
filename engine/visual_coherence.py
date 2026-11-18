"""Deterministic visual coherence rules and attribute extraction for multi-clip Quran Reels.

Ensures that multi-clip backgrounds (2–3 clips) maintain visual continuity and calm
atmosphere without abrupt, distracting shifts in lighting, weather, or scene family.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence


SCENE_FAMILY_WATER = "water"
SCENE_FAMILY_SKY_CLOUDS = "sky_clouds"
SCENE_FAMILY_MOUNTAIN = "mountain"
SCENE_FAMILY_FOREST = "forest"
SCENE_FAMILY_RAIN = "rain"
SCENE_FAMILY_MIST = "mist"
SCENE_FAMILY_SPACE = "space"
SCENE_FAMILY_TEXTURE = "nature_texture"
SCENE_FAMILY_UNKNOWN = "unknown"

LIGHTING_DAY = "day"
LIGHTING_SUNSET = "sunset"
LIGHTING_NIGHT = "night"
LIGHTING_NEUTRAL = "neutral"

WEATHER_CLEAR = "clear"
WEATHER_CLOUDY = "cloudy"
WEATHER_RAIN = "rain"
WEATHER_MIST = "mist"

BRIGHTNESS_DARK = "dark"
BRIGHTNESS_MEDIUM = "medium"
BRIGHTNESS_BRIGHT = "bright"


@dataclass(frozen=True)
class VisualAttributes:
    """Lightweight deterministic visual attributes for a background asset."""

    scene_family: str
    lighting: str
    weather: str
    brightness_bucket: str = BRIGHTNESS_MEDIUM
    source_query: str = ""


# Compatible scene families (undirected graph)
COMPATIBLE_SCENE_FAMILIES: dict[str, set[str]] = {
    SCENE_FAMILY_WATER: {
        SCENE_FAMILY_WATER,
        SCENE_FAMILY_FOREST,
        SCENE_FAMILY_MOUNTAIN,
        SCENE_FAMILY_RAIN,
        SCENE_FAMILY_MIST,
        SCENE_FAMILY_TEXTURE,
    },
    SCENE_FAMILY_SKY_CLOUDS: {
        SCENE_FAMILY_SKY_CLOUDS,
        SCENE_FAMILY_MOUNTAIN,
        SCENE_FAMILY_WATER,
        SCENE_FAMILY_MIST,
    },
    SCENE_FAMILY_MOUNTAIN: {
        SCENE_FAMILY_MOUNTAIN,
        SCENE_FAMILY_SKY_CLOUDS,
        SCENE_FAMILY_FOREST,
        SCENE_FAMILY_WATER,
        SCENE_FAMILY_MIST,
    },
    SCENE_FAMILY_FOREST: {
        SCENE_FAMILY_FOREST,
        SCENE_FAMILY_WATER,
        SCENE_FAMILY_MOUNTAIN,
        SCENE_FAMILY_RAIN,
        SCENE_FAMILY_MIST,
        SCENE_FAMILY_TEXTURE,
    },
    SCENE_FAMILY_RAIN: {
        SCENE_FAMILY_RAIN,
        SCENE_FAMILY_WATER,
        SCENE_FAMILY_FOREST,
        SCENE_FAMILY_MIST,
    },
    SCENE_FAMILY_MIST: {
        SCENE_FAMILY_MIST,
        SCENE_FAMILY_FOREST,
        SCENE_FAMILY_MOUNTAIN,
        SCENE_FAMILY_WATER,
        SCENE_FAMILY_RAIN,
        SCENE_FAMILY_SKY_CLOUDS,
    },
    SCENE_FAMILY_SPACE: {
        SCENE_FAMILY_SPACE,  # Space is only coherent with space
    },
    SCENE_FAMILY_TEXTURE: {
        SCENE_FAMILY_TEXTURE,
        SCENE_FAMILY_WATER,
        SCENE_FAMILY_FOREST,
    },
    SCENE_FAMILY_UNKNOWN: set(),
}


def extract_visual_attributes(
    query: str,
    metadata: Optional[Mapping[str, Any]] = None,
) -> VisualAttributes:
    """Extract deterministic visual attributes from query string and asset metadata."""
    q = (query or "").lower().strip()
    meta = dict(metadata or {})

    # 1. Scene Family
    if any(term in q for term in ("space", "stars", "galaxy", "night sky")):
        scene_family = SCENE_FAMILY_SPACE
    elif any(term in q for term in ("rain", "rainfall", "storm")):
        scene_family = SCENE_FAMILY_RAIN
    elif any(term in q for term in ("fog", "mist", "haze")):
        scene_family = SCENE_FAMILY_MIST
    elif any(term in q for term in ("waterfall", "river", "ocean", "sea waves", "beach waves", "water texture")):
        scene_family = SCENE_FAMILY_WATER
    elif any(term in q for term in ("mountain", "peak", "hills")):
        scene_family = SCENE_FAMILY_MOUNTAIN
    elif any(term in q for term in ("forest", "trees", "woods", "flowers")):
        scene_family = SCENE_FAMILY_FOREST
    elif any(term in q for term in ("clouds", "sky clouds", "moving clouds", "cloud")):
        scene_family = SCENE_FAMILY_SKY_CLOUDS
    elif any(term in q for term in ("texture", "abstract")):
        scene_family = SCENE_FAMILY_TEXTURE
    else:
        scene_family = SCENE_FAMILY_UNKNOWN

    # 2. Lighting
    if any(term in q for term in ("space", "stars", "night")):
        lighting = LIGHTING_NIGHT
    elif any(term in q for term in ("sunset", "sunrise", "golden", "dusk", "dawn")):
        lighting = LIGHTING_SUNSET
    else:
        lighting = LIGHTING_DAY

    # 3. Weather
    if any(term in q for term in ("rain", "rainfall")):
        weather = WEATHER_RAIN
    elif any(term in q for term in ("fog", "mist")):
        weather = WEATHER_MIST
    elif any(term in q for term in ("cloud", "clouds")):
        weather = WEATHER_CLOUDY
    else:
        weather = WEATHER_CLEAR

    # 4. Brightness bucket
    # If render robustness or safety metadata has brightness metrics, use them
    brightness = BRIGHTNESS_MEDIUM
    if lighting == LIGHTING_NIGHT:
        brightness = BRIGHTNESS_DARK
    elif lighting == LIGHTING_SUNSET:
        brightness = BRIGHTNESS_MEDIUM
    else:
        brightness = BRIGHTNESS_BRIGHT

    return VisualAttributes(
        scene_family=scene_family,
        lighting=lighting,
        weather=weather,
        brightness_bucket=brightness,
        source_query=q,
    )


def are_clips_coherent(clip_a: VisualAttributes, clip_b: VisualAttributes) -> tuple[bool, str]:
    """Check whether two background clips satisfy visual coherence requirements."""
    # 1. Lighting continuity: night is strictly incompatible with day and sunset
    if clip_a.lighting != clip_b.lighting:
        if LIGHTING_NIGHT in (clip_a.lighting, clip_b.lighting):
            return False, f"Lighting conflict: {clip_a.lighting} vs {clip_b.lighting}"

    # 2. Space exclusivity: space/stars cannot be mixed with earthly terrestrial scenes
    if (clip_a.scene_family == SCENE_FAMILY_SPACE) != (clip_b.scene_family == SCENE_FAMILY_SPACE):
        return False, f"Space/celestial scene cannot mix with terrestrial scene ({clip_a.scene_family} vs {clip_b.scene_family})"

    # 3. Weather continuity: heavy rain cannot abruptly transition to sunset/golden hour or bright beach
    if (clip_a.weather == WEATHER_RAIN and clip_b.lighting == LIGHTING_SUNSET) or (
        clip_b.weather == WEATHER_RAIN and clip_a.lighting == LIGHTING_SUNSET
    ):
        return False, "Weather/lighting conflict: heavy rain cannot pair with sunset/golden hour"

    if (clip_a.weather == WEATHER_RAIN and "beach" in clip_b.source_query) or (
        clip_b.weather == WEATHER_RAIN and "beach" in clip_a.source_query
    ):
        return False, "Weather conflict: rain cannot pair with beach waves"

    # 4. Scene family compatibility
    if clip_a.scene_family != SCENE_FAMILY_UNKNOWN and clip_b.scene_family != SCENE_FAMILY_UNKNOWN:
        allowed_b = COMPATIBLE_SCENE_FAMILIES.get(clip_a.scene_family, set())
        if clip_b.scene_family not in allowed_b:
            return False, f"Incompatible scene families: {clip_a.scene_family} and {clip_b.scene_family}"

    return True, "COHERENT"


from typing import Any, Callable, Mapping, Optional, Sequence, TypeVar

T = TypeVar("T")


def are_all_clips_coherent(clips: Sequence[VisualAttributes]) -> tuple[bool, str]:
    """Check pairwise visual coherence for an entire group of 2 or 3 clips."""
    if len(clips) <= 1:
        return True, "COHERENT"

    for i in range(len(clips)):
        for j in range(i + 1, len(clips)):
            coherent, reason = are_clips_coherent(clips[i], clips[j])
            if not coherent:
                return False, f"Clips [{i}] and [{j}] incompatible: {reason}"

    return True, "COHERENT"


def plan_background_clips(
    available_assets: Sequence[T],
    target_duration: float,
    *,
    max_clips: int = 3,
    crossfade_seconds: float = 0.75,
    get_duration: Callable[[T], float],
    get_attributes: Callable[[T], VisualAttributes],
) -> Optional[tuple[T, ...]]:
    """Deterministic long-reel background planning algorithm (Phase 8G.5 Section 8).

    Hierarchy:
    1. Prefer 1 clip if it can cover target_duration with speed >= 0.5x (duration >= target_duration * 0.5).
    2. Try compatible pair (2 clips) if combined duration covers with speed >= 0.5x.
    3. Try compatible triple (3 clips) if combined duration covers with speed >= 0.5x.
    4. Never return 4 or 5 clips. Return None if unable to plan.
    """
    if target_duration <= 0.0 or not available_assets:
        return None

    bounded_max_clips = min(3, max(1, max_clips))

    # Priority 1: 1 clip
    for asset in available_assets:
        dur = get_duration(asset)
        if dur >= target_duration * 0.5:
            return (asset,)

    if bounded_max_clips < 2:
        return None

    # Priority 2: 2 compatible clips
    pair_target_total = (target_duration + crossfade_seconds) * 0.5
    pair_target_each = ((target_duration + crossfade_seconds) / 2.0) * 0.5
    n = len(available_assets)
    for i in range(n):
        dur_a = get_duration(available_assets[i])
        if dur_a < pair_target_each:
            continue
        attr_a = get_attributes(available_assets[i])
        for j in range(i + 1, n):
            dur_b = get_duration(available_assets[j])
            if dur_b < pair_target_each:
                continue
            if (dur_a + dur_b) < pair_target_total:
                continue
            attr_b = get_attributes(available_assets[j])
            coherent, _ = are_clips_coherent(attr_a, attr_b)
            if coherent:
                return (available_assets[i], available_assets[j])

    if bounded_max_clips < 3:
        return None

    # Priority 3: 3 compatible clips
    triple_target_total = (target_duration + 2.0 * crossfade_seconds) * 0.5
    triple_target_each = ((target_duration + 2.0 * crossfade_seconds) / 3.0) * 0.5
    for i in range(n):
        dur_a = get_duration(available_assets[i])
        if dur_a < triple_target_each:
            continue
        attr_a = get_attributes(available_assets[i])
        for j in range(i + 1, n):
            dur_b = get_duration(available_assets[j])
            if dur_b < triple_target_each:
                continue
            attr_b = get_attributes(available_assets[j])
            coherent_ab, _ = are_clips_coherent(attr_a, attr_b)
            if not coherent_ab:
                continue
            for k in range(j + 1, n):
                dur_c = get_duration(available_assets[k])
                if dur_c < triple_target_each:
                    continue
                if (dur_a + dur_b + dur_c) < triple_target_total:
                    continue
                attr_c = get_attributes(available_assets[k])
                coherent_all, _ = are_all_clips_coherent([attr_a, attr_b, attr_c])
                if coherent_all:
                    return (available_assets[i], available_assets[j], available_assets[k])

    return None
