import random
import unittest

from engine.config import (
    ConfigurationError,
    DurationConfig,
    VerseSelectionLimits,
    choose_random_target_duration,
)


class DurationConfigTests(unittest.TestCase):
    def test_default_duration_configuration(self) -> None:
        config = DurationConfig()

        self.assertEqual(config.minimum_duration, 15.0)
        self.assertEqual(config.maximum_duration, 45.0)

    def test_valid_custom_duration_configuration(self) -> None:
        config = DurationConfig(minimum_duration=10, maximum_duration=22.5)

        self.assertEqual(config.minimum_duration, 10.0)
        self.assertEqual(config.maximum_duration, 22.5)

    def test_minimum_below_ten_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "below 10"):
            DurationConfig(minimum_duration=9, maximum_duration=20)

    def test_maximum_above_sixty_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "exceed 60"):
            DurationConfig(minimum_duration=15, maximum_duration=61)

    def test_minimum_greater_than_maximum_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "less than or equal"):
            DurationConfig(minimum_duration=25, maximum_duration=20)

    def test_non_finite_or_non_numeric_duration_is_rejected(self) -> None:
        invalid_values = (float("nan"), float("inf"), True, "15")

        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(ConfigurationError):
                    DurationConfig(minimum_duration=value, maximum_duration=30)

    def test_seeded_random_target_is_deterministic_and_inside_range(self) -> None:
        config = DurationConfig(minimum_duration=12, maximum_duration=18)
        first_rng = random.Random(42)
        second_rng = random.Random(42)

        first_targets = [
            choose_random_target_duration(config, first_rng)
            for _ in range(10)
        ]
        second_targets = [
            choose_random_target_duration(config, second_rng)
            for _ in range(10)
        ]

        self.assertEqual(first_targets, second_targets)
        self.assertTrue(all(12 <= target <= 18 for target in first_targets))
        self.assertGreater(len(set(first_targets)), 1)

    def test_fixed_duration_range_returns_that_duration(self) -> None:
        config = DurationConfig(minimum_duration=15, maximum_duration=15)

        self.assertEqual(
            choose_random_target_duration(config, random.Random(1)),
            15.0,
        )


class VerseSelectionLimitsTests(unittest.TestCase):
    def test_limits_must_be_positive(self) -> None:
        with self.assertRaises(ConfigurationError):
            VerseSelectionLimits(maximum_arabic_characters=0)

        with self.assertRaises(ConfigurationError):
            VerseSelectionLimits(maximum_english_characters=-1)

        with self.assertRaises(ConfigurationError):
            VerseSelectionLimits(maximum_ayahs=0)


if __name__ == "__main__":
    unittest.main()
