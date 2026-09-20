"""Master-backed effect catalog for the fixed Initial Regular FKTN route.

Only exact ``ProduceStepEventDetail`` and suggestion identities are accepted.
The three Master effect buckets remain separate and ordered: detail effects,
suggestion base effects, and the selected success-or-fail effects.  This module
does not expose a flattened execution order and does not infer reward or card
pool membership from identifiers.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from enum import Enum
import json
from pathlib import Path
import sqlite3

from .initial_regular_event_scenario import NextStepRef
from .master_db import DEFAULT_DATABASE


FKTN_INITIAL_REGULAR_EVENT_DETAIL_IDS = (
    *(f"event-detail-school-001-fktn-{index:03d}" for index in range(1, 26)),
    *(f"event-detail-activity-001-fktn-{index:03d}" for index in range(1, 21)),
    "event-detail-activity-001-fktn-017-1",
    "event-detail-activity-001-fktn-018-1",
)
FKTN_INITIAL_REGULAR_EVENT_DETAIL_ID_SET = frozenset(
    FKTN_INITIAL_REGULAR_EVENT_DETAIL_IDS
)

_ATTRIBUTE_EFFECT_TYPES = {
    "ProduceEffectType_VocalAddition": "vocal",
    "ProduceEffectType_DanceAddition": "dance",
    "ProduceEffectType_VisualAddition": "visual",
}
_STAMINA_RECOVER_MULTIPLE = "ProduceEffectType_StaminaRecoverMultiple"
_PRODUCE_REWARD = "ProduceEffectType_ProduceReward"
_PRODUCE_REWARD_SET = "ProduceEffectType_ProduceRewardSet"
_PRODUCE_CARD_UPGRADE = "ProduceEffectType_ProduceCardUpgrade"
_PRODUCE_CARD_CHANGE = "ProduceEffectType_ProduceCardChange"
_PRODUCE_CARD_DELETE = "ProduceEffectType_ProduceCardDelete"


class InitialRegularEventEffectCatalogError(ValueError):
    """An exact static event catalog cannot be produced without guessing."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _text(value: object, name: str, *, empty: bool = False) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be text")
    if not empty and not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    return value


def _integer(value: object, name: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be boolean")
    return value


def _sqlite_boolean(value: object, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    raise InitialRegularEventEffectCatalogError(
        "invalid-master-boolean", f"{name} must be stored as 0 or 1"
    )


def _json_list(raw: object, name: str) -> list[object]:
    if not isinstance(raw, str):
        raise TypeError(f"{name} JSON must be text")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise InitialRegularEventEffectCatalogError(
            "invalid-master-json", f"{name}: {error}"
        ) from error
    if not isinstance(value, list):
        raise InitialRegularEventEffectCatalogError(
            "invalid-master-json", f"{name} must decode to a list"
        )
    return value


def _id_list(raw: object, name: str) -> tuple[str, ...]:
    result: list[str] = []
    for index, value in enumerate(_json_list(raw, name)):
        try:
            result.append(_text(value, f"{name}[{index}]"))
        except (TypeError, ValueError) as error:
            raise InitialRegularEventEffectCatalogError(
                "invalid-master-id-list", str(error)
            ) from error
    return tuple(result)


@dataclass(frozen=True, slots=True)
class EmbeddedProduceReward:
    """One literal member of ``ProduceEffect.produceRewards``."""

    resource_type: str
    resource_id: str
    resource_level: int

    def __post_init__(self) -> None:
        _text(self.resource_type, "embedded reward resource type")
        _text(self.resource_id, "embedded reward resource id", empty=True)
        _integer(self.resource_level, "embedded reward resource level", minimum=0)


class ScenarioInputRequirement(str, Enum):
    """Dynamic facts that Master cannot resolve for an exact replay."""

    EFFECT_TARGET_IDS = "effect-target-ids"
    REWARD_SET_RESOLUTION = "reward-set-membership-and-selection"
    CARD_CHANGE_REPLACEMENT = "card-change-replacement"


@dataclass(frozen=True, slots=True)
class CatalogEffect:
    """Fields common to every exact ``ProduceEffect`` row."""

    effect_id: str
    effect_type: str
    effect_value_min: int
    effect_value_max: int
    resource_type: str
    embedded_rewards: tuple[EmbeddedProduceReward, ...]
    card_search_id: str
    exam_status_enchant_id: str
    step_event_detail_id: str
    pick_range_type: str
    pick_count_min: int
    pick_count_max: int
    is_research: bool

    def __post_init__(self) -> None:
        _text(self.effect_id, "effect id")
        _text(self.effect_type, "effect type")
        _integer(self.effect_value_min, "effect value minimum")
        _integer(self.effect_value_max, "effect value maximum")
        if self.effect_value_max < self.effect_value_min:
            raise ValueError("effect value range is reversed")
        _text(self.resource_type, "effect resource type")
        rewards = tuple(self.embedded_rewards)
        if not all(isinstance(value, EmbeddedProduceReward) for value in rewards):
            raise TypeError("embedded rewards must contain typed values")
        object.__setattr__(self, "embedded_rewards", rewards)
        _text(self.card_search_id, "effect card-search id", empty=True)
        _text(
            self.exam_status_enchant_id,
            "effect exam-status-enchant id",
            empty=True,
        )
        _text(self.step_event_detail_id, "effect step-event-detail id", empty=True)
        _text(self.pick_range_type, "effect pick-range type")
        _integer(self.pick_count_min, "effect pick-count minimum", minimum=0)
        _integer(self.pick_count_max, "effect pick-count maximum", minimum=0)
        if self.pick_count_max < self.pick_count_min:
            raise ValueError("effect pick-count range is reversed")
        _boolean(self.is_research, "effect is_research")

    @property
    def scenario_inputs(self) -> tuple[ScenarioInputRequirement, ...]:
        """Server/result facts required in addition to this static row."""

        return ()


@dataclass(frozen=True, slots=True)
class AttributeAdditionEffect(CatalogEffect):
    attribute: str

    def __post_init__(self) -> None:
        super(AttributeAdditionEffect, self).__post_init__()
        if self.attribute not in {"vocal", "dance", "visual"}:
            raise ValueError(f"unsupported attribute: {self.attribute}")


@dataclass(frozen=True, slots=True)
class StaminaRecoverMultipleEffect(CatalogEffect):
    """Raw Master multiple; arithmetic/rounding is intentionally not inferred."""


@dataclass(frozen=True, slots=True)
class ProduceRewardEffect(CatalogEffect):
    """Direct reward whose only members are literal ``embedded_rewards``."""


@dataclass(frozen=True, slots=True)
class ProduceRewardSetEffect(CatalogEffect):
    """Reward offer shape; server-rolled membership is not represented here."""

    @property
    def scenario_inputs(self) -> tuple[ScenarioInputRequirement, ...]:
        return (ScenarioInputRequirement.REWARD_SET_RESOLUTION,)


@dataclass(frozen=True, slots=True)
class ProduceCardUpgradeEffect(CatalogEffect):
    """Card-search and target-pick shape for an upgrade."""

    @property
    def scenario_inputs(self) -> tuple[ScenarioInputRequirement, ...]:
        return (ScenarioInputRequirement.EFFECT_TARGET_IDS,)


@dataclass(frozen=True, slots=True)
class ProduceCardChangeEffect(CatalogEffect):
    """Source-search shape; replacement membership remains server-owned."""

    @property
    def scenario_inputs(self) -> tuple[ScenarioInputRequirement, ...]:
        return (
            ScenarioInputRequirement.EFFECT_TARGET_IDS,
            ScenarioInputRequirement.CARD_CHANGE_REPLACEMENT,
        )


@dataclass(frozen=True, slots=True)
class ProduceCardDeleteEffect(CatalogEffect):
    """Card-search and target-pick shape for deletion."""

    @property
    def scenario_inputs(self) -> tuple[ScenarioInputRequirement, ...]:
        return (ScenarioInputRequirement.EFFECT_TARGET_IDS,)


@dataclass(frozen=True, slots=True)
class EffectCatalogBlocker:
    code: str
    message: str
    effect_id: str
    effect_type: str

    def __post_init__(self) -> None:
        _text(self.code, "effect blocker code")
        _text(self.message, "effect blocker message")
        _text(self.effect_id, "effect blocker id")
        _text(self.effect_type, "effect blocker type")


@dataclass(frozen=True, slots=True)
class OpaqueCatalogEffect(CatalogEffect):
    """Complete static row whose outer mutation semantics are unsupported."""

    blocker: EffectCatalogBlocker

    def __post_init__(self) -> None:
        super(OpaqueCatalogEffect, self).__post_init__()
        if not isinstance(self.blocker, EffectCatalogBlocker):
            raise TypeError("opaque effect blocker must be typed")
        if (
            self.blocker.effect_id != self.effect_id
            or self.blocker.effect_type != self.effect_type
        ):
            raise ValueError("opaque effect blocker identity differs from effect row")


P0CatalogEffect = (
    AttributeAdditionEffect
    | StaminaRecoverMultipleEffect
    | ProduceRewardEffect
    | ProduceRewardSetEffect
    | ProduceCardUpgradeEffect
    | ProduceCardChangeEffect
    | ProduceCardDeleteEffect
    | OpaqueCatalogEffect
)


@dataclass(frozen=True, slots=True)
class DirectSuggestionFields:
    """Raw direct Master fields, not server-effective costs."""

    raw_stamina_cost: int
    raw_produce_point_cost: int
    produce_card_id: str
    produce_card_upgrade_count: int

    def __post_init__(self) -> None:
        _integer(self.raw_stamina_cost, "raw stamina cost", minimum=0)
        _integer(self.raw_produce_point_cost, "raw produce-point cost", minimum=0)
        _text(self.produce_card_id, "direct produce-card id", empty=True)
        _integer(
            self.produce_card_upgrade_count,
            "direct produce-card upgrade count",
            minimum=0,
        )


@dataclass(frozen=True, slots=True)
class InitialRegularEventEffectCatalog:
    """Three ordered effect buckets for one exact selected outcome."""

    detail_id: str
    suggestion_id: str
    suggestion_index: int
    actual_success: bool
    always_successful: bool
    success_probability_permyriad: int
    suggestion_type: str
    produce_story_id: str
    event_type: str
    direct: DirectSuggestionFields
    detail_effects: tuple[P0CatalogEffect, ...]
    base_effects: tuple[P0CatalogEffect, ...]
    success_or_fail_effects: tuple[P0CatalogEffect, ...]
    selected_outcome: str
    direct_next_step: NextStepRef | None
    success_or_fail_next_step: NextStepRef | None
    produce_effect_fire_step: int
    is_campaign: bool

    def __post_init__(self) -> None:
        if self.detail_id not in FKTN_INITIAL_REGULAR_EVENT_DETAIL_ID_SET:
            raise ValueError("catalog detail is outside the fixed FKTN allowlist")
        _text(self.suggestion_id, "catalog suggestion id")
        _integer(self.suggestion_index, "catalog suggestion index", minimum=0)
        _boolean(self.actual_success, "catalog actual success")
        _boolean(self.always_successful, "catalog always successful")
        probability = _integer(
            self.success_probability_permyriad,
            "catalog success probability",
            minimum=0,
        )
        if probability > 10_000:
            raise ValueError("catalog success probability exceeds 10000")
        _text(self.suggestion_type, "catalog suggestion type")
        _text(self.produce_story_id, "catalog produce-story id")
        _text(self.event_type, "catalog event type")
        if not isinstance(self.direct, DirectSuggestionFields):
            raise TypeError("catalog direct fields must be typed")
        for name in (
            "detail_effects",
            "base_effects",
            "success_or_fail_effects",
        ):
            values = tuple(getattr(self, name))
            if not all(isinstance(value, CatalogEffect) for value in values):
                raise TypeError(f"{name} must contain typed catalog effects")
            object.__setattr__(self, name, values)
        if self.selected_outcome not in {"success", "fail"}:
            raise ValueError("selected outcome must be success or fail")
        if (self.selected_outcome == "success") != self.actual_success:
            raise ValueError("selected outcome differs from actual success")
        for name in ("direct_next_step", "success_or_fail_next_step"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, NextStepRef):
                raise TypeError(f"{name} must be NextStepRef or None")
        _integer(
            self.produce_effect_fire_step,
            "catalog produce-effect fire step",
            minimum=0,
        )
        _boolean(self.is_campaign, "catalog is_campaign")

    @property
    def blocked(self) -> bool:
        """Whether any bucket contains an explicitly opaque effect."""

        return any(
            isinstance(value, OpaqueCatalogEffect)
            for bucket in (
                self.detail_effects,
                self.base_effects,
                self.success_or_fail_effects,
            )
            for value in bucket
        )


def _embedded_rewards(raw: object, effect_id: str) -> tuple[EmbeddedProduceReward, ...]:
    result: list[EmbeddedProduceReward] = []
    for index, value in enumerate(_json_list(raw, f"{effect_id}.produceRewards")):
        if not isinstance(value, Mapping):
            raise InitialRegularEventEffectCatalogError(
                "invalid-embedded-reward",
                f"{effect_id}.produceRewards[{index}] is not an object",
            )
        required = {"resourceType", "resourceId", "resourceLevel"}
        if not required.issubset(value):
            raise InitialRegularEventEffectCatalogError(
                "invalid-embedded-reward",
                f"{effect_id}.produceRewards[{index}] lacks {sorted(required - set(value))}",
            )
        try:
            result.append(
                EmbeddedProduceReward(
                    resource_type=_text(
                        value["resourceType"], "embedded reward resource type"
                    ),
                    resource_id=_text(
                        value["resourceId"],
                        "embedded reward resource id",
                        empty=True,
                    ),
                    resource_level=_integer(
                        value["resourceLevel"],
                        "embedded reward resource level",
                        minimum=0,
                    ),
                )
            )
        except (TypeError, ValueError) as error:
            raise InitialRegularEventEffectCatalogError(
                "invalid-embedded-reward", f"{effect_id}: {error}"
            ) from error
    return tuple(result)


def _catalog_effect(row: sqlite3.Row) -> P0CatalogEffect:
    effect_id = _text(row["id"], "effect id")
    effect_type = _text(row["effect_type"], "effect type")
    common: dict[str, object] = {
        "effect_id": effect_id,
        "effect_type": effect_type,
        "effect_value_min": _integer(row["effect_value_min"], "effect value min"),
        "effect_value_max": _integer(row["effect_value_max"], "effect value max"),
        "resource_type": _text(row["resource_type"], "effect resource type"),
        "embedded_rewards": _embedded_rewards(row["rewards_json"], effect_id),
        "card_search_id": _text(
            row["card_search_id"], "effect card-search id", empty=True
        ),
        "exam_status_enchant_id": _text(
            row["exam_status_enchant_id"],
            "effect exam-status-enchant id",
            empty=True,
        ),
        "step_event_detail_id": _text(
            row["step_event_detail_id"],
            "effect step-event-detail id",
            empty=True,
        ),
        "pick_range_type": _text(row["pick_range_type"], "effect pick-range type"),
        "pick_count_min": _integer(
            row["pick_count_min"], "effect pick-count min", minimum=0
        ),
        "pick_count_max": _integer(
            row["pick_count_max"], "effect pick-count max", minimum=0
        ),
        "is_research": _sqlite_boolean(row["is_research"], "effect is_research"),
    }
    attribute = _ATTRIBUTE_EFFECT_TYPES.get(effect_type)
    if attribute is not None:
        return AttributeAdditionEffect(**common, attribute=attribute)  # type: ignore[arg-type]
    effect_class: type[CatalogEffect] | None = {
        _STAMINA_RECOVER_MULTIPLE: StaminaRecoverMultipleEffect,
        _PRODUCE_REWARD: ProduceRewardEffect,
        _PRODUCE_REWARD_SET: ProduceRewardSetEffect,
        _PRODUCE_CARD_UPGRADE: ProduceCardUpgradeEffect,
        _PRODUCE_CARD_CHANGE: ProduceCardChangeEffect,
        _PRODUCE_CARD_DELETE: ProduceCardDeleteEffect,
    }.get(effect_type)
    if effect_class is not None:
        return effect_class(**common)  # type: ignore[arg-type,return-value]
    blocker = EffectCatalogBlocker(
        code="unsupported-effect-type",
        message="outer mutation semantics are not implemented by the P0 catalog",
        effect_id=effect_id,
        effect_type=effect_type,
    )
    return OpaqueCatalogEffect(**common, blocker=blocker)  # type: ignore[arg-type]


def _load_effects(
    connection: sqlite3.Connection,
    effect_ids: tuple[str, ...],
    bucket: str,
) -> tuple[P0CatalogEffect, ...]:
    result: list[P0CatalogEffect] = []
    for index, effect_id in enumerate(effect_ids):
        row = connection.execute(
            """
            SELECT id, effect_type, effect_value_min, effect_value_max,
                   resource_type, rewards_json, card_search_id,
                   exam_status_enchant_id, step_event_detail_id,
                   pick_range_type, pick_count_min, pick_count_max, is_research
              FROM produce_effect
             WHERE id = ?
            """,
            (effect_id,),
        ).fetchone()
        if row is None:
            raise InitialRegularEventEffectCatalogError(
                "effect-row-missing",
                f"{bucket}[{index}] references missing effect {effect_id}",
            )
        result.append(_catalog_effect(row))
    return tuple(result)


def _next_step(step_type: object, step_id: object, name: str) -> NextStepRef | None:
    raw_type = _text(step_type, f"{name} type")
    raw_id = _text(step_id, f"{name} id", empty=True)
    if not raw_id:
        return None
    if raw_type == "ProduceStepType_Unknown":
        raise InitialRegularEventEffectCatalogError(
            "continuation-type-unknown", f"{name} has an id but Unknown type"
        )
    return NextStepRef(step_type=raw_type, step_id=raw_id)


def _validate_actual_success(
    *,
    actual_success: bool,
    always_successful: bool,
    probability: int,
) -> None:
    if always_successful and not actual_success:
        raise InitialRegularEventEffectCatalogError(
            "impossible-event-outcome", "always-successful suggestion cannot fail"
        )
    if not always_successful and probability == 10_000 and not actual_success:
        raise InitialRegularEventEffectCatalogError(
            "impossible-event-outcome", "10000-permyriad suggestion cannot fail"
        )
    if not always_successful and probability == 0 and actual_success:
        raise InitialRegularEventEffectCatalogError(
            "impossible-event-outcome", "zero-permyriad suggestion cannot succeed"
        )


def load_initial_regular_event_effect_catalog(
    detail_id: str,
    suggestion_id: str,
    *,
    actual_success: bool,
    database: Path = DEFAULT_DATABASE,
) -> InitialRegularEventEffectCatalog:
    """Load one exact FKTN detail/suggestion outcome from imported PC Master."""

    detail_id = _text(detail_id, "event detail id")
    suggestion_id = _text(suggestion_id, "event suggestion id")
    actual_success = _boolean(actual_success, "actual success")
    if detail_id not in FKTN_INITIAL_REGULAR_EVENT_DETAIL_ID_SET:
        raise InitialRegularEventEffectCatalogError(
            "detail-outside-fktn-p0",
            f"detail is not one of the 47 fixed FKTN rows: {detail_id}",
        )
    database = Path(database)
    if not database.is_file():
        raise InitialRegularEventEffectCatalogError(
            "master-database-missing", str(database)
        )

    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        detail = connection.execute(
            """
            SELECT id, suggestion_type, produce_story_id,
                   produce_effect_ids_json, suggestion_ids_json, event_type
              FROM step_event_detail
             WHERE id = ?
            """,
            (detail_id,),
        ).fetchone()
        if detail is None:
            raise InitialRegularEventEffectCatalogError(
                "detail-row-missing", detail_id
            )
        suggestion_ids = _id_list(
            detail["suggestion_ids_json"], f"{detail_id}.suggestion_ids"
        )
        matching_indexes = tuple(
            index for index, value in enumerate(suggestion_ids) if value == suggestion_id
        )
        if len(matching_indexes) != 1:
            raise InitialRegularEventEffectCatalogError(
                "suggestion-not-in-detail",
                f"{suggestion_id} occurs {len(matching_indexes)} times in {detail_id}",
            )
        suggestion_index = matching_indexes[0]
        suggestion = connection.execute(
            """
            SELECT id, produce_point, stamina, produce_card_id,
                   produce_card_upgrade_count, produce_effect_ids_json,
                   step_type, step_id, success_probability_permyriad,
                   success_produce_effect_ids_json, success_step_type,
                   success_step_id, fail_produce_effect_ids_json,
                   fail_step_type, fail_step_id, always_successful,
                   produce_effect_fire_step, is_campaign
              FROM step_event_suggestion
             WHERE id = ?
            """,
            (suggestion_id,),
        ).fetchone()
        if suggestion is None:
            raise InitialRegularEventEffectCatalogError(
                "suggestion-row-missing", suggestion_id
            )

        probability = _integer(
            suggestion["success_probability_permyriad"],
            "success probability",
            minimum=0,
        )
        if probability > 10_000:
            raise InitialRegularEventEffectCatalogError(
                "invalid-success-probability", str(probability)
            )
        always_successful = _sqlite_boolean(
            suggestion["always_successful"], "suggestion always_successful"
        )
        _validate_actual_success(
            actual_success=actual_success,
            always_successful=always_successful,
            probability=probability,
        )

        detail_ids = _id_list(
            detail["produce_effect_ids_json"], f"{detail_id}.detail_effects"
        )
        base_ids = _id_list(
            suggestion["produce_effect_ids_json"],
            f"{suggestion_id}.base_effects",
        )
        if actual_success:
            outcome_ids = _id_list(
                suggestion["success_produce_effect_ids_json"],
                f"{suggestion_id}.success_effects",
            )
            outcome_step = _next_step(
                suggestion["success_step_type"],
                suggestion["success_step_id"],
                "success continuation",
            )
            selected_outcome = "success"
        else:
            outcome_ids = _id_list(
                suggestion["fail_produce_effect_ids_json"],
                f"{suggestion_id}.fail_effects",
            )
            outcome_step = _next_step(
                suggestion["fail_step_type"],
                suggestion["fail_step_id"],
                "fail continuation",
            )
            selected_outcome = "fail"

        return InitialRegularEventEffectCatalog(
            detail_id=detail_id,
            suggestion_id=suggestion_id,
            suggestion_index=suggestion_index,
            actual_success=actual_success,
            always_successful=always_successful,
            success_probability_permyriad=probability,
            suggestion_type=_text(
                detail["suggestion_type"], "detail suggestion type"
            ),
            produce_story_id=_text(detail["produce_story_id"], "produce-story id"),
            event_type=_text(detail["event_type"], "detail event type"),
            direct=DirectSuggestionFields(
                raw_stamina_cost=_integer(
                    suggestion["stamina"], "raw stamina cost", minimum=0
                ),
                raw_produce_point_cost=_integer(
                    suggestion["produce_point"],
                    "raw produce-point cost",
                    minimum=0,
                ),
                produce_card_id=_text(
                    suggestion["produce_card_id"],
                    "direct produce-card id",
                    empty=True,
                ),
                produce_card_upgrade_count=_integer(
                    suggestion["produce_card_upgrade_count"],
                    "direct produce-card upgrade count",
                    minimum=0,
                ),
            ),
            detail_effects=_load_effects(connection, detail_ids, "detail_effects"),
            base_effects=_load_effects(connection, base_ids, "base_effects"),
            success_or_fail_effects=_load_effects(
                connection, outcome_ids, "success_or_fail_effects"
            ),
            selected_outcome=selected_outcome,
            direct_next_step=_next_step(
                suggestion["step_type"],
                suggestion["step_id"],
                "direct continuation",
            ),
            success_or_fail_next_step=outcome_step,
            produce_effect_fire_step=_integer(
                suggestion["produce_effect_fire_step"],
                "produce-effect fire step",
                minimum=0,
            ),
            is_campaign=_sqlite_boolean(
                suggestion["is_campaign"], "suggestion is_campaign"
            ),
        )


__all__ = [
    "AttributeAdditionEffect",
    "CatalogEffect",
    "DirectSuggestionFields",
    "EffectCatalogBlocker",
    "EmbeddedProduceReward",
    "FKTN_INITIAL_REGULAR_EVENT_DETAIL_IDS",
    "FKTN_INITIAL_REGULAR_EVENT_DETAIL_ID_SET",
    "InitialRegularEventEffectCatalog",
    "InitialRegularEventEffectCatalogError",
    "OpaqueCatalogEffect",
    "P0CatalogEffect",
    "ProduceCardChangeEffect",
    "ProduceCardDeleteEffect",
    "ProduceCardUpgradeEffect",
    "ProduceRewardEffect",
    "ProduceRewardSetEffect",
    "ScenarioInputRequirement",
    "StaminaRecoverMultipleEffect",
    "load_initial_regular_event_effect_catalog",
]
