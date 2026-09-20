"""A small, read-only imitation prior for leaderboard audition card plays.

The normalized leaderboard format deliberately stores an ``ExamAction`` as a
hand index.  It does not store the runtime card identity that was at that
index, and reconstructing that identity with the full fixed-exam simulator is
both expensive and unnecessarily coupled to the live reducer.  This module
keeps the boundary explicit:

* :func:`project_leaderboard_episode` accepts labels only from an explicit
  verified-action extension (card ID, settled GUID, turn, and a complete
  settled hand).  The canonical v2 episode has no such extension, so it
  returns no labels and records an abstention rather than pretending that
  ``produce_cards[index]`` is the card at a hand index.
* :class:`LeaderboardCardImitationPrior` learns a behavior-only score from
  externally verified ``(flow, stage, turn, finite action history, card_id)``
  examples.  Same-ID hand instances with one equal complete semantic
  signature share the stable card action class; differing/unknown signatures
  receive signature/occurrence keys.  GUIDs and slots remain dispatch/audit
  identity only.  The terminal score changes the vote weight but is never
  treated as a state transition or reward model.
* :func:`evaluate_leaderboard_card_imitation` evaluates with the complete
  settled hand carried by those verified examples and leaves one trajectory
  out at a time.  Runtime ranking has a separate ``legal`` candidate contract:
  only a native planner may supply that set.  Canonical leaderboard rows
  therefore produce a zero-coverage audit until a stepwise settled identity
  source is available.

The resulting object is intentionally runtime-neutral.  A live caller must
pass its already-resolved legal hand candidates to :meth:`rank`; this prior
cannot create, remove, or legalize a card.  A candidate card ID absent from
the requested flow's training evidence is omitted from the rank and reported
as an abstention candidate.  ``rank_behavior`` exists for offline evaluation
against a settled hand; it must never be used as a runtime legality source.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Literal

from .leaderboard_replay import LeaderboardReplayEpisode
from .leaderboard_replay_adapter import read_leaderboard_episode


SCHEMA = "gkms.leaderboard-card-imitation-prior.v1"
DEFAULT_LEADERBOARD_EPISODES = (
    Path(__file__).resolve().parents[2]
    / "var"
    / "leaderboard_dataset"
    / "v330_nia_five_archetype_all_modes_v1"
    / "episodes.jsonl"
)

STAGES = (
    "ProduceStepType_AuditionMid1",
    "ProduceStepType_AuditionMid2",
    "ProduceStepType_AuditionFinal",
)
_STAGE_BY_INDEX = {index: value for index, value in enumerate(STAGES)}
DEFAULT_HISTORY_SIZE = 2
MAX_SCORE = 1000

# A stable action key is deliberately separate from a runtime GUID.  GUIDs and
# slots identify one occurrence in one settled hand; they are not useful
# cross-run features.  When two same-ID instances have the same complete
# semantic signature they use the plain card ID action key.  Otherwise the
# signature/occurrence suffix keeps the alternatives distinct.
INSTANCE_SIGNATURE_SCHEMA = "gkms.leaderboard-card-instance-signature.v1"
INSTANCE_ACTION_KEY_SEPARATOR = "::instance:"
OCCURRENCE_ACTION_KEY_SEPARATOR = "::occurrence:"

# A settled hand is identity/evidence for behavior alternatives.  A legal set
# is a runtime input proven by the native planner.  Keeping these as distinct
# values prevents a full hand snapshot from being silently advertised as
# legal, and prevents the prior from becoming a legality provider.
CANDIDATE_SET_KINDS = ("settled_hand", "legal")
CandidateSetKind = Literal["settled_hand", "legal"]

FlowKey = tuple[str, str, str]
HistoryKey = tuple[str, ...]


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


def _flow_key(
    produce_id: object,
    plan_type: object,
    exam_effect_type: object,
    *,
    label: str = "flow",
) -> FlowKey:
    return (
        _text(produce_id, f"{label}.produce_id"),
        _text(plan_type, f"{label}.plan_type"),
        _text(exam_effect_type, f"{label}.exam_effect_type"),
    )


def flow_for_episode(episode: LeaderboardReplayEpisode) -> FlowKey:
    """Return the non-idol flow identity used by the prior.

    The flow keeps ``produce_id``, ``plan_type`` and ``exam_effect_type``
    separate.  In particular, Review evidence cannot leak into Aggressive or
    another plan/produce.  Idol-card identity is retained on each observation
    for audit but is intentionally not a broad-policy key.
    """

    return _flow_key(
        episode.produce_id,
        episode.plan_type,
        episode.exam_effect_type,
    )


def flow_string(flow: FlowKey) -> str:
    """Serialize a flow key for compact feature dictionaries and telemetry."""

    return "|".join(flow)


def _parse_flow(value: object) -> FlowKey | None:
    if isinstance(value, str):
        parts = tuple(value.split("|"))
        if len(parts) == 3 and all(isinstance(item, str) and item for item in parts):
            return parts  # type: ignore[return-value]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        parts = tuple(value)
        if len(parts) == 3 and all(isinstance(item, str) and item for item in parts):
            return parts  # type: ignore[return-value]
    return None


def _stage(value: object) -> str | None:
    if isinstance(value, str) and value in STAGES:
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return _STAGE_BY_INDEX.get(value)
    return None


def _history(value: object, *, maximum: int = DEFAULT_HISTORY_SIZE) -> HistoryKey | None:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return None
    values = tuple(item for item in value if isinstance(item, str) and item)
    if len(values) != len(value):
        return None
    return values[-maximum:] if maximum else ()


_MISSING = object()


def _instance_value(value: object, *names: str) -> object:
    """Read a field from a JSON row or one of the typed card records.

    The leaderboard/observer boundary intentionally accepts both shapes.  A
    tiny local reader keeps the signature code independent of the legal gate
    and, importantly, does not turn a missing field into a guessed default.
    """

    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
        return _MISSING
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    return _MISSING


def _canonical_instance_json(value: object) -> object:
    """Detach JSON-compatible semantic fields in deterministic order."""

    if hasattr(value, "to_value") and callable(getattr(value, "to_value")):
        try:
            value = value.to_value()
        except Exception as error:
            raise ValueError("instance semantic value cannot be read") from error
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_instance_json(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_canonical_instance_json(item) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("instance semantic value is not finite")
        return value
    raise ValueError("instance semantic value is not JSON-compatible")


def _semantic_list(
    value: object,
    *,
    kind: Literal["int", "text"],
) -> tuple[object, ...] | None:
    if hasattr(value, "to_value") and callable(getattr(value, "to_value")):
        try:
            value = value.to_value()
        except Exception:
            return None
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return None
    output: list[object] = []
    for item in value:
        if kind == "int":
            if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                return None
            output.append(int(item))
        else:
            if not isinstance(item, str) or not item:
                return None
            output.append(item)
    return tuple(output)


def _customize_identity_for_signature(value: object) -> tuple[object, bool]:
    """Return canonical customize identity and whether it is complete.

    ``None`` (and an omitted identity on legacy observer rows) means unknown,
    not an empty customization.  The one safe exception is a typed local-save
    runtime with an explicitly empty customize-count list: there cannot be a
    hidden customize ID/effect in that state.
    """

    raw = _instance_value(value, "customize_identity")
    if raw is not _MISSING:
        if not isinstance(raw, Mapping):
            return {"unknown": True}, False
        aliases = (
            ("customize_count_list", ("customize_count_list", "customizeCountList"), "int"),
            ("customize_id_list", ("customize_id_list", "customizeIdList"), "text"),
            (
                "customize_grow_effect_id_list",
                ("customize_grow_effect_id_list", "customizeGrowEffectIdList"),
                "text",
            ),
            (
                "support_upgrade_id_list",
                ("support_upgrade_id_list", "supportUpgradeIdList"),
                "text",
            ),
            (
                "effect_group_id_list",
                ("effect_group_id_list", "effectGroupIdList"),
                "text",
            ),
        )
        result: dict[str, object] = {}
        for canonical, names, kind in aliases:
            field = _instance_value(raw, *names)
            if field is _MISSING:
                return {"unknown": True}, False
            values = _semantic_list(field, kind=kind)  # type: ignore[arg-type]
            if values is None:
                return {"unknown": True}, False
            result[canonical] = list(values)
        if len(result["customize_count_list"]) != len(result["customize_id_list"]):
            return {"unknown": True}, False
        return result, True

    # A few offline decoders flatten customize identity instead of nesting it
    # under ``customize_identity``.  Accept that shape only when every list is
    # present; a partial flattened row remains unknown.
    flattened_names = (
        "customize_count_list",
        "customizeCountList",
        "customize_id_list",
        "customizeIdList",
        "customize_grow_effect_id_list",
        "customizeGrowEffectIdList",
        "support_upgrade_id_list",
        "supportUpgradeIdList",
        "effect_group_id_list",
        "effectGroupIdList",
    )
    if any(_instance_value(value, name) is not _MISSING for name in flattened_names):
        flattened: dict[str, object] = {}
        for canonical, names, kind in (
            ("customize_count_list", ("customize_count_list", "customizeCountList"), "int"),
            ("customize_id_list", ("customize_id_list", "customizeIdList"), "text"),
            (
                "customize_grow_effect_id_list",
                ("customize_grow_effect_id_list", "customizeGrowEffectIdList"),
                "text",
            ),
            (
                "support_upgrade_id_list",
                ("support_upgrade_id_list", "supportUpgradeIdList"),
                "text",
            ),
            (
                "effect_group_id_list",
                ("effect_group_id_list", "effectGroupIdList"),
                "text",
            ),
        ):
            field = _instance_value(value, *names)
            if field is _MISSING:
                return {"unknown": True}, False
            values = _semantic_list(field, kind=kind)  # type: ignore[arg-type]
            if values is None:
                return {"unknown": True}, False
            flattened[canonical] = list(values)
        if len(flattened["customize_count_list"]) != len(flattened["customize_id_list"]):
            return {"unknown": True}, False
        return flattened, True

    scalar_custom = _instance_value(value, "customizes", "customize")
    if scalar_custom is not _MISSING:
        try:
            return {"customizes": _canonical_instance_json(scalar_custom)}, True
        except ValueError:
            return {"unknown": True}, False

    runtime = _instance_value(value, "runtime_state", "runtime")
    count = _instance_value(
        runtime if runtime is not _MISSING else value,
        "customize_count_list",
        "customizeCountList",
    )
    if count is _MISSING:
        payload_method = _instance_value(value, "runtime_payload")
        if callable(payload_method):
            try:
                payload = payload_method()
            except Exception:
                payload = _MISSING
            if isinstance(payload, Mapping):
                count = _instance_value(payload, "customize_count_list", "customizeCountList")
    if count is _MISSING:
        return {"unknown": True}, False
    counts = _semantic_list(count, kind="int")
    if counts is None:
        return {"unknown": True}, False
    # A zero-length count list is a complete statement that no customize
    # identity exists.  Non-empty counts need the ordered IDs/effects too.
    if counts:
        return {"customize_count_list": list(counts), "unknown": True}, False
    return {
        "customize_count_list": [],
        "customize_id_list": [],
        "customize_grow_effect_id_list": [],
        "support_upgrade_id_list": [],
        "effect_group_id_list": [],
    }, True


def _runtime_payload_for_signature(value: object) -> tuple[object, bool]:
    """Extract effect-relevant state, preserving an explicit unknown marker."""

    payload_method = _instance_value(value, "runtime_payload")
    if callable(payload_method):
        try:
            payload = payload_method()
        except Exception:
            return {"unknown": True}, False
        if payload is None:
            return {"unknown": True}, False
        try:
            return _canonical_instance_json(payload), True
        except ValueError:
            return {"unknown": True}, False

    runtime = _instance_value(value, "runtime_state", "runtime")
    effect_fields = (
        ("play_count", ("play_count", "playCount")),
        ("status_effect", ("status_effect", "statusEffect")),
        (
            "affect_grow_effect_id_list",
            ("affect_grow_effect_id_list", "affectGrowEffectIdList"),
        ),
        (
            "grow_effect_exam_start_after_list",
            ("grow_effect_exam_start_after_list", "growEffectExamStartAfterList"),
        ),
        (
            "is_move_produce_exam_effect_use_in_turn",
            (
                "is_move_produce_exam_effect_use_in_turn",
                "isMoveProduceExamEffectUseInTurn",
            ),
        ),
        (
            "stamina_consumption_specify_effect_list",
            (
                "stamina_consumption_specify_effect_list",
                "staminaConsumptionSpecifyEffectList",
            ),
        ),
        ("support_upgrade_ids", ("support_upgrade_ids", "supportUpgradeIds")),
        ("fixed_deck_order", ("fixed_deck_order", "fixedDeckOrder")),
        ("produce_card_skin_id", ("produce_card_skin_id", "produceCardSkinId")),
        (
            "produce_card_skin_asset_id",
            ("produce_card_skin_asset_id", "produceCardSkinAssetId"),
        ),
    )
    result: dict[str, object] = {}
    found = False
    for canonical, names in effect_fields:
        field = _instance_value(runtime if runtime is not _MISSING else value, *names)
        if field is _MISSING and runtime is not _MISSING:
            field = _instance_value(value, *names)
        if field is not _MISSING:
            result[canonical] = field
            found = True
    # Observer/native adapter rows may put effects under one explicit object.
    explicit = _instance_value(value, "instance_effects", "runtime_effects", "effects", "play_effects")
    if explicit is not _MISSING:
        result["explicit_effects"] = explicit
        found = True
    digest = _instance_value(value, "runtime_state_digest", "runtimeStateDigest")
    if digest is not _MISSING:
        if not isinstance(digest, str) or not digest:
            return {"unknown": True}, False
        result["runtime_state_digest"] = digest
        found = True
    if not found:
        return {"unknown": True}, False
    try:
        return _canonical_instance_json(result), True
    except ValueError:
        return {"unknown": True}, False


def instance_semantic_payload(value: object) -> Mapping[str, object] | None:
    """Return the non-GUID semantic payload for one card instance.

    A ``None`` result means at least one required identity/effect component is
    unknown.  GUID, slot, position, occurrence and detector geometry are
    intentionally excluded so two independent runs can compare this value.
    """

    card_id = _instance_value(value, "card_id", "id")
    upgrade = _instance_value(value, "effective_upgrade", "upgrade", "upgrade_count", "upgradeCount")
    if not isinstance(card_id, str) or not card_id:
        return None
    if isinstance(upgrade, bool) or not isinstance(upgrade, int) or upgrade < 0:
        return None
    customize, customize_known = _customize_identity_for_signature(value)
    effects, effects_known = _runtime_payload_for_signature(value)
    if not customize_known or not effects_known:
        return None
    payload: dict[str, object] = {
        "schema": INSTANCE_SIGNATURE_SCHEMA,
        "card_id": card_id,
        "upgrade": int(upgrade),
        "customize_identity": customize,
        "instance_effects": effects,
    }
    # Keep the upgrade decomposition when it is available.  It is semantically
    # useful for support/temporary upgrades even when effective_upgrade agrees.
    for canonical, names in (
        ("base_upgrade", ("base_upgrade", "baseUpgrade")),
        ("temporary_upgrade", ("temporary_upgrade", "temporaryUpgrade")),
    ):
        field = _instance_value(value, *names)
        if field is not _MISSING:
            if isinstance(field, bool) or not isinstance(field, int) or field < 0:
                return None
            payload[canonical] = int(field)
    try:
        return _canonical_instance_json(payload)  # type: ignore[return-value]
    except ValueError:
        return None


def instance_signature(value: object) -> str | None:
    """Return a SHA-256 semantic signature, excluding GUID/slot/occurrence."""

    payload = instance_semantic_payload(value)
    if payload is None:
        return None
    try:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# Descriptive aliases used by native/projector adapters.
card_instance_signature = instance_signature
semantic_instance_signature = instance_signature


def instances_semantically_equivalent(left: object, right: object) -> bool:
    """Return true only when both complete semantic signatures are equal."""

    left_signature = instance_signature(left)
    right_signature = instance_signature(right)
    return left_signature is not None and left_signature == right_signature


def _candidate_mapping(value: object) -> Mapping[str, object] | None:
    if isinstance(value, Mapping):
        return value
    fields: dict[str, object] = {}
    for name in (
        "card_id",
        "id",
        "guid",
        "card_guid",
        "slot",
        "position",
        "upgrade",
        "upgrade_count",
        "upgradeCount",
        "effective_upgrade",
        "runtime_state",
        "runtime_payload",
        "runtime_state_digest",
        "customize_identity",
        "instance_signature",
        "candidate_key",
        "occurrence",
    ):
        if hasattr(value, name):
            fields[name] = getattr(value, name)
    return fields or None


def _candidate_card_id(value: object) -> str | None:
    if isinstance(value, str):
        return value if value else None
    raw = _instance_value(value, "card_id", "id")
    return raw if isinstance(raw, str) and raw else None


def _candidate_signature(value: object) -> str | None:
    raw = _instance_value(value, "instance_signature", "semantic_signature")
    if raw is not _MISSING:
        return raw if isinstance(raw, str) and raw else None
    return instance_signature(value)


def _instance_action_key(card_id: str, signature: str) -> str:
    return f"{card_id}{INSTANCE_ACTION_KEY_SEPARATOR}{signature}"


def _occurrence_action_key(card_id: str, occurrence: int) -> str:
    return f"{card_id}{OCCURRENCE_ACTION_KEY_SEPARATOR}{occurrence}"


def candidate_action_keys(values: Sequence[object] | Iterable[object]) -> tuple[str, ...]:
    """Build unique action classes while retaining duplicate occurrences.

    The returned order is first-seen hand order.  A same-ID group collapses to
    the plain ID only when every instance has one equal, complete semantic
    signature.  Distinct signatures get signature keys; unknown signatures
    get occurrence keys.  This is the central rule shared by training and the
    live legal-candidate runtime.
    """

    records = tuple(values)
    by_id: defaultdict[str, list[tuple[int, str | None]]] = defaultdict(list)
    for index, value in enumerate(records):
        card_id = _candidate_card_id(value)
        if card_id is None:
            continue
        by_id[card_id].append((index, _candidate_signature(value)))
    aligned: list[str | None] = [None] * len(records)
    for card_id, group in by_id.items():
        signatures = [signature for _, signature in group]
        equivalent = (
            len(group) == 1
            or all(signature is not None for signature in signatures)
            and len(set(signatures)) == 1
        )
        if equivalent:
            for index, _signature in group:
                aligned[index] = card_id
            continue
        unknown_occurrence = 0
        for index, signature in group:
            if signature is not None:
                aligned[index] = _instance_action_key(card_id, signature)
            else:
                aligned[index] = _occurrence_action_key(card_id, unknown_occurrence)
                unknown_occurrence += 1
    return tuple(dict.fromkeys(value for value in aligned if value is not None))


def candidate_action_keys_aligned(values: Sequence[object] | Iterable[object]) -> tuple[str | None, ...]:
    """Return one action key per input occurrence for runtime GUID joining."""

    records = tuple(values)
    by_id: defaultdict[str, list[tuple[int, str | None]]] = defaultdict(list)
    for index, value in enumerate(records):
        card_id = _candidate_card_id(value)
        if card_id is not None:
            by_id[card_id].append((index, _candidate_signature(value)))
    aligned: list[str | None] = [None] * len(records)
    for card_id, group in by_id.items():
        signatures = [signature for _, signature in group]
        equivalent = (
            len(group) == 1
            or all(signature is not None for signature in signatures)
            and len(set(signatures)) == 1
        )
        if equivalent:
            for index, _signature in group:
                aligned[index] = card_id
            continue
        unknown_occurrence = 0
        for index, signature in group:
            if signature is not None:
                aligned[index] = _instance_action_key(card_id, signature)
            else:
                aligned[index] = _occurrence_action_key(card_id, unknown_occurrence)
                unknown_occurrence += 1
    return tuple(aligned)


def candidate_action_key(value: object, values: Sequence[object] | None = None) -> str | None:
    """Return one candidate key, using ``values`` to resolve duplicate groups."""

    if values is None:
        values = (value,)
    aligned = candidate_action_keys_aligned(values)
    try:
        index = tuple(values).index(value)
    except ValueError:
        return None
    return aligned[index]


@dataclass(frozen=True, slots=True)
class InstanceGroupClassification:
    """Semantic duplicate classification for one stable card ID."""

    card_id: str
    occurrence_count: int
    signatures: tuple[str | None, ...]
    semantic_equivalent: bool
    unknown_signature_count: int
    distinct_signature_count: int

    @property
    def requires_guid_occurrence(self) -> bool:
        return not self.semantic_equivalent

    @property
    def excess_occurrence_count(self) -> int:
        return max(0, self.occurrence_count - 1)

    @property
    def non_equivalent(self) -> bool:
        return self.occurrence_count > 1 and self.distinct_signature_count > 1

    def to_dict(self) -> dict[str, object]:
        return {
            "card_id": self.card_id,
            "occurrence_count": self.occurrence_count,
            "signatures": list(self.signatures),
            "semantic_equivalent": self.semantic_equivalent,
            "requires_guid_occurrence": self.requires_guid_occurrence,
            "unknown_signature_count": self.unknown_signature_count,
            "distinct_signature_count": self.distinct_signature_count,
            "excess_occurrence_count": self.excess_occurrence_count,
            "non_equivalent": self.non_equivalent,
        }


def classify_instance_groups(
    values: Sequence[object] | Iterable[object],
) -> tuple[InstanceGroupClassification, ...]:
    """Classify duplicate IDs without collapsing their runtime occurrences."""

    grouped: defaultdict[str, list[str | None]] = defaultdict(list)
    for value in values:
        card_id = _candidate_card_id(value)
        if card_id is None:
            continue
        grouped[card_id].append(_candidate_signature(value))
    output: list[InstanceGroupClassification] = []
    for card_id, signatures_list in grouped.items():
        if len(signatures_list) <= 1:
            continue
        signatures = tuple(signatures_list)
        known = tuple(signature for signature in signatures if signature is not None)
        equivalent = bool(known) and len(known) == len(signatures) and len(set(known)) == 1
        output.append(
            InstanceGroupClassification(
                card_id=card_id,
                occurrence_count=len(signatures),
                signatures=signatures,
                semantic_equivalent=equivalent,
                unknown_signature_count=len(signatures) - len(known),
                distinct_signature_count=len(set(known)),
            )
        )
    return tuple(output)


# Singular/plural aliases keep the helper discoverable from projector code.
classify_duplicate_instances = classify_instance_groups


def _features(
    value: Mapping[str, object],
    *,
    history_size: int = DEFAULT_HISTORY_SIZE,
) -> tuple[FlowKey, str, int, HistoryKey] | None:
    raw_flow = _parse_flow(value.get("flow"))
    if raw_flow is None:
        try:
            raw_flow = _flow_key(
                value.get("produce_id"),
                value.get("plan_type"),
                value.get("exam_effect_type"),
            )
        except ValueError:
            return None
    stage = _stage(value.get("stage", value.get("step_type")))
    turn = value.get("turn", value.get("turn_index"))
    if stage is None or isinstance(turn, bool) or not isinstance(turn, int) or turn < 1:
        return None
    history = _history(
        value.get("action_history", value.get("history", ())),
        maximum=history_size,
    )
    if history is None:
        return None
    return raw_flow, stage, turn, history


@dataclass(frozen=True, slots=True)
class LeaderboardCardImitationObservation:
    """One behavior-only card-play label backed by settled identity proof.

    ``chosen_guid`` and optional ``candidate_instances`` are retained even
    though scoring is by stable ``card_id``.  They prevent a future adapter
    from silently turning an index-only row into training data: observations
    can only be constructed after the adapter (or another offline evidence
    producer) has supplied the settled runtime GUID and a complete hand.
    Duplicate IDs are valid instances; GUIDs remain local evidence and never
    enter prior feature keys.  ``candidate_set_kind`` describes the alternatives
    represented by ``candidate_ids``.  New telemetry uses ``settled_hand``;
    ``legal`` is retained for explicitly typed legacy evidence and runtime
    audits, but is never inferred from a hand snapshot.
    """

    source_id: str
    flow: FlowKey
    idol_card_id: str
    stage: str
    turn: int
    action_order: int
    action_history: HistoryKey
    candidate_ids: tuple[str, ...]
    chosen_id: str
    chosen_guid: str
    terminal_score: int
    schema: str = SCHEMA
    candidate_set_kind: CandidateSetKind = "legal"
    legal_candidate_ids: tuple[str, ...] | None = None
    # ``candidate_ids`` is the behavior-level stable-ID view.  These optional
    # instance fields retain per-hand identity evidence without ever entering
    # prior keys (GUIDs are local runtime identities, not cross-run features).
    candidate_instances: tuple[Mapping[str, object], ...] = ()
    chosen_instance: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        _text(self.source_id, "observation.source_id")
        if len(self.flow) != 3 or any(not isinstance(value, str) or not value for value in self.flow):
            raise ValueError("observation.flow must be a three-field flow key")
        _text(self.idol_card_id, "observation.idol_card_id")
        if self.stage not in STAGES:
            raise ValueError("observation.stage is unsupported")
        _integer(self.turn, "observation.turn", minimum=1)
        _integer(self.action_order, "observation.action_order", minimum=0)
        if any(not isinstance(value, str) or not value for value in self.action_history):
            raise ValueError("observation.action_history contains an invalid card ID")
        candidates = tuple(self.candidate_ids)
        if not candidates or any(not isinstance(value, str) or not value for value in candidates):
            raise ValueError("observation.candidate_ids must be non-empty card IDs")
        if len(candidates) != len(set(candidates)):
            raise ValueError("observation.candidate_ids must be unique")
        if self.chosen_id not in candidates:
            raise ValueError("observation.chosen_id is outside candidate_ids")
        _text(self.chosen_guid, "observation.chosen_guid")
        _integer(self.terminal_score, "observation.terminal_score")
        if self.schema != SCHEMA:
            raise ValueError("unsupported leaderboard card imitation schema")
        if self.candidate_set_kind not in CANDIDATE_SET_KINDS:
            raise ValueError(
                "observation.candidate_set_kind must be 'settled_hand' or 'legal'"
            )
        if self.legal_candidate_ids is not None:
            legal = tuple(self.legal_candidate_ids)
            if (
                not legal
                or any(not isinstance(value, str) or not value for value in legal)
            ):
                raise ValueError("observation.legal_candidate_ids must contain card IDs")
        instances = tuple(self.candidate_instances)
        if instances:
            seen_guids: set[str] = set()
            seen_slots: set[int] = set()
            instance_ids: list[str] = []
            for index, instance in enumerate(instances):
                if not isinstance(instance, Mapping):
                    raise ValueError(
                        f"observation.candidate_instances[{index}] must be an object"
                    )
                card_id = instance.get("card_id", instance.get("id"))
                guid = instance.get("guid", instance.get("card_guid"))
                slot = instance.get("slot", instance.get("position"))
                if not isinstance(card_id, str) or not card_id:
                    raise ValueError(
                        f"observation.candidate_instances[{index}].card_id is invalid"
                    )
                if not isinstance(guid, str) or not guid:
                    raise ValueError(
                        f"observation.candidate_instances[{index}].guid is invalid"
                    )
                if isinstance(slot, bool) or not isinstance(slot, int) or slot < 0:
                    raise ValueError(
                        f"observation.candidate_instances[{index}].slot is invalid"
                    )
                if guid in seen_guids:
                    raise ValueError(
                        "observation.candidate_instances must have unique GUIDs"
                    )
                if slot in seen_slots:
                    raise ValueError(
                        "observation.candidate_instances must have unique slots"
                    )
                seen_guids.add(guid)
                seen_slots.add(slot)
                instance_ids.append(card_id)
            if tuple(dict.fromkeys(instance_ids)) != candidates:
                raise ValueError(
                    "observation.candidate_instances IDs must match candidate_ids"
                )
        if self.chosen_instance is not None:
            chosen = self.chosen_instance
            if not isinstance(chosen, Mapping):
                raise ValueError("observation.chosen_instance must be an object")
            chosen_id = chosen.get("card_id", chosen.get("id"))
            chosen_guid = chosen.get("guid", chosen.get("card_guid"))
            if chosen_id != self.chosen_id or chosen_guid != self.chosen_guid:
                raise ValueError(
                    "observation.chosen_instance must match chosen_id/chosen_guid"
                )

    @property
    def behavior_candidate_ids(self) -> tuple[str, ...]:
        """Alternatives used for behavior imitation, never runtime legality."""

        return tuple(self.candidate_ids)

    @property
    def candidate_instance_signatures(self) -> tuple[str | None, ...]:
        """Semantic signatures aligned to ``candidate_instances``.

        ``None`` is intentionally observable: a same-ID occurrence whose
        customize/effect identity is unknown must not be merged with another
        occurrence merely because its card ID matches.
        """

        if not self.candidate_instances:
            return ()
        return tuple(_candidate_signature(value) for value in self.candidate_instances)

    @property
    def behavior_candidate_keys(self) -> tuple[str, ...]:
        """Unique action classes used by offline behavior ranking."""

        if not self.candidate_instances:
            return tuple(self.candidate_ids)
        return candidate_action_keys(self.candidate_instances)

    @property
    def candidate_action_keys(self) -> tuple[str, ...]:
        """Alias for the occurrence-aware behavior candidate classes."""

        return self.behavior_candidate_keys

    @property
    def chosen_instance_signature(self) -> str | None:
        if self.chosen_instance is not None:
            return _candidate_signature(self.chosen_instance)
        return None

    @property
    def chosen_action_key(self) -> str:
        """Action class for the chosen instance (stable ID when safe)."""

        if not self.candidate_instances:
            return self.chosen_id
        aligned = candidate_action_keys_aligned(self.candidate_instances)
        for index, instance in enumerate(self.candidate_instances):
            guid = instance.get("guid", instance.get("card_guid"))
            if guid == self.chosen_guid:
                key = aligned[index]
                if key is not None:
                    return key
        # Construction validation normally makes this unreachable.  Keeping a
        # stable fallback preserves the old observation API for hand snapshots
        # created by older callers.
        return self.chosen_id

    @property
    def equivalent_duplicate_card_ids(self) -> tuple[str, ...]:
        """Same-ID groups proven interchangeable by complete signatures."""

        if not self.candidate_instances:
            return ()
        groups: defaultdict[str, list[Mapping[str, object]]] = defaultdict(list)
        for instance in self.candidate_instances:
            card_id = instance.get("card_id", instance.get("id"))
            if isinstance(card_id, str):
                groups[card_id].append(instance)
        return tuple(
            card_id
            for card_id, values in groups.items()
            if len(values) > 1
            and all(_candidate_signature(value) is not None for value in values)
            and len({_candidate_signature(value) for value in values}) == 1
        )

    @property
    def identity_required_duplicate_card_ids(self) -> tuple[str, ...]:
        """Same-ID groups requiring GUID/occurrence identity."""

        if not self.candidate_instances:
            return ()
        groups: defaultdict[str, list[Mapping[str, object]]] = defaultdict(list)
        for instance in self.candidate_instances:
            card_id = instance.get("card_id", instance.get("id"))
            if isinstance(card_id, str):
                groups[card_id].append(instance)
        return tuple(
            card_id
            for card_id, values in groups.items()
            if len(values) > 1
            and not (
                all(_candidate_signature(value) is not None for value in values)
                and len({_candidate_signature(value) for value in values}) == 1
            )
        )

    @property
    def flow_id(self) -> str:
        return flow_string(self.flow)

    def feature_dict(self) -> dict[str, object]:
        return {
            "flow": self.flow_id,
            "produce_id": self.flow[0],
            "plan_type": self.flow[1],
            "exam_effect_type": self.flow[2],
            "stage": self.stage,
            "turn": self.turn,
            "action_history": list(self.action_history),
        }

    def to_dict(self) -> dict[str, object]:
        """Serialize stable labels plus occurrence identity for audit output."""

        return {
            "schema": self.schema,
            "source_id": self.source_id,
            "flow": list(self.flow),
            "idol_card_id": self.idol_card_id,
            "stage": self.stage,
            "turn": self.turn,
            "action_order": self.action_order,
            "action_history": list(self.action_history),
            "candidate_ids": list(self.candidate_ids),
            "candidate_action_keys": list(self.candidate_action_keys),
            "candidate_instance_signatures": list(self.candidate_instance_signatures),
            "chosen_id": self.chosen_id,
            "chosen_guid": self.chosen_guid,
            "chosen_action_key": self.chosen_action_key,
            "chosen_instance_signature": self.chosen_instance_signature,
            "terminal_score": self.terminal_score,
            "candidate_set_kind": self.candidate_set_kind,
            "legal_candidate_ids": (
                None
                if self.legal_candidate_ids is None
                else list(self.legal_candidate_ids)
            ),
            "candidate_instances": [dict(value) for value in self.candidate_instances],
            "chosen_instance": (
                None if self.chosen_instance is None else dict(self.chosen_instance)
            ),
            "equivalent_duplicate_card_ids": list(self.equivalent_duplicate_card_ids),
            "identity_required_duplicate_card_ids": list(
                self.identity_required_duplicate_card_ids
            ),
        }


@dataclass(frozen=True, slots=True)
class LeaderboardCardProjectionReport:
    """Coverage audit for the canonical action-to-card identity boundary.

    ``produce_cards`` is deliberately *not* counted as hand identity
    evidence.  It is a realized deck/loadout list, while ``UseHand.indexes``
    points into a changing runtime hand.  Until a settled replay step supplies
    both values, ``mapped_action_count`` remains zero.
    """

    episode_count: int
    episode_with_observations: int
    action_count: int
    hand_action_count: int
    verified_identity_count: int
    verified_candidate_set_count: int
    verified_turn_count: int
    mapped_action_count: int
    abstained_action_count: int
    reason_counts: Mapping[str, int]
    identity_source: str = "canonical-action-index-only"
    settled_hand_count: int = 0
    legal_candidate_set_count: int = 0
    behavior_only: bool = True

    @property
    def action_coverage(self) -> float:
        return self.mapped_action_count / self.hand_action_count if self.hand_action_count else 0.0

    @property
    def episode_coverage(self) -> float:
        return self.episode_with_observations / self.episode_count if self.episode_count else 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "episode_count": self.episode_count,
            "episode_with_observations": self.episode_with_observations,
            "action_count": self.action_count,
            "hand_action_count": self.hand_action_count,
            "verified_identity_count": self.verified_identity_count,
            "verified_candidate_set_count": self.verified_candidate_set_count,
            "verified_turn_count": self.verified_turn_count,
            "mapped_action_count": self.mapped_action_count,
            "abstained_action_count": self.abstained_action_count,
            "action_coverage": self.action_coverage,
            "episode_coverage": self.episode_coverage,
            "reason_counts": dict(sorted(self.reason_counts.items())),
            "identity_source": self.identity_source,
            "settled_hand_count": self.settled_hand_count,
            "legal_candidate_set_count": self.legal_candidate_set_count,
            "behavior_only": self.behavior_only,
        }


def _verified_action_rows(
    source: Mapping[str, object],
) -> tuple[Mapping[str, object], ...] | None:
    """Read an optional offline evidence extension, never infer one.

    The canonical v2 schema has no ``verified_card_actions`` field.  The
    extension is intentionally not written by the normalizer; it is an input
    contract for a separately reviewed adapter/evidence job.  Each row must
    carry ``mapping_verified=true`` and ``settled=true`` before this module
    will use it.
    """

    raw = source.get("verified_card_actions")
    # ``settled_hand_actions`` is the name emitted by the next native
    # observer contract.  Keep the old extension name as a compatibility
    # alias, but do not merge two sources implicitly.
    if raw is None:
        raw = source.get("settled_hand_actions")
    if raw is None:
        return None
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return None
    if any(not isinstance(value, Mapping) for value in raw):
        return None
    return tuple(value for value in raw if isinstance(value, Mapping))


def _candidate_instance_evidence(
    row: Mapping[str, object],
    *,
    candidates: tuple[str, ...],
    chosen_id: str,
    chosen_guid: str,
) -> tuple[tuple[Mapping[str, object], ...], Mapping[str, object] | None, str | None]:
    """Validate/normalize complete settled-hand instance evidence.

    Card IDs may repeat; GUID and slot may not.  The returned instance rows
    are retained for audit only.  The prior continues to key on stable card
    IDs and never learns a GUID.
    """

    raw_instances: object = row.get("candidate_instances")
    if raw_instances is None:
        snapshot = row.get("hand_snapshot")
        if isinstance(snapshot, Mapping):
            raw_instances = snapshot.get("candidate_instances")
            if raw_instances is None:
                raw_instances = snapshot.get("cards")
    if raw_instances is None:
        return (), None, None
    if not isinstance(raw_instances, Sequence) or isinstance(
        raw_instances, (str, bytes)
    ):
        return (), None, "verified-step-invalid-candidate-instances"
    instances: list[Mapping[str, object]] = []
    seen_guids: set[str] = set()
    seen_slots: set[int] = set()
    instance_ids: list[str] = []
    chosen_instance: Mapping[str, object] | None = None
    for index, raw in enumerate(raw_instances):
        if not isinstance(raw, Mapping):
            return (), None, "verified-step-invalid-candidate-instances"
        card_id = raw.get("card_id", raw.get("id"))
        guid = raw.get("guid", raw.get("card_guid"))
        slot = raw.get("slot", raw.get("position", raw.get("index")))
        upgrade = raw.get("upgrade", raw.get("upgrade_count", raw.get("upgradeCount")))
        play_count = raw.get("play_count", raw.get("playCount"))
        if (
            not isinstance(card_id, str)
            or not card_id
            or not isinstance(guid, str)
            or not guid
            or isinstance(slot, bool)
            or not isinstance(slot, int)
            or slot < 0
            or isinstance(upgrade, bool)
            or not isinstance(upgrade, int)
            or upgrade < 0
            or isinstance(play_count, bool)
            or not isinstance(play_count, int)
            or play_count < 0
        ):
            return (), None, "verified-step-invalid-candidate-instances"
        if guid in seen_guids:
            return (), None, "verified-step-duplicate-candidate-guid"
        if slot in seen_slots:
            return (), None, "verified-step-duplicate-candidate-slot"
        seen_guids.add(guid)
        seen_slots.add(slot)
        normalized = {
            "card_id": card_id,
            "guid": guid,
            "slot": slot,
            "position": slot,
            "upgrade": upgrade,
            "play_count": play_count,
        }
        if "customize_identity" in raw:
            # The telemetry adapter has already reduced malformed/partial
            # identity to None.  Preserve that explicit unknown marker (and
            # complete identity object) for offline instance auditing without
            # entering it into prior feature keys.
            normalized["customize_identity"] = raw["customize_identity"]
        # Preserve explicit effect/runtime evidence in the normalized audit
        # row.  Without this, the generated signature would be retained but
        # recomputing ``candidate_instance_signatures`` would see only the
        # scalar fields and incorrectly report the identity as unknown.
        for field_name in (
            "instance_effects",
            "runtime_effects",
            "effects",
            "play_effects",
            "runtime_state_digest",
            "fixed_deck_order",
            "support_upgrade_ids",
            "support_upgrade_id_list",
        ):
            if field_name in raw:
                normalized[field_name] = raw[field_name]
        # The signature excludes runtime-only GUID/slot/occurrence fields.  A
        # missing value is retained as an explicit unknown marker so callers
        # can distinguish "not comparable" from two equal signatures.
        signature = instance_signature(raw)
        normalized["instance_signature"] = signature
        normalized["instance_signature_known"] = signature is not None
        instances.append(normalized)
        instance_ids.append(card_id)
        if card_id == chosen_id and guid == chosen_guid:
            if chosen_instance is not None:
                return (), None, "verified-step-duplicate-chosen-instance"
            chosen_instance = normalized
    if tuple(dict.fromkeys(instance_ids)) != candidates:
        return (), None, "verified-step-candidate-instances-mismatch"
    if chosen_instance is None:
        return (), None, "verified-step-chosen-instance-unavailable"
    return tuple(instances), chosen_instance, None


def _verified_observations(
    source: Mapping[str, object],
    episode: LeaderboardReplayEpisode,
    *,
    history_size: int,
) -> tuple[tuple[LeaderboardCardImitationObservation, ...], Counter[str]]:
    """Build behavior labels only from a complete, explicit hand extension.

    A row explicitly marked ``legal`` is a runtime candidate contract, not a
    replacement for a settled-hand capture.  It is accepted only when the
    same row also carries a complete ``settled_hand_ids`` list.  Rows from the
    v1 extension that omitted ``candidate_set_kind`` retain their old typed
    ``legal`` observation for compatibility; new adapter output is always
    marked ``settled_hand``.
    """

    reasons: Counter[str] = Counter()
    verified = _verified_action_rows(source)
    if verified is None:
        reasons["missing-verified-card-identity"] += sum(
            action.action_type == "use-hand" for action in episode.actions
        )
        return (), reasons
    by_order: dict[int, Mapping[str, object]] = {}
    for index, row in enumerate(verified):
        raw_order = row.get("order", index)
        if isinstance(raw_order, bool) or not isinstance(raw_order, int) or raw_order < 0:
            reasons["invalid-verified-action-order"] += 1
            return (), reasons
        if raw_order in by_order:
            reasons["duplicate-verified-action-order"] += 1
            return (), reasons
        by_order[raw_order] = row

    hand_actions = tuple(
        action
        for action in sorted(episode.actions, key=lambda value: value.order)
        if action.action_type == "use-hand"
    )
    if not hand_actions:
        reasons["no-hand-actions"] += 1
        return (), reasons
    if any(action.order not in by_order for action in hand_actions):
        reasons["verified-card-identity-incomplete"] += sum(
            action.order not in by_order for action in hand_actions
        )
        return (), reasons

    flow = flow_for_episode(episode)
    output: list[LeaderboardCardImitationObservation] = []
    history: list[str] = []
    for action in hand_actions:
        row = by_order[action.order]
        if row.get("mapping_verified") is not True or row.get("settled") is not True:
            reasons["verified-step-not-settled"] += 1
            return (), reasons
        raw_card_id = row.get("card_id", row.get("settled_card_id"))
        raw_guid = row.get("guid", row.get("card_guid", row.get("settled_guid")))
        if not isinstance(raw_card_id, str) or not raw_card_id:
            reasons["verified-step-missing-card-id"] += 1
            return (), reasons
        if not isinstance(raw_guid, str) or not raw_guid:
            reasons["verified-step-missing-guid"] += 1
            return (), reasons
        raw_turn = row.get("turn", row.get("turn_index"))
        if isinstance(raw_turn, bool) or not isinstance(raw_turn, int) or raw_turn < 1:
            reasons["verified-step-missing-turn"] += 1
            return (), reasons
        raw_kind = row.get("candidate_set_kind")
        legacy_kind = raw_kind is None
        if raw_kind is None:
            candidate_kind: CandidateSetKind = "legal"
        elif raw_kind in CANDIDATE_SET_KINDS:
            candidate_kind = raw_kind  # type: ignore[assignment]
        else:
            reasons["verified-step-invalid-candidate-set-kind"] += 1
            return (), reasons
        if row.get("candidate_set_complete") is not True:
            reasons["verified-step-candidate-set-not-complete"] += 1
            return (), reasons
        raw_candidates = row.get(
            "settled_hand_ids"
            if candidate_kind == "settled_hand"
            else "legal_candidate_ids"
        )
        if raw_candidates is None:
            # ``candidate_ids`` was the v1 field.  It is still accepted only
            # for a legacy row or an explicitly settled-hand row.
            raw_candidates = row.get("candidate_ids")
        if not isinstance(raw_candidates, Sequence) or isinstance(raw_candidates, (str, bytes)):
            reasons["verified-step-missing-candidate-set"] += 1
            return (), reasons
        if any(not isinstance(value, str) or not value for value in raw_candidates):
            reasons["verified-step-invalid-candidate-set"] += 1
            return (), reasons
        # A settled hand may contain multiple runtime instances of one card
        # ID.  The behavior prior sees stable card types in first-seen hand
        # order; complete instance evidence is retained separately below.
        candidates = tuple(dict.fromkeys(str(value) for value in raw_candidates))
        if not candidates:
            reasons["verified-step-invalid-candidate-set"] += 1
            return (), reasons
        # Explicit ``legal`` rows are not a training hand.  Require a
        # separately supplied settled-hand identity before using the row for
        # behavior examples; the legal list remains audit metadata only.
        settled_hand_ids: tuple[str, ...] | None = None
        if candidate_kind == "legal" and not legacy_kind:
            raw_hand = row.get("settled_hand_ids", row.get("hand_card_ids"))
            if not isinstance(raw_hand, Sequence) or isinstance(raw_hand, (str, bytes)):
                reasons["verified-step-settled-hand-unavailable"] += 1
                return (), reasons
            if any(not isinstance(value, str) or not value for value in raw_hand):
                reasons["verified-step-invalid-settled-hand"] += 1
                return (), reasons
            settled_hand_ids = tuple(dict.fromkeys(str(value) for value in raw_hand))
            if not settled_hand_ids:
                reasons["verified-step-invalid-settled-hand"] += 1
                return (), reasons
            candidates = settled_hand_ids
            candidate_kind = "settled_hand"
        if raw_card_id not in candidates:
            reasons["verified-step-chosen-outside-candidates"] += 1
            return (), reasons
        candidate_instances: tuple[Mapping[str, object], ...] = ()
        chosen_instance: Mapping[str, object] | None = None
        if candidate_kind == "settled_hand":
            candidate_instances, chosen_instance, instance_error = (
                _candidate_instance_evidence(
                    row,
                    candidates=candidates,
                    chosen_id=raw_card_id,
                    chosen_guid=raw_guid,
                )
            )
            # Complete duplicate-ID hands must not be projected from IDs
            # alone.  Unique-ID legacy rows remain readable when older
            # artifacts predate candidate_instances.
            raw_instance_source = row.get("candidate_instances")
            if raw_instance_source is None and isinstance(
                row.get("hand_snapshot"), Mapping
            ):
                raw_instance_source = row["hand_snapshot"].get("cards")  # type: ignore[index]
            if instance_error is not None or (
                len(candidates) < len(raw_candidates) and not candidate_instances
            ):
                reasons[instance_error or "verified-step-missing-candidate-instances"] += 1
                return (), reasons
            if instance_error is None and raw_instance_source is not None:
                # Non-empty instance input was present and has been validated.
                pass
        raw_legal = row.get("legal_candidate_ids")
        legal_candidate_ids: tuple[str, ...] | None = None
        if raw_legal is not None:
            if not isinstance(raw_legal, Sequence) or isinstance(raw_legal, (str, bytes)):
                reasons["verified-step-invalid-legal-candidate-set"] += 1
                return (), reasons
            legal_candidate_ids = tuple(
                value for value in raw_legal if isinstance(value, str) and value
            )
            if (
                not legal_candidate_ids
                or len(legal_candidate_ids) != len(raw_legal)
            ):
                reasons["verified-step-invalid-legal-candidate-set"] += 1
                return (), reasons
            if len(legal_candidate_ids) != len(set(legal_candidate_ids)):
                # A native legal GUID set may legitimately contain two copies
                # of one card ID.  Stable IDs alone cannot prove those
                # occurrences, so require the already validated complete hand
                # instance evidence before retaining duplicate legal IDs.
                if not candidate_instances:
                    reasons["verified-step-duplicate-legal-id-without-instances"] += 1
                    return (), reasons
                available = Counter(
                    instance.get("card_id", instance.get("id"))
                    for instance in candidate_instances
                )
                wanted = Counter(legal_candidate_ids)
                if any(
                    not isinstance(card_id, str) or count > available.get(card_id, 0)
                    for card_id, count in wanted.items()
                ):
                    reasons["verified-step-duplicate-legal-id-instance-mismatch"] += 1
                    return (), reasons
        raw_history = row.get("action_history")
        if raw_history is None:
            action_history = tuple(history[-history_size:]) if history_size else ()
        else:
            action_history = _history(raw_history, maximum=history_size)
            if action_history is None:
                reasons["verified-step-invalid-history"] += 1
                return (), reasons
        output.append(
            LeaderboardCardImitationObservation(
                source_id=episode.trajectory_id,
                flow=flow,
                idol_card_id=episode.idol_card_id,
                stage=str(episode.step_type),
                turn=raw_turn,
                action_order=action.order,
                action_history=action_history,
                candidate_ids=candidates,
                chosen_id=raw_card_id,
                chosen_guid=raw_guid,
                terminal_score=episode.terminal_score,
                candidate_set_kind=candidate_kind,
                legal_candidate_ids=legal_candidate_ids,
                candidate_instances=candidate_instances,
                chosen_instance=chosen_instance,
            )
        )
        history.append(raw_card_id)
    return tuple(output), reasons


def project_leaderboard_episode(
    source: LeaderboardReplayEpisode | Mapping[str, object] | str | Path,
    *,
    history_size: int = DEFAULT_HISTORY_SIZE,
) -> tuple[LeaderboardCardImitationObservation, ...]:
    """Return labels only when explicit settled-action evidence is present.

    There is intentionally no fallback to ``produce_cards`` or to an index
    frequency model.  A plain canonical row therefore returns ``()``.
    """

    _integer(history_size, "history_size")
    episode = read_leaderboard_episode(source)
    if isinstance(source, Mapping):
        raw: Mapping[str, object] = source
    elif isinstance(source, LeaderboardReplayEpisode):
        raw = {}
    else:
        # Keep optional verified extensions when the public API receives a
        # JSON/JSONL path.  ``read_leaderboard_episode`` validates the same
        # first row, while this read preserves fields unknown to the typed
        # canonical episode (including settled-hand evidence).
        rows = _raw_episode_rows(Path(source))
        raw = rows[0] if rows else {}
    observations, _reasons = _verified_observations(
        raw,
        episode,
        history_size=history_size,
    )
    return observations


def _raw_episode_rows(path: Path) -> tuple[Mapping[str, object], ...]:
    if not path.is_file():
        raise FileNotFoundError(path)
    text = path.read_text(encoding="utf-8-sig")
    if not text.strip():
        raise ValueError("leaderboard episode source is empty")
    rows: list[Mapping[str, object]] = []
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        decoded = None
    if isinstance(decoded, Mapping):
        rows.append(decoded)
    elif isinstance(decoded, Sequence) and not isinstance(decoded, (str, bytes)):
        for index, value in enumerate(decoded):
            if not isinstance(value, Mapping):
                raise ValueError(f"leaderboard episode array row {index} is not an object")
            rows.append(value)
    elif decoded is not None:
        raise ValueError("leaderboard episode source must contain objects")
    else:
        for line_number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"leaderboard episode line {line_number} is malformed") from error
            if not isinstance(value, Mapping):
                raise ValueError(f"leaderboard episode line {line_number} is not an object")
            rows.append(value)
    if not rows:
        raise ValueError("leaderboard episode source is empty")
    return tuple(rows)


def build_leaderboard_card_imitation_observations(
    source: str | Path | Sequence[str | Path | Mapping[str, object]] = DEFAULT_LEADERBOARD_EPISODES,
    *,
    history_size: int = DEFAULT_HISTORY_SIZE,
) -> tuple[tuple[LeaderboardCardImitationObservation, ...], LeaderboardCardProjectionReport]:
    """Build behavior-only examples and a projection coverage report."""

    if isinstance(source, (str, Path)):
        rows: tuple[str | Path | Mapping[str, object], ...] = _raw_episode_rows(Path(source))
    else:
        rows = tuple(source)
    result: list[LeaderboardCardImitationObservation] = []
    reasons: Counter[str] = Counter()
    action_count = 0
    hand_action_count = 0
    verified_identity_count = 0
    verified_candidate_set_count = 0
    verified_turn_count = 0
    settled_hand_count = 0
    legal_candidate_set_count = 0
    mapped_action_count = 0
    episode_with_observations = 0
    for index, raw in enumerate(rows):
        try:
            episode = read_leaderboard_episode(raw)
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            reasons["malformed-episode"] += 1
            continue
        action_count += len(episode.actions)
        hand_action_count += sum(action.action_type == "use-hand" for action in episode.actions)
        try:
            raw_mapping = raw if isinstance(raw, Mapping) else {}
            observations, local_reasons = _verified_observations(
                raw_mapping,
                episode,
                history_size=history_size,
            )
        except (KeyError, OSError, TypeError, ValueError):
            reasons["unprojectable-episode"] += 1
            continue
        reasons.update(local_reasons)
        result.extend(observations)
        mapped_action_count += len(observations)
        verified_identity_count += len(observations)
        verified_candidate_set_count += len(observations)
        verified_turn_count += len(observations)
        settled_hand_count += sum(
            value.candidate_set_kind == "settled_hand" for value in observations
        )
        legal_candidate_set_count += sum(
            value.candidate_set_kind == "legal" for value in observations
        )
        if observations:
            episode_with_observations += 1
        if observations and len(observations) < sum(
            action.action_type == "use-hand" for action in episode.actions
        ):
            reasons["verified-card-identity-incomplete"] += (
                sum(action.action_type == "use-hand" for action in episode.actions)
                - len(observations)
            )
    # A canonical stage row with no mapped hand action still contributes to
    # action/episode counts above; malformed source rows are not hidden.
    reasons["mapped"] = mapped_action_count
    report = LeaderboardCardProjectionReport(
        episode_count=len(rows),
        episode_with_observations=episode_with_observations,
        action_count=action_count,
        hand_action_count=hand_action_count,
        verified_identity_count=verified_identity_count,
        verified_candidate_set_count=verified_candidate_set_count,
        verified_turn_count=verified_turn_count,
        mapped_action_count=mapped_action_count,
        abstained_action_count=max(0, hand_action_count - mapped_action_count),
        reason_counts=dict(reasons),
        identity_source=(
            "settled-hand-observer"
            if settled_hand_count
            else (
                "explicit-legacy-legal-evidence"
                if legal_candidate_set_count
                else "canonical-action-index-only"
            )
        ),
        settled_hand_count=settled_hand_count,
        legal_candidate_set_count=legal_candidate_set_count,
    )
    return tuple(result), report


@dataclass(frozen=True, slots=True)
class ImitationRanking:
    """A behavior rank plus explicit candidate-contract evidence.

    ``candidate_set_kind='legal'`` means the caller supplied a set already
    proven by the native planner.  ``'settled_hand'`` is only for offline
    behavior evaluation and must not be forwarded to an executor.
    """

    ranked: tuple[str, ...]
    scores: Mapping[str, int]
    known_candidates: tuple[str, ...]
    unknown_candidates: tuple[str, ...]
    source: Literal["history", "stage-turn", "stage", "flow", "none"]
    abstained: bool
    reason: str | None = None
    candidate_set_kind: CandidateSetKind = "legal"
    behavior_only: bool = True

    @property
    def top1(self) -> str | None:
        return self.ranked[0] if self.ranked else None

    def to_dict(self) -> dict[str, object]:
        return {
            "ranked": list(self.ranked),
            "scores": dict(self.scores),
            "known_candidates": list(self.known_candidates),
            "unknown_candidates": list(self.unknown_candidates),
            "source": self.source,
            "abstained": self.abstained,
            "reason": self.reason,
            "candidate_set_kind": self.candidate_set_kind,
            "behavior_only": self.behavior_only,
        }


@dataclass(frozen=True, slots=True)
class LeaderboardCardImitationPrior:
    """Flow/stage/turn behavior-only card-play imitation scores.

    ``history_counts`` and ``stage_turn_counts`` store terminal-score-weighted
    votes.  The public score is a bounded 0..1000 share within the selected
    context, making it safe to add as a small advisory bonus elsewhere.  This
    object is not an RL transition/reward model and never establishes legal
    actions.
    """

    observation_count: int
    source_count: int
    flow_source_counts: Mapping[FlowKey, int]
    known_cards_by_flow: Mapping[FlowKey, frozenset[str]]
    history_counts: Mapping[tuple[FlowKey, str, int, HistoryKey, str], float]
    stage_turn_counts: Mapping[tuple[FlowKey, str, int, str], float]
    history_size: int = DEFAULT_HISTORY_SIZE
    schema: str = SCHEMA
    abstaining_reason: str | None = None
    projection_report: LeaderboardCardProjectionReport | None = None
    source_path: Path | None = None
    source_sha256: str | None = None
    # ``known_cards_by_flow`` remains the stable-ID compatibility view.  This
    # parallel scope contains occurrence-aware signature/occurrence keys used
    # whenever a hand has non-equivalent same-ID instances.
    known_action_keys_by_flow: Mapping[FlowKey, frozenset[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.schema != SCHEMA:
            raise ValueError("unsupported leaderboard card imitation prior schema")
        if self.observation_count < 0 or self.source_count < 0:
            raise ValueError("card imitation prior counts cannot be negative")
        if self.observation_count == 0 and self.source_count != 0:
            raise ValueError("empty card imitation prior must have zero sources")
        if self.observation_count > 0 and self.source_count < 1:
            raise ValueError("non-empty card imitation prior requires a source")
        _integer(self.history_size, "history_size")
        if any(value < 0 for value in self.history_counts.values()) or any(
            value < 0 for value in self.stage_turn_counts.values()
        ):
            raise ValueError("imitation prior counts must be non-negative")

    @classmethod
    def from_observations(
        cls,
        observations: Sequence[LeaderboardCardImitationObservation],
        *,
        history_size: int = DEFAULT_HISTORY_SIZE,
        projection_report: LeaderboardCardProjectionReport | None = None,
        source_path: Path | None = None,
        source_sha256: str | None = None,
    ) -> "LeaderboardCardImitationPrior":
        _integer(history_size, "history_size")
        values = tuple(observations)
        if not values:
            return cls(
                observation_count=0,
                source_count=0,
                flow_source_counts={},
                known_cards_by_flow={},
                history_counts={},
                stage_turn_counts={},
                history_size=history_size,
                abstaining_reason="verified-card-identity-unavailable",
                projection_report=projection_report,
                source_path=source_path,
                source_sha256=source_sha256,
            )
        history_counts: defaultdict[tuple[FlowKey, str, int, HistoryKey, str], float] = defaultdict(float)
        stage_turn_counts: defaultdict[tuple[FlowKey, str, int, str], float] = defaultdict(float)
        flow_cards: defaultdict[FlowKey, set[str]] = defaultdict(set)
        flow_action_keys: defaultdict[FlowKey, set[str]] = defaultdict(set)
        flow_sources: defaultdict[FlowKey, set[str]] = defaultdict(set)
        maximum_by_flow: dict[FlowKey, int] = {}
        for observation in values:
            if not isinstance(observation, LeaderboardCardImitationObservation):
                raise TypeError("imitation observations must use the typed observation class")
            maximum_by_flow[observation.flow] = max(
                maximum_by_flow.get(observation.flow, 0), observation.terminal_score
            )
            flow_cards[observation.flow].update(observation.candidate_ids)
            flow_action_keys[observation.flow].update(observation.behavior_candidate_keys)
            flow_sources[observation.flow].add(observation.source_id)
        for observation in values:
            maximum = maximum_by_flow[observation.flow]
            # One vote remains for a zero/unknown terminal score.  The upper
            # bound keeps score weighting from turning one large run into a
            # hard-coded policy.
            weight = 1.0 + 4.0 * (
                observation.terminal_score / maximum if maximum > 0 else 0.0
            )
            history = tuple(observation.action_history[-history_size:]) if history_size else ()
            chosen_key = observation.chosen_action_key
            history_counts[
                (observation.flow, observation.stage, observation.turn, history, chosen_key)
            ] += weight
            stage_turn_counts[
                (observation.flow, observation.stage, observation.turn, chosen_key)
            ] += weight
        return cls(
            observation_count=len(values),
            source_count=len({observation.source_id for observation in values}),
            flow_source_counts={
                flow: len(sources) for flow, sources in flow_sources.items()
            },
            known_cards_by_flow={
                flow: frozenset(cards) for flow, cards in flow_cards.items()
            },
            history_counts=dict(history_counts),
            stage_turn_counts=dict(stage_turn_counts),
            known_action_keys_by_flow={
                flow: frozenset(keys) for flow, keys in flow_action_keys.items()
            },
            history_size=history_size,
            abstaining_reason=None,
            projection_report=projection_report,
            source_path=source_path,
            source_sha256=source_sha256,
        )

    def _context_scores(
        self,
        flow: FlowKey,
        stage: str,
        turn: int,
        history: HistoryKey,
    ) -> tuple[
        dict[str, int],
        Literal["history", "stage-turn", "stage", "flow", "none"],
    ]:
        exact: dict[str, float] = {
            card: value
            for (key_flow, key_stage, key_turn, key_history, card), value in self.history_counts.items()
            if key_flow == flow and key_stage == stage and key_turn == turn and key_history == history
        }
        source: Literal["history", "stage-turn", "stage", "flow", "none"] = "history"
        selected: Mapping[str, float] = exact
        if not selected:
            source = "stage-turn"
            selected = {
                card: value
                for (key_flow, key_stage, key_turn, card), value in self.stage_turn_counts.items()
                if key_flow == flow and key_stage == stage and key_turn == turn
            }
        if not selected:
            source = "stage"
            stage_values: defaultdict[str, float] = defaultdict(float)
            for (key_flow, key_stage, _key_turn, card), value in self.stage_turn_counts.items():
                if key_flow == flow and key_stage == stage:
                    stage_values[card] += value
            selected = dict(stage_values)
        if not selected:
            source = "flow"
            flow_values: defaultdict[str, float] = defaultdict(float)
            for (key_flow, _key_stage, _key_turn, card), value in self.stage_turn_counts.items():
                if key_flow == flow:
                    flow_values[card] += value
            selected = dict(flow_values)
        if not selected:
            return {}, "none"
        total = sum(selected.values())
        if total <= 0:
            return {}, "none"
        return {
            card: max(0, min(MAX_SCORE, round(MAX_SCORE * value / total)))
            for card, value in selected.items()
        }, source

    def _parse_features(self, features: Mapping[str, object]) -> tuple[FlowKey, str, int, HistoryKey] | None:
        return _features(features, history_size=self.history_size)

    def known_card_ids(self, features: Mapping[str, object]) -> frozenset[str]:
        parsed = self._parse_features(features)
        return frozenset() if parsed is None else self.known_cards_by_flow.get(parsed[0], frozenset())

    def score_for(self, features: Mapping[str, object], card_id: str) -> int:
        """Return a bounded score for a stable ID or occurrence-aware key."""

        if not isinstance(card_id, str) or not card_id:
            return 0
        if self.observation_count == 0:
            return 0
        parsed = self._parse_features(features)
        if parsed is None:
            return 0
        flow, stage, turn, history = parsed
        stable_scope = self.known_cards_by_flow.get(flow, frozenset())
        action_scope = self.known_action_keys_by_flow.get(flow, stable_scope)
        if card_id not in action_scope and card_id not in stable_scope:
            return 0
        scores, _source = self._context_scores(flow, stage, turn, history)
        return scores.get(card_id, 0)

    def rank_with_evidence(
        self,
        features: Mapping[str, object],
        legal_card_ids: Sequence[object],
        *,
        candidate_set_kind: CandidateSetKind = "legal",
    ) -> ImitationRanking:
        """Rank a native-planner-proven legal candidate set.

        ``candidate_set_kind`` is deliberately checked at this boundary.  A
        settled-hand snapshot cannot be passed to this runtime-shaped API by
        accident; use :meth:`rank_behavior_with_evidence` for offline
        evaluation instead.
        """

        legal = candidate_action_keys(legal_card_ids)
        if candidate_set_kind != "legal":
            return ImitationRanking(
                (),
                {},
                (),
                legal,
                "none",
                True,
                "runtime-requires-legal-candidates",
                candidate_set_kind=candidate_set_kind
                if candidate_set_kind in CANDIDATE_SET_KINDS
                else "legal",
            )
        return self._rank_candidates(features, legal, candidate_set_kind="legal")

    def rank_advisory_with_evidence(
        self,
        features: Mapping[str, object],
        legal_card_ids: Sequence[object],
    ) -> ImitationRanking:
        """Return a complete legal ordering for bounded advisory use.

        Unlike the strict imitation dispatcher boundary, an unknown card does
        not make the whole native action set unusable.  Known cards receive
        their bounded 0..1000 behavior score; unknown/no-vote cards retain the
        caller's native order at score zero.  At least one positive learned
        score is still required, so this method cannot invent a preference in
        a completely unseen flow.
        """

        parsed = self._parse_features(features)
        legal = candidate_action_keys(legal_card_ids)
        if self.observation_count == 0:
            return ImitationRanking(
                (), {}, (), legal, "none", True,
                self.abstaining_reason or "verified-card-identity-unavailable",
                candidate_set_kind="legal",
            )
        if parsed is None:
            return ImitationRanking(
                (), {}, (), legal, "none", True, "invalid-features",
                candidate_set_kind="legal",
            )
        if not legal:
            return ImitationRanking(
                (), {}, (), (), "none", True, "empty-legal-candidates",
                candidate_set_kind="legal",
            )
        flow, stage, turn, history = parsed
        known_scope = self.known_action_keys_by_flow.get(
            flow, self.known_cards_by_flow.get(flow, frozenset())
        )
        known = tuple(value for value in legal if value in known_scope)
        unknown = tuple(value for value in legal if value not in known_scope)
        context_scores, source = self._context_scores(flow, stage, turn, history)
        scores = {value: int(context_scores.get(value, 0)) for value in legal}
        # A context can contain votes while none of its voted cards are in the
        # current native legal set.  In that case, fall back across turns (and
        # then stages) for these exact candidate IDs instead of returning a
        # useless all-zero context.
        if not any(value > 0 for value in scores.values()):
            stage_counts: defaultdict[str, float] = defaultdict(float)
            for (key_flow, key_stage, _key_turn, card), value in self.stage_turn_counts.items():
                if key_flow == flow and key_stage == stage and card in legal:
                    stage_counts[card] += value
            if stage_counts:
                total = sum(stage_counts.values())
                if total > 0:
                    scores = {
                        value: max(
                            0,
                            min(MAX_SCORE, round(MAX_SCORE * stage_counts.get(value, 0.0) / total)),
                        )
                        for value in legal
                    }
                    source = "stage"
        if not any(value > 0 for value in scores.values()):
            flow_counts: defaultdict[str, float] = defaultdict(float)
            for (key_flow, _key_stage, _key_turn, card), value in self.stage_turn_counts.items():
                if key_flow == flow and card in legal:
                    flow_counts[card] += value
            if flow_counts:
                total = sum(flow_counts.values())
                if total > 0:
                    scores = {
                        value: max(
                            0,
                            min(MAX_SCORE, round(MAX_SCORE * flow_counts.get(value, 0.0) / total)),
                        )
                        for value in legal
                    }
                    source = "flow"
        if not any(value > 0 for value in scores.values()):
            return ImitationRanking(
                (), scores, known, unknown, "none", True,
                "no-context-evidence",
                candidate_set_kind="legal",
            )
        order = {value: index for index, value in enumerate(legal)}
        ranked = tuple(
            sorted(legal, key=lambda value: (-scores[value], order[value]))
        )
        return ImitationRanking(
            ranked,
            scores,
            known,
            unknown,
            source,
            False,
            "partial-unknown-candidates-advisory-zero"
            if unknown
            else None,
            candidate_set_kind="legal",
        )

    def _rank_candidates(
        self,
        features: Mapping[str, object],
        candidates: Sequence[object],
        *,
        candidate_set_kind: CandidateSetKind,
    ) -> ImitationRanking:
        parsed = self._parse_features(features)
        normalised = candidate_action_keys(candidates)
        if self.observation_count == 0:
            return ImitationRanking(
                (),
                {},
                (),
                normalised,
                "none",
                True,
                self.abstaining_reason or "verified-card-identity-unavailable",
                candidate_set_kind=candidate_set_kind,
            )
        if parsed is None:
            return ImitationRanking(
                (), {}, (), normalised, "none", True, "invalid-features",
                candidate_set_kind=candidate_set_kind,
            )
        if not normalised:
            reason = (
                "empty-legal-candidates"
                if candidate_set_kind == "legal"
                else "empty-settled-hand"
            )
            return ImitationRanking(
                (), {}, (), (), "none", True, reason,
                candidate_set_kind=candidate_set_kind,
            )
        flow, stage, turn, history = parsed
        known_scope = self.known_action_keys_by_flow.get(
            flow, self.known_cards_by_flow.get(flow, frozenset())
        )
        known = tuple(value for value in normalised if value in known_scope)
        unknown = tuple(value for value in normalised if value not in known_scope)
        scores, source = self._context_scores(flow, stage, turn, history)
        scored = {value: scores[value] for value in known if scores.get(value, 0) > 0}
        if not scored:
            reason = "unknown-candidates" if unknown else "no-context-evidence"
            return ImitationRanking(
                (), {}, known, unknown, "none", True, reason,
                candidate_set_kind=candidate_set_kind,
            )
        order = {value: index for index, value in enumerate(normalised)}
        ranked = tuple(sorted(scored, key=lambda value: (-scored[value], order[value])))
        return ImitationRanking(
            ranked,
            scored,
            known,
            unknown,
            source,
            bool(unknown),
            "partial-unknown-candidates" if unknown else None,
            candidate_set_kind=candidate_set_kind,
        )

    def rank_behavior_with_evidence(
        self,
        features: Mapping[str, object],
        settled_hand_ids: Sequence[object],
    ) -> ImitationRanking:
        """Rank settled-hand behavior alternatives for offline evaluation.

        This method intentionally exposes ``settled_hand`` in its evidence;
        callers must not treat the result as a legal action set.
        """

        return self._rank_candidates(
            features,
            settled_hand_ids,
            candidate_set_kind="settled_hand",
        )

    def rank_behavior(
        self,
        features: Mapping[str, object],
        settled_hand_ids: Sequence[object],
    ) -> tuple[str, ...]:
        """Return behavior ordering over a settled hand (offline only)."""

        return self.rank_behavior_with_evidence(features, settled_hand_ids).ranked

    def rank(
        self,
        features: Mapping[str, object],
        legal_card_ids: Sequence[object],
        *,
        candidate_set_kind: CandidateSetKind = "legal",
    ) -> tuple[str, ...]:
        """Rank only observed native-legal IDs; never infer legality."""

        return self.rank_with_evidence(
            features,
            legal_card_ids,
            candidate_set_kind=candidate_set_kind,
        ).ranked

    def rank_with_source(
        self,
        features: Mapping[str, object],
        legal_card_ids: Sequence[object],
        *,
        candidate_set_kind: CandidateSetKind = "legal",
    ) -> tuple[tuple[str, ...], str | None]:
        evidence = self.rank_with_evidence(
            features,
            legal_card_ids,
            candidate_set_kind=candidate_set_kind,
        )
        return evidence.ranked, None if evidence.source == "none" else evidence.source

    def applies_to(
        self,
        *,
        produce_id: str,
        plan_type: str,
        exam_effect_type: str,
    ) -> bool:
        flow = _flow_key(produce_id, plan_type, exam_effect_type)
        return flow in self.known_cards_by_flow

    def summary(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "behavior_only": True,
            "rl_transition_model": False,
            "candidate_contract": {
                "training": "settled_hand",
                "runtime": "legal",
                "legality_provider": "native_planner_only",
            },
            "source_path": None if self.source_path is None else str(self.source_path),
            "source_sha256": self.source_sha256,
            "observation_count": self.observation_count,
            "source_count": self.source_count,
            "flow_count": len(self.known_cards_by_flow),
            "card_count": len({card for values in self.known_cards_by_flow.values() for card in values}),
            "action_key_count": len({
                key
                for values in self.known_action_keys_by_flow.values()
                for key in values
            }),
            "signature_action_key_count": sum(
                key.count(INSTANCE_ACTION_KEY_SEPARATOR) > 0
                for values in self.known_action_keys_by_flow.values()
                for key in values
            ),
            "occurrence_action_key_count": sum(
                key.count(OCCURRENCE_ACTION_KEY_SEPARATOR) > 0
                for values in self.known_action_keys_by_flow.values()
                for key in values
            ),
            "history_size": self.history_size,
            "flows": [flow_string(flow) for flow in sorted(self.known_cards_by_flow)],
            "abstaining_reason": self.abstaining_reason,
            "projection": None if self.projection_report is None else self.projection_report.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class LeaderboardCardImitationEvaluation:
    """Held-out behavior-only metrics, never RL transition metrics."""

    observation_count: int
    scored_count: int
    top1_correct: int
    coverage: float
    top1_accuracy: float
    top1_over_all: float
    by_flow: Mapping[str, Mapping[str, float | int]] = field(default_factory=dict)
    behavior_only: bool = True
    candidate_set_kind: str = "settled_hand"

    def to_dict(self) -> dict[str, object]:
        return {
            "observation_count": self.observation_count,
            "scored_count": self.scored_count,
            "top1_correct": self.top1_correct,
            "coverage": self.coverage,
            "top1_accuracy": self.top1_accuracy,
            "top1_over_all": self.top1_over_all,
            "by_flow": {key: dict(value) for key, value in self.by_flow.items()},
            "behavior_only": self.behavior_only,
            "rl_transition_model": False,
            "candidate_set_kind": self.candidate_set_kind,
        }


def evaluate_leaderboard_card_imitation(
    observations: Sequence[LeaderboardCardImitationObservation],
    *,
    history_size: int = DEFAULT_HISTORY_SIZE,
) -> LeaderboardCardImitationEvaluation:
    """Leave one trajectory out and score observed behavior alternatives.

    New observations use ``settled_hand`` candidates.  Legacy explicitly
    typed ``legal`` observations are evaluated with the legal-shaped method,
    but the returned metrics remain behavior-only and do not imply a
    transition or reward model.
    """

    values = tuple(observations)
    if not values:
        return LeaderboardCardImitationEvaluation(0, 0, 0, 0.0, 0.0, 0.0, {})
    by_source: defaultdict[str, list[LeaderboardCardImitationObservation]] = defaultdict(list)
    for value in values:
        by_source[value.source_id].append(value)
    scored_count = 0
    top1_correct = 0
    flow_totals: Counter[str] = Counter()
    flow_scored: Counter[str] = Counter()
    flow_correct: Counter[str] = Counter()
    for source_id, held_out in by_source.items():
        train = tuple(value for value in values if value.source_id != source_id)
        if not train:
            continue
        prior = LeaderboardCardImitationPrior.from_observations(
            train,
            history_size=history_size,
        )
        for observation in held_out:
            flow_id = observation.flow_id
            flow_totals[flow_id] += 1
            if observation.candidate_set_kind == "settled_hand":
                ranking = prior.rank_behavior_with_evidence(
                    observation.feature_dict(),
                    observation.behavior_candidate_keys,
                )
            else:
                ranking = prior.rank_with_evidence(
                    observation.feature_dict(),
                    observation.candidate_action_keys,
                )
            if not ranking.ranked:
                continue
            scored_count += 1
            flow_scored[flow_id] += 1
            if ranking.ranked[0] == observation.chosen_action_key:
                top1_correct += 1
                flow_correct[flow_id] += 1
    coverage = scored_count / len(values)
    top1_accuracy = top1_correct / scored_count if scored_count else 0.0
    by_flow = {
        flow: {
            "observation_count": flow_totals[flow],
            "scored_count": flow_scored[flow],
            "top1_correct": flow_correct[flow],
            "coverage": flow_scored[flow] / flow_totals[flow] if flow_totals[flow] else 0.0,
            "top1_accuracy": flow_correct[flow] / flow_scored[flow] if flow_scored[flow] else 0.0,
        }
        for flow in sorted(flow_totals)
    }
    return LeaderboardCardImitationEvaluation(
        observation_count=len(values),
        scored_count=scored_count,
        top1_correct=top1_correct,
        coverage=coverage,
        top1_accuracy=top1_accuracy,
        top1_over_all=top1_correct / len(values),
        by_flow=by_flow,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@lru_cache(maxsize=4)
def _load_cached(path_text: str, history_size: int) -> LeaderboardCardImitationPrior:
    path = Path(path_text)
    observations, report = build_leaderboard_card_imitation_observations(path, history_size=history_size)
    prior = LeaderboardCardImitationPrior.from_observations(
        observations,
        history_size=history_size,
        projection_report=report,
        source_path=path,
        source_sha256=_sha256(path),
    )
    return prior


def load_leaderboard_card_imitation_prior(
    source: str | Path = DEFAULT_LEADERBOARD_EPISODES,
    *,
    history_size: int = DEFAULT_HISTORY_SIZE,
) -> LeaderboardCardImitationPrior:
    """Load a behavior-only prior from canonical episode JSONL."""

    return _load_cached(str(Path(source).resolve()), history_size)


def try_load_default_leaderboard_card_imitation_prior(
    *,
    history_size: int = DEFAULT_HISTORY_SIZE,
) -> LeaderboardCardImitationPrior | None:
    try:
        return load_leaderboard_card_imitation_prior(history_size=history_size)
    except (FileNotFoundError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


__all__ = [
    "CANDIDATE_SET_KINDS",
    "CandidateSetKind",
    "INSTANCE_ACTION_KEY_SEPARATOR",
    "INSTANCE_SIGNATURE_SCHEMA",
    "OCCURRENCE_ACTION_KEY_SEPARATOR",
    "DEFAULT_LEADERBOARD_EPISODES",
    "DEFAULT_HISTORY_SIZE",
    "LeaderboardCardImitationEvaluation",
    "LeaderboardCardImitationObservation",
    "LeaderboardCardImitationPrior",
    "LeaderboardCardProjectionReport",
    "ImitationRanking",
    "InstanceGroupClassification",
    "SCHEMA",
    "STAGES",
    "build_leaderboard_card_imitation_observations",
    "candidate_action_key",
    "candidate_action_keys",
    "candidate_action_keys_aligned",
    "card_instance_signature",
    "classify_duplicate_instances",
    "classify_instance_groups",
    "evaluate_leaderboard_card_imitation",
    "flow_for_episode",
    "flow_string",
    "instance_semantic_payload",
    "instance_signature",
    "instances_semantically_equivalent",
    "semantic_instance_signature",
    "load_leaderboard_card_imitation_prior",
    "project_leaderboard_episode",
    "try_load_default_leaderboard_card_imitation_prior",
]
