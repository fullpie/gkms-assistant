"""Typed Plan2 lesson facts for FKTN Initial Regular school event 022.

This catalog is deliberately separate from the route-week lesson catalog.
The event suggestions expose three logical ``exam_n`` continuation IDs, while
``ProduceStepLesson`` owns different stage IDs.  There is no Master foreign
key between those surfaces.  A global naming audit and explicit per-character
scope are therefore retained as typed evidence; an authoritative
current-progress stage ID supplied by the caller takes precedence.

The module performs no game, device, or network I/O.  Android evidence below
is a static contract only: the 3.2.3 client passes ``UserProduceProgress``'s
current ``StepId`` through ``GoStepAsync`` to
``CreateProduceExamTransitionParam`` without rewriting an ``exam_n`` alias.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
from pathlib import Path
import re
import sqlite3
from typing import TYPE_CHECKING, Final, Iterable, Mapping

import yaml

from .initial_regular_inner_protocol import (
    InitialRegularInnerRuntimeRefs,
    InitialRegularInnerStageRequest,
    build_initial_regular_lesson_stage_request,
    validate_initial_regular_inner_stage_request,
)
from .master_db import DEFAULT_DATABASE
from .produce_rollout import (
    ChanceBranch,
    ExternalKind,
    ExternalRequest,
    ProduceRolloutState,
    RolloutPhase,
)

if TYPE_CHECKING:
    from .plan2_native_stage_bootstrap import Plan2NativeStageFacts


PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
DEFAULT_MASTER_DIR: Final = PROJECT_ROOT / "_research" / "gakumasu-diff"
DEFAULT_ANDROID_DUMP_CS: Final = (
    PROJECT_ROOT
    / "_research"
    / "android"
    / "game-v3.2.3"
    / "il2cppdumper"
    / "dump.cs"
)
DEFAULT_ANDROID_LIB: Final = (
    PROJECT_ROOT
    / "_research"
    / "android"
    / "game-v3.2.3"
    / "extracted"
    / "lib"
    / "arm64-v8a"
    / "libil2cpp.so"
)

INITIAL_REGULAR_EVENT_LESSON_DETAIL_ID: Final = (
    "event-detail-school-001-fktn-022"
)
INITIAL_REGULAR_EVENT_LESSON_PRODUCE_ID: Final = "produce-001"
INITIAL_REGULAR_EVENT_LESSON_CHARACTER_ID: Final = "fktn"
INITIAL_REGULAR_EVENT_LESSON_PLAN_TYPE: Final = "plan2"
INITIAL_REGULAR_EVENT_LESSON_MASTER_PLAN_TYPE: Final = (
    "ProducePlanType_Plan2"
)
INITIAL_REGULAR_EVENT_LESSON_ADAPTER_REF: Final = (
    "gkms_tool.initial_regular_plan2_inner_adapter"
)
INITIAL_REGULAR_EVENT_LESSON_REQUIRED_FIELD: Final = "inner_stage_request"
PLAN2_LESSON_EXAM_TYPE: Final = 0
_UINT32_MAX: Final = 0xFFFFFFFF

_NEXT_STEP_PATTERN = re.compile(
    r"^exam_n-002_school-event_(?P<character>[a-z0-9]+)_"
    r"(?P<attribute>vo|da|vi)$"
)
_LESSON_STAGE_PATTERN = re.compile(
    r"^p_step_lesson_level-002-(?P<character>[a-z0-9]+)-"
    r"school-event-(?P<attribute>vo|da|vi)$"
)


class InitialRegularEventLessonAttribute(StrEnum):
    VOCAL = "vocal"
    DANCE = "dance"
    VISUAL = "visual"


class InitialRegularEventLessonEvidenceKind(StrEnum):
    """Authority attached to one fact rather than a generic provenance blob."""

    PC_MASTER_EXACT = "pc_master_exact"
    PC_MASTER_FOREIGN_KEY = "pc_master_foreign_key"
    NAMING_BRIDGE = "naming_bridge"
    ANDROID_CURRENT_PROGRESS_STEP_ID = "android_current_progress_step_id"
    ANDROID_STATIC_CONTRACT = "android_static_contract"


@dataclass(frozen=True, slots=True, order=True)
class InitialRegularEventLessonIssue:
    code: str
    field: str
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not self.code:
            raise ValueError("issue code must be non-empty text")
        if not isinstance(self.field, str) or not self.field:
            raise ValueError("issue field must be non-empty text")
        if not isinstance(self.detail, str):
            raise TypeError("issue detail must be text")


@dataclass(frozen=True, slots=True, order=True)
class InitialRegularEventLessonEvidence:
    kind: InitialRegularEventLessonEvidenceKind
    field: str
    source: str
    detail: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, InitialRegularEventLessonEvidenceKind):
            raise TypeError("evidence kind is invalid")
        if not self.field or not self.source or not self.detail:
            raise ValueError("evidence field, source, and detail are required")


@dataclass(frozen=True, slots=True, order=True)
class InitialRegularEventLessonNamingException:
    code: str
    character_id: str
    attribute_code: str
    next_step_id: str | None
    expected_stage_id: str | None


@dataclass(frozen=True, slots=True)
class InitialRegularEventLessonNamingScope:
    """One character's exact structural slice of the global naming audit."""

    character_id: str
    next_step_count: int
    lesson_stage_count: int
    matched_count: int
    exceptions: tuple[InitialRegularEventLessonNamingException, ...]

    @property
    def complete(self) -> bool:
        return (
            self.next_step_count > 0
            and not self.exceptions
            and self.next_step_count == self.lesson_stage_count
            and self.matched_count == self.next_step_count
        )


@dataclass(frozen=True, slots=True)
class InitialRegularEventLessonNamingBridgeAudit:
    """Global structural audit; callers consume an explicit character scope."""

    next_step_count: int
    lesson_stage_count: int
    matched_count: int
    exceptions: tuple[InitialRegularEventLessonNamingException, ...]
    scopes: tuple[InitialRegularEventLessonNamingScope, ...]
    evidence_kind: InitialRegularEventLessonEvidenceKind = (
        InitialRegularEventLessonEvidenceKind.NAMING_BRIDGE
    )

    @property
    def complete(self) -> bool:
        return (
            self.next_step_count > 0
            and not self.exceptions
            and self.next_step_count == self.lesson_stage_count
            and self.matched_count == self.next_step_count
        )

    def for_character(
        self, character_id: str
    ) -> InitialRegularEventLessonNamingScope | None:
        matches = tuple(
            scope for scope in self.scopes if scope.character_id == character_id
        )
        return matches[0] if len(matches) == 1 else None


@dataclass(frozen=True, slots=True)
class InitialRegularEventLessonAndroidEvidence:
    """Documented static boundary plus an optional development-time audit."""

    version: str
    transition_factory_rva: int
    go_step_move_next_rva: int
    transition_call_rva: int
    progress_step_type_offset: int
    progress_step_id_offset: int
    audit_performed: bool
    signature_present: bool | None
    enum_values_present: bool | None
    native_image_present: bool | None
    client_alias_rewrite: bool
    evidence_kind: InitialRegularEventLessonEvidenceKind = (
        InitialRegularEventLessonEvidenceKind.ANDROID_STATIC_CONTRACT
    )

    @property
    def complete(self) -> bool:
        return (
            self.audit_performed
            and self.signature_present is True
            and self.enum_values_present is True
            and self.native_image_present is True
            and self.client_alias_rewrite is False
        )


@dataclass(frozen=True, slots=True)
class InitialRegularEventLessonEvidenceReport:
    exact: tuple[InitialRegularEventLessonEvidence, ...]
    naming_bridge: InitialRegularEventLessonNamingBridgeAudit
    android: InitialRegularEventLessonAndroidEvidence


@dataclass(frozen=True, slots=True)
class InitialRegularEventLessonStaticFact:
    """Exact event row and the separately proven candidate lesson row."""

    detail_id: str
    suggestion_id: str
    suggestion_index: int
    continuation_step_type: str
    continuation_step_id: str
    attribute: InitialRegularEventLessonAttribute
    attribute_code: str
    current_parameter_type: int
    lesson_type: str
    step_type_value: int
    candidate_stage_id: str
    lesson_level_id: str
    setting_id: str
    plan_type: str
    exam_type: int
    base_turns: int
    base_clear_target: int
    base_limit_target: int

    def __post_init__(self) -> None:
        for name in (
            "detail_id",
            "suggestion_id",
            "continuation_step_type",
            "continuation_step_id",
            "attribute_code",
            "lesson_type",
            "candidate_stage_id",
            "lesson_level_id",
            "setting_id",
            "plan_type",
        ):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"{name} must be non-empty text")
        if self.suggestion_index not in (1, 2, 3):
            raise ValueError("suggestion index must be 1, 2, or 3")
        if self.exam_type != PLAN2_LESSON_EXAM_TYPE:
            raise ValueError("event lesson exam_type must be lesson")
        if self.base_turns < 1:
            raise ValueError("base turns must be positive")
        if not 0 < self.base_clear_target < self.base_limit_target:
            raise ValueError("base lesson borders are invalid")


@dataclass(frozen=True, slots=True)
class InitialRegularEventLessonRuntimeFacts:
    """Runtime facts absent from the event and lesson Master rows.

    ``current_progress_step_id`` is the authoritative response-side stage ID.
    It may be omitted only when this character's naming bridge audit is complete.
    Parameter limits are injected because ``UserProduceProgress`` can carry
    per-attribute additional limits beyond the mode's base cap.
    """

    current_progress_step_id: str | None
    parameter_limits: tuple[int, int, int] | None
    lesson_limit_up_score: int | None
    exam_extra_turn: int | None
    battle_bonus_permille: tuple[int, int, int] | None
    gimmick_group_id: str | None

    def __post_init__(self) -> None:
        if self.current_progress_step_id is not None and (
            not isinstance(self.current_progress_step_id, str)
            or not self.current_progress_step_id
        ):
            raise ValueError("current_progress_step_id must be non-empty or None")
        if self.parameter_limits is not None:
            limits = tuple(self.parameter_limits)
            if len(limits) != 3 or any(type(value) is not int or value < 0 for value in limits):
                raise ValueError("parameter_limits must contain three non-negative integers")
            object.__setattr__(self, "parameter_limits", limits)
        for name in ("lesson_limit_up_score", "exam_extra_turn"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"{name} must be a non-negative integer or None")
        if self.battle_bonus_permille is not None:
            bonuses = tuple(self.battle_bonus_permille)
            if len(bonuses) != 3 or any(type(value) is not int or value < 0 for value in bonuses):
                raise ValueError(
                    "battle_bonus_permille must contain three non-negative integers"
                )
            object.__setattr__(self, "battle_bonus_permille", bonuses)
        if self.gimmick_group_id is not None and not isinstance(
            self.gimmick_group_id, str
        ):
            raise TypeError("gimmick_group_id must be text or None")


@dataclass(frozen=True, slots=True)
class InitialRegularEventLessonResolvedRuntime:
    exam_extra_turn: int
    battle_bonus_permille: tuple[int, int, int]
    gimmick_group_id: str


@dataclass(frozen=True, slots=True)
class InitialRegularEventLessonStageFact:
    """Resolved stage fact satisfying both common and Plan2 structural APIs."""

    stage_id: str
    lesson_level_id: str
    plan_type: str
    setting_id: str
    attribute: InitialRegularEventLessonAttribute
    current_parameter_type: int
    lesson_type: str
    step_type_value: int
    base_turns: int
    base_clear_target: int
    base_limit_target: int
    start_stamina: int
    max_stamina: int
    current_attribute: int
    attribute_limit: int
    clear_border: int
    runtime_turns: int
    limit_border: int
    runtime_context: InitialRegularEventLessonResolvedRuntime
    continuation_step_id: str
    continuation_step_type: str
    stage_id_evidence_kind: InitialRegularEventLessonEvidenceKind
    is_sp: bool = False
    is_hard: bool = False
    variant: str = "normal"


@dataclass(frozen=True, slots=True)
class InitialRegularEventLessonCatalog:
    detail_id: str
    entries: tuple[InitialRegularEventLessonStaticFact, ...]
    evidence: InitialRegularEventLessonEvidenceReport
    issues: tuple[InitialRegularEventLessonIssue, ...]
    master_dir: Path
    database: Path

    @property
    def available(self) -> bool:
        return len(self.entries) == 3 and not self.issues

    def for_next_step(
        self, next_step_id: str
    ) -> InitialRegularEventLessonStaticFact | None:
        matches = tuple(
            entry for entry in self.entries if entry.continuation_step_id == next_step_id
        )
        return matches[0] if len(matches) == 1 else None


@dataclass(frozen=True, slots=True)
class InitialRegularEventLessonBuildResult:
    request: InitialRegularInnerStageRequest | None
    stage_fact: InitialRegularEventLessonStageFact | None
    plan2_stage_facts: Plan2NativeStageFacts | None
    issues: tuple[InitialRegularEventLessonIssue, ...]
    evidence: InitialRegularEventLessonEvidenceReport

    @property
    def available(self) -> bool:
        return (
            self.request is not None
            and self.stage_fact is not None
            and self.plan2_stage_facts is not None
            and not self.issues
        )


_EXACT_SUGGESTIONS: Final = (
    (
        "p_s_e_s-event-detail-school-001-fktn-022-01",
        1,
        "ProduceStepType_LessonVocalNormal",
        "exam_n-002_school-event_fktn_vo",
        InitialRegularEventLessonAttribute.VOCAL,
        "vo",
        1,
        "ProduceStepLessonType_LessonVocal",
        "p_step_lesson_level-002-fktn-school-event-vo",
    ),
    (
        "p_s_e_s-event-detail-school-001-fktn-022-02",
        2,
        "ProduceStepType_LessonDanceNormal",
        "exam_n-002_school-event_fktn_da",
        InitialRegularEventLessonAttribute.DANCE,
        "da",
        4,
        "ProduceStepLessonType_LessonDance",
        "p_step_lesson_level-002-fktn-school-event-da",
    ),
    (
        "p_s_e_s-event-detail-school-001-fktn-022-03",
        3,
        "ProduceStepType_LessonVisualNormal",
        "exam_n-002_school-event_fktn_vi",
        InitialRegularEventLessonAttribute.VISUAL,
        "vi",
        7,
        "ProduceStepLessonType_LessonVisual",
        "p_step_lesson_level-002-fktn-school-event-vi",
    ),
)

_EXPECTED_BY_ID: Final = {entry[3]: entry for entry in _EXACT_SUGGESTIONS}
_STEP_TYPE_BY_CODE: Final = {
    "vo": "ProduceStepType_LessonVocalNormal",
    "da": "ProduceStepType_LessonDanceNormal",
    "vi": "ProduceStepType_LessonVisualNormal",
}
_ATTRIBUTE_INDEX: Final = {
    InitialRegularEventLessonAttribute.VOCAL: 0,
    InitialRegularEventLessonAttribute.DANCE: 1,
    InitialRegularEventLessonAttribute.VISUAL: 2,
}


def _issue(
    code: str, field: str, detail: str = ""
) -> InitialRegularEventLessonIssue:
    return InitialRegularEventLessonIssue(code, field, detail)


def _append_issue(
    issues: list[InitialRegularEventLessonIssue],
    code: str,
    field: str,
    detail: str = "",
) -> None:
    issue = _issue(code, field, detail)
    if issue not in issues:
        issues.append(issue)


def _yaml_rows(
    path: Path,
    label: str,
    issues: list[InitialRegularEventLessonIssue],
) -> tuple[Mapping[str, object], ...]:
    if not path.is_file():
        _append_issue(issues, "master-file-missing", label, str(path))
        return ()
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        _append_issue(
            issues,
            "master-file-unreadable",
            label,
            f"{type(error).__name__}:{error}",
        )
        return ()
    if not isinstance(payload, list) or any(not isinstance(row, Mapping) for row in payload):
        _append_issue(issues, "master-shape-invalid", label)
        return ()
    return tuple(payload)


def _rows_by_id(
    rows: Iterable[Mapping[str, object]],
) -> dict[str, tuple[Mapping[str, object], ...]]:
    grouped: dict[str, list[Mapping[str, object]]] = {}
    for row in rows:
        row_id = row.get("id")
        if isinstance(row_id, str) and row_id:
            grouped.setdefault(row_id, []).append(row)
    return {key: tuple(values) for key, values in grouped.items()}


def _one_yaml_row(
    grouped: Mapping[str, tuple[Mapping[str, object], ...]],
    row_id: str,
    field: str,
    issues: list[InitialRegularEventLessonIssue],
) -> Mapping[str, object] | None:
    rows = grouped.get(row_id, ())
    if not rows:
        _append_issue(issues, "master-row-missing", field, row_id)
        return None
    if len(rows) != 1:
        _append_issue(issues, "master-row-ambiguous", field, row_id)
        return None
    return rows[0]


def _query_rows(
    database: Path,
    sql: str,
    parameters: tuple[object, ...],
    field: str,
    issues: list[InitialRegularEventLessonIssue],
) -> tuple[sqlite3.Row, ...]:
    if not database.is_file():
        _append_issue(issues, "master-database-missing", field, str(database))
        return ()
    try:
        connection = sqlite3.connect(database)
        connection.row_factory = sqlite3.Row
        try:
            return tuple(connection.execute(sql, parameters).fetchall())
        finally:
            connection.close()
    except sqlite3.Error as error:
        _append_issue(
            issues,
            "master-query-failed",
            field,
            f"{type(error).__name__}:{error}",
        )
        return ()


def _naming_bridge_audit(
    database: Path,
    lesson_rows: tuple[Mapping[str, object], ...],
    issues: list[InitialRegularEventLessonIssue],
) -> InitialRegularEventLessonNamingBridgeAudit:
    next_rows = _query_rows(
        database,
        "SELECT id, step_id, step_type FROM step_event_suggestion "
        "WHERE step_id LIKE 'exam_n-002_school-event_%' ORDER BY step_id, id",
        (),
        "step_event_suggestion.naming_bridge",
        issues,
    )
    next_by_key: dict[tuple[str, str], list[sqlite3.Row]] = {}
    exceptions: list[InitialRegularEventLessonNamingException] = []
    for row in next_rows:
        step_id = str(row["step_id"])
        match = _NEXT_STEP_PATTERN.fullmatch(step_id)
        if match is None:
            exceptions.append(
                InitialRegularEventLessonNamingException(
                    "next-step-pattern-invalid", "", "", step_id, None
                )
            )
            continue
        key = (match.group("character"), match.group("attribute"))
        next_by_key.setdefault(key, []).append(row)
        expected_type = _STEP_TYPE_BY_CODE[key[1]]
        if row["step_type"] != expected_type:
            exceptions.append(
                InitialRegularEventLessonNamingException(
                    "next-step-type-mismatch",
                    key[0],
                    key[1],
                    step_id,
                    f"p_step_lesson_level-002-{key[0]}-school-event-{key[1]}",
                )
            )

    stage_by_key: dict[tuple[str, str], list[str]] = {}
    for row in lesson_rows:
        row_id = row.get("id")
        if not isinstance(row_id, str):
            continue
        match = _LESSON_STAGE_PATTERN.fullmatch(row_id)
        if match is not None:
            key = (match.group("character"), match.group("attribute"))
            stage_by_key.setdefault(key, []).append(row_id)

    for key in sorted(set(next_by_key) | set(stage_by_key)):
        next_matches = next_by_key.get(key, [])
        stage_matches = stage_by_key.get(key, [])
        expected_stage = (
            f"p_step_lesson_level-002-{key[0]}-school-event-{key[1]}"
        )
        next_step_id = (
            str(next_matches[0]["step_id"]) if next_matches else None
        )
        if len(next_matches) != 1:
            exceptions.append(
                InitialRegularEventLessonNamingException(
                    "next-step-missing" if not next_matches else "next-step-ambiguous",
                    key[0],
                    key[1],
                    next_step_id,
                    expected_stage,
                )
            )
        if len(stage_matches) != 1:
            exceptions.append(
                InitialRegularEventLessonNamingException(
                    "lesson-stage-missing" if not stage_matches else "lesson-stage-ambiguous",
                    key[0],
                    key[1],
                    next_step_id,
                    expected_stage,
                )
            )

    matched = sum(
        len(next_by_key.get(key, ())) == 1 and len(stage_by_key.get(key, ())) == 1
        for key in set(next_by_key) | set(stage_by_key)
    )
    characters = sorted(
        {character_id for character_id, _ in set(next_by_key) | set(stage_by_key)}
    )
    scopes = tuple(
        InitialRegularEventLessonNamingScope(
            character_id=character_id,
            next_step_count=sum(
                len(rows)
                for (candidate, _), rows in next_by_key.items()
                if candidate == character_id
            ),
            lesson_stage_count=sum(
                len(rows)
                for (candidate, _), rows in stage_by_key.items()
                if candidate == character_id
            ),
            matched_count=sum(
                len(next_by_key.get(key, ())) == 1
                and len(stage_by_key.get(key, ())) == 1
                for key in set(next_by_key) | set(stage_by_key)
                if key[0] == character_id
            ),
            exceptions=tuple(
                sorted(
                    value
                    for value in set(exceptions)
                    if value.character_id == character_id
                )
            ),
        )
        for character_id in characters
    )
    return InitialRegularEventLessonNamingBridgeAudit(
        next_step_count=sum(len(rows) for rows in next_by_key.values()),
        lesson_stage_count=sum(len(rows) for rows in stage_by_key.values()),
        matched_count=matched,
        exceptions=tuple(sorted(set(exceptions))),
        scopes=scopes,
    )


def _android_evidence(
    dump_cs: Path | None,
    native_image: Path | None,
) -> InitialRegularEventLessonAndroidEvidence:
    """Optionally re-check development artifacts without gating the catalog."""

    audit_performed = dump_cs is not None or native_image is not None
    signature_present: bool | None = None
    enum_values_present: bool | None = None
    native_present: bool | None = None
    if dump_cs is not None:
        signature_present = False
        enum_values_present = False
        try:
            if dump_cs.is_file():
                text = dump_cs.read_text(encoding="utf-8")
                signature_present = all(
                    marker in text
                    for marker in (
                        "CreateProduceExamTransitionParam(string stepId, ProduceStepType stepType",
                        "private ProduceStepType stepType_; // 0xD0",
                        "private string stepId_; // 0xD8",
                    )
                )
                enum_values_present = all(
                    marker in text
                    for marker in (
                        "public const ProduceStepType LessonVocalNormal = 1;",
                        "public const ProduceStepType LessonDanceNormal = 4;",
                        "public const ProduceStepType LessonVisualNormal = 7;",
                    )
                )
        except (OSError, UnicodeError):
            pass
    if native_image is not None:
        native_present = native_image.is_file()
    return InitialRegularEventLessonAndroidEvidence(
        version="3.2.3",
        transition_factory_rva=0x7FD2AC4,
        go_step_move_next_rva=0x76B4A64,
        transition_call_rva=0x76B5B18,
        progress_step_type_offset=0xD0,
        progress_step_id_offset=0xD8,
        audit_performed=audit_performed,
        signature_present=signature_present,
        enum_values_present=enum_values_present,
        native_image_present=native_present,
        client_alias_rewrite=False,
    )


def load_initial_regular_event_lesson_catalog(
    *,
    master_dir: Path = DEFAULT_MASTER_DIR,
    database: Path = DEFAULT_DATABASE,
    android_dump_cs: Path | None = None,
    android_native_image: Path | None = None,
) -> InitialRegularEventLessonCatalog:
    """Load event-022 facts; Android artifact checks are opt-in and non-gating."""

    master_dir = Path(master_dir)
    database = Path(database)
    issues: list[InitialRegularEventLessonIssue] = []
    lesson_rows = _yaml_rows(
        master_dir / "ProduceStepLesson.yaml", "ProduceStepLesson", issues
    )
    level_rows = _yaml_rows(
        master_dir / "ProduceStepLessonLevel.yaml",
        "ProduceStepLessonLevel",
        issues,
    )
    setting_rows = _yaml_rows(
        master_dir / "ExamSetting.yaml", "ExamSetting", issues
    )
    lesson_by_id = _rows_by_id(lesson_rows)
    level_by_id = _rows_by_id(level_rows)
    setting_by_id = _rows_by_id(setting_rows)

    detail_rows = _query_rows(
        database,
        "SELECT id, suggestion_ids_json FROM step_event_detail WHERE id = ?",
        (INITIAL_REGULAR_EVENT_LESSON_DETAIL_ID,),
        "step_event_detail",
        issues,
    )
    expected_suggestion_ids = tuple(row[0] for row in _EXACT_SUGGESTIONS)
    if len(detail_rows) != 1:
        _append_issue(
            issues,
            "master-row-missing" if not detail_rows else "master-row-ambiguous",
            "step_event_detail",
            INITIAL_REGULAR_EVENT_LESSON_DETAIL_ID,
        )
    else:
        try:
            actual_ids_raw = json.loads(detail_rows[0]["suggestion_ids_json"])
            actual_ids = tuple(actual_ids_raw) if isinstance(actual_ids_raw, list) else ()
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            actual_ids = ()
            _append_issue(
                issues,
                "master-field-invalid",
                "step_event_detail.suggestion_ids",
                f"{type(error).__name__}:{error}",
            )
        if actual_ids != expected_suggestion_ids:
            _append_issue(
                issues,
                "event-suggestions-mismatch",
                "step_event_detail.suggestion_ids",
                f"expected={expected_suggestion_ids}:actual={actual_ids}",
            )

    produce_rows = _query_rows(
        database,
        "SELECT id, exam_setting_id FROM produce_mode WHERE id = ?",
        (INITIAL_REGULAR_EVENT_LESSON_PRODUCE_ID,),
        "produce_mode",
        issues,
    )
    setting_id: str | None = None
    if len(produce_rows) != 1:
        _append_issue(
            issues,
            "master-row-missing" if not produce_rows else "master-row-ambiguous",
            "produce_mode",
            INITIAL_REGULAR_EVENT_LESSON_PRODUCE_ID,
        )
    else:
        raw_setting = produce_rows[0]["exam_setting_id"]
        if isinstance(raw_setting, str) and raw_setting:
            setting_id = raw_setting
            _one_yaml_row(setting_by_id, setting_id, "ExamSetting", issues)
        else:
            _append_issue(issues, "master-field-invalid", "produce_mode.exam_setting_id")

    static_facts: list[InitialRegularEventLessonStaticFact] = []
    exact_evidence: list[InitialRegularEventLessonEvidence] = []
    for (
        suggestion_id,
        suggestion_index,
        step_type,
        step_id,
        attribute,
        attribute_code,
        step_type_value,
        lesson_type,
        candidate_stage_id,
    ) in _EXACT_SUGGESTIONS:
        suggestion_rows = _query_rows(
            database,
            "SELECT id, step_type, step_id, always_successful "
            "FROM step_event_suggestion WHERE id = ?",
            (suggestion_id,),
            f"step_event_suggestion[{suggestion_id}]",
            issues,
        )
        if len(suggestion_rows) != 1:
            _append_issue(
                issues,
                "master-row-missing" if not suggestion_rows else "master-row-ambiguous",
                "step_event_suggestion",
                suggestion_id,
            )
            continue
        suggestion = suggestion_rows[0]
        for field_name, expected in (
            ("step_type", step_type),
            ("step_id", step_id),
            ("always_successful", 1),
        ):
            if suggestion[field_name] != expected:
                _append_issue(
                    issues,
                    "master-field-mismatch",
                    f"step_event_suggestion.{field_name}",
                    f"id={suggestion_id}:expected={expected}:actual={suggestion[field_name]}",
                )
        lesson_row = _one_yaml_row(
            lesson_by_id, candidate_stage_id, "ProduceStepLesson", issues
        )
        if lesson_row is None or setting_id is None:
            continue
        lesson_level_id = lesson_row.get("produceStepLessonLevelId")
        if not isinstance(lesson_level_id, str) or not lesson_level_id:
            _append_issue(
                issues,
                "master-field-invalid",
                "ProduceStepLesson.produceStepLessonLevelId",
                candidate_stage_id,
            )
            continue
        if lesson_level_id != "p_step_lesson_level-002-plan2-school-event":
            _append_issue(
                issues,
                "lesson-level-plan-mismatch",
                "ProduceStepLesson.produceStepLessonLevelId",
                f"stage={candidate_stage_id}:level={lesson_level_id}",
            )
        level_row = _one_yaml_row(
            level_by_id, lesson_level_id, "ProduceStepLessonLevel", issues
        )
        if level_row is None:
            continue
        values: dict[str, int] = {}
        for key in ("limitTurn", "successThreshold", "resultTargetValueLimit"):
            value = level_row.get(key)
            if type(value) is not int or value < 1:
                _append_issue(
                    issues,
                    "master-field-invalid",
                    f"ProduceStepLessonLevel.{key}",
                    lesson_level_id,
                )
            else:
                values[key] = value
        if len(values) != 3 or not (
            values["successThreshold"] < values["resultTargetValueLimit"]
        ):
            if len(values) == 3:
                _append_issue(
                    issues,
                    "lesson-border-order-invalid",
                    "ProduceStepLessonLevel",
                    lesson_level_id,
                )
            continue
        static_facts.append(
            InitialRegularEventLessonStaticFact(
                detail_id=INITIAL_REGULAR_EVENT_LESSON_DETAIL_ID,
                suggestion_id=suggestion_id,
                suggestion_index=suggestion_index,
                continuation_step_type=step_type,
                continuation_step_id=step_id,
                attribute=attribute,
                attribute_code=attribute_code,
                current_parameter_type=(step_type_value + 2) // 3,
                lesson_type=lesson_type,
                step_type_value=step_type_value,
                candidate_stage_id=candidate_stage_id,
                lesson_level_id=lesson_level_id,
                setting_id=setting_id,
                plan_type=INITIAL_REGULAR_EVENT_LESSON_PLAN_TYPE,
                exam_type=PLAN2_LESSON_EXAM_TYPE,
                base_turns=values["limitTurn"],
                base_clear_target=values["successThreshold"],
                base_limit_target=values["resultTargetValueLimit"],
            )
        )
        exact_evidence.extend(
            (
                InitialRegularEventLessonEvidence(
                    InitialRegularEventLessonEvidenceKind.PC_MASTER_EXACT,
                    f"suggestion[{suggestion_index}]",
                    "step_event_suggestion",
                    f"{suggestion_id}:{step_type}:{step_id}",
                ),
                InitialRegularEventLessonEvidence(
                    InitialRegularEventLessonEvidenceKind.PC_MASTER_FOREIGN_KEY,
                    f"lesson[{attribute.value}]",
                    "ProduceStepLesson->ProduceStepLessonLevel",
                    f"{candidate_stage_id}->{lesson_level_id}",
                ),
            )
        )

    naming_audit = _naming_bridge_audit(database, lesson_rows, issues)
    android = _android_evidence(
        None if android_dump_cs is None else Path(android_dump_cs),
        None if android_native_image is None else Path(android_native_image),
    )
    exact_evidence.append(
        InitialRegularEventLessonEvidence(
            InitialRegularEventLessonEvidenceKind.ANDROID_STATIC_CONTRACT,
            "current_progress.step_id",
            "ProduceTransitionUtility.<GoStepAsync>d__8.MoveNext",
            "GoStepAsync.stepId is passed directly to CreateProduceExamTransitionParam",
        )
    )
    return InitialRegularEventLessonCatalog(
        detail_id=INITIAL_REGULAR_EVENT_LESSON_DETAIL_ID,
        entries=tuple(static_facts),
        evidence=InitialRegularEventLessonEvidenceReport(
            exact=tuple(exact_evidence),
            naming_bridge=naming_audit,
            android=android,
        ),
        issues=tuple(issues),
        master_dir=master_dir,
        database=database,
    )


def _idol_plan_issues(
    database: Path,
    idol_card_id: str,
) -> tuple[InitialRegularEventLessonIssue, ...]:
    issues: list[InitialRegularEventLessonIssue] = []
    rows = _query_rows(
        database,
        "SELECT id, character_id, plan_type FROM idol_card WHERE id = ?",
        (idol_card_id,),
        "idol_card",
        issues,
    )
    if len(rows) != 1:
        _append_issue(
            issues,
            "idol-card-master-missing" if not rows else "idol-card-master-ambiguous",
            "idol_card_id",
            idol_card_id,
        )
        return tuple(issues)
    row = rows[0]
    if row["character_id"] != INITIAL_REGULAR_EVENT_LESSON_CHARACTER_ID:
        _append_issue(
            issues,
            "idol-card-character-mismatch",
            "idol_card.character_id",
            f"expected={INITIAL_REGULAR_EVENT_LESSON_CHARACTER_ID}:actual={row['character_id']}",
        )
    if row["plan_type"] != INITIAL_REGULAR_EVENT_LESSON_MASTER_PLAN_TYPE:
        _append_issue(
            issues,
            "idol-card-plan-mismatch",
            "idol_card.plan_type",
            f"expected={INITIAL_REGULAR_EVENT_LESSON_MASTER_PLAN_TYPE}:actual={row['plan_type']}",
        )
    return tuple(issues)


def build_initial_regular_event_lesson_inner_stage_request(
    outer_state: ProduceRolloutState,
    pending_request: ExternalRequest,
    *,
    branch: ChanceBranch,
    rng_before: int | None,
    idol_card_id: str | None,
    runtime_refs: InitialRegularInnerRuntimeRefs,
    runtime_facts: InitialRegularEventLessonRuntimeFacts,
    catalog: InitialRegularEventLessonCatalog | None = None,
    master_dir: Path = DEFAULT_MASTER_DIR,
    database: Path = DEFAULT_DATABASE,
) -> InitialRegularEventLessonBuildResult:
    """Bind one exact event continuation to common and Plan2 stage facts.

    All caller-owned values are explicit.  In particular, this function does
    not consult a route week or choose a normal-lesson index.  If the response
    current-progress stage ID is absent, FKTN's character-scoped naming audit
    must be a complete bijection before its candidate may be used.
    """

    if catalog is None:
        catalog = load_initial_regular_event_lesson_catalog(
            master_dir=master_dir, database=database
        )
    issues: list[InitialRegularEventLessonIssue] = list(catalog.issues)
    if not isinstance(outer_state, ProduceRolloutState):
        _append_issue(issues, "outer-state-invalid", "outer_state")
    if not isinstance(pending_request, ExternalRequest):
        _append_issue(issues, "pending-request-invalid", "pending_request")
    if not isinstance(branch, ChanceBranch):
        _append_issue(issues, "branch-invalid", "branch")
    if not isinstance(runtime_refs, InitialRegularInnerRuntimeRefs):
        _append_issue(issues, "runtime-refs-invalid", "runtime_refs")
    if not isinstance(runtime_facts, InitialRegularEventLessonRuntimeFacts):
        _append_issue(issues, "runtime-facts-invalid", "runtime_facts")
    if issues and any(
        issue.code.endswith("-invalid")
        and issue.field
        in {
            "outer_state",
            "pending_request",
            "branch",
            "runtime_refs",
            "runtime_facts",
        }
        for issue in issues
    ):
        return InitialRegularEventLessonBuildResult(
            None, None, None, tuple(issues), catalog.evidence
        )
    assert isinstance(outer_state, ProduceRolloutState)
    assert isinstance(pending_request, ExternalRequest)
    assert isinstance(branch, ChanceBranch)
    assert isinstance(runtime_refs, InitialRegularInnerRuntimeRefs)
    assert isinstance(runtime_facts, InitialRegularEventLessonRuntimeFacts)

    if outer_state.phase is not RolloutPhase.WAITING_EXTERNAL or outer_state.pending is None:
        _append_issue(issues, "outer-request-not-pending", "outer_state.pending")
    elif outer_state.pending != pending_request:
        _append_issue(
            issues,
            "pending-request-identity-mismatch",
            "outer_state.pending",
            f"state={outer_state.pending.request_id}:caller={pending_request.request_id}",
        )
    if pending_request.kind is not ExternalKind.WEEKLY_ACTION_OUTCOME:
        _append_issue(
            issues,
            "pending-request-kind-mismatch",
            "pending_request.kind",
            pending_request.kind.value,
        )
    if pending_request.week != outer_state.week:
        _append_issue(
            issues,
            "pending-request-week-mismatch",
            "pending_request.week",
            f"outer={outer_state.week}:pending={pending_request.week}",
        )
    if outer_state.mode_id != INITIAL_REGULAR_EVENT_LESSON_PRODUCE_ID:
        _append_issue(
            issues,
            "outer-mode-mismatch",
            "outer_state.mode_id",
            outer_state.mode_id,
        )
    if outer_state.character_id != INITIAL_REGULAR_EVENT_LESSON_CHARACTER_ID:
        _append_issue(
            issues,
            "outer-character-mismatch",
            "outer_state.character_id",
            outer_state.character_id,
        )
    if pending_request.adapter_ref != INITIAL_REGULAR_EVENT_LESSON_ADAPTER_REF:
        _append_issue(
            issues,
            "pending-adapter-mismatch",
            "pending_request.adapter_ref",
            str(pending_request.adapter_ref),
        )
    if INITIAL_REGULAR_EVENT_LESSON_REQUIRED_FIELD not in pending_request.required_fields:
        _append_issue(
            issues,
            "pending-required-field-missing",
            "pending_request.required_fields",
            INITIAL_REGULAR_EVENT_LESSON_REQUIRED_FIELD,
        )

    entry = catalog.for_next_step(pending_request.action_id)
    if entry is None:
        _append_issue(
            issues,
            "event-next-step-unsupported",
            "pending_request.action_id",
            pending_request.action_id,
        )
    else:
        if pending_request.stage_type != entry.continuation_step_type:
            _append_issue(
                issues,
                "event-next-step-type-mismatch",
                "pending_request.stage_type",
                f"expected={entry.continuation_step_type}:actual={pending_request.stage_type}",
            )
        expected = _EXPECTED_BY_ID.get(pending_request.action_id)
        if expected is None or expected[4] is not entry.attribute:
            _append_issue(
                issues,
                "event-next-step-attribute-mismatch",
                "pending_request.action_id",
                pending_request.action_id,
            )

    expected_refs = InitialRegularInnerRuntimeRefs(
        item_session_refs=outer_state.item_session_refs,
        drink_session_refs=outer_state.drink_session_refs,
        passive_session_refs=outer_state.passive_session_refs,
    )
    if runtime_refs != expected_refs:
        _append_issue(
            issues,
            "runtime-refs-mismatch",
            "runtime_refs",
            f"expected={expected_refs}:actual={runtime_refs}",
        )
    if idol_card_id is None or not isinstance(idol_card_id, str) or not idol_card_id:
        _append_issue(issues, "idol-card-id-unresolved", "idol_card_id")
    else:
        for issue in _idol_plan_issues(catalog.database, idol_card_id):
            if issue not in issues:
                issues.append(issue)
    if type(rng_before) is not int or not 0 <= rng_before <= _UINT32_MAX:
        _append_issue(issues, "rng-before-unresolved", "rng_before")

    for field_name in (
        "parameter_limits",
        "lesson_limit_up_score",
        "exam_extra_turn",
        "battle_bonus_permille",
        "gimmick_group_id",
    ):
        if getattr(runtime_facts, field_name) is None:
            _append_issue(
                issues,
                "runtime-field-unresolved",
                f"runtime_facts.{field_name}",
            )

    stage_id: str | None = runtime_facts.current_progress_step_id
    stage_id_evidence = (
        InitialRegularEventLessonEvidenceKind.ANDROID_CURRENT_PROGRESS_STEP_ID
    )
    if entry is not None:
        if stage_id is None:
            naming_scope = catalog.evidence.naming_bridge.for_character(
                INITIAL_REGULAR_EVENT_LESSON_CHARACTER_ID
            )
            if naming_scope is not None and naming_scope.complete:
                stage_id = entry.candidate_stage_id
                stage_id_evidence = InitialRegularEventLessonEvidenceKind.NAMING_BRIDGE
            else:
                exception_codes = (
                    "scope-missing"
                    if naming_scope is None
                    else ",".join(
                        sorted({value.code for value in naming_scope.exceptions})
                    )
                )
                _append_issue(
                    issues,
                    "character-naming-bridge-incomplete",
                    "runtime_facts.current_progress_step_id",
                    exception_codes,
                )
        elif stage_id != entry.candidate_stage_id:
            _append_issue(
                issues,
                "current-progress-stage-mismatch",
                "runtime_facts.current_progress_step_id",
                f"expected={entry.candidate_stage_id}:actual={stage_id}",
            )

    if issues:
        return InitialRegularEventLessonBuildResult(
            None, None, None, tuple(issues), catalog.evidence
        )
    assert entry is not None
    assert stage_id is not None
    assert idol_card_id is not None
    assert rng_before is not None
    assert runtime_facts.parameter_limits is not None
    assert runtime_facts.lesson_limit_up_score is not None
    assert runtime_facts.exam_extra_turn is not None
    assert runtime_facts.battle_bonus_permille is not None
    assert runtime_facts.gimmick_group_id is not None

    attribute_index = _ATTRIBUTE_INDEX[entry.attribute]
    current_attribute = (
        outer_state.attributes.vocal,
        outer_state.attributes.dance,
        outer_state.attributes.visual,
    )[attribute_index]
    attribute_limit = runtime_facts.parameter_limits[attribute_index]
    if current_attribute > attribute_limit:
        _append_issue(
            issues,
            "attribute-limit-below-current",
            f"runtime_facts.parameter_limits[{attribute_index}]",
            f"current={current_attribute}:limit={attribute_limit}",
        )
        return InitialRegularEventLessonBuildResult(
            None, None, None, tuple(issues), catalog.evidence
        )
    remaining = attribute_limit - current_attribute
    clear_border = min(entry.base_clear_target, remaining)
    limit_border = min(
        entry.base_limit_target + runtime_facts.lesson_limit_up_score,
        remaining,
    )
    resolved_runtime = InitialRegularEventLessonResolvedRuntime(
        exam_extra_turn=runtime_facts.exam_extra_turn,
        battle_bonus_permille=runtime_facts.battle_bonus_permille,
        gimmick_group_id=runtime_facts.gimmick_group_id,
    )
    stage_fact = InitialRegularEventLessonStageFact(
        stage_id=stage_id,
        lesson_level_id=entry.lesson_level_id,
        plan_type=entry.plan_type,
        setting_id=entry.setting_id,
        attribute=entry.attribute,
        current_parameter_type=entry.current_parameter_type,
        lesson_type=entry.lesson_type,
        step_type_value=entry.step_type_value,
        base_turns=entry.base_turns,
        base_clear_target=entry.base_clear_target,
        base_limit_target=entry.base_limit_target,
        start_stamina=outer_state.stamina,
        max_stamina=outer_state.max_stamina,
        current_attribute=current_attribute,
        attribute_limit=attribute_limit,
        clear_border=clear_border,
        runtime_turns=entry.base_turns + runtime_facts.exam_extra_turn,
        limit_border=limit_border,
        runtime_context=resolved_runtime,
        continuation_step_id=entry.continuation_step_id,
        continuation_step_type=entry.continuation_step_type,
        stage_id_evidence_kind=stage_id_evidence,
    )
    request = build_initial_regular_lesson_stage_request(
        outer_state,
        pending_request,
        stage_fact,
        branch=branch,
        idol_card_id=idol_card_id,
        rng_before=rng_before,
    )
    request_checks = (
        ("external_request_id", pending_request.request_id),
        ("week", pending_request.week),
        ("action_id", entry.continuation_step_id),
        ("attribute", entry.attribute.value),
        ("exam_type", PLAN2_LESSON_EXAM_TYPE),
        ("stage_id", stage_id),
        ("setting_id", entry.setting_id),
        ("step_type_value", entry.step_type_value),
        ("limit_turn", entry.base_turns),
        ("extra_turn", runtime_facts.exam_extra_turn),
        ("clear_border", clear_border),
        ("limit_border", limit_border),
    )
    for field_name, expected in request_checks:
        actual = getattr(request, field_name)
        if actual != expected:
            _append_issue(
                issues,
                "inner-request-field-mismatch",
                f"request.{field_name}",
                f"expected={expected}:actual={actual}",
            )
    for issue in validate_initial_regular_inner_stage_request(request):
        _append_issue(
            issues,
            "common-inner-request-invalid",
            issue.field,
            f"{issue.code}:{issue.detail}",
        )

    try:
        from .plan2_native_stage_bootstrap import (
            plan2_stage_facts_from_initial_regular_lesson,
        )

        plan2_facts: Plan2NativeStageFacts | None = (
            plan2_stage_facts_from_initial_regular_lesson(stage_fact)
        )
    except (TypeError, ValueError) as error:
        plan2_facts = None
        _append_issue(
            issues,
            "plan2-stage-facts-invalid",
            "stage_fact",
            f"{type(error).__name__}:{error}",
        )
    return InitialRegularEventLessonBuildResult(
        request=request,
        stage_fact=stage_fact,
        plan2_stage_facts=plan2_facts,
        issues=tuple(issues),
        evidence=catalog.evidence,
    )


# Concise aliases for the two primary call sites.
load_event_022_plan2_lesson_catalog = load_initial_regular_event_lesson_catalog
build_event_022_plan2_inner_stage_request = (
    build_initial_regular_event_lesson_inner_stage_request
)
