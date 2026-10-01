"""Pure-data expected surface chains for one selected N.I.A. outer action.

The compiler deliberately does not recognize screens.  A weekly action has one
fixed main chain, while optional reward/effect bindings append only their typed
expected surfaces.  Business reward associations come from
:mod:`nia_reward_catalog`; Outing effects are resolved from their ordered,
pinned Master IDs.  Every other effect must already be bound by its caller.
Unknown effect families fail closed.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

from .master_db import DEFAULT_DATABASE
from .nia_outer_resource_model import _master_by_id, load_nia_outing_option_effect
from .nia_reward_catalog import (
    CatalogBlocker,
    RewardSetMetadata,
    lookup_business_suggestion,
    lookup_reward_set,
)
from .nia_static_adapter import DEFAULT_MASTER_DIR
from .overview_actions import (
    ACTIVITY,
    DANCE_LESSON,
    REST,
    VOCAL_LESSON,
    VISUAL_LESSON,
)
from .route_calendar import BUSINESS, CONSULTATION, OUTING, SPECIAL_GUIDANCE


EXAM: Final = "exam"
LESSON_ACTIONS: Final = frozenset(
    {VOCAL_LESSON, DANCE_LESSON, VISUAL_LESSON}
)

FAMILY_CARD: Final = "card"
FAMILY_DRINK: Final = "drink"
FAMILY_ITEM: Final = "item"
FAMILY_STRENGTHEN: Final = "strengthen"
FAMILY_DELETE: Final = "delete"
FAMILY_CHANGE: Final = "change"
FAMILY_NUMERIC: Final = "numeric"

_KNOWN_FAMILIES: Final = frozenset(
    {
        FAMILY_CARD,
        FAMILY_DRINK,
        FAMILY_ITEM,
        FAMILY_STRENGTHEN,
        FAMILY_DELETE,
        FAMILY_CHANGE,
        FAMILY_NUMERIC,
    }
)
_RESOURCE_FAMILY: Final = {
    "ProduceResourceType_ProduceCard": FAMILY_CARD,
    "ProduceResourceType_ProduceDrink": FAMILY_DRINK,
    "ProduceResourceType_ProduceItem": FAMILY_ITEM,
}
_STAMINA_RECOVER_FIX: Final = "ProduceEffectType_StaminaRecoverFix"
_STAMINA_RECOVER_MULTIPLE: Final = "ProduceEffectType_StaminaRecoverMultiple"
_PRODUCE_REWARD_SET: Final = "ProduceEffectType_ProduceRewardSet"
_PRODUCE_CARD_UPGRADE: Final = "ProduceEffectType_ProduceCardUpgrade"
_PRODUCE_CARD_DELETE: Final = "ProduceEffectType_ProduceCardDelete"
_PRODUCE_CARD_CHANGE: Final = "ProduceEffectType_ProduceCardChange"
_PRODUCE_CARD_CHANGE_UPGRADE: Final = "ProduceEffectType_ProduceCardChangeUpgrade"


@dataclass(frozen=True, slots=True)
class ExpectedSurface:
    """One ordered semantic surface, independent of image/OCR implementation."""

    kind: str
    target: str
    source_id: str
    ordinal: int = 1
    optional: bool = False

    def __post_init__(self) -> None:
        for name in ("kind", "target", "source_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"expected surface {name} must be non-empty text")
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int):
            raise TypeError("expected surface ordinal must be an integer")
        if self.ordinal < 1:
            raise ValueError("expected surface ordinal must be positive")
        if type(self.optional) is not bool:
            raise TypeError("expected surface optional must be boolean")


@dataclass(frozen=True, slots=True)
class EffectBinding:
    """One caller- or Master-proven effect in official execution order."""

    effect_id: str
    family: str
    count: int = 1
    numeric_key: str | None = None
    numeric_value: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.effect_id, str) or not self.effect_id:
            raise ValueError("effect_id must be non-empty text")
        if not isinstance(self.family, str) or not self.family:
            raise ValueError("effect family must be non-empty text")
        if isinstance(self.count, bool) or not isinstance(self.count, int):
            raise TypeError("effect count must be an integer")
        if self.count < 1:
            raise ValueError("effect count must be positive")
        if self.numeric_key is not None and (
            not isinstance(self.numeric_key, str) or not self.numeric_key
        ):
            raise ValueError("numeric_key must be non-empty text or None")
        if self.numeric_value is not None and (
            isinstance(self.numeric_value, bool)
            or not isinstance(self.numeric_value, int)
        ):
            raise TypeError("numeric_value must be an integer or None")


@dataclass(frozen=True, slots=True)
class SelectedAction:
    """One selected outer action plus already-bound effect authorities."""

    action: str
    suggestion_id: str | None = None
    reward_effect_ids: tuple[str, ...] = ()
    effects: tuple[EffectBinding, ...] = ()
    max_stamina: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.action, str) or not self.action:
            raise ValueError("selected action must be non-empty text")
        if self.suggestion_id is not None and (
            not isinstance(self.suggestion_id, str) or not self.suggestion_id
        ):
            raise ValueError("suggestion_id must be non-empty text or None")
        rewards = tuple(self.reward_effect_ids)
        if any(not isinstance(value, str) or not value for value in rewards):
            raise ValueError("reward_effect_ids must contain non-empty text")
        if len(rewards) != len(set(rewards)):
            raise ValueError("reward_effect_ids must not contain duplicates")
        effects = tuple(self.effects)
        if any(not isinstance(value, EffectBinding) for value in effects):
            raise TypeError("effects must contain EffectBinding values")
        object.__setattr__(self, "reward_effect_ids", rewards)
        object.__setattr__(self, "effects", effects)
        if self.max_stamina is not None and (
            isinstance(self.max_stamina, bool)
            or not isinstance(self.max_stamina, int)
            or self.max_stamina <= 0
        ):
            raise ValueError("max_stamina must be positive or None")


@dataclass(frozen=True, slots=True)
class EffectDrivenExpectedSequence:
    bindings: tuple[EffectBinding, ...]
    surfaces: tuple[ExpectedSurface, ...]
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        bindings = tuple(self.bindings)
        surfaces = tuple(self.surfaces)
        evidence_ids = tuple(self.evidence_ids)
        if any(not isinstance(value, EffectBinding) for value in bindings):
            raise TypeError("sequence bindings must be EffectBinding values")
        if any(not isinstance(value, ExpectedSurface) for value in surfaces):
            raise TypeError("sequence surfaces must be ExpectedSurface values")
        if any(not isinstance(value, str) or not value for value in evidence_ids):
            raise ValueError("sequence evidence_ids must contain non-empty text")
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("sequence evidence_ids must not contain duplicates")
        object.__setattr__(self, "bindings", bindings)
        object.__setattr__(self, "surfaces", surfaces)
        object.__setattr__(self, "evidence_ids", evidence_ids)


@dataclass(frozen=True, slots=True)
class SelectedActionChain:
    selected: SelectedAction
    main_surfaces: tuple[ExpectedSurface, ...]
    effects: EffectDrivenExpectedSequence

    def __post_init__(self) -> None:
        if not isinstance(self.selected, SelectedAction):
            raise TypeError("chain selected must be SelectedAction")
        main = tuple(self.main_surfaces)
        if (
            not main
            or any(not isinstance(value, ExpectedSurface) for value in main)
            or main[-1].kind != "outer-settled"
        ):
            raise ValueError("main chain must end in outer-settled")
        if not isinstance(self.effects, EffectDrivenExpectedSequence):
            raise TypeError("chain effects must be EffectDrivenExpectedSequence")
        object.__setattr__(self, "main_surfaces", main)

    @property
    def surfaces(self) -> tuple[ExpectedSurface, ...]:
        # Every fixed main chain ends in the same server-owned settlement.
        # Effect-driven UI/evidence belongs immediately before that terminal.
        ordered = (
            *self.main_surfaces[:-1],
            *self.effects.surfaces,
            self.main_surfaces[-1],
        )
        # ``ordinal`` is the public, stable cursor over the complete sequence.
        # It never resets for another effect or for the terminal surface.
        return tuple(
            value if value.ordinal == index else replace(value, ordinal=index)
            for index, value in enumerate(ordered, start=1)
        )


@dataclass(frozen=True, slots=True)
class ActionChainIssue:
    code: str
    detail: str
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not self.code:
            raise ValueError("issue code must be non-empty text")
        if not isinstance(self.detail, str) or not self.detail:
            raise ValueError("issue detail must be non-empty text")
        evidence_ids = tuple(self.evidence_ids)
        if any(not isinstance(value, str) or not value for value in evidence_ids):
            raise ValueError("issue evidence_ids must contain non-empty text")
        object.__setattr__(self, "evidence_ids", evidence_ids)


@dataclass(frozen=True, slots=True)
class ActionChainCompilation:
    chain: SelectedActionChain | None
    issues: tuple[ActionChainIssue, ...] = ()

    def __post_init__(self) -> None:
        if (self.chain is None) == (not self.issues):
            raise ValueError("compilation must contain exactly one of chain or issues")

    @property
    def ready(self) -> bool:
        return self.chain is not None and not self.issues


_MAIN_CHAIN: Final = {
    BUSINESS: (
        ("outer-action", "business"),
        ("business-choice", "suggestion"),
        ("work-start", "start"),
        ("outer-settled", "business"),
    ),
    ACTIVITY: (
        ("outer-action", "activity"),
        ("activity-story", "fan-present"),
        ("outer-settled", "activity"),
    ),
    OUTING: (
        ("outer-action", "outing"),
        ("outing-choice", "suggestion"),
        ("outer-settled", "outing"),
    ),
    VOCAL_LESSON: (
        ("outer-action", "lesson"),
        ("lesson-start", "lesson"),
        ("lesson-result", "self-lesson-result"),
        ("outer-settled", "lesson"),
    ),
    DANCE_LESSON: (
        ("outer-action", "lesson"),
        ("lesson-start", "lesson"),
        ("lesson-result", "self-lesson-result"),
        ("outer-settled", "lesson"),
    ),
    VISUAL_LESSON: (
        ("outer-action", "lesson"),
        ("lesson-start", "lesson"),
        ("lesson-result", "self-lesson-result"),
        ("outer-settled", "lesson"),
    ),
    CONSULTATION: (
        ("outer-action", "consultation"),
        ("consultation-shop", "shop"),
        ("shop-exit", "exit"),
        ("outer-settled", "consultation"),
    ),
    SPECIAL_GUIDANCE: (
        ("outer-action", "guidance"),
        ("card-operation-page", "customize"),
        ("card-operation-submit", "customize"),
        ("card-customize-option-select", "customize-option"),
        ("card-customize-option-execute", "execute"),
        ("card-customize-confirm-execute", "confirm"),
        ("outer-settled", "guidance"),
    ),
    REST: (
        ("outer-action", "rest"),
        ("rest-confirm", "rest-confirm"),
        ("outer-settled", "rest"),
    ),
    EXAM: (
        ("outer-action", "exam"),
        ("mirror-choice", "difficulty"),
        ("exam", "performance"),
        ("exam-result", "terminal"),
        ("outer-settled", "exam"),
    ),
}


def _main_surfaces(action: str) -> tuple[ExpectedSurface, ...] | None:
    rows = _MAIN_CHAIN.get(action)
    if rows is None:
        return None
    return tuple(
        ExpectedSurface(
            kind,
            target,
            f"main:{action}",
            ordinal=index,
            optional=(
                action == SPECIAL_GUIDANCE
                and kind == "card-customize-confirm-execute"
            ),
        )
        for index, (kind, target) in enumerate(rows, start=1)
    )


def _binding_surfaces(
    binding: EffectBinding,
    *,
    start_ordinal: int,
) -> tuple[ExpectedSurface, ...] | None:
    family = binding.family
    if family not in _KNOWN_FAMILIES:
        return None
    result: list[ExpectedSurface] = []
    next_ordinal = start_ordinal
    for _ in range(binding.count):
        if family == FAMILY_CARD:
            rows = (("reward-choice", "card"), ("reward-confirm", "cards-get"))
        elif family == FAMILY_DRINK:
            rows = (
                ("item-reward-choice", "drink"),
                ("item-reward-confirm", "drink"),
            )
        elif family == FAMILY_ITEM:
            rows = (
                ("item-reward-choice", "item"),
                ("item-reward-confirm", "item"),
            )
        elif family == FAMILY_STRENGTHEN:
            rows = (
                ("card-operation-page", "strengthen"),
                ("card-operation-submit", "strengthen"),
            )
        elif family == FAMILY_DELETE:
            rows = (
                ("card-operation-page", "delete"),
                ("card-operation-submit", "delete"),
            )
        elif family == FAMILY_CHANGE:
            rows = (
                ("card-operation-page", "exchange"),
                ("card-operation-submit", "exchange"),
            )
        else:
            if binding.numeric_key is None or binding.numeric_value is None:
                return None
            rows = (("numeric-settlement", binding.numeric_key),)
        for kind, target in rows:
            result.append(
                ExpectedSurface(
                    kind,
                    target,
                    binding.effect_id,
                    ordinal=next_ordinal,
                )
            )
            next_ordinal += 1
        if family == FAMILY_DRINK:
            result.append(
                ExpectedSurface(
                    "drink-keep",
                    "capacity-resolution",
                    binding.effect_id,
                    ordinal=next_ordinal,
                    optional=True,
                )
            )
            next_ordinal += 1
    return tuple(result)


def _exact_effect_int(
    row: object,
    field: str,
    *,
    effect_id: str,
) -> int | ActionChainIssue:
    value = row.get(field) if hasattr(row, "get") else None
    if isinstance(value, bool) or not isinstance(value, int):
        return ActionChainIssue(
            "outing-effect-field-invalid",
            f"{effect_id}.{field} must be an integer",
            (effect_id,),
        )
    return value


def _compile_ordered_outing_effects(
    *,
    effect_ids: tuple[str, ...],
    max_stamina: int,
    master_dir: Path,
) -> tuple[tuple[EffectBinding, ...], tuple[ActionChainIssue, ...]]:
    """Bind each official Outing effect without aggregating or reordering it."""

    try:
        effect_rows = _master_by_id(master_dir / "ProduceEffect.yaml")
    except (OSError, ValueError) as error:
        return (), (
            ActionChainIssue(
                "outing-effect-master-unavailable",
                f"ProduceEffect Master could not be loaded: {type(error).__name__}: {error}",
                effect_ids,
            ),
        )

    bindings: list[EffectBinding] = []
    issues: list[ActionChainIssue] = []
    for effect_index, effect_id in enumerate(effect_ids):
        row = effect_rows.get(effect_id)
        if row is None:
            issues.append(
                ActionChainIssue(
                    "outing-effect-master-missing",
                    f"effect[{effect_index}] references missing effect {effect_id}",
                    (effect_id,),
                )
            )
            continue

        effect_type = row.get("produceEffectType")
        if not isinstance(effect_type, str) or not effect_type:
            issues.append(
                ActionChainIssue(
                    "outing-effect-type-invalid",
                    f"{effect_id}.produceEffectType must be non-empty text",
                    (effect_id,),
                )
            )
            continue

        minimum = _exact_effect_int(row, "effectValueMin", effect_id=effect_id)
        maximum = _exact_effect_int(row, "effectValueMax", effect_id=effect_id)
        pick_min = _exact_effect_int(row, "pickCountMin", effect_id=effect_id)
        pick_max = _exact_effect_int(row, "pickCountMax", effect_id=effect_id)
        invalid = tuple(
            value
            for value in (minimum, maximum, pick_min, pick_max)
            if isinstance(value, ActionChainIssue)
        )
        if invalid:
            issues.extend(invalid)
            continue
        assert isinstance(minimum, int)
        assert isinstance(maximum, int)
        assert isinstance(pick_min, int)
        assert isinstance(pick_max, int)
        if minimum != maximum:
            issues.append(
                ActionChainIssue(
                    "outing-effect-value-not-exact",
                    f"{effect_id} value is {minimum}..{maximum}",
                    (effect_id,),
                )
            )
            continue
        if pick_min != pick_max:
            issues.append(
                ActionChainIssue(
                    "outing-effect-count-not-exact",
                    f"{effect_id} count is {pick_min}..{pick_max}",
                    (effect_id,),
                )
            )
            continue

        if effect_type == _STAMINA_RECOVER_MULTIPLE:
            bindings.append(
                EffectBinding(
                    effect_id,
                    FAMILY_NUMERIC,
                    numeric_key="stamina",
                    numeric_value=max_stamina * minimum // 1000,
                )
            )
            continue
        if effect_type == _STAMINA_RECOVER_FIX:
            bindings.append(
                EffectBinding(
                    effect_id,
                    FAMILY_NUMERIC,
                    numeric_key="stamina",
                    numeric_value=minimum,
                )
            )
            continue
        if effect_type == _PRODUCE_REWARD_SET:
            family = _RESOURCE_FAMILY.get(row.get("produceResourceType"))
            if family is not None and pick_min >= 1:
                bindings.append(EffectBinding(effect_id, family, pick_min))
                continue
        elif effect_type == _PRODUCE_CARD_UPGRADE and pick_min >= 1:
            bindings.append(EffectBinding(effect_id, FAMILY_STRENGTHEN, pick_min))
            continue
        elif effect_type == _PRODUCE_CARD_DELETE and pick_min >= 1:
            bindings.append(EffectBinding(effect_id, FAMILY_DELETE, pick_min))
            continue
        elif effect_type in {
            _PRODUCE_CARD_CHANGE,
            _PRODUCE_CARD_CHANGE_UPGRADE,
        } and pick_min >= 1:
            bindings.append(EffectBinding(effect_id, FAMILY_CHANGE, pick_min))
            continue

        issues.append(
            ActionChainIssue(
                "outing-effect-unresolved",
                (
                    f"effect[{effect_index}] {effect_id} ({effect_type}) cannot be "
                    "uniquely bound to an expected surface"
                ),
                (effect_id,),
            )
        )

    if issues:
        return (), tuple(issues)
    return tuple(bindings), ()


def _issue_from_catalog(blocker: CatalogBlocker) -> ActionChainIssue:
    return ActionChainIssue(
        f"reward-catalog:{blocker.code}",
        blocker.message,
        blocker.evidence_ids,
    )


def _reward_binding(metadata: RewardSetMetadata) -> EffectBinding | ActionChainIssue:
    family = _RESOURCE_FAMILY.get(metadata.resource_type)
    if family is None:
        return ActionChainIssue(
            "reward-resource-unsupported",
            f"unsupported reward resource {metadata.resource_type}",
            metadata.evidence_ids,
        )
    if (
        metadata.pick_count_min != metadata.pick_count_max
        or metadata.pick_count_min < 1
    ):
        return ActionChainIssue(
            "reward-count-not-exact",
            (
                f"{metadata.effect_id} pick count is "
                f"{metadata.pick_count_min}..{metadata.pick_count_max}"
            ),
            metadata.evidence_ids,
        )
    return EffectBinding(metadata.effect_id, family, metadata.pick_count_min)


def _compile_reward_effect(
    effect_id: str,
    *,
    database: Path,
) -> EffectBinding | tuple[ActionChainIssue, ...]:
    result = lookup_reward_set(effect_id, database)
    if not result.ready or result.value is None:
        return tuple(_issue_from_catalog(blocker) for blocker in result.blockers)
    compiled = _reward_binding(result.value)
    return (compiled,) if isinstance(compiled, ActionChainIssue) else compiled


def _compile_suggestion(
    selected: SelectedAction,
    *,
    database: Path,
    master_dir: Path,
) -> tuple[tuple[EffectBinding, ...], tuple[ActionChainIssue, ...], tuple[str, ...]]:
    suggestion_id = selected.suggestion_id
    if suggestion_id is None:
        return (), (), ()
    if selected.action == BUSINESS:
        result = lookup_business_suggestion(suggestion_id, database)
        if not result.ready or result.value is None:
            return (), tuple(_issue_from_catalog(value) for value in result.blockers), ()
        compiled = _reward_binding(result.value.reward_set)
        if isinstance(compiled, ActionChainIssue):
            return (), (compiled,), ()
        return (compiled,), (), result.value.evidence_ids
    if selected.action == OUTING:
        if selected.max_stamina is None:
            return (), (
                ActionChainIssue(
                    "outing-max-stamina-missing",
                    "Outing suggestion requires max_stamina",
                    (suggestion_id,),
                ),
            ), ()
        effect = load_nia_outing_option_effect(
            suggestion_id,
            max_stamina=selected.max_stamina,
            master_dir=master_dir,
        )
        try:
            suggestion_row = _master_by_id(
                master_dir / "ProduceStepEventSuggestion.yaml"
            ).get(suggestion_id)
        except (OSError, ValueError) as error:
            return (), (
                ActionChainIssue(
                    "outing-suggestion-master-unavailable",
                    f"Outing suggestion Master could not be loaded: {error}",
                    (suggestion_id,),
                ),
            ), ()
        if suggestion_row is None:
            return (), (
                ActionChainIssue(
                    "outing-suggestion-master-missing",
                    f"Outing suggestion is missing: {suggestion_id}",
                    (suggestion_id,),
                ),
            ), ()
        success_effect_ids = tuple(
            value
            for value in suggestion_row.get("successProduceEffectIds", ())
            if isinstance(value, str) and value
        )
        fail_effect_ids = tuple(
            value
            for value in suggestion_row.get("failProduceEffectIds", ())
            if isinstance(value, str) and value
        )
        if success_effect_ids or fail_effect_ids:
            return (), (
                ActionChainIssue(
                    "outing-conditional-effect-runtime-branch-required",
                    (
                        "Outing has success/fail effects; the actual runtime "
                        "response must select the executed branch"
                    ),
                    (
                        suggestion_id,
                        *success_effect_ids,
                        *fail_effect_ids,
                    ),
                ),
            ), ()
        # ``load_nia_outing_option_effect`` predates this ordered compiler. Its
        # aggregate view intentionally calls card-change unmodeled and expects
        # exactly one stamina recovery. Neither is an action-chain rule: every
        # official effect row is parsed below, and an empty option is valid.
        legacy_only_issue_codes = {"nia-outing-recovery-not-exact"}
        loader_issues = tuple(
            ActionChainIssue(value.code, value.detail, value.evidence_ids)
            for value in effect.issues
            if value.code not in legacy_only_issue_codes
        )
        if loader_issues:
            return (), loader_issues, ()
        # Direct suggestion fields are not members of ``produceEffectIds``.
        # Keep those explicit, then append the official effect list exactly in
        # source order.  Never reconstruct the list from aggregate counters.
        bindings: list[EffectBinding] = []
        if effect.added_card_ids:
            bindings.append(
                EffectBinding(
                    "outing:added-card",
                    FAMILY_CARD,
                    len(effect.added_card_ids),
                )
            )
        ordered, ordered_issues = _compile_ordered_outing_effects(
            effect_ids=effect.produce_effect_ids,
            max_stamina=selected.max_stamina,
            master_dir=master_dir,
        )
        if ordered_issues:
            return (), ordered_issues, ()
        bindings.extend(ordered)
        if effect.produce_point_cost:
            bindings.append(
                EffectBinding(
                    "outing:produce-point-cost",
                    FAMILY_NUMERIC,
                    numeric_key="produce_points",
                    numeric_value=-effect.produce_point_cost,
                )
            )
        return tuple(bindings), (), (suggestion_id, *effect.produce_effect_ids)
    return (), (
        ActionChainIssue(
            "suggestion-action-unsupported",
            f"{selected.action} cannot bind suggestion {suggestion_id}",
            (suggestion_id,),
        ),
    ), ()


def compile_selected_action_chain(
    selected: SelectedAction,
    *,
    database: Path = DEFAULT_DATABASE,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> ActionChainCompilation:
    """Compile one fixed main chain and its typed effect-driven continuation."""

    if not isinstance(selected, SelectedAction):
        raise TypeError("selected must be SelectedAction")
    main = _main_surfaces(selected.action)
    if main is None:
        return ActionChainCompilation(
            None,
            (
                ActionChainIssue(
                    "selected-action-unsupported",
                    f"unsupported selected action {selected.action}",
                ),
            ),
        )

    bindings, issues, evidence_ids = _compile_suggestion(
        selected,
        database=Path(database),
        master_dir=Path(master_dir),
    )
    all_bindings = [*bindings, *selected.effects]
    all_evidence_ids = [*evidence_ids]
    all_issues = [*issues]
    for effect_id in selected.reward_effect_ids:
        compiled = _compile_reward_effect(effect_id, database=Path(database))
        if isinstance(compiled, EffectBinding):
            all_bindings.append(compiled)
            all_evidence_ids.append(effect_id)
        else:
            all_issues.extend(compiled)

    effect_surfaces: list[ExpectedSurface] = []
    next_surface_ordinal = len(main)
    for binding in all_bindings:
        surfaces = _binding_surfaces(
            binding,
            start_ordinal=next_surface_ordinal,
        )
        if surfaces is None:
            all_issues.append(
                ActionChainIssue(
                    (
                        "numeric-binding-incomplete"
                        if binding.family == FAMILY_NUMERIC
                        else "effect-family-unsupported"
                    ),
                    f"cannot compile {binding.effect_id} as {binding.family}",
                    (binding.effect_id,),
                )
            )
            continue
        effect_surfaces.extend(surfaces)
        next_surface_ordinal += len(surfaces)
        all_evidence_ids.append(binding.effect_id)

    if all_issues:
        return ActionChainCompilation(None, tuple(all_issues))
    sequence = EffectDrivenExpectedSequence(
        tuple(all_bindings),
        tuple(effect_surfaces),
        tuple(dict.fromkeys(all_evidence_ids)),
    )
    return ActionChainCompilation(
        SelectedActionChain(selected, main, sequence),
        (),
    )


__all__ = [
    "ACTIVITY",
    "ActionChainCompilation",
    "ActionChainIssue",
    "BUSINESS",
    "CONSULTATION",
    "EXAM",
    "EffectBinding",
    "EffectDrivenExpectedSequence",
    "ExpectedSurface",
    "FAMILY_CARD",
    "FAMILY_CHANGE",
    "FAMILY_DELETE",
    "FAMILY_DRINK",
    "FAMILY_ITEM",
    "FAMILY_NUMERIC",
    "FAMILY_STRENGTHEN",
    "LESSON_ACTIONS",
    "OUTING",
    "REST",
    "SPECIAL_GUIDANCE",
    "VOCAL_LESSON",
    "DANCE_LESSON",
    "VISUAL_LESSON",
    "SelectedAction",
    "SelectedActionChain",
    "compile_selected_action_chain",
]
