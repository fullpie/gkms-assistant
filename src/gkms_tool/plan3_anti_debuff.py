"""Native AntiDebuff count transition for Plan 3/Common.

Android v3.2.3 proves that ``ExamAntiDebuff`` is a preventive, permanent
count status, not a cleanse.  ``AntiDebuffEffectExecutor.ExecuteEffect`` adds
``effectCount`` to one merged ``AntiDebuffStatusEffect`` with ``turn = -1``.
It never enumerates, orders, shortens, or removes an existing debuff.

Before *any* status addition, ``ExamStatusEffectCollection.IsBlockAddStatus``
classifies the incoming ``ProduceExamEffectType``.  Exactly when its target is
``Debuff`` and AntiDebuff count is positive, it spends one AntiDebuff count,
removes the single status at zero, records the AntiDebuff 66 before/after
difference, and makes the caller skip the incoming status.  Thus Master
counts 1/2/3/5 are charges, not a number of current debuffs to select.

The runtime below is a narrow immutable projection of the one proven native
field.  It deliberately does not invent a list of Plan3 debuffs or mutate the
central Plan3 state.  ``simulate`` changes only the result marker: the same
transition is returned because the native executor has no simulation branch;
callers choose whether to commit the returned projection.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from enum import Enum
from typing import Generic, Literal, TypeAlias, TypeVar


EFFECT_TYPE = "ProduceExamEffectType_ExamAntiDebuff"
PLAN_COMMON = "ProducePlanType_Common"
MENTAL_SKILL = "ProduceCardCategory_MentalSkill"
ANTI_DEBUFF_EFFECT_TYPE_VALUE = 66
ANTI_DEBUFF_PERMANENT_TURN = -1
INT32_MAX = (1 << 31) - 1


JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | tuple["JsonValue", ...]


class UnresolvedReason(str, Enum):
    """A semantic input which is required before a mutation is safe."""

    DEBUFF_SELECTION = "debuff_selection_unresolved"
    REMOVAL_AMOUNT = "removal_or_consumption_amount_unresolved"
    PRIORITY = "debuff_priority_unresolved"
    DURATION = "duration_unresolved"
    TRIGGER_TIMING = "trigger_timing_unresolved"
    EXECUTOR_BODY = "executor_body_semantics_unresolved"
    UNSUPPORTED_ROW_SHAPE = "unsupported_effect_row_shape"
    MALFORMED_EFFECT = "malformed_effect_contract"
    UNSUPPORTED_STATE_SHAPE = "unsupported_anti_debuff_state_shape"
    COUNT_OVERFLOW = "anti_debuff_count_overflow"


class ExecutionStatus(str, Enum):
    """Resolved and fail-closed execution states."""

    EXECUTED = "executed"
    UNRESOLVED = "unresolved"


class ExamStatusEffectTargetType(str, Enum):
    """The native three-way target classification used by the block gate."""

    NONE = "None"
    BUFF = "Buff"
    DEBUFF = "Debuff"


def _freeze_json(value: JsonValue) -> JsonValue:
    """Normalize nested JSON arrays to tuples so catalog values are hashable."""

    if isinstance(value, tuple):
        return tuple(_freeze_json(item) for item in value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def _json_value(value: JsonValue) -> JsonScalar | list[object]:
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class EffectField:
    """One ordered, hashable field from the Master effect payload."""

    name: str
    value: JsonValue

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("effect field name must not be empty")
        object.__setattr__(self, "value", _freeze_json(self.value))

    def to_json(self) -> dict[str, object]:
        return {"name": self.name, "value": _json_value(self.value)}


# The first 28 fields are the execution/graph payload.  The two description
# arrays are intentionally not copied into the executable payload: they are
# UI metadata.  Their positions remain recorded in RAW_EFFECT_FIELD_ORDER.
EXECUTION_EFFECT_FIELD_ORDER: tuple[str, ...] = (
    "id",
    "effectType",
    "effectValue1",
    "effectValue2",
    "effectCount",
    "effectTurn",
    "targetProduceCardId",
    "targetUpgradeCount",
    "targetExamEffectType",
    "produceCardSearchId",
    "movePositionType",
    "pickRangeType",
    "pickCountReferenceProduceCardSearchId",
    "pickCountType",
    "pickCountMin",
    "pickCountMax",
    "produceCardSearchId2",
    "pickRangeType2",
    "pickCountReferenceProduceCardSearchId2",
    "pickCountType2",
    "pickCountMin2",
    "pickCountMax2",
    "chainProduceExamEffectId",
    "chainProduceExamEffectIds",
    "produceExamStatusEnchantId",
    "produceCardStatusEnchantId",
    "produceCardGrowEffectIds",
    "effectGroupIds",
)

RAW_EFFECT_FIELD_ORDER: tuple[str, ...] = (
    *EXECUTION_EFFECT_FIELD_ORDER,
    "produceDescriptions",
    "customizeProduceDescriptions",
)


@dataclass(frozen=True, slots=True)
class AntiDebuffEffectRow:
    """A single Master ``effect`` row with source order preserved."""

    source_effect_id: str
    effect_type: str
    catalog_order: int
    fields: tuple[EffectField, ...]

    def __post_init__(self) -> None:
        if not self.source_effect_id:
            raise ValueError("source effect id must not be empty")
        if self.catalog_order < 0:
            raise ValueError("catalog order must be non-negative")
        fields = tuple(self.fields)
        object.__setattr__(self, "fields", fields)
        names = tuple(field.name for field in fields)
        if names != EXECUTION_EFFECT_FIELD_ORDER:
            raise ValueError("effect fields must retain the Master execution-field order")
        if self.field_value("id") != self.source_effect_id:
            raise ValueError("source effect id does not match the id field")
        if self.field_value("effectType") != self.effect_type:
            raise ValueError("effect type does not match the effectType field")

    def field_value(self, name: str) -> JsonValue:
        for field in self.fields:
            if field.name == name:
                return field.value
        raise KeyError(name)

    @property
    def effect_count(self) -> int:
        value = self.field_value("effectCount")
        if not isinstance(value, int) or isinstance(value, bool):
            raise TypeError("effectCount is not an integer")
        return value

    @property
    def effect_turn(self) -> int:
        value = self.field_value("effectTurn")
        if not isinstance(value, int) or isinstance(value, bool):
            raise TypeError("effectTurn is not an integer")
        return value

    @property
    def effect_group_ids(self) -> tuple[str, ...]:
        value = self.field_value("effectGroupIds")
        if not isinstance(value, tuple) or not all(isinstance(item, str) for item in value):
            raise TypeError("effectGroupIds is not a string tuple")
        return value

    @property
    def raw_field_order(self) -> tuple[str, ...]:
        """Full raw JSON/YAML field order, including UI-only description arrays."""

        return RAW_EFFECT_FIELD_ORDER

    def to_json(self) -> dict[str, object]:
        return {
            "source_effect_id": self.source_effect_id,
            "effect_type": self.effect_type,
            "catalog_order": self.catalog_order,
            "fields": [field.to_json() for field in self.fields],
            "raw_field_order": list(self.raw_field_order),
            "ui_description_fields_omitted": [
                "produceDescriptions",
                "customizeProduceDescriptions",
            ],
        }


@dataclass(frozen=True, slots=True)
class CardEffectSlot:
    """One ordered ``card.play_effects_json`` slot."""

    effect_order: int
    source_effect_id: str
    effect_type: str
    slot_trigger_id: str
    hide_icon: bool
    once_only: bool

    def __post_init__(self) -> None:
        if self.effect_order < 0:
            raise ValueError("effect order must be non-negative")
        if not self.source_effect_id or not self.effect_type:
            raise ValueError("card effect slot identity is required")

    def to_json(self) -> dict[str, object]:
        return {
            "effect_order": self.effect_order,
            "source_effect_id": self.source_effect_id,
            "effect_type": self.effect_type,
            "slot_trigger_id": self.slot_trigger_id,
            "hide_icon": self.hide_icon,
            "once_only": self.once_only,
        }


@dataclass(frozen=True, slots=True)
class CardVersionRef:
    """A card/version reference used for scope accounting."""

    card_id: str
    upgrade_count: int
    plan_type: str

    def __post_init__(self) -> None:
        if not self.card_id or not self.plan_type:
            raise ValueError("card version identity is required")
        if self.upgrade_count < 0:
            raise ValueError("upgrade count must be non-negative")

    def to_json(self) -> dict[str, object]:
        return {
            "card_id": self.card_id,
            "upgrade_count": self.upgrade_count,
            "plan_type": self.plan_type,
        }


@dataclass(frozen=True, slots=True)
class CommonCardVersion(CardVersionRef):
    """A Common card version with its complete declared play-effect order."""

    category: str
    effect_slots: tuple[CardEffectSlot, ...]

    def __post_init__(self) -> None:
        CardVersionRef.__post_init__(self)
        if self.category == "":
            raise ValueError("card category must not be empty")
        slots = tuple(self.effect_slots)
        object.__setattr__(self, "effect_slots", slots)
        if tuple(slot.effect_order for slot in slots) != tuple(range(len(slots))):
            raise ValueError("card effect slots must retain contiguous source order")

    @property
    def anti_debuff_slots(self) -> tuple[CardEffectSlot, ...]:
        return tuple(slot for slot in self.effect_slots if slot.effect_type == EFFECT_TYPE)

    def to_json(self) -> dict[str, object]:
        return {
            **CardVersionRef.to_json(self),
            "category": self.category,
            "effect_slots": [slot.to_json() for slot in self.effect_slots],
        }


@dataclass(frozen=True, slots=True)
class ExecutorEvidence:
    """PC identity plus the Android native bodies used as semantic proof."""

    effect_type: str
    executor_type: str
    metadata_fields: tuple[str, ...]
    metadata_methods: tuple[str, ...]
    status_effect_type: str
    status_metadata_fields: tuple[str, ...]
    status_metadata_methods: tuple[str, ...]
    factory_enum_value: int
    factory_native_va: str
    constructor_va: str
    constructor_token: str
    factory_branch_target_va: str
    type_global_va: str
    usage_cell_va: str
    execute_effect_native_va: str
    try_add_native_va: str
    get_count_native_va: str
    use_count_native_va: str
    block_check_native_va: str
    remove_status_native_va: str
    add_status_native_va: str
    target_type_native_va: str
    android_binary_sha256: str
    semantic_reference_platform: str
    pc_execute_effect_token: str
    pc_native_body_mapping_proven: bool
    body_semantics_proven: bool

    def to_json(self) -> dict[str, object]:
        return {
            "effect_type": self.effect_type,
            "executor_type": self.executor_type,
            "metadata_fields": list(self.metadata_fields),
            "metadata_methods": list(self.metadata_methods),
            "status_effect_type": self.status_effect_type,
            "status_metadata_fields": list(self.status_metadata_fields),
            "status_metadata_methods": list(self.status_metadata_methods),
            "factory_enum_value": self.factory_enum_value,
            "factory_native_va": self.factory_native_va,
            "constructor_va": self.constructor_va,
            "constructor_token": self.constructor_token,
            "factory_branch_target_va": self.factory_branch_target_va,
            "type_global_va": self.type_global_va,
            "usage_cell_va": self.usage_cell_va,
            "execute_effect_native_va": self.execute_effect_native_va,
            "try_add_native_va": self.try_add_native_va,
            "get_count_native_va": self.get_count_native_va,
            "use_count_native_va": self.use_count_native_va,
            "block_check_native_va": self.block_check_native_va,
            "remove_status_native_va": self.remove_status_native_va,
            "add_status_native_va": self.add_status_native_va,
            "target_type_native_va": self.target_type_native_va,
            "android_binary_sha256": self.android_binary_sha256,
            "semantic_reference_platform": self.semantic_reference_platform,
            "pc_execute_effect_token": self.pc_execute_effect_token,
            "pc_native_body_mapping_proven": self.pc_native_body_mapping_proven,
            "body_semantics_proven": self.body_semantics_proven,
        }


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    source: str
    locator: str
    claim: str

    def to_json(self) -> dict[str, str]:
        return {"source": self.source, "locator": self.locator, "claim": self.claim}


@dataclass(frozen=True, slots=True)
class AntiDebuffContract:
    """The state-transition boundary exposed to a future core hook."""

    effect_type: str
    recognized_source_effect_ids: tuple[str, ...]
    executable_source_effect_ids: tuple[str, ...]
    unresolved_reasons: tuple[UnresolvedReason, ...]
    state_policy: str
    failure_status: ExecutionStatus

    @property
    def executable_count(self) -> int:
        return len(self.executable_source_effect_ids)

    def to_json(self) -> dict[str, object]:
        return {
            "effect_type": self.effect_type,
            "recognized_source_effect_ids": list(self.recognized_source_effect_ids),
            "executable_source_effect_ids": list(self.executable_source_effect_ids),
            "unresolved_reasons": [reason.value for reason in self.unresolved_reasons],
            "state_policy": self.state_policy,
            "failure_status": self.failure_status.value,
        }


@dataclass(frozen=True, slots=True)
class AntiDebuffCatalog:
    """Immutable Plan3/Common catalog and its evidence boundary."""

    scope: str
    master_effect_rows: tuple[AntiDebuffEffectRow, ...]
    affected_card_versions: tuple[CommonCardVersion, ...]
    out_of_scope_references: tuple[CardVersionRef, ...]
    executor: ExecutorEvidence
    evidence: tuple[EvidenceRef, ...]
    contract: AntiDebuffContract
    order_basis: str
    runtime_order_proven: bool
    directly_unlocked_if_fixed_alone: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "master_effect_rows", tuple(self.master_effect_rows))
        object.__setattr__(self, "affected_card_versions", tuple(self.affected_card_versions))
        object.__setattr__(self, "out_of_scope_references", tuple(self.out_of_scope_references))
        object.__setattr__(self, "evidence", tuple(self.evidence))
        if not 0 <= self.directly_unlocked_if_fixed_alone <= self.affected_version_count:
            raise ValueError("directly unlocked count is outside affected scope")

    @property
    def master_shape_count(self) -> int:
        return len(self.master_effect_rows)

    @property
    def affected_version_count(self) -> int:
        return len(self.affected_card_versions)

    @property
    def affected_unique_card_count(self) -> int:
        return len({card.card_id for card in self.affected_card_versions})

    @property
    def total_master_referenced_version_count(self) -> int:
        return self.affected_version_count + len(self.out_of_scope_references)

    @property
    def executable_count(self) -> int:
        return self.contract.executable_count

    def row(self, source_effect_id: str) -> AntiDebuffEffectRow:
        for row in self.master_effect_rows:
            if row.source_effect_id == source_effect_id:
                return row
        raise KeyError(source_effect_id)

    def to_json(self) -> dict[str, object]:
        return {
            "scope": self.scope,
            "master_shape_count": self.master_shape_count,
            "affected_version_count": self.affected_version_count,
            "affected_unique_card_count": self.affected_unique_card_count,
            "total_master_referenced_version_count": self.total_master_referenced_version_count,
            "directly_unlocked_if_fixed_alone": self.directly_unlocked_if_fixed_alone,
            "master_effect_rows": [row.to_json() for row in self.master_effect_rows],
            "affected_card_versions": [card.to_json() for card in self.affected_card_versions],
            "out_of_scope_references": [ref.to_json() for ref in self.out_of_scope_references],
            "executor": self.executor.to_json(),
            "evidence": [item.to_json() for item in self.evidence],
            "contract": self.contract.to_json(),
            "order_basis": self.order_basis,
            "runtime_order_proven": self.runtime_order_proven,
        }


def _anti_debuff_fields(source_effect_id: str, count: int) -> tuple[EffectField, ...]:
    return (
        EffectField("id", source_effect_id),
        EffectField("effectType", EFFECT_TYPE),
        EffectField("effectValue1", 0),
        EffectField("effectValue2", 0),
        EffectField("effectCount", count),
        EffectField("effectTurn", 0),
        EffectField("targetProduceCardId", ""),
        EffectField("targetUpgradeCount", 0),
        EffectField("targetExamEffectType", "ProduceExamEffectType_Unknown"),
        EffectField("produceCardSearchId", ""),
        EffectField("movePositionType", "ProduceCardMovePositionType_Unknown"),
        EffectField("pickRangeType", "ProducePickRangeType_Unknown"),
        EffectField("pickCountReferenceProduceCardSearchId", ""),
        EffectField("pickCountType", "ProducePickCountType_Unknown"),
        EffectField("pickCountMin", 0),
        EffectField("pickCountMax", 0),
        EffectField("produceCardSearchId2", ""),
        EffectField("pickRangeType2", "ProducePickRangeType_Unknown"),
        EffectField("pickCountReferenceProduceCardSearchId2", ""),
        EffectField("pickCountType2", "ProducePickCountType_Unknown"),
        EffectField("pickCountMin2", 0),
        EffectField("pickCountMax2", 0),
        EffectField("chainProduceExamEffectId", ""),
        EffectField("chainProduceExamEffectIds", ()),
        EffectField("produceExamStatusEnchantId", ""),
        EffectField("produceCardStatusEnchantId", ""),
        EffectField("produceCardGrowEffectIds", ()),
        EffectField("effectGroupIds", ("effect_group-visible-exam_anti_debuff-000",)),
    )


MASTER_EFFECT_ROWS: tuple[AntiDebuffEffectRow, ...] = tuple(
    AntiDebuffEffectRow(
        source_effect_id=source_effect_id,
        effect_type=EFFECT_TYPE,
        catalog_order=index,
        fields=_anti_debuff_fields(source_effect_id, count),
    )
    for index, (source_effect_id, count) in enumerate(
        (
            ("e_effect-exam_anti_debuff-01", 1),
            ("e_effect-exam_anti_debuff-02", 2),
            ("e_effect-exam_anti_debuff-03", 3),
            ("e_effect-exam_anti_debuff-05", 5),
        )
    )
)


def _slot(
    order: int,
    source_effect_id: str,
    effect_type: str,
) -> CardEffectSlot:
    return CardEffectSlot(
        effect_order=order,
        source_effect_id=source_effect_id,
        effect_type=effect_type,
        slot_trigger_id="",
        hide_icon=False,
        once_only=False,
    )


def _common_card(
    card_id: str,
    upgrade_count: int,
    effect_ids: tuple[tuple[str, str], ...],
) -> CommonCardVersion:
    return CommonCardVersion(
        card_id=card_id,
        upgrade_count=upgrade_count,
        plan_type=PLAN_COMMON,
        category=MENTAL_SKILL,
        effect_slots=tuple(_slot(index, effect_id, effect_type) for index, (effect_id, effect_type) in enumerate(effect_ids)),
    )


_BLOCK = "ProduceExamEffectType_ExamBlock"
_DRAW = "ProduceExamEffectType_ExamCardDraw"
_PLAYABLE = "ProduceExamEffectType_ExamPlayableValueAdd"
_TIMER = "ProduceExamEffectType_ExamEffectTimer"


_MEN_EFFECTS = (
    ("e_effect-exam_block-0006", _BLOCK),
    ("e_effect-exam_anti_debuff-01", EFFECT_TYPE),
    ("e_effect-exam_playable_value_add-01", _PLAYABLE),
)
_MEN_UPGRADED_EFFECTS = _MEN_EFFECTS + (
    ("e_effect-exam_effect_timer-0001-01-e_effect-exam_card_upgrade-p_card_search-hand-all-0_0", _TIMER),
)
_SUP_EFFECTS = (
    ("e_effect-exam_anti_debuff-01", EFFECT_TYPE),
    ("e_effect-exam_card_draw-0001", _DRAW),
    ("e_effect-exam_playable_value_add-01", _PLAYABLE),
)
_SUP_UPGRADED_EFFECTS = (
    ("e_effect-exam_anti_debuff-01", EFFECT_TYPE),
    ("e_effect-exam_card_draw-0002", _DRAW),
    ("e_effect-exam_playable_value_add-01", _PLAYABLE),
)
_SUP_PLUS2_EFFECTS = (
    ("e_effect-exam_block-0001", _BLOCK),
    *_SUP_UPGRADED_EFFECTS,
)
_SUP_PLUS3_EFFECTS = (
    ("e_effect-exam_block-0002", _BLOCK),
    *_SUP_UPGRADED_EFFECTS,
)


COMMON_AFFECTED_CARD_VERSIONS: tuple[CommonCardVersion, ...] = (
    _common_card("p_card-00-men-3_003", 0, _MEN_EFFECTS),
    *tuple(_common_card("p_card-00-men-3_003", upgrade, _MEN_UPGRADED_EFFECTS) for upgrade in (1, 2, 3)),
    _common_card("p_card-00-sup-3_093", 0, _SUP_EFFECTS),
    _common_card("p_card-00-sup-3_093", 1, _SUP_UPGRADED_EFFECTS),
    _common_card("p_card-00-sup-3_093", 2, _SUP_PLUS2_EFFECTS),
    _common_card("p_card-00-sup-3_093", 3, _SUP_PLUS3_EFFECTS),
)


OUT_OF_SCOPE_REFERENCES: tuple[CardVersionRef, ...] = tuple(
    CardVersionRef("p_card-01-ido-3_080", upgrade, "ProducePlanType_Plan1")
    for upgrade in (0, 1, 2, 3)
)


EXECUTOR_EVIDENCE = ExecutorEvidence(
    effect_type=EFFECT_TYPE,
    executor_type="Campus.InGame.Exam.AntiDebuffEffectExecutor",
    metadata_fields=("_count",),
    metadata_methods=(".ctor", "ExecuteEffect"),
    status_effect_type="Campus.InGame.Exam.AntiDebuffStatusEffect",
    status_metadata_fields=("_count",),
    status_metadata_methods=(
        "get_Type",
        "get_IconValue",
        "get_DescriptionReactiveDataList",
        ".ctor",
        "get_IsCountLimited",
        "get_Count",
        "SpendCount",
        "AddCount",
        "ToString",
        "ToSaveData",
    ),
    factory_enum_value=66,
    factory_native_va="0x7E5BB60",
    constructor_va="0x7E71428",
    constructor_token="0x060047CA",
    factory_branch_target_va="0x7E5D380",
    type_global_va="0xE756F08",
    usage_cell_va="0xEB24F78",
    execute_effect_native_va="0x7E714E0",
    try_add_native_va="0x7E9F084",
    get_count_native_va="0x7E9F194",
    use_count_native_va="0x7E9F210",
    block_check_native_va="0x7E937D4",
    remove_status_native_va="0x7E94064",
    add_status_native_va="0x8F70ACC",
    target_type_native_va="0x680B478",
    android_binary_sha256=(
        "107E2DA660CEA4193E0BDAC95920472E3CB1B8B2B38C6C894D6F7723FA1C802B"
    ),
    semantic_reference_platform="Android v3.2.3 arm64-v8a",
    pc_execute_effect_token="0x0600456E",
    # The protected PC artifact proves the matching metadata identity but its
    # recovered stage-one loader is explicitly not a final method-body map.
    pc_native_body_mapping_proven=False,
    body_semantics_proven=True,
)


UNRESOLVED_REASONS: tuple[UnresolvedReason, ...] = ()


ANTI_DEBUFF_CONTRACT = AntiDebuffContract(
    effect_type=EFFECT_TYPE,
    recognized_source_effect_ids=tuple(row.source_effect_id for row in MASTER_EFFECT_ROWS),
    executable_source_effect_ids=tuple(row.source_effect_id for row in MASTER_EFFECT_ROWS),
    unresolved_reasons=UNRESOLVED_REASONS,
    state_policy="immutable_native_anti_debuff_count_projection",
    failure_status=ExecutionStatus.UNRESOLVED,
)


EVIDENCE: tuple[EvidenceRef, ...] = (
    EvidenceRef(
        "var/master.sqlite3",
        "effect.effect_type = ProduceExamEffectType_ExamAntiDebuff",
        "four rows: e_effect-exam_anti_debuff-01/-02/-03/-05; only effectCount varies (1/2/3/5)",
    ),
    EvidenceRef(
        "_research/gakumasu-diff/ProduceExamEffect.yaml",
        "rows at 42523, 42761, 42999, 43237",
        "raw field order and execution payload shape",
    ),
    EvidenceRef(
        "_research/gakumasu-diff/ProduceCard.yaml",
        "Common rows at 13384..15727 and 57671..59593",
        "eight Common card versions reference ...anti_debuff-01 in ordered play effects",
    ),
    EvidenceRef(
        "var/coverage/plan3_card_executable_coverage.json",
        "gap effect-type:ProduceExamEffectType_ExamAntiDebuff",
        "8 affected Common versions; blocker remains unsupported",
    ),
    EvidenceRef(
        "var/coverage/plan3_master_effect_schema.json",
        "metadata.executor_types",
        "AntiDebuffEffectExecutor has _count, .ctor, ExecuteEffect",
    ),
    EvidenceRef(
        "_research/il2cpp/B6120AF66E75E72757E3D4086E2C48D0E7D31DA9F3A3DFBCDC7B3ED5BFFD61DA/targeted-metadata-index.json",
        "Campus.InGame.Exam.AntiDebuffEffectExecutor and AntiDebuffStatusEffect",
        "PC metadata identity: _count plus .ctor/ExecuteEffect token 0x0600456E; no final PC native-body mapping",
    ),
    EvidenceRef(
        "var/coverage/android_pc_exam_metadata_compatibility.json",
        "structurally_compatible=true; native_body_equality_proven=false",
        "version-pinned Android exam semantics are admitted as cross-build evidence without claiming byte-identical PC instructions",
    ),
    EvidenceRef(
        "_research/android/game-v3.2.3/native-analysis/create-executor-mapping.json",
        "enum_name ExamAntiDebuff / value 66",
        "Android factory control-flow mapping and constructor address",
    ),
    EvidenceRef(
        "docs/android-v323-create-executor-mapping.md",
        "row 66",
        "constructor VA 0x7E71428 and constructor token 0x060047CA",
    ),
    EvidenceRef(
        "_research/android/game-v3.2.3/extracted/lib/arm64-v8a/libil2cpp.so",
        "0x7E714E0, 0x7E9F084, 0x7E9F194, 0x7E9F210, 0x7E937D4",
        "executor adds one permanent merged count status; target-classified future debuffs spend exactly one charge before their add path",
    ),
    EvidenceRef(
        "var/coverage/plan3_anti_debuff_native_audit.json",
        "android_v3_2_3.control_flow and conclusions",
        "body-level call order, merge/removal behavior, logging order, and PC evidence boundary",
    ),
)


ANTI_DEBUFF_CATALOG = AntiDebuffCatalog(
    scope="Plan3/Common",
    master_effect_rows=MASTER_EFFECT_ROWS,
    affected_card_versions=COMMON_AFFECTED_CARD_VERSIONS,
    out_of_scope_references=OUT_OF_SCOPE_REFERENCES,
    executor=EXECUTOR_EVIDENCE,
    evidence=EVIDENCE,
    contract=ANTI_DEBUFF_CONTRACT,
    order_basis="card.play_effects_json array index; declaration order only",
    runtime_order_proven=False,
    directly_unlocked_if_fixed_alone=5,
)

# Short aliases make the independent catalog convenient for callers without
# introducing a dependency on the existing engine's effect model.
CATALOG = ANTI_DEBUFF_CATALOG
EXECUTABLE_EFFECT_ROWS: tuple[AntiDebuffEffectRow, ...] = MASTER_EFFECT_ROWS


StateT = TypeVar("StateT")


@dataclass(frozen=True, slots=True)
class AntiDebuffRuntime:
    """Settled projection of the single merged native AntiDebuff status.

    ``uid`` and ``passing_turn_start`` are optional native identity metadata.
    The historical count-only API remains valid for plan-neutral callers;
    exact LocalSave/Plan3 paths additionally require a positive UID.
    """

    count: int = 0
    uid: int | None = None
    passing_turn_start: bool = False

    def __post_init__(self) -> None:
        if (
            isinstance(self.count, bool)
            or not isinstance(self.count, int)
            or not 0 <= self.count <= INT32_MAX
        ):
            raise ValueError("anti-debuff count must be a non-negative int32")
        if self.uid is not None and (
            isinstance(self.uid, bool)
            or not isinstance(self.uid, int)
            or not 1 <= self.uid <= INT32_MAX
        ):
            raise ValueError("anti-debuff uid must be a positive int32 or None")
        if not isinstance(self.passing_turn_start, bool):
            raise TypeError("anti-debuff passing_turn_start must be a boolean")
        if self.count == 0 and (
            self.uid is not None or self.passing_turn_start
        ):
            raise ValueError(
                "absent anti-debuff status must not retain native identity"
            )

    @property
    def status_present(self) -> bool:
        return self.count > 0

    @property
    def exact_status_identity(self) -> bool:
        return self.status_present and self.uid is not None

    def mark_passing_turn_start(self) -> "AntiDebuffRuntime":
        """Mirror the native outer TurnStart marker without spending count."""

        if not self.status_present or self.passing_turn_start:
            return self
        return replace(self, passing_turn_start=True)

    def to_json(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "count": self.count,
            "status_present": self.status_present,
        }
        # Preserve the historical count-only JSON surface when no native
        # identity was supplied.  Exact Plan3 checkpoints carry both fields.
        if self.uid is not None or self.passing_turn_start:
            payload["uid"] = self.uid
            payload["passing_turn_start"] = self.passing_turn_start
        return payload


@dataclass(frozen=True, slots=True)
class AntiDebuffDifference:
    """One native ``SetStatusDifference(ExamAntiDebuff=66)`` payload."""

    before: int
    after: int
    effect_type: str = EFFECT_TYPE
    effect_type_value: int = ANTI_DEBUFF_EFFECT_TYPE_VALUE

    def __post_init__(self) -> None:
        for name in ("before", "after"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= INT32_MAX
            ):
                raise ValueError(f"anti-debuff difference {name} is invalid")
        if (
            self.effect_type != EFFECT_TYPE
            or self.effect_type_value != ANTI_DEBUFF_EFFECT_TYPE_VALUE
        ):
            raise ValueError("anti-debuff difference identity is invalid")

    def to_json(self) -> dict[str, object]:
        return {
            "effect_type": self.effect_type,
            "effect_type_value": self.effect_type_value,
            "before": self.before,
            "after": self.after,
        }


AntiDebuffAddOperation: TypeAlias = Literal["installed", "stacked"]


@dataclass(frozen=True, slots=True)
class AntiDebuffExecution:
    """Resolved immutable projection of ``AntiDebuffEffectExecutor``."""

    status: ExecutionStatus
    source_effect_id: str
    effect_type: str
    state_before: AntiDebuffRuntime
    state_after: AntiDebuffRuntime
    count_added: int
    operation: AntiDebuffAddOperation
    status_turn: int
    callback_fired: bool
    difference: AntiDebuffDifference
    trace: tuple[str, ...]
    simulated: bool
    effect: AntiDebuffEffectRow

    def __post_init__(self) -> None:
        if self.status is not ExecutionStatus.EXECUTED:
            raise ValueError("resolved anti-debuff execution must be executed")
        if self.effect_type != EFFECT_TYPE or self.effect.effect_type != EFFECT_TYPE:
            raise ValueError("resolved anti-debuff effect type is invalid")
        if self.source_effect_id != self.effect.source_effect_id:
            raise ValueError("resolved anti-debuff source id is invalid")
        if self.count_added != self.effect.effect_count or self.count_added <= 0:
            raise ValueError("resolved anti-debuff count is invalid")
        if self.status_turn != ANTI_DEBUFF_PERMANENT_TURN:
            raise ValueError("anti-debuff status must be permanent")
        if self.operation not in ("installed", "stacked"):
            raise ValueError("anti-debuff operation is invalid")
        if self.callback_fired != (self.operation == "installed"):
            raise ValueError("native add callback is inconsistent")
        if self.difference != AntiDebuffDifference(
            self.state_before.count, self.state_after.count
        ):
            raise ValueError("anti-debuff difference does not match state")
        if self.state_after.count - self.state_before.count != self.count_added:
            raise ValueError("anti-debuff state transition is inconsistent")
        if self.operation == "stacked" and (
            self.state_after.uid != self.state_before.uid
            or self.state_after.passing_turn_start
            != self.state_before.passing_turn_start
        ):
            raise ValueError("stacked anti-debuff must preserve native identity")
        if self.operation == "installed" and (
            self.state_before.status_present
            or self.state_after.passing_turn_start
        ):
            raise ValueError("installed anti-debuff must begin fresh")
        if not isinstance(self.simulated, bool):
            raise TypeError("simulated must be a boolean")
        object.__setattr__(self, "trace", tuple(self.trace))

    @property
    def executable(self) -> bool:
        return True

    @property
    def changed(self) -> bool:
        return self.state_after != self.state_before

    @property
    def state_unchanged(self) -> bool:
        return not self.changed

    def to_json(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "source_effect_id": self.source_effect_id,
            "effect_type": self.effect_type,
            "count_added": self.count_added,
            "operation": self.operation,
            "status_turn": self.status_turn,
            "callback_fired": self.callback_fired,
            "simulated": self.simulated,
            "state_before": self.state_before.to_json(),
            "state_after": self.state_after.to_json(),
            "difference": self.difference.to_json(),
            "trace": list(self.trace),
            "effect": self.effect.to_json(),
        }


@dataclass(frozen=True, slots=True)
class AntiDebuffBlockResult:
    """Result of the native pre-add target-classification gate."""

    state_before: AntiDebuffRuntime
    state_after: AntiDebuffRuntime
    incoming_effect_type_value: int
    incoming_target_type: ExamStatusEffectTargetType
    blocked: bool
    consumed_count: int
    removed_status: bool
    difference: AntiDebuffDifference | None
    trace: tuple[str, ...]
    simulated: bool

    def __post_init__(self) -> None:
        if (
            isinstance(self.incoming_effect_type_value, bool)
            or not isinstance(self.incoming_effect_type_value, int)
            or self.incoming_effect_type_value < 0
        ):
            raise ValueError("incoming effect type value must be non-negative")
        if not isinstance(self.incoming_target_type, ExamStatusEffectTargetType):
            raise TypeError("incoming target type must be explicit")
        if not isinstance(self.simulated, bool):
            raise TypeError("simulated must be a boolean")
        expected_consumed = int(self.blocked)
        if self.consumed_count != expected_consumed:
            raise ValueError("blocked status must consume exactly one count")
        if self.blocked:
            if self.incoming_target_type is not ExamStatusEffectTargetType.DEBUFF:
                raise ValueError("only a debuff target can be blocked")
            if self.state_before.count <= 0:
                raise ValueError("a blocked status needs an available count")
            if self.state_after.count != self.state_before.count - 1:
                raise ValueError("blocked status count transition is invalid")
            if self.removed_status != (self.state_after.count == 0):
                raise ValueError("anti-debuff removal-at-zero is inconsistent")
            if self.removed_status:
                if (
                    self.state_after.uid is not None
                    or self.state_after.passing_turn_start
                ):
                    raise ValueError(
                        "removed anti-debuff must clear native identity"
                    )
            elif (
                self.state_after.uid != self.state_before.uid
                or self.state_after.passing_turn_start
                != self.state_before.passing_turn_start
            ):
                raise ValueError(
                    "spent anti-debuff must preserve native identity"
                )
            if self.difference != AntiDebuffDifference(
                self.state_before.count, self.state_after.count
            ):
                raise ValueError("blocked status difference is inconsistent")
        elif (
            self.state_after is not self.state_before
            or self.removed_status
            or self.difference is not None
        ):
            raise ValueError("non-blocked status must not mutate AntiDebuff")
        object.__setattr__(self, "trace", tuple(self.trace))

    @property
    def used_uid(self) -> int | None:
        return self.state_before.uid if self.blocked else None

    @property
    def removed_uid(self) -> int | None:
        return self.used_uid if self.removed_status else None

    def to_json(self) -> dict[str, object]:
        return {
            "incoming_effect_type_value": self.incoming_effect_type_value,
            "incoming_target_type": self.incoming_target_type.value,
            "blocked": self.blocked,
            "consumed_count": self.consumed_count,
            "removed_status": self.removed_status,
            "used_uid": self.used_uid,
            "removed_uid": self.removed_uid,
            "simulated": self.simulated,
            "state_before": self.state_before.to_json(),
            "state_after": self.state_after.to_json(),
            "difference": (
                self.difference.to_json() if self.difference is not None else None
            ),
            "trace": list(self.trace),
        }


@dataclass(frozen=True, slots=True)
class UnresolvedExecution(Generic[StateT]):
    """A pure, typed result which explicitly carries state through unchanged."""

    status: ExecutionStatus
    source_effect_id: str
    effect_type: str
    state_before: StateT
    state_after: StateT
    reasons: tuple[UnresolvedReason, ...]
    effect: AntiDebuffEffectRow | None

    @property
    def executable(self) -> bool:
        return False

    @property
    def changed(self) -> bool:
        return False

    @property
    def state_unchanged(self) -> bool:
        return self.state_before is self.state_after

    def to_json(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "source_effect_id": self.source_effect_id,
            "effect_type": self.effect_type,
            "reasons": [reason.value for reason in self.reasons],
            "changed": False,
            "state_policy": "state_after_is_state_before",
            "effect": self.effect.to_json() if self.effect is not None else None,
        }


def execute_anti_debuff(
    effect: object,
    state: StateT,
    *,
    simulate: bool = False,
    install_uid: int | None = None,
) -> AntiDebuffExecution | UnresolvedExecution[StateT]:
    """Execute a canonical Master row against the proven scalar projection.

    Both normal and simulated calls return the same projected native state.
    This function is pure, so the input is never mutated; ``simulate`` records
    the caller's intent and does not suppress the executor's transition.
    """

    if not isinstance(simulate, bool):
        raise TypeError("simulate must be a boolean")
    if install_uid is not None and (
        isinstance(install_uid, bool)
        or not isinstance(install_uid, int)
        or not 1 <= install_uid <= INT32_MAX
    ):
        raise ValueError("install_uid must be a positive int32 or None")

    if isinstance(effect, AntiDebuffEffectRow):
        source_effect_id = effect.source_effect_id
        effect_type = effect.effect_type
        canonical = effect in ANTI_DEBUFF_CATALOG.master_effect_rows
        if not canonical:
            return UnresolvedExecution(
                status=ExecutionStatus.UNRESOLVED,
                source_effect_id=source_effect_id,
                effect_type=effect_type,
                state_before=state,
                state_after=state,
                reasons=(UnresolvedReason.UNSUPPORTED_ROW_SHAPE,),
                effect=effect,
            )
        if not isinstance(state, AntiDebuffRuntime):
            return UnresolvedExecution(
                status=ExecutionStatus.UNRESOLVED,
                source_effect_id=source_effect_id,
                effect_type=effect_type,
                state_before=state,
                state_after=state,
                reasons=(UnresolvedReason.UNSUPPORTED_STATE_SHAPE,),
                effect=effect,
            )
        if state.count > INT32_MAX - effect.effect_count:
            return UnresolvedExecution(
                status=ExecutionStatus.UNRESOLVED,
                source_effect_id=source_effect_id,
                effect_type=effect_type,
                state_before=state,
                state_after=state,
                reasons=(UnresolvedReason.COUNT_OVERFLOW,),
                effect=effect,
            )

        operation: AntiDebuffAddOperation = (
            "stacked" if state.status_present else "installed"
        )
        if operation == "stacked":
            if install_uid is not None:
                raise ValueError("stacking AntiDebuff must not allocate a new uid")
            after = replace(
                state,
                count=state.count + effect.effect_count,
            )
        else:
            after = AntiDebuffRuntime(
                state.count + effect.effect_count,
                uid=install_uid,
                passing_turn_start=False,
            )
        trace = [
            "get-anti-debuff-count:before",
            "get-effect-target-type:66=buff",
            "is-block-add-status:false",
            "get-first-anti-debuff-status:is-use=false",
        ]
        if operation == "stacked":
            trace.extend(("clear-description-cache", "add-count-existing-status"))
        else:
            trace.extend(
                (
                    f"construct-anti-debuff-status:count={effect.effect_count}:turn=-1",
                    "add-status:assign-uid-and-append",
                    "on-value-changed-callback",
                )
            )
        trace.extend(
            (
                "get-anti-debuff-count:after",
                "create-effect-difference",
                "set-status-difference:66",
                "append-effect-difference",
            )
        )
        return AntiDebuffExecution(
            status=ExecutionStatus.EXECUTED,
            source_effect_id=source_effect_id,
            effect_type=effect_type,
            state_before=state,
            state_after=after,
            count_added=effect.effect_count,
            operation=operation,
            status_turn=ANTI_DEBUFF_PERMANENT_TURN,
            callback_fired=operation == "installed",
            difference=AntiDebuffDifference(state.count, after.count),
            trace=tuple(trace),
            simulated=simulate,
            effect=effect,
        )

    return UnresolvedExecution(
        status=ExecutionStatus.UNRESOLVED,
        source_effect_id="",
        effect_type="",
        state_before=state,
        state_after=state,
        reasons=(UnresolvedReason.MALFORMED_EFFECT,),
        effect=None,
    )


def try_block_status_addition(
    state: AntiDebuffRuntime,
    *,
    incoming_effect_type_value: int,
    incoming_target_type: ExamStatusEffectTargetType,
    simulate: bool = False,
) -> AntiDebuffBlockResult:
    """Project ``IsBlockAddStatus`` without naming or ordering debuff kinds.

    Native selection is solely ``GetEffectTargetType(effectType) == Debuff``.
    The incoming status API receives ``false`` and skips its add path when this
    result is blocked.  Existing debuffs are never inspected.
    """

    if not isinstance(state, AntiDebuffRuntime):
        raise TypeError("state must be AntiDebuffRuntime")
    if not isinstance(incoming_target_type, ExamStatusEffectTargetType):
        raise TypeError("incoming target type must be ExamStatusEffectTargetType")
    if (
        isinstance(incoming_effect_type_value, bool)
        or not isinstance(incoming_effect_type_value, int)
        or incoming_effect_type_value < 0
    ):
        raise ValueError("incoming effect type value must be non-negative")
    if not isinstance(simulate, bool):
        raise TypeError("simulate must be a boolean")

    trace = [
        f"get-effect-target-type:{incoming_effect_type_value}={incoming_target_type.value.lower()}"
    ]
    if incoming_target_type is not ExamStatusEffectTargetType.DEBUFF:
        trace.append("is-block-add-status:false-non-debuff")
        return AntiDebuffBlockResult(
            state,
            state,
            incoming_effect_type_value,
            incoming_target_type,
            False,
            0,
            False,
            None,
            tuple(trace),
            simulate,
        )

    trace.append("get-anti-debuff-count:availability")
    if not state.status_present:
        trace.append("is-block-add-status:false-no-count")
        return AntiDebuffBlockResult(
            state,
            state,
            incoming_effect_type_value,
            incoming_target_type,
            False,
            0,
            False,
            None,
            tuple(trace),
            simulate,
        )

    before = state.count
    after = (
        AntiDebuffRuntime()
        if before == 1
        else replace(state, count=before - 1)
    )
    trace.extend(
        (
            "get-anti-debuff-count:before",
            "mark-anti-debuff-uid-recently-used",
            "spend-count:1",
        )
    )
    removed = not after.status_present
    if removed:
        trace.extend(("remove-status-by-uid", "append-removed-effect"))
    trace.extend(
        (
            "get-anti-debuff-count:after",
            "create-effect-difference",
            "set-status-difference:66",
            "append-effect-difference",
            "caller-skips-incoming-status",
        )
    )
    return AntiDebuffBlockResult(
        state,
        after,
        incoming_effect_type_value,
        incoming_target_type,
        True,
        1,
        removed,
        AntiDebuffDifference(before, after.count),
        tuple(trace),
        simulate,
    )


def catalog_json() -> dict[str, object]:
    """Return a JSON-ready snapshot without exposing mutable catalog state."""

    return json.loads(json.dumps(ANTI_DEBUFF_CATALOG.to_json()))


__all__ = [
    "ANTI_DEBUFF_CATALOG",
    "ANTI_DEBUFF_CONTRACT",
    "CATALOG",
    "COMMON_AFFECTED_CARD_VERSIONS",
    "EXECUTION_EFFECT_FIELD_ORDER",
    "EXECUTABLE_EFFECT_ROWS",
    "EFFECT_TYPE",
    "EVIDENCE",
    "ANTI_DEBUFF_EFFECT_TYPE_VALUE",
    "ANTI_DEBUFF_PERMANENT_TURN",
    "AntiDebuffBlockResult",
    "ExecutionStatus",
    "ExamStatusEffectTargetType",
    "ExecutorEvidence",
    "EvidenceRef",
    "AntiDebuffCatalog",
    "AntiDebuffContract",
    "AntiDebuffDifference",
    "AntiDebuffEffectRow",
    "AntiDebuffExecution",
    "AntiDebuffRuntime",
    "CardEffectSlot",
    "CardVersionRef",
    "CommonCardVersion",
    "EffectField",
    "MASTER_EFFECT_ROWS",
    "RAW_EFFECT_FIELD_ORDER",
    "UnresolvedExecution",
    "UnresolvedReason",
    "catalog_json",
    "execute_anti_debuff",
    "try_block_status_addition",
]
