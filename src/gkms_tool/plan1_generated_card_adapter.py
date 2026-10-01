"""Bind exact runtime generated-card identities to the Plan1 stage.

The native recorder reports a generated card at the managed
``CreateGuidIfNeed`` boundary.  :mod:`runtime_generated_card_adapter` is the
single parser for that evidence; this module only adds the two pieces of
Plan1 context that are not part of the recorder row:

* the selected card's ``play_count`` from the captured ``state_before``; and
* the unique zero-based effect index in the already compiled card program.

No post-state is accepted or inspected here.  In particular, a GUID found in
``state_after`` cannot be used to repair an incomplete recorder observation.
The result is deliberately typed and fail-closed so callers can retain the
observation and explain why a binding was not handed to the reducer.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .plan1_native_core import Plan1CompiledCard
from .plan1_native_stage import Plan1CardCreateGuidBinding
from .runtime_generated_card_adapter import (
    CARD_CREATE_ID_EFFECT_TYPE,
    GeneratedCardAdapterBlocker,
    GeneratedCardIdentityObservation,
    adapt_generated_card_identity,
)


@dataclass(frozen=True, slots=True)
class Plan1GeneratedCardBindingResult:
    """Strict generated identity plus its Plan1 invocation binding."""

    observation: GeneratedCardIdentityObservation | None
    binding: Plan1CardCreateGuidBinding | None
    blockers: tuple[GeneratedCardAdapterBlocker, ...] = ()

    @property
    def accepted(self) -> bool:
        return self.observation is not None and self.binding is not None and not self.blockers

    @property
    def ready(self) -> bool:
        return self.accepted

    @property
    def exact(self) -> bool:
        return self.accepted

    @property
    def identity_exact(self) -> bool:
        return self.accepted

    @property
    def blocker_codes(self) -> tuple[str, ...]:
        return tuple(value.code for value in self.blockers)

    def to_dict(self) -> dict[str, object]:
        return {
            "accepted": self.accepted,
            "identity_exact": self.identity_exact,
            "observation": (
                None if self.observation is None else self.observation.to_dict()
            ),
            "binding": (
                None
                if self.binding is None
                else {
                    "source_guid": self.binding.source_guid,
                    "source_play_count": self.binding.source_play_count,
                    "effect_id": self.binding.effect_id,
                    "effect_index": self.binding.effect_index,
                    "guid_tokens": list(self.binding.guid_tokens),
                }
            ),
            "blockers": [value.to_dict() for value in self.blockers],
        }


def _blocker(code: str, detail: object = "") -> GeneratedCardAdapterBlocker:
    return GeneratedCardAdapterBlocker(
        code,
        "" if detail is None else str(detail),
    )


def _dedupe(
    blockers: tuple[GeneratedCardAdapterBlocker, ...] | list[GeneratedCardAdapterBlocker],
) -> tuple[GeneratedCardAdapterBlocker, ...]:
    result: list[GeneratedCardAdapterBlocker] = []
    seen: set[tuple[str, str]] = set()
    for value in blockers:
        key = (value.code, value.detail)
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return tuple(result)


def _effect_index(
    observation: GeneratedCardIdentityObservation,
    program: Plan1CompiledCard,
) -> tuple[int | None, tuple[GeneratedCardAdapterBlocker, ...]]:
    """Resolve one effect id against the compiled program, without guessing."""

    matches = tuple(
        (index, effect)
        for index, effect in enumerate(program.effects)
        if effect.effect_id == observation.effect_id
    )
    if not matches:
        return None, (
            _blocker(
                "plan1-card-create-effect-index-missing",
                f"program={program.card_id}@{program.upgrade};effect={observation.effect_id}",
            ),
        )
    if len(matches) != 1:
        return None, (
            _blocker(
                "plan1-card-create-effect-index-ambiguous",
                f"program={program.card_id}@{program.upgrade};effect={observation.effect_id};count={len(matches)}",
            ),
        )

    index, effect = matches[0]
    blockers: list[GeneratedCardAdapterBlocker] = []
    if effect.effect_type != CARD_CREATE_ID_EFFECT_TYPE:
        blockers.append(
            _blocker(
                "plan1-card-create-effect-type-mismatch",
                f"effect={observation.effect_id};compiled={effect.effect_type}",
            )
        )
    if effect.target_card_id != observation.target_card_id:
        blockers.append(
            _blocker(
                "plan1-card-create-target-mismatch",
                f"compiled={effect.target_card_id!r};observed={observation.target_card_id!r}",
            )
        )
    if effect.target_upgrade != observation.target_upgrade:
        blockers.append(
            _blocker(
                "plan1-card-create-target-mismatch",
                f"compiled_upgrade={effect.target_upgrade};observed_upgrade={observation.target_upgrade}",
            )
        )
    if effect.move_position_type != "ProduceCardMovePositionType_DeckRandom":
        blockers.append(
            _blocker(
                "plan1-card-create-destination-mismatch",
                f"compiled={effect.move_position_type!r};observed={observation.destination!r}",
            )
        )
    expected_count = effect.pick_count_min
    if (
        effect.pick_count_min != effect.pick_count_max
        or expected_count <= 0
        or len(observation.cards) != expected_count
    ):
        blockers.append(
            _blocker(
                "plan1-card-create-count-mismatch",
                f"compiled={effect.pick_count_min}..{effect.pick_count_max};observed={len(observation.cards)}",
            )
        )
    return index, _dedupe(blockers)


def adapt_generated_card_identity_to_plan1_binding(
    record: Mapping[str, object] | object,
    *,
    source_play_count: int,
    program: Plan1CompiledCard,
    expected_action_order: int | None = None,
    expected_source_guid: str | None = None,
) -> Plan1GeneratedCardBindingResult:
    """Turn one exact recorder row into a Plan1 GUID binding.

    ``source_play_count`` must come from the selected card in the captured
    ``state_before``.  ``program`` must be the compiled program for that same
    selected card.  Both are caller-supplied context; this function does not
    inspect a replay ``state_after`` or infer an effect index from a card id.
    """

    blockers: list[GeneratedCardAdapterBlocker] = []
    if isinstance(source_play_count, bool) or not isinstance(source_play_count, int):
        blockers.append(
            _blocker("plan1-card-create-source-play-count-invalid", source_play_count)
        )
    elif source_play_count < 0:
        blockers.append(
            _blocker("plan1-card-create-source-play-count-invalid", source_play_count)
        )
    if not isinstance(program, Plan1CompiledCard):
        blockers.append(
            _blocker(
                "plan1-card-create-compiled-program-invalid",
                type(program).__name__,
            )
        )
        # Keep the strict observation available for diagnostics even when
        # program context is absent.
        observed = adapt_generated_card_identity(
            record,
            expected_action_order=expected_action_order,
            expected_source_guid=expected_source_guid,
        )
        blockers.extend(observed.blockers)
        return Plan1GeneratedCardBindingResult(
            observed.observation,
            None,
            _dedupe(blockers),
        )

    observed = adapt_generated_card_identity(
        record,
        expected_action_order=expected_action_order,
        expected_source_guid=expected_source_guid,
    )
    blockers.extend(observed.blockers)
    if observed.observation is None:
        return Plan1GeneratedCardBindingResult(
            None,
            None,
            _dedupe(blockers),
        )
    observation = observed.observation
    effect_index, effect_blockers = _effect_index(observation, program)
    blockers.extend(effect_blockers)
    if effect_index is None or blockers:
        return Plan1GeneratedCardBindingResult(
            observation,
            None,
            _dedupe(blockers),
        )
    assert isinstance(source_play_count, int) and source_play_count >= 0
    binding = Plan1CardCreateGuidBinding(
        source_guid=observation.source_guid,
        source_play_count=source_play_count,
        effect_id=observation.effect_id,
        effect_index=effect_index,
        guid_tokens=observation.guid_tokens,
    )
    return Plan1GeneratedCardBindingResult(
        observation,
        binding,
        _dedupe(blockers),
    )


# Descriptive aliases make the boundary easy to discover without creating a
# second parser or a second source of identity truth.
bind_generated_card_identity_to_plan1 = adapt_generated_card_identity_to_plan1_binding
build_plan1_card_create_guid_binding = adapt_generated_card_identity_to_plan1_binding
adapt_generated_card_identity_to_plan1 = adapt_generated_card_identity_to_plan1_binding


__all__ = [
    "Plan1GeneratedCardBindingResult",
    "adapt_generated_card_identity_to_plan1",
    "adapt_generated_card_identity_to_plan1_binding",
    "bind_generated_card_identity_to_plan1",
    "build_plan1_card_create_guid_binding",
]
