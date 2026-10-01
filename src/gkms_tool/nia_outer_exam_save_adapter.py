"""Compose outer Produce local saves with a N.I.A. ``ExamSaveData``.

The adapter removes the identity fields that ``nia_exam_save_bridge`` used to
require from callers.  Android v3.2.3 supplies the pieces in three places:

* ``ProducePlayLogSaveData`` records the audition step and the selected
  difficulty ``Number`` (detail-line type 35);
* ``ExamSaveData`` records the step, battle-config parameters, generated turn
  schedule and every card zone; and
* Master maps the idol's mandatory signature card to its exact audition
  difficulty row and maps the battle config to ``produce-004`` or
  ``produce-005``.

``ProduceLocalSaveData`` may be attached to the outer snapshot for lifecycle
context, but it contains none of the composite difficulty-key fields.  The
only optional runtime value accepted here is the RNG state immediately before
``CalcTurnParameterType``; normal local saves do not serialize that state.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .audition_rules import FINAL, MID1, MID2
from .nia_exam_save_bridge import (
    NiaExamSaveAuditionProjection,
    NiaExamSaveDifficultyKey,
    project_nia_exam_save_audition,
)
from .nia_static_adapter import DEFAULT_MASTER_DIR, NIA_PRODUCE_IDS
from .produce_outer_local_save import (
    ProduceLifecycleState,
    ProduceLogLine,
    ProduceOuterLocalSaveSnapshot,
    parse_produce_lifecycle,
    parse_produce_play_log,
)


_STEP_TYPE_BY_NUMBER = {16: MID1, 17: MID2, 18: FINAL}
_STEP_NUMBER_BY_TYPE = {value: key for key, value in _STEP_TYPE_BY_NUMBER.items()}
_PARAMETER_TYPE_BY_NUMBER = {
    1: "ProduceParameterType_Vocal",
    2: "ProduceParameterType_Dance",
    3: "ProduceParameterType_Visual",
}
_PARAMETER_TYPE_NAMES = frozenset(_PARAMETER_TYPE_BY_NUMBER.values())


class NiaOuterExamSaveAdapterError(ValueError):
    """Raised when supplied local-save evidence cannot identify one audition."""


@dataclass(frozen=True, slots=True)
class NiaOuterExamSaveComposition:
    """One automatically identified N.I.A. audition and its bridge result."""

    outer_snapshot: ProduceOuterLocalSaveSnapshot
    audition_select_event: ProduceLogLine
    idol_card_id: str
    difficulty_key: NiaExamSaveDifficultyKey
    serialized_turn_parameter_types: tuple[str, ...]
    projection: NiaExamSaveAuditionProjection

    @property
    def static_identity_complete(self) -> bool:
        return self.projection.profile is not None and not any(
            issue.code != "pre-schedule-random-state-missing"
            for issue in self.projection.issues
        )

    @property
    def pre_schedule_rng_is_only_gap(self) -> bool:
        return self.static_identity_complete and {
            issue.code for issue in self.projection.issues
        } == {"pre-schedule-random-state-missing"}


@lru_cache(maxsize=24)
def _master_rows(path: Path) -> tuple[Mapping[str, Any], ...]:
    if not path.is_file():
        raise FileNotFoundError(f"Master file not found: {path}")
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    payload = yaml.load(path.read_text(encoding="utf-8"), Loader=loader)
    if not isinstance(payload, list):
        raise NiaOuterExamSaveAdapterError(f"{path.name} must contain a list")
    return tuple(row for row in payload if isinstance(row, Mapping))


def _field(mapping: Mapping[str, object], *names: str) -> object:
    for name in names:
        if name in mapping:
            return mapping[name]
    raise NiaOuterExamSaveAdapterError(
        "missing ExamSaveData field: " + "/".join(names)
    )


def _integer(value: object, label: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise NiaOuterExamSaveAdapterError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise NiaOuterExamSaveAdapterError(f"{label} must be >= {minimum}")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise NiaOuterExamSaveAdapterError(f"{label} must be non-empty text")
    return value


def _step_type(value: object) -> tuple[int, str]:
    if isinstance(value, int) and not isinstance(value, bool):
        name = _STEP_TYPE_BY_NUMBER.get(value)
        if name is not None:
            return value, name
    elif isinstance(value, str) and value in _STEP_NUMBER_BY_TYPE:
        return _STEP_NUMBER_BY_TYPE[value], value
    raise NiaOuterExamSaveAdapterError(
        "ExamSaveData.stepType is not a N.I.A. audition step"
    )


def _outer_snapshot(
    value: ProduceOuterLocalSaveSnapshot | Mapping[str, object],
    lifecycle: ProduceLifecycleState | Mapping[str, object] | None,
) -> ProduceOuterLocalSaveSnapshot:
    if isinstance(value, ProduceOuterLocalSaveSnapshot):
        if lifecycle is not None:
            raise NiaOuterExamSaveAdapterError(
                "lifecycle must already be attached when outer is a snapshot"
            )
        return value
    parsed_lifecycle: ProduceLifecycleState | None
    if lifecycle is None or isinstance(lifecycle, ProduceLifecycleState):
        parsed_lifecycle = lifecycle
    else:
        parsed_lifecycle = parse_produce_lifecycle(lifecycle)
    return parse_produce_play_log(value, lifecycle=parsed_lifecycle)


def _audition_selection(
    outer: ProduceOuterLocalSaveSnapshot, step_number: int
) -> ProduceLogLine:
    step_log_indexes = {
        step.log_index
        for step in outer.completed_steps
        if step.step_type == step_number
    }
    matches = tuple(
        event
        for event in outer.audition_select_events
        if event.log_index in step_log_indexes
    )
    if not matches:
        raise NiaOuterExamSaveAdapterError(
            "ProducePlayLog has no AuditionSelect event for the current step"
        )
    event = max(
        matches,
        key=lambda item: (item.log_index, item.detail_index, item.line_index),
    )
    _integer(
        event.target_subscription_number,
        "AuditionSelect.targetSubscriptionNumber",
        minimum=1,
    )
    return event


def _collect_produce_card_ids(value: object) -> frozenset[str]:
    found: set[str] = set()

    def visit(item: object) -> None:
        if isinstance(item, Mapping):
            for child in item.values():
                visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)
        elif isinstance(item, str) and item.startswith("p_card-"):
            found.add(item)

    visit(value)
    return frozenset(found)


def _runtime_gimmick_group_ids(exam_save: Mapping[str, object]) -> frozenset[str]:
    raw = exam_save.get("gimmickList", exam_save.get("gimmick_list", []))
    if not isinstance(raw, list):
        raise NiaOuterExamSaveAdapterError("ExamSaveData.gimmickList must be a list")
    values: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise NiaOuterExamSaveAdapterError(
                f"ExamSaveData.gimmickList[{index}] must be an object"
            )
        group_id = item.get("gimmickGroupId", item.get("gimmick_group_id", ""))
        if not isinstance(group_id, str):
            raise NiaOuterExamSaveAdapterError(
                f"ExamSaveData.gimmickList[{index}].gimmickGroupId must be text"
            )
        if group_id:
            values.add(group_id)
    return frozenset(values)


def _serialized_turn_types(
    exam_save: Mapping[str, object], limit_turn: int
) -> tuple[str, ...]:
    raw = _field(
        exam_save,
        "turnStatusParameterTypeList",
        "turn_status_parameter_type_list",
    )
    if not isinstance(raw, list):
        raise NiaOuterExamSaveAdapterError(
            "ExamSaveData.turnStatusParameterTypeList must be a list"
        )
    result: list[str] = []
    for index, value in enumerate(raw):
        if isinstance(value, int) and not isinstance(value, bool):
            name = _PARAMETER_TYPE_BY_NUMBER.get(value)
        elif isinstance(value, str) and value in _PARAMETER_TYPE_NAMES:
            name = value
        else:
            name = None
        if name is None:
            raise NiaOuterExamSaveAdapterError(
                f"turnStatusParameterTypeList[{index}] is not Vocal/Dance/Visual"
            )
        result.append(name)
    if len(result) != limit_turn:
        raise NiaOuterExamSaveAdapterError(
            "turnStatusParameterTypeList length does not equal limitTurn"
        )
    return tuple(result)


def _identity_candidates(
    exam_save: Mapping[str, object],
    *,
    step_type: str,
    audition_number: int,
    limit_turn: int,
    vocal: int,
    dance: int,
    visual: int,
    prefer_exam_save_runtime: bool,
    master_dir: Path,
) -> tuple[tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]], ...]:
    """Return (IdolCard, difficulty row, battle config) exact matches."""

    character_id = _text(
        _field(exam_save, "characterId", "character_id"),
        "ExamSaveData.characterId",
    )
    observed_cards = _collect_produce_card_ids(exam_save)
    if not observed_cards:
        raise NiaOuterExamSaveAdapterError(
            "ExamSaveData contains no Produce card IDs for signature-card lookup"
        )

    idol_rows = tuple(
        row
        for row in _master_rows(master_dir / "IdolCard.yaml")
        if row.get("characterId") == character_id
        and (
            row.get("produceCardId") in observed_cards
            or (
                isinstance(row.get("secondProduceCardId"), str)
                and bool(row.get("secondProduceCardId"))
                and row.get("secondProduceCardId") in observed_cards
            )
        )
    )
    if not idol_rows:
        raise NiaOuterExamSaveAdapterError(
            "mandatory signature card did not resolve an IdolCard Master row"
        )

    difficulty_rows = _master_rows(master_dir / "ProduceStepAuditionDifficulty.yaml")
    configs = {
        row.get("id"): row
        for row in _master_rows(master_dir / "ProduceExamBattleConfig.yaml")
        if isinstance(row.get("id"), str)
    }
    runtime_groups = _runtime_gimmick_group_ids(exam_save)
    matches: list[
        tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]
    ] = []
    for idol in idol_rows:
        difficulty_id = idol.get("produceStepAuditionDifficultyId")
        if not isinstance(difficulty_id, str) or not difficulty_id:
            continue
        for difficulty in difficulty_rows:
            if (
                difficulty.get("id") != difficulty_id
                or difficulty.get("produceId") not in NIA_PRODUCE_IDS
                or difficulty.get("stepType") != step_type
                or difficulty.get("number") != audition_number
            ):
                continue
            config = configs.get(difficulty.get("produceExamBattleConfigId"))
            if config is None:
                continue
            if not prefer_exam_save_runtime and (
                config.get("turn") != limit_turn
                or config.get("vocal") != vocal
                or config.get("dance") != dance
                or config.get("visual") != visual
            ):
                continue
            group_id = difficulty.get("produceExamGimmickEffectGroupId")
            if runtime_groups and group_id not in runtime_groups:
                continue
            matches.append((idol, difficulty, config))

    # Several IdolCards may intentionally share the same character-level
    # difficulty row.  That is still one exact composite difficulty identity.
    identities = {
        (
            str(difficulty.get("id")),
            str(difficulty.get("produceId")),
            str(difficulty.get("stepType")),
            int(difficulty.get("number", 0)),
        )
        for _, difficulty, _ in matches
    }
    if len(identities) != 1:
        raise NiaOuterExamSaveAdapterError(
            "local-save and Master evidence did not resolve exactly one "
            f"N.I.A. difficulty identity (found {len(identities)})"
        )
    identity = next(iter(identities))
    return tuple(
        item
        for item in matches
        if (
            item[1].get("id"),
            item[1].get("produceId"),
            item[1].get("stepType"),
            item[1].get("number"),
        )
        == identity
    )


def compose_nia_outer_exam_save(
    outer: ProduceOuterLocalSaveSnapshot | Mapping[str, object],
    exam_save: Mapping[str, object],
    *,
    lifecycle: ProduceLifecycleState | Mapping[str, object] | None = None,
    pre_schedule_random_state: int | None = None,
    prefer_exam_save_runtime: bool = False,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> NiaOuterExamSaveComposition:
    """Infer the exact bridge context from decrypted local-save mappings.

    No produce ID, audition number, idol-card ID or difficulty key is accepted
    from the caller.  Supplying ``pre_schedule_random_state`` is optional; if
    omitted, the returned bridge has exactly the documented RNG replay gap
    while the serialized turn schedule remains directly available.
    """

    if not isinstance(exam_save, Mapping):
        raise TypeError("exam_save must be a mapping")
    if type(prefer_exam_save_runtime) is not bool:
        raise TypeError("prefer_exam_save_runtime must be bool")
    snapshot = _outer_snapshot(outer, lifecycle)
    step_number, step_type = _step_type(
        _field(exam_save, "stepType", "step_type")
    )
    event = _audition_selection(snapshot, step_number)
    audition_number = event.target_subscription_number
    limit_turn = _integer(
        _field(exam_save, "limitTurn", "limit_turn"),
        "ExamSaveData.limitTurn",
        minimum=1,
    )
    vocal = _integer(
        _field(exam_save, "vocalConfigParameter", "vocal_config_parameter"),
        "ExamSaveData.vocalConfigParameter",
        minimum=0,
    )
    dance = _integer(
        _field(exam_save, "danceConfigParameter", "dance_config_parameter"),
        "ExamSaveData.danceConfigParameter",
        minimum=0,
    )
    visual = _integer(
        _field(exam_save, "visualConfigParameter", "visual_config_parameter"),
        "ExamSaveData.visualConfigParameter",
        minimum=0,
    )
    directory = Path(master_dir).resolve()
    matches = _identity_candidates(
        exam_save,
        step_type=step_type,
        audition_number=audition_number,
        limit_turn=limit_turn,
        vocal=vocal,
        dance=dance,
        visual=visual,
        prefer_exam_save_runtime=prefer_exam_save_runtime,
        master_dir=directory,
    )
    idol, difficulty, _ = matches[0]
    idol_card_id = _text(idol.get("id"), "IdolCard.id")
    key = NiaExamSaveDifficultyKey(
        row_id=_text(difficulty.get("id"), "difficulty.id"),
        produce_id=_text(difficulty.get("produceId"), "difficulty.produceId"),
        step_type=step_type,
        number=audition_number,
    )
    bridge_payload: dict[str, object] = {
        "examSaveData": exam_save,
        "idolCardId": idol_card_id,
        "difficultyKey": {
            "id": key.row_id,
            "produceId": key.produce_id,
            "stepType": key.step_type,
            "number": key.number,
        },
    }
    if pre_schedule_random_state is not None:
        bridge_payload["randomStateBeforeTurnSchedule"] = (
            pre_schedule_random_state
        )
    projection = project_nia_exam_save_audition(
        bridge_payload,
        master_dir=directory,
    )
    return NiaOuterExamSaveComposition(
        outer_snapshot=snapshot,
        audition_select_event=event,
        idol_card_id=idol_card_id,
        difficulty_key=key,
        serialized_turn_parameter_types=_serialized_turn_types(
            exam_save, limit_turn
        ),
        projection=projection,
    )


__all__ = [
    "NiaOuterExamSaveAdapterError",
    "NiaOuterExamSaveComposition",
    "compose_nia_outer_exam_save",
]
