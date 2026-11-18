"""Local visual safety scanning module for person, face, and animal detection.

Nature-query filtering reduces risk, but this module performs mandatory local visual
inspection of downloaded background footage and final rendered Quran Reels.

Fail-closed policy: If any configured safety detector finds a prohibited subject,
or if required scanning fails or is incomplete, the media is marked unsafe.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence, Union

try:
    import cv2
    import numpy as np
except ImportError:
    cv2 = None
    np = None


DEFAULT_SAMPLE_FPS = 4.0
DEFAULT_PERSON_CONFIDENCE_THRESHOLD = 0.5
DEFAULT_FACE_CONFIDENCE_THRESHOLD = 0.6
DEFAULT_ANIMAL_CONFIDENCE_THRESHOLD = 0.35
DEFAULT_ANIMAL_NMS_THRESHOLD = 0.50
DEFAULT_NMS_THRESHOLD = 0.3
DEFAULT_TOP_K = 5000

# OpenCV Zoo YOLOX was validated against explicit dog/cat/bird/horse fixtures
# and a separate clouds/waterfall/ocean/mountain/forest negative matrix. The
# policy only claims the ten COCO animal classes below; it does not claim an
# open-world ability to identify every possible animal species.
ANIMAL_POLICY_READY = True
ANIMAL_POLICY_NOT_READY = "ANIMAL_POLICY_NOT_READY"
ANIMAL_DETECTOR_ID = "opencv-zoo-yolox-2022nov"
ANIMAL_POLICY_VERSION = "coco-common-animals-v1"

COCO_CLASS_NAMES = (
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck",
    "boat", "traffic light", "fire hydrant", "stop sign", "parking meter", "bench",
    "bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra",
    "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
    "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove",
    "skateboard", "surfboard", "tennis racket", "bottle", "wine glass", "cup",
    "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
    "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse",
    "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear", "hair drier",
    "toothbrush",
)
SUPPORTED_ANIMAL_CLASS_IDS = frozenset(range(14, 24))
SUPPORTED_ANIMAL_CLASS_NAMES = tuple(COCO_CLASS_NAMES[index] for index in range(14, 24))
ANIMAL_DETECTION_CLASSES = SUPPORTED_ANIMAL_CLASS_NAMES

DEFAULT_CACHE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    ".cache",
    "cv_models",
)
DEFAULT_FACE_MODEL_FILENAME = "face_detection_yunet_2023mar.onnx"
DEFAULT_PERSON_MODEL_FILENAME = "person_detection_mediapipe_2023mar.onnx"
DEFAULT_ANIMAL_MODEL_FILENAME = "object_detection_yolox_2022nov.onnx"


class SafetyScanError(ValueError):
    """Raised when visual safety scanning configuration or execution encounters a critical error."""


@dataclass(frozen=True)
class SafetyConfig:
    """Configuration for visual safety scanning."""

    sample_fps: float = DEFAULT_SAMPLE_FPS
    person_confidence_threshold: float = DEFAULT_PERSON_CONFIDENCE_THRESHOLD
    face_confidence_threshold: float = DEFAULT_FACE_CONFIDENCE_THRESHOLD
    animal_confidence_threshold: float = DEFAULT_ANIMAL_CONFIDENCE_THRESHOLD
    animal_detection_required: bool = False
    fail_closed: bool = True
    max_frames: Optional[int] = None
    models_dir: Optional[str] = None


@dataclass(frozen=True)
class Detection:
    """A detected prohibited subject in a video frame."""

    frame_index: int
    timestamp_seconds: float
    detection_type: str  # 'person' | 'face' | 'animal'
    confidence: float
    bounding_box: tuple[float, float, float, float]  # (x, y, w, h)
    class_id: Optional[int] = None
    class_name: Optional[str] = None


@dataclass(frozen=True)
class SafetyScanResult:
    """Result of visual safety scan on a video file."""

    safe: bool
    frames_scanned: int
    duration_seconds: float
    person_detections: tuple[Detection, ...]
    face_detections: tuple[Detection, ...]
    failure_reason: Optional[str] = None
    animal_detections: tuple[Detection, ...] = ()
    animal_policy_status: str = "NOT_EVALUATED"
    decode_seconds: float = 0.0
    person_inference_seconds: float = 0.0
    face_inference_seconds: float = 0.0
    animal_inference_seconds: float = 0.0
    other_processing_seconds: float = 0.0


def generate_mp_anchors() -> Any:
    """Generate 2,254 SSD anchor centers for MediaPipe Person Detection model (224x224 input)."""
    if np is None:
        return None
    anchors = []
    # Grid 28x28 (stride 8): 2 anchors per cell -> 1,568 anchors
    for y in range(28):
        for x in range(28):
            for _ in range(2):
                anchors.append([(x + 0.5) / 28.0, (y + 0.5) / 28.0])
    # Grid 14x14 (stride 16): 2 anchors per cell -> 392 anchors
    for y in range(14):
        for x in range(14):
            for _ in range(2):
                anchors.append([(x + 0.5) / 14.0, (y + 0.5) / 14.0])
    # Grid 7x7 (stride 32): 6 anchors per cell -> 294 anchors
    for y in range(7):
        for x in range(7):
            for _ in range(6):
                anchors.append([(x + 0.5) / 7.0, (y + 0.5) / 7.0])
    return np.array(anchors, dtype=np.float32)


class YuNetFaceDetector:
    """YuNet face detector wrapper using OpenCV cv2.FaceDetectorYN."""

    def __init__(
        self,
        model_path: str,
        score_threshold: float = DEFAULT_FACE_CONFIDENCE_THRESHOLD,
        nms_threshold: float = DEFAULT_NMS_THRESHOLD,
        top_k: int = DEFAULT_TOP_K,
    ) -> None:
        if cv2 is None:
            raise RuntimeError("OpenCV (cv2) is required to initialize YuNetFaceDetector.")
        if not os.path.isfile(model_path):
            raise FileNotFoundError(f"YuNet model file not found at: {model_path}")

        self.model_path = model_path
        self.score_threshold = score_threshold
        self.nms_threshold = nms_threshold
        self.top_k = top_k
        self._detector: Optional[Any] = None

    def _get_detector(self, width: int, height: int) -> Any:
        if self._detector is None:
            self._detector = cv2.FaceDetectorYN.create(
                model=self.model_path,
                config="",
                input_size=(width, height),
                score_threshold=self.score_threshold,
                nms_threshold=self.nms_threshold,
                top_k=self.top_k,
            )
        else:
            self._detector.setInputSize((width, height))
        return self._detector

    def detect(
        self,
        frame: Any,
        score_threshold: Optional[float] = None,
    ) -> list[tuple[float, tuple[float, float, float, float]]]:
        """Detect faces in a BGR image frame.

        Returns list of (confidence, (x, y, w, h)).
        """
        if frame is None or getattr(frame, "size", 0) == 0:
            return []

        h, w = frame.shape[:2]
        detector = self._get_detector(w, h)
        thresh = score_threshold if score_threshold is not None else self.score_threshold
        if thresh != self.score_threshold:
            detector.setScoreThreshold(thresh)
            self.score_threshold = thresh

        _, faces = detector.detect(frame)
        if faces is None:
            return []

        results: list[tuple[float, tuple[float, float, float, float]]] = []
        for face in faces:
            x, y, w_box, h_box = float(face[0]), float(face[1]), float(face[2]), float(face[3])
            score = float(face[-1])
            if score >= thresh:
                results.append((score, (x, y, w_box, h_box)))
        return results


class MPPersonDetector:
    """MediaPipe Person Detection (MP-PersonDet) ONNX model runner using OpenCV DNN."""

    def __init__(
        self,
        model_path: str,
        score_threshold: float = DEFAULT_PERSON_CONFIDENCE_THRESHOLD,
        nms_threshold: float = DEFAULT_NMS_THRESHOLD,
        top_k: int = DEFAULT_TOP_K,
    ) -> None:
        if cv2 is None or np is None:
            raise RuntimeError("OpenCV (cv2) and numpy are required to initialize MPPersonDetector.")
        if not os.path.isfile(model_path):
            raise FileNotFoundError(f"MP-PersonDet model file not found at: {model_path}")

        self.model_path = model_path
        self.score_threshold = score_threshold
        self.nms_threshold = nms_threshold
        self.top_k = top_k
        self.net = cv2.dnn.readNet(self.model_path)
        self.anchors = generate_mp_anchors()
        self.input_size = np.array([224, 224], dtype=np.float32)

    def detect(
        self,
        frame: Any,
        score_threshold: Optional[float] = None,
    ) -> list[tuple[float, tuple[float, float, float, float]]]:
        """Detect persons in a BGR image frame.

        Returns list of (confidence, (x, y, w, h)).
        """
        if frame is None or getattr(frame, "size", 0) == 0:
            return []

        conf_thresh = score_threshold if score_threshold is not None else self.score_threshold
        h, w = frame.shape[:2]

        # Match OpenCV Zoo MP-PersonDet: normalize RGB before interpolation
        # and letterbox with zero in model space, not normalized black (-1).
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB).astype(np.float32)
        normalized = ((rgb / 255.0) - 0.5) * 2.0
        ratio = min(224.0 / h, 224.0 / w)
        new_w, new_h = max(1, int(w * ratio)), max(1, int(h * ratio))
        resized = cv2.resize(normalized, (new_w, new_h))

        pad_w = 224 - new_w
        pad_h = 224 - new_h
        pad_left = pad_w // 2
        pad_top = pad_h // 2
        padded = cv2.copyMakeBorder(
            resized,
            pad_top,
            pad_h - pad_top,
            pad_left,
            pad_w - pad_left,
            cv2.BORDER_CONSTANT,
            value=(0, 0, 0),
        )

        blob = np.transpose(padded, [2, 0, 1])[np.newaxis, :, :, :]

        self.net.setInput(blob)
        outs = self.net.forward(self.net.getUnconnectedOutLayersNames())

        if outs[0].shape[-1] == 1:
            raw_scores = outs[0][0, :, 0]
            raw_boxes = outs[1][0, :, :]
        else:
            raw_scores = outs[1][0, :, 0]
            raw_boxes = outs[0][0, :, :]

        # Sigmoid scores
        scores = 1.0 / (1.0 + np.exp(-np.clip(raw_scores.astype(np.float64), -100.0, 100.0)))

        # Box decoding
        scale = max(w, h)
        cxy = raw_boxes[:, :2] / self.input_size
        wh = raw_boxes[:, 2:4] / self.input_size
        xy1 = (cxy - wh / 2.0 + self.anchors) * scale
        xy2 = (cxy + wh / 2.0 + self.anchors) * scale

        pad_bias_x = pad_left / ratio
        pad_bias_y = pad_top / ratio
        boxes = np.zeros((len(self.anchors), 4), dtype=np.float32)
        boxes[:, 0] = xy1[:, 0] - pad_bias_x
        boxes[:, 1] = xy1[:, 1] - pad_bias_y
        boxes[:, 2] = xy2[:, 0] - xy1[:, 0]
        boxes[:, 3] = xy2[:, 1] - xy1[:, 1]

        keep_indices = cv2.dnn.NMSBoxes(
            boxes.tolist(),
            scores.tolist(),
            conf_thresh,
            self.nms_threshold,
            top_k=self.top_k,
        )
        if len(keep_indices) == 0:
            return []

        results: list[tuple[float, tuple[float, float, float, float]]] = []
        flat_indices = np.array(keep_indices).flatten().tolist()
        for idx in flat_indices:
            i = int(idx)
            conf = float(scores[i])
            if conf >= conf_thresh:
                results.append((
                    conf,
                    (float(boxes[i, 0]), float(boxes[i, 1]), float(boxes[i, 2]), float(boxes[i, 3])),
                ))
        return results


class YoloXAnimalDetector:
    """OpenCV Zoo YOLOX COCO detector restricted to explicit animal classes."""

    input_size = (640, 640)

    def __init__(
        self,
        model_path: str,
        score_threshold: float = DEFAULT_ANIMAL_CONFIDENCE_THRESHOLD,
        nms_threshold: float = DEFAULT_ANIMAL_NMS_THRESHOLD,
    ) -> None:
        if cv2 is None or np is None:
            raise RuntimeError("OpenCV (cv2) and numpy are required for animal detection.")
        if not os.path.isfile(model_path):
            raise FileNotFoundError(f"YOLOX animal model file not found at: {model_path}")
        self.score_threshold = float(score_threshold)
        self.nms_threshold = float(nms_threshold)
        self.net = cv2.dnn.readNet(model_path)
        self.strides = (8, 16, 32)
        grids: list[Any] = []
        expanded: list[Any] = []
        for stride in self.strides:
            size = self.input_size[0] // stride
            xv, yv = np.meshgrid(np.arange(size), np.arange(size))
            grid = np.stack((xv, yv), axis=2).reshape(1, -1, 2)
            grids.append(grid)
            expanded.append(np.full((*grid.shape[:2], 1), stride))
        self.grids = np.concatenate(grids, axis=1)
        self.expanded_strides = np.concatenate(expanded, axis=1)

    def _letterbox(self, frame: Any) -> tuple[Any, float]:
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        target_h, target_w = self.input_size
        ratio = min(target_h / rgb.shape[0], target_w / rgb.shape[1])
        resized = cv2.resize(
            rgb,
            (max(1, int(rgb.shape[1] * ratio)), max(1, int(rgb.shape[0] * ratio))),
            interpolation=cv2.INTER_LINEAR,
        ).astype(np.float32)
        padded = np.full((target_h, target_w, 3), 114.0, dtype=np.float32)
        padded[: resized.shape[0], : resized.shape[1]] = resized
        return np.transpose(padded, (2, 0, 1))[np.newaxis, ...], ratio

    def detect(
        self,
        frame: Any,
        score_threshold: Optional[float] = None,
    ) -> list[tuple[float, tuple[float, float, float, float], int, str]]:
        if frame is None or getattr(frame, "size", 0) == 0:
            return []
        threshold = float(score_threshold if score_threshold is not None else self.score_threshold)
        blob, ratio = self._letterbox(frame)
        self.net.setInput(blob)
        outputs = self.net.forward(self.net.getUnconnectedOutLayersNames())
        if not outputs:
            return []
        raw = outputs[0][0].copy()
        raw[:, :2] = (raw[:, :2] + self.grids[0]) * self.expanded_strides[0]
        raw[:, 2:4] = np.exp(raw[:, 2:4]) * self.expanded_strides[0]
        boxes = np.empty_like(raw[:, :4])
        boxes[:, 0] = raw[:, 0] - raw[:, 2] / 2.0
        boxes[:, 1] = raw[:, 1] - raw[:, 3] / 2.0
        boxes[:, 2] = raw[:, 2]
        boxes[:, 3] = raw[:, 3]
        scores = raw[:, 4:5] * raw[:, 5:]
        class_ids = np.argmax(scores, axis=1)
        confidences = np.max(scores, axis=1)
        animal_indices = np.flatnonzero(
            np.isin(class_ids, tuple(SUPPORTED_ANIMAL_CLASS_IDS)) & (confidences >= threshold)
        )
        if len(animal_indices) == 0:
            return []
        animal_boxes = boxes[animal_indices]
        animal_scores = confidences[animal_indices]
        animal_classes = class_ids[animal_indices]
        keep = cv2.dnn.NMSBoxesBatched(
            animal_boxes.tolist(),
            animal_scores.tolist(),
            animal_classes.tolist(),
            threshold,
            self.nms_threshold,
        )
        results: list[tuple[float, tuple[float, float, float, float], int, str]] = []
        for raw_index in np.asarray(keep).reshape(-1).tolist() if len(keep) else []:
            idx = int(raw_index)
            class_id = int(animal_classes[idx])
            box = tuple(float(value / ratio) for value in animal_boxes[idx])
            results.append((
                float(animal_scores[idx]),
                box,  # type: ignore[arg-type]
                class_id,
                COCO_CLASS_NAMES[class_id],
            ))
        return results


def compute_sampling_schedule(
    total_frames: int,
    video_fps: float,
    sample_fps: float = DEFAULT_SAMPLE_FPS,
    max_frames: Optional[int] = None,
) -> list[int]:
    """Compute frame indices to sample based on video fps, target sample fps, and total frames.

    Always includes frame 0 (first frame) and total_frames - 1 (last frame).
    """
    if total_frames <= 0 or video_fps <= 0 or sample_fps <= 0:
        return []

    if total_frames == 1:
        return [0]

    interval = max(1.0, video_fps / sample_fps)
    sampled_indices: set[int] = {0, total_frames - 1}

    curr_frame_float = 0.0
    while True:
        curr_frame_float += interval
        idx = int(round(curr_frame_float))
        if idx >= total_frames - 1:
            break
        sampled_indices.add(idx)

    sorted_indices = sorted(sampled_indices)
    if max_frames is not None and max_frames > 0 and len(sorted_indices) > max_frames:
        sorted_indices = sorted_indices[:max_frames]
        if (total_frames - 1) not in sorted_indices:
            sorted_indices[-1] = total_frames - 1

    return sorted_indices


def _resolve_detectors(
    config: SafetyConfig,
    person_detector: Optional[Any] = None,
    face_detector: Optional[Any] = None,
) -> tuple[Any, Any]:
    """Resolve or instantiate person and face detectors based on config."""
    models_dir = config.models_dir or DEFAULT_CACHE_DIR

    resolved_person = person_detector
    if resolved_person is None:
        person_model_path = os.path.join(models_dir, DEFAULT_PERSON_MODEL_FILENAME)
        resolved_person = MPPersonDetector(
            model_path=person_model_path,
            score_threshold=config.person_confidence_threshold,
        )

    resolved_face = face_detector
    if resolved_face is None:
        face_model_path = os.path.join(models_dir, DEFAULT_FACE_MODEL_FILENAME)
        resolved_face = YuNetFaceDetector(
            model_path=face_model_path,
            score_threshold=config.face_confidence_threshold,
        )

    return resolved_person, resolved_face


def _resolve_animal_detector(
    config: SafetyConfig,
    animal_detector: Optional[Any] = None,
) -> Optional[Any]:
    if not config.animal_detection_required:
        return None
    if not ANIMAL_POLICY_READY:
        raise SafetyScanError(ANIMAL_POLICY_NOT_READY)
    if animal_detector is not None:
        return animal_detector
    models_dir = config.models_dir or DEFAULT_CACHE_DIR
    return YoloXAnimalDetector(
        os.path.join(models_dir, DEFAULT_ANIMAL_MODEL_FILENAME),
        score_threshold=config.animal_confidence_threshold,
    )


def _detect_animals(
    frame: Any,
    *,
    frame_index: int,
    timestamp_seconds: float,
    config: SafetyConfig,
    detector: Any,
    profile_timings: dict[str, float],
) -> list[Detection]:
    started = time.perf_counter()
    try:
        raw_animals = (
            detector.detect(frame, config.animal_confidence_threshold)
            if hasattr(detector, "detect")
            else detector(frame) if callable(detector)
            else None
        )
        if raw_animals is None:
            raise SafetyScanError("Invalid animal detector interface.")
        detections: list[Detection] = []
        for item in raw_animals:
            if not isinstance(item, (tuple, list)) or len(item) < 2:
                continue
            confidence = float(item[0])
            if confidence < config.animal_confidence_threshold:
                continue
            bbox = tuple(float(value) for value in item[1])
            class_id = int(item[2]) if len(item) >= 3 and item[2] is not None else None
            class_name = str(item[3]) if len(item) >= 4 and item[3] is not None else None
            if class_name is not None and class_name not in SUPPORTED_ANIMAL_CLASS_NAMES:
                continue
            detections.append(Detection(
                frame_index=frame_index,
                timestamp_seconds=timestamp_seconds,
                detection_type="animal",
                confidence=confidence,
                bounding_box=bbox,  # type: ignore[arg-type]
                class_id=class_id,
                class_name=class_name,
            ))
        return detections
    finally:
        profile_timings["animal_seconds"] = (
            profile_timings.get("animal_seconds", 0.0) + time.perf_counter() - started
        )


def scan_frame(
    frame: Any,
    frame_index: int = 0,
    timestamp_seconds: float = 0.0,
    config: Optional[SafetyConfig] = None,
    person_detector: Optional[Any] = None,
    face_detector: Optional[Any] = None,
    profile_timings: Optional[dict[str, float]] = None,
) -> tuple[list[Detection], list[Detection]]:
    """Scan a single BGR frame using both person and face detectors.

    Returns (person_detections, face_detections).
    """
    resolved_config = config or SafetyConfig()
    p_detector, f_detector = _resolve_detectors(
        resolved_config,
        person_detector=person_detector,
        face_detector=face_detector,
    )

    person_dets: list[Detection] = []
    face_dets: list[Detection] = []

    # 1. Person detection
    person_started_at = time.perf_counter()
    if hasattr(p_detector, "detect"):
        raw_persons = p_detector.detect(frame, resolved_config.person_confidence_threshold)
    elif callable(p_detector):
        raw_persons = p_detector(frame)
    else:
        raise SafetyScanError("Invalid person detector interface.")

    for item in raw_persons:
        if isinstance(item, (tuple, list)) and len(item) >= 2:
            conf = float(item[0])
            bbox = tuple(float(v) for v in item[1])
            if conf >= resolved_config.person_confidence_threshold:
                person_dets.append(
                    Detection(
                        frame_index=frame_index,
                        timestamp_seconds=timestamp_seconds,
                        detection_type="person",
                        confidence=conf,
                        bounding_box=bbox,  # type: ignore
                    )
                )
    if profile_timings is not None:
        profile_timings["person_seconds"] = (
            profile_timings.get("person_seconds", 0.0)
            + time.perf_counter()
            - person_started_at
        )

    # 2. Face detection
    face_started_at = time.perf_counter()
    if hasattr(f_detector, "detect"):
        raw_faces = f_detector.detect(frame, resolved_config.face_confidence_threshold)
    elif callable(f_detector):
        raw_faces = f_detector(frame)
    else:
        raise SafetyScanError("Invalid face detector interface.")

    for item in raw_faces:
        if isinstance(item, (tuple, list)) and len(item) >= 2:
            conf = float(item[0])
            bbox = tuple(float(v) for v in item[1])
            if conf >= resolved_config.face_confidence_threshold:
                face_dets.append(
                    Detection(
                        frame_index=frame_index,
                        timestamp_seconds=timestamp_seconds,
                        detection_type="face",
                        confidence=conf,
                        bounding_box=bbox,  # type: ignore
                    )
                )
    if profile_timings is not None:
        profile_timings["face_seconds"] = (
            profile_timings.get("face_seconds", 0.0)
            + time.perf_counter()
            - face_started_at
        )

    return person_dets, face_dets


def scan_video(
    video_path: str,
    config: Optional[SafetyConfig] = None,
    person_detector: Optional[Any] = None,
    face_detector: Optional[Any] = None,
    animal_detector: Optional[Any] = None,
    abort_check: Optional[Callable[[], bool]] = None,
) -> SafetyScanResult:
    """Perform local visual safety scan on a video file.

    Extracts sampled frames at configured sample_fps (default 4.0) and runs both
    person and face detection, plus animal detection when explicitly required.
    """
    resolved_config = config or SafetyConfig()
    scan_started_at = time.perf_counter()
    decode_seconds = 0.0
    profile_timings: dict[str, float] = {
        "person_seconds": 0.0,
        "face_seconds": 0.0,
        "animal_seconds": 0.0,
    }

    def _result(
        *,
        safe: bool,
        reason: Optional[str],
        frames: int,
        duration: float,
        person_detections: Sequence[Detection] = (),
        face_detections: Sequence[Detection] = (),
        animal_detections: Sequence[Detection] = (),
        animal_scan_complete: bool = False,
    ) -> SafetyScanResult:
        person_seconds = profile_timings["person_seconds"]
        face_seconds = profile_timings["face_seconds"]
        animal_seconds = profile_timings["animal_seconds"]
        total_seconds = time.perf_counter() - scan_started_at
        other_seconds = max(
            0.0,
            total_seconds - decode_seconds - person_seconds - face_seconds - animal_seconds,
        )
        return SafetyScanResult(
            safe=safe,
            frames_scanned=frames,
            duration_seconds=duration,
            person_detections=tuple(person_detections),
            face_detections=tuple(face_detections),
            failure_reason=reason,
            animal_detections=tuple(animal_detections),
            animal_policy_status=(
                "REJECT" if animal_detections
                else "CLEAR" if resolved_config.animal_detection_required and animal_scan_complete
                else "INCOMPLETE" if resolved_config.animal_detection_required
                else "NOT_EVALUATED"
            ),
            decode_seconds=decode_seconds,
            person_inference_seconds=person_seconds,
            face_inference_seconds=face_seconds,
            animal_inference_seconds=animal_seconds,
            other_processing_seconds=other_seconds,
        )

    def _fail_closed(
        reason: str,
        frames: int = 0,
        duration: float = 0.0,
        person_detections: Sequence[Detection] = (),
        face_detections: Sequence[Detection] = (),
        animal_detections: Sequence[Detection] = (),
    ) -> SafetyScanResult:
        if not resolved_config.fail_closed:
            raise SafetyScanError(reason)
        return _result(
            safe=False,
            reason=reason,
            frames=frames,
            duration=duration,
            person_detections=person_detections,
            face_detections=face_detections,
            animal_detections=animal_detections,
        )

    if not isinstance(video_path, str) or not video_path.strip():
        return _fail_closed("Video path must be a non-empty string.")

    if not os.path.isfile(video_path):
        return _fail_closed(f"Video file not found at: {video_path}")

    try:
        file_size = os.path.getsize(video_path)
    except OSError as err:
        return _fail_closed(f"Unable to read video file size: {err}")

    if file_size <= 0:
        return _fail_closed("Video file is empty (0 bytes).")

    if cv2 is None:
        return _fail_closed("OpenCV (cv2) library is not installed or available.")

    # Initialize detectors early to catch model missing/init errors
    try:
        p_detector, f_detector = _resolve_detectors(
            resolved_config,
            person_detector=person_detector,
            face_detector=face_detector,
        )
        a_detector = _resolve_animal_detector(resolved_config, animal_detector)
    except Exception as err:
        return _fail_closed(f"Visual safety detector initialization failed: {err}")

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return _fail_closed(f"OpenCV failed to open video file: {video_path}")

    try:
        video_fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)

        if video_fps <= 0.0 or total_frames <= 0 or width <= 0 or height <= 0:
            return _fail_closed(
                f"Invalid video metadata: fps={video_fps}, frames={total_frames}, res={width}x{height}."
            )

        duration_seconds = total_frames / video_fps
        schedule = compute_sampling_schedule(
            total_frames=total_frames,
            video_fps=video_fps,
            sample_fps=resolved_config.sample_fps,
            max_frames=resolved_config.max_frames,
        )

        if not schedule:
            return _fail_closed("Sampling schedule produced 0 frames to scan.")

        all_person_detections: list[Detection] = []
        all_face_detections: list[Detection] = []
        all_animal_detections: list[Detection] = []
        scanned_count = 0
        decoded_frame_index = -1

        for frame_idx in schedule:
            if abort_check is not None and abort_check():
                return _fail_closed(
                    "Safety scan aborted by runtime budget.",
                    frames=scanned_count,
                    duration=duration_seconds,
                    person_detections=all_person_detections,
                    face_detections=all_face_detections,
                    animal_detections=all_animal_detections,
                )

            # Decode sequentially and retrieve only the exact 4-FPS schedule
            # frames. Repeated random seeks made compressed Pexels MP4s decode
            # the same GOPs over and over; this preserves the identical sample
            # indices and full-resolution detector input without that cost.
            decode_started_at = time.perf_counter()
            grabbed = True
            while decoded_frame_index < frame_idx:
                grabbed = bool(cap.grab())
                if not grabbed:
                    break
                decoded_frame_index += 1
            ret, frame = cap.retrieve() if grabbed else (False, None)
            decode_seconds += time.perf_counter() - decode_started_at

            if not ret or frame is None or getattr(frame, "size", 0) == 0:
                # If EOF boundary frame failed due to container metadata frame count discrepancy, try penultimate frame
                if frame_idx >= total_frames - 2 and frame_idx > 0:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx - 1)
                    ret, frame = cap.read()
                if not ret or frame is None or getattr(frame, "size", 0) == 0:
                    return _fail_closed(
                        f"Failed to decode frame at index {frame_idx}/{total_frames}.",
                        frames=scanned_count,
                        duration=duration_seconds,
                        person_detections=all_person_detections,
                        face_detections=all_face_detections,
                        animal_detections=all_animal_detections,
                    )

            timestamp = frame_idx / video_fps

            try:
                p_dets, f_dets = scan_frame(
                    frame=frame,
                    frame_index=frame_idx,
                    timestamp_seconds=timestamp,
                    config=resolved_config,
                    person_detector=p_detector,
                    face_detector=f_detector,
                    profile_timings=profile_timings,
                )
            except Exception as err:
                return _fail_closed(
                    f"Detector error at frame {frame_idx}: {err}",
                    frames=scanned_count,
                    duration=duration_seconds,
                    person_detections=all_person_detections,
                    face_detections=all_face_detections,
                    animal_detections=all_animal_detections,
                )

            all_person_detections.extend(p_dets)
            all_face_detections.extend(f_dets)
            if resolved_config.animal_detection_required and a_detector is not None:
                try:
                    all_animal_detections.extend(_detect_animals(
                        frame,
                        frame_index=frame_idx,
                        timestamp_seconds=timestamp,
                        config=resolved_config,
                        detector=a_detector,
                        profile_timings=profile_timings,
                    ))
                except Exception as err:
                    return _fail_closed(
                        f"Animal detector error at frame {frame_idx}: {err}",
                        frames=scanned_count,
                        duration=duration_seconds,
                        person_detections=all_person_detections,
                        face_detections=all_face_detections,
                        animal_detections=all_animal_detections,
                    )
            scanned_count += 1

        is_safe = not all_person_detections and not all_face_detections and not all_animal_detections
        reason = None if is_safe else (
            f"Detected {len(all_person_detections)} person(s), {len(all_face_detections)} face(s), "
            f"and {len(all_animal_detections)} animal(s)."
        )

        return _result(
            safe=is_safe,
            frames=scanned_count,
            duration=duration_seconds,
            person_detections=tuple(all_person_detections),
            face_detections=tuple(all_face_detections),
            animal_detections=tuple(all_animal_detections),
            animal_scan_complete=resolved_config.animal_detection_required,
            reason=reason,
        )
    finally:
        cap.release()
