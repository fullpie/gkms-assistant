"""Independent durable evidence for exact audition support runtime permils.

The builder is intentionally read-only with respect to the game save.  It
delegates envelope decryption/schema extraction to :mod:`local_save_decoder`,
then binds those decoded values to the authoritative six-card loadout order.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import yaml

from .loadout_runtime_bridge import loadout_snapshot_digest
from .loadout_snapshot import LoadoutSnapshot, SUPPORT_CARD_COUNT
from .local_save_decoder import (
    LocalSaveDecodeError,
    decode_local_save_bytes,
    exam_current_turn_support_state,
    exam_support_card_permils,
)


AUDITION_SUPPORT_RUNTIME_EVIDENCE_SCHEMA_VERSION = 2
SUPPORT_RUNTIME_EVIDENCE_FILENAME = "audition_support_runtime_evidence.json"
EXAM_SAVE_DATA_SOURCE_TYPE = "Campus.InGame.Exam.ExamSaveData"
RUNTIME_PERMIL_EVIDENCE_KIND = "exam-save-data-local-save-v1"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SUPPORT_CARD_MASTER = PROJECT_ROOT / "_research" / "gakumasu-diff" / "SupportCard.yaml"
_PROVENANCE_FILES = (
    PROJECT_ROOT / "src" / "gkms_tool" / "local_save_decoder.py",
    PROJECT_ROOT / "docs" / "local-save-static-decode.md",
)
_SHA256_LENGTH = 64
_LESSON_TYPE_BY_PARAMETER_TYPE = {
    "ProduceParameterType_Unknown": "ProduceStepLessonType_Unknown",
    "ProduceParameterType_Vocal": "ProduceStepLessonType_LessonVocal",
    "ProduceParameterType_Dance": "ProduceStepLessonType_LessonDance",
    "ProduceParameterType_Visual": "ProduceStepLessonType_LessonVisual",
}
_PARAMETER_TYPE_BY_SUPPORT_TYPE = {
    "SupportCardType_Assist": "ProduceParameterType_Unknown",
    "SupportCardType_Vocal": "ProduceParameterType_Vocal",
    "SupportCardType_Dance": "ProduceParameterType_Dance",
    "SupportCardType_Visual": "ProduceParameterType_Visual",
}


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value


def _integer(
    value: object, label: str, *, minimum: int = 0, maximum: int | None = None
) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{label} must be an integer <= {maximum}")
    return value


def _sha256(value: object, label: str) -> str:
    text = _text(value, label)
    if len(text) != _SHA256_LENGTH or any(
        character not in "0123456789abcdef" for character in text
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return text


def _optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _text(value, label)


def _optional_sha256(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _sha256(value, label)


def _exact(payload: Mapping[str, object], fields: set[str], label: str) -> None:
    actual = set(payload)
    if actual != fields:
        raise ValueError(
            f"{label} fields are invalid: "
            f"missing={sorted(fields - actual)} unknown={sorted(actual - fields)}"
        )


def _canonical_json(payload: Mapping[str, object]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def decoder_native_provenance_digest() -> str:
    """Hash the decoder implementation and its recovered-native provenance."""

    files: dict[str, str] = {}
    for path in _PROVENANCE_FILES:
        if not path.is_file():
            raise FileNotFoundError(f"decoder provenance file missing: {path}")
        files[path.relative_to(PROJECT_ROOT).as_posix()] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    return hashlib.sha256(
        _canonical_json({"files": files}).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, order=True, slots=True)
class SupportRuntimePermilEvidenceValue:
    loadout_order: int
    support_id: str
    runtime_permil: int
    support_type: str
    lesson_parameter_type: str
    lesson_type: str
    card_search_id: str

    def __post_init__(self) -> None:
        _integer(self.loadout_order, "loadout_order")
        _text(self.support_id, "support_id")
        _integer(
            self.runtime_permil, "runtime_permil", minimum=0, maximum=1000
        )
        expected_parameter_type = _PARAMETER_TYPE_BY_SUPPORT_TYPE.get(
            _text(self.support_type, "support_type")
        )
        if expected_parameter_type is None:
            raise ValueError(f"unsupported support_type: {self.support_type}")
        if self.lesson_parameter_type != expected_parameter_type:
            raise ValueError("support_type/lesson_parameter_type mismatch")
        expected_lesson_type = _LESSON_TYPE_BY_PARAMETER_TYPE.get(
            _text(self.lesson_parameter_type, "lesson_parameter_type")
        )
        if self.lesson_type != expected_lesson_type:
            raise ValueError("lesson_parameter_type/lesson_type mismatch")
        _text(self.card_search_id, "card_search_id")

    def to_dict(self) -> dict[str, object]:
        return {
            "loadout_order": self.loadout_order,
            "support_id": self.support_id,
            "runtime_permil": self.runtime_permil,
            "support_type": self.support_type,
            "lesson_parameter_type": self.lesson_parameter_type,
            "lesson_type": self.lesson_type,
            "card_search_id": self.card_search_id,
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> "SupportRuntimePermilEvidenceValue":
        if not isinstance(payload, Mapping):
            raise ValueError("support runtime permil value must be an object")
        _exact(
            payload,
            {
                "loadout_order",
                "support_id",
                "runtime_permil",
                "support_type",
                "lesson_parameter_type",
                "lesson_type",
                "card_search_id",
            },
            "support runtime permil value",
        )
        return cls(
            loadout_order=_integer(payload["loadout_order"], "loadout_order"),
            support_id=_text(payload["support_id"], "support_id"),
            runtime_permil=_integer(
                payload["runtime_permil"],
                "runtime_permil",
                maximum=1000,
            ),
            support_type=_text(payload["support_type"], "support_type"),
            lesson_parameter_type=_text(
                payload["lesson_parameter_type"], "lesson_parameter_type"
            ),
            lesson_type=_text(payload["lesson_type"], "lesson_type"),
            card_search_id=_text(payload["card_search_id"], "card_search_id"),
        )


@dataclass(frozen=True, order=True, slots=True)
class SupportRuntimeHandEvidenceValue:
    hand_index: int
    card_id: str
    base_upgrade: int
    effective_upgrade: int
    support_upgrade_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _integer(self.hand_index, "hand_index")
        _text(self.card_id, "card_id")
        _integer(self.base_upgrade, "base_upgrade", maximum=3)
        _integer(self.effective_upgrade, "effective_upgrade", maximum=3)
        support_ids = tuple(self.support_upgrade_ids)
        if any(not isinstance(value, str) or not value.strip() for value in support_ids):
            raise ValueError("support_upgrade_ids must contain support IDs")
        if len(set(support_ids)) != len(support_ids):
            raise ValueError("support_upgrade_ids must be unique")
        if self.effective_upgrade != self.base_upgrade + len(support_ids):
            raise ValueError("Hand support IDs must exactly explain the upgrade delta")
        object.__setattr__(self, "support_upgrade_ids", support_ids)

    def to_dict(self) -> dict[str, object]:
        return {
            "hand_index": self.hand_index,
            "card_id": self.card_id,
            "base_upgrade": self.base_upgrade,
            "effective_upgrade": self.effective_upgrade,
            "support_upgrade_ids": list(self.support_upgrade_ids),
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> "SupportRuntimeHandEvidenceValue":
        if not isinstance(payload, Mapping):
            raise ValueError("support runtime Hand value must be an object")
        _exact(
            payload,
            {
                "hand_index",
                "card_id",
                "base_upgrade",
                "effective_upgrade",
                "support_upgrade_ids",
            },
            "support runtime Hand value",
        )
        raw_support_ids = payload["support_upgrade_ids"]
        if not isinstance(raw_support_ids, list):
            raise ValueError("support_upgrade_ids must be a list")
        return cls(
            hand_index=_integer(payload["hand_index"], "hand_index"),
            card_id=_text(payload["card_id"], "card_id"),
            base_upgrade=_integer(
                payload["base_upgrade"], "base_upgrade", maximum=3
            ),
            effective_upgrade=_integer(
                payload["effective_upgrade"], "effective_upgrade", maximum=3
            ),
            support_upgrade_ids=tuple(raw_support_ids),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class AuditionSupportRuntimeEvidence:
    schema_version: int
    run_id: str
    loadout_snapshot_digest: str
    session_transition_id: str | None
    zone_checkpoint_digest: str | None
    source_path: str
    source_sha256: str
    source_type: str
    envelope_version: int
    support_permils: tuple[SupportRuntimePermilEvidenceValue, ...]
    current_turn: int
    remain_turn: int
    turn_use_support_ids: tuple[str, ...]
    hand: tuple[SupportRuntimeHandEvidenceValue, ...]
    decoder_native_provenance_sha256: str
    support_card_master_sha256: str

    def __post_init__(self) -> None:
        if self.schema_version != AUDITION_SUPPORT_RUNTIME_EVIDENCE_SCHEMA_VERSION:
            raise ValueError("unsupported audition support runtime evidence schema")
        _text(self.run_id, "run_id")
        _sha256(self.loadout_snapshot_digest, "loadout_snapshot_digest")
        _optional_text(self.session_transition_id, "session_transition_id")
        _optional_sha256(self.zone_checkpoint_digest, "zone_checkpoint_digest")
        if (self.session_transition_id is None) != (
            self.zone_checkpoint_digest is None
        ):
            raise ValueError(
                "session_transition_id and zone_checkpoint_digest must be bound together"
            )
        _text(self.source_path, "source_path")
        _sha256(self.source_sha256, "source_sha256")
        if self.source_type != EXAM_SAVE_DATA_SOURCE_TYPE:
            raise ValueError(f"unsupported support runtime source_type: {self.source_type}")
        _integer(self.envelope_version, "envelope_version")
        _sha256(
            self.decoder_native_provenance_sha256,
            "decoder_native_provenance_sha256",
        )
        _sha256(self.support_card_master_sha256, "support_card_master_sha256")
        values = tuple(self.support_permils)
        if len(values) != SUPPORT_CARD_COUNT or not all(
            isinstance(value, SupportRuntimePermilEvidenceValue) for value in values
        ):
            raise ValueError(
                f"support_permils must contain exactly {SUPPORT_CARD_COUNT} values"
            )
        ordered = tuple(sorted(values, key=lambda value: value.loadout_order))
        if tuple(value.loadout_order for value in ordered) != tuple(
            range(SUPPORT_CARD_COUNT)
        ):
            raise ValueError("support_permils must cover loadout_order 0..5 exactly")
        if len({value.support_id for value in ordered}) != SUPPORT_CARD_COUNT:
            raise ValueError("support_permils support IDs must be unique")
        object.__setattr__(self, "support_permils", ordered)
        _integer(self.current_turn, "current_turn", minimum=1)
        _integer(self.remain_turn, "remain_turn")
        used = tuple(self.turn_use_support_ids)
        if any(not isinstance(value, str) or not value.strip() for value in used):
            raise ValueError("turn_use_support_ids must contain support IDs")
        if len(set(used)) != len(used):
            raise ValueError("turn_use_support_ids must be unique")
        unknown_used = set(used) - {value.support_id for value in ordered}
        if unknown_used:
            raise ValueError(
                f"turn_use_support_ids are absent from loadout: {sorted(unknown_used)}"
            )
        object.__setattr__(self, "turn_use_support_ids", used)
        hand = tuple(self.hand)
        if not all(isinstance(value, SupportRuntimeHandEvidenceValue) for value in hand):
            raise TypeError("hand must contain SupportRuntimeHandEvidenceValue values")
        if tuple(value.hand_index for value in hand) != tuple(range(len(hand))):
            raise ValueError("Hand evidence must cover contiguous screen order")
        hand_support_ids = tuple(
            support_id
            for value in hand
            for support_id in value.support_upgrade_ids
        )
        if len(set(hand_support_ids)) != len(hand_support_ids):
            raise ValueError("a support upgrade ID cannot appear twice in Hand")
        if set(hand_support_ids) - set(used):
            raise ValueError("Hand support IDs must be present in turn_use_support_ids")
        object.__setattr__(self, "hand", hand)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "loadout_snapshot_digest": self.loadout_snapshot_digest,
            "session_transition_id": self.session_transition_id,
            "zone_checkpoint_digest": self.zone_checkpoint_digest,
            "source_path": self.source_path,
            "source_sha256": self.source_sha256,
            "source_type": self.source_type,
            "envelope_version": self.envelope_version,
            "support_permils": [value.to_dict() for value in self.support_permils],
            "current_turn": self.current_turn,
            "remain_turn": self.remain_turn,
            "turn_use_support_ids": list(self.turn_use_support_ids),
            "hand": [value.to_dict() for value in self.hand],
            "decoder_native_provenance_sha256": (
                self.decoder_native_provenance_sha256
            ),
            "support_card_master_sha256": self.support_card_master_sha256,
        }

    def canonical_json(self) -> str:
        return _canonical_json(self.to_dict())

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> "AuditionSupportRuntimeEvidence":
        if not isinstance(payload, Mapping):
            raise ValueError("audition support runtime evidence root must be an object")
        if (
            payload.get("schema_version")
            != AUDITION_SUPPORT_RUNTIME_EVIDENCE_SCHEMA_VERSION
        ):
            raise ValueError("unsupported audition support runtime evidence schema")
        fields = {
            "schema_version",
            "run_id",
            "loadout_snapshot_digest",
            "session_transition_id",
            "zone_checkpoint_digest",
            "source_path",
            "source_sha256",
            "source_type",
            "envelope_version",
            "support_permils",
            "current_turn",
            "remain_turn",
            "turn_use_support_ids",
            "hand",
            "decoder_native_provenance_sha256",
            "support_card_master_sha256",
        }
        _exact(payload, fields, "audition support runtime evidence")
        raw_values = payload["support_permils"]
        if not isinstance(raw_values, list) or not all(
            isinstance(value, Mapping) for value in raw_values
        ):
            raise ValueError("support_permils must be a list of objects")
        raw_used = payload["turn_use_support_ids"]
        if not isinstance(raw_used, list):
            raise ValueError("turn_use_support_ids must be a list")
        raw_hand = payload["hand"]
        if not isinstance(raw_hand, list) or not all(
            isinstance(value, Mapping) for value in raw_hand
        ):
            raise ValueError("hand must be a list of objects")
        return cls(
            schema_version=_integer(payload["schema_version"], "schema_version"),
            run_id=_text(payload["run_id"], "run_id"),
            loadout_snapshot_digest=_sha256(
                payload["loadout_snapshot_digest"], "loadout_snapshot_digest"
            ),
            session_transition_id=_optional_text(
                payload["session_transition_id"], "session_transition_id"
            ),
            zone_checkpoint_digest=_optional_sha256(
                payload["zone_checkpoint_digest"], "zone_checkpoint_digest"
            ),
            source_path=_text(payload["source_path"], "source_path"),
            source_sha256=_sha256(payload["source_sha256"], "source_sha256"),
            source_type=_text(payload["source_type"], "source_type"),
            envelope_version=_integer(
                payload["envelope_version"], "envelope_version"
            ),
            support_permils=tuple(
                SupportRuntimePermilEvidenceValue.from_dict(value)
                for value in raw_values
            ),
            current_turn=_integer(
                payload["current_turn"], "current_turn", minimum=1
            ),
            remain_turn=_integer(payload["remain_turn"], "remain_turn"),
            turn_use_support_ids=tuple(raw_used),  # type: ignore[arg-type]
            hand=tuple(
                SupportRuntimeHandEvidenceValue.from_dict(value)
                for value in raw_hand
            ),
            decoder_native_provenance_sha256=_sha256(
                payload["decoder_native_provenance_sha256"],
                "decoder_native_provenance_sha256",
            ),
            support_card_master_sha256=_sha256(
                payload["support_card_master_sha256"],
                "support_card_master_sha256",
            ),
        )


def build_audition_support_runtime_evidence(
    *,
    run_id: str,
    loadout: LoadoutSnapshot,
    source_path: Path,
    source_type: str = EXAM_SAVE_DATA_SOURCE_TYPE,
    support_card_master: Path = DEFAULT_SUPPORT_CARD_MASTER,
    session_transition_id: str | None = None,
    zone_checkpoint_digest: str | None = None,
) -> AuditionSupportRuntimeEvidence:
    """Decode one source file read-only and bind its exact ordered six values."""

    _text(run_id, "run_id")
    if not isinstance(loadout, LoadoutSnapshot):
        raise TypeError("loadout must be LoadoutSnapshot")
    if loadout.run_id != run_id:
        raise ValueError("support runtime evidence run/loadout mismatch")
    if not loadout.is_observation_authoritative():
        raise ValueError("support runtime evidence requires a complete authoritative loadout")
    if source_type != EXAM_SAVE_DATA_SOURCE_TYPE:
        raise ValueError(f"unsupported support runtime source_type: {source_type}")
    _optional_text(session_transition_id, "session_transition_id")
    _optional_sha256(zone_checkpoint_digest, "zone_checkpoint_digest")
    if (session_transition_id is None) != (zone_checkpoint_digest is None):
        raise ValueError(
            "session_transition_id and zone_checkpoint_digest must be bound together"
        )
    expected_ids = tuple(slot.card_id for slot in loadout.support_cards)
    if len(expected_ids) != SUPPORT_CARD_COUNT or any(
        not isinstance(value, str) or not value.strip() for value in expected_ids
    ):
        raise ValueError("loadout must contain six exact support card IDs")
    if len(set(expected_ids)) != SUPPORT_CARD_COUNT:
        raise ValueError("loadout support card IDs must be unique")

    master_path = Path(support_card_master).resolve()
    master_source = master_path.read_bytes()
    master_payload = yaml.safe_load(master_source.decode("utf-8"))
    if not isinstance(master_payload, list):
        raise ValueError("SupportCard.yaml must contain a list")
    master_by_id: dict[str, Mapping[str, object]] = {}
    for row in master_payload:
        if not isinstance(row, Mapping):
            continue
        support_id = row.get("id")
        if support_id in expected_ids:
            if support_id in master_by_id:
                raise ValueError(f"duplicate SupportCard master row: {support_id}")
            master_by_id[str(support_id)] = row
    missing_master_ids = tuple(
        support_id for support_id in expected_ids if support_id not in master_by_id
    )
    if missing_master_ids:
        raise ValueError(
            f"loadout supports missing from SupportCard master: {missing_master_ids!r}"
        )

    target = Path(source_path).resolve()
    source = target.read_bytes()
    envelope = decode_local_save_bytes(source, source_type)
    try:
        decoded = json.loads(envelope.plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LocalSaveDecodeError(
            "decrypted ExamSaveData body is not valid UTF-8 JSON"
        ) from error
    raw_values = exam_support_card_permils(decoded)
    raw_current_turn = exam_current_turn_support_state(decoded)
    if len(raw_values) != SUPPORT_CARD_COUNT:
        raise ValueError(
            f"ExamSaveData must contain exactly {SUPPORT_CARD_COUNT} support cards"
        )

    values: list[SupportRuntimePermilEvidenceValue] = []
    for loadout_order, (expected_id, raw) in enumerate(
        zip(expected_ids, raw_values, strict=True)
    ):
        raw_index = raw.get("index")
        support_id = raw.get("supportCardId")
        runtime_permil = raw.get("produceCardUpgradePermil")
        if raw_index != loadout_order:
            raise ValueError("ExamSaveData support order/index is not contiguous")
        if support_id != expected_id:
            raise ValueError(
                "ExamSaveData support order differs from loadout: "
                f"order={loadout_order};expected={expected_id!r};actual={support_id!r}"
            )
        master_row = master_by_id[expected_id]
        support_type = _text(master_row.get("type"), "SupportCard.type")
        lesson_parameter_type = _text(
            master_row.get("produceCardUpgradeLessonParameterType"),
            "SupportCard.produceCardUpgradeLessonParameterType",
        )
        expected_parameter_type = _PARAMETER_TYPE_BY_SUPPORT_TYPE.get(support_type)
        if expected_parameter_type != lesson_parameter_type:
            raise ValueError(
                f"SupportCard type/upgrade parameter mismatch: {expected_id}"
            )
        lesson_type = _LESSON_TYPE_BY_PARAMETER_TYPE.get(lesson_parameter_type)
        if lesson_type is None:
            raise ValueError(
                f"unsupported SupportCard upgrade parameter: {lesson_parameter_type}"
            )
        card_search_id = _text(
            master_row.get("upgradeProduceCardSearchId"),
            "SupportCard.upgradeProduceCardSearchId",
        )
        values.append(
            SupportRuntimePermilEvidenceValue(
                loadout_order=loadout_order,
                support_id=expected_id,
                runtime_permil=_integer(
                    runtime_permil,
                    f"support[{loadout_order}].runtime_permil",
                    maximum=1000,
                ),
                support_type=support_type,
                lesson_parameter_type=lesson_parameter_type,
                lesson_type=lesson_type,
                card_search_id=card_search_id,
            )
        )
    raw_used_support_ids = raw_current_turn["turnUseSupportCardIdList"]
    assert isinstance(raw_used_support_ids, list)
    unknown_used_support_ids = set(raw_used_support_ids) - set(expected_ids)
    if unknown_used_support_ids:
        raise ValueError(
            "ExamSaveData current-turn support IDs are absent from loadout: "
            f"{sorted(unknown_used_support_ids)}"
        )
    raw_hand = raw_current_turn["handList"]
    assert isinstance(raw_hand, list)
    hand = tuple(
        SupportRuntimeHandEvidenceValue(
            hand_index=_integer(raw["index"], "hand.index"),
            card_id=_text(raw["cardId"], "hand.card_id"),
            base_upgrade=_integer(
                raw["baseUpgradeCount"], "hand.base_upgrade", maximum=3
            ),
            effective_upgrade=_integer(
                raw["effectiveUpgradeCount"],
                "hand.effective_upgrade",
                maximum=3,
            ),
            support_upgrade_ids=tuple(raw["supportUpgradeIdList"]),
        )
        for raw in raw_hand
    )
    return AuditionSupportRuntimeEvidence(
        schema_version=AUDITION_SUPPORT_RUNTIME_EVIDENCE_SCHEMA_VERSION,
        run_id=run_id,
        loadout_snapshot_digest=loadout_snapshot_digest(loadout),
        session_transition_id=session_transition_id,
        zone_checkpoint_digest=zone_checkpoint_digest,
        source_path=str(target),
        source_sha256=hashlib.sha256(source).hexdigest(),
        source_type=source_type,
        envelope_version=envelope.save_data_version,
        support_permils=tuple(values),
        current_turn=_integer(
            raw_current_turn["currentTurn"], "current_turn", minimum=1
        ),
        remain_turn=_integer(raw_current_turn["remainTurn"], "remain_turn"),
        turn_use_support_ids=tuple(raw_used_support_ids),
        hand=hand,
        decoder_native_provenance_sha256=decoder_native_provenance_digest(),
        support_card_master_sha256=hashlib.sha256(master_source).hexdigest(),
    )


def load_audition_support_runtime_evidence(
    path: Path,
) -> AuditionSupportRuntimeEvidence | None:
    target = Path(path)
    if not target.exists():
        return None
    payload = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("audition support runtime evidence root must be an object")
    return AuditionSupportRuntimeEvidence.from_dict(payload)


def save_audition_support_runtime_evidence(
    evidence: AuditionSupportRuntimeEvidence, path: Path
) -> Path:
    if not isinstance(evidence, AuditionSupportRuntimeEvidence):
        raise TypeError("evidence must be AuditionSupportRuntimeEvidence")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(evidence.canonical_json() + "\n", encoding="utf-8")
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return target


__all__ = [
    "AUDITION_SUPPORT_RUNTIME_EVIDENCE_SCHEMA_VERSION",
    "AuditionSupportRuntimeEvidence",
    "EXAM_SAVE_DATA_SOURCE_TYPE",
    "DEFAULT_SUPPORT_CARD_MASTER",
    "RUNTIME_PERMIL_EVIDENCE_KIND",
    "SUPPORT_RUNTIME_EVIDENCE_FILENAME",
    "SupportRuntimePermilEvidenceValue",
    "SupportRuntimeHandEvidenceValue",
    "build_audition_support_runtime_evidence",
    "decoder_native_provenance_digest",
    "load_audition_support_runtime_evidence",
    "save_audition_support_runtime_evidence",
]
