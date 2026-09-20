"""Project-local, observation-only logging for Plan 3 training samples.

This module accepts artifacts produced elsewhere.  It never captures a frame,
talks to the Maa controller, sends input, reads process memory, or updates a
Plan 3 session.  Its only side effect is appending one JSON object to a
project-local JSONL dataset and, on first use, creating that dataset's
manifest.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .audition_local_save_state import (
    LocalSaveExamCard,
    LocalSaveExamState,
    parse_local_save_exam_state,
)
from .audition_multiplier_checkpoint import EXPECTED_CAPTURE_METHOD
from .audition_native_transition_verifier import (
    NativeAuditionTransitionDifference,
)
from .plan3_local_save_bridge import (
    Plan3LocalSaveProjection,
    decode_plan3_local_save_bytes,
    project_plan3_local_save,
)
from .run_identity import DEFAULT_RUN_ROOT, MANIFEST_NAME, load_run


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TRAINING_ROOT = PROJECT_ROOT / "var" / "plan3_training"
TRAINING_MANIFEST_SCHEMA_VERSION = 1
TRAINING_RECORD_SCHEMA_VERSION = 1
MANIFEST_FILENAME = "manifest.json"
RECORDS_FILENAME = "records.jsonl"
_MISSING_VALUE = {"missing": True}
_SAFE_DATASET_ID = re.compile(r"^[A-Za-z0-9._-]+$")
LOCAL_SAVE_FORMAT_AUTO = "auto"
LOCAL_SAVE_FORMAT_ENCRYPTED = "encrypted-localsave"
LOCAL_SAVE_FORMAT_DECRYPTED_JSON = "decrypted-json"
_LOCAL_SAVE_FORMATS = frozenset(
    {
        LOCAL_SAVE_FORMAT_AUTO,
        LOCAL_SAVE_FORMAT_ENCRYPTED,
        LOCAL_SAVE_FORMAT_DECRYPTED_JSON,
    }
)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value.strip()


def _integer(value: object, label: str, *, minimum: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} must be at least {minimum}")
    return value


def _json_value(value: object, label: str = "value") -> object:
    """Return a detached strict-JSON value."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{label} cannot contain NaN or infinity")
        return value
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{label} keys must be text")
            result[key] = _json_value(item, f"{label}.{key}")
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            _json_value(item, f"{label}[{index}]")
            for index, item in enumerate(value)
        ]
    raise ValueError(f"{label} contains a non-JSON value: {type(value).__name__}")


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _basename(value: object, label: str) -> str:
    result = _text(value, label)
    if (
        Path(result).name != result
        or result in {".", ".."}
        or _SAFE_DATASET_ID.fullmatch(result) is None
    ):
        raise ValueError(f"{label} must be a single path-safe name")
    return result


@dataclass(frozen=True, slots=True)
class MaaCaptureReference:
    """A reference to an already-created Maa background capture."""

    path: str
    capture_method: str
    timestamp: int | float | None = None
    hwnd: int | None = None
    pid: int | None = None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "MaaCaptureReference":
        if not isinstance(payload, Mapping):
            raise ValueError("capture must be an object")
        allowed = {"png_path", "path", "capture_method", "timestamp", "hwnd", "pid"}
        unknown = set(payload) - allowed
        if unknown:
            raise ValueError(f"unknown capture fields: {sorted(unknown)}")
        raw_path = payload.get("png_path", payload.get("path"))
        target = Path(_text(raw_path, "capture path")).resolve()
        if not target.is_file():
            raise FileNotFoundError(f"capture does not exist: {target}")
        method = _text(payload.get("capture_method"), "capture_method")
        if method != EXPECTED_CAPTURE_METHOD:
            raise ValueError("capture is not an existing Maa background PrintWindow frame")
        timestamp = payload.get("timestamp")
        if timestamp is not None and (
            not isinstance(timestamp, (int, float))
            or isinstance(timestamp, bool)
            or not math.isfinite(float(timestamp))
        ):
            raise ValueError("capture timestamp must be finite numeric data")
        hwnd = payload.get("hwnd")
        pid = payload.get("pid")
        if hwnd is not None:
            hwnd = _integer(hwnd, "capture hwnd", minimum=1)
        if pid is not None:
            pid = _integer(pid, "capture pid", minimum=1)
        return cls(str(target), method, timestamp, hwnd, pid)

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "path": self.path,
            "capture_method": self.capture_method,
        }
        for name in ("timestamp", "hwnd", "pid"):
            value = getattr(self, name)
            if value is not None:
                result[name] = value
        return result


def _card_summary(card: LocalSaveExamCard) -> dict[str, object]:
    return {
        "guid": card.guid,
        "card_id": card.card_id,
        "base_upgrade": card.base_upgrade,
        "temporary_upgrade": card.temporary_upgrade,
        "effective_upgrade": card.effective_upgrade,
        "support_upgrade_ids": list(card.support_upgrade_ids),
    }


def summarize_local_save(state: LocalSaveExamState) -> dict[str, object]:
    """Reduce a parsed decrypted save to stable training-relevant fields."""

    if not isinstance(state, LocalSaveExamState):
        raise TypeError("state must be LocalSaveExamState")
    return {
        "character_id": state.character_id,
        "setting_id": state.setting_id,
        "phase": state.phase,
        "current_turn": state.current_turn,
        "limit_turn": state.limit_turn,
        "remain_turn": state.remain_turn,
        "extra_turn": state.extra_turn,
        "score": state.score,
        "stamina": state.stamina,
        "max_stamina": state.max_stamina,
        "block": state.block,
        "vocal_bonus_permille": state.vocal_bonus_permille,
        "dance_bonus_permille": state.dance_bonus_permille,
        "visual_bonus_permille": state.visual_bonus_permille,
        "turn_card_play_count": state.turn_card_play_count,
        "exam_card_play_count": state.exam_card_play_count,
        "is_turn_card_play_end": state.is_turn_card_play_end,
        "random_state": state.random_state,
        "turn_use_support_ids": list(state.turn_use_support_ids),
        "native_actionable_settled": state.is_native_actionable_settled,
        "zones": {
            name: [_card_summary(card) for card in getattr(state.zones, name)]
            for name in ("hand", "deck", "grave", "lost", "hold")
        },
    }


def load_decrypted_local_save(path: Path) -> LocalSaveExamState:
    """Read a caller-supplied decrypted ExamSaveData JSON file."""

    target = Path(path).resolve()
    payload = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("decrypted LocalSave root must be an object")
    return parse_local_save_exam_state(payload)


def _projection_summary(
    projection: Plan3LocalSaveProjection,
) -> dict[str, object]:
    state = _json_value(projection.state.to_dict(), "plan3_bridge.state")
    assert isinstance(state, dict)
    return {
        "exact": projection.exact,
        "issues": [
            {
                "code": issue.code,
                "field": issue.field,
                "detail": issue.detail,
            }
            for issue in projection.issues
        ],
        "visible_hand": [
            {"card_id": card.card_id, "upgrade": card.upgrade}
            for card in projection.visible_hand
        ],
        "state": state,
    }


@dataclass(frozen=True, slots=True)
class LoadedTrainingLocalSave:
    """One caller-supplied save decoded without modifying its source file."""

    source_path: Path
    source_format: str
    save_data_version: int | None
    state: LocalSaveExamState
    projection: Plan3LocalSaveProjection


def load_training_local_save(
    path: Path,
    *,
    source_format: str = LOCAL_SAVE_FORMAT_AUTO,
) -> LoadedTrainingLocalSave:
    """Load encrypted ``.localsave`` or legacy decrypted JSON read-only.

    Auto mode identifies JSON by its first non-whitespace byte.  Every other
    payload is passed to the existing Plan 3 LocalSave bridge, which uses the
    repository's LocalSave decoder and strict ExamSaveData parser.
    """

    target = Path(path).resolve()
    if not target.is_file():
        raise FileNotFoundError(f"LocalSave input does not exist: {target}")
    if source_format not in _LOCAL_SAVE_FORMATS:
        raise ValueError(
            "local-save format must be auto, encrypted-localsave, or decrypted-json"
        )
    data = target.read_bytes()
    detected = source_format
    if detected == LOCAL_SAVE_FORMAT_AUTO:
        detected = (
            LOCAL_SAVE_FORMAT_DECRYPTED_JSON
            if data.lstrip().startswith(b"{")
            else LOCAL_SAVE_FORMAT_ENCRYPTED
        )
    if detected == LOCAL_SAVE_FORMAT_DECRYPTED_JSON:
        state = load_decrypted_local_save(target)
        version: int | None = None
    else:
        decoded = decode_plan3_local_save_bytes(data)
        state = decoded.exam_state
        version = decoded.envelope.save_data_version
    return LoadedTrainingLocalSave(
        source_path=target,
        source_format=detected,
        save_data_version=version,
        state=state,
        projection=project_plan3_local_save(state),
    )


def _compare_exact(
    expected: object,
    observed: object,
    *,
    path: str,
    code: str,
    projection: bool = False,
) -> tuple[NativeAuditionTransitionDifference, ...]:
    """Create the repository's existing audition difference value objects.

    With ``projection=True``, mapping keys omitted by the prediction are not
    compared.  This lets a prediction name only fields it actually models.
    Lists remain ordered and are compared in full.
    """

    left = _json_value(expected, f"{path}.expected")
    right = _json_value(observed, f"{path}.observed")
    differences: list[NativeAuditionTransitionDifference] = []

    def visit(expected_value: object, observed_value: object, current: str) -> None:
        if type(expected_value) is not type(observed_value):
            differences.append(
                NativeAuditionTransitionDifference(
                    code, current, expected_value, observed_value
                )
            )
            return
        if isinstance(expected_value, dict):
            assert isinstance(observed_value, dict)
            keys = set(expected_value) if projection else set(expected_value) | set(observed_value)
            for key in sorted(keys):
                child = f"{current}.{key}"
                if key not in expected_value:
                    differences.append(
                        NativeAuditionTransitionDifference(
                            code, child, _MISSING_VALUE, observed_value[key]
                        )
                    )
                elif key not in observed_value:
                    differences.append(
                        NativeAuditionTransitionDifference(
                            code, child, expected_value[key], _MISSING_VALUE
                        )
                    )
                else:
                    visit(expected_value[key], observed_value[key], child)
            return
        if isinstance(expected_value, list):
            assert isinstance(observed_value, list)
            for index in range(max(len(expected_value), len(observed_value))):
                child = f"{current}[{index}]"
                if index >= len(expected_value):
                    differences.append(
                        NativeAuditionTransitionDifference(
                            code, child, _MISSING_VALUE, observed_value[index]
                        )
                    )
                elif index >= len(observed_value):
                    differences.append(
                        NativeAuditionTransitionDifference(
                            code, child, expected_value[index], _MISSING_VALUE
                        )
                    )
                else:
                    visit(expected_value[index], observed_value[index], child)
            return
        if expected_value != observed_value:
            differences.append(
                NativeAuditionTransitionDifference(
                    code, current, expected_value, observed_value
                )
            )

    visit(left, right, path)
    return tuple(differences)


def compare_training_values(
    expected: object,
    observed: object,
    *,
    path: str = "state",
    code: str = "plan3-training-replay-mismatch",
    projection: bool = False,
) -> tuple[NativeAuditionTransitionDifference, ...]:
    """Public exact JSON-field diff used by the read-only replay harness."""

    return _compare_exact(
        expected,
        observed,
        path=path,
        code=code,
        projection=projection,
    )


def _action(payload: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        raise ValueError("action must be an object")
    required = {"x", "y", "card_id", "upgrade"}
    optional = {"hand_index"}
    missing = required - set(payload)
    unknown = set(payload) - required - optional
    if missing or unknown:
        raise ValueError(
            f"action fields invalid: missing={sorted(missing)} unknown={sorted(unknown)}"
        )
    result: dict[str, object] = {
        "x": _integer(payload["x"], "action.x", minimum=0),
        "y": _integer(payload["y"], "action.y", minimum=0),
        "card_id": _text(payload["card_id"], "action.card_id"),
        "upgrade": _integer(payload["upgrade"], "action.upgrade", minimum=0),
    }
    if "hand_index" in payload:
        result["hand_index"] = _integer(
            payload["hand_index"], "action.hand_index", minimum=0
        )
    return result


@dataclass(frozen=True, slots=True)
class Plan3TrainingPaths:
    root: Path
    dataset_id: str

    @property
    def directory(self) -> Path:
        return self.root / self.dataset_id

    @property
    def manifest(self) -> Path:
        return self.directory / MANIFEST_FILENAME

    @property
    def records(self) -> Path:
        return self.directory / RECORDS_FILENAME


class Plan3TrainingLogger:
    """Append-only logger bound to one existing run manifest."""

    def __init__(
        self,
        *,
        run_id: str,
        dataset_id: str | None = None,
        training_root: Path = DEFAULT_TRAINING_ROOT,
        run_root: Path = DEFAULT_RUN_ROOT,
    ) -> None:
        self.identity = load_run(_text(run_id, "run_id"), root=Path(run_root))
        chosen_id = _basename(dataset_id or run_id, "dataset_id")
        self.paths = Plan3TrainingPaths(Path(training_root).resolve(), chosen_id)
        self._run_manifest = (Path(run_root).resolve() / run_id / MANIFEST_NAME)
        self._ensure_manifest()

    def _manifest_payload(self) -> dict[str, object]:
        return {
            "schema_version": TRAINING_MANIFEST_SCHEMA_VERSION,
            "dataset_id": self.paths.dataset_id,
            "records_file": RECORDS_FILENAME,
            "run_manifest_path": str(self._run_manifest),
            "run": self.identity.to_dict(),
            "scope": {
                "project_local_only": True,
                "controls_game": False,
                "reads_process_memory": False,
                "captures_frames": False,
                "updates_ui": False,
            },
        }

    def _ensure_manifest(self) -> None:
        self.paths.directory.mkdir(parents=True, exist_ok=True)
        expected = self._manifest_payload()
        if self.paths.manifest.exists():
            current = json.loads(self.paths.manifest.read_text(encoding="utf-8"))
            if current != expected:
                raise ValueError("training dataset manifest differs from requested run")
            return
        if self.paths.records.exists():
            raise ValueError("training records exist without a dataset manifest")
        _atomic_json(self.paths.manifest, expected)

    def _sample_ids(self) -> set[str]:
        if not self.paths.records.exists():
            return set()
        ids: set[str] = set()
        for line_number, line in enumerate(
            self.paths.records.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            payload = json.loads(line)
            if not isinstance(payload, Mapping):
                raise ValueError(f"training JSONL line {line_number} is not an object")
            ids.add(_text(payload.get("sample_id"), f"line {line_number} sample_id"))
        return ids

    def append(
        self,
        *,
        sample_id: str,
        before_capture: Mapping[str, object],
        after_capture: Mapping[str, object],
        action: Mapping[str, object],
        before_state: LocalSaveExamState,
        after_state: LocalSaveExamState,
        before_local_save_path: Path,
        after_local_save_path: Path,
        predicted_post: Mapping[str, object],
        certainty: float,
        uncertain_negative: bool,
        context: Mapping[str, object] | None = None,
        before_local_save_format: str = LOCAL_SAVE_FORMAT_DECRYPTED_JSON,
        after_local_save_format: str = LOCAL_SAVE_FORMAT_DECRYPTED_JSON,
        before_save_data_version: int | None = None,
        after_save_data_version: int | None = None,
    ) -> dict[str, object]:
        """Validate and append one observed action pair."""

        sample_id = _text(sample_id, "sample_id")
        if sample_id in self._sample_ids():
            raise ValueError(f"duplicate training sample_id: {sample_id}")
        pre_capture = MaaCaptureReference.from_mapping(before_capture)
        post_capture = MaaCaptureReference.from_mapping(after_capture)
        if Path(pre_capture.path) == Path(post_capture.path):
            raise ValueError("before and after captures must be distinct files")
        if (
            pre_capture.timestamp is not None
            and post_capture.timestamp is not None
            and float(post_capture.timestamp) <= float(pre_capture.timestamp)
        ):
            raise ValueError("after capture timestamp must follow before capture")
        for identity_field in ("hwnd", "pid"):
            before_identity = getattr(pre_capture, identity_field)
            after_identity = getattr(post_capture, identity_field)
            if (
                before_identity is not None
                and after_identity is not None
                and before_identity != after_identity
            ):
                raise ValueError(
                    f"before and after capture {identity_field} must match"
                )
        if not isinstance(before_state, LocalSaveExamState) or not isinstance(
            after_state, LocalSaveExamState
        ):
            raise TypeError("before_state and after_state must be LocalSaveExamState")
        before_source = Path(before_local_save_path).resolve()
        after_source = Path(after_local_save_path).resolve()
        if not before_source.is_file() or not after_source.is_file():
            raise FileNotFoundError("before/after LocalSave path is missing")
        if before_source == after_source:
            raise ValueError("before and after LocalSave files must be distinct")
        for name, source_format in (
            ("before_local_save_format", before_local_save_format),
            ("after_local_save_format", after_local_save_format),
        ):
            if source_format not in {
                LOCAL_SAVE_FORMAT_ENCRYPTED,
                LOCAL_SAVE_FORMAT_DECRYPTED_JSON,
            }:
                raise ValueError(f"{name} is unsupported: {source_format!r}")
        for name, version, source_format in (
            (
                "before_save_data_version",
                before_save_data_version,
                before_local_save_format,
            ),
            (
                "after_save_data_version",
                after_save_data_version,
                after_local_save_format,
            ),
        ):
            if version is not None:
                _integer(version, name)
            if source_format == LOCAL_SAVE_FORMAT_ENCRYPTED and version is None:
                raise ValueError(f"{name} is required for encrypted LocalSave input")
        if not isinstance(predicted_post, Mapping) or not predicted_post:
            raise ValueError("predicted_post must be a non-empty summary projection")
        prediction = _json_value(predicted_post, "predicted_post")
        assert isinstance(prediction, dict)
        if (
            not isinstance(certainty, (int, float))
            or isinstance(certainty, bool)
            or not math.isfinite(float(certainty))
            or not 0.0 <= float(certainty) <= 1.0
        ):
            raise ValueError("certainty must be a number from 0 to 1")
        if not isinstance(uncertain_negative, bool):
            raise ValueError("uncertain_negative must be boolean")

        normalized_action = _action(action)
        selected_cards = [
            card
            for card in before_state.zones.hand
            if card.card_id == normalized_action["card_id"]
            and card.effective_upgrade == normalized_action["upgrade"]
        ]
        if "hand_index" in normalized_action:
            hand_index = normalized_action["hand_index"]
            assert isinstance(hand_index, int)
            if hand_index >= len(before_state.zones.hand):
                raise ValueError("action.hand_index is outside the observed before hand")
            selected = before_state.zones.hand[hand_index]
            if (
                selected.card_id != normalized_action["card_id"]
                or selected.effective_upgrade != normalized_action["upgrade"]
            ):
                raise ValueError(
                    "action card_id/upgrade does not match the observed hand_index"
                )
        elif not selected_cards:
            raise ValueError(
                "action card_id/upgrade is absent from the observed before hand"
            )

        before_summary = summarize_local_save(before_state)
        after_summary = summarize_local_save(after_state)
        before_projection = _projection_summary(
            project_plan3_local_save(before_state)
        )
        after_projection = _projection_summary(
            project_plan3_local_save(after_state)
        )
        observed_changes = _compare_exact(
            before_summary,
            after_summary,
            path="local_save",
            code="observed-state-change",
        )
        composite_prediction = bool(
            {"local_save", "plan3"}.intersection(prediction)
        )
        prediction_target: object = (
            {"local_save": after_summary, "plan3": after_projection}
            if composite_prediction
            else after_summary
        )
        prediction_differences = _compare_exact(
            prediction,
            prediction_target,
            path="post",
            code="plan3-training-prediction-mismatch",
            projection=True,
        )
        record: dict[str, object] = {
            "schema_version": TRAINING_RECORD_SCHEMA_VERSION,
            "sample_id": sample_id,
            "run_id": self.identity.run_id,
            "captures": {
                "before": pre_capture.to_dict(),
                "after": post_capture.to_dict(),
            },
            "action": normalized_action,
            "local_save": {
                "before_source_path": str(before_source),
                "after_source_path": str(after_source),
                "before_source": {
                    "path": str(before_source),
                    "format": before_local_save_format,
                    "save_data_version": before_save_data_version,
                },
                "after_source": {
                    "path": str(after_source),
                    "format": after_local_save_format,
                    "save_data_version": after_save_data_version,
                },
                "before": before_summary,
                "after": after_summary,
                "observed_changes": [item.to_dict() for item in observed_changes],
            },
            "plan3_bridge": {
                "before": before_projection,
                "after": after_projection,
            },
            "prediction": {
                "post": prediction,
                "target": (
                    "local-save-and-plan3-bridge"
                    if composite_prediction
                    else "local-save-summary"
                ),
                "differences": [item.to_dict() for item in prediction_differences],
                "matches_observation": not prediction_differences,
            },
            "labels": {
                "certainty": float(certainty),
                "uncertain_negative": uncertain_negative,
            },
            "context": _json_value(context or {}, "context"),
        }
        encoded = json.dumps(
            record, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
        with self.paths.records.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded + "\n")
        return record

    def append_historical_observation_pair(
        self,
        *,
        sample_id: str,
        before: LoadedTrainingLocalSave,
        after: LoadedTrainingLocalSave,
        observed_action: Mapping[str, object] | None = None,
        predicted_post: Mapping[str, object] | None = None,
        reconstruction: Mapping[str, object] | None = None,
        gaps: Sequence[str] = (),
        context: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Append a LocalSave-only historical pair without inventing evidence.

        Unlike :meth:`append`, this entry point does not require captures,
        coordinates, labels, or a historical prediction.  Missing evidence is
        represented explicitly.  A caller may attach a separately labelled
        current-engine reconstruction, but it never becomes the historical
        ``prediction`` verdict.
        """

        sample_id = _text(sample_id, "sample_id")
        if sample_id in self._sample_ids():
            raise ValueError(f"duplicate training sample_id: {sample_id}")
        if not isinstance(before, LoadedTrainingLocalSave) or not isinstance(
            after, LoadedTrainingLocalSave
        ):
            raise TypeError("before and after must be LoadedTrainingLocalSave")
        if before.source_path == after.source_path:
            raise ValueError("before and after LocalSave files must be distinct")
        normalized_gaps = tuple(dict.fromkeys(_text(value, "gap") for value in gaps))
        before_summary = summarize_local_save(before.state)
        after_summary = summarize_local_save(after.state)
        before_projection = _projection_summary(before.projection)
        after_projection = _projection_summary(after.projection)
        observed_changes = _compare_exact(
            before_summary,
            after_summary,
            path="local_save",
            code="observed-state-change",
        )

        prediction_record: dict[str, object]
        if predicted_post is None:
            prediction_record = {
                "status": "missing-historical-prediction",
                "post": None,
                "target": None,
                "differences": None,
                "matches_observation": None,
            }
        else:
            prediction = _json_value(predicted_post, "predicted_post")
            if not isinstance(prediction, dict) or not prediction:
                raise ValueError("predicted_post must be a non-empty mapping")
            composite = bool({"local_save", "plan3"}.intersection(prediction))
            target: object = (
                {"local_save": after_summary, "plan3": after_projection}
                if composite
                else after_summary
            )
            differences = _compare_exact(
                prediction,
                target,
                path="post",
                code="plan3-training-prediction-mismatch",
                projection=True,
            )
            prediction_record = {
                "status": "historical-prediction-present",
                "post": prediction,
                "target": (
                    "local-save-and-plan3-bridge"
                    if composite
                    else "local-save-summary"
                ),
                "differences": [item.to_dict() for item in differences],
                "matches_observation": not differences,
            }

        record: dict[str, object] = {
            "schema_version": TRAINING_RECORD_SCHEMA_VERSION,
            "record_kind": "historical-local-save-pair",
            "sample_id": sample_id,
            "run_id": self.identity.run_id,
            "captures": {
                "status": "missing",
                "before": None,
                "after": None,
            },
            "action": (
                {"status": "missing"}
                if observed_action is None
                else {
                    "status": "observed-from-local-save",
                    **_json_mapping(observed_action, "observed_action"),
                }
            ),
            "local_save": {
                "before_source_path": str(before.source_path),
                "after_source_path": str(after.source_path),
                "before_source": {
                    "path": str(before.source_path),
                    "format": before.source_format,
                    "save_data_version": before.save_data_version,
                },
                "after_source": {
                    "path": str(after.source_path),
                    "format": after.source_format,
                    "save_data_version": after.save_data_version,
                },
                "before": before_summary,
                "after": after_summary,
                "observed_changes": [item.to_dict() for item in observed_changes],
            },
            "plan3_bridge": {
                "before": before_projection,
                "after": after_projection,
            },
            "prediction": prediction_record,
            "reconstruction": (
                None
                if reconstruction is None
                else _json_mapping(reconstruction, "reconstruction")
            ),
            "labels": {
                "certainty": None,
                "uncertain_negative": None,
            },
            "gaps": list(normalized_gaps),
            "context": _json_value(context or {}, "context"),
        }
        encoded = json.dumps(
            record, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
        with self.paths.records.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded + "\n")
        return record


def _mapping(payload: object, label: str) -> Mapping[str, object]:
    if not isinstance(payload, Mapping):
        raise ValueError(f"{label} must be an object")
    return payload


def _json_mapping(payload: Mapping[str, object], label: str) -> dict[str, object]:
    value = _json_value(payload, label)
    if not isinstance(value, dict):
        raise AssertionError(f"{label} did not normalize to an object")
    return value


def _optional_format(spec: Mapping[str, object], field: str) -> str:
    value = spec.get(field, LOCAL_SAVE_FORMAT_AUTO)
    return _text(value, field)


def record_from_spec(
    spec_path: Path,
    *,
    training_root: Path = DEFAULT_TRAINING_ROOT,
    run_root: Path = DEFAULT_RUN_ROOT,
) -> tuple[Plan3TrainingLogger, dict[str, object]]:
    """Record one sample from a JSON spec; intended for the thin CLI."""

    spec_file = Path(spec_path).resolve()
    raw = json.loads(spec_file.read_text(encoding="utf-8"))
    spec = _mapping(raw, "spec")
    before_path = Path(_text(spec.get("before_local_save_path"), "before_local_save_path"))
    after_path = Path(_text(spec.get("after_local_save_path"), "after_local_save_path"))
    if not before_path.is_absolute():
        before_path = spec_file.parent / before_path
    if not after_path.is_absolute():
        after_path = spec_file.parent / after_path
    before_loaded = load_training_local_save(
        before_path,
        source_format=_optional_format(spec, "before_local_save_format"),
    )
    after_loaded = load_training_local_save(
        after_path,
        source_format=_optional_format(spec, "after_local_save_format"),
    )
    logger = Plan3TrainingLogger(
        run_id=_text(spec.get("run_id"), "run_id"),
        dataset_id=(
            None
            if spec.get("dataset_id") is None
            else _text(spec.get("dataset_id"), "dataset_id")
        ),
        training_root=training_root,
        run_root=run_root,
    )
    labels = _mapping(spec.get("labels"), "labels")
    record = logger.append(
        sample_id=_text(spec.get("sample_id"), "sample_id"),
        before_capture=_mapping(spec.get("before_capture"), "before_capture"),
        after_capture=_mapping(spec.get("after_capture"), "after_capture"),
        action=_mapping(spec.get("action"), "action"),
        before_state=before_loaded.state,
        after_state=after_loaded.state,
        before_local_save_path=before_loaded.source_path,
        after_local_save_path=after_loaded.source_path,
        predicted_post=_mapping(spec.get("predicted_post"), "predicted_post"),
        certainty=labels.get("certainty"),  # type: ignore[arg-type]
        uncertain_negative=labels.get("uncertain_negative"),  # type: ignore[arg-type]
        context=(
            None
            if spec.get("context") is None
            else _mapping(spec.get("context"), "context")
        ),
        before_local_save_format=before_loaded.source_format,
        after_local_save_format=after_loaded.source_format,
        before_save_data_version=before_loaded.save_data_version,
        after_save_data_version=after_loaded.save_data_version,
    )
    return logger, record


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Append one observation-only Plan 3 training sample."
    )
    parser.add_argument("spec", type=Path, help="JSON sample spec")
    parser.add_argument("--training-root", type=Path, default=DEFAULT_TRAINING_ROOT)
    parser.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    args = parser.parse_args(argv)
    logger, record = record_from_spec(
        args.spec, training_root=args.training_root, run_root=args.run_root
    )
    print(
        json.dumps(
            {
                "sample_id": record["sample_id"],
                "records_path": str(logger.paths.records),
                "matches_observation": record["prediction"]["matches_observation"],  # type: ignore[index]
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DEFAULT_TRAINING_ROOT",
    "LOCAL_SAVE_FORMAT_AUTO",
    "LOCAL_SAVE_FORMAT_DECRYPTED_JSON",
    "LOCAL_SAVE_FORMAT_ENCRYPTED",
    "LoadedTrainingLocalSave",
    "MaaCaptureReference",
    "Plan3TrainingLogger",
    "Plan3TrainingPaths",
    "compare_training_values",
    "load_decrypted_local_save",
    "load_training_local_save",
    "record_from_spec",
    "summarize_local_save",
]
