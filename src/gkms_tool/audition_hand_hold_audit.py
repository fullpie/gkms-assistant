"""Run-bound proof for the native ``ResetHand``/``HandHold`` boundary.

The native client does not always discard the visible Hand at the turn
boundary: ``ExamCardMoveController.ResetHand`` first asks the status
collection whether ``HandHold`` is active.  Treating that value as an implicit
zero corrupts both card conservation and future support-card upgrade rolls.

This module proves only the safe zero case.  It walks every current source
that can install an exam effect (all static upgrades of every deck/created
card, equipped items, selected memory/support passives, already installed
status enchants, and the current audition gimmick binding).  Missing rows or
an actual ``ExamHandHold`` effect block publication.  A card-search whose ID
contains ``hand-hold`` is recorded separately: it moves selected cards to the
hold zone and is not the global status queried by ``IsHandHold``.

No process memory, network interception, capture, or game input is used.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from .audition_multiplier_checkpoint import load_multiplier_checkpoint
from .contextual_passive_step import load_contextual_passive_step_session
from .equipped_item_snapshot import load_equipped_item_snapshot
from .exam_session import SESSION_SCHEMA_VERSION, load_exam_session
from .initial_modifier_reconciliation import (
    load_initial_modifier_reconciliation,
)
from .loadout_runtime_bridge import loadout_snapshot_digest
from .loadout_snapshot import load_loadout_snapshot
from .master_db import DEFAULT_DATABASE
from .passive_catalog import DEFAULT_MASTER_DIR, MasterPassiveCatalog
from .run_deck_snapshot import load_run_deck_snapshot
from .run_identity import DEFAULT_RUN_ROOT, RunIdentity, paths_for


AUDITION_HAND_HOLD_AUDIT_SCHEMA_VERSION = 1
HAND_HOLD_EFFECT_TYPE = "ProduceExamEffectType_ExamHandHold"
EXAM_STATUS_ENCHANT_EFFECT_TYPE = "ProduceExamEffectType_ExamStatusEnchant"
PRODUCE_STATUS_ENCHANT_EFFECT_TYPE = "ProduceEffectType_ExamStatusEnchant"
CARD_MOVE_EFFECT_TYPE = "ProduceExamEffectType_ExamCardMove"
HOLD_ZONE_TOKEN = "hand-hold"
DEFAULT_AUDITION_DIFFICULTY = (
    Path(__file__).resolve().parents[2]
    / "_research"
    / "gakumasu-diff"
    / "ProduceStepAuditionDifficulty.yaml"
)


def _canonical_json(payload: Mapping[str, object]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _canonical_digest(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _strict_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value


def _plain_text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text")
    return value


def _strict_sha256(value: object, label: str) -> str:
    text = _strict_text(value, label)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return text


def _strict_non_negative(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{label} must be an integer >= 0")
    return value


def _deduplicate(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


@dataclass(frozen=True, order=True, slots=True)
class AuditedExamEffect:
    effect_id: str
    effect_type: str
    status_enchant_id: str
    chain_effect_id: str
    source_roots: tuple[str, ...]

    def __post_init__(self) -> None:
        _strict_text(self.effect_id, "effect_id")
        _strict_text(self.effect_type, "effect_type")
        if not isinstance(self.status_enchant_id, str):
            raise ValueError("status_enchant_id must be text")
        if not isinstance(self.chain_effect_id, str):
            raise ValueError("chain_effect_id must be text")
        roots = tuple(sorted(set(self.source_roots)))
        if not roots or any(not isinstance(value, str) or not value for value in roots):
            raise ValueError("source_roots must contain non-empty text")
        object.__setattr__(self, "source_roots", roots)

    def to_dict(self) -> dict[str, object]:
        return {
            "effect_id": self.effect_id,
            "effect_type": self.effect_type,
            "status_enchant_id": self.status_enchant_id,
            "chain_effect_id": self.chain_effect_id,
            "source_roots": list(self.source_roots),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "AuditedExamEffect":
        if set(payload) != {
            "effect_id",
            "effect_type",
            "status_enchant_id",
            "chain_effect_id",
            "source_roots",
        }:
            raise ValueError("audited exam effect fields are invalid")
        roots = payload["source_roots"]
        if not isinstance(roots, list):
            raise ValueError("source_roots must be an array")
        return cls(
            effect_id=_strict_text(payload["effect_id"], "effect_id"),
            effect_type=_strict_text(payload["effect_type"], "effect_type"),
            status_enchant_id=_plain_text(
                payload["status_enchant_id"], "status_enchant_id"
            ),
            chain_effect_id=_plain_text(
                payload["chain_effect_id"], "chain_effect_id"
            ),
            source_roots=tuple(str(value) for value in roots),
        )


@dataclass(frozen=True, slots=True)
class AuditionHandHoldAudit:
    """A complete, canonical proof that the current HandHold count is zero."""

    schema_version: int
    run_id: str
    step_context_id: str
    step_context_digest: str
    session_transition_id: str
    round_number: int
    battle_config_id: str
    gimmick_effect_group_id: str
    input_digests: tuple[tuple[str, str], ...]
    card_variants: tuple[str, ...]
    item_ids: tuple[str, ...]
    passive_source_keys: tuple[str, ...]
    produce_effect_ids: tuple[str, ...]
    status_enchant_ids: tuple[str, ...]
    active_runtime_status_ids: tuple[str, ...]
    created_card_refs: tuple[str, ...]
    exam_effects: tuple[AuditedExamEffect, ...]
    excluded_hold_zone_move_effect_ids: tuple[str, ...]
    hand_hold_effect_ids: tuple[str, ...]
    hand_hold_count: int

    def __post_init__(self) -> None:
        if self.schema_version != AUDITION_HAND_HOLD_AUDIT_SCHEMA_VERSION:
            raise ValueError("unsupported audition HandHold audit schema")
        for label in (
            "run_id",
            "step_context_id",
            "session_transition_id",
            "battle_config_id",
        ):
            _strict_text(getattr(self, label), label)
        _strict_sha256(self.step_context_digest, "step_context_digest")
        _strict_non_negative(self.round_number, "round_number")
        if self.round_number < 1:
            raise ValueError("round_number must be >= 1")
        if not isinstance(self.gimmick_effect_group_id, str):
            raise ValueError("gimmick_effect_group_id must be text")
        if self.gimmick_effect_group_id:
            raise ValueError("zero HandHold proof does not support a gimmick group")
        if self.hand_hold_count != 0:
            raise ValueError("this artifact proves only hand_hold_count=0")
        if self.hand_hold_effect_ids:
            raise ValueError("zero HandHold proof contains a HandHold effect")
        names: list[str] = []
        for name, digest in self.input_digests:
            names.append(_strict_text(name, "input digest name"))
            _strict_sha256(digest, f"input digest:{name}")
        if len(names) != len(set(names)) or tuple(names) != tuple(sorted(names)):
            raise ValueError("input_digests must be uniquely sorted by name")
        for field_name in (
            "card_variants",
            "item_ids",
            "passive_source_keys",
            "produce_effect_ids",
            "status_enchant_ids",
            "active_runtime_status_ids",
            "created_card_refs",
            "excluded_hold_zone_move_effect_ids",
            "hand_hold_effect_ids",
        ):
            values = tuple(getattr(self, field_name))
            if values != tuple(sorted(set(values))):
                raise ValueError(f"{field_name} must be uniquely sorted")
            if any(not isinstance(value, str) or not value for value in values):
                raise ValueError(f"{field_name} must contain non-empty text")
        if not self.card_variants:
            raise ValueError("zero HandHold proof requires audited card variants")
        if not self.item_ids:
            raise ValueError("zero HandHold proof requires audited equipped items")
        if not self.passive_source_keys:
            raise ValueError("zero HandHold proof requires audited passives")
        if not all(isinstance(value, AuditedExamEffect) for value in self.exam_effects):
            raise TypeError("exam_effects must contain AuditedExamEffect values")
        effect_ids = tuple(value.effect_id for value in self.exam_effects)
        if effect_ids != tuple(sorted(set(effect_ids))):
            raise ValueError("exam_effects must be uniquely sorted by effect_id")
        actual_hold_moves = tuple(
            sorted(
                value.effect_id
                for value in self.exam_effects
                if value.effect_type == CARD_MOVE_EFFECT_TYPE
                and HOLD_ZONE_TOKEN in value.effect_id.lower()
            )
        )
        if actual_hold_moves != self.excluded_hold_zone_move_effect_ids:
            raise ValueError("excluded hold-zone moves do not match audited effects")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "step_context_id": self.step_context_id,
            "step_context_digest": self.step_context_digest,
            "session_transition_id": self.session_transition_id,
            "round_number": self.round_number,
            "battle_config_id": self.battle_config_id,
            "gimmick_effect_group_id": self.gimmick_effect_group_id,
            "input_digests": {key: value for key, value in self.input_digests},
            "card_variants": list(self.card_variants),
            "item_ids": list(self.item_ids),
            "passive_source_keys": list(self.passive_source_keys),
            "produce_effect_ids": list(self.produce_effect_ids),
            "status_enchant_ids": list(self.status_enchant_ids),
            "active_runtime_status_ids": list(self.active_runtime_status_ids),
            "created_card_refs": list(self.created_card_refs),
            "exam_effects": [value.to_dict() for value in self.exam_effects],
            "excluded_hold_zone_move_effect_ids": list(
                self.excluded_hold_zone_move_effect_ids
            ),
            "hand_hold_effect_ids": list(self.hand_hold_effect_ids),
            "hand_hold_count": self.hand_hold_count,
        }

    def canonical_json(self) -> str:
        return _canonical_json(self.to_dict())

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "AuditionHandHoldAudit":
        fields = {
            "schema_version",
            "run_id",
            "step_context_id",
            "step_context_digest",
            "session_transition_id",
            "round_number",
            "battle_config_id",
            "gimmick_effect_group_id",
            "input_digests",
            "card_variants",
            "item_ids",
            "passive_source_keys",
            "produce_effect_ids",
            "status_enchant_ids",
            "active_runtime_status_ids",
            "created_card_refs",
            "exam_effects",
            "excluded_hold_zone_move_effect_ids",
            "hand_hold_effect_ids",
            "hand_hold_count",
        }
        if set(payload) != fields:
            raise ValueError("audition HandHold audit fields are invalid")
        digests = payload["input_digests"]
        effects = payload["exam_effects"]
        if not isinstance(digests, Mapping) or not isinstance(effects, list):
            raise ValueError("HandHold audit nested fields are invalid")
        if not all(isinstance(value, Mapping) for value in effects):
            raise ValueError("exam_effects must be an array of objects")

        def text_array(name: str) -> tuple[str, ...]:
            raw = payload[name]
            if not isinstance(raw, list):
                raise ValueError(f"{name} must be an array")
            return tuple(str(value) for value in raw)

        return cls(
            schema_version=_strict_non_negative(
                payload["schema_version"], "schema_version"
            ),
            run_id=_strict_text(payload["run_id"], "run_id"),
            step_context_id=_strict_text(
                payload["step_context_id"], "step_context_id"
            ),
            step_context_digest=_strict_sha256(
                payload["step_context_digest"], "step_context_digest"
            ),
            session_transition_id=_strict_text(
                payload["session_transition_id"], "session_transition_id"
            ),
            round_number=_strict_non_negative(
                payload["round_number"], "round_number"
            ),
            battle_config_id=_strict_text(
                payload["battle_config_id"], "battle_config_id"
            ),
            gimmick_effect_group_id=_plain_text(
                payload["gimmick_effect_group_id"],
                "gimmick_effect_group_id",
            ),
            input_digests=tuple(
                sorted(
                    (
                        _strict_text(str(key), "input digest name"),
                        _strict_sha256(value, f"input digest:{key}"),
                    )
                    for key, value in digests.items()
                )
            ),
            card_variants=text_array("card_variants"),
            item_ids=text_array("item_ids"),
            passive_source_keys=text_array("passive_source_keys"),
            produce_effect_ids=text_array("produce_effect_ids"),
            status_enchant_ids=text_array("status_enchant_ids"),
            active_runtime_status_ids=text_array("active_runtime_status_ids"),
            created_card_refs=text_array("created_card_refs"),
            exam_effects=tuple(
                AuditedExamEffect.from_dict(value) for value in effects
            ),  # type: ignore[arg-type]
            excluded_hold_zone_move_effect_ids=text_array(
                "excluded_hold_zone_move_effect_ids"
            ),
            hand_hold_effect_ids=text_array("hand_hold_effect_ids"),
            hand_hold_count=_strict_non_negative(
                payload["hand_hold_count"], "hand_hold_count"
            ),
        )


class HandHoldAuditBlockedError(RuntimeError):
    def __init__(self, reasons: Iterable[str]) -> None:
        self.reasons = _deduplicate(str(value) for value in reasons)
        if not self.reasons:
            raise ValueError("blocked audit requires at least one reason")
        super().__init__("HandHold audit blocked: " + "; ".join(self.reasons))


class _ExamGraphCollector:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.effects: dict[str, tuple[str, str, str]] = {}
        self.effect_roots: dict[str, set[str]] = {}
        self.status_enchants: set[str] = set()
        self.blockers: list[str] = []

    def add_effect(self, effect_id: str, root: str, ancestry: tuple[str, ...] = ()) -> None:
        if not effect_id:
            return
        if effect_id in ancestry:
            self.blockers.append("effect-cycle:" + "->".join((*ancestry, effect_id)))
            return
        self.effect_roots.setdefault(effect_id, set()).add(root)
        known = self.effects.get(effect_id)
        if known is not None:
            _effect_type, status_enchant_id, chain_effect_id = known
            if status_enchant_id:
                self.add_status_enchant(
                    status_enchant_id,
                    root,
                    ancestry=(*ancestry, effect_id),
                )
            if chain_effect_id:
                self.add_effect(chain_effect_id, root, (*ancestry, effect_id))
            return
        row = self.connection.execute(
            "SELECT effect_type, status_enchant_id, chain_effect_id "
            "FROM effect WHERE id = ?",
            (effect_id,),
        ).fetchone()
        if row is None:
            self.blockers.append(f"exam-effect-missing:{effect_id}:{root}")
            return
        effect_type, status_enchant_id, chain_effect_id = (str(value or "") for value in row)
        if not effect_type:
            self.blockers.append(f"exam-effect-type-missing:{effect_id}:{root}")
            return
        self.effects[effect_id] = (effect_type, status_enchant_id, chain_effect_id)
        if status_enchant_id:
            self.add_status_enchant(
                status_enchant_id,
                root,
                ancestry=(*ancestry, effect_id),
            )
        if chain_effect_id:
            self.add_effect(chain_effect_id, root, (*ancestry, effect_id))

    def add_status_enchant(
        self, enchant_id: str, root: str, ancestry: tuple[str, ...] = ()
    ) -> None:
        if not enchant_id:
            return
        if enchant_id in ancestry:
            self.blockers.append("enchant-cycle:" + "->".join((*ancestry, enchant_id)))
            return
        self.status_enchants.add(enchant_id)
        row = self.connection.execute(
            "SELECT produce_exam_trigger_id, produce_exam_effect_ids_json "
            "FROM produce_exam_status_enchant WHERE id = ?",
            (enchant_id,),
        ).fetchone()
        if row is None:
            self.blockers.append(f"status-enchant-missing:{enchant_id}:{root}")
            return
        trigger_id = str(row[0] or "")
        if not trigger_id:
            self.blockers.append(f"status-enchant-trigger-missing:{enchant_id}:{root}")
        elif self.connection.execute(
            "SELECT 1 FROM produce_exam_trigger WHERE id = ?", (trigger_id,)
        ).fetchone() is None:
            self.blockers.append(f"exam-trigger-missing:{trigger_id}:{enchant_id}")
        try:
            effect_ids = json.loads(str(row[1]))
        except (TypeError, json.JSONDecodeError):
            effect_ids = None
        if not isinstance(effect_ids, list) or not all(
            isinstance(value, str) and value for value in effect_ids
        ):
            self.blockers.append(f"status-enchant-effects-invalid:{enchant_id}")
            return
        for effect_id in effect_ids:
            self.add_effect(effect_id, root, (*ancestry, enchant_id))

    def audited_effects(self) -> tuple[AuditedExamEffect, ...]:
        return tuple(
            AuditedExamEffect(
                effect_id=effect_id,
                effect_type=self.effects[effect_id][0],
                status_enchant_id=self.effects[effect_id][1],
                chain_effect_id=self.effects[effect_id][2],
                source_roots=tuple(sorted(self.effect_roots[effect_id])),
            )
            for effect_id in sorted(self.effects)
        )


def _card_effect_roots(
    connection: sqlite3.Connection,
    collector: _ExamGraphCollector,
    card_ids: Iterable[str],
) -> tuple[str, ...]:
    variants: list[str] = []
    for card_id in sorted(set(card_ids)):
        rows = connection.execute(
            "SELECT upgrade_count, play_effects_json, raw_json FROM card "
            "WHERE id = ? ORDER BY upgrade_count",
            (card_id,),
        ).fetchall()
        if not rows:
            collector.blockers.append(f"card-master-missing:{card_id}")
            continue
        upgrades = [int(row[0]) for row in rows]
        if upgrades != list(range(min(upgrades), max(upgrades) + 1)):
            collector.blockers.append(f"card-upgrade-gap:{card_id}:{upgrades}")
        for upgrade, play_effects_json, raw_json in rows:
            ref = f"{card_id}@{int(upgrade)}"
            variants.append(ref)
            root = f"card:{ref}"
            try:
                play_effects = json.loads(str(play_effects_json))
                raw = json.loads(str(raw_json))
            except (TypeError, json.JSONDecodeError):
                collector.blockers.append(f"card-json-invalid:{ref}")
                continue
            if not isinstance(play_effects, list) or not isinstance(raw, Mapping):
                collector.blockers.append(f"card-json-shape:{ref}")
                continue
            effect_ids: list[str] = []
            for index, value in enumerate(play_effects):
                if not isinstance(value, Mapping):
                    collector.blockers.append(f"card-play-effect-invalid:{ref}:{index}")
                    continue
                effect_id = value.get("produceExamEffectId")
                if not isinstance(effect_id, str) or not effect_id:
                    collector.blockers.append(f"card-play-effect-id-missing:{ref}:{index}")
                    continue
                effect_ids.append(effect_id)
            move_effects = raw.get("moveProduceExamEffectIds", [])
            if not isinstance(move_effects, list) or not all(
                isinstance(value, str) and value for value in move_effects
            ):
                collector.blockers.append(f"card-move-effects-invalid:{ref}")
            else:
                effect_ids.extend(move_effects)
            status_enchant_id = raw.get("produceCardStatusEnchantId", "")
            if not isinstance(status_enchant_id, str):
                collector.blockers.append(f"card-status-enchant-invalid:{ref}")
            elif status_enchant_id:
                collector.add_status_enchant(status_enchant_id, root)
            for effect_id in effect_ids:
                collector.add_effect(effect_id, root)
    return tuple(sorted(set(variants)))


def _item_effect_roots(
    connection: sqlite3.Connection,
    collector: _ExamGraphCollector,
    item_ids: Iterable[str],
) -> None:
    for item_id in sorted(set(item_ids)):
        row = connection.execute(
            "SELECT produce_item_effect_ids_json FROM produce_item WHERE id = ?",
            (item_id,),
        ).fetchone()
        if row is None:
            collector.blockers.append(f"produce-item-missing:{item_id}")
            continue
        try:
            effect_ids = json.loads(str(row[0]))
        except (TypeError, json.JSONDecodeError):
            effect_ids = None
        if not isinstance(effect_ids, list) or not all(
            isinstance(value, str) and value for value in effect_ids
        ):
            collector.blockers.append(f"produce-item-effects-invalid:{item_id}")
            continue
        for item_effect_id in effect_ids:
            effect = connection.execute(
                "SELECT effect_type, produce_effect_id, "
                "produce_exam_status_enchant_id FROM produce_item_effect WHERE id = ?",
                (item_effect_id,),
            ).fetchone()
            if effect is None:
                collector.blockers.append(f"produce-item-effect-missing:{item_effect_id}")
                continue
            effect_type, produce_effect_id, enchant_id = (
                str(value or "") for value in effect
            )
            root = f"item:{item_id}:{item_effect_id}:{effect_type}"
            if "HandHold" in effect_type:
                collector.blockers.append(
                    f"hand-hold-item-effect-present:{item_effect_id}:{effect_type}"
                )
            if produce_effect_id:
                _produce_effect_root(connection, collector, produce_effect_id, root)
            if enchant_id:
                collector.add_status_enchant(enchant_id, root)


def _produce_effect_root(
    connection: sqlite3.Connection,
    collector: _ExamGraphCollector,
    effect_id: str,
    root: str,
) -> str | None:
    row = connection.execute(
        "SELECT effect_type, exam_status_enchant_id FROM produce_effect WHERE id = ?",
        (effect_id,),
    ).fetchone()
    if row is None:
        collector.blockers.append(f"produce-effect-missing:{effect_id}:{root}")
        return None
    effect_type, enchant_id = (str(value or "") for value in row)
    if not effect_type:
        collector.blockers.append(f"produce-effect-type-missing:{effect_id}:{root}")
        return None
    if effect_type == PRODUCE_STATUS_ENCHANT_EFFECT_TYPE:
        if not enchant_id:
            collector.blockers.append(f"produce-effect-enchant-missing:{effect_id}")
        else:
            collector.add_status_enchant(enchant_id, root)
    elif enchant_id:
        collector.blockers.append(
            f"produce-effect-unexpected-enchant:{effect_id}:{effect_type}"
        )
    if "HandHold" in effect_type:
        collector.blockers.append(
            f"hand-hold-produce-effect-present:{effect_id}:{effect_type}"
        )
    return effect_type


def _difficulty_row(
    path: Path,
    identity: RunIdentity,
    *,
    step_type: str,
    stage_number: int,
    battle_config_id: str,
) -> Mapping[str, object]:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise HandHoldAuditBlockedError(("audition-difficulty-table-invalid",))
    exact_id = f"p_step_audition_difficulty-{identity.idol_card_id}"
    rows = [
        value
        for value in payload
        if isinstance(value, Mapping)
        and value.get("id") == exact_id
        and value.get("produceId") == identity.produce_id
        and value.get("stepType") == step_type
        and value.get("number") == stage_number
        and value.get("produceExamBattleConfigId") == battle_config_id
    ]
    if len(rows) != 1:
        raise HandHoldAuditBlockedError(
            (f"audition-difficulty-row-count:{exact_id}:{len(rows)}",)
        )
    return rows[0]


def build_zero_hand_hold_audit(
    *,
    identity: RunIdentity,
    root: Path = DEFAULT_RUN_ROOT,
    database: Path = DEFAULT_DATABASE,
    master_dir: Path | None = None,
    audition_difficulty_path: Path = DEFAULT_AUDITION_DIFFICULTY,
) -> AuditionHandHoldAudit:
    """Build a canonical proof, or fail closed with source-specific reasons."""

    identity.validate()
    paths = paths_for(identity, root=Path(root))
    database = Path(database)
    difficulty_path = Path(audition_difficulty_path)
    required_paths = {
        "audition_multiplier_checkpoint": paths.audition_multiplier_checkpoint,
        "contextual_passive_session": paths.contextual_passive_session,
        "deck_snapshot": paths.deck_snapshot,
        "equipped_item_snapshot": paths.equipped_item_snapshot,
        "exam_session": paths.exam_session,
        "initial_modifier_reconciliation": paths.initial_modifier_reconciliation,
        "loadout_snapshot": paths.loadout_snapshot,
        "master_database": database,
        "produce_step_audition_difficulty": difficulty_path,
    }
    missing = [f"input-missing:{name}:{path}" for name, path in required_paths.items() if not Path(path).is_file()]
    if missing:
        raise HandHoldAuditBlockedError(missing)

    session = load_exam_session(paths.exam_session)
    deck_record = load_run_deck_snapshot(identity, root=Path(root))
    loadout = load_loadout_snapshot(paths.loadout_snapshot)
    equipped = load_equipped_item_snapshot(
        paths.equipped_item_snapshot, database=database
    )
    initial = load_initial_modifier_reconciliation(
        paths.initial_modifier_reconciliation
    )
    contextual = load_contextual_passive_step_session(
        paths.contextual_passive_session
    )
    multiplier = load_multiplier_checkpoint(
        paths.audition_multiplier_checkpoint, validate_files=True
    )
    if any(value is None for value in (session, deck_record, loadout, equipped, initial, contextual, multiplier)):
        raise HandHoldAuditBlockedError(("run-input-loader-returned-none",))
    assert session is not None
    assert deck_record is not None
    assert loadout is not None
    assert equipped is not None
    assert initial is not None
    assert contextual is not None
    assert multiplier is not None

    blockers: list[str] = []
    if session.schema_version != SESSION_SCHEMA_VERSION or session.preflight_binding is None:
        blockers.append("exam-session-live-binding-missing")
    elif not session.matches_run(
        run_id=identity.run_id,
        idol_card_id=identity.idol_card_id,
        character_id=identity.character_id,
        produce_id=identity.produce_id,
    ):
        blockers.append("exam-session-run-mismatch")
    if session.transition_id is None:
        blockers.append("exam-session-transition-missing")
    if deck_record.run_id != identity.run_id:
        blockers.append("deck-run-mismatch")
    if not deck_record.snapshot.is_authoritative() or not deck_record.evidence_valid:
        blockers.append("deck-not-authoritative")
    if (
        deck_record.snapshot.produce_id,
        deck_record.snapshot.step_type,
        deck_record.snapshot.stage_number,
    ) != (session.produce_id, session.step_type, session.stage_number):
        blockers.append("deck-stage-mismatch")
    blockers.extend(loadout.observation_blocking_reasons())
    if loadout.run_id != identity.run_id or loadout.produce_id != identity.produce_id:
        blockers.append("loadout-run-mismatch")
    if equipped.run_id != identity.run_id or equipped.idol_card_id != identity.idol_card_id:
        blockers.append("equipped-item-run-mismatch")
    if not equipped.authoritative or not equipped.evidence_valid:
        blockers.append("equipped-item-not-authoritative")

    snapshot_digest = loadout_snapshot_digest(loadout)
    if equipped.loadout_snapshot_digest != snapshot_digest:
        blockers.append("equipped-item-loadout-digest-mismatch")
    if session.preflight_binding is not None:
        binding = session.preflight_binding
        expected_digests = {
            "loadout": snapshot_digest,
            "initial": _canonical_digest(initial.to_dict()),
            "contextual": _canonical_digest(contextual.to_dict()),
            "item": _canonical_digest(equipped.to_dict()),
            "deck": _canonical_digest(deck_record.to_dict()),
        }
        actual_digests = {
            "loadout": binding.loadout_snapshot_digest,
            "initial": binding.initial_reconciliation_digest,
            "contextual": binding.contextual_session_digest,
            "item": binding.equipped_item_snapshot_digest,
            "deck": binding.deck_snapshot_digest,
        }
        for name, expected in expected_digests.items():
            if actual_digests[name] != expected:
                blockers.append(f"preflight-{name}-digest-mismatch")

    multiplier_binding = multiplier.binding
    if (
        multiplier_binding.run_id,
        multiplier_binding.idol_card_id,
        multiplier_binding.produce_id,
        multiplier_binding.step_type,
        multiplier_binding.stage_number,
    ) != (
        identity.run_id,
        identity.idol_card_id,
        identity.produce_id,
        session.step_type,
        session.stage_number,
    ):
        blockers.append("multiplier-run-stage-mismatch")
    if session.preflight_binding is not None and (
        multiplier_binding.step_context_id
        != session.preflight_binding.step_context_id
        or multiplier_binding.step_context_digest
        != session.preflight_binding.step_context_digest
    ):
        blockers.append("multiplier-step-context-mismatch")

    difficulty = _difficulty_row(
        difficulty_path,
        identity,
        step_type=session.step_type,
        stage_number=session.stage_number,
        battle_config_id=multiplier_binding.battle_config_id,
    )
    gimmick = difficulty.get("produceExamGimmickEffectGroupId", "")
    if not isinstance(gimmick, str):
        blockers.append("audition-gimmick-id-invalid")
        gimmick = ""
    elif gimmick:
        blockers.append(f"audition-gimmick-unsupported:{gimmick}")

    if blockers:
        raise HandHoldAuditBlockedError(blockers)

    catalog = MasterPassiveCatalog.load(
        Path(master_dir) if master_dir is not None else DEFAULT_MASTER_DIR
    )
    resolution = loadout.audit_resolution(catalog)
    # Runtime support limitations (probabilistic activation, unrelated event
    # phases) do not obscure effect identity.  Unknown selected sources do.
    source_identity_blockers = tuple(
        reason
        for reason in resolution.blocking_reasons
        if reason.startswith("unknown-memory-ability:")
        or reason.startswith("unknown-support-card:")
        or reason.startswith("missing-")
    )
    if source_identity_blockers:
        raise HandHoldAuditBlockedError(source_identity_blockers)

    passive_keys: list[str] = []
    produce_effect_ids: set[str] = set()
    active_runtime_status_ids = tuple(
        sorted(
            {
                value.enchant_id
                for value in session.logic_state.runtime_status_enchants
            }
        )
    )
    created_card_refs = tuple(
        sorted(
            {
                f"{card_id}@{upgrade}"
            for card_id, upgrade, _position, count in session.logic_state.created_cards
            if count > 0
            }
        )
    )
    current_card_ids = {
        stack.card_id for stack in deck_record.snapshot.cards
    } | {
        card_id
        for card_id, _upgrade, _position, count in session.logic_state.created_cards
        if count > 0
    }

    with sqlite3.connect(database) as connection:
        collector = _ExamGraphCollector(connection)
        card_variants = _card_effect_roots(connection, collector, current_card_ids)
        item_ids = tuple(sorted(equipped.item_ids))
        _item_effect_roots(connection, collector, item_ids)

        for source_index, source in enumerate(resolution.passive_sources):
            source_key = (
                f"{source_index}:{source.source_kind}:{source.source_id}:"
                f"{source.skill_id}:{source.skill_level}"
            )
            passive_keys.append(source_key)
            for reason in source.unsupported_rules:
                collector.blockers.append(
                    f"passive-source-unresolved:{source_key}:{reason}"
                )
            for rule in source.rules:
                structural_reasons = tuple(
                    reason
                    for reason in rule.unsupported_rules
                    if not reason.startswith("probabilistic-activation-rate:")
                    and not reason.startswith("random-effect-range:")
                    and not reason.startswith("rule-not-whitelisted:")
                )
                collector.blockers.extend(
                    f"passive-rule-unresolved:{source_key}:{rule.slot}:{reason}"
                    for reason in structural_reasons
                )
                if not rule.effect_id or not rule.effect_type:
                    collector.blockers.append(
                        f"passive-rule-effect-unresolved:{source_key}:{rule.slot}"
                    )
                    continue
                produce_effect_ids.add(rule.effect_id)
                actual_type = _produce_effect_root(
                    connection,
                    collector,
                    rule.effect_id,
                    f"passive:{source_key}:rule-{rule.slot}",
                )
                if actual_type is not None and actual_type != rule.effect_type:
                    collector.blockers.append(
                        f"passive-effect-type-mismatch:{rule.effect_id}:"
                        f"{rule.effect_type}:{actual_type}"
                    )

        for status in session.logic_state.active_status_enchants:
            root = f"active-status:{status.id}"
            for effect in status.effects:
                collector.add_effect(effect.id, root)
        for enchant_id in active_runtime_status_ids:
            collector.add_status_enchant(enchant_id, f"runtime-status:{enchant_id}")

        if collector.blockers:
            raise HandHoldAuditBlockedError(collector.blockers)
        exam_effects = collector.audited_effects()

    hand_hold_effect_ids = tuple(
        sorted(
            value.effect_id
            for value in exam_effects
            if value.effect_type == HAND_HOLD_EFFECT_TYPE
        )
    )
    if hand_hold_effect_ids:
        raise HandHoldAuditBlockedError(
            f"hand-hold-source-present:{value}" for value in hand_hold_effect_ids
        )
    hold_zone_moves = tuple(
        sorted(
            value.effect_id
            for value in exam_effects
            if value.effect_type == CARD_MOVE_EFFECT_TYPE
            and HOLD_ZONE_TOKEN in value.effect_id.lower()
        )
    )
    assert session.preflight_binding is not None
    assert session.transition_id is not None
    return AuditionHandHoldAudit(
        schema_version=AUDITION_HAND_HOLD_AUDIT_SCHEMA_VERSION,
        run_id=identity.run_id,
        step_context_id=session.preflight_binding.step_context_id,
        step_context_digest=session.preflight_binding.step_context_digest,
        session_transition_id=session.transition_id,
        round_number=session.logic_state.round_number,
        battle_config_id=multiplier_binding.battle_config_id,
        gimmick_effect_group_id=gimmick,
        input_digests=tuple(
            sorted((name, _file_sha256(Path(path))) for name, path in required_paths.items())
        ),
        card_variants=card_variants,
        item_ids=item_ids,
        passive_source_keys=tuple(sorted(passive_keys)),
        produce_effect_ids=tuple(sorted(produce_effect_ids)),
        status_enchant_ids=tuple(sorted(collector.status_enchants)),
        active_runtime_status_ids=active_runtime_status_ids,
        created_card_refs=created_card_refs,
        exam_effects=exam_effects,
        excluded_hold_zone_move_effect_ids=hold_zone_moves,
        hand_hold_effect_ids=(),
        hand_hold_count=0,
    )


def save_audition_hand_hold_audit(
    audit: AuditionHandHoldAudit, path: Path
) -> Path:
    if not isinstance(audit, AuditionHandHoldAudit):
        raise TypeError("audit must be AuditionHandHoldAudit")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(audit.canonical_json() + "\n", encoding="utf-8")
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return target


def load_audition_hand_hold_audit(
    path: Path,
) -> AuditionHandHoldAudit | None:
    target = Path(path)
    if not target.is_file():
        return None
    payload = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("audition HandHold audit root must be an object")
    return AuditionHandHoldAudit.from_dict(payload)


__all__ = [
    "AUDITION_HAND_HOLD_AUDIT_SCHEMA_VERSION",
    "AuditedExamEffect",
    "AuditionHandHoldAudit",
    "HandHoldAuditBlockedError",
    "build_zero_hand_hold_audit",
    "load_audition_hand_hold_audit",
    "save_audition_hand_hold_audit",
]
