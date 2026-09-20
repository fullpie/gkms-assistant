"""Plan2/Common adapter for one exact ``ExamCardCreateSearch`` effect.

This is deliberately a narrow boundary around the already-tested native
CardCreateSearch primitive.  It does not copy the search/RNG/AddCard engine,
and it does not widen Plan2 coverage: only the ``花萌ゆ季節`` effect row and
its four Master card versions are accepted here.

The linked search is the random-pool ``all-upgrade_1`` search.  Android's
helper adds the Common cards, the current Exam plan, and the explicit ignore
whitelist to the authoritative ``ProduceCardPool`` tickets.  The shared
Plan3-native implementation owns the exact xorshift/RNG order, lazy-GUID
boundary, and Hand/DeckFirst overflow placement; this module only supplies
the Plan2/Common scope and the fixed Master boundary.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
from typing import TypeAlias

from .master_db import DEFAULT_DATABASE
from .plan3_card_create_search import (
    default_card_create_search_catalog,
    CARD_CREATE_SEARCH_EFFECT_TYPE,
    CardCreateSearchAffectedCardVariant,
    CardCreateSearchBranch,
    CardCreateSearchCatalog,
    CardCreateSearchChanceInput,
    CardCreateSearchCondition,
    CardCreateSearchDestination,
    CardCreateSearchEffectContract,
    CardCreateSearchError,
    CardCreateSearchEvidenceLink,
    CardCreateSearchExecutionInput,
    CardCreateSearchInputError,
    CardCreateSearchMutation,
    CardCreateSearchNativeEvidence,
    CardCreateSearchPool,
    CardCreateSearchPoolCandidate,
    CardCreateSearchResolutionError,
    CardCreateSearchResolvedBranch,
    CardCreateSearchResult,
    CardCreateSearchTrace,
    CardCreateSearchUnresolvedBranch,
    CardCreateSearchUnresolvedInput,
    apply_card_create_search,
    resolve_card_create_search_contract,
)
from .plan3_native_state import Plan3NativeCard, Plan3NativeState


PLAN_COMMON = "ProducePlanType_Common"
PLAN2 = "ProducePlanType_Plan2"
PLAN2_COMMON_PLAN_TYPES = frozenset((PLAN_COMMON, PLAN2))

TARGET_CARD_ID = "p_card-00-sup-3_024"
TARGET_EFFECT_ID = (
    "e_effect-exam_card_create_search-0001-"
    "p_card_search-random-random_pool-p_random_pool-all-upgrade_1-1-"
    "hand-random-1_1"
)
TARGET_SEARCH_ID = "p_card_search-random-random_pool-p_random_pool-all-upgrade_1-1"
TARGET_POOL_ID = "p_random_pool-all-upgrade_1"
TARGET_UPGRADES = (0, 1, 2, 3)

ANDROID_VERSION = "Android v3.2.3"
MASTER_DATABASE_SOURCE = "var/master.sqlite3"
PC_SEARCH_SOURCE = "_research/gakumasu-diff/ProduceCardSearch.yaml"
PC_POOL_SOURCE = "_research/gakumasu-diff/ProduceCardPool.yaml"
PC_CARD_SOURCE = "_research/gakumasu-diff/ProduceCard.yaml"
ANDROID_EXECUTOR_MAPPING_SOURCE = (
    "_research/android/game-v3.2.3/native-analysis/create-executor-mapping.json"
)
ANDROID_TARGET_METADATA_SOURCE = (
    "_research/android/game-v3.2.3/native-analysis/target-metadata.json"
)
ANDROID_EXECUTOR_SOURCE = (
    "_research/android/game-v3.2.3/cpp2il-plugin/DiffableCs/"
    "Assembly-CSharp/Campus/Ingame/Exam/CardCreateSearchEffectExecutor.cs"
)
PC_TARGET_METADATA_SOURCE = (
    "_research/il2cpp/"
    "B6120AF66E75E72757E3D4086E2C48D0E7D31DA9F3A3DFBCDC7B3ED5BFFD61DA/"
    "targeted-metadata-index.json"
)

Plan2CardCreateSearchState: TypeAlias = Plan3NativeState
Plan2CardCreateSearchCard: TypeAlias = Plan3NativeCard
Plan2CardCreateSearchContract: TypeAlias = CardCreateSearchEffectContract
Plan2CardCreateSearchResult: TypeAlias = CardCreateSearchResult


_EXPECTED_EFFECT_SEQUENCE: dict[int, tuple[str, ...]] = {
    0: (
        TARGET_EFFECT_ID,
        "e_effect-exam_playable_value_add-01",
        "e_effect-exam_effect_timer-0001-01-e_effect-exam_card_draw-0001",
    ),
    1: (
        TARGET_EFFECT_ID,
        "e_effect-exam_playable_value_add-01",
        "e_effect-exam_effect_timer-0001-01-e_effect-exam_card_draw-0001",
    ),
    2: (
        TARGET_EFFECT_ID,
        "e_effect-exam_playable_value_add-01",
        "e_effect-exam_effect_timer-0001-01-e_effect-exam_card_draw-0001",
    ),
    3: (
        "e_effect-exam_block-0001",
        TARGET_EFFECT_ID,
        "e_effect-exam_playable_value_add-01",
        "e_effect-exam_effect_timer-0001-01-e_effect-exam_card_draw-0001",
    ),
}
_EXPECTED_CUSTOMIZE_IDS: dict[int, tuple[str, ...]] = {
    0: (),
    1: (
        "p_card_custom-070-g_effect-effect_add-e_effect-exam_block-0004",
        "p_card_custom-070-g_effect-effect_add-e_effect-exam_lesson-0006-01",
    ),
    2: (
        "p_card_custom-070-g_effect-effect_add-e_effect-exam_block-0004",
        "p_card_custom-070-g_effect-effect_add-e_effect-exam_lesson-0006-01",
    ),
    3: (
        "p_card_custom-070-g_effect-effect_add-e_effect-exam_block-0004",
        "p_card_custom-070-g_effect-effect_add-e_effect-exam_lesson-0006-01",
    ),
}


@dataclass(frozen=True, slots=True)
class Plan2CommonCardVersion:
    """Master-backed card version and exact command timing.

    ``name`` is retained for audit display only.  Resolution never uses
    display text; the card id, upgrade, plan, effect ids, and source slots are
    the authority.
    """

    card_id: str
    upgrade: int
    name: str
    plan_type: str
    max_customize_count: int
    customize_ids: tuple[str, ...]
    effect_ids: tuple[str, ...]
    search_effect_slot: int

    def __post_init__(self) -> None:
        if self.card_id != TARGET_CARD_ID:
            raise CardCreateSearchResolutionError("unexpected-target-card", self.card_id)
        if self.upgrade not in TARGET_UPGRADES:
            raise CardCreateSearchResolutionError("unexpected-target-upgrade", str(self.upgrade))
        if self.plan_type != PLAN_COMMON:
            raise CardCreateSearchResolutionError("unsupported-plan", self.plan_type)
        if not isinstance(self.name, str):
            raise CardCreateSearchResolutionError("invalid-card-name")
        if (
            isinstance(self.max_customize_count, bool)
            or not isinstance(self.max_customize_count, int)
            or self.max_customize_count < 0
        ):
            raise CardCreateSearchResolutionError("invalid-customize-count")
        customize_ids = tuple(self.customize_ids)
        effect_ids = tuple(self.effect_ids)
        if any(not isinstance(value, str) or not value for value in customize_ids):
            raise CardCreateSearchResolutionError("invalid-customize-id")
        if any(not isinstance(value, str) or not value for value in effect_ids):
            raise CardCreateSearchResolutionError("invalid-card-effect-id")
        if self.search_effect_slot < 0 or self.search_effect_slot >= len(effect_ids):
            raise CardCreateSearchResolutionError("invalid-search-effect-slot")
        if effect_ids[self.search_effect_slot] != TARGET_EFFECT_ID:
            raise CardCreateSearchResolutionError("search-effect-slot-mismatch")
        object.__setattr__(self, "customize_ids", customize_ids)
        object.__setattr__(self, "effect_ids", effect_ids)

    @property
    def direct(self) -> bool:
        return True

    def to_dict(self) -> dict[str, object]:
        return {
            "card_id": self.card_id,
            "upgrade": self.upgrade,
            "name": self.name,
            "plan_type": self.plan_type,
            "max_customize_count": self.max_customize_count,
            "customize_ids": list(self.customize_ids),
            "effect_ids": list(self.effect_ids),
            "search_effect_slot": self.search_effect_slot,
            "direct": self.direct,
        }


def _row_value(row: Mapping[str, object] | sqlite3.Row, *names: str) -> object:
    if isinstance(row, Mapping):
        for name in names:
            if name in row:
                return row[name]
        return None
    keys = getattr(row, "keys", None)
    if callable(keys):
        available = set(keys())
        for name in names:
            if name in available:
                return row[name]
    return None


def _master_effect_id(row: Mapping[str, object] | sqlite3.Row) -> str | None:
    outer = _row_value(row, "id", "effect_id")
    raw_value = _row_value(row, "raw_json", "rawJson", "raw")
    raw: object = raw_value
    if isinstance(raw_value, (bytes, bytearray)):
        raw = raw_value.decode("utf-8")
    if isinstance(raw, str) and raw:
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as error:
            raise CardCreateSearchResolutionError("invalid-master-raw-json") from error
    raw_id = raw.get("id") if isinstance(raw, Mapping) else None
    if outer is not None and raw_id is not None and outer != raw_id:
        raise CardCreateSearchResolutionError("master-id-mismatch")
    value = raw_id if raw_id is not None else outer
    return value if isinstance(value, str) else None


def _require_plan2_common(
    row: Mapping[str, object] | sqlite3.Row,
    explicit_plan_type: str | None,
) -> None:
    observed = explicit_plan_type
    if observed is None:
        candidate = _row_value(
            row,
            "plan_type",
            "planType",
            "producePlanType",
            "card_plan_type",
        )
        if candidate is not None:
            observed = candidate if isinstance(candidate, str) else str(candidate)
    if observed is not None and observed not in PLAN2_COMMON_PLAN_TYPES:
        raise CardCreateSearchResolutionError("unsupported-plan", str(observed))


def _target_contract_checks(contract: CardCreateSearchEffectContract) -> None:
    if contract.effect_id != TARGET_EFFECT_ID:
        raise CardCreateSearchResolutionError(
            "out-of-scope-effect-id", contract.effect_id
        )
    if contract.search_id != TARGET_SEARCH_ID:
        raise CardCreateSearchResolutionError("target-search-mismatch")
    if contract.pool.pool_id != TARGET_POOL_ID:
        raise CardCreateSearchResolutionError("target-pool-mismatch")
    if contract.destination != "hand":
        raise CardCreateSearchResolutionError("target-destination-mismatch")
    if contract.pick_range_type != "ProducePickRangeType_Random":
        raise CardCreateSearchResolutionError("target-pick-range-mismatch")
    if (
        contract.pick_count_reference_search_id != ""
        or contract.pick_count_type != "ProducePickCountType_Unknown"
        or contract.pick_count_min != 1
        or contract.pick_count_max != 1
    ):
        raise CardCreateSearchResolutionError("target-pick-count-mismatch")
    if contract.effect_value1 != 1:
        raise CardCreateSearchResolutionError("target-multiplier-mismatch")
    if contract.pool.authoritative_source != "ProduceCardPool":
        raise CardCreateSearchResolutionError("unproven-pool-authority")


def load_master_plan2_card_create_search_rows(
    database: Path = DEFAULT_DATABASE,
) -> tuple[dict[str, object], ...]:
    """Read exactly the one target effect row from the local Master DB."""

    try:
        with sqlite3.connect(Path(database)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM effect WHERE id = ? AND effect_type = ?",
                (TARGET_EFFECT_ID, CARD_CREATE_SEARCH_EFFECT_TYPE),
            ).fetchall()
    except sqlite3.Error as error:
        raise CardCreateSearchResolutionError("master-effect-read-failed") from error
    if len(rows) != 1:
        raise CardCreateSearchResolutionError("target-effect-row-missing", TARGET_EFFECT_ID)
    return (dict(rows[0]),)


def resolve_plan2_card_create_search_contract(
    effect_row: Mapping[str, object] | sqlite3.Row,
    *,
    plan_type: str | None = None,
) -> Plan2CardCreateSearchContract:
    """Resolve the target row through the shared exact Master/PC resolver."""

    _require_plan2_common(effect_row, plan_type)
    effect_id = _master_effect_id(effect_row)
    if effect_id != TARGET_EFFECT_ID:
        raise CardCreateSearchResolutionError(
            "out-of-scope-effect-id", str(effect_id or "")
        )
    contract = resolve_card_create_search_contract(
        effect_row,
        catalog=default_card_create_search_catalog(),
    )
    _target_contract_checks(contract)
    return contract


def try_resolve_plan2_card_create_search_contract(
    effect_row: Mapping[str, object] | sqlite3.Row,
    *,
    plan_type: str | None = None,
) -> Plan2CardCreateSearchContract | CardCreateSearchUnresolvedInput:
    """Return a typed no-op input for an unknown or malformed row."""

    try:
        return resolve_plan2_card_create_search_contract(
            effect_row, plan_type=plan_type
        )
    except CardCreateSearchResolutionError as error:
        return CardCreateSearchUnresolvedInput(error.code, error.detail or error.code)


def load_plan2_common_card_versions(
    database: Path = DEFAULT_DATABASE,
) -> tuple[Plan2CommonCardVersion, ...]:
    """Read and validate the four target card versions from Master.

    The expected effect ordering and customization ids are structural facts
    from the local Master snapshot.  Names are copied for evidence only and
    never used to identify or execute an effect.
    """

    try:
        with sqlite3.connect(Path(database)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT id, upgrade_count, name, plan_type, raw_json "
                "FROM card WHERE id = ? ORDER BY upgrade_count",
                (TARGET_CARD_ID,),
            ).fetchall()
    except sqlite3.Error as error:
        raise CardCreateSearchResolutionError("master-card-read-failed") from error
    if tuple(row["upgrade_count"] for row in rows) != TARGET_UPGRADES:
        raise CardCreateSearchResolutionError("target-card-upgrade-rows-mismatch")

    versions: list[Plan2CommonCardVersion] = []
    for row in rows:
        upgrade = row["upgrade_count"]
        if (
            isinstance(upgrade, bool)
            or not isinstance(upgrade, int)
            or upgrade not in TARGET_UPGRADES
        ):
            raise CardCreateSearchResolutionError("unexpected-target-upgrade", str(upgrade))
        try:
            raw = json.loads(str(row["raw_json"]))
        except (TypeError, json.JSONDecodeError) as error:
            raise CardCreateSearchResolutionError(
                "invalid-target-card-raw-json", str(upgrade)
            ) from error
        if not isinstance(raw, Mapping):
            raise CardCreateSearchResolutionError("target-card-raw-must-be-object")
        raw_plan = raw.get("planType")
        if row["plan_type"] != PLAN_COMMON or raw_plan != PLAN_COMMON:
            raise CardCreateSearchResolutionError("unsupported-plan", str(raw_plan))
        raw_effects = raw.get("playEffects")
        if not isinstance(raw_effects, Sequence) or isinstance(
            raw_effects, (str, bytes, bytearray)
        ):
            raise CardCreateSearchResolutionError("invalid-target-card-effects", str(upgrade))
        effect_ids: list[str] = []
        for entry in raw_effects:
            if not isinstance(entry, Mapping) or not isinstance(
                entry.get("produceExamEffectId"), str
            ):
                raise CardCreateSearchResolutionError(
                    "invalid-target-card-effect-entry", str(upgrade)
                )
            if entry["produceExamEffectId"] == TARGET_EFFECT_ID and entry.get(
                "isOncePlayEffect"
            ) is not False:
                raise CardCreateSearchResolutionError(
                    "target-search-once-flag-mismatch", str(upgrade)
                )
            effect_ids.append(entry["produceExamEffectId"])
        observed_effect_ids = tuple(effect_ids)
        if observed_effect_ids != _EXPECTED_EFFECT_SEQUENCE[upgrade]:
            raise CardCreateSearchResolutionError(
                "target-card-effect-sequence-mismatch", str(upgrade)
            )
        customize_ids = raw.get("produceCardCustomizeIds")
        if not isinstance(customize_ids, Sequence) or isinstance(
            customize_ids, (str, bytes, bytearray)
        ):
            raise CardCreateSearchResolutionError(
                "invalid-target-card-customize-ids", str(upgrade)
            )
        observed_customize_ids = tuple(customize_ids)
        if observed_customize_ids != _EXPECTED_CUSTOMIZE_IDS[upgrade]:
            raise CardCreateSearchResolutionError(
                "target-card-customize-sequence-mismatch", str(upgrade)
            )
        max_customize_count = raw.get("maxCustomizeCount")
        expected_max = 0 if upgrade == 0 else 1
        if max_customize_count != expected_max:
            raise CardCreateSearchResolutionError(
                "target-card-customize-count-mismatch", str(upgrade)
            )
        if (
            not isinstance(row["id"], str)
            or row["id"] != TARGET_CARD_ID
            or not isinstance(row["name"], str)
            or not isinstance(row["plan_type"], str)
        ):
            raise CardCreateSearchResolutionError(
                "invalid-target-card-row", str(upgrade)
            )
        versions.append(
            Plan2CommonCardVersion(
                card_id=row["id"],
                upgrade=upgrade,
                name=row["name"],
                plan_type=row["plan_type"],
                max_customize_count=max_customize_count,
                customize_ids=observed_customize_ids,
                effect_ids=observed_effect_ids,
                search_effect_slot=observed_effect_ids.index(TARGET_EFFECT_ID),
            )
        )
    return tuple(versions)


def apply_plan2_card_create_search(
    state: Plan2CardCreateSearchState,
    contract: Plan2CardCreateSearchContract,
    *,
    chance_input: CardCreateSearchChanceInput | None = None,
    execution_input: CardCreateSearchChanceInput | None = None,
) -> Plan2CardCreateSearchResult:
    """Execute only the target through the shared native transition.

    An absent plan, ignore list, hand limit, or explicit GUID tokens remains a
    typed unchanged-state branch in the shared primitive.  An explicit plan
    outside Plan2/Common is rejected before it can select a guessed card.
    """

    if isinstance(contract, CardCreateSearchEffectContract):
        if contract.effect_id != TARGET_EFFECT_ID:
            raise CardCreateSearchInputError(
                "out-of-scope-effect-id", contract.effect_id
            )
    supplied = execution_input if execution_input is not None else chance_input
    if isinstance(supplied, CardCreateSearchChanceInput):
        if supplied.plan_type and supplied.plan_type not in PLAN2_COMMON_PLAN_TYPES:
            raise CardCreateSearchInputError("unsupported-plan", supplied.plan_type)
    return apply_card_create_search(
        state,
        contract,
        chance_input=chance_input,
        execution_input=execution_input,
    )


execute_plan2_card_create_search = apply_plan2_card_create_search
apply_plan2_common_card_create_search = apply_plan2_card_create_search


__all__ = [
    "ANDROID_EXECUTOR_MAPPING_SOURCE",
    "ANDROID_EXECUTOR_SOURCE",
    "ANDROID_TARGET_METADATA_SOURCE",
    "ANDROID_VERSION",
    "default_card_create_search_catalog",
    "CARD_CREATE_SEARCH_EFFECT_TYPE",
    "MASTER_DATABASE_SOURCE",
    "PC_CARD_SOURCE",
    "PC_POOL_SOURCE",
    "PC_SEARCH_SOURCE",
    "PC_TARGET_METADATA_SOURCE",
    "PLAN2",
    "PLAN2_COMMON_PLAN_TYPES",
    "PLAN_COMMON",
    "TARGET_CARD_ID",
    "TARGET_EFFECT_ID",
    "TARGET_POOL_ID",
    "TARGET_SEARCH_ID",
    "TARGET_UPGRADES",
    "CardCreateSearchAffectedCardVariant",
    "CardCreateSearchBranch",
    "CardCreateSearchCatalog",
    "CardCreateSearchChanceInput",
    "CardCreateSearchCondition",
    "CardCreateSearchDestination",
    "CardCreateSearchEffectContract",
    "CardCreateSearchError",
    "CardCreateSearchEvidenceLink",
    "CardCreateSearchExecutionInput",
    "CardCreateSearchInputError",
    "CardCreateSearchMutation",
    "CardCreateSearchNativeEvidence",
    "CardCreateSearchPool",
    "CardCreateSearchPoolCandidate",
    "CardCreateSearchResolutionError",
    "CardCreateSearchResolvedBranch",
    "CardCreateSearchResult",
    "CardCreateSearchTrace",
    "CardCreateSearchUnresolvedBranch",
    "CardCreateSearchUnresolvedInput",
    "Plan2CardCreateSearchCard",
    "Plan2CardCreateSearchContract",
    "Plan2CardCreateSearchResult",
    "Plan2CardCreateSearchState",
    "Plan2CommonCardVersion",
    "apply_plan2_card_create_search",
    "apply_plan2_common_card_create_search",
    "execute_plan2_card_create_search",
    "load_master_plan2_card_create_search_rows",
    "load_plan2_common_card_versions",
    "resolve_plan2_card_create_search_contract",
    "try_resolve_plan2_card_create_search_contract",
]
