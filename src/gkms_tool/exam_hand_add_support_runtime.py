"""Plan-neutral projection of native HandAdd support runtime inputs.

``ExamSaveData.supportCardList`` is the runtime authority for support IDs,
lesson filters, loadout order, card-search IDs, and upgrade permils.  This
module turns that already-decoded data into the common immutable HandAdd
catalog/request consumed by every exam plan.  It does not execute an action,
modify a horizon, or read a captured post-state.

Plan-specific modules may keep compatibility aliases, but the projection and
its value objects live here so Plan 1/2/3 adapters do not need to depend on a
Plan 2-named parser for a shared native data shape.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .audition_local_save_state import (
    AuditionLocalSaveStateEvidence,
    LocalSaveExamState,
)
from .audition_support_runtime import (
    LESSON_TYPE_UNKNOWN,
    SUPPORTED_LESSON_TYPES,
    SupportUpgradeRuntimeInput,
)
from .card_search import ProduceCardSearchRule, load_produce_card_search
from .master_db import DEFAULT_DATABASE


EXAM_HAND_ADD_SUPPORT_RUNTIME_SCHEMA_VERSION: Final = 1
EXAM_HAND_ADD_SUPPORT_RUNTIME_SOURCE: Final = "ExamSaveData.supportCardList"
EXAM_HAND_ADD_SUPPORT_CARD_SEARCH_ID: Final = "p_card_search-hand"

_SUPPORT_FIELDS: Final = frozenset(
    {
        "_cardSearchId",
        "_filterParameterType",
        "_produceCardUpgradePermil",
        "_supportCardId",
    }
)
_LESSON_BY_PARAMETER: Final = {
    0: LESSON_TYPE_UNKNOWN,
    1: "ProduceStepLessonType_LessonVocal",
    2: "ProduceStepLessonType_LessonDance",
    3: "ProduceStepLessonType_LessonVisual",
}
_LESSON_EXAM_TYPE = 0
_AUDITION_EXAM_TYPE = 1


class ExamHandAddSupportRuntimeError(ValueError):
    """Typed error for an incomplete shared support-runtime projection."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = str(code)
        self.detail = str(detail)
        super().__init__(self.code if not detail else f"{self.code}:{self.detail}")


def _error(code: str, detail: str = "") -> ExamHandAddSupportRuntimeError:
    return ExamHandAddSupportRuntimeError(code, detail)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _error("support-runtime-text-invalid", label)
    return value


def _uint(value: object, label: str, *, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= maximum
    ):
        raise _error("support-runtime-integer-invalid", label)
    return int(value)


def _lesson_type_for_state(state: LocalSaveExamState) -> str:
    if state.exam_type == _LESSON_EXAM_TYPE:
        parameter = ((state.step_type_value - 1) // 3) + 1
        lesson_type = _LESSON_BY_PARAMETER.get(parameter)
        if lesson_type is None:
            raise _error(
                "support-runtime-lesson-stage-unsupported",
                str(state.step_type_value),
            )
        return lesson_type
    if state.exam_type == _AUDITION_EXAM_TYPE:
        index = state.current_turn - 1
        if index < 0 or index >= len(state.turn_parameter_types):
            raise _error(
                "support-runtime-audition-parameter-missing",
                str(state.current_turn),
            )
        parameter = state.turn_parameter_types[index]
        lesson_type = _LESSON_BY_PARAMETER.get(parameter)
        if lesson_type is None:
            raise _error(
                "support-runtime-audition-parameter-unsupported",
                str(parameter),
            )
        return lesson_type
    raise _error("support-runtime-exam-type-unsupported", str(state.exam_type))


def _validate_drawn_guids(drawn_guids: Sequence[str]) -> tuple[str, ...]:
    try:
        values = tuple(drawn_guids)
    except TypeError as error:
        raise _error("support-runtime-drawn-guids-invalid") from error
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise _error("support-runtime-drawn-guid-invalid")
    if len(values) != len(set(values)):
        raise _error("support-runtime-drawn-guid-duplicate")
    return values


def _validate_used_support_ids(
    values: Sequence[str], support_ids: tuple[str, ...]
) -> tuple[str, ...]:
    try:
        used = tuple(values)
    except TypeError as error:
        raise _error("support-runtime-used-id-invalid") from error
    if any(not isinstance(value, str) or not value.strip() for value in used):
        raise _error("support-runtime-used-id-invalid")
    if len(used) != len(set(used)):
        raise _error("support-runtime-used-id-duplicate")
    unknown = tuple(value for value in used if value not in support_ids)
    if unknown:
        raise _error("support-runtime-used-id-unknown", unknown[0])
    return used


@dataclass(frozen=True, slots=True)
class ExamHandAddSupportCatalog:
    """Immutable common support/search inputs already bound by a caller."""

    support_upgrades: tuple[SupportUpgradeRuntimeInput, ...]
    support_card_searches: tuple[ProduceCardSearchRule, ...]
    # HandAdd asks native ``IsUpgradable`` for the exact next card version.
    # Runtime-only projections may leave this unbound; executable Plan 2
    # paths bind the current Master universe before applying a draw.
    master_card_refs: tuple[tuple[str, int], ...] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "support_upgrades", tuple(self.support_upgrades))
        object.__setattr__(
            self,
            "support_card_searches",
            tuple(self.support_card_searches),
        )
        if self.master_card_refs is not None:
            refs = tuple(self.master_card_refs)
            if refs != tuple(sorted(set(refs))):
                raise ValueError("master_card_refs must be sorted and unique")
            if any(
                not isinstance(card_id, str)
                or not card_id
                or not isinstance(upgrade, int)
                or isinstance(upgrade, bool)
                or upgrade < 0
                for card_id, upgrade in refs
            ):
                raise ValueError("master_card_refs contain an invalid card ref")
            object.__setattr__(self, "master_card_refs", refs)


@dataclass(frozen=True, slots=True)
class ExamHandAddSupportInput:
    """One exact post-draw HandAdd event in native GUID order."""

    drawn_guids: tuple[str, ...]
    lesson_type: str
    used_support_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "drawn_guids", tuple(self.drawn_guids))
        object.__setattr__(self, "used_support_ids", tuple(self.used_support_ids))


@dataclass(frozen=True, slots=True)
class ExamHandAddSupportRuntimeProjection:
    """Shared catalog/request projection for one decoded ExamSave snapshot."""

    schema_version: int
    catalog: ExamHandAddSupportCatalog
    request: ExamHandAddSupportInput
    random_state: int
    source: str = EXAM_HAND_ADD_SUPPORT_RUNTIME_SOURCE
    source_path: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != EXAM_HAND_ADD_SUPPORT_RUNTIME_SCHEMA_VERSION:
            raise ValueError("unsupported Exam HandAdd support runtime schema")
        if not isinstance(self.catalog, ExamHandAddSupportCatalog):
            raise TypeError("catalog must be ExamHandAddSupportCatalog")
        if not isinstance(self.request, ExamHandAddSupportInput):
            raise TypeError("request must be ExamHandAddSupportInput")
        _uint(self.random_state, "random_state", maximum=0xFFFFFFFF)
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("source must be non-empty text")
        if self.source_path is not None and (
            not isinstance(self.source_path, str) or not self.source_path.strip()
        ):
            raise ValueError("source_path must be non-empty text or None")

    @property
    def input(self) -> ExamHandAddSupportInput:
        return self.request

    @property
    def support_upgrades(self) -> tuple[SupportUpgradeRuntimeInput, ...]:
        return self.catalog.support_upgrades

    @property
    def support_card_searches(self) -> tuple[ProduceCardSearchRule, ...]:
        return self.catalog.support_card_searches

    @property
    def used_support_ids(self) -> tuple[str, ...]:
        return self.request.used_support_ids

    @property
    def runtime_permils(self) -> tuple[int, ...]:
        return tuple(value.runtime_permil for value in self.support_upgrades)


def project_exam_hand_add_support_runtime_from_root(
    support_card_list: Sequence[Mapping[str, object]],
    *,
    lesson_type: str,
    random_state: int,
    turn_use_support_ids: Sequence[str] = (),
    drawn_guids: Sequence[str] = (),
    database: str | Path = DEFAULT_DATABASE,
    source_path: str | None = None,
) -> ExamHandAddSupportRuntimeProjection:
    """Project exact root support rows without Master-derived support values."""

    if lesson_type not in SUPPORTED_LESSON_TYPES:
        raise _error("support-runtime-lesson-type-invalid", str(lesson_type))
    random_state = _uint(random_state, "random_state", maximum=0xFFFFFFFF)
    try:
        rows = tuple(support_card_list)
    except TypeError as error:
        raise _error("support-runtime-card-list-invalid") from error
    if not rows:
        raise _error("support-runtime-card-list-empty")

    supports: list[SupportUpgradeRuntimeInput] = []
    seen_ids: set[str] = set()
    search_rules: dict[str, ProduceCardSearchRule] = {}
    for order, raw in enumerate(rows):
        if not isinstance(raw, Mapping):
            raise _error("support-runtime-row-invalid", str(order))
        if set(raw) != set(_SUPPORT_FIELDS):
            missing = sorted(_SUPPORT_FIELDS - set(raw))
            unknown = sorted(set(raw) - _SUPPORT_FIELDS)
            raise _error(
                "support-runtime-row-shape-invalid",
                f"{order}:missing={missing}:unknown={unknown}",
            )
        support_id = _text(
            raw["_supportCardId"],
            f"supportCardList[{order}]._supportCardId",
        )
        if support_id in seen_ids:
            raise _error("support-runtime-support-id-duplicate", support_id)
        seen_ids.add(support_id)
        parameter = _uint(
            raw["_filterParameterType"],
            f"supportCardList[{order}]._filterParameterType",
            maximum=3,
        )
        if parameter not in _LESSON_BY_PARAMETER:
            raise _error(
                "support-runtime-parameter-type-unsupported",
                f"{support_id}:{parameter}",
            )
        card_search_id = _text(
            raw["_cardSearchId"],
            f"supportCardList[{order}]._cardSearchId",
        )
        if card_search_id != EXAM_HAND_ADD_SUPPORT_CARD_SEARCH_ID:
            raise _error(
                "support-runtime-card-search-unsupported",
                f"{support_id}:{card_search_id}",
            )
        runtime_permil = _uint(
            raw["_produceCardUpgradePermil"],
            f"supportCardList[{order}]._produceCardUpgradePermil",
            maximum=1000,
        )
        try:
            support = SupportUpgradeRuntimeInput(
                support_id=support_id,
                lesson_type=_LESSON_BY_PARAMETER[parameter],
                runtime_permil=runtime_permil,
                loadout_order=order,
                card_search_id=card_search_id,
            )
        except (TypeError, ValueError) as error:
            raise _error(
                "support-runtime-input-invalid",
                f"{support_id}:{error}",
            ) from error
        supports.append(support)
        if card_search_id not in search_rules:
            try:
                search_rules[card_search_id] = load_produce_card_search(
                    card_search_id,
                    database=Path(database),
                )
            except (KeyError, OSError, TypeError, ValueError) as error:
                raise _error(
                    "support-runtime-card-search-load-failed",
                    f"{card_search_id}:{type(error).__name__}:{error}",
                ) from error

    support_ids = tuple(value.support_id for value in supports)
    used = _validate_used_support_ids(turn_use_support_ids, support_ids)
    drawn = _validate_drawn_guids(drawn_guids)
    catalog = ExamHandAddSupportCatalog(
        support_upgrades=tuple(supports),
        support_card_searches=tuple(search_rules.values()),
    )
    request = ExamHandAddSupportInput(
        drawn_guids=drawn,
        lesson_type=lesson_type,
        used_support_ids=used,
    )
    return ExamHandAddSupportRuntimeProjection(
        schema_version=EXAM_HAND_ADD_SUPPORT_RUNTIME_SCHEMA_VERSION,
        catalog=catalog,
        request=request,
        random_state=random_state,
        source_path=source_path,
    )


def project_exam_hand_add_support_runtime(
    source: LocalSaveExamState | AuditionLocalSaveStateEvidence,
    *,
    drawn_guids: Sequence[str] = (),
    database: str | Path = DEFAULT_DATABASE,
) -> ExamHandAddSupportRuntimeProjection:
    """Project one typed LocalSave state into the shared HandAdd boundary."""

    if isinstance(source, AuditionLocalSaveStateEvidence):
        state = source.state
        source_path = source.source_path
    elif isinstance(source, LocalSaveExamState):
        state = source
        source_path = None
    else:
        raise TypeError(
            "source must be LocalSaveExamState or AuditionLocalSaveStateEvidence"
        )
    if state.root_runtime is None:
        raise _error("support-runtime-root-runtime-missing")
    opaque = state.root_runtime.opaque_fields.to_value()
    if not isinstance(opaque, Mapping):
        raise _error("support-runtime-opaque-fields-invalid")
    raw_rows = opaque.get("supportCardList")
    if not isinstance(raw_rows, list):
        raise _error("support-runtime-card-list-missing")
    lesson_type = _lesson_type_for_state(state)
    return project_exam_hand_add_support_runtime_from_root(
        raw_rows,  # type: ignore[arg-type]
        lesson_type=lesson_type,
        random_state=state.random_state,
        turn_use_support_ids=state.turn_use_support_ids,
        drawn_guids=drawn_guids,
        database=database,
        source_path=source_path,
    )


build_exam_hand_add_support_runtime = project_exam_hand_add_support_runtime


__all__ = [
    "EXAM_HAND_ADD_SUPPORT_RUNTIME_SCHEMA_VERSION",
    "EXAM_HAND_ADD_SUPPORT_RUNTIME_SOURCE",
    "EXAM_HAND_ADD_SUPPORT_CARD_SEARCH_ID",
    "ExamHandAddSupportRuntimeError",
    "ExamHandAddSupportCatalog",
    "ExamHandAddSupportInput",
    "ExamHandAddSupportRuntimeProjection",
    "build_exam_hand_add_support_runtime",
    "project_exam_hand_add_support_runtime",
    "project_exam_hand_add_support_runtime_from_root",
]
