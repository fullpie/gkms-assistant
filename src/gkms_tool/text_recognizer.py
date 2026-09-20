"""Small PaddleOCR line recognizer using MaaGakumasu's local ONNX assets."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from time import perf_counter

import numpy as np
import onnxruntime as ort
from PIL import Image


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODEL_DIR = (
    PROJECT_ROOT
    / "_research"
    / "MaaGakumasu"
    / "assets"
    / "resource"
    / "base"
    / "model"
    / "ocr"
)


@dataclass(frozen=True, slots=True)
class TextRecognition:
    text: str
    confidence: float
    elapsed_ms: float
    provider: str


def _softmax(values: np.ndarray) -> np.ndarray:
    shifted = values - np.max(values, axis=-1, keepdims=True)
    exponential = np.exp(shifted)
    return exponential / np.sum(exponential, axis=-1, keepdims=True)


def ctc_greedy_decode(
    probabilities: np.ndarray, characters: tuple[str, ...]
) -> tuple[str, float]:
    if probabilities.ndim != 2 or probabilities.shape[1] != len(characters):
        raise ValueError("CTC output shape does not match character dictionary")
    indexes = np.argmax(probabilities, axis=1)
    scores = np.max(probabilities, axis=1)
    selected_chars: list[str] = []
    selected_scores: list[float] = []
    previous = -1
    for index, score in zip(indexes.tolist(), scores.tolist()):
        # PaddleOCR uses index zero as CTC blank.  Repeated non-blank indexes
        # without an intervening blank are one character.
        if index != 0 and index != previous:
            selected_chars.append(characters[index])
            selected_scores.append(float(score))
        previous = index
    confidence = sum(selected_scores) / len(selected_scores) if selected_scores else 0.0
    return "".join(selected_chars), confidence


def _prepare_line(image: Image.Image, *, height: int = 48) -> np.ndarray:
    rgb = image.convert("RGB")
    width, source_height = rgb.size
    if width <= 0 or source_height <= 0:
        raise ValueError("text line dimensions must be positive")
    resized_width = max(8, int(round(height * width / source_height)))
    # Dynamic-width Paddle models still benefit from stride-friendly input.
    padded_width = min(2048, ((resized_width + 7) // 8) * 8)
    resized_width = min(resized_width, padded_width)
    resized = rgb.resize((resized_width, height), Image.Resampling.BICUBIC)
    array = np.zeros((height, padded_width, 3), dtype=np.float32)
    array[:, :resized_width, :] = np.asarray(resized, dtype=np.float32)
    array = array / 255.0
    array = (array - 0.5) / 0.5
    return np.ascontiguousarray(np.transpose(array, (2, 0, 1))[None, ...])


class PaddleLineRecognizer:
    def __init__(self, model_dir: Path = DEFAULT_MODEL_DIR) -> None:
        directory = model_dir.resolve()
        model_path = directory / "rec.onnx"
        keys_path = directory / "keys.txt"
        if not model_path.is_file() or not keys_path.is_file():
            raise FileNotFoundError(f"找不到 Maa OCR 模型：{directory}")
        keys = keys_path.read_text(encoding="utf-8").splitlines()
        # Paddle's standard CTC decoder adds blank at the front and space at
        # the end; 18,708 dictionary lines therefore map to 18,710 outputs.
        self.characters = tuple(["", *keys, " "])
        self.session = ort.InferenceSession(
            str(model_path), providers=["CPUExecutionProvider"]
        )
        self.input_name = self.session.get_inputs()[0].name
        output_classes = self.session.get_outputs()[0].shape[-1]
        if isinstance(output_classes, int) and output_classes != len(self.characters):
            raise ValueError(
                f"OCR dictionary/model mismatch: {len(self.characters)} != {output_classes}"
            )

    @property
    def provider(self) -> str:
        return self.session.get_providers()[0]

    def recognize(self, image: Image.Image) -> TextRecognition:
        tensor = _prepare_line(image)
        started = perf_counter()
        raw = np.asarray(self.session.run(None, {self.input_name: tensor})[0])[0]
        # Some exports return probabilities; others return logits.
        row_sums = np.sum(raw, axis=1)
        probabilities = raw if np.allclose(row_sums, 1.0, atol=1e-3) else _softmax(raw)
        text, confidence = ctc_greedy_decode(probabilities, self.characters)
        return TextRecognition(
            text=text,
            confidence=confidence,
            elapsed_ms=(perf_counter() - started) * 1000,
            provider=self.provider,
        )

    def recognize_path(self, path: Path, crop: tuple[int, int, int, int] | None = None) -> TextRecognition:
        with Image.open(path.resolve()) as image:
            image.load()
            line = image.crop(crop) if crop is not None else image.copy()
        return self.recognize(line)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Maa's PaddleOCR recognizer on one text line")
    parser.add_argument("image", type=Path)
    parser.add_argument("--crop", type=int, nargs=4, metavar=("LEFT", "TOP", "RIGHT", "BOTTOM"))
    arguments = parser.parse_args()
    recognition = PaddleLineRecognizer().recognize_path(
        arguments.image, tuple(arguments.crop) if arguments.crop else None
    )
    print(json.dumps(asdict(recognition), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
