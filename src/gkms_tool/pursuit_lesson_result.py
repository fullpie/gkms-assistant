"""Fail-closed parser and run-scoped checkpoint for pursuit lesson results."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from PIL import Image

from .run_identity import RunIdentity, RunPaths, paths_for
from .run_shadow import RunShadowState, ShadowObservation, load_run_shadow, save_run_shadow
from .screen_state import scale_canonical_box
from .text_recognizer import PaddleLineRecognizer, TextRecognition

RESULT_REGIONS: Mapping[str, tuple[int, int, int, int]] = {
    "vocal": (90, 795, 205, 850), "dance": (330, 795, 450, 850),
    "visual": (555, 795, 685, 850), "vocal_rank": (80, 690, 200, 795),
    "dance_rank": (300, 690, 450, 795), "visual_rank": (520, 690, 680, 795),
    "vocal_delta": (210, 960, 555, 1015), "dance_delta": (210, 1010, 555, 1065),
    "visual_delta": (210, 1060, 555, 1120),
}
MIN_CONFIDENCE = 0.95
_RANK = re.compile(r"[SABCDEF](?:\+)?")


@dataclass(frozen=True, slots=True)
class PursuitLessonResult:
    vocal: int; dance: int; visual: int
    vocal_delta: int; dance_delta: int; visual_delta: int
    vocal_rank: str; dance_rank: str; visual_rank: str
    confidence: float
    raw_text: Mapping[str, str]

    def to_dict(self) -> dict[str, Any]: return asdict(self)


def _integer(text: str, field: str) -> int:
    matches = re.findall(r"\d+", text)
    if len(matches) != 1:
        raise ValueError(f"{field} OCR is not one integer: {text!r}")
    return int(matches[0])


def parse_pursuit_lesson_result(
    recognized: Mapping[str, TextRecognition],
    *,
    minimum_confidence: float = MIN_CONFIDENCE,
    allow_sparse_deltas: bool = False,
) -> PursuitLessonResult:
    missing = set(RESULT_REGIONS) - set(recognized)
    if missing: raise ValueError(f"missing pursuit result OCR fields: {sorted(missing)}")
    # Only numeric values are state-changing authority.  Rank text is report
    # metadata: require that it parses as a rank, but do not reject otherwise
    # exact Vo/Da/Vi values because a decorative gauge makes one rank glyph
    # low-confidence (for example ``(C)``).  The result is still reconciled
    # against the outer LocalSave before checkpointing.
    numeric_fields = ("vocal", "dance", "visual")
    weak = [key for key in numeric_fields if recognized[key].confidence < minimum_confidence]
    if weak: raise ValueError(f"pursuit result OCR confidence is too low: {sorted(weak)}")
    values = {key: _integer(recognized[key].text, key) for key in ("vocal", "dance", "visual")}
    deltas: dict[str, int] = {}
    reported_delta_fields: list[str] = []
    explicit_sparse_deltas: dict[str, tuple[int, str]] = {}
    for name in ("vocal", "dance", "visual"):
        field = f"{name}_delta"
        result = recognized[field]
        text = result.text.replace(" ", "")
        if allow_sparse_deltas and not text:
            deltas[name] = 0
            continue
        if result.confidence < minimum_confidence:
            raise ValueError(f"pursuit result OCR confidence is too low: {[field]}")
        if "+" not in text and "上昇" not in text and "上升" not in text:
            raise ValueError(f"{name} delta is not a result increase: {text!r}")
        delta = _integer(text, f"{name}_delta")
        deltas[name] = delta
        if allow_sparse_deltas:
            explicit = re.search(r"(?i)(?:^|[^A-Z])(VO|DA|VI)\.?", text)
            if explicit is not None:
                target = {"VO": "vocal", "DA": "dance", "VI": "visual"}[
                    explicit.group(1).upper()
                ]
                if target in explicit_sparse_deltas:
                    raise ValueError(
                        f"sparse pursuit result reports {target} more than once"
                    )
                explicit_sparse_deltas[target] = (delta, field)
        reported_delta_fields.append(field)
    if allow_sparse_deltas and not reported_delta_fields:
        raise ValueError("sparse pursuit result has no reported attribute increase")
    if explicit_sparse_deltas:
        # Single-attribute result text can be horizontally centred and land in
        # the middle OCR strip regardless of the trained attribute.  Its
        # explicit Vo/Da/Vi prefix is the semantic owner; region position is
        # only a fallback when no prefix is present.
        deltas = {"vocal": 0, "dance": 0, "visual": 0}
        for target, (delta, _source_field) in explicit_sparse_deltas.items():
            deltas[target] = delta
    ranks = {}
    for name in ("vocal", "dance", "visual"):
        match = _RANK.search(recognized[f"{name}_rank"].text.upper())
        if match is None: raise ValueError(f"{name} rank OCR is invalid")
        ranks[name] = match.group()
    confidence_fields = (*numeric_fields, *reported_delta_fields)
    return PursuitLessonResult(**values, **{f"{key}_delta": value for key, value in deltas.items()}, **{f"{key}_rank": value for key, value in ranks.items()}, confidence=min(recognized[key].confidence for key in confidence_fields), raw_text={key: recognized[key].text for key in RESULT_REGIONS})


def read_pursuit_lesson_result(
    image: Image.Image,
    recognizer: PaddleLineRecognizer | None = None,
    *,
    minimum_confidence: float = MIN_CONFIDENCE,
    allow_sparse_deltas: bool = False,
) -> PursuitLessonResult:
    recognizer = recognizer or PaddleLineRecognizer()
    recognized = {key: recognizer.recognize(image.crop(scale_canonical_box(box, image.width, image.height))) for key, box in RESULT_REGIONS.items()}
    return parse_pursuit_lesson_result(
        recognized,
        minimum_confidence=minimum_confidence,
        allow_sparse_deltas=allow_sparse_deltas,
    )


def read_pursuit_lesson_result_path(
    path: Path,
    recognizer: PaddleLineRecognizer | None = None,
    *,
    minimum_confidence: float = MIN_CONFIDENCE,
    allow_sparse_deltas: bool = False,
) -> PursuitLessonResult:
    with Image.open(path.resolve()) as image:
        image.load()
        return read_pursuit_lesson_result(
            image,
            recognizer,
            minimum_confidence=minimum_confidence,
            allow_sparse_deltas=allow_sparse_deltas,
        )


def checkpoint_pursuit_lesson_result(
    result: PursuitLessonResult,
    *,
    before: Mapping[str, int],
    captured_at: float,
    evidence_path: str,
    identity: RunIdentity,
    run_paths: RunPaths | None = None,
    additional_values: Mapping[str, int] | None = None,
    authority: Mapping[str, Any] | None = None,
) -> RunShadowState:
    """Atomically apply final stats only when all three visible deltas agree."""
    for key, final, delta in (("vocal", result.vocal, result.vocal_delta), ("dance", result.dance, result.dance_delta), ("visual", result.visual, result.visual_delta)):
        if key not in before or not isinstance(before[key], int) or isinstance(before[key], bool) or before[key] < 0:
            raise ValueError(f"pursuit result requires known final-before {key}")
        if final - delta != before[key]:
            raise ValueError(f"pursuit result {key} final-before/delta mismatch")
    paths = run_paths or paths_for(identity)
    shadow = load_run_shadow(paths.shadow) or RunShadowState(identity.produce_id, identity.character_id, identity.idol_card_id)
    if (shadow.produce_id, shadow.character_id, shadow.idol_card_id) != (identity.produce_id, identity.character_id, identity.idol_card_id):
        raise ValueError("run shadow identity does not match result identity")
    extra = dict(additional_values or {})
    for field, value in extra.items():
        if field in {"vocal", "dance", "visual"}:
            raise ValueError("additional result values cannot replace visible attributes")
        if not isinstance(value, int) or isinstance(value, bool):
            raise TypeError("additional result values must be integers")
    metadata: dict[str, Any] = {
        "before": dict(before),
        "deltas": {
            "vocal": result.vocal_delta,
            "dance": result.dance_delta,
            "visual": result.visual_delta,
        },
        "ranks": {
            "vocal": result.vocal_rank,
            "dance": result.dance_rank,
            "visual": result.visual_rank,
        },
    }
    if authority is not None:
        if not isinstance(authority, Mapping):
            raise TypeError("result authority must be a mapping or None")
        metadata["authority"] = dict(authority)
    observation = ShadowObservation(
        "pursuit_lesson_result",
        captured_at,
        evidence_path,
        result.confidence,
        {
            "vocal": result.vocal,
            "dance": result.dance,
            "visual": result.visual,
            **extra,
        },
        {},
        metadata,
    )
    updated = shadow.apply(observation)
    save_run_shadow(updated, paths.shadow)
    return updated
