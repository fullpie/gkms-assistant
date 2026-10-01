"""MaaGakumasu YOLOv11 card-region detector.

This module consumes screenshots only.  It does not access the game process.
The bundled model identifies card-shaped regions and classifies their visual
state (normal/recommended/useless); card identity is a separate recognition
step performed on each returned crop.
"""

from __future__ import annotations

import argparse
import ast
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from time import perf_counter
from typing import Iterable

import numpy as np
import onnxruntime as ort
from PIL import Image, ImageDraw


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODEL_PATH = (
    PROJECT_ROOT
    / "_research"
    / "MaaGakumasu"
    / "assets"
    / "resource"
    / "base"
    / "model"
    / "detect"
    / "cards.onnx"
)


@dataclass(frozen=True, slots=True)
class CardDetection:
    label: str
    confidence: float
    x: int
    y: int
    width: int
    height: int

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.width // 2, self.y + self.height // 2


@dataclass(frozen=True, slots=True)
class DetectionReport:
    source: str
    model: str
    provider: str
    elapsed_ms: float
    image_width: int
    image_height: int
    detections: tuple[CardDetection, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            **asdict(self),
            "detections": [asdict(item) for item in self.detections],
        }


def _letterbox(image: Image.Image, size: int = 640) -> tuple[np.ndarray, float, int, int]:
    """Resize with YOLO's aspect-preserving 114-gray padding."""

    rgb = image.convert("RGB")
    width, height = rgb.size
    if width <= 0 or height <= 0:
        raise ValueError("image dimensions must be positive")
    scale = min(size / width, size / height)
    resized_width = max(1, round(width * scale))
    resized_height = max(1, round(height * scale))
    resized = rgb.resize((resized_width, resized_height), Image.Resampling.BILINEAR)
    pad_x = (size - resized_width) // 2
    pad_y = (size - resized_height) // 2
    canvas = Image.new("RGB", (size, size), (114, 114, 114))
    canvas.paste(resized, (pad_x, pad_y))
    array = np.asarray(canvas, dtype=np.float32) / 255.0
    tensor = np.transpose(array, (2, 0, 1))[None, ...]
    return np.ascontiguousarray(tensor), scale, pad_x, pad_y


def _iou(box: np.ndarray, boxes: np.ndarray) -> np.ndarray:
    left = np.maximum(box[0], boxes[:, 0])
    top = np.maximum(box[1], boxes[:, 1])
    right = np.minimum(box[2], boxes[:, 2])
    bottom = np.minimum(box[3], boxes[:, 3])
    intersection = np.maximum(0.0, right - left) * np.maximum(0.0, bottom - top)
    box_area = max(0.0, float(box[2] - box[0])) * max(0.0, float(box[3] - box[1]))
    areas = np.maximum(0.0, boxes[:, 2] - boxes[:, 0]) * np.maximum(
        0.0, boxes[:, 3] - boxes[:, 1]
    )
    return intersection / np.maximum(box_area + areas - intersection, 1e-7)


def _non_max_suppression(
    boxes: np.ndarray,
    scores: np.ndarray,
    classes: np.ndarray,
    *,
    iou_threshold: float,
) -> np.ndarray:
    """Class-aware NMS returning indexes in descending confidence order."""

    kept: list[int] = []
    for class_id in np.unique(classes):
        class_indexes = np.flatnonzero(classes == class_id)
        order = class_indexes[np.argsort(scores[class_indexes])[::-1]]
        while order.size:
            current = int(order[0])
            kept.append(current)
            if order.size == 1:
                break
            remaining = order[1:]
            order = remaining[_iou(boxes[current], boxes[remaining]) <= iou_threshold]
    return np.asarray(sorted(kept, key=lambda index: scores[index], reverse=True), dtype=np.int64)


def _parse_labels(session: ort.InferenceSession) -> tuple[str, ...]:
    raw = session.get_modelmeta().custom_metadata_map.get("names")
    if raw:
        parsed = ast.literal_eval(raw)
        if isinstance(parsed, dict) and all(isinstance(key, int) for key in parsed):
            return tuple(str(parsed[index]) for index in sorted(parsed))
    return ("cards", "recommend", "useless")


class CardDetector:
    def __init__(self, model_path: Path = DEFAULT_MODEL_PATH) -> None:
        self.model_path = model_path.resolve()
        if not self.model_path.is_file():
            raise FileNotFoundError(f"找不到 Maa 卡牌模型：{self.model_path}")
        self.session = ort.InferenceSession(
            str(self.model_path), providers=["CPUExecutionProvider"]
        )
        self.input_name = self.session.get_inputs()[0].name
        self.labels = _parse_labels(self.session)

    @property
    def provider(self) -> str:
        return self.session.get_providers()[0]

    def detect(
        self,
        image: Image.Image,
        *,
        confidence_threshold: float = 0.25,
        iou_threshold: float = 0.45,
    ) -> tuple[CardDetection, ...]:
        if not 0.0 <= confidence_threshold <= 1.0:
            raise ValueError("confidence_threshold must be between 0 and 1")
        if not 0.0 <= iou_threshold <= 1.0:
            raise ValueError("iou_threshold must be between 0 and 1")
        tensor, scale, pad_x, pad_y = _letterbox(image)
        output = self.session.run(None, {self.input_name: tensor})[0]
        predictions = np.asarray(output)[0].T
        if predictions.ndim != 2 or predictions.shape[1] != 4 + len(self.labels):
            raise ValueError(f"unexpected cards.onnx output shape: {output.shape}")

        class_scores = predictions[:, 4:]
        classes = np.argmax(class_scores, axis=1)
        scores = class_scores[np.arange(len(predictions)), classes]
        selected = scores >= confidence_threshold
        if not np.any(selected):
            return ()
        xywh = predictions[selected, :4]
        scores = scores[selected]
        classes = classes[selected]
        boxes = np.empty_like(xywh)
        boxes[:, 0] = xywh[:, 0] - xywh[:, 2] / 2
        boxes[:, 1] = xywh[:, 1] - xywh[:, 3] / 2
        boxes[:, 2] = xywh[:, 0] + xywh[:, 2] / 2
        boxes[:, 3] = xywh[:, 1] + xywh[:, 3] / 2
        keep = _non_max_suppression(
            boxes, scores, classes, iou_threshold=iou_threshold
        )

        image_width, image_height = image.size
        result: list[CardDetection] = []
        for index in keep:
            x1 = float(np.clip((boxes[index, 0] - pad_x) / scale, 0, image_width))
            y1 = float(np.clip((boxes[index, 1] - pad_y) / scale, 0, image_height))
            x2 = float(np.clip((boxes[index, 2] - pad_x) / scale, 0, image_width))
            y2 = float(np.clip((boxes[index, 3] - pad_y) / scale, 0, image_height))
            left, top = round(x1), round(y1)
            right, bottom = round(x2), round(y2)
            if right <= left or bottom <= top:
                continue
            result.append(
                CardDetection(
                    label=self.labels[int(classes[index])],
                    confidence=float(scores[index]),
                    x=left,
                    y=top,
                    width=right - left,
                    height=bottom - top,
                )
            )
        result.sort(key=lambda item: item.x)
        return tuple(result)

    def detect_path(
        self,
        path: Path,
        *,
        confidence_threshold: float = 0.25,
        iou_threshold: float = 0.45,
    ) -> DetectionReport:
        source = path.resolve()
        with Image.open(source) as image:
            image.load()
            started = perf_counter()
            detections = self.detect(
                image,
                confidence_threshold=confidence_threshold,
                iou_threshold=iou_threshold,
            )
            elapsed_ms = (perf_counter() - started) * 1000
            width, height = image.size
        return DetectionReport(
            source=str(source),
            model=str(self.model_path),
            provider=self.provider,
            elapsed_ms=elapsed_ms,
            image_width=width,
            image_height=height,
            detections=detections,
        )


def annotate(source: Path, output: Path, detections: Iterable[CardDetection]) -> None:
    with Image.open(source) as image:
        canvas = image.convert("RGB")
    draw = ImageDraw.Draw(canvas)
    colors = {"cards": "#00E5FF", "recommend": "#38E54D", "useless": "#FF4D6D"}
    for item in detections:
        box = (item.x, item.y, item.x + item.width, item.y + item.height)
        color = colors.get(item.label, "#FFD166")
        draw.rectangle(box, outline=color, width=3)
        draw.text((item.x + 3, max(0, item.y - 14)), f"{item.label} {item.confidence:.2f}", fill=color)
    output.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run MaaGakumasu cards.onnx on one screenshot")
    parser.add_argument("image", type=Path)
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL_PATH)
    parser.add_argument("--confidence", type=float, default=0.25)
    parser.add_argument("--iou", type=float, default=0.45)
    parser.add_argument("--annotate", type=Path)
    arguments = parser.parse_args()
    detector = CardDetector(arguments.model)
    report = detector.detect_path(
        arguments.image,
        confidence_threshold=arguments.confidence,
        iou_threshold=arguments.iou,
    )
    if arguments.annotate is not None:
        annotate(arguments.image, arguments.annotate, report.detections)
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
