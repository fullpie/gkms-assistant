"""Fail-closed eligibility gate for deterministic native ordered replay.

This module only inspects already-decoded LocalSave evidence and an explicit
Master-card catalog.  It neither reads nor mutates the game, and it does not
reuse the legacy belief-state horizon audit.  The first supported subset is
deliberately narrow: every mutable per-GUID card field must have the exact
neutral shape understood by the v3.2.3 replay, and every effective upgrade a
card can reach through HandAdd support upgrades must have safe Master data.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TypeAlias

from .audition_horizon import HorizonCardRef
from .audition_local_save_state import (
    AUDITION_LOCAL_SAVE_STATE_SCHEMA_VERSION,
    AuditionLocalSaveStateEvidence,
    CanonicalJsonValue,
    LocalSaveExamCard,
    LocalSaveExamCardRuntimeState,
    LocalSaveExamState,
)
from .logic_engine import MasterCard, MasterCardEffect


MAX_EFFECTIVE_UPGRADE = 3
MOVE_GRAVE = "ProduceCardMovePositionType_Grave"
MOVE_LOST = "ProduceCardMovePositionType_Lost"
_NO_MOVE_EFFECT_TRIGGERS = frozenset(
    {"", "ProduceCardMoveEffectTriggerType_Unknown"}
)

# Exact idle ExamCardStatusEffect payload emitted by the pinned v3.2.3 client.
# Keeping this sentinel local makes the eligibility contract explicit instead
# of relying on a private parser implementation detail.
NEUTRAL_V323_CARD_STATUS_EFFECT: dict[str, object] = {
    "_id": "",
    "_produceExamTriggerId": "",
    "_produceCardGrowEffectIdList": [],
    "_effectGroupIdList": [],
    "_triggerCount": 0,
    "_phaseCountDictionary": {"_list": []},
    "_spendTurn": 0,
    "_spendCount": 0,
}

CatalogKey: TypeAlias = HorizonCardRef | tuple[str, int]


def _same_json_value(actual: object, expected: object) -> bool:
    """Compare canonical JSON without Python's bool/int numeric coercion."""

    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(  # type: ignore[union-attr]
            _same_json_value(actual[key], value)  # type: ignore[index]
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(  # type: ignore[arg-type]
            _same_json_value(left, right)
            for left, right in zip(actual, expected, strict=True)  # type: ignore[arg-type]
        )
    return actual == expected


@dataclass(frozen=True, order=True, slots=True)
class NativeOrderedReplayBlocker:
    """One stable, machine-readable reason native replay is not eligible."""

    code: str
    subject: str = ""
    detail: str = ""

    def __post_init__(self) -> None:
        for field_name in ("code", "subject", "detail"):
            value = getattr(self, field_name)
            if not isinstance(value, str):
                raise TypeError(f"{field_name} must be text")
        if not self.code.strip():
            raise ValueError("blocker code must be non-empty text")

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "subject": self.subject,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class NativeOrderedReplayEligibility:
    """Deterministic result of the native replay eligibility audit."""

    blockers: tuple[NativeOrderedReplayBlocker, ...]
    required_variants: tuple[HorizonCardRef, ...]
    end_turn_lost_variants: tuple[HorizonCardRef, ...]

    def __post_init__(self) -> None:
        blockers = tuple(self.blockers)
        required = tuple(self.required_variants)
        end_turn_lost = tuple(self.end_turn_lost_variants)
        if any(not isinstance(item, NativeOrderedReplayBlocker) for item in blockers):
            raise TypeError("blockers must contain NativeOrderedReplayBlocker values")
        if blockers != tuple(sorted(set(blockers))):
            raise ValueError("blockers must be uniquely and stably sorted")
        for label, values in (
            ("required_variants", required),
            ("end_turn_lost_variants", end_turn_lost),
        ):
            if any(not isinstance(item, HorizonCardRef) for item in values):
                raise TypeError(f"{label} must contain HorizonCardRef values")
            if values != tuple(sorted(set(values))):
                raise ValueError(f"{label} must be uniquely and stably sorted")
        if not set(end_turn_lost).issubset(required):
            raise ValueError("end_turn_lost_variants must be required variants")
        object.__setattr__(self, "blockers", blockers)
        object.__setattr__(self, "required_variants", required)
        object.__setattr__(self, "end_turn_lost_variants", end_turn_lost)

    @property
    def eligible(self) -> bool:
        return not self.blockers

    def to_dict(self) -> dict[str, object]:
        return {
            "eligible": self.eligible,
            "blockers": [item.to_dict() for item in self.blockers],
            "required_variants": [item.key for item in self.required_variants],
            "end_turn_lost_variants": [
                item.key for item in self.end_turn_lost_variants
            ],
        }


def _block(
    blockers: list[NativeOrderedReplayBlocker],
    code: str,
    subject: str = "",
    detail: str = "",
) -> None:
    blockers.append(NativeOrderedReplayBlocker(code, subject, detail))


def _card_locations(state: LocalSaveExamState) -> tuple[tuple[str, LocalSaveExamCard], ...]:
    located: list[tuple[str, LocalSaveExamCard]] = []
    for zone_name in ("hand", "deck", "grave", "lost", "hold"):
        for index, card in enumerate(getattr(state.zones, zone_name)):
            located.append((f"{zone_name}[{index}]", card))
    if state.playing_card is not None:
        located.append(("playing_card", state.playing_card))
    for index, card in enumerate(state.removed_cards):
        located.append((f"removed_cards[{index}]", card))
    for group_index, group in enumerate(state.future_deck):
        for index, card in enumerate(group):
            located.append((f"future_deck[{group_index}][{index}]", card))
    if state.past_deck is not None:
        for group_index, group in enumerate(state.past_deck):
            for index, card in enumerate(group):
                located.append((f"past_deck[{group_index}][{index}]", card))
    return tuple(located)


def _json_value(
    value: object,
    *,
    subject: str,
    field_name: str,
    blockers: list[NativeOrderedReplayBlocker],
) -> object | None:
    if not isinstance(value, CanonicalJsonValue):
        _block(
            blockers,
            "card-runtime-json-shape-invalid",
            subject,
            f"{field_name}:not-CanonicalJsonValue",
        )
        return None
    try:
        return value.to_value()
    except (TypeError, ValueError) as error:
        _block(
            blockers,
            "card-runtime-json-shape-invalid",
            subject,
            f"{field_name}:{type(error).__name__}:{error}",
        )
        return None


def _audit_empty_runtime_list(
    value: object,
    *,
    subject: str,
    field_name: str,
    blockers: list[NativeOrderedReplayBlocker],
) -> None:
    decoded = _json_value(
        value,
        subject=subject,
        field_name=field_name,
        blockers=blockers,
    )
    if decoded is None:
        return
    if not isinstance(decoded, list):
        _block(
            blockers,
            "card-runtime-json-shape-invalid",
            subject,
            f"{field_name}:expected-list",
        )
    elif decoded:
        _block(
            blockers,
            "card-runtime-list-unsupported",
            subject,
            field_name,
        )


def _audit_runtime(
    card: LocalSaveExamCard,
    *,
    location: str,
    blockers: list[NativeOrderedReplayBlocker],
) -> None:
    subject = f"{location}:{card.guid}:{card.card_id}@{card.effective_upgrade}"
    runtime = card.runtime_state
    if runtime is None:
        _block(blockers, "card-runtime-state-missing", subject)
        return
    if not isinstance(runtime, LocalSaveExamCardRuntimeState):
        _block(
            blockers,
            "card-runtime-state-invalid",
            subject,
            type(runtime).__name__,
        )
        return

    if (
        not isinstance(runtime.play_count, int)
        or isinstance(runtime.play_count, bool)
        or runtime.play_count < 0
    ):
        _block(
            blockers,
            "card-runtime-play-count-invalid",
            subject,
            repr(runtime.play_count),
        )

    status = _json_value(
        runtime.status_effect,
        subject=subject,
        field_name="status_effect",
        blockers=blockers,
    )
    if status is not None:
        if not isinstance(status, dict):
            _block(
                blockers,
                "card-runtime-json-shape-invalid",
                subject,
                "status_effect:expected-object",
            )
        elif not _same_json_value(status, NEUTRAL_V323_CARD_STATUS_EFFECT):
            _block(
                blockers,
                "card-runtime-status-effect-unsupported",
                subject,
            )

    for field_name in (
        "affect_grow_effect_id_list",
        "grow_effect_exam_start_after_list",
        "stamina_consumption_specify_effect_list",
    ):
        _audit_empty_runtime_list(
            getattr(runtime, field_name, None),
            subject=subject,
            field_name=field_name,
            blockers=blockers,
        )

    move_used = runtime.is_move_produce_exam_effect_use_in_turn
    if not isinstance(move_used, bool):
        _block(
            blockers,
            "card-runtime-move-use-invalid",
            subject,
            repr(move_used),
        )
    elif move_used:
        _block(blockers, "card-runtime-move-use-unsupported", subject)

    customize = _json_value(
        runtime.customize_count_list,
        subject=subject,
        field_name="customize_count_list",
        blockers=blockers,
    )
    if customize is not None:
        if not isinstance(customize, list):
            _block(
                blockers,
                "card-runtime-json-shape-invalid",
                subject,
                "customize_count_list:expected-list",
            )
        elif any(
            not isinstance(item, int) or isinstance(item, bool)
            for item in customize
        ):
            _block(
                blockers,
                "card-runtime-customize-invalid",
                subject,
                "all entries must be integers",
            )
        elif any(item != 0 for item in customize):
            _block(blockers, "card-runtime-customize-unsupported", subject)

    for field_name in ("produce_card_skin_id", "produce_card_skin_asset_id"):
        if not isinstance(getattr(runtime, field_name, None), str):
            _block(
                blockers,
                "card-runtime-field-type-invalid",
                subject,
                f"{field_name}:expected-text",
            )


def _parse_catalog_key(key: object) -> tuple[str, int] | None:
    if isinstance(key, HorizonCardRef):
        card_id = key.card_id
        upgrade = key.upgrade
    elif isinstance(key, tuple) and len(key) == 2:
        card_id, upgrade = key
    else:
        return None
    if not isinstance(card_id, str) or not card_id.strip():
        return None
    if (
        not isinstance(upgrade, int)
        or isinstance(upgrade, bool)
        or not 0 <= upgrade <= MAX_EFFECTIVE_UPGRADE
    ):
        return None
    return card_id, upgrade


def _normalise_catalog(
    catalog: object,
    blockers: list[NativeOrderedReplayBlocker],
) -> dict[tuple[str, int], MasterCard]:
    if not isinstance(catalog, Mapping):
        _block(
            blockers,
            "master-catalog-invalid",
            detail="catalog must be a mapping",
        )
        return {}
    result: dict[tuple[str, int], MasterCard] = {}
    for raw_key, raw_card in catalog.items():
        key = _parse_catalog_key(raw_key)
        if key is None:
            _block(
                blockers,
                "master-catalog-key-invalid",
                detail=repr(raw_key),
            )
            continue
        key_text = f"{key[0]}@{key[1]}"
        if key in result:
            _block(blockers, "master-catalog-key-duplicate", key_text)
            continue
        if not isinstance(raw_card, MasterCard):
            _block(
                blockers,
                "master-catalog-value-invalid",
                key_text,
                type(raw_card).__name__,
            )
            continue
        result[key] = raw_card
        if raw_card.id != key[0] or raw_card.upgrade != key[1]:
            _block(
                blockers,
                "card-master-key-mismatch",
                key_text,
                f"value={raw_card.id}@{raw_card.upgrade}",
            )
    return result


def _audit_master_variant(
    ref: HorizonCardRef,
    card: MasterCard,
    blockers: list[NativeOrderedReplayBlocker],
) -> bool:
    """Return whether this safe variant is classified EndTurnLost."""

    subject = ref.key
    if card.id != ref.card_id or card.upgrade != ref.upgrade:
        # The normalization phase already emits this blocker; never classify a
        # mismatched value as a usable Master row.
        return False

    if not isinstance(card.effects, tuple) or any(
        not isinstance(effect, MasterCardEffect) for effect in card.effects
    ):
        _block(blockers, "card-master-effects-invalid", subject)
    else:
        invalid_once = any(not isinstance(effect.once, bool) for effect in card.effects)
        if invalid_once:
            _block(blockers, "card-master-effects-invalid", subject, "once:not-bool")
        elif any(effect.once for effect in card.effects):
            _block(blockers, "card-master-once-effect-unsupported", subject)

    if not isinstance(card.move_position_type, str):
        _block(blockers, "card-master-move-position-invalid", subject)
    elif card.move_position_type not in {MOVE_GRAVE, MOVE_LOST}:
        _block(
            blockers,
            "card-master-move-position-unsupported",
            subject,
            card.move_position_type,
        )

    if not isinstance(card.move_effect_trigger_type, str):
        _block(blockers, "card-master-move-effect-invalid", subject)
    elif card.move_effect_trigger_type not in _NO_MOVE_EFFECT_TRIGGERS:
        _block(
            blockers,
            "card-master-move-effect-unsupported",
            subject,
            f"trigger={card.move_effect_trigger_type}",
        )

    for field_name in ("move_effect_ids", "move_trigger_ids"):
        value = getattr(card, field_name, None)
        if not isinstance(value, tuple) or any(
            not isinstance(item, str) or not item.strip() for item in value
        ):
            _block(
                blockers,
                "card-master-move-effect-invalid",
                subject,
                f"{field_name}:expected text tuple",
            )
        elif value:
            _block(
                blockers,
                "card-master-move-effect-unsupported",
                subject,
                field_name,
            )

    if not isinstance(card.produce_card_status_enchant_id, str):
        _block(blockers, "card-master-status-enchant-invalid", subject)
    elif card.produce_card_status_enchant_id:
        _block(
            blockers,
            "card-master-status-enchant-unsupported",
            subject,
            card.produce_card_status_enchant_id,
        )

    if not isinstance(card.is_end_turn_lost, bool):
        _block(blockers, "card-master-end-turn-lost-invalid", subject)
        return False
    return card.is_end_turn_lost


def audit_native_ordered_replay_eligibility(
    source: AuditionLocalSaveStateEvidence | LocalSaveExamState,
    catalog: Mapping[CatalogKey, MasterCard],
) -> NativeOrderedReplayEligibility:
    """Return a stable fail-closed eligibility decision for native replay.

    A bare :class:`LocalSaveExamState` is the current in-memory schema-v5
    representation.  Serialized callers should pass the evidence object so
    its schema number is checked explicitly.  Every card requires exact
    card and root runtime state either way; migrated v2/v3/v4 state therefore
    remains blocked.

    For each card, every upgrade from ``base + temporary`` through +3 is
    treated as reachable.  That is conservative for HandAdd support rolls and
    prevents a future successful support from selecting unaudited Master data.
    """

    blockers: list[NativeOrderedReplayBlocker] = []
    if isinstance(source, AuditionLocalSaveStateEvidence):
        if source.schema_version != AUDITION_LOCAL_SAVE_STATE_SCHEMA_VERSION:
            _block(
                blockers,
                "local-save-schema-unsupported",
                detail=(
                    f"actual={source.schema_version};"
                    f"required={AUDITION_LOCAL_SAVE_STATE_SCHEMA_VERSION}"
                ),
            )
        state = source.state
    elif isinstance(source, LocalSaveExamState):
        state = source
    else:
        _block(
            blockers,
            "local-save-state-invalid",
            detail=type(source).__name__,
        )
        state = None

    normalised_catalog = _normalise_catalog(catalog, blockers)
    required: set[HorizonCardRef] = set()
    end_turn_lost: set[HorizonCardRef] = set()

    if state is not None:
        if not state.has_complete_root_runtime_state:
            _block(blockers, "local-save-root-runtime-unknown")
        if not state.is_native_actionable_settled:
            _block(blockers, "local-save-not-native-actionable-settled")
        try:
            located_cards = _card_locations(state)
        except (AttributeError, TypeError, ValueError) as error:
            _block(
                blockers,
                "local-save-state-invalid",
                detail=f"{type(error).__name__}:{error}",
            )
            located_cards = ()

        for location, card in located_cards:
            if not isinstance(card, LocalSaveExamCard):
                _block(
                    blockers,
                    "local-save-card-invalid",
                    location,
                    type(card).__name__,
                )
                continue
            _audit_runtime(card, location=location, blockers=blockers)

            subject = f"{location}:{card.guid}:{card.card_id}"
            values = (
                card.base_upgrade,
                card.temporary_upgrade,
                card.effective_upgrade,
            )
            if any(
                not isinstance(value, int)
                or isinstance(value, bool)
                or not 0 <= value <= MAX_EFFECTIVE_UPGRADE
                for value in values
            ):
                _block(
                    blockers,
                    "card-upgrade-state-invalid",
                    subject,
                    repr(values),
                )
                continue
            floor = card.base_upgrade + card.temporary_upgrade
            if floor > MAX_EFFECTIVE_UPGRADE or card.effective_upgrade < floor:
                _block(
                    blockers,
                    "card-upgrade-state-invalid",
                    subject,
                    f"floor={floor};effective={card.effective_upgrade}",
                )
                continue
            if (
                not isinstance(card.card_id, str)
                or not card.card_id.strip()
                or not isinstance(card.guid, str)
                or not card.guid.strip()
            ):
                _block(blockers, "local-save-card-identity-invalid", subject)
                continue
            required.update(
                HorizonCardRef(card.card_id, upgrade)
                for upgrade in range(floor, MAX_EFFECTIVE_UPGRADE + 1)
            )

    for ref in sorted(required):
        card = normalised_catalog.get((ref.card_id, ref.upgrade))
        if card is None:
            _block(blockers, "card-master-missing", ref.key)
            continue
        if _audit_master_variant(ref, card, blockers):
            end_turn_lost.add(ref)

    return NativeOrderedReplayEligibility(
        blockers=tuple(sorted(set(blockers))),
        required_variants=tuple(sorted(required)),
        end_turn_lost_variants=tuple(sorted(end_turn_lost)),
    )


__all__ = [
    "CatalogKey",
    "MAX_EFFECTIVE_UPGRADE",
    "MOVE_GRAVE",
    "MOVE_LOST",
    "NEUTRAL_V323_CARD_STATUS_EFFECT",
    "NativeOrderedReplayBlocker",
    "NativeOrderedReplayEligibility",
    "audit_native_ordered_replay_eligibility",
]
