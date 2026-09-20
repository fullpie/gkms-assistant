"""Direct encrypted ExamSaveData to GUID/RNG-aware Plan 3 search."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Mapping, Sequence

import yaml

from .audition_local_save_state import (
    LocalSaveExamCard,
    LocalSaveExamState,
    parse_local_save_exam_state,
)
from .audition_native_support import (
    NativeHandAddCard,
    NativeHandAddSupportResult,
    evaluate_native_hand_add_support,
)
from .audition_support_runtime import LESSON_TYPE_UNKNOWN, SupportUpgradeRuntimeInput
from .card_search import ProduceCardSearchRule, load_produce_card_search
from .exam_hand_add_support_runtime import ExamHandAddSupportCatalog
from .local_save_decoder import LocalSaveDecodeError, exam_support_card_permils
from .master_db import DEFAULT_DATABASE
from .plan3_engine import (
    DEFAULT_MASTER_DIR,
    Plan3GimmickProfile,
    load_plan3_exam_settings,
    load_plan3_gimmick_profile,
)
from .plan3_local_save_bridge import (
    DecodedPlan3LocalSave,
    Plan3LocalSaveProjection,
    decode_plan3_local_save_bytes,
    decode_plan3_local_save_file,
    project_plan3_local_save,
)
from .plan3_native_search import (
    Plan3NativeAcceptedPlayExtension,
    Plan3NativeSearchResult,
    Plan3NativeTurnStartExtension,
    search_plan3_native,
)
from .plan3_native_state import Plan3NativeState
from .plan3_search import Objective


DEFAULT_SUPPORT_CARD_MASTER = DEFAULT_MASTER_DIR / "SupportCard.yaml"
_LESSON_BY_PARAMETER_TYPE = {
    "ProduceParameterType_Unknown": LESSON_TYPE_UNKNOWN,
    "ProduceParameterType_Vocal": "ProduceStepLessonType_LessonVocal",
    "ProduceParameterType_Dance": "ProduceStepLessonType_LessonDance",
    "ProduceParameterType_Visual": "ProduceStepLessonType_LessonVisual",
}
_LESSON_BY_PARAMETER_VALUE = {
    1: "ProduceStepLessonType_LessonVocal",
    2: "ProduceStepLessonType_LessonDance",
    3: "ProduceStepLessonType_LessonVisual",
}


@dataclass(frozen=True, slots=True)
class Plan3NativeSearchBridgeIssue:
    code: str
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not self.code:
            raise ValueError("issue code must be non-empty text")


@dataclass(frozen=True, slots=True)
class Plan3NativeSearchBridgeResult:
    decoded: DecodedPlan3LocalSave
    projection: Plan3LocalSaveProjection
    native_state: Plan3NativeState | None
    support_upgrades: tuple[SupportUpgradeRuntimeInput, ...]
    support_card_searches: tuple[tuple[str, ProduceCardSearchRule], ...]
    gimmick_profile: Plan3GimmickProfile | None
    search: Plan3NativeSearchResult | None
    turn_start_extension: Plan3NativeTurnStartExtension | None = None
    accepted_play_extension: Plan3NativeAcceptedPlayExtension | None = None
    initial_turn_start_extension_state: object | None = None
    issues: tuple[Plan3NativeSearchBridgeIssue, ...] = ()

    @property
    def ready(self) -> bool:
        return self.search is not None and not self.issues

    @property
    def best(self):
        return None if self.search is None else self.search.best

    @property
    def semantic_gaps(self) -> tuple[str, ...]:
        values = [
            issue.code if not issue.detail else f"{issue.code}:{issue.detail}"
            for issue in self.issues
        ]
        if self.search is not None:
            values.extend(
                gap
                for diagnostic in self.search.diagnostics
                for gap in diagnostic.semantic_gaps
            )
        return tuple(dict.fromkeys(values))


@dataclass(frozen=True, slots=True)
class Plan3HandAddSupportReplay:
    """One plan-neutral HandAdd result projected through Plan 3 authorities.

    ``result`` is calculated only from the captured before-state, explicit
    drawn GUID event, runtime support rows and static support predicates.  A
    native after-state is deliberately not accepted by this boundary; callers
    own the differential comparison.
    """

    parsed: LocalSaveExamState
    drawn_guids: tuple[str, ...]
    lesson_type: str
    support_upgrades: tuple[SupportUpgradeRuntimeInput, ...]
    support_card_searches: tuple[tuple[str, ProduceCardSearchRule], ...]
    result: NativeHandAddSupportResult

    def __post_init__(self) -> None:
        if not isinstance(self.parsed, LocalSaveExamState):
            raise TypeError("parsed must be LocalSaveExamState")
        object.__setattr__(self, "drawn_guids", tuple(self.drawn_guids))
        object.__setattr__(self, "support_upgrades", tuple(self.support_upgrades))
        object.__setattr__(
            self,
            "support_card_searches",
            tuple(self.support_card_searches),
        )
        if not isinstance(self.result, NativeHandAddSupportResult):
            raise TypeError("result must be NativeHandAddSupportResult")


def _raw_exam_save(decoded: DecodedPlan3LocalSave) -> Mapping[str, object]:
    try:
        value = json.loads(decoded.envelope.plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LocalSaveDecodeError(
            "decrypted ExamSaveData body is not valid UTF-8 JSON"
        ) from error
    if not isinstance(value, Mapping):
        raise LocalSaveDecodeError("decrypted ExamSaveData root must be an object")
    return value


@lru_cache(maxsize=4)
def _support_master_index(
    path: Path,
) -> dict[str, Mapping[str, object]]:
    target = Path(path)
    payload = yaml.load(
        target.read_text(encoding="utf-8"),
        Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader),
    )
    if not isinstance(payload, list):
        raise ValueError("SupportCard.yaml must contain a list")
    result: dict[str, Mapping[str, object]] = {}
    for row in payload:
        if not isinstance(row, Mapping):
            continue
        support_id = row.get("id")
        if not isinstance(support_id, str) or not support_id:
            continue
        if support_id in result:
            raise ValueError(f"duplicate SupportCard row: {support_id}")
        result[support_id] = row
    return result


def _support_inputs(
    raw_exam_save: Mapping[str, object],
    *,
    support_card_master: Path,
    database: Path,
) -> tuple[
    tuple[SupportUpgradeRuntimeInput, ...],
    tuple[tuple[str, ProduceCardSearchRule], ...],
]:
    runtime_rows = exam_support_card_permils(raw_exam_save)
    master = _support_master_index(support_card_master)
    supports: list[SupportUpgradeRuntimeInput] = []
    searches: dict[str, ProduceCardSearchRule] = {}
    for expected_order, runtime in enumerate(runtime_rows):
        if runtime.get("index") != expected_order:
            raise ValueError("supportCardList order/index is not contiguous")
        support_id = runtime["supportCardId"]
        runtime_permil = runtime["produceCardUpgradePermil"]
        assert isinstance(support_id, str)
        assert isinstance(runtime_permil, int)
        row = master.get(support_id)
        if row is None:
            raise KeyError(f"SupportCard master row not found: {support_id}")
        parameter_type = row.get("produceCardUpgradeLessonParameterType")
        if not isinstance(parameter_type, str):
            raise ValueError(
                f"SupportCard lesson parameter is missing: {support_id}"
            )
        lesson_type = _LESSON_BY_PARAMETER_TYPE.get(parameter_type)
        if lesson_type is None:
            raise ValueError(
                f"unsupported SupportCard lesson parameter: {support_id}:{parameter_type}"
            )
        card_search_id = row.get("upgradeProduceCardSearchId")
        if not isinstance(card_search_id, str) or not card_search_id:
            raise ValueError(f"SupportCard card search is missing: {support_id}")
        supports.append(
            SupportUpgradeRuntimeInput(
                support_id=support_id,
                lesson_type=lesson_type,
                runtime_permil=runtime_permil,
                loadout_order=expected_order,
                card_search_id=card_search_id,
            )
        )
        if card_search_id not in searches:
            searches[card_search_id] = load_produce_card_search(
                card_search_id, database
            )
    # Preserve Plan 3's existing authority split (runtime ID/permil plus
    # SupportCard Master predicate), then normalize the resolved values through
    # the same plan-neutral catalog consumed by the other exam adapters.
    catalog = ExamHandAddSupportCatalog(
        support_upgrades=tuple(supports),
        support_card_searches=tuple(searches.values()),
    )
    return catalog.support_upgrades, tuple(
        (rule.id, rule) for rule in catalog.support_card_searches
    )


def _gimmick_profile(
    raw_exam_save: Mapping[str, object],
    *,
    database: Path,
) -> Plan3GimmickProfile | None:
    raw = raw_exam_save.get("gimmickList", [])
    if not isinstance(raw, list):
        raise ValueError("ExamSaveData.gimmickList must be a list")
    group_ids: list[str] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise ValueError(f"gimmickList[{index}] must be an object")
        group_id = item.get("gimmickGroupId")
        if not isinstance(group_id, str) or not group_id:
            raise ValueError(f"gimmickList[{index}] has no gimmickGroupId")
        if group_id not in group_ids:
            group_ids.append(group_id)
    if not group_ids:
        return None
    if len(group_ids) != 1:
        raise ValueError(
            "multiple gimmick groups are not composable: " + ",".join(group_ids)
        )
    return load_plan3_gimmick_profile(group_ids[0], database=database)


# Public aliases let replay/runtime adapters reuse the exact same support and
# gimmick binding without importing a full search or copying private logic.
build_plan3_runtime_support_inputs = _support_inputs
load_plan3_runtime_gimmick_profile = _gimmick_profile


def replay_plan3_hand_add_support_runtime(
    raw_exam_save: Mapping[str, object],
    *,
    drawn_guids: Sequence[str],
    database: Path = DEFAULT_DATABASE,
    support_card_master: Path = DEFAULT_SUPPORT_CARD_MASTER,
) -> Plan3HandAddSupportReplay:
    """Replay one observed post-draw HandAdd event from a native before-state.

    This is intentionally narrower than a full Plan 3 action reducer.  The
    next battle parameter is read from the captured schedule, card identities
    and upgrades come from native zones, and support permils come from
    ``supportCardList``.  No post-state, simulator prediction, default root
    field or legacy migration value is accepted.
    """

    if not isinstance(raw_exam_save, Mapping):
        raise TypeError("raw_exam_save must be a mapping")
    parsed = parse_local_save_exam_state(raw_exam_save)
    try:
        ordered_guids = tuple(drawn_guids)
    except TypeError as error:
        raise ValueError("drawn_guids must be a sequence") from error
    if not ordered_guids or any(
        not isinstance(value, str) or not value.strip()
        for value in ordered_guids
    ):
        raise ValueError("drawn_guids must contain non-empty GUIDs")
    if len(set(ordered_guids)) != len(ordered_guids):
        raise ValueError("drawn_guids must be unique")

    # ``current_turn`` is one-based.  HandAdd runs after the settled action
    # advances to the following battle turn, so the next schedule entry is at
    # the current one-based index.
    schedule_index = parsed.current_turn
    if schedule_index >= len(parsed.turn_parameter_types):
        raise ValueError("next battle parameter is unavailable")
    parameter = parsed.turn_parameter_types[schedule_index]
    lesson_type = _LESSON_BY_PARAMETER_VALUE.get(parameter)
    if lesson_type is None:
        raise ValueError(f"next battle parameter is unsupported: {parameter}")

    cards_by_guid: dict[str, LocalSaveExamCard] = {}
    for card in parsed.zones.all_cards:
        if card.guid in cards_by_guid:
            raise ValueError(f"duplicate native card GUID: {card.guid}")
        cards_by_guid[card.guid] = card
    drawn_cards: list[NativeHandAddCard] = []
    for guid in ordered_guids:
        card = cards_by_guid.get(guid)
        if card is None:
            raise ValueError(f"drawn GUID is absent from native zones: {guid}")
        drawn_cards.append(
            NativeHandAddCard(
                guid=card.guid,
                card_id=card.card_id,
                base_upgrade=card.base_upgrade,
                effective_upgrade=card.effective_upgrade,
            )
        )

    supports, searches = _support_inputs(
        raw_exam_save,
        support_card_master=Path(support_card_master),
        database=Path(database),
    )
    result = evaluate_native_hand_add_support(
        drawn_cards,
        lesson_type=lesson_type,
        random_state=parsed.random_state,
        support_upgrades=supports,
        support_card_searches=dict(searches),
        used_support_ids=parsed.turn_use_support_ids,
    )
    return Plan3HandAddSupportReplay(
        parsed=parsed,
        drawn_guids=ordered_guids,
        lesson_type=lesson_type,
        support_upgrades=supports,
        support_card_searches=searches,
        result=result,
    )


def search_plan3_native_decoded_local_save(
    decoded: DecodedPlan3LocalSave,
    *,
    raw_exam_save: Mapping[str, object] | None = None,
    beam_width: int = 64,
    depth: int | None = None,
    objective: Objective | None = None,
    database: Path = DEFAULT_DATABASE,
    support_card_master: Path = DEFAULT_SUPPORT_CARD_MASTER,
    turn_start_extension: Plan3NativeTurnStartExtension | None = None,
    accepted_play_extension: Plan3NativeAcceptedPlayExtension | None = None,
    initial_turn_start_extension_state: object | None = None,
) -> Plan3NativeSearchBridgeResult:
    """Build every native-search input directly from one decoded LocalSave."""

    if not isinstance(decoded, DecodedPlan3LocalSave):
        raise TypeError("decoded must be DecodedPlan3LocalSave")
    raw = _raw_exam_save(decoded) if raw_exam_save is None else raw_exam_save
    if not isinstance(raw, Mapping):
        raise TypeError("raw_exam_save must be a mapping")
    projection = project_plan3_local_save(
        decoded.exam_state,
        database=database,
        native_card_play_counts=True,
    )
    issues = [
        Plan3NativeSearchBridgeIssue(
            f"projection:{issue.code}",
            issue.field if not issue.detail else f"{issue.field}:{issue.detail}",
        )
        for issue in projection.issues
    ]
    native: Plan3NativeState | None = None
    supports: tuple[SupportUpgradeRuntimeInput, ...] = ()
    searches: tuple[tuple[str, ProduceCardSearchRule], ...] = ()
    gimmick: Plan3GimmickProfile | None = None
    try:
        native = Plan3NativeState.from_local_save(
            decoded.exam_state,
            database=Path(database),
            projected_state=projection.state,
        )
    except (TypeError, ValueError) as error:
        issues.append(
            Plan3NativeSearchBridgeIssue(
                "native-state-unavailable", f"{type(error).__name__}:{error}"
            )
        )
    try:
        supports, searches = _support_inputs(
            raw,
            support_card_master=Path(support_card_master),
            database=Path(database),
        )
    except (KeyError, TypeError, ValueError, OSError) as error:
        issues.append(
            Plan3NativeSearchBridgeIssue(
                "support-runtime-unavailable", f"{type(error).__name__}:{error}"
            )
        )
    try:
        gimmick = _gimmick_profile(raw, database=Path(database))
    except (KeyError, TypeError, ValueError, OSError) as error:
        issues.append(
            Plan3NativeSearchBridgeIssue(
                "gimmick-profile-unavailable", f"{type(error).__name__}:{error}"
            )
        )
    search: Plan3NativeSearchResult | None = None
    if native is not None and not issues:
        try:
            settings = load_plan3_exam_settings(decoded.exam_state.setting_id)
            search = search_plan3_native(
                projection.state,
                native,
                beam_width=beam_width,
                depth=depth,
                settings=settings,
                gimmick_profile=(
                    None if turn_start_extension is not None else gimmick
                ),
                objective=objective,
                database=Path(database),
                support_upgrades=supports,
                support_card_searches=dict(searches),
                turn_start_extension=turn_start_extension,
                accepted_play_extension=accepted_play_extension,
                initial_turn_start_extension_state=(
                    initial_turn_start_extension_state
                ),
            )
        except (KeyError, TypeError, ValueError, OSError) as error:
            issues.append(
                Plan3NativeSearchBridgeIssue(
                    "native-search-unavailable", f"{type(error).__name__}:{error}"
                )
            )
    return Plan3NativeSearchBridgeResult(
        decoded=decoded,
        projection=projection,
        native_state=native,
        support_upgrades=supports,
        support_card_searches=searches,
        gimmick_profile=gimmick,
        search=search,
        turn_start_extension=turn_start_extension,
        accepted_play_extension=accepted_play_extension,
        initial_turn_start_extension_state=initial_turn_start_extension_state,
        issues=tuple(issues),
    )


def search_plan3_native_local_save_bytes(
    data: bytes,
    **kwargs,
) -> Plan3NativeSearchBridgeResult:
    return search_plan3_native_decoded_local_save(
        decode_plan3_local_save_bytes(data), **kwargs
    )


def search_plan3_native_local_save_file(
    path: str | Path,
    **kwargs,
) -> Plan3NativeSearchBridgeResult:
    return search_plan3_native_decoded_local_save(
        decode_plan3_local_save_file(path), **kwargs
    )


__all__ = [
    "DEFAULT_SUPPORT_CARD_MASTER",
    "Plan3NativeSearchBridgeIssue",
    "Plan3NativeSearchBridgeResult",
    "Plan3HandAddSupportReplay",
    "build_plan3_runtime_support_inputs",
    "load_plan3_runtime_gimmick_profile",
    "replay_plan3_hand_add_support_runtime",
    "search_plan3_native_decoded_local_save",
    "search_plan3_native_local_save_bytes",
    "search_plan3_native_local_save_file",
]
