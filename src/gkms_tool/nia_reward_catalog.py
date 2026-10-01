"""Read-only, fail-closed lookup of the bounded static N.I.A. reward graph.

This module deliberately sits beside the existing Master database helpers.  It
does not import route, GUI, live-source, item-rule, or Master-import code, and
opens the database with SQLite's read-only URI mode.  The result objects are
frozen and contain only exact rows/IDs recovered from the imported Master
tables.  In particular, a reward-set's selection cardinality is never treated
as a list of candidate cards/items.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Generic, Literal, TypeVar


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE = PROJECT_ROOT / "var" / "master.sqlite3"

_REWARD_SET_TYPE = "ProduceEffectType_ProduceRewardSet"
_ITEM_EFFECT_TYPE = "ProduceItemEffectType_ProduceEffect"
_EVENT_CHARACTER_TYPE = "ProduceEventType_Character"
_EVENT_BUSINESS_TYPE = "ProduceEventType_Business"
_SELECTION_TYPES = {
    "ProducePickRangeType_Select": "Select",
    "ProducePickRangeType_Random": "Random",
}
_RESOURCE_TYPES = {
    "ProduceResourceType_ProduceItem",
    "ProduceResourceType_ProduceCard",
    "ProduceResourceType_ProduceDrink",
}
_PRODUCE_MODES = MappingProxyType(
    {
        "produce-004": "Pro",
        "produce-005": "Master",
    }
)
_PLAN_TYPES = MappingProxyType(
    {
        "ProducePlanType_Common": "Common",
        "ProducePlanType_Plan1": "Plan1",
        "ProducePlanType_Plan2": "Plan2",
        "ProducePlanType_Plan3": "Plan3",
    }
)
_NIA_OWNER_TRIGGERS = frozenset(
    {
        "p_trigger-end_audition-for_nia_master",
        "p_trigger-end_before_audition_refresh-for_nia_master",
        "p_trigger-end_before_audition_refresh-produce_card_count-0022_0000-for_nia_master",
        "p_trigger-end_present-for_nia_master",
        "p_trigger-end_shop-for_nia_master",
        "p_trigger-end_step_event_activity-for_nia_master",
        "p_trigger-end_step_event_business-produce_card",
        "p_trigger-end_step_event_business-produce_drink",
        "p_trigger-end_step_event_business-produce_point",
        "p_trigger-start_customize-for_nia_master",
    }
)

_REQUIRED_COLUMNS = MappingProxyType(
    {
        "produce_effect": {
            "id": "TEXT",
            "effect_type": "TEXT",
            "effect_value_min": "INTEGER",
            "effect_value_max": "INTEGER",
            "resource_type": "TEXT",
            "rewards_json": "TEXT",
            "card_search_id": "TEXT",
            "pick_range_type": "TEXT",
            "pick_count_min": "INTEGER",
            "pick_count_max": "INTEGER",
            "is_research": "INTEGER",
        },
        "produce_mode": {"id": "TEXT"},
        "step_event_detail": {
            "id": "TEXT",
            "produce_story_id": "TEXT",
            "produce_story_group_id": "TEXT",
            "produce_effect_ids_json": "TEXT",
            "suggestion_ids_json": "TEXT",
            "event_type": "TEXT",
            "event_character_type": "TEXT",
        },
        "step_event_suggestion": {
            "id": "TEXT",
            "produce_effect_ids_json": "TEXT",
            "success_produce_effect_ids_json": "TEXT",
            "fail_produce_effect_ids_json": "TEXT",
        },
        "produce_item": {
            "id": "TEXT",
            "plan_type": "TEXT",
            "produce_trigger_id": "TEXT",
            "produce_item_effect_ids_json": "TEXT",
        },
        "produce_item_effect": {
            "id": "TEXT",
            "effect_type": "TEXT",
            "effect_turn": "INTEGER",
            "effect_count": "INTEGER",
            "produce_effect_id": "TEXT",
        },
        "produce_card_search": {
            "id": "TEXT",
            "card_rarities_json": "TEXT",
            "produce_card_ids_json": "TEXT",
            "upgrade_counts_json": "TEXT",
            "card_categories_json": "TEXT",
            "effect_group_ids_json": "TEXT",
        },
    }
)


class NiaRewardCatalogError(RuntimeError):
    """Raised only for an invalid catalog operation, not for a data blocker."""


CatalogState = Literal["ready", "unresolved", "blocked"]
CandidateState = Literal["explicit", "unresolved"]
CandidateSource = Literal["produceRewards", "cardSearch", "none"]
T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class CatalogBlocker:
    """A structured reason a static association cannot be exposed as ready."""

    code: str
    message: str
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for label, value in (("code", self.code), ("message", self.message)):
            if not isinstance(value, str) or not value:
                raise ValueError(f"blocker {label} must be non-empty text")
        _validate_text_tuple(self.evidence_ids, "blocker evidence_ids")


@dataclass(frozen=True, slots=True)
class CatalogResult(Generic[T]):
    """Immutable ready, unresolved, or blocked lookup result."""

    state: CatalogState
    value: T | None
    blockers: tuple[CatalogBlocker, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.blockers, tuple) or not all(
            isinstance(blocker, CatalogBlocker) for blocker in self.blockers
        ):
            raise TypeError("catalog blockers must be a tuple of CatalogBlocker")
        if self.state not in {"ready", "unresolved", "blocked"}:
            raise ValueError(f"unknown catalog state: {self.state!r}")
        if self.state == "ready" and (self.value is None or self.blockers):
            raise ValueError("ready catalog results require a value and no blockers")
        if self.state == "blocked" and (self.value is not None or not self.blockers):
            raise ValueError("blocked catalog results require blockers and no value")
        if self.state == "unresolved" and (
            self.value is not None or not self.blockers
        ):
            raise ValueError(
                "unresolved catalog results require blockers and no value"
            )

    @property
    def ready(self) -> bool:
        return self.state == "ready"

    @property
    def unresolved(self) -> bool:
        return self.state == "unresolved"

    @property
    def blocked(self) -> bool:
        return self.state == "blocked"

    @classmethod
    def ready_result(cls, value: T) -> "CatalogResult[T]":
        return cls("ready", value, ())

    @classmethod
    def blocked_result(
        cls, *blockers: CatalogBlocker
    ) -> "CatalogResult[T]":
        return cls("blocked", None, tuple(blockers))

    @classmethod
    def unresolved_result(
        cls, *blockers: CatalogBlocker
    ) -> "CatalogResult[T]":
        return cls("unresolved", None, tuple(blockers))


@dataclass(frozen=True, slots=True)
class CandidateMembership:
    """Candidate membership without deriving members from a pick count."""

    state: CandidateState
    members: tuple[str, ...] | None
    source: CandidateSource
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_text_tuple(self.evidence_ids, "candidate evidence_ids")
        if self.state == "explicit":
            if not self.members:
                raise ValueError("explicit candidate membership requires members")
            _validate_text_tuple(self.members, "candidate members")
            if self.source != "produceRewards":
                raise ValueError("only produceRewards can provide explicit members")
        elif self.state == "unresolved":
            if self.members is not None:
                raise ValueError("unresolved candidate membership cannot contain members")
            if self.source not in {"cardSearch", "none"}:
                raise ValueError("invalid unresolved candidate source")
        else:
            raise ValueError(f"unknown candidate membership state: {self.state!r}")

    @property
    def resolved(self) -> bool:
        return self.state == "explicit"

    @property
    def unresolved(self) -> bool:
        return self.state == "unresolved"


@dataclass(frozen=True, slots=True)
class RewardSetMetadata:
    """Validated metadata owned by one ProduceRewardSet effect row."""

    effect_id: str
    effect_type: str
    produce_id: str | None
    mode: str | None
    event_stage: str | None
    event_type: str | None
    event_character_type: str | None
    resource_type: str
    selection_method: str
    pick_range_type: str
    effect_value_min: int
    effect_value_max: int
    pick_count_min: int
    pick_count_max: int
    is_research: bool
    owner_plan: str | None
    reward_family: str | None
    card_search_id: str | None
    candidate_membership: CandidateMembership
    evidence_ids: tuple[str, ...]

    @property
    def plan(self) -> str | None:
        return None if self.owner_plan is None else _PLAN_TYPES.get(self.owner_plan)

    @property
    def candidate_members(self) -> tuple[str, ...] | None:
        return self.candidate_membership.members


@dataclass(frozen=True, slots=True)
class AuditionRewardAssociation:
    """One validated Mid1/Mid2 detail-to-reward association."""

    detail_id: str
    produce_story_id: str
    produce_story_group_id: str
    produce_id: str
    mode: str
    event_stage: str
    event_type: str
    event_character_type: str
    produce_point_min: int
    produce_point_max: int
    reward_set: RewardSetMetadata
    evidence_ids: tuple[str, ...]

    @property
    def effect_id(self) -> str:
        return self.reward_set.effect_id

    @property
    def candidate_membership(self) -> CandidateMembership:
        return self.reward_set.candidate_membership


@dataclass(frozen=True, slots=True)
class BusinessRewardAssociation:
    """A business suggestion joined through exact blank-story detail rows."""

    suggestion_id: str
    detail_ids: tuple[str, ...]
    produce_story_group_ids: tuple[str, ...]
    produce_story_ids: tuple[str, ...]
    source_produce_id: str
    source_mode: str
    event_stage: str
    event_type: str
    event_character_type: str
    owner_plan: str
    resource_selector: str
    attribute: str
    suggestion_family: str
    reward_set: RewardSetMetadata
    evidence_ids: tuple[str, ...]

    @property
    def effect_id(self) -> str:
        return self.reward_set.effect_id

    @property
    def produce_id(self) -> str | None:
        return self.reward_set.produce_id

    @property
    def mode(self) -> str | None:
        return self.reward_set.mode

    @property
    def candidate_membership(self) -> CandidateMembership:
        return self.reward_set.candidate_membership


@dataclass(frozen=True, slots=True)
class ItemRewardAssociation:
    """One item -> item-effect -> reward-set effect owner join."""

    item_id: str
    item_effect_id: str
    produce_effect_id: str
    owner_plan: str
    trigger_id: str
    reward_set: RewardSetMetadata
    evidence_ids: tuple[str, ...]

    @property
    def plan(self) -> str:
        return _PLAN_TYPES[self.owner_plan]

    @property
    def effect_id(self) -> str:
        return self.reward_set.effect_id

    @property
    def produce_id(self) -> str | None:
        return self.reward_set.produce_id

    @property
    def mode(self) -> str | None:
        return self.reward_set.mode

    @property
    def event_stage(self) -> str | None:
        return self.reward_set.event_stage

    @property
    def candidate_membership(self) -> CandidateMembership:
        return self.reward_set.candidate_membership


_NIA_AUDITION_DETAIL_RE = re.compile(
    r"^event-detail-002-(?P<story>p_story_002_[a-z0-9_]+_after-audition-"
    r"(?P<leg>a|b)-normal-(?:02|03))(?P<master>-produce_005)?$"
)
_AUDITION_STAGE_BY_CHARACTER_TYPE = MappingProxyType(
    {
        "ProduceEventCharacterType_AfterAuditionMid1": "Mid1",
        "ProduceEventCharacterType_AfterAuditionMid2": "Mid2",
    }
)
_AUDITION_STAGE_BY_LEG = MappingProxyType({"a": "Mid1", "b": "Mid2"})
_AUDITION_POINT_BY_STAGE = MappingProxyType({"Mid1": 100, "Mid2": 150})

_FINAL_DETAIL_PREFIX = "event-detail-002-p_story_002_"

_AUDITION_EFFECT_RE = re.compile(
    r"^p_effect-produce_reward_set-p_rd-item_set-produce_(?P<produce>004|005)"
    r"-end_mid(?P<stage>[12])_audition(?P<research>_reserch)?"
    r"-select-01_01$"
)
_BUSINESS_EFFECT_RE = re.compile(
    r"^p_effect-produce_reward_set-p_rd-produce-(?P<produce>004|005)"
    r"-business-before_(?P<stage>1st|2nd|3rd)-(?P<family>[^-]+)-"
    r"(?P<selection>upgrade_[01]|drink)-select-01_01$"
)
_BUSINESS_DRINK_EFFECT_RE = re.compile(
    r"^p_effect-produce_reward_set-p_rd-produce-(?P<produce>004|005)"
    r"-business-before_(?P<stage>1st|2nd|3rd)-drink-select-01_01$"
)
_MASTER_CHALLENGE_EFFECT_RE = re.compile(
    r"^p_effect-produce_reward_set-p_rd-produce-005-before_"
    r"(?P<stage>1st|2nd|3rd)-(?P<family>[^-]+)-for_nia_master-"
    r"select-01_01$"
)
_MASTER_BUSINESS_EFFECT_RE = re.compile(
    r"^p_effect-produce_reward_set-p_rd-produce-005-business-before_"
    r"(?P<stage>2nd)-(?P<family>[^-]+)-upgrade_1-select-01_01$"
)
_ACTIVITY_EFFECT_RE = re.compile(
    r"^p_effect-produce_reward_set-p_rd-produce-004-event_activity-"
    r"drink-(?:(?P<family>[^-]+)-)?random-(?P<min>\d+)_(?P<max>\d+)$"
)
_BUSINESS_SUGGESTION_RE = re.compile(
    r"^p_s_e_s-event-detail-business-(?P<produce>produce_(?:004|005))-"
    r"before_(?P<stage>1st|2nd|3rd)-"
    r"(?P<selector>produce_card|produce_point|produce_drink|stamina)-"
    r"(?P<plan>plan[123])-(?P<attribute>vocal|dance|visual)-"
    r"(?P<family>active|lesson_buff|mental|parameter_buff|review|"
    r"card_play_aggressive|concentration|full_power)$"
)

_BUSINESS_FAMILY_MAP = MappingProxyType(
    {
        "active": "active_skill",
        "lesson_buff": "exam_lesson_buff",
        "parameter_buff": "exam_parameter_buff",
        "mental": "mental_skill",
        "review": "exam_review",
        "card_play_aggressive": "exam_card_play_aggressive",
        "concentration": "exam_concentration",
        "full_power": "exam_full_power",
    }
)


@dataclass(frozen=True, slots=True)
class _EffectShape:
    produce_id: str
    event_stage: str
    reward_family: str
    expected_resource: str
    expected_selection: str
    expected_count_min: int
    expected_count_max: int
    expected_research: bool


def _validate_text_tuple(values: object, label: str) -> tuple[str, ...]:
    if not isinstance(values, tuple) or not all(
        isinstance(value, str) and value for value in values
    ):
        raise TypeError(f"{label} must be a tuple of non-empty strings")
    if len(values) != len(set(values)):
        raise ValueError(f"{label} must not contain duplicates")
    return values


def _unique_ids(ids: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    result: list[str] = []
    for item in ids:
        if item and item not in result:
            result.append(item)
    return tuple(result)


def _blocker(
    code: str, message: str, evidence_ids: tuple[str, ...] | list[str] = ()
) -> CatalogBlocker:
    return CatalogBlocker(code, message, _unique_ids(evidence_ids))


def _canonical_produce(value: str) -> str:
    if value in {"004", "produce_004"}:
        return "produce-004"
    if value in {"005", "produce_005"}:
        return "produce-005"
    return value.replace("_", "-", 1) if value.startswith("produce_") else value


def _parse_effect_shape(effect_id: str) -> _EffectShape | None:
    match = _AUDITION_EFFECT_RE.fullmatch(effect_id)
    if match is not None:
        produce_id = _canonical_produce(match.group("produce"))
        return _EffectShape(
            produce_id=produce_id,
            event_stage=f"Mid{match.group('stage')}",
            reward_family="audition_item_set",
            expected_resource="ProduceResourceType_ProduceItem",
            expected_selection="Select",
            expected_count_min=1,
            expected_count_max=1,
            expected_research=bool(match.group("research")),
        )

    match = _BUSINESS_EFFECT_RE.fullmatch(effect_id)
    if match is not None:
        selection = match.group("selection")
        return _EffectShape(
            produce_id=_canonical_produce(match.group("produce")),
            event_stage=match.group("stage"),
            reward_family=match.group("family"),
            expected_resource=(
                "ProduceResourceType_ProduceDrink"
                if selection == "drink"
                else "ProduceResourceType_ProduceCard"
            ),
            expected_selection="Select",
            expected_count_min=1,
            expected_count_max=1,
            expected_research=False,
        )

    match = _BUSINESS_DRINK_EFFECT_RE.fullmatch(effect_id)
    if match is not None:
        return _EffectShape(
            produce_id=_canonical_produce(match.group("produce")),
            event_stage=match.group("stage"),
            reward_family="drink",
            expected_resource="ProduceResourceType_ProduceDrink",
            expected_selection="Select",
            expected_count_min=1,
            expected_count_max=1,
            expected_research=False,
        )

    match = _MASTER_CHALLENGE_EFFECT_RE.fullmatch(effect_id)
    if match is not None:
        return _EffectShape(
            produce_id="produce-005",
            event_stage=match.group("stage"),
            reward_family=match.group("family"),
            expected_resource="ProduceResourceType_ProduceCard",
            expected_selection="Select",
            expected_count_min=1,
            expected_count_max=1,
            expected_research=False,
        )

    match = _MASTER_BUSINESS_EFFECT_RE.fullmatch(effect_id)
    if match is not None:
        return _EffectShape(
            produce_id="produce-005",
            event_stage=match.group("stage"),
            reward_family=match.group("family"),
            expected_resource="ProduceResourceType_ProduceCard",
            expected_selection="Select",
            expected_count_min=1,
            expected_count_max=1,
            expected_research=False,
        )

    match = _ACTIVITY_EFFECT_RE.fullmatch(effect_id)
    if match is not None:
        return _EffectShape(
            produce_id="produce-004",
            event_stage="Activity",
            reward_family=match.group("family") or "random",
            expected_resource="ProduceResourceType_ProduceDrink",
            expected_selection="Random",
            expected_count_min=int(match.group("min")),
            expected_count_max=int(match.group("max")),
            expected_research=False,
        )
    return None


def _parse_id_list(
    payload: Any,
    *,
    field: str,
    evidence_ids: tuple[str, ...],
) -> tuple[tuple[str, ...], tuple[CatalogBlocker, ...]]:
    try:
        value = json.loads(payload) if isinstance(payload, str) else payload
    except (TypeError, json.JSONDecodeError) as exc:
        return (), (
            _blocker(
                "MALFORMED_ID_LIST",
                f"{field} is not valid JSON: {exc}",
                evidence_ids,
            ),
        )
    if not isinstance(value, list):
        return (), (
            _blocker("MALFORMED_ID_LIST", f"{field} must be a JSON list", evidence_ids),
        )
    blockers: list[CatalogBlocker] = []
    ids: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item:
            blockers.append(
                _blocker(
                    "MALFORMED_ID_LIST",
                    f"{field}[{index}] must be a non-empty string",
                    evidence_ids,
                )
            )
            continue
        if item in ids:
            blockers.append(
                _blocker(
                    "DUPLICATE_ID",
                    f"{field} contains duplicate ID {item!r}",
                    evidence_ids + (item,),
                )
            )
        ids.append(item)
    return tuple(ids), tuple(blockers)


def _parse_json_array(
    payload: Any,
    *,
    field: str,
    evidence_ids: tuple[str, ...],
) -> tuple[tuple[Any, ...], tuple[CatalogBlocker, ...]]:
    try:
        value = json.loads(payload) if isinstance(payload, str) else payload
    except (TypeError, json.JSONDecodeError) as exc:
        return (), (
            _blocker(
                "MALFORMED_JSON_ARRAY",
                f"{field} is not valid JSON: {exc}",
                evidence_ids,
            ),
        )
    if not isinstance(value, list):
        return (), (
            _blocker("MALFORMED_JSON_ARRAY", f"{field} must be a JSON list", evidence_ids),
        )
    return tuple(value), ()


def _validate_identifier(value: Any, label: str) -> tuple[str | None, tuple[CatalogBlocker, ...]]:
    if not isinstance(value, str) or not value or "\x00" in value:
        return None, (
            _blocker("INVALID_ID", f"{label} must be a non-empty ID string"),
        )
    return value, ()


def _parse_business_suggestion_id(
    suggestion_id: str,
) -> tuple[re.Match[str] | None, tuple[CatalogBlocker, ...]]:
    match = _BUSINESS_SUGGESTION_RE.fullmatch(suggestion_id)
    if match is None:
        return None, (
            _blocker(
                "MALFORMED_BUSINESS_SUGGESTION_ID",
                f"unsupported business suggestion ID shape: {suggestion_id}",
                (suggestion_id,),
            ),
        )
    return match, ()


def _plan_type_for_token(token: str) -> str:
    return f"ProducePlanType_{token.capitalize()}"


def _connection_uri(database: Path) -> str:
    return f"{database.resolve().as_uri()}?mode=ro"


def _schema_blockers(
    connection: sqlite3.Connection, tables: tuple[str, ...]
) -> tuple[CatalogBlocker, ...]:
    blockers: list[CatalogBlocker] = []
    for table in tables:
        expected = _REQUIRED_COLUMNS.get(table)
        if expected is None:
            blockers.append(
                _blocker(
                    "CATALOG_SCHEMA_INVALID",
                    f"no schema contract is registered for required table {table}",
                    (table,),
                )
            )
            continue
        columns = {
            str(row["name"]): str(row["type"]).upper()
            for row in connection.execute(f'PRAGMA table_info("{table}")')
        }
        for column, affinity in expected.items():
            actual = columns.get(column)
            if actual is None:
                blockers.append(
                    _blocker(
                        "CATALOG_SCHEMA_INVALID",
                        f"required column is missing: {table}.{column}",
                        (table, column),
                    )
                )
            elif actual != affinity:
                blockers.append(
                    _blocker(
                        "CATALOG_SCHEMA_INVALID",
                        f"column {table}.{column} has affinity {actual!r}, expected {affinity}",
                        (table, column),
                    )
                )
    return tuple(blockers)


class NiaRewardCatalog:
    """A deterministic read-only view of the bounded N.I.A. reward graph."""

    def __init__(self, database: Path = DEFAULT_DATABASE) -> None:
        self.database = Path(database)

    def _open(self) -> sqlite3.Connection:
        if not self.database.is_file():
            raise NiaRewardCatalogError(f"Master database does not exist: {self.database}")
        try:
            connection = sqlite3.connect(_connection_uri(self.database), uri=True)
        except (OSError, sqlite3.Error) as exc:
            raise NiaRewardCatalogError(
                f"cannot open Master database read-only: {self.database}: {exc}"
            ) from exc
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        return connection

    def _run(
        self,
        required_tables: tuple[str, ...],
        operation: Callable[[sqlite3.Connection], CatalogResult[T]],
    ) -> CatalogResult[T]:
        if not self.database.is_file():
            return CatalogResult.blocked_result(
                _blocker(
                    "DATABASE_NOT_FOUND",
                    f"Master database does not exist: {self.database}",
                )
            )
        try:
            with closing(self._open()) as connection:
                placeholders = ",".join("?" for _ in required_tables)
                rows = connection.execute(
                    f"SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ({placeholders})",
                    required_tables,
                ).fetchall()
                present = {str(row[0]) for row in rows}
                missing = tuple(table for table in required_tables if table not in present)
                if missing:
                    return CatalogResult.blocked_result(
                        _blocker(
                            "CATALOG_SCHEMA_MISSING",
                            f"required Master tables are missing: {', '.join(missing)}",
                            missing,
                        )
                    )
                schema_blockers = _schema_blockers(connection, required_tables)
                if schema_blockers:
                    return CatalogResult.blocked_result(*schema_blockers)
                return operation(connection)
        except NiaRewardCatalogError as exc:
            return CatalogResult.blocked_result(_blocker("DATABASE_ERROR", str(exc)))
        except sqlite3.Error as exc:
            return CatalogResult.blocked_result(
                _blocker("DATABASE_ERROR", f"read-only Master query failed: {exc}")
            )
        except (
            IndexError,
            KeyError,
            TypeError,
            json.JSONDecodeError,
            ValueError,
        ) as exc:
            return CatalogResult.blocked_result(
                _blocker(
                    "CATALOG_SCHEMA_INVALID",
                    f"Master row could not be decoded by the strict schema: {type(exc).__name__}: {exc}",
                )
            )

    def lookup_reward_set(
        self, effect_id: str
    ) -> CatalogResult[RewardSetMetadata]:
        """Return validated metadata for an exact bounded N.I.A. reward-set ID."""

        identifier, blockers = _validate_identifier(effect_id, "effect_id")
        if blockers:
            return CatalogResult.blocked_result(*blockers)
        assert identifier is not None
        return self._run(
            ("produce_effect", "produce_mode"),
            lambda connection: self._lookup_reward_set_connection(connection, identifier),
        )

    def lookup_audition_detail(
        self, detail_id: str
    ) -> CatalogResult[AuditionRewardAssociation]:
        """Resolve one exact N.I.A. produce-004/005 Mid1/Mid2 detail."""

        identifier, blockers = _validate_identifier(detail_id, "detail_id")
        if blockers:
            return CatalogResult.blocked_result(*blockers)
        assert identifier is not None
        return self._run(
            ("step_event_detail", "produce_effect", "produce_mode"),
            lambda connection: self._lookup_audition_connection(connection, identifier),
        )

    def lookup_business_suggestion(
        self, suggestion_id: str
    ) -> CatalogResult[BusinessRewardAssociation]:
        """Resolve one exact business suggestion, including the 005 -> 004 alias."""

        identifier, blockers = _validate_identifier(suggestion_id, "suggestion_id")
        if blockers:
            return CatalogResult.blocked_result(*blockers)
        assert identifier is not None
        return self._run(
            (
                "step_event_detail",
                "step_event_suggestion",
                "produce_effect",
                "produce_mode",
            ),
            lambda connection: self._lookup_business_suggestion_connection(
                connection, identifier
            ),
        )

    def lookup_business_detail(
        self, detail_id: str
    ) -> CatalogResult[tuple[BusinessRewardAssociation, ...]]:
        """Compatibility alias for :meth:`lookup_business_detail_options`."""

        return self.lookup_business_detail_options(detail_id)

    def lookup_business_detail_options(
        self, detail_id: str
    ) -> CatalogResult[tuple[BusinessRewardAssociation, ...]]:
        """Return every explicit suggestion option for a blank-story detail."""

        identifier, blockers = _validate_identifier(detail_id, "detail_id")
        if blockers:
            return CatalogResult.blocked_result(*blockers)
        assert identifier is not None
        return self._run(
            (
                "step_event_detail",
                "step_event_suggestion",
                "produce_effect",
                "produce_mode",
            ),
            lambda connection: self._lookup_business_detail_connection(
                connection, identifier
            ),
        )

    def resolve_business_reward(
        self, detail_id: str, suggestion_id: str | None = None
    ) -> CatalogResult[BusinessRewardAssociation]:
        """Resolve an actionable business reward using one exact suggestion ID."""

        identifier, blockers = _validate_identifier(detail_id, "detail_id")
        if blockers:
            return CatalogResult.blocked_result(*blockers)
        if suggestion_id is not None:
            selected, blockers = _validate_identifier(suggestion_id, "suggestion_id")
            if blockers:
                return CatalogResult.blocked_result(*blockers)
        else:
            selected = None
        assert identifier is not None
        return self._run(
            (
                "step_event_detail",
                "step_event_suggestion",
                "produce_effect",
                "produce_mode",
            ),
            lambda connection: self._resolve_business_reward_connection(
                connection, identifier, selected
            ),
        )

    def lookup_item_reward_sets(
        self, item_id: str
    ) -> CatalogResult[tuple[ItemRewardAssociation, ...]]:
        """Resolve one exact item ID to its reward-set owner."""

        identifier, blockers = _validate_identifier(item_id, "item_id")
        if blockers:
            return CatalogResult.blocked_result(*blockers)
        assert identifier is not None
        return self._run(
            ("produce_item", "produce_item_effect", "produce_effect", "produce_mode"),
            lambda connection: self._lookup_item_connection(connection, identifier),
        )

    def list_item_reward_family(
        self, item_id_prefix: str
    ) -> CatalogResult[tuple[ItemRewardAssociation, ...]]:
        """Enumerate reward-set owners below an explicitly named item family prefix."""

        identifier, blockers = _validate_identifier(item_id_prefix, "item_id_prefix")
        if blockers:
            return CatalogResult.blocked_result(*blockers)
        assert identifier is not None
        return self._run(
            ("produce_item", "produce_item_effect", "produce_effect", "produce_mode"),
            lambda connection: self._list_item_family_connection(
                connection, identifier
            ),
        )

    def lookup_item_reward_association(
        self, item_id: str
    ) -> CatalogResult[ItemRewardAssociation]:
        """Singular exact-item convenience API."""

        result = self.lookup_item_reward_sets(item_id)
        if not result.ready:
            return CatalogResult(result.state, None, result.blockers)
        assert result.value is not None
        if len(result.value) != 1:
            return CatalogResult.blocked_result(
                _blocker(
                    "AMBIGUOUS_ITEM_ID",
                    f"item selector resolves to {len(result.value)} exact item rows; use the plural API",
                    tuple(item.item_id for item in result.value),
                )
            )
        return CatalogResult.ready_result(result.value[0])

    def lookup_final_reward(
        self, detail_id: str
    ) -> CatalogResult[RewardSetMetadata]:
        """Return an explicit unresolved result for the unproven final reward."""

        identifier, blockers = _validate_identifier(detail_id, "detail_id")
        if blockers:
            return CatalogResult.blocked_result(*blockers)
        assert identifier is not None
        return self._run(
            ("step_event_detail",),
            lambda connection: self._lookup_final_connection(connection, identifier),
        )

    def _lookup_reward_set_connection(
        self, connection: sqlite3.Connection, effect_id: str
    ) -> CatalogResult[RewardSetMetadata]:
        row = connection.execute(
            "SELECT * FROM produce_effect WHERE id = ?", (effect_id,)
        ).fetchone()
        if row is None:
            return CatalogResult.blocked_result(
                _blocker("UNKNOWN_EFFECT_ID", f"unknown produce effect: {effect_id}", (effect_id,))
            )
        metadata, blockers = _build_metadata(
            connection,
            row,
            evidence_ids=(effect_id,),
        )
        if blockers:
            return CatalogResult.blocked_result(*blockers)
        assert metadata is not None
        return CatalogResult.ready_result(metadata)

    def _lookup_audition_connection(
        self, connection: sqlite3.Connection, detail_id: str
    ) -> CatalogResult[AuditionRewardAssociation]:
        row = connection.execute(
            "SELECT * FROM step_event_detail WHERE id = ?", (detail_id,)
        ).fetchone()
        if row is None:
            return CatalogResult.blocked_result(
                _blocker("UNKNOWN_DETAIL_ID", f"unknown step event detail: {detail_id}", (detail_id,))
            )

        blockers: list[CatalogBlocker] = []
        detail_match = _NIA_AUDITION_DETAIL_RE.fullmatch(detail_id)
        if detail_match is None:
            blockers.append(
                _blocker(
                    "AUDITION_ASSOCIATION_NOT_PROVEN",
                    "detail ID is outside the exact N.I.A. Mid1/Mid2 character-detail shape",
                    (detail_id,),
                )
            )
        if row["event_type"] != _EVENT_CHARACTER_TYPE:
            blockers.append(
                _blocker(
                    "WRONG_EVENT_TYPE",
                    f"audition detail has event type {row['event_type']!r}",
                    (detail_id,),
                )
            )
        event_character_type = row["event_character_type"]
        event_stage = _AUDITION_STAGE_BY_CHARACTER_TYPE.get(event_character_type)
        if event_stage is None:
            blockers.append(
                _blocker(
                    "WRONG_EVENT_STAGE",
                    f"audition detail has unsupported character type {event_character_type!r}",
                    (detail_id,),
                )
            )
        if detail_match is not None and event_stage is not None:
            id_stage = _AUDITION_STAGE_BY_LEG[detail_match.group("leg")]
            if id_stage != event_stage:
                blockers.append(
                    _blocker(
                        "WRONG_EVENT_STAGE",
                        f"detail ID implies {id_stage}, row character type implies {event_stage}",
                        (detail_id,),
                    )
                )
        story_id = row["produce_story_id"]
        story_group_id = row["produce_story_group_id"]
        if not isinstance(story_id, str) or not story_id:
            blockers.append(
                _blocker(
                    "MISSING_STORY_ID",
                    "the proven audition detail must retain its exact produceStoryId",
                    (detail_id,),
                )
            )
        elif detail_match is not None and story_id != detail_match.group("story"):
            blockers.append(
                _blocker(
                    "CONFLICTING_STORY_ID",
                    "audition detail ID and produceStoryId disagree",
                    (detail_id, story_id),
                )
            )
        if not isinstance(story_group_id, str):
            blockers.append(
                _blocker(
                    "MALFORMED_STORY_GROUP_ID",
                    "audition produceStoryGroupId must be text",
                    (detail_id,),
                )
            )
        effect_ids, effect_id_blockers = _parse_id_list(
            row["produce_effect_ids_json"],
            field="step_event_detail.produce_effect_ids_json",
            evidence_ids=(detail_id,),
        )
        blockers.extend(effect_id_blockers)
        effect_rows, effect_blockers = _load_effect_rows(
            connection, effect_ids, (detail_id,)
        )
        blockers.extend(effect_blockers)

        point_rows = tuple(
            effect
            for effect in effect_rows
            if effect["effect_type"] == "ProduceEffectType_ProducePointAddition"
        )
        reward_rows = tuple(
            effect for effect in effect_rows if effect["effect_type"] == _REWARD_SET_TYPE
        )
        other_rows = tuple(
            effect
            for effect in effect_rows
            if effect["effect_type"]
            not in {"ProduceEffectType_ProducePointAddition", _REWARD_SET_TYPE}
        )
        if other_rows or len(effect_rows) != 2:
            blockers.append(
                _blocker(
                    "CONFLICTING_DETAIL_EFFECTS",
                    "audition detail must contain exactly one point effect and one reward-set effect",
                    (detail_id,) + effect_ids,
                )
            )
        if len(point_rows) != 1:
            blockers.append(
                _blocker(
                    "AMBIGUOUS_POINT_EFFECT",
                    "audition detail must have exactly one produce-point effect",
                    (detail_id,),
                )
            )
        if len(reward_rows) != 1:
            blockers.append(
                _blocker(
                    "AMBIGUOUS_REWARD_SET_EFFECT",
                    "audition detail must have exactly one reward-set effect",
                    (detail_id,),
                )
            )
        point_min = point_max = 0
        if point_rows:
            point_min = point_rows[0]["effect_value_min"]
            point_max = point_rows[0]["effect_value_max"]
            if (
                not isinstance(point_min, int)
                or isinstance(point_min, bool)
                or not isinstance(point_max, int)
                or isinstance(point_max, bool)
                or point_min < 0
                or point_min != point_max
            ):
                blockers.append(
                    _blocker(
                        "CONFLICTING_POINT_VALUE",
                        f"audition point effect must be one fixed non-negative integer, got {point_min!r}/{point_max!r}",
                        (detail_id, str(point_rows[0]["id"])),
                    )
                )
            elif (
                event_stage is not None
                and point_min != _AUDITION_POINT_BY_STAGE[event_stage]
            ):
                blockers.append(
                    _blocker(
                        "CONFLICTING_POINT_VALUE",
                        f"{event_stage} audition detail must award exactly {_AUDITION_POINT_BY_STAGE[event_stage]} produce points",
                        (detail_id, str(point_rows[0]["id"])),
                    )
                )

        reward_shape = (
            None
            if len(reward_rows) != 1
            else _parse_effect_shape(str(reward_rows[0]["id"]))
        )
        if reward_shape is None or reward_shape.reward_family != "audition_item_set":
            blockers.append(
                _blocker(
                    "CONFLICTING_REWARD_SET_EFFECT",
                    "audition detail does not reference one legal N.I.A. audition reward set",
                    (detail_id,) + tuple(str(value["id"]) for value in reward_rows),
                )
            )
        expected_produce_id = None
        if detail_match is not None:
            expected_produce_id = (
                "produce-005" if detail_match.group("master") else "produce-004"
            )
        if reward_shape is not None:
            if event_stage is not None and reward_shape.event_stage != event_stage:
                blockers.append(
                    _blocker(
                        "CONFLICTING_REWARD_SET_METADATA",
                        "audition reward stage disagrees with eventCharacterType",
                        (detail_id, str(reward_rows[0]["id"])),
                    )
                )
            if (
                expected_produce_id is not None
                and reward_shape.produce_id != expected_produce_id
            ):
                blockers.append(
                    _blocker(
                        "CONFLICTING_REWARD_SET_METADATA",
                        "audition reward produce mode disagrees with the exact detail ID",
                        (detail_id, str(reward_rows[0]["id"])),
                    )
                )

        if blockers or reward_shape is None or event_stage is None:
            return CatalogResult.blocked_result(*blockers)

        assert reward_rows and point_rows
        reward_effect_id = str(reward_rows[0]["id"])
        metadata, metadata_blockers = _build_metadata(
            connection,
            reward_rows[0],
            evidence_ids=(detail_id, reward_effect_id),
            event_type=row["event_type"],
            event_character_type=event_character_type,
        )
        blockers.extend(metadata_blockers)
        if metadata is None:
            blockers.append(
                _blocker(
                    "MISSING_REWARD_SET_METADATA",
                    "reward-set metadata could not be validated",
                    (detail_id, reward_effect_id),
                )
            )
        elif (
            metadata.produce_id != reward_shape.produce_id
            or metadata.event_stage != event_stage
        ):
            blockers.append(
                _blocker(
                    "CONFLICTING_REWARD_SET_METADATA",
                    "reward-set metadata does not match the proven audition association",
                    (detail_id, metadata.effect_id),
                )
            )
        if blockers:
            return CatalogResult.blocked_result(*blockers)
        assert metadata is not None
        evidence_ids = _unique_ids(
            (detail_id, str(story_id), str(point_rows[0]["id"]), reward_effect_id)
        )
        return CatalogResult.ready_result(
            AuditionRewardAssociation(
                detail_id=detail_id,
                produce_story_id=str(story_id),
                produce_story_group_id=str(story_group_id or ""),
                produce_id=metadata.produce_id or reward_shape.produce_id,
                mode=metadata.mode or _PRODUCE_MODES[reward_shape.produce_id],
                event_stage=event_stage,
                event_type=str(row["event_type"]),
                event_character_type=str(row["event_character_type"]),
                produce_point_min=point_min,
                produce_point_max=point_max,
                reward_set=replace(metadata, evidence_ids=evidence_ids),
                evidence_ids=evidence_ids,
            )
        )

    def _lookup_business_suggestion_connection(
        self, connection: sqlite3.Connection, suggestion_id: str
    ) -> CatalogResult[BusinessRewardAssociation]:
        suggestion = connection.execute(
            "SELECT * FROM step_event_suggestion WHERE id = ?", (suggestion_id,)
        ).fetchone()
        if suggestion is None:
            return CatalogResult.blocked_result(
                _blocker(
                    "UNKNOWN_SUGGESTION_ID",
                    f"unknown step event suggestion: {suggestion_id}",
                    (suggestion_id,),
                )
            )
        match, id_blockers = _parse_business_suggestion_id(suggestion_id)
        blockers: list[CatalogBlocker] = list(id_blockers)
        if match is None:
            return CatalogResult.blocked_result(*blockers)

        source_token = match.group("produce")
        source_produce_id = _canonical_produce(source_token)
        source_mode, mode_blockers = _mode_name(connection, source_produce_id, (suggestion_id,))
        blockers.extend(mode_blockers)
        stage = match.group("stage")
        selector = match.group("selector")
        plan_token = match.group("plan")
        owner_plan = _plan_type_for_token(plan_token)
        attribute = match.group("attribute")
        suggestion_family = match.group("family")

        detail_rows, detail_blockers = _business_detail_rows_for_suggestion(
            connection, suggestion_id
        )
        blockers.extend(detail_blockers)
        expected_detail_prefix = (
            f"event-detail-business-{source_token}-{plan_token}-"
            f"{selector}-before_{stage}-"
        )
        detail_rows = tuple(
            detail
            for detail in detail_rows
            if str(detail["id"]).startswith(expected_detail_prefix)
            and detail["event_type"] == _EVENT_BUSINESS_TYPE
            and detail["produce_story_id"] == ""
        )
        if not detail_rows:
            blockers.append(
                _blocker(
                    "MISSING_BUSINESS_DETAIL_JOIN",
                    "business suggestion is not referenced by an exact blank-story business detail",
                    (suggestion_id,),
                )
            )
        else:
            for detail in detail_rows:
                detail_id = str(detail["id"])
                if not isinstance(detail["produce_story_group_id"], str) or not detail[
                    "produce_story_group_id"
                ]:
                    blockers.append(
                        _blocker(
                            "MISSING_BUSINESS_STORY_GROUP_ID",
                            f"business detail {detail_id} has no produceStoryGroupId",
                            (suggestion_id, detail_id),
                        )
                    )

        primary_ids, primary_blockers = _parse_id_list(
            suggestion["produce_effect_ids_json"],
            field="step_event_suggestion.produce_effect_ids_json",
            evidence_ids=(suggestion_id,),
        )
        success_ids, success_blockers = _parse_id_list(
            suggestion["success_produce_effect_ids_json"],
            field="step_event_suggestion.success_produce_effect_ids_json",
            evidence_ids=(suggestion_id,),
        )
        fail_ids, fail_blockers = _parse_id_list(
            suggestion["fail_produce_effect_ids_json"],
            field="step_event_suggestion.fail_produce_effect_ids_json",
            evidence_ids=(suggestion_id,),
        )
        blockers.extend(primary_blockers)
        blockers.extend(success_blockers)
        blockers.extend(fail_blockers)
        primary_rows, primary_row_blockers = _load_effect_rows(
            connection, primary_ids, (suggestion_id,)
        )
        blockers.extend(primary_row_blockers)
        success_rows, success_row_blockers = _load_effect_rows(
            connection, success_ids, (suggestion_id,)
        )
        fail_rows, fail_row_blockers = _load_effect_rows(
            connection, fail_ids, (suggestion_id,)
        )
        blockers.extend(success_row_blockers)
        blockers.extend(fail_row_blockers)
        branch_rows: tuple[tuple[str, tuple[sqlite3.Row, ...]], ...] = (
            ("success", success_rows),
            ("fail", fail_rows),
        )
        for branch_name, rows in branch_rows:
            if any(row["effect_type"] == _REWARD_SET_TYPE for row in rows):
                blockers.append(
                    _blocker(
                        "BRANCHED_REWARD_SET_ASSOCIATION",
                        f"business reward-set effect appears in {branch_name} branch, not only the exact primary association",
                        (suggestion_id,),
                    )
                )
        reward_rows = tuple(
            row for row in primary_rows if row["effect_type"] == _REWARD_SET_TYPE
        )
        if not reward_rows:
            blockers.append(
                _blocker(
                    "AMBIGUOUS_SUGGESTION_REWARD_SET",
                    "business suggestion must have at least one primary reward-set effect",
                    (suggestion_id,) + primary_ids,
                )
            )
        if blockers:
            return CatalogResult.blocked_result(*blockers)
        assert reward_rows

        family = _BUSINESS_FAMILY_MAP.get(suggestion_family)
        if family is None:
            return CatalogResult.blocked_result(
                _blocker(
                    "UNKNOWN_BUSINESS_REWARD_FAMILY",
                    f"unsupported business suggestion family: {suggestion_family}",
                    (suggestion_id,),
                )
            )
        suffix = {
            "produce_card": "upgrade_1",
            "produce_point": "upgrade_0",
            "stamina": "upgrade_0",
            "produce_drink": "drink",
        }.get(selector)
        if suffix is None:
            return CatalogResult.blocked_result(
                _blocker(
                    "UNKNOWN_BUSINESS_RESOURCE_SELECTOR",
                    f"unsupported business resource selector: {selector}",
                    (suggestion_id,),
                )
            )
        target_rows: tuple[sqlite3.Row, ...]
        target_matches: tuple[re.Match[str], ...]
        if selector == "produce_drink":
            parsed_targets = tuple(
                (row, match)
                for row in reward_rows
                if (match := _BUSINESS_DRINK_EFFECT_RE.fullmatch(str(row["id"])))
                is not None
                and match.group("stage") == stage
            )
        else:
            parsed_targets = tuple(
                (row, match)
                for row in reward_rows
                if (match := _BUSINESS_EFFECT_RE.fullmatch(str(row["id"])))
                is not None
                and match.group("stage") == stage
                and match.group("family") == family
                and match.group("selection") == suffix
            )
        target_rows = tuple(row for row, _ in parsed_targets)
        target_matches = tuple(match for _, match in parsed_targets)

        if selector != "produce_drink" and len(reward_rows) != 1:
            blockers.append(
                _blocker(
                    "AMBIGUOUS_SUGGESTION_REWARD_SET",
                    "business suggestion must have exactly one primary reward-set effect",
                    (suggestion_id,) + tuple(str(row["id"]) for row in reward_rows),
                )
            )
        if len(target_rows) != 1:
            blockers.append(
                _blocker(
                    "AMBIGUOUS_SUGGESTION_REWARD_SET",
                    "resource selector does not identify exactly one primary reward-set effect",
                    (suggestion_id,) + tuple(str(row["id"]) for row in reward_rows),
                )
            )
        elif selector == "produce_drink":
            target_produce = target_matches[0].group("produce")
            target_effect_id = str(target_rows[0]["id"])
            companion_rows = tuple(
                row
                for row in reward_rows
                if str(row["id"]) != target_effect_id
                and (
                    companion_match := _BUSINESS_EFFECT_RE.fullmatch(
                        str(row["id"])
                    )
                )
                is not None
                and companion_match.group("produce") == target_produce
                and companion_match.group("stage") == stage
                and companion_match.group("family") == family
                and companion_match.group("selection") == "upgrade_0"
            )
            if len(reward_rows) != 2 or len(companion_rows) != 1:
                blockers.append(
                    _blocker(
                        "CONFLICTING_BUSINESS_COMPANION_EFFECTS",
                        "produce_drink suggestion must contain one same-mode, same-stage family companion",
                        (suggestion_id,)
                        + tuple(str(row["id"]) for row in reward_rows),
                    )
                )
        if blockers:
            return CatalogResult.blocked_result(*blockers)
        actual_effect_id = str(target_rows[0]["id"])
        target_produce_id = _canonical_produce(target_matches[0].group("produce"))
        if source_token == "produce_004" and target_produce_id != "produce-004":
            return CatalogResult.blocked_result(
                _blocker(
                    "CONFLICTING_BUSINESS_EFFECT_ASSOCIATION",
                    "produce-004 suggestion points at a reward effect for a different mode",
                    (suggestion_id, actual_effect_id),
                )
            )

        detail_ids = tuple(sorted(str(row["id"]) for row in detail_rows))
        group_ids = tuple(
            sorted(
                _unique_ids(
                    [str(row["produce_story_group_id"]) for row in detail_rows]
                )
            )
        )
        story_ids = tuple(
            sorted({str(row["produce_story_id"]) for row in detail_rows})
        )
        event_types = _unique_ids([str(row["event_type"]) for row in detail_rows])
        character_types = _unique_ids(
            [str(row["event_character_type"]) for row in detail_rows]
        )
        if len(event_types) != 1 or len(character_types) != 1:
            return CatalogResult.blocked_result(
                _blocker(
                    "CONFLICTING_BUSINESS_EVENT_TYPES",
                    "business detail references disagree on event type",
                    (suggestion_id,) + detail_ids,
                )
            )
        metadata, metadata_blockers = _build_metadata(
            connection,
            target_rows[0],
            evidence_ids=(suggestion_id, actual_effect_id) + detail_ids + group_ids,
            event_stage=stage,
            event_type=event_types[0],
            event_character_type=character_types[0],
            owner_plan=owner_plan,
        )
        blockers.extend(metadata_blockers)
        if metadata is None:
            blockers.append(
                _blocker(
                    "MISSING_REWARD_SET_METADATA",
                    "business reward-set metadata could not be validated",
                    (suggestion_id, actual_effect_id),
                )
            )
        elif (
            metadata.event_stage != stage
            or metadata.produce_id != target_produce_id
            or metadata.reward_family
            != ("drink" if selector == "produce_drink" else family)
        ):
            blockers.append(
                _blocker(
                    "CONFLICTING_BUSINESS_METADATA",
                    "business reward-set metadata conflicts with its actual selector, family, stage, or mode",
                    (suggestion_id, metadata.effect_id),
                )
            )
        expected_resource = (
            "ProduceResourceType_ProduceDrink"
            if selector == "produce_drink"
            else "ProduceResourceType_ProduceCard"
        )
        if metadata is not None and metadata.resource_type != expected_resource:
            blockers.append(
                _blocker(
                    "CONFLICTING_BUSINESS_RESOURCE",
                    "business suggestion resource selector conflicts with reward-set resource type",
                    (suggestion_id, metadata.effect_id),
                )
            )
        if blockers:
            return CatalogResult.blocked_result(*blockers)
        assert metadata is not None
        evidence_ids = _unique_ids(
            (suggestion_id, actual_effect_id) + detail_ids + group_ids
        )
        return CatalogResult.ready_result(
            BusinessRewardAssociation(
                suggestion_id=suggestion_id,
                detail_ids=detail_ids,
                produce_story_group_ids=group_ids,
                produce_story_ids=story_ids,
                source_produce_id=source_produce_id,
                source_mode=source_mode or _PRODUCE_MODES[source_produce_id],
                event_stage=stage,
                event_type=event_types[0],
                event_character_type=character_types[0],
                owner_plan=owner_plan,
                resource_selector=selector,
                attribute=attribute,
                suggestion_family=suggestion_family,
                reward_set=replace(metadata, evidence_ids=evidence_ids),
                evidence_ids=evidence_ids,
            )
        )

    def _lookup_business_detail_connection(
        self, connection: sqlite3.Connection, detail_id: str
    ) -> CatalogResult[tuple[BusinessRewardAssociation, ...]]:
        row = connection.execute(
            "SELECT * FROM step_event_detail WHERE id = ?", (detail_id,)
        ).fetchone()
        if row is None:
            return CatalogResult.blocked_result(
                _blocker("UNKNOWN_DETAIL_ID", f"unknown step event detail: {detail_id}", (detail_id,))
            )
        if row["event_type"] != _EVENT_BUSINESS_TYPE or row["produce_story_id"] != "":
            return CatalogResult.blocked_result(
                _blocker(
                    "NOT_BLANK_STORY_BUSINESS_DETAIL",
                    "detail is not a blank-story business detail",
                    (detail_id,),
                )
            )
        suggestion_ids, blockers = _parse_id_list(
            row["suggestion_ids_json"],
            field="step_event_detail.suggestion_ids_json",
            evidence_ids=(detail_id,),
        )
        if not suggestion_ids:
            blockers += (
                _blocker(
                    "MISSING_BUSINESS_SUGGESTIONS",
                    "business detail has no exact suggestion IDs",
                    (detail_id,),
                ),
            )
        associations: list[BusinessRewardAssociation] = []
        for suggestion_id in suggestion_ids:
            result = self._lookup_business_suggestion_connection(connection, suggestion_id)
            if result.blocked:
                blockers += result.blockers
            elif result.value is not None:
                associations.append(result.value)
                if detail_id not in result.value.detail_ids:
                    blockers += (
                        _blocker(
                            "DETAIL_SUGGESTION_JOIN_MISMATCH",
                            "suggestion lookup did not retain its exact originating detail",
                            (detail_id, suggestion_id),
                        ),
                    )
        if blockers:
            return CatalogResult.blocked_result(*blockers)
        if len(associations) != len(suggestion_ids):
            return CatalogResult.blocked_result(
                _blocker(
                    "INCOMPLETE_BUSINESS_DETAIL_JOIN",
                    "not every exact business suggestion resolved",
                    (detail_id,) + suggestion_ids,
                )
            )
        return CatalogResult.ready_result(tuple(associations))

    def _resolve_business_reward_connection(
        self,
        connection: sqlite3.Connection,
        detail_id: str,
        suggestion_id: str | None,
    ) -> CatalogResult[BusinessRewardAssociation]:
        options_result = self._lookup_business_detail_connection(connection, detail_id)
        if not options_result.ready:
            return CatalogResult(
                options_result.state, None, options_result.blockers
            )
        assert options_result.value is not None
        options = options_result.value
        suggestion_ids = tuple(option.suggestion_id for option in options)
        if suggestion_id is None:
            if len(options) == 1:
                return CatalogResult.ready_result(options[0])
            return CatalogResult.unresolved_result(
                _blocker(
                    "AMBIGUOUS_BUSINESS_SUGGESTION",
                    "business detail has multiple explicit options; choose one exact suggestion ID",
                    (detail_id,) + suggestion_ids,
                )
            )
        matches = tuple(
            option for option in options if option.suggestion_id == suggestion_id
        )
        if len(matches) != 1:
            return CatalogResult.blocked_result(
                _blocker(
                    "SUGGESTION_NOT_IN_DETAIL",
                    "the selected suggestion ID is not an exact option of this business detail",
                    (detail_id, suggestion_id) + suggestion_ids,
                )
            )
        return CatalogResult.ready_result(matches[0])

    def _lookup_item_connection(
        self, connection: sqlite3.Connection, item_id: str
    ) -> CatalogResult[tuple[ItemRewardAssociation, ...]]:
        exact = connection.execute(
            "SELECT * FROM produce_item WHERE id = ?", (item_id,)
        ).fetchone()
        if exact is None:
            return CatalogResult.blocked_result(
                _blocker("UNKNOWN_ITEM_ID", f"unknown produce item: {item_id}", (item_id,))
            )
        return self._lookup_item_rows(connection, (exact,), item_id)

    def _list_item_family_connection(
        self, connection: sqlite3.Connection, item_id_prefix: str
    ) -> CatalogResult[tuple[ItemRewardAssociation, ...]]:
        if "%" in item_id_prefix or "\\" in item_id_prefix:
            return CatalogResult.blocked_result(
                _blocker(
                    "INVALID_ITEM_FAMILY_PREFIX",
                    "item family prefixes cannot contain SQL percent wildcards or escape characters",
                    (item_id_prefix,),
                )
            )
        item_rows = tuple(
            connection.execute(
                "SELECT * FROM produce_item WHERE id LIKE ? ESCAPE '\\' ORDER BY id",
                (item_id_prefix.replace("_", "\\_") + "-%",),
            ).fetchall()
        )
        if not item_rows:
            return CatalogResult.blocked_result(
                _blocker(
                    "UNKNOWN_ITEM_FAMILY",
                    f"unknown produce item family: {item_id_prefix}",
                    (item_id_prefix,),
                )
            )
        return self._lookup_item_rows(connection, item_rows, item_id_prefix)

    def _lookup_item_rows(
        self,
        connection: sqlite3.Connection,
        item_rows: tuple[sqlite3.Row, ...],
        evidence_id: str,
    ) -> CatalogResult[tuple[ItemRewardAssociation, ...]]:

        associations: list[ItemRewardAssociation] = []
        blockers: list[CatalogBlocker] = []
        for item in item_rows:
            association, row_blockers = _item_association(connection, item)
            blockers.extend(row_blockers)
            if association is not None:
                associations.append(association)
        if blockers:
            return CatalogResult.blocked_result(*blockers)
        if not associations:
            return CatalogResult.blocked_result(
                _blocker(
                    "NO_REWARD_SET_ITEM_ASSOCIATION",
                    f"item selector has no validated reward-set owner: {evidence_id}",
                    (evidence_id,),
                )
            )
        return CatalogResult.ready_result(tuple(associations))

    def _lookup_final_connection(
        self, connection: sqlite3.Connection, detail_id: str
    ) -> CatalogResult[RewardSetMetadata]:
        row = connection.execute(
            "SELECT * FROM step_event_detail WHERE id = ?", (detail_id,)
        ).fetchone()
        if row is None:
            return CatalogResult.blocked_result(
                _blocker("UNKNOWN_DETAIL_ID", f"unknown step event detail: {detail_id}", (detail_id,))
            )
        if not detail_id.startswith(_FINAL_DETAIL_PREFIX):
            return CatalogResult.blocked_result(
                _blocker(
                    "NOT_NIA_FINAL_DETAIL",
                    "final unresolved lookup is bounded to produce_002 N.I.A. details",
                    (detail_id,),
                )
            )
        if row["event_type"] != _EVENT_CHARACTER_TYPE or row["event_character_type"] != (
            "ProduceEventCharacterType_AfterAuditionFinal"
        ):
            return CatalogResult.blocked_result(
                _blocker(
                    "WRONG_FINAL_DETAIL_TYPE",
                    "detail is not an N.I.A. final character detail",
                    (detail_id,),
                )
            )
        effect_ids, blockers = _parse_id_list(
            row["produce_effect_ids_json"],
            field="step_event_detail.produce_effect_ids_json",
            evidence_ids=(detail_id,),
        )
        if effect_ids:
            blockers += (
                _blocker(
                    "UNEXPECTED_FINAL_EFFECTS",
                    "final detail has effects, but no authoritative N.I.A. final reward-set association was proven",
                    (detail_id,) + effect_ids,
                ),
            )
        if blockers:
            return CatalogResult.blocked_result(*blockers)
        return CatalogResult.unresolved_result(
            _blocker(
                "FINAL_REWARD_UNRESOLVED",
                "no authoritative N.I.A. final reward-set effect was recovered; candidate membership is not inferred",
                (detail_id,),
            )
        )


def _mode_name(
    connection: sqlite3.Connection,
    produce_id: str,
    evidence_ids: tuple[str, ...],
) -> tuple[str | None, tuple[CatalogBlocker, ...]]:
    row = connection.execute(
        "SELECT id FROM produce_mode WHERE id = ?", (produce_id,)
    ).fetchone()
    if row is None or produce_id not in _PRODUCE_MODES:
        return None, (
            _blocker(
                "UNKNOWN_PRODUCE_ID",
                f"reward effect references an unverified produce mode: {produce_id}",
                evidence_ids + (produce_id,),
            ),
        )
    return _PRODUCE_MODES[produce_id], ()


def _load_effect_rows(
    connection: sqlite3.Connection,
    effect_ids: tuple[str, ...],
    evidence_ids: tuple[str, ...],
) -> tuple[tuple[sqlite3.Row, ...], tuple[CatalogBlocker, ...]]:
    rows: list[sqlite3.Row] = []
    blockers: list[CatalogBlocker] = []
    for effect_id in effect_ids:
        row = connection.execute(
            "SELECT * FROM produce_effect WHERE id = ?", (effect_id,)
        ).fetchone()
        if row is None:
            blockers.append(
                _blocker(
                    "UNKNOWN_EFFECT_ID",
                    f"referenced produce effect does not exist: {effect_id}",
                    evidence_ids + (effect_id,),
                )
            )
        else:
            rows.append(row)
    return tuple(rows), tuple(blockers)


def _business_detail_rows_for_suggestion(
    connection: sqlite3.Connection, suggestion_id: str
) -> tuple[tuple[sqlite3.Row, ...], tuple[CatalogBlocker, ...]]:
    rows = connection.execute(
        """SELECT id, produce_story_id, produce_story_group_id,
                         event_type, event_character_type, suggestion_ids_json
             FROM step_event_detail
            WHERE instr(suggestion_ids_json, ?) > 0
            ORDER BY id""",
        (f'"{suggestion_id}"',),
    ).fetchall()
    matches: list[sqlite3.Row] = []
    blockers: list[CatalogBlocker] = []
    for row in rows:
        ids, row_blockers = _parse_id_list(
            row["suggestion_ids_json"],
            field=f"step_event_detail[{row['id']}].suggestion_ids_json",
            evidence_ids=(suggestion_id, str(row["id"])),
        )
        blockers.extend(row_blockers)
        if suggestion_id in ids:
            matches.append(row)
    return tuple(matches), tuple(blockers)


def _item_association(
    connection: sqlite3.Connection, item: sqlite3.Row
) -> tuple[ItemRewardAssociation | None, tuple[CatalogBlocker, ...]]:
    item_id = str(item["id"])
    blockers: list[CatalogBlocker] = []
    plan_type = item["plan_type"]
    if plan_type not in _PLAN_TYPES:
        blockers.append(
            _blocker(
                "UNKNOWN_OWNER_PLAN",
                f"item {item_id} has unknown owner plan {plan_type!r}",
                (item_id,),
            )
        )
    trigger_id = item["produce_trigger_id"]
    if not isinstance(trigger_id, str) or not trigger_id:
        blockers.append(
            _blocker(
                "MISSING_OWNER_TRIGGER",
                f"item {item_id} has no exact produce trigger owner",
                (item_id,),
            )
        )
    elif trigger_id not in _NIA_OWNER_TRIGGERS:
        blockers.append(
            _blocker(
                "UNKNOWN_OWNER_TRIGGER",
                f"item {item_id} has an unverified N.I.A. owner trigger: {trigger_id}",
                (item_id, trigger_id),
            )
        )
    item_effect_ids, item_effect_id_blockers = _parse_id_list(
        item["produce_item_effect_ids_json"],
        field=f"produce_item[{item_id}].produce_item_effect_ids_json",
        evidence_ids=(item_id,),
    )
    blockers.extend(item_effect_id_blockers)
    item_effect_rows: list[sqlite3.Row] = []
    for item_effect_id in item_effect_ids:
        row = connection.execute(
            "SELECT * FROM produce_item_effect WHERE id = ?", (item_effect_id,)
        ).fetchone()
        if row is None:
            blockers.append(
                _blocker(
                    "UNKNOWN_ITEM_EFFECT_ID",
                    f"item {item_id} references unknown item effect {item_effect_id}",
                    (item_id, item_effect_id),
                )
            )
        else:
            item_effect_rows.append(row)

    reward_candidates: list[tuple[sqlite3.Row, sqlite3.Row]] = []
    for item_effect in item_effect_rows:
        for field in ("effect_turn", "effect_count"):
            value = item_effect[field]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                blockers.append(
                    _blocker(
                        "MALFORMED_ITEM_EFFECT_COUNT",
                        f"item effect {item_effect['id']} has malformed {field}: {value!r}",
                        (item_id, str(item_effect["id"])),
                    )
                )
        produce_effect_id = item_effect["produce_effect_id"]
        if not isinstance(produce_effect_id, str) or not produce_effect_id:
            continue
        produce_effect = connection.execute(
            "SELECT * FROM produce_effect WHERE id = ?", (produce_effect_id,)
        ).fetchone()
        if produce_effect is None:
            blockers.append(
                _blocker(
                    "UNKNOWN_EFFECT_ID",
                    f"item effect {item_effect['id']} references unknown produce effect {produce_effect_id}",
                    (item_id, str(item_effect["id"]), produce_effect_id),
                )
            )
            continue
        if produce_effect["effect_type"] == _REWARD_SET_TYPE:
            if item_effect["effect_type"] != _ITEM_EFFECT_TYPE:
                blockers.append(
                    _blocker(
                        "WRONG_ITEM_EFFECT_TYPE",
                        f"reward-set effect {produce_effect_id} is owned through {item_effect['effect_type']!r}",
                        (item_id, str(item_effect["id"]), produce_effect_id),
                    )
                )
            else:
                reward_candidates.append((item_effect, produce_effect))
        elif item_effect["effect_type"] == _ITEM_EFFECT_TYPE:
            # Non-reward ProduceEffect item effects are legitimate members of
            # the same item chain and are not reward-set associations.
            continue

    if len(reward_candidates) != 1:
        blockers.append(
            _blocker(
                "AMBIGUOUS_ITEM_REWARD_SET",
                f"item {item_id} has {len(reward_candidates)} validated reward-set item effects",
                (item_id,) + tuple(str(item_effect["id"]) for item_effect, _ in reward_candidates),
            )
        )
    if blockers:
        return None, tuple(blockers)
    assert reward_candidates
    item_effect, produce_effect = reward_candidates[0]
    metadata, metadata_blockers = _build_metadata(
        connection,
        produce_effect,
        evidence_ids=(item_id, str(item_effect["id"]), str(produce_effect["id"])),
        owner_plan=plan_type,
    )
    blockers.extend(metadata_blockers)
    if metadata is None:
        blockers.append(
            _blocker(
                "MISSING_REWARD_SET_METADATA",
                f"item {item_id} reward-set metadata could not be validated",
                (item_id, str(produce_effect["id"])),
            )
        )
    if blockers:
        return None, tuple(blockers)
    assert metadata is not None
    evidence_ids = _unique_ids(
        (item_id, str(item_effect["id"]), str(produce_effect["id"]), str(trigger_id))
    )
    return (
        ItemRewardAssociation(
            item_id=item_id,
            item_effect_id=str(item_effect["id"]),
            produce_effect_id=str(produce_effect["id"]),
            owner_plan=str(plan_type),
            trigger_id=str(trigger_id),
            reward_set=replace(metadata, evidence_ids=evidence_ids),
            evidence_ids=evidence_ids,
        ),
        (),
    )


def _build_metadata(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    evidence_ids: tuple[str, ...],
    event_stage: str | None = None,
    event_type: str | None = None,
    event_character_type: str | None = None,
    owner_plan: str | None = None,
) -> tuple[RewardSetMetadata | None, tuple[CatalogBlocker, ...]]:
    effect_id = row["id"]
    blockers: list[CatalogBlocker] = []
    if not isinstance(effect_id, str) or not effect_id:
        blockers.append(_blocker("MALFORMED_EFFECT_ID", "reward effect has no valid ID", evidence_ids))
        return None, tuple(blockers)
    if row["effect_type"] != _REWARD_SET_TYPE:
        blockers.append(
            _blocker(
                "WRONG_EFFECT_TYPE",
                f"effect {effect_id} has type {row['effect_type']!r}, expected {_REWARD_SET_TYPE}",
                evidence_ids + (effect_id,),
            )
        )
    shape = _parse_effect_shape(effect_id)
    if shape is None:
        blockers.append(
            _blocker(
                "UNSUPPORTED_REWARD_SET_ID",
                f"effect ID is outside the bounded N.I.A. reward-set catalog: {effect_id}",
                evidence_ids + (effect_id,),
            )
        )
        return None, tuple(blockers)

    mode, mode_blockers = _mode_name(connection, shape.produce_id, evidence_ids)
    blockers.extend(mode_blockers)
    resource_type = row["resource_type"]
    if resource_type not in _RESOURCE_TYPES:
        blockers.append(
            _blocker(
                "MALFORMED_RESOURCE_TYPE",
                f"reward-set effect {effect_id} has unsupported resource type {resource_type!r}",
                evidence_ids + (effect_id,),
            )
        )
    if resource_type != shape.expected_resource:
        blockers.append(
            _blocker(
                "CONFLICTING_RESOURCE_TYPE",
                f"reward-set ID implies {shape.expected_resource}, row has {resource_type!r}",
                evidence_ids + (effect_id,),
            )
        )
    pick_range_type = row["pick_range_type"]
    selection_method = _SELECTION_TYPES.get(pick_range_type)
    if selection_method is None:
        blockers.append(
            _blocker(
                "MALFORMED_SELECTION_METHOD",
                f"reward-set effect {effect_id} has unsupported pick range {pick_range_type!r}",
                evidence_ids + (effect_id,),
            )
        )
    elif selection_method != shape.expected_selection:
        blockers.append(
            _blocker(
                "CONFLICTING_SELECTION_METHOD",
                f"reward-set ID implies {shape.expected_selection}, row has {selection_method}",
                evidence_ids + (effect_id,),
            )
        )

    int_values: dict[str, int] = {}
    for field in (
        "effect_value_min",
        "effect_value_max",
        "pick_count_min",
        "pick_count_max",
    ):
        value = row[field]
        if isinstance(value, bool) or not isinstance(value, int):
            blockers.append(
                _blocker(
                    "MALFORMED_COUNT",
                    f"reward-set field {field} is not an integer: {value!r}",
                    evidence_ids + (effect_id,),
                )
            )
        else:
            int_values[field] = value
            if value < 0:
                blockers.append(
                    _blocker(
                        "MALFORMED_COUNT",
                        f"reward-set field {field} is negative: {value}",
                        evidence_ids + (effect_id,),
                    )
                )
    if (
        "effect_value_min" in int_values
        and "effect_value_max" in int_values
        and int_values["effect_value_min"] > int_values["effect_value_max"]
    ):
        blockers.append(
            _blocker(
                "MALFORMED_RANGE",
                "effect_value_min is greater than effect_value_max",
                evidence_ids + (effect_id,),
            )
        )
    if (
        "pick_count_min" in int_values
        and "pick_count_max" in int_values
        and (
            int_values["pick_count_min"] < 1
            or int_values["pick_count_min"] > int_values["pick_count_max"]
        )
    ):
        blockers.append(
            _blocker(
                "MALFORMED_PICK_COUNT",
                "pick counts must be positive and min must not exceed max",
                evidence_ids + (effect_id,),
            )
        )
    if (
        int_values.get("pick_count_min") != shape.expected_count_min
        or int_values.get("pick_count_max") != shape.expected_count_max
    ):
        blockers.append(
            _blocker(
                "CONFLICTING_PICK_COUNT",
                f"reward-set ID implies counts {shape.expected_count_min}/{shape.expected_count_max}",
                evidence_ids + (effect_id,),
            )
        )

    is_research = row["is_research"]
    if type(is_research) is not int or is_research not in (0, 1):
        blockers.append(
            _blocker(
                "MALFORMED_RESEARCH_FLAG",
                f"is_research must be 0 or 1, got {is_research!r}",
                evidence_ids + (effect_id,),
            )
        )
        normalized_research = False
    else:
        normalized_research = bool(is_research)
        if normalized_research != shape.expected_research:
            blockers.append(
                _blocker(
                    "CONFLICTING_RESEARCH_FLAG",
                    f"reward-set ID research suffix implies {shape.expected_research}, row has {normalized_research}",
                    evidence_ids + (effect_id,),
                )
            )

    if owner_plan is not None and owner_plan not in _PLAN_TYPES:
        blockers.append(
            _blocker(
                "UNKNOWN_OWNER_PLAN",
                f"association has unknown owner plan {owner_plan!r}",
                evidence_ids + (effect_id,),
            )
        )

    candidate, candidate_blockers = _candidate_membership(connection, row, evidence_ids)
    blockers.extend(candidate_blockers)
    if candidate is None:
        candidate = CandidateMembership("unresolved", None, "none", evidence_ids)

    if blockers:
        return None, tuple(blockers)
    assert selection_method is not None
    assert mode is not None
    return (
        RewardSetMetadata(
            effect_id=effect_id,
            effect_type=str(row["effect_type"]),
            produce_id=shape.produce_id,
            mode=mode,
            event_stage=event_stage or shape.event_stage,
            event_type=event_type,
            event_character_type=event_character_type,
            resource_type=str(resource_type),
            selection_method=selection_method,
            pick_range_type=str(pick_range_type),
            effect_value_min=int_values.get("effect_value_min", 0),
            effect_value_max=int_values.get("effect_value_max", 0),
            pick_count_min=int_values.get("pick_count_min", 0),
            pick_count_max=int_values.get("pick_count_max", 0),
            is_research=normalized_research,
            owner_plan=owner_plan,
            reward_family=shape.reward_family,
            card_search_id=(str(row["card_search_id"]) if row["card_search_id"] else None),
            candidate_membership=candidate,
            evidence_ids=_unique_ids(evidence_ids + (effect_id,)),
        ),
        (),
    )


def _candidate_membership(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    evidence_ids: tuple[str, ...],
) -> tuple[CandidateMembership | None, tuple[CatalogBlocker, ...]]:
    effect_id = str(row["id"])
    blockers: list[CatalogBlocker] = []
    try:
        rewards = json.loads(row["rewards_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        return None, (
            _blocker(
                "MALFORMED_REWARD_CANDIDATES",
                f"produceRewards is not valid JSON: {exc}",
                evidence_ids + (effect_id,),
            ),
        )
    if not isinstance(rewards, list):
        return None, (
            _blocker(
                "MALFORMED_REWARD_CANDIDATES",
                "produceRewards must be a JSON list",
                evidence_ids + (effect_id,),
            ),
        )
    card_search_id = row["card_search_id"]
    if card_search_id is None:
        card_search_id = ""
    if not isinstance(card_search_id, str):
        blockers.append(
            _blocker(
                "MALFORMED_CARD_SEARCH_ID",
                f"produceCardSearchId is not a string: {card_search_id!r}",
                evidence_ids + (effect_id,),
            )
        )
        card_search_id = ""

    if card_search_id:
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'produce_card_search'"
        ).fetchone()
        if table is None:
            blockers.append(
                _blocker(
                    "CARD_SEARCH_SCHEMA_MISSING",
                    "produceCardSearchId is present but produce_card_search is unavailable",
                    evidence_ids + (effect_id, card_search_id),
                )
            )
        else:
            blockers.extend(_schema_blockers(connection, ("produce_card_search",)))
            if blockers:
                return None, tuple(blockers)
            search = connection.execute(
                "SELECT * FROM produce_card_search WHERE id = ?", (card_search_id,)
            ).fetchone()
            if search is None:
                blockers.append(
                    _blocker(
                        "UNKNOWN_CARD_SEARCH_ID",
                        f"unknown produce card search: {card_search_id}",
                        evidence_ids + (effect_id, card_search_id),
                    )
                )
            else:
                text_array_fields = {
                    "card_rarities_json",
                    "produce_card_ids_json",
                    "card_categories_json",
                    "effect_group_ids_json",
                }
                for field in (
                    "card_rarities_json",
                    "produce_card_ids_json",
                    "upgrade_counts_json",
                    "card_categories_json",
                    "effect_group_ids_json",
                ):
                    values, field_blockers = _parse_json_array(
                        search[field],
                        field=f"produce_card_search.{field}",
                        evidence_ids=(effect_id, card_search_id),
                    )
                    blockers.extend(field_blockers)
                    if field_blockers:
                        continue
                    if field in text_array_fields:
                        valid_values = all(
                            isinstance(value, str) and value for value in values
                        )
                        if not valid_values:
                            blockers.append(
                                _blocker(
                                    "MALFORMED_CARD_SEARCH",
                                    f"{field} must contain only non-empty strings",
                                    (effect_id, card_search_id),
                                )
                            )
                        elif len(values) != len(set(values)):
                            blockers.append(
                                _blocker(
                                    "DUPLICATE_CARD_SEARCH_VALUE",
                                    f"{field} must not contain duplicate values",
                                    (effect_id, card_search_id),
                                )
                            )
                    elif field == "upgrade_counts_json":
                        valid_values = all(
                            type(value) is int and value >= 0 for value in values
                        )
                        if not valid_values:
                            blockers.append(
                                _blocker(
                                    "MALFORMED_CARD_SEARCH",
                                    "upgrade_counts_json must contain non-negative integers",
                                    (effect_id, card_search_id),
                                )
                            )
                        elif len(values) != len(set(values)):
                            blockers.append(
                                _blocker(
                                    "DUPLICATE_CARD_SEARCH_VALUE",
                                    "upgrade_counts_json must not contain duplicate values",
                                    (effect_id, card_search_id),
                                )
                            )

    if rewards and card_search_id:
        blockers.append(
            _blocker(
                "CONFLICTING_CANDIDATE_SOURCES",
                "effect contains both produceRewards and produceCardSearchId; membership is not safely singular",
                evidence_ids + (effect_id, card_search_id),
            )
        )

    members: list[str] = []
    for index, reward in enumerate(rewards):
        if not isinstance(reward, dict):
            blockers.append(
                _blocker(
                    "MALFORMED_REWARD_CANDIDATES",
                    f"produceRewards[{index}] must be an object",
                    evidence_ids + (effect_id,),
                )
            )
            continue
        member = reward.get("resourceId")
        if not isinstance(member, str) or not member:
            blockers.append(
                _blocker(
                    "MALFORMED_REWARD_CANDIDATES",
                    f"produceRewards[{index}].resourceId must be a non-empty string",
                    evidence_ids + (effect_id,),
                )
            )
            continue
        if member in members:
            blockers.append(
                _blocker(
                    "DUPLICATE_REWARD_CANDIDATE",
                    f"produceRewards repeats candidate {member}",
                    evidence_ids + (effect_id, member),
                )
            )
        members.append(member)
    if blockers:
        return None, tuple(blockers)
    if members:
        return (
            CandidateMembership(
                state="explicit",
                members=tuple(members),
                source="produceRewards",
                evidence_ids=_unique_ids(evidence_ids + (effect_id,)),
            ),
            (),
        )
    return (
        CandidateMembership(
            state="unresolved",
            members=None,
            source="cardSearch" if card_search_id else "none",
            evidence_ids=_unique_ids(evidence_ids + (effect_id,)),
        ),
        (),
    )


def lookup_reward_set(
    effect_id: str, database: Path = DEFAULT_DATABASE
) -> CatalogResult[RewardSetMetadata]:
    return NiaRewardCatalog(database).lookup_reward_set(effect_id)


def lookup_audition_detail(
    detail_id: str, database: Path = DEFAULT_DATABASE
) -> CatalogResult[AuditionRewardAssociation]:
    return NiaRewardCatalog(database).lookup_audition_detail(detail_id)


def lookup_business_suggestion(
    suggestion_id: str, database: Path = DEFAULT_DATABASE
) -> CatalogResult[BusinessRewardAssociation]:
    return NiaRewardCatalog(database).lookup_business_suggestion(suggestion_id)


def lookup_business_detail(
    detail_id: str, database: Path = DEFAULT_DATABASE
) -> CatalogResult[tuple[BusinessRewardAssociation, ...]]:
    return NiaRewardCatalog(database).lookup_business_detail(detail_id)


def lookup_business_detail_options(
    detail_id: str, database: Path = DEFAULT_DATABASE
) -> CatalogResult[tuple[BusinessRewardAssociation, ...]]:
    return NiaRewardCatalog(database).lookup_business_detail_options(detail_id)


def resolve_business_reward(
    detail_id: str,
    suggestion_id: str | None = None,
    database: Path = DEFAULT_DATABASE,
) -> CatalogResult[BusinessRewardAssociation]:
    return NiaRewardCatalog(database).resolve_business_reward(
        detail_id, suggestion_id
    )


def lookup_item_reward_sets(
    item_id: str, database: Path = DEFAULT_DATABASE
) -> CatalogResult[tuple[ItemRewardAssociation, ...]]:
    return NiaRewardCatalog(database).lookup_item_reward_sets(item_id)


def list_item_reward_family(
    item_id_prefix: str, database: Path = DEFAULT_DATABASE
) -> CatalogResult[tuple[ItemRewardAssociation, ...]]:
    return NiaRewardCatalog(database).list_item_reward_family(item_id_prefix)


def lookup_item_reward_association(
    item_id: str, database: Path = DEFAULT_DATABASE
) -> CatalogResult[ItemRewardAssociation]:
    return NiaRewardCatalog(database).lookup_item_reward_association(item_id)


def lookup_final_reward(
    detail_id: str, database: Path = DEFAULT_DATABASE
) -> CatalogResult[RewardSetMetadata]:
    return NiaRewardCatalog(database).lookup_final_reward(detail_id)


__all__ = [
    "AuditionRewardAssociation",
    "BusinessRewardAssociation",
    "CandidateMembership",
    "CatalogBlocker",
    "CatalogResult",
    "DEFAULT_DATABASE",
    "ItemRewardAssociation",
    "NiaRewardCatalog",
    "NiaRewardCatalogError",
    "RewardSetMetadata",
    "lookup_audition_detail",
    "lookup_business_detail",
    "lookup_business_detail_options",
    "lookup_business_suggestion",
    "lookup_final_reward",
    "lookup_item_reward_association",
    "lookup_item_reward_sets",
    "lookup_reward_set",
    "list_item_reward_family",
    "resolve_business_reward",
]
