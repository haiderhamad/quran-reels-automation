"""Validated configuration for the provider-independent Reel engine."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from numbers import Real
from typing import Optional


DEFAULT_MINIMUM_DURATION_SECONDS = 15.0
DEFAULT_MAXIMUM_DURATION_SECONDS = 45.0
MINIMUM_ALLOWED_DURATION_SECONDS = 10.0
MAXIMUM_ALLOWED_DURATION_SECONDS = 60.0

DEFAULT_MAXIMUM_ARABIC_CHARACTERS = 220
DEFAULT_MAXIMUM_ENGLISH_CHARACTERS = 320
DEFAULT_MAXIMUM_AYAHS = 3


class ConfigurationError(ValueError):
    """Raised when engine configuration is invalid."""


def _finite_number(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ConfigurationError(f"{name} must be a finite number.")
    number = float(value)
    if not math.isfinite(number):
        raise ConfigurationError(f"{name} must be a finite number.")
    return number


def _positive_integer(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigurationError(f"{name} must be a positive integer.")
    return value


@dataclass(frozen=True)
class DurationConfig:
    """Allowed authentic recitation duration range, in seconds."""

    minimum_duration: float = DEFAULT_MINIMUM_DURATION_SECONDS
    maximum_duration: float = DEFAULT_MAXIMUM_DURATION_SECONDS

    def __post_init__(self) -> None:
        minimum = _finite_number("minimum_duration", self.minimum_duration)
        maximum = _finite_number("maximum_duration", self.maximum_duration)

        if minimum < MINIMUM_ALLOWED_DURATION_SECONDS:
            raise ConfigurationError(
                "minimum_duration cannot be below 10 seconds."
            )
        if maximum > MAXIMUM_ALLOWED_DURATION_SECONDS:
            raise ConfigurationError(
                f"maximum_duration cannot exceed {int(MAXIMUM_ALLOWED_DURATION_SECONDS)} seconds."
            )
        if minimum > maximum:
            raise ConfigurationError(
                "minimum_duration must be less than or equal to maximum_duration."
            )

        object.__setattr__(self, "minimum_duration", minimum)
        object.__setattr__(self, "maximum_duration", maximum)


@dataclass(frozen=True)
class VerseSelectionLimits:
    """Simple text and ayah-count limits used before rendering exists."""

    maximum_arabic_characters: int = DEFAULT_MAXIMUM_ARABIC_CHARACTERS
    maximum_english_characters: int = DEFAULT_MAXIMUM_ENGLISH_CHARACTERS
    maximum_ayahs: Optional[int] = DEFAULT_MAXIMUM_AYAHS

    def __post_init__(self) -> None:
        _positive_integer(
            "maximum_arabic_characters",
            self.maximum_arabic_characters,
        )
        _positive_integer(
            "maximum_english_characters",
            self.maximum_english_characters,
        )
        if self.maximum_ayahs is not None:
            _positive_integer("maximum_ayahs", self.maximum_ayahs)


def choose_random_target_duration(
    config: DurationConfig,
    rng: Optional[random.Random] = None,
) -> float:
    """Choose a target duration inside the validated configured range."""
    generator = rng if rng is not None else random.Random()
    return generator.uniform(
        config.minimum_duration,
        config.maximum_duration,
    )
