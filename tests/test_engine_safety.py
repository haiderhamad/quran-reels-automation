"""Unit tests for the local visual safety scanning module (person and face detection)."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

try:
    import cv2
    import numpy as np
except ImportError:
    cv2 = None
    np = None

from engine.safety import (
    ANIMAL_DETECTION_CLASSES,
    ANIMAL_POLICY_READY,
    DEFAULT_ANIMAL_CONFIDENCE_THRESHOLD,
    DEFAULT_ANIMAL_MODEL_FILENAME,
    DEFAULT_CACHE_DIR,
    DEFAULT_FACE_CONFIDENCE_THRESHOLD,
    DEFAULT_FACE_MODEL_FILENAME,
    DEFAULT_PERSON_CONFIDENCE_THRESHOLD,
    DEFAULT_PERSON_MODEL_FILENAME,
    DEFAULT_SAMPLE_FPS,
    Detection,
    MPPersonDetector,
    SafetyConfig,
    SafetyScanError,
    SafetyScanResult,
    YuNetFaceDetector,
    YoloXAnimalDetector,
    compute_sampling_schedule,
    generate_mp_anchors,
    scan_frame,
    scan_video,
)


def create_synthetic_test_video(
    filepath: str,
    num_frames: int = 30,
    fps: float = 30.0,
    width: int = 320,
    height: int = 320,
) -> str:
    """Create a minimal synthetic MP4 video file in memory/disk for testing."""
    if cv2 is None or np is None:
        raise unittest.SkipTest("OpenCV/numpy is required to generate test video fixtures.")

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    out = cv2.VideoWriter(filepath, fourcc, fps, (width, height))
    for i in range(num_frames):
        # Create gradient image
        frame = np.full((height, width, 3), i % 256, dtype=np.uint8)
        out.write(frame)
    out.release()
    return filepath


class VisualSafetyScheduleTests(unittest.TestCase):
    def test_production_safety_defaults_remain_unchanged(self) -> None:
        self.assertEqual(DEFAULT_SAMPLE_FPS, 4.0)
        self.assertEqual(DEFAULT_PERSON_CONFIDENCE_THRESHOLD, 0.50)
        self.assertEqual(DEFAULT_FACE_CONFIDENCE_THRESHOLD, 0.60)
        self.assertEqual(DEFAULT_ANIMAL_CONFIDENCE_THRESHOLD, 0.35)
        self.assertTrue(ANIMAL_POLICY_READY)
        self.assertEqual(
            ANIMAL_DETECTION_CLASSES,
            ("bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe"),
        )

    def test_sampling_schedule_calculation(self) -> None:
        # 15s video at 30 fps (450 frames) with 4 FPS sampling -> interval 7.5 frames
        schedule = compute_sampling_schedule(total_frames=450, video_fps=30.0, sample_fps=4.0)

        self.assertIn(0, schedule)  # First frame included
        self.assertIn(449, schedule)  # Last frame included
        self.assertGreaterEqual(len(schedule), 60)
        self.assertLessEqual(len(schedule), 62)

        # Monotonicity check
        for i in range(len(schedule) - 1):
            self.assertLess(schedule[i], schedule[i + 1])

    def test_sampling_schedule_single_frame(self) -> None:
        schedule = compute_sampling_schedule(total_frames=1, video_fps=30.0, sample_fps=4.0)
        self.assertEqual(schedule, [0])

    def test_sampling_schedule_invalid_inputs(self) -> None:
        self.assertEqual(compute_sampling_schedule(total_frames=0, video_fps=30.0), [])
        self.assertEqual(compute_sampling_schedule(total_frames=100, video_fps=0.0), [])
        self.assertEqual(compute_sampling_schedule(total_frames=100, video_fps=30.0, sample_fps=0.0), [])

    def test_sampling_schedule_max_frames_limit(self) -> None:
        schedule = compute_sampling_schedule(
            total_frames=450,
            video_fps=30.0,
            sample_fps=4.0,
            max_frames=10,
        )
        self.assertEqual(len(schedule), 10)
        self.assertIn(0, schedule)
        self.assertIn(449, schedule)

    def test_generate_mp_anchors_shape(self) -> None:
        anchors = generate_mp_anchors()
        if anchors is not None:
            self.assertEqual(anchors.shape, (2254, 2))


class VisualSafetyScanTests(unittest.TestCase):
    def setUp(self) -> None:
        if cv2 is None or np is None:
            self.skipTest("OpenCV and numpy required for visual safety tests.")
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.video_path = os.path.join(self.tmp_dir.name, "test_clip.mp4")
        create_synthetic_test_video(self.video_path, num_frames=30, fps=30.0)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_clean_video_with_fake_detectors_passes(self) -> None:
        mock_person_detector = lambda frame: []  # No person
        mock_face_detector = lambda frame: []    # No face

        result = scan_video(
            self.video_path,
            config=SafetyConfig(sample_fps=4.0),
            person_detector=mock_person_detector,
            face_detector=mock_face_detector,
        )

        self.assertTrue(result.safe)
        self.assertEqual(len(result.person_detections), 0)
        self.assertEqual(len(result.face_detections), 0)
        self.assertGreater(result.frames_scanned, 0)
        self.assertAlmostEqual(result.duration_seconds, 1.0, places=1)
        self.assertIsNone(result.failure_reason)
        self.assertGreaterEqual(result.decode_seconds, 0.0)
        self.assertGreaterEqual(result.person_inference_seconds, 0.0)
        self.assertGreaterEqual(result.face_inference_seconds, 0.0)
        self.assertGreaterEqual(result.other_processing_seconds, 0.0)

    def test_runtime_abort_fails_closed_without_reducing_sampling_rate(self) -> None:
        checks = [False, True]

        def abort_check() -> bool:
            return checks.pop(0) if checks else True

        result = scan_video(
            self.video_path,
            config=SafetyConfig(sample_fps=4.0),
            person_detector=lambda frame: [],
            face_detector=lambda frame: [],
            abort_check=abort_check,
        )

        self.assertFalse(result.safe)
        self.assertEqual(result.frames_scanned, 1)
        self.assertIn("runtime budget", result.failure_reason or "")

    def test_person_detected_at_first_frame_rejects_media(self) -> None:
        # Detect person at frame 0
        def fake_person_detector(frame):
            return [(0.85, (50.0, 50.0, 100.0, 200.0))]

        mock_face_detector = lambda frame: []

        result = scan_video(
            self.video_path,
            config=SafetyConfig(sample_fps=4.0),
            person_detector=fake_person_detector,
            face_detector=mock_face_detector,
        )

        self.assertFalse(result.safe)
        self.assertGreater(len(result.person_detections), 0)
        self.assertEqual(result.person_detections[0].detection_type, "person")
        self.assertEqual(result.person_detections[0].frame_index, 0)
        self.assertIn("person", result.failure_reason or "")

    def test_face_detected_without_person_rejects_media(self) -> None:
        mock_person_detector = lambda frame: []

        # Detect face with confidence 0.9
        def fake_face_detector(frame):
            return [(0.90, (100.0, 100.0, 80.0, 80.0))]

        result = scan_video(
            self.video_path,
            config=SafetyConfig(sample_fps=4.0),
            person_detector=mock_person_detector,
            face_detector=fake_face_detector,
        )

        self.assertFalse(result.safe)
        self.assertEqual(len(result.person_detections), 0)
        self.assertGreater(len(result.face_detections), 0)
        self.assertEqual(result.face_detections[0].detection_type, "face")
        self.assertIn("face", result.failure_reason or "")

    def test_required_animal_detection_rejects_obvious_animal_result(self) -> None:
        result = scan_video(
            self.video_path,
            config=SafetyConfig(sample_fps=4.0, animal_detection_required=True),
            person_detector=lambda _frame: [],
            face_detector=lambda _frame: [],
            animal_detector=lambda _frame: [(0.91, (10, 20, 100, 80), 16, "dog")],
        )
        self.assertFalse(result.safe)
        self.assertEqual(result.animal_policy_status, "REJECT")
        self.assertEqual(result.animal_detections[0].class_name, "dog")

    def test_required_animal_detection_allows_clear_nature_result(self) -> None:
        result = scan_video(
            self.video_path,
            config=SafetyConfig(sample_fps=4.0, animal_detection_required=True),
            person_detector=lambda _frame: [],
            face_detector=lambda _frame: [],
            animal_detector=lambda _frame: [],
        )
        self.assertTrue(result.safe)
        self.assertEqual(result.animal_policy_status, "CLEAR")
        self.assertGreaterEqual(result.animal_inference_seconds, 0.0)

    def test_failed_scan_never_reports_animal_policy_clear(self) -> None:
        missing = os.path.join(self.tmp_dir.name, "missing.mp4")
        result = scan_video(
            missing,
            config=SafetyConfig(sample_fps=4.0, animal_detection_required=True),
            person_detector=lambda _frame: [],
            face_detector=lambda _frame: [],
            animal_detector=lambda _frame: [],
        )
        self.assertFalse(result.safe)
        self.assertEqual(result.animal_policy_status, "INCOMPLETE")

    def test_low_confidence_detection_below_threshold_is_ignored(self) -> None:
        # Person detector returns 0.35 (configured threshold is 0.50)
        fake_person_detector = lambda frame: [(0.35, (10.0, 10.0, 50.0, 50.0))]
        # Face detector returns 0.40 (configured threshold is 0.60)
        fake_face_detector = lambda frame: [(0.40, (10.0, 10.0, 50.0, 50.0))]

        result = scan_video(
            self.video_path,
            config=SafetyConfig(
                sample_fps=4.0,
                person_confidence_threshold=0.50,
                face_confidence_threshold=0.60,
            ),
            person_detector=fake_person_detector,
            face_detector=fake_face_detector,
        )

        self.assertTrue(result.safe)
        self.assertEqual(len(result.person_detections), 0)
        self.assertEqual(len(result.face_detections), 0)

    def test_missing_video_file_fails_closed(self) -> None:
        missing_path = os.path.join(self.tmp_dir.name, "non_existent.mp4")
        result = scan_video(
            missing_path,
            person_detector=lambda f: [],
            face_detector=lambda f: [],
        )

        self.assertFalse(result.safe)
        self.assertEqual(result.frames_scanned, 0)
        self.assertIn("not found", result.failure_reason or "")

    def test_zero_byte_video_file_fails_closed(self) -> None:
        empty_path = os.path.join(self.tmp_dir.name, "empty.mp4")
        with open(empty_path, "wb") as f:
            pass

        result = scan_video(
            empty_path,
            person_detector=lambda f: [],
            face_detector=lambda f: [],
        )

        self.assertFalse(result.safe)
        self.assertEqual(result.frames_scanned, 0)
        self.assertIn("empty", result.failure_reason or "")

    def test_corrupt_video_file_fails_closed(self) -> None:
        corrupt_path = os.path.join(self.tmp_dir.name, "corrupt.mp4")
        with open(corrupt_path, "wb") as f:
            f.write(b"not a real mp4 video file data")

        result = scan_video(
            corrupt_path,
            person_detector=lambda f: [],
            face_detector=lambda f: [],
        )

        self.assertFalse(result.safe)
        self.assertIn("failed", (result.failure_reason or "").lower())

    def test_detector_exception_fails_closed(self) -> None:
        def crashing_detector(frame):
            raise RuntimeError("Model execution failed")

        result = scan_video(
            self.video_path,
            config=SafetyConfig(sample_fps=4.0, fail_closed=True),
            person_detector=crashing_detector,
            face_detector=lambda f: [],
        )

        self.assertFalse(result.safe)
        self.assertIn("Detector error", result.failure_reason or "")

    def test_scan_frame_metadata_integrity(self) -> None:
        dummy_frame = np.zeros((100, 100, 3), dtype=np.uint8)
        mock_p = lambda f: [(0.75, (10.0, 20.0, 30.0, 40.0))]
        mock_f = lambda f: [(0.88, (15.0, 25.0, 35.0, 45.0))]

        p_dets, f_dets = scan_frame(
            frame=dummy_frame,
            frame_index=5,
            timestamp_seconds=1.25,
            config=SafetyConfig(person_confidence_threshold=0.5, face_confidence_threshold=0.5),
            person_detector=mock_p,
            face_detector=mock_f,
        )

        self.assertEqual(len(p_dets), 1)
        self.assertEqual(p_dets[0].frame_index, 5)
        self.assertEqual(p_dets[0].timestamp_seconds, 1.25)
        self.assertEqual(p_dets[0].confidence, 0.75)
        self.assertEqual(p_dets[0].bounding_box, (10.0, 20.0, 30.0, 40.0))

        self.assertEqual(len(f_dets), 1)
        self.assertEqual(f_dets[0].frame_index, 5)
        self.assertEqual(f_dets[0].detection_type, "face")


@unittest.skipIf(cv2 is None or np is None, "OpenCV/numpy required.")
class PersonInputContractTests(unittest.TestCase):
    """Exercise the actual detector's tensor boundary with inert inference only."""

    def _input_blob(self, frame):
        class RecordingNet:
            def setInput(self, blob):
                self.blob = blob.copy()

            def getUnconnectedOutLayersNames(self):
                return ("boxes", "scores")

            def forward(self, _names):
                return (
                    np.zeros((1, 2254, 12), dtype=np.float32),
                    np.full((1, 2254, 1), -100.0, dtype=np.float32),
                )

        net = RecordingNet()
        with tempfile.NamedTemporaryFile(suffix=".onnx") as model:
            with patch("engine.safety.cv2.dnn.readNet", return_value=net):
                detector = MPPersonDetector(model.name)
            detector.detect(frame)
        return net.blob

    def test_person_letterbox_uses_zero_in_normalized_model_space(self):
        for height, width, border_pixel in (
            (448, 224, (112, 0)),
            (224, 448, (0, 112)),
        ):
            with self.subTest(shape=(height, width)):
                frame = np.full((height, width, 3), (0, 64, 255), dtype=np.uint8)
                blob = self._input_blob(frame)
                self.assertEqual(blob.shape, (1, 3, 224, 224))
                self.assertEqual(blob.dtype, np.float32)
                y, x = border_pixel
                np.testing.assert_array_equal(blob[0, :, y, x], [0.0, 0.0, 0.0])
                np.testing.assert_allclose(
                    blob[0, :, 112, 112], [1.0, -0.49803922, -1.0], atol=1e-7,
                )

    def test_person_normalizes_before_resize_without_uint8_rounding(self):
        frame = np.zeros((224, 448, 3), dtype=np.uint8)
        frame[:, 1::2] = 255
        blob = self._input_blob(frame)
        # Half-size linear interpolation of adjacent -1/+1 pixels is exactly 0.
        np.testing.assert_allclose(blob[0, :, 112, 112], [0.0, 0.0, 0.0], atol=1e-7)

    def test_square_person_input_preserves_rgb_and_normalization(self):
        frame = np.full((224, 224, 3), (0, 64, 255), dtype=np.uint8)
        blob = self._input_blob(frame)
        np.testing.assert_allclose(
            blob[0, :, 0, 0], [1.0, -0.49803922, -1.0], atol=1e-7,
        )


class RealModelSmokeTests(unittest.TestCase):
    """Optional smoke test using actual downloaded ONNX models if available."""

    def test_real_models_execution_if_present(self) -> None:
        if cv2 is None or np is None:
            self.skipTest("OpenCV and numpy required.")

        face_path = os.path.join(DEFAULT_CACHE_DIR, DEFAULT_FACE_MODEL_FILENAME)
        person_path = os.path.join(DEFAULT_CACHE_DIR, DEFAULT_PERSON_MODEL_FILENAME)
        animal_path = os.path.join(DEFAULT_CACHE_DIR, DEFAULT_ANIMAL_MODEL_FILENAME)

        if not os.path.isfile(face_path) or not os.path.isfile(person_path):
            self.skipTest("ONNX model files not cached locally; skipping smoke test.")

        face_detector = YuNetFaceDetector(face_path, score_threshold=0.6)
        person_detector = MPPersonDetector(person_path, score_threshold=0.5)

        # Blank green nature frame
        nature_frame = np.full((1920, 1080, 3), (34, 139, 34), dtype=np.uint8)

        faces = face_detector.detect(nature_frame)
        persons = person_detector.detect(nature_frame)

        self.assertEqual(len(faces), 0)
        self.assertEqual(len(persons), 0)

        if os.path.isfile(animal_path):
            animals = YoloXAnimalDetector(animal_path).detect(nature_frame)
            self.assertEqual(animals, [])


if __name__ == "__main__":
    unittest.main()
