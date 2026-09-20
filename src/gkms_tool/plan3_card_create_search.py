"""Android v3.2.3-native ``ExamCardCreateSearch`` execution.

The executor uses ``ProduceCardPool`` (despite the legacy
``ProduceCardRandomPoolId`` property name), expands each ratio into uniform
tickets, filters those tickets by the current plan, and uses the shared exam
RNG for both ordering and random picking.  GUIDs remain explicit caller-owned
tokens because ``ExamCardData.Guid`` is generated lazily with ``Guid.NewGuid``
and is independent of the exam RNG.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from functools import lru_cache
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Literal, TypeAlias

from .master_db import DEFAULT_DATABASE
from .exam_native_rng import advance_state, next_int32, next_range
from .plan3_native_state import Plan3NativeCard, Plan3NativeState


CARD_CREATE_SEARCH_EFFECT_TYPE = "ProduceExamEffectType_ExamCardCreateSearch"
EFFECT_CARD_CREATE_SEARCH = CARD_CREATE_SEARCH_EFFECT_TYPE
ANDROID_VERSION = "Android v3.2.3"
MASTER_SNAPSHOT = "_research/gakumasu-diff local Master snapshot"

MASTER_DB_SOURCE = "var/master.sqlite3"
MASTER_EFFECT_SOURCE = "_research/gakumasu-diff/ProduceExamEffect.yaml"
CARD_SEARCH_SOURCE = "_research/gakumasu-diff/ProduceCardSearch.yaml"
CARD_POOL_SOURCE = "_research/gakumasu-diff/ProduceCardPool.yaml"
CARD_RANDOM_POOL_SOURCE = "_research/gakumasu-diff/ProduceCardRandomPool.yaml"
CARD_SOURCE = "_research/gakumasu-diff/ProduceCard.yaml"
EXECUTOR_MAPPING_SOURCE = (
    "_research/android/game-v3.2.3/native-analysis/create-executor-mapping.json"
)
EXECUTOR_CS_SOURCE = (
    "_research/android/game-v3.2.3/cpp2il-plugin/DiffableCs/"
    "Assembly-CSharp/Campus/Ingame/Exam/CardCreateSearchEffectExecutor.cs"
)
EXECUTOR_METADATA_SOURCE = (
    "_research/il2cpp/"
    "B6120AF66E75E72757E3D4086E2C48D0E7D31DA9F3A3DFBCDC7B3ED5BFFD61DA/"
    "targeted-metadata-index.json"
)
NATIVE_AUDIT_SOURCE = "var/coverage/plan3_card_create_search_native_audit.json"

CardCreateSearchDestination: TypeAlias = Literal["hand", "deck_random"]
PoolSource: TypeAlias = Literal["ProduceCardPool", "ProduceCardRandomPool"]

_DESTINATION_BY_MOVE_POSITION: dict[str, CardCreateSearchDestination] = {
    "ProduceCardMovePositionType_Hand": "hand",
    "ProduceCardMovePositionType_DeckRandom": "deck_random",
}
_MOVE_POSITION_BY_DESTINATION = {
    destination: move_position
    for move_position, destination in _DESTINATION_BY_MOVE_POSITION.items()
}
_MISSING = object()


class CardCreateSearchError(ValueError):
    """Base error with a stable machine-readable code."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}:{detail}")


class CardCreateSearchResolutionError(CardCreateSearchError):
    """The static Master shape cannot be resolved safely."""


class CardCreateSearchInputError(CardCreateSearchError):
    """An explicitly supplied future execution input is malformed."""


def _text(value: object, field: str, *, error: type[CardCreateSearchError]) -> str:
    if not isinstance(value, str) or not value.strip():
        raise error("invalid-text", field)
    return value


def _allow_empty_text(
    value: object, field: str, *, error: type[CardCreateSearchError]
) -> str:
    if not isinstance(value, str):
        raise error("invalid-text", field)
    return value


def _nonnegative_int(
    value: object, field: str, *, error: type[CardCreateSearchError]
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise error("invalid-nonnegative-int", field)
    return value


def _text_tuple(
    values: Sequence[object], field: str, *, error: type[CardCreateSearchError]
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes, bytearray)):
        raise error("invalid-text-sequence", field)
    result = tuple(values)
    if any(not isinstance(value, str) or not value.strip() for value in result):
        raise error("invalid-text-sequence", field)
    return result


def _empty_or_text_tuple(
    values: Sequence[object], field: str, *, error: type[CardCreateSearchError]
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes, bytearray)):
        raise error("invalid-text-sequence", field)
    result = tuple(values)
    if any(not isinstance(value, str) for value in result):
        raise error("invalid-text-sequence", field)
    return result


def _int_tuple(
    values: Sequence[object], field: str, *, error: type[CardCreateSearchError]
) -> tuple[int, ...]:
    if isinstance(values, (str, bytes, bytearray)):
        raise error("invalid-int-sequence", field)
    result = tuple(values)
    if any(isinstance(value, bool) or not isinstance(value, int) for value in result):
        raise error("invalid-int-sequence", field)
    return result


@dataclass(frozen=True, slots=True, order=True)
class CardCreateSearchEvidenceLink:
    """One ordered source/link edge retained in the static catalog."""

    source: str
    link: str
    slot: int
    relation: str = ""

    def __post_init__(self) -> None:
        _text(self.source, "source", error=CardCreateSearchInputError)
        _text(self.link, "link", error=CardCreateSearchInputError)
        _nonnegative_int(self.slot, "slot", error=CardCreateSearchInputError)
        _allow_empty_text(self.relation, "relation", error=CardCreateSearchInputError)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True, order=True)
class CardCreateSearchPoolCandidate:
    """One candidate exactly as ordered by one pool source."""

    source: str
    link: str
    slot: int
    upgrade_count: int
    ratio: int
    plan_type: str = "ProducePlanType_Unknown"

    def __post_init__(self) -> None:
        _text(self.source, "source", error=CardCreateSearchInputError)
        _text(self.link, "link", error=CardCreateSearchInputError)
        _nonnegative_int(self.slot, "slot", error=CardCreateSearchInputError)
        _nonnegative_int(
            self.upgrade_count, "upgrade_count", error=CardCreateSearchInputError
        )
        if (
            isinstance(self.ratio, bool)
            or not isinstance(self.ratio, int)
            or self.ratio <= 0
        ):
            raise CardCreateSearchInputError("invalid-pool-ratio")
        _text(self.plan_type, "plan_type", error=CardCreateSearchInputError)

    @property
    def card_id(self) -> str:
        return self.link

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CardCreateSearchPool:
    """Both linked pool representations, retaining each source's slot order."""

    pool_id: str
    produce_card_pool: tuple[CardCreateSearchPoolCandidate, ...]
    produce_card_random_pool: tuple[CardCreateSearchPoolCandidate, ...]
    source_links: tuple[CardCreateSearchEvidenceLink, ...] = ()

    def __post_init__(self) -> None:
        _text(self.pool_id, "pool_id", error=CardCreateSearchInputError)
        produce = tuple(self.produce_card_pool)
        random = tuple(self.produce_card_random_pool)
        links = tuple(self.source_links)
        if not all(
            isinstance(value, CardCreateSearchPoolCandidate) for value in (*produce, *random)
        ):
            raise CardCreateSearchInputError("invalid-pool-candidate")
        if not all(
            isinstance(value, CardCreateSearchEvidenceLink) for value in links
        ):
            raise CardCreateSearchInputError("invalid-pool-source-link")
        if tuple(value.slot for value in produce) != tuple(range(len(produce))):
            raise CardCreateSearchInputError("noncontiguous-produce-pool-slots")
        if tuple(value.slot for value in random) != tuple(range(len(random))):
            raise CardCreateSearchInputError("noncontiguous-random-pool-slots")
        object.__setattr__(self, "produce_card_pool", produce)
        object.__setattr__(self, "produce_card_random_pool", random)
        object.__setattr__(self, "source_links", links)

    @property
    def produce_card_pool_ids(self) -> tuple[str, ...]:
        return tuple(value.card_id for value in self.produce_card_pool)

    @property
    def produce_card_random_pool_ids(self) -> tuple[str, ...]:
        return tuple(value.card_id for value in self.produce_card_random_pool)

    @property
    def candidate_count_agrees(self) -> bool:
        return len(self.produce_card_pool) == len(self.produce_card_random_pool)

    @property
    def candidate_order_agrees(self) -> bool:
        return self.produce_card_pool_ids == self.produce_card_random_pool_ids

    @property
    def candidate_identity_agrees(self) -> bool:
        return tuple(
            (value.card_id, value.upgrade_count) for value in self.produce_card_pool
        ) == tuple(
            (value.card_id, value.upgrade_count)
            for value in self.produce_card_random_pool
        )

    @property
    def source_agreement(self) -> bool:
        return (
            self.candidate_count_agrees
            and self.candidate_order_agrees
            and self.candidate_identity_agrees
        )

    @property
    def authoritative_source(self) -> PoolSource:
        """Runtime Android source proven at ``ExamMasterDataContainer``."""

        return "ProduceCardPool"

    @property
    def authoritative_candidates(self) -> tuple[CardCreateSearchPoolCandidate, ...]:
        return self.produce_card_pool

    @property
    def expanded_ticket_count(self) -> int:
        return sum(value.ratio for value in self.authoritative_candidates)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CardCreateSearchCondition:
    """The exact linked ``ProduceCardSearch`` filter shape."""

    search_id: str
    card_rarities: tuple[str, ...]
    produce_card_ids: tuple[str, ...]
    upgrade_counts: tuple[int, ...]
    plan_type: str
    card_categories: tuple[str, ...]
    card_status_type: str
    order_type: str
    card_position_type: str
    card_search_tag: str
    produce_card_random_pool_id: str
    limit_count: int
    stamina_min_max_type: str
    stamina_min: int
    stamina_max: int
    exam_effect_type: str
    effect_group_ids: tuple[str, ...]
    is_self: bool
    produce_card_pool_id: str
    cost_type: str
    is_customized: bool
    source_links: tuple[CardCreateSearchEvidenceLink, ...] = ()

    def __post_init__(self) -> None:
        _text(self.search_id, "search_id", error=CardCreateSearchInputError)
        for field in (
            "plan_type",
            "card_status_type",
            "order_type",
            "card_position_type",
            "stamina_min_max_type",
            "exam_effect_type",
            "cost_type",
        ):
            _allow_empty_text(
                getattr(self, field), field, error=CardCreateSearchInputError
            )
        for field in (
            "card_rarities",
            "produce_card_ids",
            "card_categories",
            "effect_group_ids",
        ):
            value = _empty_or_text_tuple(
                getattr(self, field), field, error=CardCreateSearchInputError
            )
            object.__setattr__(self, field, value)
        upgrade_counts = _int_tuple(
            self.upgrade_counts, "upgrade_counts", error=CardCreateSearchInputError
        )
        if any(value < 0 for value in upgrade_counts):
            raise CardCreateSearchInputError("invalid-upgrade-count-filter")
        object.__setattr__(self, "upgrade_counts", upgrade_counts)
        _allow_empty_text(
            self.card_search_tag, "card_search_tag", error=CardCreateSearchInputError
        )
        _text(
            self.produce_card_random_pool_id,
            "produce_card_random_pool_id",
            error=CardCreateSearchInputError,
        ) if self.produce_card_random_pool_id else None
        _text(
            self.produce_card_pool_id,
            "produce_card_pool_id",
            error=CardCreateSearchInputError,
        ) if self.produce_card_pool_id else None
        for field in ("limit_count", "stamina_min", "stamina_max"):
            _nonnegative_int(
                getattr(self, field), field, error=CardCreateSearchInputError
            )
        if not isinstance(self.is_self, bool) or not isinstance(
            self.is_customized, bool
        ):
            raise CardCreateSearchInputError("invalid-search-boolean")
        links = tuple(self.source_links)
        if not all(
            isinstance(value, CardCreateSearchEvidenceLink) for value in links
        ):
            raise CardCreateSearchInputError("invalid-search-source-link")
        object.__setattr__(self, "source_links", links)

    @property
    def random_pool_id(self) -> str:
        return self.produce_card_random_pool_id

    @property
    def pool_id(self) -> str:
        return self.produce_card_pool_id

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CardCreateSearchEffectContract:
    """One complete static ``ExamCardCreateSearch`` effect contract."""

    effect_id: str
    source_slot: int
    effect_type: str
    effect_value1: int
    effect_value2: int
    effect_count: int
    effect_turn: int
    target_produce_card_id: str
    target_upgrade_count: int
    target_exam_effect_type: str
    search_id: str
    move_position_type: str
    destination: CardCreateSearchDestination
    pick_range_type: str
    pick_count_reference_search_id: str
    pick_count_type: str
    pick_count_min: int
    pick_count_max: int
    search_id2: str
    pick_range_type2: str
    pick_count_reference_search_id2: str
    pick_count_type2: str
    pick_count_min2: int
    pick_count_max2: int
    chain_effect_id: str
    chain_effect_ids: tuple[str, ...]
    produce_exam_status_enchant_id: str
    produce_card_status_enchant_id: str
    produce_card_grow_effect_ids: tuple[str, ...]
    effect_group_ids: tuple[str, ...]
    search: CardCreateSearchCondition
    pool: CardCreateSearchPool
    source_links: tuple[CardCreateSearchEvidenceLink, ...] = ()

    def __post_init__(self) -> None:
        _text(self.effect_id, "effect_id", error=CardCreateSearchInputError)
        _nonnegative_int(self.source_slot, "source_slot", error=CardCreateSearchInputError)
        if self.effect_type != CARD_CREATE_SEARCH_EFFECT_TYPE:
            raise CardCreateSearchInputError("unexpected-effect-type", self.effect_type)
        for field in (
            "effect_value1",
            "effect_value2",
            "effect_count",
            "effect_turn",
            "target_upgrade_count",
            "pick_count_min",
            "pick_count_max",
            "pick_count_min2",
            "pick_count_max2",
        ):
            _nonnegative_int(
                getattr(self, field), field, error=CardCreateSearchInputError
            )
        for field in (
            "target_produce_card_id",
            "target_exam_effect_type",
            "search_id2",
            "pick_count_reference_search_id2",
            "chain_effect_id",
            "produce_exam_status_enchant_id",
            "produce_card_status_enchant_id",
        ):
            _allow_empty_text(
                getattr(self, field), field, error=CardCreateSearchInputError
            )
        for field in (
            "search_id",
            "move_position_type",
            "pick_range_type",
            "pick_count_reference_search_id",
            "pick_count_type",
            "pick_range_type2",
            "pick_count_type2",
        ):
            _allow_empty_text(
                getattr(self, field), field, error=CardCreateSearchInputError
            )
        if self.destination not in _MOVE_POSITION_BY_DESTINATION:
            raise CardCreateSearchInputError("invalid-destination", str(self.destination))
        if self.pick_count_min > self.pick_count_max:
            raise CardCreateSearchInputError("invalid-count-range")
        if self.pick_count_min2 > self.pick_count_max2:
            raise CardCreateSearchInputError("invalid-second-count-range")
        for field in (
            "chain_effect_ids",
            "produce_card_grow_effect_ids",
            "effect_group_ids",
        ):
            value = _empty_or_text_tuple(
                getattr(self, field), field, error=CardCreateSearchInputError
            )
            object.__setattr__(self, field, value)
        if not isinstance(self.search, CardCreateSearchCondition):
            raise CardCreateSearchInputError("invalid-search-condition")
        if not isinstance(self.pool, CardCreateSearchPool):
            raise CardCreateSearchInputError("invalid-pool")
        if self.search.search_id != self.search_id:
            raise CardCreateSearchInputError("search-link-mismatch")
        if self.search.produce_card_random_pool_id != self.pool.pool_id:
            raise CardCreateSearchInputError("random-pool-link-mismatch")
        if self.search.produce_card_pool_id != self.pool.pool_id:
            raise CardCreateSearchInputError("produce-pool-link-mismatch")
        links = tuple(self.source_links)
        if not all(
            isinstance(value, CardCreateSearchEvidenceLink) for value in links
        ):
            raise CardCreateSearchInputError("invalid-effect-source-link")
        object.__setattr__(self, "source_links", links)

    @property
    def count_range(self) -> tuple[int, int]:
        return self.pick_count_min, self.pick_count_max

    @property
    def fixed_count(self) -> int | None:
        if (
            self.pick_count_type == "ProducePickCountType_Unknown"
            and not self.pick_count_reference_search_id
            and self.pick_count_min == self.pick_count_max
        ):
            return self.pick_count_min
        return None

    @property
    def static_catalog_only(self) -> bool:
        return False

    @property
    def unresolved_fields(self) -> tuple[str, ...]:
        return ()

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    def to_json(self, **kwargs: Any) -> str:
        return json.dumps(self.to_dict(), **kwargs)


@dataclass(frozen=True, slots=True)
class CardCreateSearchAffectedCardVariant:
    """A card-version-to-effect link retained in play-effect slot order."""

    card_id: str
    upgrade: int
    name: str
    plan_type: str
    category: str
    effect_id: str
    effect_slot: int
    source_links: tuple[CardCreateSearchEvidenceLink, ...] = ()

    def __post_init__(self) -> None:
        for field in (
            "card_id",
            "name",
            "plan_type",
            "category",
            "effect_id",
        ):
            _text(getattr(self, field), field, error=CardCreateSearchInputError)
        _nonnegative_int(self.upgrade, "upgrade", error=CardCreateSearchInputError)
        _nonnegative_int(self.effect_slot, "effect_slot", error=CardCreateSearchInputError)
        links = tuple(self.source_links)
        if not all(
            isinstance(value, CardCreateSearchEvidenceLink) for value in links
        ):
            raise CardCreateSearchInputError("invalid-card-source-link")
        object.__setattr__(self, "source_links", links)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CardCreateSearchNativeEvidence:
    """Bound Android executor/helper bodies used by the executable model."""

    android_version: str
    enum_name: str
    enum_value: int
    executor_type: str
    factory_branch_va: str
    type_global_va: str
    usage_cell_va: str
    constructor_va: str
    constructor_metadata_method_index: int
    constructor_token: str
    executor_method_index: int
    executor_method_token: str
    execute_effect_va: str
    get_search_card_list_va: str
    get_pick_count_min_max_va: str
    get_random_pool_pick_card_list_va: str
    pick_card_position_list_impl_va: str
    master_pool_resolver_va: str
    add_card_va: str
    guid_getter_va: str
    executor_body_proven: bool
    source_links: tuple[CardCreateSearchEvidenceLink, ...]

    def __post_init__(self) -> None:
        for field in (
            "android_version",
            "enum_name",
            "executor_type",
            "factory_branch_va",
            "type_global_va",
            "usage_cell_va",
            "constructor_va",
            "constructor_token",
            "executor_method_token",
            "execute_effect_va",
            "get_search_card_list_va",
            "get_pick_count_min_max_va",
            "get_random_pool_pick_card_list_va",
            "pick_card_position_list_impl_va",
            "master_pool_resolver_va",
            "add_card_va",
            "guid_getter_va",
        ):
            _text(getattr(self, field), field, error=CardCreateSearchInputError)
        _nonnegative_int(
            self.enum_value, "enum_value", error=CardCreateSearchInputError
        )
        for field in (
            "constructor_metadata_method_index",
            "executor_method_index",
        ):
            _nonnegative_int(
                getattr(self, field), field, error=CardCreateSearchInputError
            )
        if not isinstance(self.executor_body_proven, bool):
            raise CardCreateSearchInputError("invalid-executor-body-proof")
        links = tuple(self.source_links)
        if not all(
            isinstance(value, CardCreateSearchEvidenceLink) for value in links
        ):
            raise CardCreateSearchInputError("invalid-native-source-link")
        object.__setattr__(self, "source_links", links)

    @property
    def executable(self) -> bool:
        return self.executor_body_proven

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CardCreateSearchCoverage:
    """Explicit coverage split for this effect family."""

    effect_rows_cataloged: int
    affected_card_versions_cataloged: int
    executable_effect_rows: int
    executable_card_versions: int
    status: Literal["executable"] = "executable"

    def __post_init__(self) -> None:
        for field in (
            "effect_rows_cataloged",
            "affected_card_versions_cataloged",
            "executable_effect_rows",
            "executable_card_versions",
        ):
            _nonnegative_int(
                getattr(self, field), field, error=CardCreateSearchInputError
            )
        if self.status != "executable":
            raise CardCreateSearchInputError("invalid-coverage-status")

    @property
    def executable(self) -> bool:
        return True

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CardCreateSearchCatalog:
    """Immutable complete static catalog for the current evidence snapshot."""

    master_snapshot: str
    effects: tuple[CardCreateSearchEffectContract, ...]
    searches: tuple[CardCreateSearchCondition, ...]
    pools: tuple[CardCreateSearchPool, ...]
    affected_card_versions: tuple[CardCreateSearchAffectedCardVariant, ...]
    native: CardCreateSearchNativeEvidence
    proven_order: tuple[str, ...]
    unproven_order: tuple[str, ...]
    coverage: CardCreateSearchCoverage

    def __post_init__(self) -> None:
        _text(self.master_snapshot, "master_snapshot", error=CardCreateSearchInputError)
        effects = tuple(self.effects)
        searches = tuple(self.searches)
        pools = tuple(self.pools)
        cards = tuple(self.affected_card_versions)
        proven = _text_tuple(
            self.proven_order, "proven_order", error=CardCreateSearchInputError
        )
        unproven = _text_tuple(
            self.unproven_order, "unproven_order", error=CardCreateSearchInputError
        )
        if not all(
            isinstance(value, CardCreateSearchEffectContract) for value in effects
        ):
            raise CardCreateSearchInputError("invalid-catalog-effect")
        if not all(
            isinstance(value, CardCreateSearchCondition) for value in searches
        ):
            raise CardCreateSearchInputError("invalid-catalog-search")
        if not all(isinstance(value, CardCreateSearchPool) for value in pools):
            raise CardCreateSearchInputError("invalid-catalog-pool")
        if not all(
            isinstance(value, CardCreateSearchAffectedCardVariant) for value in cards
        ):
            raise CardCreateSearchInputError("invalid-catalog-card")
        if not isinstance(self.native, CardCreateSearchNativeEvidence):
            raise CardCreateSearchInputError("invalid-catalog-native-evidence")
        if not isinstance(self.coverage, CardCreateSearchCoverage):
            raise CardCreateSearchInputError("invalid-catalog-coverage")
        if len({value.effect_id for value in effects}) != len(effects):
            raise CardCreateSearchInputError("duplicate-catalog-effect")
        if len({value.search_id for value in searches}) != len(searches):
            raise CardCreateSearchInputError("duplicate-catalog-search")
        if len({value.pool_id for value in pools}) != len(pools):
            raise CardCreateSearchInputError("duplicate-catalog-pool")
        object.__setattr__(self, "effects", effects)
        object.__setattr__(self, "searches", searches)
        object.__setattr__(self, "pools", pools)
        object.__setattr__(self, "affected_card_versions", cards)
        object.__setattr__(self, "proven_order", proven)
        object.__setattr__(self, "unproven_order", unproven)

    @property
    def effect_ids(self) -> tuple[str, ...]:
        return tuple(value.effect_id for value in self.effects)

    @property
    def search_ids(self) -> tuple[str, ...]:
        return tuple(value.search_id for value in self.searches)

    @property
    def pool_ids(self) -> tuple[str, ...]:
        return tuple(value.pool_id for value in self.pools)

    def effect_by_id(self, effect_id: str) -> CardCreateSearchEffectContract | None:
        return next((value for value in self.effects if value.effect_id == effect_id), None)

    def search_by_id(self, search_id: str) -> CardCreateSearchCondition | None:
        return next((value for value in self.searches if value.search_id == search_id), None)

    def pool_by_id(self, pool_id: str) -> CardCreateSearchPool | None:
        return next((value for value in self.pools if value.pool_id == pool_id), None)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    def to_json(self, **kwargs: Any) -> str:
        return json.dumps(self.to_dict(), **kwargs)


@dataclass(frozen=True, slots=True)
class CardCreateSearchChanceInput:
    """Runtime context plus optional validation tokens.

    ``plan_type`` and ``hand_limit`` are context fields absent from
    :class:`Plan3NativeState`.  Selection and insertion values are derived
    from the state's RNG; when supplied, the corresponding tuples are
    validation-only.  GUID tokens are the sole caller-owned random identity.
    """

    plan_type: str = ""
    plan_ignore_card_ids: tuple[str, ...] | None = None
    hand_limit: int | None = None
    selected_card_ids: tuple[str, ...] = ()
    chance_tokens: tuple[str, ...] = ()
    guid_tokens: tuple[str, ...] = ()
    insertion_indices: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        _allow_empty_text(
            self.plan_type, "plan_type", error=CardCreateSearchInputError
        )
        for field in ("selected_card_ids", "chance_tokens", "guid_tokens"):
            value = _text_tuple(
                getattr(self, field), field, error=CardCreateSearchInputError
            )
            object.__setattr__(self, field, value)
        if self.plan_ignore_card_ids is not None:
            value = _text_tuple(
                self.plan_ignore_card_ids,
                "plan_ignore_card_ids",
                error=CardCreateSearchInputError,
            )
            object.__setattr__(self, "plan_ignore_card_ids", value)
        if self.hand_limit is not None:
            _nonnegative_int(
                self.hand_limit, "hand_limit", error=CardCreateSearchInputError
            )
        indices = _int_tuple(
            self.insertion_indices,
            "insertion_indices",
            error=CardCreateSearchInputError,
        )
        if any(value < 0 for value in indices):
            raise CardCreateSearchInputError("invalid-insertion-index")
        object.__setattr__(self, "insertion_indices", indices)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CardCreateSearchUnresolvedInput:
    """One proof or explicit caller input that is not available."""

    field: str
    reason: str

    def __post_init__(self) -> None:
        _text(self.field, "field", error=CardCreateSearchInputError)
        _text(self.reason, "reason", error=CardCreateSearchInputError)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CardCreateSearchUnresolvedBranch:
    """Typed no-op branch; it never contains a guessed card mutation."""

    reason: str
    required_inputs: tuple[CardCreateSearchUnresolvedInput, ...]
    detail: str = ""

    def __post_init__(self) -> None:
        _text(self.reason, "reason", error=CardCreateSearchInputError)
        inputs = tuple(self.required_inputs)
        if not all(
            isinstance(value, CardCreateSearchUnresolvedInput) for value in inputs
        ):
            raise CardCreateSearchInputError("invalid-unresolved-input")
        _allow_empty_text(self.detail, "detail", error=CardCreateSearchInputError)
        object.__setattr__(self, "required_inputs", inputs)

    @property
    def code(self) -> str:
        return self.reason

    @property
    def required_fields(self) -> tuple[str, ...]:
        return tuple(value.field for value in self.required_inputs)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CardCreateSearchResolvedBranch:
    """Fully derived count, ticket-selection, and placement branch."""

    reference_match_count: int
    pick_count: int
    selected_ticket_indices: tuple[int, ...]
    created_count: int

    def __post_init__(self) -> None:
        for field in ("reference_match_count", "pick_count", "created_count"):
            _nonnegative_int(
                getattr(self, field), field, error=CardCreateSearchInputError
            )
        indices = _int_tuple(
            self.selected_ticket_indices,
            "selected_ticket_indices",
            error=CardCreateSearchInputError,
        )
        if any(value < 0 for value in indices):
            raise CardCreateSearchInputError("invalid-selected-ticket-index")
        object.__setattr__(self, "selected_ticket_indices", indices)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


CardCreateSearchBranch: TypeAlias = (
    CardCreateSearchResolvedBranch | CardCreateSearchUnresolvedBranch
)


@dataclass(frozen=True, slots=True)
class CardCreateSearchMutation:
    """One native destination mutation in actual mutation order."""

    ordinal: int
    card_id: str
    upgrade: int
    guid: str
    destination: str
    insertion_index: int

    def __post_init__(self) -> None:
        for field in ("ordinal", "upgrade", "insertion_index"):
            _nonnegative_int(
                getattr(self, field), field, error=CardCreateSearchInputError
            )
        for field in ("card_id", "guid", "destination"):
            _text(getattr(self, field), field, error=CardCreateSearchInputError)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CardCreateSearchTrace:
    """Immutable Android-order RNG, selection, GUID, and placement trace."""

    effect_id: str
    random_state_before: int
    random_state_after: int
    rng_consumed: bool
    selected_card_ids: tuple[str, ...]
    guid_tokens: tuple[str, ...]
    insertion_indices: tuple[int, ...]
    selected_upgrades: tuple[int, ...] = ()
    selected_ticket_indices: tuple[int, ...] = ()
    mutations: tuple[CardCreateSearchMutation, ...] = ()
    rng_call_count: int = 0
    unresolved_inputs: tuple[CardCreateSearchUnresolvedInput, ...] = ()

    def __post_init__(self) -> None:
        _text(self.effect_id, "effect_id", error=CardCreateSearchInputError)
        _nonnegative_int(
            self.random_state_before,
            "random_state_before",
            error=CardCreateSearchInputError,
        )
        _nonnegative_int(
            self.random_state_after,
            "random_state_after",
            error=CardCreateSearchInputError,
        )
        if not isinstance(self.rng_consumed, bool):
            raise CardCreateSearchInputError("invalid-rng-consumed")
        _nonnegative_int(
            self.rng_call_count,
            "rng_call_count",
            error=CardCreateSearchInputError,
        )
        if self.rng_consumed != (self.rng_call_count > 0):
            raise CardCreateSearchInputError("trace-rng-consumption-mismatch")
        for field in ("selected_card_ids", "guid_tokens"):
            value = _text_tuple(
                getattr(self, field), field, error=CardCreateSearchInputError
            )
            object.__setattr__(self, field, value)
        for field in (
            "selected_upgrades",
            "selected_ticket_indices",
            "insertion_indices",
        ):
            indices = _int_tuple(
                getattr(self, field), field, error=CardCreateSearchInputError
            )
            if any(value < 0 for value in indices):
                raise CardCreateSearchInputError("invalid-trace-index", field)
            object.__setattr__(self, field, indices)
        mutations = tuple(self.mutations)
        if not all(isinstance(value, CardCreateSearchMutation) for value in mutations):
            raise CardCreateSearchInputError("invalid-trace-mutation")
        object.__setattr__(self, "mutations", mutations)
        unresolved = tuple(self.unresolved_inputs)
        if not all(
            isinstance(value, CardCreateSearchUnresolvedInput)
            for value in unresolved
        ):
            raise CardCreateSearchInputError("invalid-trace-unresolved-input")
        object.__setattr__(self, "unresolved_inputs", unresolved)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CardCreateSearchResult:
    """Pure result; unresolved branches preserve the input state."""

    before: Plan3NativeState
    after: Plan3NativeState
    contract: CardCreateSearchEffectContract
    chance_input: CardCreateSearchChanceInput
    branch: CardCreateSearchBranch
    trace: CardCreateSearchTrace

    def __post_init__(self) -> None:
        if not isinstance(self.before, Plan3NativeState) or not isinstance(
            self.after, Plan3NativeState
        ):
            raise CardCreateSearchInputError("state-must-be-plan3-native-state")
        if isinstance(self.branch, CardCreateSearchUnresolvedBranch) and self.after is not self.before:
            raise CardCreateSearchInputError("unresolved-result-must-preserve-state")
        if not isinstance(self.contract, CardCreateSearchEffectContract):
            raise CardCreateSearchInputError("invalid-contract")
        if not isinstance(self.chance_input, CardCreateSearchChanceInput):
            raise CardCreateSearchInputError("invalid-chance-input")
        if not isinstance(
            self.branch,
            (CardCreateSearchResolvedBranch, CardCreateSearchUnresolvedBranch),
        ):
            raise CardCreateSearchInputError("invalid-branch")
        if not isinstance(self.trace, CardCreateSearchTrace):
            raise CardCreateSearchInputError("invalid-trace")

    @property
    def state(self) -> Plan3NativeState:
        return self.after

    @property
    def new_state(self) -> Plan3NativeState:
        return self.after

    @property
    def resolved(self) -> bool:
        return isinstance(self.branch, CardCreateSearchResolvedBranch)

    @property
    def unresolved(self) -> bool:
        return not self.resolved

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    def to_json(self, **kwargs: Any) -> str:
        return json.dumps(self.to_dict(), **kwargs)


# The eight real effect rows, in ProduceExamEffect.yaml source order.
_EFFECT_ROWS: tuple[tuple[object, ...], ...] = (
    (
        "e_effect-exam_card_create_search-0001-p_card_search-random-random_pool-p_random_pool-all-upgrade_1-1-hand-random-1_1",
        0,
        "p_card_search-random-random_pool-p_random_pool-all-upgrade_1-1",
        "ProduceCardMovePositionType_Hand",
        "ProducePickRangeType_Random",
        "",
        "ProducePickCountType_Unknown",
        1,
        1,
    ),
    (
        "e_effect-exam_card_create_search-0001-p_card_search-random-random_pool-p_random_pool-produce_007-create_set-card_play_aggressive-p_card_search-deck-deck_random-random_shortage-22_22",
        1,
        "p_card_search-random-random_pool-p_random_pool-produce_007-create_set-card_play_aggressive",
        "ProduceCardMovePositionType_DeckRandom",
        "ProducePickRangeType_Random",
        "p_card_search-deck",
        "ProducePickCountType_Shortage",
        22,
        22,
    ),
    (
        "e_effect-exam_card_create_search-0001-p_card_search-random-random_pool-p_random_pool-produce_007-create_set-concentration-p_card_search-deck-deck_random-random_shortage-22_22",
        2,
        "p_card_search-random-random_pool-p_random_pool-produce_007-create_set-concentration",
        "ProduceCardMovePositionType_DeckRandom",
        "ProducePickRangeType_Random",
        "p_card_search-deck",
        "ProducePickCountType_Shortage",
        22,
        22,
    ),
    (
        "e_effect-exam_card_create_search-0001-p_card_search-random-random_pool-p_random_pool-produce_007-create_set-lesson_buff-p_card_search-deck-deck_random-random_shortage-22_22",
        3,
        "p_card_search-random-random_pool-p_random_pool-produce_007-create_set-lesson_buff",
        "ProduceCardMovePositionType_DeckRandom",
        "ProducePickRangeType_Random",
        "p_card_search-deck",
        "ProducePickCountType_Shortage",
        22,
        22,
    ),
    (
        "e_effect-exam_card_create_search-0001-p_card_search-random-random_pool-p_random_pool-produce_007-create_set-parameter_buff-p_card_search-deck-deck_random-random_shortage-22_22",
        4,
        "p_card_search-random-random_pool-p_random_pool-produce_007-create_set-parameter_buff",
        "ProduceCardMovePositionType_DeckRandom",
        "ProducePickRangeType_Random",
        "p_card_search-deck",
        "ProducePickCountType_Shortage",
        22,
        22,
    ),
    (
        "e_effect-exam_card_create_search-0001-p_card_search-random-random_pool-p_random_pool-produce_007-create_set-review-p_card_search-deck-deck_random-random_shortage-22_22",
        5,
        "p_card_search-random-random_pool-p_random_pool-produce_007-create_set-review",
        "ProduceCardMovePositionType_DeckRandom",
        "ProducePickRangeType_Random",
        "p_card_search-deck",
        "ProducePickCountType_Shortage",
        22,
        22,
    ),
    (
        "e_effect-exam_card_create_search-0001-p_card_search-random-random_pool-p_random_pool-produce_007-full_power-p_card_search-deck-deck_random-random_shortage-22_22",
        6,
        "p_card_search-random-random_pool-p_random_pool-produce_007-full_power",
        "ProduceCardMovePositionType_DeckRandom",
        "ProducePickRangeType_Random",
        "p_card_search-deck",
        "ProducePickCountType_Shortage",
        22,
        22,
    ),
    (
        "e_effect-exam_card_create_search-0001-p_card_search-random-random_pool-p_random_pool-ssr-upgrade_1-1-hand-random-1_1",
        7,
        "p_card_search-random-random_pool-p_random_pool-ssr-upgrade_1-1",
        "ProduceCardMovePositionType_Hand",
        "ProducePickRangeType_Random",
        "",
        "ProducePickCountType_Unknown",
        1,
        1,
    ),
)

_SEARCH_ROWS: tuple[tuple[str, str, int], ...] = (
    (
        "p_card_search-random-random_pool-p_random_pool-all-upgrade_1-1",
        "p_random_pool-all-upgrade_1",
        1,
    ),
    (
        "p_card_search-random-random_pool-p_random_pool-produce_007-create_set-card_play_aggressive",
        "p_random_pool-produce_007-create_set-card_play_aggressive",
        0,
    ),
    (
        "p_card_search-random-random_pool-p_random_pool-produce_007-create_set-concentration",
        "p_random_pool-produce_007-create_set-concentration",
        0,
    ),
    (
        "p_card_search-random-random_pool-p_random_pool-produce_007-create_set-lesson_buff",
        "p_random_pool-produce_007-create_set-lesson_buff",
        0,
    ),
    (
        "p_card_search-random-random_pool-p_random_pool-produce_007-create_set-parameter_buff",
        "p_random_pool-produce_007-create_set-parameter_buff",
        0,
    ),
    (
        "p_card_search-random-random_pool-p_random_pool-produce_007-create_set-review",
        "p_random_pool-produce_007-create_set-review",
        0,
    ),
    (
        "p_card_search-random-random_pool-p_random_pool-produce_007-full_power",
        "p_random_pool-produce_007-full_power",
        0,
    ),
    (
        "p_card_search-random-random_pool-p_random_pool-ssr-upgrade_1-1",
        "p_random_pool-ssr-upgrade_1",
        1,
    ),
)

_REFERENCE_SEARCH_ROW = ("p_card_search-deck", "", 0)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RESEARCH_DIR = _PROJECT_ROOT / "_research" / "gakumasu-diff"


def _parse_int(value: str, field: str, source: Path) -> int:
    try:
        return int(value)
    except ValueError as error:
        raise CardCreateSearchResolutionError(
            "invalid-static-pool-number", f"{source}:{field}:{value}"
        ) from error


def _parse_pool_source(
    path: Path,
    pool_ids: Sequence[str],
    *,
    random_rows: bool,
) -> dict[str, tuple[tuple[str, int, int], ...]]:
    """Read only the linked pool rows, retaining their file order.

    The two pool YAML shapes are deliberately parsed with a tiny, exact
    line-reader instead of adding a dependency or sorting the rows.  This is
    not a general YAML parser; it accepts only the fields used by these two
    Master files.
    """

    wanted = frozenset(pool_ids)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise CardCreateSearchResolutionError("static-pool-read-failed", str(path)) from error

    rows: dict[str, list[tuple[str, int, int]]] = {pool_id: [] for pool_id in wanted}
    current_pool = ""
    current_card = ""
    current_upgrade: int | None = None
    current_ratio: int | None = None

    def flush() -> None:
        nonlocal current_card, current_upgrade, current_ratio
        if current_card and current_pool in wanted:
            if current_upgrade is None or current_ratio is None:
                raise CardCreateSearchResolutionError(
                    "incomplete-static-pool-row", f"{path}:{current_card}"
                )
            rows[current_pool].append(
                (current_card, current_upgrade, current_ratio)
            )
        current_card = ""
        current_upgrade = None
        current_ratio = None

    for line in lines:
        top = re.fullmatch(r"- id: (.+)", line)
        if top is not None:
            flush()
            current_pool = top.group(1)
            continue

        if random_rows:
            card = re.fullmatch(r"  produceCardId: (.+)", line)
        else:
            card = re.fullmatch(r"  - id: (.+)", line)
        if card is not None:
            flush()
            current_card = card.group(1)
            continue

        if random_rows:
            upgrade = re.fullmatch(r"  upgradeCount: (.+)", line)
            ratio = re.fullmatch(r"  ratio: (.+)", line)
        else:
            upgrade = re.fullmatch(r"    upgradeCount: (.+)", line)
            ratio = re.fullmatch(r"    ratio: (.+)", line)
        if upgrade is not None:
            current_upgrade = _parse_int(upgrade.group(1), "upgradeCount", path)
        elif ratio is not None:
            current_ratio = _parse_int(ratio.group(1), "ratio", path)

    flush()
    return {pool_id: tuple(rows[pool_id]) for pool_id in pool_ids}


def _parse_card_plan_types(
    path: Path,
    card_ids: Sequence[str],
) -> dict[str, str]:
    """Read the plan discriminator used by native RandomPool filtering."""

    wanted = frozenset(card_ids)
    result: dict[str, str] = {}
    current_id = ""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise CardCreateSearchResolutionError(
            "static-card-read-failed", str(path)
        ) from error
    for line in lines:
        top = re.fullmatch(r"- id: (.+)", line)
        if top is not None:
            current_id = top.group(1)
            continue
        plan = re.fullmatch(r"  planType: (.+)", line)
        if plan is None or current_id not in wanted:
            continue
        value = plan.group(1)
        previous = result.setdefault(current_id, value)
        if previous != value:
            raise CardCreateSearchResolutionError(
                "card-plan-type-disagrees-across-upgrades", current_id
            )
    missing = wanted - result.keys()
    if missing:
        raise CardCreateSearchResolutionError(
            "static-card-plan-type-missing", sorted(missing)[0]
        )
    return result


def _make_pool_candidate_rows(
    source: str,
    rows: Sequence[tuple[str, int, int]],
    plan_types: Mapping[str, str],
) -> tuple[CardCreateSearchPoolCandidate, ...]:
    return tuple(
        CardCreateSearchPoolCandidate(
            source=source,
            link=card_id,
            slot=slot,
            upgrade_count=upgrade,
            ratio=ratio,
            plan_type=plan_types[card_id],
        )
        for slot, (card_id, upgrade, ratio) in enumerate(rows)
    )


def _make_pool(
    pool_id: str,
    produce_rows: Sequence[tuple[str, int, int]],
    random_rows: Sequence[tuple[str, int, int]],
    plan_types: Mapping[str, str],
) -> CardCreateSearchPool:
    return CardCreateSearchPool(
        pool_id=pool_id,
        produce_card_pool=_make_pool_candidate_rows(
            CARD_POOL_SOURCE, produce_rows, plan_types
        ),
        produce_card_random_pool=_make_pool_candidate_rows(
            CARD_RANDOM_POOL_SOURCE, random_rows, plan_types
        ),
        source_links=(
            CardCreateSearchEvidenceLink(
                source=CARD_POOL_SOURCE,
                link=pool_id,
                slot=0,
                relation="pool-row",
            ),
            CardCreateSearchEvidenceLink(
                source=CARD_RANDOM_POOL_SOURCE,
                link=pool_id,
                slot=0,
                relation="pool-row",
            ),
        ),
    )


def _make_search(
    search_id: str,
    pool_id: str,
    limit_count: int,
    source_slot: int,
) -> CardCreateSearchCondition:
    is_reference = search_id == _REFERENCE_SEARCH_ROW[0]
    return CardCreateSearchCondition(
        search_id=search_id,
        card_rarities=(),
        produce_card_ids=(),
        upgrade_counts=(),
        plan_type="ProducePlanType_Unknown",
        card_categories=(),
        card_status_type="ProduceCardSearchStatusType_Unknown",
        order_type=(
            "ProduceCardOrderType_Unknown"
            if is_reference
            else "ProduceCardOrderType_Random"
        ),
        card_position_type=(
            "ProduceCardPositionType_Deck"
            if is_reference
            else "ProduceCardPositionType_RandomPool"
        ),
        card_search_tag="",
        produce_card_random_pool_id="" if is_reference else pool_id,
        limit_count=0 if is_reference else limit_count,
        stamina_min_max_type="ConditionMinMaxType_Unknown",
        stamina_min=0,
        stamina_max=0,
        exam_effect_type="ProduceExamEffectType_Unknown",
        effect_group_ids=(),
        is_self=False,
        produce_card_pool_id="" if is_reference else pool_id,
        cost_type="ExamCostType_Unknown",
        is_customized=False,
        source_links=(
            CardCreateSearchEvidenceLink(
                source=CARD_SEARCH_SOURCE,
                link=search_id,
                slot=source_slot,
                relation="search-row",
            ),
        ),
    )


def _make_effect(
    row: tuple[object, ...],
    searches: Mapping[str, CardCreateSearchCondition],
    pools: Mapping[str, CardCreateSearchPool],
) -> CardCreateSearchEffectContract:
    (
        effect_id,
        source_slot,
        search_id,
        move_position_type,
        pick_range_type,
        pick_count_reference_search_id,
        pick_count_type,
        pick_count_min,
        pick_count_max,
    ) = row
    if not all(isinstance(value, str) for value in (effect_id, search_id)):
        raise CardCreateSearchResolutionError("invalid-static-effect-id")
    search = searches.get(search_id)
    if search is None:
        raise CardCreateSearchResolutionError("missing-static-search", search_id)
    pool = pools.get(search.produce_card_random_pool_id)
    if pool is None:
        raise CardCreateSearchResolutionError(
            "missing-static-pool", search.produce_card_random_pool_id
        )
    if not isinstance(move_position_type, str) or move_position_type not in _DESTINATION_BY_MOVE_POSITION:
        raise CardCreateSearchResolutionError(
            "unknown-static-destination", str(move_position_type)
        )
    if not isinstance(source_slot, int):
        raise CardCreateSearchResolutionError("invalid-static-effect-slot")
    source_links = (
        CardCreateSearchEvidenceLink(
            source=MASTER_DB_SOURCE,
            link=effect_id,
            slot=source_slot,
            relation="effect-row",
        ),
        CardCreateSearchEvidenceLink(
            source=MASTER_EFFECT_SOURCE,
            link=effect_id,
            slot=source_slot,
            relation="effect-row",
        ),
        CardCreateSearchEvidenceLink(
            source=CARD_SEARCH_SOURCE,
            link=search_id,
            slot=source_slot,
            relation="produceCardSearchId",
        ),
        CardCreateSearchEvidenceLink(
            source=CARD_POOL_SOURCE,
            link=pool.pool_id,
            slot=source_slot,
            relation="produceCardPoolId",
        ),
        CardCreateSearchEvidenceLink(
            source=CARD_RANDOM_POOL_SOURCE,
            link=pool.pool_id,
            slot=source_slot,
            relation="produceCardRandomPoolId",
        ),
    )
    return CardCreateSearchEffectContract(
        effect_id=effect_id,
        source_slot=source_slot,
        effect_type=CARD_CREATE_SEARCH_EFFECT_TYPE,
        effect_value1=1,
        effect_value2=0,
        effect_count=0,
        effect_turn=0,
        target_produce_card_id="",
        target_upgrade_count=0,
        target_exam_effect_type="ProduceExamEffectType_Unknown",
        search_id=search_id,
        move_position_type=move_position_type,
        destination=_DESTINATION_BY_MOVE_POSITION[move_position_type],
        pick_range_type=pick_range_type,
        pick_count_reference_search_id=pick_count_reference_search_id,
        pick_count_type=pick_count_type,
        pick_count_min=pick_count_min,
        pick_count_max=pick_count_max,
        search_id2="",
        pick_range_type2="ProducePickRangeType_Unknown",
        pick_count_reference_search_id2="",
        pick_count_type2="ProducePickCountType_Unknown",
        pick_count_min2=0,
        pick_count_max2=0,
        chain_effect_id="",
        chain_effect_ids=(),
        produce_exam_status_enchant_id="",
        produce_card_status_enchant_id="",
        produce_card_grow_effect_ids=(),
        effect_group_ids=(),
        search=search,
        pool=pool,
        source_links=source_links,
    )


def _make_card_versions(effect_id: str) -> tuple[CardCreateSearchAffectedCardVariant, ...]:
    rows = (
        (0, "花萌ゆ季節", 0),
        (1, "花萌ゆ季節+", 0),
        (2, "花萌ゆ季節++", 0),
        (3, "花萌ゆ季節+++", 1),
    )
    return tuple(
        CardCreateSearchAffectedCardVariant(
            card_id="p_card-00-sup-3_024",
            upgrade=upgrade,
            name=name,
            plan_type="ProducePlanType_Common",
            category="ProduceCardCategory_MentalSkill",
            effect_id=effect_id,
            effect_slot=effect_slot,
            source_links=(
                CardCreateSearchEvidenceLink(
                    source=CARD_SOURCE,
                    link=effect_id,
                    slot=effect_slot,
                    relation=f"playEffects[{effect_slot}]",
                ),
            ),
        )
        for upgrade, name, effect_slot in rows
    )


def _make_native_evidence() -> CardCreateSearchNativeEvidence:
    return CardCreateSearchNativeEvidence(
        android_version=ANDROID_VERSION,
        enum_name="ExamCardCreateSearch",
        enum_value=21,
        executor_type="Campus.InGame.Exam.CardCreateSearchEffectExecutor",
        factory_branch_va="0x7E5D284",
        type_global_va="0xE756F88",
        usage_cell_va="0xEB26598",
        constructor_va="0x7E742B4",
        constructor_metadata_method_index=17807,
        constructor_token="0x06004590",
        executor_method_index=17808,
        executor_method_token="0x06004591",
        execute_effect_va="0x7E74624",
        get_search_card_list_va="0x7E57B94",
        get_pick_count_min_max_va="0x7E58FDC",
        get_random_pool_pick_card_list_va="0x7E596B0",
        pick_card_position_list_impl_va="0x7E59CA4",
        master_pool_resolver_va="0x7F54B2C",
        add_card_va="0x809A760",
        guid_getter_va="0x808F030",
        executor_body_proven=True,
        source_links=(
            CardCreateSearchEvidenceLink(
                source=EXECUTOR_MAPPING_SOURCE,
                link="ExamCardCreateSearch",
                slot=0,
                relation="factory-branch-and-constructor",
            ),
            CardCreateSearchEvidenceLink(
                source=EXECUTOR_CS_SOURCE,
                link="CardCreateSearchEffectExecutor",
                slot=0,
                relation="field-and-method-skeleton",
            ),
            CardCreateSearchEvidenceLink(
                source=EXECUTOR_METADATA_SOURCE,
                link="CardCreateSearchEffectExecutor",
                slot=0,
                relation="metadata-method-index",
            ),
            CardCreateSearchEvidenceLink(
                source=NATIVE_AUDIT_SOURCE,
                link="android-v3.2.3-card-create-search",
                slot=0,
                relation="executor-search-pool-rng-placement-guid-proof",
            ),
        ),
    )


def _build_catalog(research_dir: Path = DEFAULT_RESEARCH_DIR) -> CardCreateSearchCatalog:
    pool_ids = tuple(pool_id for _, pool_id, _ in _SEARCH_ROWS)
    produce_rows = _parse_pool_source(
        research_dir / "ProduceCardPool.yaml",
        pool_ids,
        random_rows=False,
    )
    random_rows = _parse_pool_source(
        research_dir / "ProduceCardRandomPool.yaml",
        pool_ids,
        random_rows=True,
    )
    card_ids = tuple(
        dict.fromkeys(
            card_id
            for pool_id in pool_ids
            for rows in (produce_rows[pool_id], random_rows[pool_id])
            for card_id, _upgrade, _ratio in rows
        )
    )
    plan_types = _parse_card_plan_types(
        research_dir / "ProduceCard.yaml", card_ids
    )
    pools = {
        pool_id: _make_pool(
            pool_id,
            produce_rows[pool_id],
            random_rows[pool_id],
            plan_types,
        )
        for pool_id in pool_ids
    }
    search_rows = (_REFERENCE_SEARCH_ROW, *_SEARCH_ROWS)
    searches = {
        search_id: _make_search(search_id, pool_id, limit, slot)
        for slot, (search_id, pool_id, limit) in enumerate(search_rows)
    }
    effects = tuple(_make_effect(row, searches, pools) for row in _EFFECT_ROWS)
    cards = _make_card_versions(effects[0].effect_id)
    return CardCreateSearchCatalog(
        master_snapshot=MASTER_SNAPSHOT,
        effects=effects,
        searches=tuple(searches.values()),
        pools=tuple(pools[pool_id] for pool_id in dict.fromkeys(pool_ids)),
        affected_card_versions=cards,
        native=_make_native_evidence(),
        proven_order=(
            "Master effect row -> produceCardSearchId link is present",
            "ProduceCardSearch -> ProduceCardPoolId and ProduceCardRandomPoolId links are present",
            "CreateExecutor enum value 21 -> CardCreateSearchEffectExecutor branch is mapped",
            "mapped factory branch -> CardCreateSearchEffectExecutor constructor call is mapped",
            "ExamMasterDataContainer.GetProduceCardRandomPool -> ProduceCardPoolMaster -> ProduceCardRatioList",
            "ratio rows -> repeated uniform tickets in source order",
            "plan filter -> random OrderBy keys -> LimitCount",
            "GetPickCountMinMax -> inclusive count RNG -> per-ticket random keys -> Take -> original-index sort",
            "selected ticket id/upgrade -> GetProduceCardData -> cloned ExamCardData",
            "created-card list -> AddCard Hand/DeckRandom in constructor order",
            "ExamCardData.Guid -> lazy CreateGuidIfNeed -> Guid.NewGuid",
        ),
        unproven_order=(
            "caller must supply current plan type because Plan3NativeState does not own ExamParameter.PlanType",
            "caller must supply GUID tokens because Guid.NewGuid is outside the exam RNG",
            "Hand effects require the runtime hand limit setting",
        ),
        coverage=CardCreateSearchCoverage(
            effect_rows_cataloged=len(effects),
            affected_card_versions_cataloged=len(cards),
            executable_effect_rows=len(effects),
            executable_card_versions=len(cards),
        ),
    )


def load_card_create_search_catalog(
    research_dir: Path = DEFAULT_RESEARCH_DIR,
) -> CardCreateSearchCatalog:
    """Load the same typed catalog from the two local pool YAML sources."""

    return _build_catalog(Path(research_dir))


@lru_cache(maxsize=1)
def default_card_create_search_catalog() -> CardCreateSearchCatalog:
    """Load legacy simulator data only when that simulator is requested."""
    return _build_catalog()


def _row_dict(effect_row: Mapping[str, object] | sqlite3.Row) -> dict[str, object]:
    if isinstance(effect_row, Mapping):
        return dict(effect_row)
    keys = getattr(effect_row, "keys", None)
    if callable(keys):
        try:
            return {key: effect_row[key] for key in keys()}
        except (KeyError, IndexError, TypeError) as error:
            raise CardCreateSearchResolutionError("invalid-master-row") from error
    raise CardCreateSearchResolutionError("master-row-must-be-mapping")


def _value(
    row: Mapping[str, object], *names: str, default: object = _MISSING
) -> object:
    for name in names:
        if name in row:
            return row[name]
    if default is not _MISSING:
        return default
    raise CardCreateSearchResolutionError("missing-structural-field", names[0])


def _raw_master_payload(outer: Mapping[str, object]) -> dict[str, object]:
    candidate = _value(outer, "raw_json", "rawJson", "raw", default=_MISSING)
    if candidate is _MISSING or candidate is None or candidate == "":
        structural_keys = {
            "effectType",
            "effectValue1",
            "effectValue2",
            "effectCount",
            "effectTurn",
            "produceCardSearchId",
            "movePositionType",
            "pickRangeType",
            "pickCountMin",
            "pickCountMax",
        }
        if structural_keys.intersection(outer):
            return dict(outer)
        raise CardCreateSearchResolutionError("missing-master-raw-json")
    if isinstance(candidate, (bytes, bytearray)):
        candidate = candidate.decode("utf-8")
    if isinstance(candidate, str):
        try:
            candidate = json.loads(candidate)
        except json.JSONDecodeError as error:
            raise CardCreateSearchResolutionError("invalid-master-raw-json") from error
    if not isinstance(candidate, Mapping):
        raise CardCreateSearchResolutionError("master-raw-json-must-be-object")
    return dict(candidate)


def _canonical_expected(value: object, field: str) -> object:
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return value


def resolve_card_create_search_contract(
    effect_row: Mapping[str, object] | sqlite3.Row,
    *,
    catalog: CardCreateSearchCatalog | None = None,
) -> CardCreateSearchEffectContract:
    """Resolve one complete static row without filling missing fields.

    The normalized SQLite columns are used only for the outer id/type
    cross-check.  All execution-shape fields must be present in ``raw_json``.
    """

    if catalog is None:
        catalog = default_card_create_search_catalog()
    outer = _row_dict(effect_row)
    raw = _raw_master_payload(outer)
    outer_type = _value(outer, "effect_type", "effectType", default=_MISSING)
    raw_type = _value(raw, "effectType", "effect_type", default=_MISSING)
    for value in (outer_type, raw_type):
        if value is not _MISSING and value != CARD_CREATE_SEARCH_EFFECT_TYPE:
            raise CardCreateSearchResolutionError("unexpected-effect-type", str(value))
    if outer_type is _MISSING and raw_type is _MISSING:
        raise CardCreateSearchResolutionError("missing-effect-type")

    outer_id = _value(outer, "id", "effect_id", default=_MISSING)
    raw_id = _value(raw, "id", "effect_id", default=_MISSING)
    if outer_id is not _MISSING and raw_id is not _MISSING and outer_id != raw_id:
        raise CardCreateSearchResolutionError("master-id-mismatch")
    effect_id = raw_id if raw_id is not _MISSING else outer_id
    effect_id = _text(
        effect_id, "effect_id", error=CardCreateSearchResolutionError
    )
    contract = catalog.effect_by_id(effect_id)
    if contract is None:
        raise CardCreateSearchResolutionError("unknown-effect-id", effect_id)

    expected: dict[str, object] = {
        "effectType": contract.effect_type,
        "effectValue1": contract.effect_value1,
        "effectValue2": contract.effect_value2,
        "effectCount": contract.effect_count,
        "effectTurn": contract.effect_turn,
        "targetProduceCardId": contract.target_produce_card_id,
        "targetUpgradeCount": contract.target_upgrade_count,
        "targetExamEffectType": contract.target_exam_effect_type,
        "produceCardSearchId": contract.search_id,
        "movePositionType": contract.move_position_type,
        "pickRangeType": contract.pick_range_type,
        "pickCountReferenceProduceCardSearchId": contract.pick_count_reference_search_id,
        "pickCountType": contract.pick_count_type,
        "pickCountMin": contract.pick_count_min,
        "pickCountMax": contract.pick_count_max,
        "produceCardSearchId2": contract.search_id2,
        "pickRangeType2": contract.pick_range_type2,
        "pickCountReferenceProduceCardSearchId2": contract.pick_count_reference_search_id2,
        "pickCountType2": contract.pick_count_type2,
        "pickCountMin2": contract.pick_count_min2,
        "pickCountMax2": contract.pick_count_max2,
        "chainProduceExamEffectId": contract.chain_effect_id,
        "chainProduceExamEffectIds": contract.chain_effect_ids,
        "produceExamStatusEnchantId": contract.produce_exam_status_enchant_id,
        "produceCardStatusEnchantId": contract.produce_card_status_enchant_id,
        "produceCardGrowEffectIds": contract.produce_card_grow_effect_ids,
        "effectGroupIds": contract.effect_group_ids,
    }
    for field, expected_value in expected.items():
        observed = _value(raw, field)
        if isinstance(expected_value, tuple):
            if not isinstance(observed, Sequence) or isinstance(
                observed, (str, bytes, bytearray)
            ):
                raise CardCreateSearchResolutionError("invalid-structural-sequence", field)
            observed_value = tuple(observed)
        else:
            observed_value = observed
        if _canonical_expected(observed_value, field) != expected_value:
            raise CardCreateSearchResolutionError(
                "master-structural-mismatch", field
            )
    return contract


def try_resolve_card_create_search_contract(
    effect_row: Mapping[str, object] | sqlite3.Row,
    *,
    catalog: CardCreateSearchCatalog | None = None,
) -> CardCreateSearchEffectContract | CardCreateSearchUnresolvedInput:
    """Return a typed unresolved value for an unknown or incomplete row."""

    try:
        return resolve_card_create_search_contract(effect_row, catalog=catalog)
    except CardCreateSearchResolutionError as error:
        return CardCreateSearchUnresolvedInput(error.code, error.detail or error.code)


def load_master_card_create_search_rows(
    database: Path = DEFAULT_DATABASE,
    *,
    catalog: CardCreateSearchCatalog | None = None,
) -> tuple[dict[str, object], ...]:
    """Read the eight real effect rows and return them in catalog source order."""

    try:
        with sqlite3.connect(Path(database)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM effect WHERE effect_type = ?",
                (CARD_CREATE_SEARCH_EFFECT_TYPE,),
            ).fetchall()
    except sqlite3.Error as error:
        raise CardCreateSearchResolutionError("master-effect-read-failed") from error
    if catalog is None:
        catalog = default_card_create_search_catalog()
    by_id = {str(row["id"]): dict(row) for row in rows}
    return tuple(by_id[effect_id] for effect_id in catalog.effect_ids if effect_id in by_id)


@dataclass(frozen=True, slots=True)
class _PoolTicket:
    index: int
    candidate: CardCreateSearchPoolCandidate


def _unresolved_result(
    state: Plan3NativeState,
    contract: CardCreateSearchEffectContract,
    supplied: CardCreateSearchChanceInput,
    *inputs: CardCreateSearchUnresolvedInput,
    detail: str = "",
) -> CardCreateSearchResult:
    branch = CardCreateSearchUnresolvedBranch(
        reason="card-create-search-runtime-input-unresolved",
        required_inputs=tuple(inputs),
        detail=detail,
    )
    trace = CardCreateSearchTrace(
        effect_id=contract.effect_id,
        random_state_before=state.random_state,
        random_state_after=state.random_state,
        rng_consumed=False,
        selected_card_ids=supplied.selected_card_ids,
        guid_tokens=supplied.guid_tokens,
        insertion_indices=supplied.insertion_indices,
        unresolved_inputs=tuple(inputs),
    )
    return CardCreateSearchResult(
        before=state,
        after=state,
        contract=contract,
        chance_input=supplied,
        branch=branch,
        trace=trace,
    )


def _expanded_eligible_tickets(
    contract: CardCreateSearchEffectContract,
    supplied: CardCreateSearchChanceInput,
) -> tuple[_PoolTicket, ...]:
    ignored = frozenset(supplied.plan_ignore_card_ids or ())
    tickets: list[_PoolTicket] = []
    index = 0
    for candidate in contract.pool.authoritative_candidates:
        eligible = (
            candidate.plan_type == "ProducePlanType_Common"
            or candidate.plan_type == supplied.plan_type
            or candidate.card_id in ignored
        )
        for _ in range(candidate.ratio):
            if eligible:
                tickets.append(_PoolTicket(index, candidate))
            index += 1
    return tuple(tickets)


def _random_order(
    values: Sequence[_PoolTicket],
    random_state: int,
) -> tuple[tuple[_PoolTicket, ...], int, int]:
    """Stable ``Enumerable.OrderBy`` with one native int key per value."""

    keyed: list[tuple[int, _PoolTicket]] = []
    current = random_state
    for value in values:
        key, current = next_int32(current)
        keyed.append((key, value))
    return tuple(value for _key, value in sorted(keyed, key=lambda x: x[0])), current, len(keyed)


def _guid_values(
    state: Plan3NativeState,
    supplied: CardCreateSearchChanceInput,
    count: int,
) -> tuple[str, ...] | CardCreateSearchUnresolvedInput:
    if count == 0:
        return ()
    tokens = supplied.guid_tokens
    if len(tokens) < count:
        return CardCreateSearchUnresolvedInput(
            field=f"guid_tokens[{len(tokens)}:{count}]",
            reason="ExamCardData.Guid is lazy Guid.NewGuid; explicit tokens are required",
        )
    if len(tokens) > count:
        raise CardCreateSearchInputError("extra-guid-tokens")
    if len(set(tokens)) != len(tokens):
        raise CardCreateSearchInputError("duplicate-guid-token")
    existing = {card.guid for card in state.all_cards}
    collision = next((token for token in tokens if token in existing), None)
    if collision is not None:
        raise CardCreateSearchInputError("guid-already-in-state", collision)
    return tokens


def apply_card_create_search(
    state: Plan3NativeState,
    contract: CardCreateSearchEffectContract,
    *,
    chance_input: CardCreateSearchChanceInput | None = None,
    execution_input: CardCreateSearchChanceInput | None = None,
) -> CardCreateSearchResult:
    """Apply the Android search/create/AddCard pipeline as a pure transition."""

    if not isinstance(state, Plan3NativeState):
        raise CardCreateSearchInputError("state-must-be-plan3-native-state")
    if not isinstance(contract, CardCreateSearchEffectContract):
        raise CardCreateSearchInputError("contract-must-be-card-create-search-contract")
    if chance_input is not None and execution_input is not None:
        raise CardCreateSearchInputError("conflicting-chance-inputs")
    supplied = execution_input if execution_input is not None else chance_input
    if supplied is None:
        supplied = CardCreateSearchChanceInput()
    if not isinstance(supplied, CardCreateSearchChanceInput):
        raise CardCreateSearchInputError("invalid-chance-input")
    if not supplied.plan_type:
        return _unresolved_result(
            state,
            contract,
            supplied,
            CardCreateSearchUnresolvedInput(
                "plan_type",
                "ExamParameter.PlanType is required for RandomPool plan filtering",
            ),
        )
    if supplied.plan_ignore_card_ids is None:
        return _unresolved_result(
            state,
            contract,
            supplied,
            CardCreateSearchUnresolvedInput(
                "plan_ignore_card_ids",
                "ExamParameter._planIgnoreProduceCardWhiteList is required; pass an explicit empty tuple when empty",
            ),
        )
    if contract.destination == "hand" and supplied.hand_limit is None:
        return _unresolved_result(
            state,
            contract,
            supplied,
            CardCreateSearchUnresolvedInput(
                "hand_limit",
                "the runtime exam setting hand limit is required",
            ),
        )
    if (
        contract.search.card_position_type != "ProduceCardPositionType_RandomPool"
        or contract.search.order_type != "ProduceCardOrderType_Random"
        or contract.pick_range_type != "ProducePickRangeType_Random"
    ):
        raise CardCreateSearchInputError("unsupported-catalog-search-shape")

    current_random_state = state.random_state
    rng_call_count = 0
    eligible = _expanded_eligible_tickets(contract, supplied)

    # PickCardPositionListImpl first applies search.OrderType, then LimitCount.
    ordered, current_random_state, calls = _random_order(
        eligible, current_random_state
    )
    rng_call_count += calls
    if contract.search.limit_count > 0:
        ordered = ordered[: contract.search.limit_count]

    reference_match_count = 0
    count_min = contract.pick_count_min
    count_max = contract.pick_count_max
    if contract.pick_count_type == "ProducePickCountType_Shortage":
        if contract.pick_count_reference_search_id != "p_card_search-deck":
            raise CardCreateSearchInputError("unsupported-shortage-reference-search")
        reference_match_count = len(state.deck)
        count_min = max(count_min - reference_match_count, 0)
        count_max = max(count_max - reference_match_count, 0)
    elif contract.pick_count_type not in {
        "ProducePickCountType_Unknown",
        "ProducePickCountType_Normal",
    }:
        raise CardCreateSearchInputError("unsupported-pick-count-type")

    # Native calls GetRandomInt(min, max + 1), even when min == max.
    pick_count, current_random_state = next_range(
        current_random_state, count_min, count_max + 1
    )
    rng_call_count += 1
    randomly_ordered, current_random_state, calls = _random_order(
        ordered, current_random_state
    )
    rng_call_count += calls
    picked = tuple(
        sorted(
            randomly_ordered[: min(pick_count, len(randomly_ordered))],
            key=lambda ticket: ticket.index,
        )
    )

    multiplier = max(contract.effect_value1, 1)
    created_tickets = tuple(
        ticket for ticket in picked for _ in range(multiplier)
    )
    selected_card_ids = tuple(
        ticket.candidate.card_id for ticket in created_tickets
    )
    selected_upgrades = tuple(
        ticket.candidate.upgrade_count for ticket in created_tickets
    )
    selected_ticket_indices = tuple(ticket.index for ticket in created_tickets)
    if supplied.selected_card_ids and supplied.selected_card_ids != selected_card_ids:
        raise CardCreateSearchInputError("selected-card-validation-mismatch")

    values = _guid_values(state, supplied, len(created_tickets))
    if isinstance(values, CardCreateSearchUnresolvedInput):
        return _unresolved_result(state, contract, supplied, values)

    new_cards = tuple(
        Plan3NativeCard(
            guid=guid,
            card_id=ticket.candidate.card_id,
            base_upgrade=ticket.candidate.upgrade_count,
            temporary_upgrade=0,
            effective_upgrade=ticket.candidate.upgrade_count,
        )
        for guid, ticket in zip(values, created_tickets, strict=True)
    )

    sampled_insertion_indices: tuple[int, ...]
    mutations: tuple[CardCreateSearchMutation, ...]
    if contract.destination == "deck_random":
        original_deck_count = len(state.deck)
        sampled: list[int] = []
        for _card in new_cards:
            if original_deck_count == 0:
                insertion_index = 0
                current_random_state = advance_state(current_random_state)
            else:
                insertion_index, current_random_state = next_range(
                    current_random_state, 0, original_deck_count
                )
            sampled.append(insertion_index)
            rng_call_count += 1
        sampled_insertion_indices = tuple(sampled)
        if (
            supplied.insertion_indices
            and supplied.insertion_indices != sampled_insertion_indices
        ):
            raise CardCreateSearchInputError("insertion-index-validation-mismatch")
        indexed_cards = tuple(
            (sampled_insertion_indices[ordinal], ordinal, card)
            for ordinal, card in enumerate(new_cards)
        )
        ordered_mutations = tuple(sorted(indexed_cards, key=lambda value: value[0]))
        deck = list(state.deck)
        for insertion_index, _ordinal, card in ordered_mutations:
            deck.insert(insertion_index, card)
        after = replace(
            state, deck=tuple(deck), random_state=current_random_state
        )
        mutations = tuple(
            CardCreateSearchMutation(
                ordinal=ordinal,
                card_id=card.card_id,
                upgrade=card.effective_upgrade,
                guid=card.guid,
                destination="deck_random",
                insertion_index=insertion_index,
            )
            for insertion_index, ordinal, card in ordered_mutations
        )
    else:
        assert supplied.hand_limit is not None
        capacity = max(0, supplied.hand_limit - len(state.hand))
        hand_count = min(len(new_cards), capacity)
        hand_cards = new_cards[:hand_count]
        overflow_cards = new_cards[hand_count:]
        sampled_insertion_indices = tuple(
            (
                len(state.hand) + ordinal
                if ordinal < hand_count
                else ordinal - hand_count
            )
            for ordinal in range(len(new_cards))
        )
        if (
            supplied.insertion_indices
            and supplied.insertion_indices != sampled_insertion_indices
        ):
            raise CardCreateSearchInputError("insertion-index-validation-mismatch")
        after = replace(
            state,
            deck=(*overflow_cards, *state.deck),
            hand=(*state.hand, *hand_cards),
            random_state=current_random_state,
        )
        mutations = tuple(
            CardCreateSearchMutation(
                ordinal=hand_count + overflow_ordinal,
                card_id=card.card_id,
                upgrade=card.effective_upgrade,
                guid=card.guid,
                destination="deck_first",
                insertion_index=overflow_ordinal,
            )
            for overflow_ordinal, card in enumerate(overflow_cards)
        ) + tuple(
            CardCreateSearchMutation(
                ordinal=ordinal,
                card_id=card.card_id,
                upgrade=card.effective_upgrade,
                guid=card.guid,
                destination="hand",
                insertion_index=len(state.hand) + ordinal,
            )
            for ordinal, card in enumerate(hand_cards)
        )

    branch = CardCreateSearchResolvedBranch(
        reference_match_count=reference_match_count,
        pick_count=pick_count,
        selected_ticket_indices=tuple(ticket.index for ticket in picked),
        created_count=len(new_cards),
    )
    trace = CardCreateSearchTrace(
        effect_id=contract.effect_id,
        random_state_before=state.random_state,
        random_state_after=after.random_state,
        rng_consumed=rng_call_count > 0,
        selected_card_ids=selected_card_ids,
        selected_upgrades=selected_upgrades,
        selected_ticket_indices=selected_ticket_indices,
        guid_tokens=values,
        insertion_indices=sampled_insertion_indices,
        mutations=mutations,
        rng_call_count=rng_call_count,
    )
    return CardCreateSearchResult(
        before=state,
        after=after,
        contract=contract,
        chance_input=supplied,
        branch=branch,
        trace=trace,
    )


resolve_plan3_card_create_search_contract = resolve_card_create_search_contract
apply_plan3_card_create_search = apply_card_create_search
execute_card_create_search = apply_card_create_search
CardCreateSearchExecutionInput = CardCreateSearchChanceInput


__all__ = [
    "ANDROID_VERSION",
    "CARD_CREATE_SEARCH_EFFECT_TYPE",
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
    "CardCreateSearchCoverage",
    "default_card_create_search_catalog",
    "apply_card_create_search",
    "apply_plan3_card_create_search",
    "execute_card_create_search",
    "load_card_create_search_catalog",
    "load_master_card_create_search_rows",
    "resolve_card_create_search_contract",
    "resolve_plan3_card_create_search_contract",
    "try_resolve_card_create_search_contract",
]
