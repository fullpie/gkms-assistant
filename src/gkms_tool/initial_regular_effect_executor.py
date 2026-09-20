"""Pure, fail-closed reducer for resolved initial-regular event effects.

``InitialRegularEventScenario`` is the authoritative response/replay boundary.
This module combines it with a small typed Master projection, but performs no
Master/database I/O.  It only implements effect semantics that are either
fixed in Master or explicitly resolved by the scenario trace.
"""

from __future__ import annotations

from dataclasses import dataclass

from .initial_regular_event_scenario import (
    EffectResultChainError,
    InitialRegularEventScenario,
    InitialRegularPostState,
    ProduceCardSnapshot,
    ProduceEffectResultTrace,
    ProduceEffectStateSnapshot,
    ProduceRewardResultSnapshot,
    validate_effect_result_chain,
)
from .produce_rollout import (
    AttributeValues,
    DeckEntry,
    ProduceRolloutState,
    ProduceStatePatch,
)


VOCAL_ADDITION = "ProduceEffectType_VocalAddition"
DANCE_ADDITION = "ProduceEffectType_DanceAddition"
VISUAL_ADDITION = "ProduceEffectType_VisualAddition"
PRODUCE_REWARD = "ProduceEffectType_ProduceReward"
PRODUCE_REWARD_SET = "ProduceEffectType_ProduceRewardSet"
PRODUCE_CARD_UPGRADE = "ProduceEffectType_ProduceCardUpgrade"
PRODUCE_CARD_CHANGE = "ProduceEffectType_ProduceCardChange"
PRODUCE_CARD_DELETE = "ProduceEffectType_ProduceCardDelete"
STAMINA_RECOVER_MULTIPLE = "ProduceEffectType_StaminaRecoverMultiple"
PRODUCE_CARD_RESOURCE = "ProduceResourceType_ProduceCard"


_ATTRIBUTE_EFFECTS = {
    VOCAL_ADDITION: "vocal",
    DANCE_ADDITION: "dance",
    VISUAL_ADDITION: "visual",
}
_CARD_MUTATION_EFFECTS = {
    PRODUCE_CARD_UPGRADE,
    PRODUCE_CARD_CHANGE,
    PRODUCE_CARD_DELETE,
}
_SUPPORTED_EFFECTS = (
    set(_ATTRIBUTE_EFFECTS)
    | _CARD_MUTATION_EFFECTS
    | {PRODUCE_REWARD, PRODUCE_REWARD_SET, STAMINA_RECOVER_MULTIPLE}
)
_SCALAR_SNAPSHOT_FIELDS = (
    "max_stamina",
    "stamina",
    "produce_point",
    "vote_count",
    "star",
    "vocal",
    "dance",
    "visual",
    "high_score_gold",
)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _nonempty_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be non-empty text")
    return value


@dataclass(frozen=True, slots=True)
class InitialRegularMasterEffectSpec:
    """Minimal static projection for one response-ordered ProduceEffect."""

    produce_effect_id: str
    effect_type: str
    effect_value_min: int
    effect_value_max: int

    def __post_init__(self) -> None:
        _nonempty_text(self.produce_effect_id, "produce_effect_id")
        _nonempty_text(self.effect_type, "effect_type")
        if not _is_int(self.effect_value_min) or not _is_int(
            self.effect_value_max
        ):
            raise TypeError("effect value bounds must be integers")
        if self.effect_value_max < self.effect_value_min:
            raise ValueError("effect value bounds are reversed")

    @property
    def fixed_value(self) -> int | None:
        if self.effect_value_min != self.effect_value_max:
            return None
        return self.effect_value_min


@dataclass(frozen=True, slots=True)
class InitialRegularEventMasterProjection:
    """Master-only facts required by the event reducer.

    Direct stamina and produce-point values deliberately do not appear here:
    the reducer may consume only the server-effective costs in the scenario.
    ``effect_specs`` must already be ordered to match response EffectResults.
    """

    detail_id: str
    suggestion_id: str
    suggestion_index: int
    attribute_cap: int
    effect_specs: tuple[InitialRegularMasterEffectSpec, ...] = ()
    direct_card: ProduceCardSnapshot | None = None

    def __post_init__(self) -> None:
        _nonempty_text(self.detail_id, "detail_id")
        _nonempty_text(self.suggestion_id, "suggestion_id")
        if not _is_int(self.suggestion_index) or self.suggestion_index < 0:
            raise ValueError("suggestion_index must be a non-negative integer")
        if not _is_int(self.attribute_cap) or self.attribute_cap < 1:
            raise ValueError("attribute_cap must be a positive integer")
        specs = tuple(self.effect_specs)
        if not all(isinstance(value, InitialRegularMasterEffectSpec) for value in specs):
            raise TypeError("effect_specs must contain typed Master effect specs")
        object.__setattr__(self, "effect_specs", specs)
        if self.direct_card is not None and not isinstance(
            self.direct_card, ProduceCardSnapshot
        ):
            raise TypeError("direct_card must be a ProduceCardSnapshot or None")

    @property
    def ordered_effects(self) -> tuple[InitialRegularMasterEffectSpec, ...]:
        return self.effect_specs


@dataclass(frozen=True, slots=True, order=True)
class InitialRegularEffectBlocker:
    """Typed reason why no outer patch may be emitted."""

    code: str
    field: str
    message: str
    effect_result_index: int | None = None
    produce_effect_id: str | None = None

    def __post_init__(self) -> None:
        _nonempty_text(self.code, "blocker code")
        _nonempty_text(self.field, "blocker field")
        _nonempty_text(self.message, "blocker message")
        if self.effect_result_index is not None and (
            not _is_int(self.effect_result_index) or self.effect_result_index < 0
        ):
            raise ValueError("effect_result_index must be non-negative or None")
        if self.produce_effect_id is not None:
            _nonempty_text(self.produce_effect_id, "blocker produce_effect_id")


@dataclass(frozen=True, slots=True)
class InitialRegularEffectExecution:
    """Validated event result: exactly one patch, or one or more blockers."""

    authoritative_post_state: InitialRegularPostState
    effect_results: tuple[ProduceEffectResultTrace, ...]
    patch: ProduceStatePatch | None
    blockers: tuple[InitialRegularEffectBlocker, ...] = ()
    applied_effect_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.authoritative_post_state, InitialRegularPostState):
            raise TypeError("authoritative_post_state must be typed")
        results = tuple(self.effect_results)
        blockers = tuple(self.blockers)
        applied = tuple(self.applied_effect_ids)
        if not all(isinstance(value, ProduceEffectResultTrace) for value in results):
            raise TypeError("effect_results must contain typed trace entries")
        if not all(isinstance(value, InitialRegularEffectBlocker) for value in blockers):
            raise TypeError("blockers must contain typed executor blockers")
        if any(not isinstance(value, str) or not value for value in applied):
            raise ValueError("applied_effect_ids must contain non-empty strings")
        if blockers and self.patch is not None:
            raise ValueError("blocked execution cannot expose a patch")
        if not blockers and not isinstance(self.patch, ProduceStatePatch):
            raise ValueError("successful execution must expose a patch")
        object.__setattr__(self, "effect_results", results)
        object.__setattr__(self, "blockers", blockers)
        object.__setattr__(self, "applied_effect_ids", applied)

    @property
    def executable(self) -> bool:
        return self.patch is not None and not self.blockers

    @property
    def issues(self) -> tuple[InitialRegularEffectBlocker, ...]:
        return self.blockers

    @property
    def post_state(self) -> InitialRegularPostState:
        return self.authoritative_post_state


@dataclass(slots=True)
class _WorkingCard:
    snapshot: ProduceCardSnapshot
    instance_id: str | None
    metadata_known: bool


@dataclass(slots=True)
class _WorkingState:
    max_stamina: int
    stamina: int
    produce_point: int
    vocal: int
    dance: int
    visual: int
    cards: list[_WorkingCard]
    vote_count: int | None = None
    star: int | None = None
    high_score_gold: int | None = None


def _blocker(
    code: str,
    field: str,
    message: str,
    *,
    index: int | None = None,
    effect_id: str | None = None,
) -> InitialRegularEffectBlocker:
    return InitialRegularEffectBlocker(
        code=code,
        field=field,
        message=message,
        effect_result_index=index,
        produce_effect_id=effect_id,
    )


def _blocked(
    scenario: InitialRegularEventScenario,
    blockers: tuple[InitialRegularEffectBlocker, ...]
    | list[InitialRegularEffectBlocker],
    *,
    applied: tuple[str, ...] | list[str] = (),
) -> InitialRegularEffectExecution:
    return InitialRegularEffectExecution(
        authoritative_post_state=scenario.post_state,
        effect_results=scenario.effect_results,
        patch=None,
        blockers=tuple(blockers),
        applied_effect_ids=tuple(applied),
    )


def _expand_outer_deck(deck: tuple[DeckEntry, ...]) -> list[_WorkingCard]:
    cards: list[_WorkingCard] = []
    for entry in deck:
        instance_ids: tuple[str | None, ...]
        if entry.instance_ids:
            instance_ids = entry.instance_ids
        else:
            instance_ids = (None,) * entry.count
        cards.extend(
            _WorkingCard(
                snapshot=ProduceCardSnapshot(
                    id=entry.card_id,
                    upgrade_count=entry.upgrade,
                ),
                instance_id=instance_id,
                metadata_known=False,
            )
            for instance_id in instance_ids
        )
    return cards


def _append_direct_card(
    working: _WorkingState, card: ProduceCardSnapshot | None
) -> None:
    if card is None:
        return
    working.cards.append(
        _WorkingCard(snapshot=card, instance_id=None, metadata_known=True)
    )


def _snapshot_scalar(working: _WorkingState, field: str) -> int | None:
    return getattr(working, field)


def _match_cards(
    working: _WorkingState,
    observed: tuple[ProduceCardSnapshot, ...],
    *,
    code: str,
    field: str,
    index: int | None,
    effect_id: str | None,
) -> InitialRegularEffectBlocker | None:
    if len(working.cards) != len(observed):
        return _blocker(
            code,
            field,
            f"expected {len(working.cards)} cards, observed {len(observed)}",
            index=index,
            effect_id=effect_id,
        )
    for card_index, (actual, expected) in enumerate(zip(working.cards, observed)):
        if (
            actual.snapshot.id != expected.id
            or actual.snapshot.upgrade_count != expected.upgrade_count
        ):
            return _blocker(
                code,
                field,
                "card identity/upgrade mismatch at position "
                f"{card_index}: computed {actual.snapshot.id}/"
                f"{actual.snapshot.upgrade_count}, observed {expected.id}/"
                f"{expected.upgrade_count}",
                index=index,
                effect_id=effect_id,
            )
        if actual.metadata_known and actual.snapshot != expected:
            return _blocker(
                code,
                field,
                f"card metadata mismatch at position {card_index}",
                index=index,
                effect_id=effect_id,
            )
    # Outer DeckEntry cannot carry customizes/skins.  A matching response-side
    # snapshot is therefore the typed authority for those metadata fields.
    for actual, expected in zip(working.cards, observed):
        actual.snapshot = expected
        actual.metadata_known = True
    return None


def _match_snapshot(
    working: _WorkingState,
    observed: ProduceEffectStateSnapshot,
    *,
    code: str,
    field_prefix: str,
    index: int | None,
    effect_id: str | None,
) -> InitialRegularEffectBlocker | None:
    for field in _SCALAR_SNAPSHOT_FIELDS:
        expected = getattr(observed, field)
        if expected is None:
            continue
        actual = _snapshot_scalar(working, field)
        if actual is None:
            # vote/star/gold are not present in ProduceRolloutState.  The first
            # response observation anchors them for exact downstream replay.
            setattr(working, field, expected)
            continue
        if actual != expected:
            return _blocker(
                code,
                f"{field_prefix}.{field}",
                f"computed {actual}, observed {expected}",
                index=index,
                effect_id=effect_id,
            )
    if observed.produce_cards is not None:
        return _match_cards(
            working,
            observed.produce_cards,
            code=code,
            field=f"{field_prefix}.produce_cards",
            index=index,
            effect_id=effect_id,
        )
    return None


def _post_snapshot(post: InitialRegularPostState) -> ProduceEffectStateSnapshot:
    return ProduceEffectStateSnapshot(
        max_stamina=post.max_stamina,
        stamina=post.stamina,
        produce_point=post.produce_point,
        vote_count=post.vote_count,
        star=post.star,
        vocal=post.vocal,
        dance=post.dance,
        visual=post.visual,
        produce_cards=post.produce_cards,
        high_score_gold=post.high_score_gold,
    )


def _fixed_value(
    spec: InitialRegularMasterEffectSpec,
    trace: ProduceEffectResultTrace,
    index: int,
) -> tuple[int | None, InitialRegularEffectBlocker | None]:
    value = spec.fixed_value
    if value is None:
        return None, _blocker(
            "non-fixed-effect",
            "master.effect_value",
            "this reducer only executes fixed Master values for this effect",
            index=index,
            effect_id=trace.produce_effect_id,
        )
    if value != trace.effect_value:
        return None, _blocker(
            "effect-value-mismatch",
            "effect_result.effect_value",
            f"Master fixed value {value}, response value {trace.effect_value}",
            index=index,
            effect_id=trace.produce_effect_id,
        )
    return value, None


def _resolve_target_card(
    working: _WorkingState,
    trace: ProduceEffectResultTrace,
    index: int,
) -> tuple[int | None, InitialRegularEffectBlocker | None]:
    target = trace.effect_target_id
    if not target:
        return None, _blocker(
            "target-missing",
            "effect_result.effect_target_id",
            "card mutation requires an explicit response target",
            index=index,
            effect_id=trace.produce_effect_id,
        )
    instance_matches = [
        card_index
        for card_index, card in enumerate(working.cards)
        if card.instance_id == target
    ]
    matches = instance_matches or [
        card_index
        for card_index, card in enumerate(working.cards)
        if card.snapshot.id == target
    ]
    if not matches:
        return None, _blocker(
            "target-not-found",
            "effect_result.effect_target_id",
            f"target card is absent from the current deck: {target}",
            index=index,
            effect_id=trace.produce_effect_id,
        )
    if len(matches) != 1:
        return None, _blocker(
            "target-ambiguous",
            "effect_result.effect_target_id",
            f"target identifies {len(matches)} current cards: {target}",
            index=index,
            effect_id=trace.produce_effect_id,
        )
    return matches[0], None


def _card_reward(
    reward: ProduceRewardResultSnapshot,
    trace: ProduceEffectResultTrace,
    index: int,
) -> tuple[ProduceCardSnapshot | None, InitialRegularEffectBlocker | None]:
    if reward.resource_type != PRODUCE_CARD_RESOURCE:
        return None, _blocker(
            "unsupported-reward-resource",
            "effect_result.provided_rewards.resource_type",
            f"outer patch cannot represent reward resource {reward.resource_type}",
            index=index,
            effect_id=trace.produce_effect_id,
        )
    if not reward.resource_id:
        return None, _blocker(
            "reward-member-missing",
            "effect_result.provided_rewards.resource_id",
            "card reward member has no card id",
            index=index,
            effect_id=trace.produce_effect_id,
        )
    return (
        ProduceCardSnapshot(
            id=reward.resource_id,
            upgrade_count=reward.resource_level,
            customizes=reward.customizes,
        ),
        None,
    )


def _apply_attribute(
    working: _WorkingState,
    spec: InitialRegularMasterEffectSpec,
    trace: ProduceEffectResultTrace,
    *,
    index: int,
    cap: int,
) -> InitialRegularEffectBlocker | None:
    value, issue = _fixed_value(spec, trace, index)
    if issue is not None:
        return issue
    assert value is not None
    if value < 0:
        return _blocker(
            "unsupported-effect-value",
            "master.effect_value",
            "fixed attribute addition cannot be negative",
            index=index,
            effect_id=trace.produce_effect_id,
        )
    if trace.ineffective:
        return None
    attribute = _ATTRIBUTE_EFFECTS[trace.effect_type]
    current = getattr(working, attribute)
    setattr(working, attribute, min(current + value, cap))
    return None


def _apply_reward(
    working: _WorkingState,
    trace: ProduceEffectResultTrace,
    *,
    index: int,
) -> InitialRegularEffectBlocker | None:
    if trace.ineffective:
        return None
    if not trace.provided_rewards:
        return _blocker(
            "reward-member-missing",
            "effect_result.provided_rewards",
            "reward execution requires explicit response members",
            index=index,
            effect_id=trace.produce_effect_id,
        )
    if trace.effect_type == PRODUCE_REWARD_SET and not trace.effect_target_id:
        return _blocker(
            "target-missing",
            "effect_result.effect_target_id",
            "reward-set execution requires an explicit selected target",
            index=index,
            effect_id=trace.produce_effect_id,
        )
    if trace.effect_target_id and trace.effect_target_id not in {
        reward.resource_id for reward in trace.provided_rewards
    }:
        return _blocker(
            "reward-target-mismatch",
            "effect_result.effect_target_id",
            "selected target is not among provided reward members",
            index=index,
            effect_id=trace.produce_effect_id,
        )
    for reward in trace.provided_rewards:
        card, issue = _card_reward(reward, trace, index)
        if issue is not None:
            return issue
        assert card is not None
        if reward.quantity < 1:
            return _blocker(
                "reward-member-missing",
                "effect_result.provided_rewards.quantity",
                "resolved card reward quantity must be positive",
                index=index,
                effect_id=trace.produce_effect_id,
            )
        working.cards.extend(
            _WorkingCard(card, None, True) for _ in range(reward.quantity)
        )
    return None


def _apply_card_mutation(
    working: _WorkingState,
    spec: InitialRegularMasterEffectSpec,
    trace: ProduceEffectResultTrace,
    *,
    index: int,
) -> InitialRegularEffectBlocker | None:
    # Static-boundary validation already proved that the response-resolved
    # value is inside this exact Master row's range.  Target and replacement
    # identity still come only from the scenario.
    value = trace.effect_value
    if value < 0:
        return _blocker(
            "unsupported-effect-value",
            "master.effect_value",
            "card mutation value cannot be negative",
            index=index,
            effect_id=trace.produce_effect_id,
        )
    if trace.ineffective:
        return None
    target_index, issue = _resolve_target_card(working, trace, index)
    if issue is not None:
        return issue
    assert target_index is not None
    target = working.cards[target_index]

    if trace.effect_type == PRODUCE_CARD_UPGRADE:
        target.snapshot = ProduceCardSnapshot(
            id=target.snapshot.id,
            upgrade_count=target.snapshot.upgrade_count + value,
            customizes=target.snapshot.customizes,
            produce_card_skin_id=target.snapshot.produce_card_skin_id,
        )
        return None

    if trace.effect_type == PRODUCE_CARD_DELETE:
        del working.cards[target_index]
        return None

    if len(trace.provided_rewards) != 1:
        return _blocker(
            "reward-member-ambiguous",
            "effect_result.provided_rewards",
            "card change requires exactly one explicit replacement member",
            index=index,
            effect_id=trace.produce_effect_id,
        )
    replacement_reward = trace.provided_rewards[0]
    if replacement_reward.quantity != 1:
        return _blocker(
            "reward-member-ambiguous",
            "effect_result.provided_rewards.quantity",
            "card change replacement quantity must be exactly one",
            index=index,
            effect_id=trace.produce_effect_id,
        )
    replacement, issue = _card_reward(replacement_reward, trace, index)
    if issue is not None:
        return issue
    assert replacement is not None
    working.cards[target_index] = _WorkingCard(
        snapshot=replacement,
        instance_id=target.instance_id,
        metadata_known=True,
    )
    return None


def _apply_stamina_recover_multiple(
    working: _WorkingState,
    spec: InitialRegularMasterEffectSpec,
    trace: ProduceEffectResultTrace,
    *,
    index: int,
) -> InitialRegularEffectBlocker | None:
    del spec  # Its identity/range was checked; no arithmetic is inferred.
    if trace.before.stamina is None or trace.after.stamina is None:
        return _blocker(
            "trace-replay-required",
            "effect_result.stamina",
            "StaminaRecoverMultiple requires observed before and after stamina",
            index=index,
            effect_id=trace.produce_effect_id,
        )
    if trace.ineffective:
        return None
    # No formula is used here.  Until golden evidence establishes rounding,
    # the resolved response-side value is the only authority.
    working.stamina = trace.after.stamina
    if working.stamina > working.max_stamina:
        return _blocker(
            "trace-state-invalid",
            "effect_result.after.stamina",
            "replayed stamina exceeds the current maximum",
            index=index,
            effect_id=trace.produce_effect_id,
        )
    return None


def _deck_entries(cards: list[_WorkingCard]) -> tuple[DeckEntry, ...]:
    entries: list[DeckEntry] = []
    index = 0
    while index < len(cards):
        first = cards[index]
        with_instances = first.instance_id is not None
        end = index + 1
        while end < len(cards):
            candidate = cards[end]
            if (
                candidate.snapshot.id != first.snapshot.id
                or candidate.snapshot.upgrade_count != first.snapshot.upgrade_count
                or (candidate.instance_id is not None) != with_instances
            ):
                break
            end += 1
        group = cards[index:end]
        entries.append(
            DeckEntry(
                card_id=first.snapshot.id,
                upgrade=first.snapshot.upgrade_count,
                count=len(group),
                instance_ids=(
                    tuple(card.instance_id for card in group)  # type: ignore[arg-type]
                    if with_instances
                    else ()
                ),
            )
        )
        index = end
    return tuple(entries)


def _validate_static_boundary(
    scenario: InitialRegularEventScenario,
    master: InitialRegularEventMasterProjection,
) -> tuple[InitialRegularEffectBlocker, ...]:
    blockers: list[InitialRegularEffectBlocker] = []
    for field in ("detail_id", "suggestion_id", "suggestion_index"):
        actual = getattr(master, field)
        expected = getattr(scenario, field)
        if actual != expected:
            blockers.append(
                _blocker(
                    "master-identity-mismatch",
                    field,
                    f"Master projection {actual!r}, scenario {expected!r}",
                )
            )
    try:
        validate_effect_result_chain(scenario.effect_results)
    except EffectResultChainError as error:
        blockers.extend(
            _blocker(
                "effect-chain-mismatch",
                f"effect_results.{mismatch.field}",
                f"result {mismatch.left_index} after {mismatch.left_after!r} "
                f"does not match result {mismatch.right_index} before "
                f"{mismatch.right_before!r}",
                index=mismatch.right_index,
                effect_id=scenario.effect_results[
                    mismatch.right_index
                ].produce_effect_id,
            )
            for mismatch in error.mismatches
        )
    if len(master.effect_specs) != len(scenario.effect_results):
        blockers.append(
            _blocker(
                "effect-count-mismatch",
                "effect_specs",
                f"Master has {len(master.effect_specs)} ordered effects, "
                f"scenario has {len(scenario.effect_results)} EffectResults",
            )
        )
        return tuple(blockers)
    for index, (spec, trace) in enumerate(
        zip(master.effect_specs, scenario.effect_results)
    ):
        if (
            spec.produce_effect_id != trace.produce_effect_id
            or spec.effect_type != trace.effect_type
        ):
            blockers.append(
                _blocker(
                    "master-effect-mismatch",
                    "effect_specs",
                    "ordered Master identity/type does not match EffectResult",
                    index=index,
                    effect_id=trace.produce_effect_id or spec.produce_effect_id,
                )
            )
            continue
        if not spec.effect_value_min <= trace.effect_value <= spec.effect_value_max:
            blockers.append(
                _blocker(
                    "effect-value-out-of-range",
                    "effect_result.effect_value",
                    f"response value {trace.effect_value} is outside Master range "
                    f"{spec.effect_value_min}..{spec.effect_value_max}",
                    index=index,
                    effect_id=trace.produce_effect_id,
                )
            )
    return tuple(blockers)


def execute_initial_regular_event_effects(
    state: ProduceRolloutState,
    scenario: InitialRegularEventScenario,
    master: InitialRegularEventMasterProjection,
) -> InitialRegularEffectExecution:
    """Reduce one resolved event into an exact outer-state patch.

    Semantic uncertainty is returned as typed blockers.  Programmer contract
    violations (passing untyped objects) raise ``TypeError``.
    """

    if not isinstance(state, ProduceRolloutState):
        raise TypeError("state must be ProduceRolloutState")
    if not isinstance(scenario, InitialRegularEventScenario):
        raise TypeError("scenario must be InitialRegularEventScenario")
    if not isinstance(master, InitialRegularEventMasterProjection):
        raise TypeError("master must be InitialRegularEventMasterProjection")

    if scenario.blockers:
        return _blocked(
            scenario,
            tuple(
                InitialRegularEffectBlocker(
                    code=value.code,
                    field="scenario.blockers",
                    message=value.message,
                    effect_result_index=value.effect_result_index,
                    produce_effect_id=value.produce_effect_id,
                )
                for value in scenario.blockers
            ),
        )

    blockers = _validate_static_boundary(scenario, master)
    if blockers:
        return _blocked(scenario, blockers)

    if max(
        state.attributes.vocal,
        state.attributes.dance,
        state.attributes.visual,
    ) > master.attribute_cap:
        return _blocked(
            scenario,
            (
                _blocker(
                    "attribute-above-cap",
                    "state.attributes",
                    "outer attribute exceeds the injected Master cap",
                ),
            ),
        )

    costs = scenario.effective_direct_costs
    cost_issues: list[InitialRegularEffectBlocker] = []
    if costs.stamina > state.stamina:
        cost_issues.append(
            _blocker(
                "insufficient-stamina",
                "effective_direct_costs.stamina",
                f"cannot subtract {costs.stamina} from {state.stamina}",
            )
        )
    if costs.produce_point > state.produce_points:
        cost_issues.append(
            _blocker(
                "insufficient-produce-points",
                "effective_direct_costs.produce_point",
                f"cannot subtract {costs.produce_point} from {state.produce_points}",
            )
        )
    if cost_issues:
        return _blocked(scenario, cost_issues)

    working = _WorkingState(
        max_stamina=state.max_stamina,
        stamina=state.stamina - costs.stamina,
        produce_point=state.produce_points - costs.produce_point,
        vocal=state.attributes.vocal,
        dance=state.attributes.dance,
        visual=state.attributes.visual,
        cards=_expand_outer_deck(state.deck),
    )
    _append_direct_card(working, master.direct_card)

    applied: list[str] = []
    for index, (spec, trace) in enumerate(
        zip(master.effect_specs, scenario.effect_results)
    ):
        before_issue = _match_snapshot(
            working,
            trace.before,
            code="trace-before-mismatch",
            field_prefix="effect_result.before",
            index=index,
            effect_id=trace.produce_effect_id,
        )
        if before_issue is not None:
            return _blocked(scenario, (before_issue,), applied=applied)

        if trace.effect_type not in _SUPPORTED_EFFECTS:
            issue = _blocker(
                "unsupported-effect-type",
                "effect_result.effect_type",
                f"no fail-closed reducer exists for {trace.effect_type}",
                index=index,
                effect_id=trace.produce_effect_id,
            )
        elif trace.effect_type in _ATTRIBUTE_EFFECTS:
            issue = _apply_attribute(
                working,
                spec,
                trace,
                index=index,
                cap=master.attribute_cap,
            )
        elif trace.effect_type in {PRODUCE_REWARD, PRODUCE_REWARD_SET}:
            issue = _apply_reward(working, trace, index=index)
        elif trace.effect_type in _CARD_MUTATION_EFFECTS:
            issue = _apply_card_mutation(working, spec, trace, index=index)
        else:
            issue = _apply_stamina_recover_multiple(
                working, spec, trace, index=index
            )
        if issue is not None:
            return _blocked(scenario, (issue,), applied=applied)

        after_issue = _match_snapshot(
            working,
            trace.after,
            code="trace-after-mismatch",
            field_prefix="effect_result.after",
            index=index,
            effect_id=trace.produce_effect_id,
        )
        if after_issue is not None:
            return _blocked(scenario, (after_issue,), applied=applied)
        applied.append(trace.produce_effect_id)

    post_issue = _match_snapshot(
        working,
        _post_snapshot(scenario.post_state),
        code="post-state-mismatch",
        field_prefix="post_state",
        index=None,
        effect_id=None,
    )
    if post_issue is not None:
        return _blocked(scenario, (post_issue,), applied=applied)

    patch = ProduceStatePatch(
        stamina=working.stamina,
        max_stamina=working.max_stamina,
        produce_points=working.produce_point,
        attributes=AttributeValues(
            vocal=working.vocal,
            dance=working.dance,
            visual=working.visual,
        ),
        deck=_deck_entries(working.cards),
    )
    return InitialRegularEffectExecution(
        authoritative_post_state=scenario.post_state,
        effect_results=scenario.effect_results,
        patch=patch,
        blockers=(),
        applied_effect_ids=tuple(applied),
    )


# Small compatibility aliases make the wire point unambiguous without adding
# a second implementation path.
InitialRegularEventEffectSpec = InitialRegularMasterEffectSpec
InitialRegularEffectIssue = InitialRegularEffectBlocker
InitialRegularEventEffectExecution = InitialRegularEffectExecution
execute_initial_regular_event = execute_initial_regular_event_effects
reduce_initial_regular_event_effects = execute_initial_regular_event_effects


__all__ = [
    "DANCE_ADDITION",
    "InitialRegularEffectBlocker",
    "InitialRegularEffectExecution",
    "InitialRegularEffectIssue",
    "InitialRegularEventEffectExecution",
    "InitialRegularEventEffectSpec",
    "InitialRegularEventMasterProjection",
    "InitialRegularMasterEffectSpec",
    "PRODUCE_CARD_CHANGE",
    "PRODUCE_CARD_DELETE",
    "PRODUCE_CARD_RESOURCE",
    "PRODUCE_CARD_UPGRADE",
    "PRODUCE_REWARD",
    "PRODUCE_REWARD_SET",
    "STAMINA_RECOVER_MULTIPLE",
    "VISUAL_ADDITION",
    "VOCAL_ADDITION",
    "execute_initial_regular_event",
    "execute_initial_regular_event_effects",
    "reduce_initial_regular_event_effects",
]
