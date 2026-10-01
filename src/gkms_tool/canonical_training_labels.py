"""Canonical, fail-closed training labels for the existing GKMS corpora.

The adapter is deliberately read-only with respect to every input.  It projects
heterogeneous leaderboard, outer-policy, live, native-transition, and runtime
legal evidence into ``gkms.canonical-training-label.v1`` rows and writes only a
new derived dataset.

``exact`` and ``legal.complete`` are independent.  In particular, the runtime
legal v2 artifact can prove an exact complete action set without proving an
exact state transition or reward.  Unknown or malformed legacy rows are kept
as tier-D labels instead of being discarded.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from .nia_inner_transition_dataset import match_legal_candidate_indices
from .training_artifact_io import (
    atomic_write as _atomic_write,
    canonical_json_bytes as _canonical_bytes,
    sha256_file as _sha256_file,
)


LABEL_SCHEMA: Final = "gkms.canonical-training-label.v1"
MANIFEST_SCHEMA: Final = "gkms.canonical-training-label-manifest.v1"
UNKNOWN: Final = "unknown"
PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT: Final = PROJECT_ROOT / "var" / "training_labels" / "canonical_v1"

LEADERBOARD_EPISODE_SCHEMA: Final = "gkms.leaderboard-replay-episode.v2"
OUTER_TRAJECTORY_SCHEMA: Final = "gkms.leaderboard-outer-trajectory.v1"
TRAINING_OBSERVATION_SCHEMA: Final = "gkms.nia-training-observation.v1"
INNER_TRANSITION_SCHEMA: Final = "gkms.nia-inner-transition.v1"
FRAGMENT_TRANSITION_SCHEMA: Final = "gkms.nia-inner-fragment-transition.v1"
LEGAL_DECISION_SCHEMA: Final = "gkms.nia-legal-decision.v1"
RUNTIME_RECORDER_SCHEMA: Final = "gkms.runtime-exam-recorder.shadow.v1"
RUNTIME_LEGAL_SCHEMA: Final = "gkms.runtime-replay-legal-candidate-probe.v1"
RUNTIME_LEGAL_V2_SHA256: Final = (
    "929fdecaf776f388f6c6b9131bdc4ff2d72f06984236faafc90f5cfc809482a1"
)
MODE_RANKING_SCHEMA: Final = "gkms.mode-ranking-query-result.v1"
LEADERBOARD_QUERY_SCHEMA: Final = "gkms.leaderboard-query-result.v1"

MODE_BY_PRODUCE_ID: Final = {
    "produce-004": "nia_pro",
    "produce-005": "nia_master",
}

PLAN_BY_NATIVE_VALUE: Final = {
    2: "ProducePlanType_Plan1",
    3: "ProducePlanType_Plan2",
    4: "ProducePlanType_Plan3",
}

EFFECT_BY_NATIVE_VALUE: Final = {
    2: "ProduceExamEffectType_ExamParameterBuff",
    10: "ProduceExamEffectType_ExamLessonBuff",
    31: "ProduceExamEffectType_ExamReview",
    42: "ProduceExamEffectType_ExamCardPlayAggressive",
    45: "ProduceExamEffectType_ExamConcentration",
    47: "ProduceExamEffectType_ExamFullPower",
}

ARCHETYPE_BY_EFFECT: Final = {
    "ProduceExamEffectType_ExamLessonBuff": "lesson_buff",
    "ProduceExamEffectType_ExamParameterBuff": "parameter_buff",
    "ProduceExamEffectType_ExamReview": "review",
    "ProduceExamEffectType_ExamCardPlayAggressive": "card_play_aggressive",
    "ProduceExamEffectType_ExamConcentration": "concentration",
    "ProduceExamEffectType_ExamFullPower": "full_power",
}

STAGE_BY_NATIVE_VALUE: Final = {
    16: "Mid1",
    17: "Mid2",
    18: "Final",
}

STAGE_BY_NAME: Final = {
    "ProduceStepType_AuditionMid1": "Mid1",
    "ProduceStepType_AuditionMid2": "Mid2",
    "ProduceStepType_AuditionFinal": "Final",
    "Mid1": "Mid1",
    "Mid2": "Mid2",
    "Final": "Final",
}

KNOWN_READINESS_V5_FLOWS: Final = frozenset(
    {
        "produce-004|ProducePlanType_Plan1|ProduceExamEffectType_ExamLessonBuff",
        "produce-004|ProducePlanType_Plan1|ProduceExamEffectType_ExamParameterBuff",
        "produce-004|ProducePlanType_Plan2|ProduceExamEffectType_ExamCardPlayAggressive",
        "produce-004|ProducePlanType_Plan2|ProduceExamEffectType_ExamReview",
        "produce-004|ProducePlanType_Plan3|ProduceExamEffectType_ExamConcentration",
    }
)

QUALITY_TIERS: Final = ("A", "B", "C", "D")
SOURCE_KINDS: Final = (
    "live",
    "official_replay",
    "ranking",
    "api",
    "outer",
    "native",
    UNKNOWN,
)
TRAJECTORY_SPLIT_POLICY_VERSION: Final = 1
TRAJECTORY_SPLIT_ASSIGNMENTS: Final = frozenset(
    {"train", "validation", "test"}
)


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON constant: {value}")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _list(value: object) -> list[Any] | None:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return None


def _plain_number(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _normal_key(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _collect_named_values(
    value: object,
    names: Iterable[str],
    *,
    max_depth: int = 8,
) -> list[object]:
    targets = {_normal_key(name) for name in names}
    result: list[object] = []

    def visit(node: object, depth: int) -> None:
        if depth > max_depth:
            return
        if isinstance(node, Mapping):
            for key, child in node.items():
                if isinstance(key, str) and _normal_key(key) in targets:
                    if child is not None and not isinstance(child, (Mapping, list, tuple)):
                        result.append(child)
                visit(child, depth + 1)
        elif isinstance(node, (list, tuple)):
            for child in node:
                visit(child, depth + 1)

    visit(value, 0)
    return result


def _unique_text(
    values: Iterable[object],
    *,
    normalizer: Any | None = None,
) -> tuple[str, bool, list[object]]:
    raw_values: list[object] = []
    normalized: list[str] = []
    for value in values:
        if value is None or isinstance(value, bool):
            continue
        raw_values.append(value)
        candidate = normalizer(value) if normalizer is not None else value
        if isinstance(candidate, str) and candidate:
            normalized.append(candidate)
    unique = sorted(set(normalized))
    if len(unique) == 1:
        return unique[0], False, raw_values
    return UNKNOWN, len(unique) > 1, raw_values


def _normalize_plan(value: object) -> str | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return PLAN_BY_NATIVE_VALUE.get(value)
    if not isinstance(value, str):
        return None
    if value in set(PLAN_BY_NATIVE_VALUE.values()):
        return value
    aliases = {
        "plan1": "ProducePlanType_Plan1",
        "plan2": "ProducePlanType_Plan2",
        "plan3": "ProducePlanType_Plan3",
    }
    return aliases.get(value.casefold())


def _normalize_effect(value: object) -> str | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return EFFECT_BY_NATIVE_VALUE.get(value)
    if isinstance(value, str) and value:
        return value
    return None


def _normalize_stage(value: object) -> str | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return STAGE_BY_NATIVE_VALUE.get(value)
    if isinstance(value, str):
        return STAGE_BY_NAME.get(value)
    return None


def _scope(
    payload: Mapping[str, Any],
    *,
    state_before: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
    phase: object = None,
    stage_hint: object = None,
) -> tuple[dict[str, object], list[str]]:
    roots: list[object] = [payload]
    nested_scope = payload.get("scope")
    if isinstance(nested_scope, Mapping):
        roots.insert(0, nested_scope)
    if metadata:
        roots.insert(0, metadata)
    if state_before:
        roots.append(state_before)

    ambiguous: list[str] = []

    def values(*names: str) -> list[object]:
        gathered: list[object] = []
        for root in roots:
            gathered.extend(_collect_named_values(root, names))
        return gathered

    produce_id, conflict, produce_raw = _unique_text(values("produce_id", "produceId"))
    if conflict:
        ambiguous.append("scope.produce_id")
    idol_card_id, conflict, idol_raw = _unique_text(values("idol_card_id", "idolCardId"))
    if conflict:
        ambiguous.append("scope.idol_card_id")
    character_id, conflict, character_raw = _unique_text(values("character_id", "characterId"))
    if conflict:
        ambiguous.append("scope.character_id")
    plan_type, conflict, plan_raw = _unique_text(
        values("plan_type", "planType"), normalizer=_normalize_plan
    )
    if conflict:
        ambiguous.append("scope.plan_type")
    exam_effect_type, conflict, effect_raw = _unique_text(
        values(
            "exam_effect_type",
            "main_effect_type",
            "mainEffectType",
            "displayMainEffectType",
        ),
        normalizer=_normalize_effect,
    )
    if conflict:
        ambiguous.append("scope.exam_effect_type")

    stage_values: list[object] = []
    if stage_hint is not None:
        stage_values.append(stage_hint)
    stage_values.extend(
        values("stage", "step_type", "stepType", "step_type_value", "exam_step_type")
    )
    if phase in (1, 2, 3):
        stage_values.append({1: "Mid1", 2: "Mid2", 3: "Final"}[int(phase)])
    stage, conflict, stage_raw = _unique_text(stage_values, normalizer=_normalize_stage)
    if conflict:
        ambiguous.append("scope.stage")

    mode = MODE_BY_PRODUCE_ID.get(produce_id, UNKNOWN)
    archetype = ARCHETYPE_BY_EFFECT.get(exam_effect_type, UNKNOWN)
    return (
        {
            "mode": mode,
            "produce_id": produce_id,
            "idol_card_id": idol_card_id,
            "character_id": character_id,
            "plan_type": plan_type,
            "exam_effect_type": exam_effect_type,
            "archetype": archetype,
            "stage": stage,
            "raw": {
                "produce_id": produce_raw,
                "idol_card_id": idol_raw,
                "character_id": character_raw,
                "plan_type": plan_raw,
                "exam_effect_type": effect_raw,
                "stage": stage_raw,
            },
        },
        ambiguous,
    )


def _state_snapshot_id(state_before: object) -> str | None:
    if not isinstance(state_before, Mapping):
        return None
    return f"snapshot:{_digest(state_before)}"


def _player_id(payload: Mapping[str, Any]) -> tuple[str | None, bool]:
    values = _collect_named_values(
        payload,
        ("player_id", "playerId", "user_id", "userId", "producer_id", "producerId"),
        max_depth=5,
    )
    normalized = sorted({str(value) for value in values if str(value)})
    if len(normalized) != 1:
        return None, len(normalized) > 1
    return f"player:{hashlib.sha256(normalized[0].encode('utf-8')).hexdigest()}", False


def _trajectory_id(payload: Mapping[str, Any], fallback: object = None) -> str | None:
    for value in (
        payload.get("trajectory_id"),
        payload.get("replay_scope"),
        fallback,
    ):
        if isinstance(value, str) and value:
            return value
    return None


def _stage_for_outer_number(produce_id: object, number: object) -> str:
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        return UNKNOWN
    if produce_id == "produce-004":
        return "Mid1" if number <= 9 else "Mid2" if number <= 18 else "Final"
    if produce_id == "produce-005":
        return "Mid1" if number <= 9 else "Mid2" if number <= 17 else "Final"
    return UNKNOWN


def _source(
    *,
    primary: str,
    tags: Iterable[str],
    provenance: Iterable[object] = (),
) -> dict[str, object]:
    tag_set = {tag for tag in tags if tag in SOURCE_KINDS and tag != UNKNOWN}
    provenance_values = sorted(
        {value for value in provenance if isinstance(value, str) and value}
    )
    for value in provenance_values:
        folded = value.casefold()
        if "ranking" in folded:
            tag_set.update({"ranking", "api", "official_replay"})
        if "history" in folded or "replay" in folded:
            tag_set.update({"api", "official_replay"})
        if "live" in folded:
            tag_set.add("live")
    if primary in SOURCE_KINDS and primary != UNKNOWN:
        tag_set.add(primary)
    else:
        primary = UNKNOWN
    return {
        "primary": primary,
        "tags": sorted(tag_set),
        "provenance": provenance_values,
    }


def _assets_from_episode(payload: Mapping[str, Any]) -> dict[str, object]:
    loadout_known = any(
        key in payload for key in ("memory_loadout", "memory_abilities", "support_cards")
    )
    return {
        "loadout": (
            {
                "memory_loadout": payload.get("memory_loadout", []),
                "memory_abilities": payload.get("memory_abilities", []),
                "support_cards": payload.get("support_cards", []),
            }
            if loadout_known
            else None
        ),
        "deck": payload.get("produce_cards") if "produce_cards" in payload else None,
        "drink": (
            payload.get("produce_drink_ids") if "produce_drink_ids" in payload else None
        ),
        "pitem": (
            {
                "produce_items": payload.get("produce_items", []),
                "produce_customize_item_ids": payload.get(
                    "produce_customize_item_ids", []
                ),
            }
            if "produce_items" in payload or "produce_customize_item_ids" in payload
            else None
        ),
    }


def _assets_from_state(state: object) -> dict[str, object]:
    if not isinstance(state, Mapping):
        return {"loadout": None, "deck": None, "drink": None, "pitem": None}
    zones = _mapping(state.get("zones"))
    root_runtime = _mapping(state.get("root_runtime"))
    opaque = _mapping(root_runtime.get("opaque_fields"))
    deck: object = None
    if "deck" in zones:
        deck = zones.get("deck")
    elif "future_deck" in state or "past_deck" in state:
        deck = {
            "future": state.get("future_deck"),
            "past": state.get("past_deck"),
            "hand": zones.get("hand") if "hand" in zones else None,
        }
    drink = opaque.get("drinkList") if "drinkList" in opaque else None
    pitem = opaque.get("itemList") if "itemList" in opaque else None
    loadout = (
        opaque.get("supportCardList") if "supportCardList" in opaque else None
    )
    return {"loadout": loadout, "deck": deck, "drink": drink, "pitem": pitem}


def _legal(
    candidates: object,
    *,
    complete: bool,
    exact: bool,
    candidate_scope: str,
    chosen_member: bool | None,
    scope_complete: bool | None = None,
) -> dict[str, object]:
    values = _list(candidates)
    return {
        "candidates": values,
        "complete": complete is True,
        "scope_complete": complete if scope_complete is None else scope_complete,
        "exact": exact is True,
        "candidate_scope": candidate_scope,
        "chosen_member": chosen_member,
    }


def _reward(
    *,
    score: object = None,
    reward: object = None,
    terminal: object = None,
    return_value: object = None,
) -> dict[str, object]:
    return {
        "score": _plain_number(score),
        "reward": _plain_number(reward),
        "terminal": terminal if type(terminal) is bool else None,
        "return": _plain_number(return_value),
    }


def _quality(
    *,
    force_tier: str | None,
    action_known: bool,
    action_exact: bool,
    transition_exact: bool,
    legal: Mapping[str, Any],
    reward: Mapping[str, Any],
    full_rl_policy_ready: bool,
    behavior_only: bool,
    fragment_incomplete: bool,
    parse_error: str | None,
) -> dict[str, object]:
    reasons: list[str] = []
    if force_tier is not None:
        tier = force_tier
    elif parse_error is not None or not action_known:
        tier = "D"
    elif (
        transition_exact
        and legal.get("complete") is True
        and legal.get("exact") is True
        and reward.get("reward") is not None
        and reward.get("terminal") is not None
        and full_rl_policy_ready
        and not fragment_incomplete
    ):
        tier = "A"
    elif transition_exact or (
        action_exact
        and legal.get("complete") is True
        and legal.get("exact") is True
    ):
        tier = "B"
    else:
        tier = "C"

    if parse_error is not None:
        reasons.append(parse_error)
    if not action_known:
        reasons.append("action-unknown")
    if transition_exact:
        reasons.append("state-action-next-state-exact")
    else:
        reasons.append("transition-not-exact")
    if legal.get("exact") is True:
        reasons.append("legal-set-exact")
    else:
        reasons.append("legal-set-not-exact")
    if legal.get("complete") is True:
        reasons.append("legal-set-complete")
    else:
        reasons.append("legal-set-not-unified-complete")
    if full_rl_policy_ready:
        reasons.append("full-rl-policy-ready")
    if behavior_only:
        reasons.append("behavior-only")
    if fragment_incomplete:
        reasons.append("fragment-trajectory-incomplete")
    return {"tier": tier, "reasons": sorted(set(reasons))}


def _unknown_fields(label: Mapping[str, Any]) -> list[str]:
    result: list[str] = []
    scope = _mapping(label.get("scope"))
    for field in (
        "mode",
        "produce_id",
        "idol_card_id",
        "character_id",
        "plan_type",
        "exam_effect_type",
        "archetype",
        "stage",
    ):
        if scope.get(field) in (None, UNKNOWN, ""):
            result.append(f"scope.{field}")
    source = _mapping(label.get("source"))
    if source.get("primary") in (None, UNKNOWN, ""):
        result.append("source.primary")
    identity = _mapping(label.get("identity"))
    for field in ("trajectory_id", "player_id", "snapshot_id"):
        if identity.get(field) in (None, UNKNOWN, ""):
            result.append(f"identity.{field}")
    transition = _mapping(label.get("transition"))
    for field in ("state_before", "action", "state_after"):
        if transition.get(field) is None:
            result.append(f"transition.{field}")
    legal = _mapping(label.get("legal"))
    if legal.get("candidates") is None:
        result.append("legal.candidates")
    reward = _mapping(label.get("reward"))
    for field in ("score", "reward", "terminal"):
        if reward.get(field) is None:
            result.append(f"reward.{field}")
    assets = _mapping(label.get("assets"))
    for field in ("loadout", "deck", "drink", "pitem"):
        if assets.get(field) is None:
            result.append(f"assets.{field}")
    return sorted(result)


def _compatibility(
    *,
    schema: str,
    path: Path,
    scope: Mapping[str, Any],
    runtime_legal_v2: bool,
) -> list[str]:
    result: set[str] = set()
    flow = "|".join(
        str(scope.get(key, UNKNOWN))
        for key in ("produce_id", "plan_type", "exam_effect_type")
    )
    if flow in KNOWN_READINESS_V5_FLOWS:
        result.add("readiness-v5-flow")
    if schema == LEADERBOARD_EPISODE_SCHEMA:
        result.add("leaderboard-v6-episode-v2")
        if "v6" in str(path).casefold():
            result.add("leaderboard-v6-corpus")
        if "frozen" in str(path).casefold():
            result.add("leaderboard-frozen-union-v1")
    if runtime_legal_v2:
        result.add("runtime-legal-v2")
    return sorted(result)


def _base_label(
    *,
    source_ref: Mapping[str, Any],
    source_schema: str,
    granularity: str,
    scope: dict[str, object],
    source: dict[str, object],
    trajectory_id: str | None,
    player_id: str | None,
    snapshot_id: str | None,
    step: object,
    state_before: object,
    action: object,
    state_after: object,
    legal: dict[str, object],
    assets: dict[str, object],
    reward: dict[str, object],
    exact: dict[str, bool],
    join_status: str,
    join_reasons: Iterable[str],
    behavior_only: bool,
    full_rl_policy_ready: bool,
    fragment_incomplete: bool = False,
    parse_error: str | None = None,
    ambiguous_fields: Iterable[str] = (),
    force_tier: str | None = None,
    runtime_legal_v2: bool = False,
) -> dict[str, object]:
    label: dict[str, object] = {
        "schema": LABEL_SCHEMA,
        "source_record": dict(source_ref),
        "source_schema": source_schema or UNKNOWN,
        "granularity": granularity,
        "scope": scope,
        "source": source,
        "identity": {
            "trajectory_id": trajectory_id,
            "player_id": player_id,
            "snapshot_id": snapshot_id,
            "step": step,
        },
        "transition": {
            "state_before": state_before,
            "action": action,
            "state_after": state_after,
        },
        "legal": legal,
        "assets": assets,
        "reward": reward,
        "exact": exact,
        "join": {
            "status": join_status,
            "reasons": sorted(set(join_reasons)),
        },
        "behavior_only": behavior_only,
        "full_rl_policy_ready": full_rl_policy_ready,
        "ambiguous_fields": sorted(set(ambiguous_fields)),
    }
    label["quality"] = _quality(
        force_tier=force_tier,
        action_known=action is not None,
        action_exact=exact.get("action") is True,
        transition_exact=exact.get("transition") is True,
        legal=legal,
        reward=reward,
        full_rl_policy_ready=full_rl_policy_ready,
        behavior_only=behavior_only,
        fragment_incomplete=fragment_incomplete,
        parse_error=parse_error,
    )
    path_value = source_ref.get("path")
    compatibility_path = Path(str(path_value)) if path_value else Path(UNKNOWN)
    label["compatibility"] = _compatibility(
        schema=source_schema,
        path=compatibility_path,
        scope=scope,
        runtime_legal_v2=runtime_legal_v2,
    )
    label["unknown_fields"] = _unknown_fields(label)
    return label


def _source_ref(
    *,
    path: Path,
    file_sha256: str,
    record_sha256: str,
    position: int,
    sub_index: int | None = None,
) -> dict[str, object]:
    try:
        display_path = path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        display_path = str(path.resolve())
    value: dict[str, object] = {
        "path": display_path,
        "file_sha256": file_sha256,
        "record_sha256": record_sha256,
        "position": position,
    }
    if sub_index is not None:
        value["sub_index"] = sub_index
    value["record_id"] = f"record:{_digest(value)}"
    return value


def _episode_labels(
    payload: Mapping[str, Any],
    *,
    ref: Mapping[str, Any],
    path: Path,
) -> list[dict[str, object]]:
    scope, ambiguous = _scope(payload, stage_hint=payload.get("step_type", payload.get("stage")))
    player, player_ambiguous = _player_id(payload)
    if player_ambiguous:
        ambiguous.append("identity.player_id")
    trajectory = _trajectory_id(payload)
    actions = _list(payload.get("actions"))
    action = (
        {"kind": "official_replay_action_sequence", "actions": actions}
        if actions
        else None
    )
    provenance = _list(payload.get("sources")) or []
    source = _source(
        primary="official_replay",
        tags=("official_replay",),
        provenance=provenance,
    )
    score = payload.get("terminal_score")
    reward = _reward(score=score, terminal=True)
    legal = _legal(
        None,
        complete=False,
        exact=False,
        candidate_scope=UNKNOWN,
        chosen_member=None,
    )
    telemetry_context = payload.get("telemetry_replay_context")
    joined = isinstance(telemetry_context, Mapping) and bool(payload.get("episode_id"))
    label = _base_label(
            source_ref=ref,
            source_schema=LEADERBOARD_EPISODE_SCHEMA,
            granularity="trajectory",
            scope=scope,
            source=source,
            trajectory_id=trajectory,
            player_id=player,
            snapshot_id=None,
            step=payload.get("audition_index"),
            state_before=None,
            action=action,
            state_after=None,
            legal=legal,
            assets=_assets_from_episode(payload),
            reward=reward,
            exact={
                "state_before": False,
                "action": action is not None,
                "state_after": False,
                "reward": False,
                "score": reward["score"] is not None,
                "terminal": True,
                "transition": False,
            },
            join_status="unique" if joined else "not_applicable",
            join_reasons=(
                ("embedded-telemetry-replay-context",)
                if joined
                else ("episode-is-already-canonical",)
            ),
            behavior_only=True,
            full_rl_policy_ready=False,
            ambiguous_fields=ambiguous,
        )
    identity = _mapping(label.get("identity"))
    identity_value = dict(identity)
    audition_index = payload.get("audition_index")
    identity_value.update(
        {
            "episode_id": (
                payload.get("episode_id")
                if isinstance(payload.get("episode_id"), str)
                else (
                    f"{trajectory}:audition:{audition_index}"
                    if trajectory is not None
                    and isinstance(audition_index, int)
                    and not isinstance(audition_index, bool)
                    else None
                )
            ),
            "audition_index": audition_index,
            "stage_index": payload.get("stage_index"),
            "section_index": payload.get("section_index"),
        }
    )
    label["identity"] = identity_value
    if not provenance:
        quality = dict(_mapping(label.get("quality")))
        quality["reasons"] = sorted(
            set(list(quality.get("reasons", [])) + ["source-provenance-missing"])
        )
        label["quality"] = quality
    return [label]


def _observation_labels(
    payload: Mapping[str, Any],
    *,
    ref: Mapping[str, Any],
    path: Path,
) -> list[dict[str, object]]:
    phase = payload.get("phase")
    scope, ambiguous = _scope(payload, state_before=_mapping(payload.get("state_before")), phase=phase)
    player, player_ambiguous = _player_id(payload)
    if player_ambiguous:
        ambiguous.append("identity.player_id")
    source_name = payload.get("source")
    if source_name == "accepted_live":
        source = _source(primary="live", tags=("live", "outer"))
    else:
        source = _source(
            primary="outer",
            tags=("outer", "official_replay"),
            provenance=_list(payload.get("provenance", payload.get("sources"))) or (),
        )
    candidate_ids = _list(payload.get("candidate_ids"))
    chosen_id = payload.get("chosen_id")
    chosen_known = isinstance(chosen_id, str) and bool(chosen_id)
    chosen_member = chosen_id in candidate_ids if candidate_ids is not None and chosen_known else None
    legal = _legal(
        candidate_ids,
        complete=candidate_ids is not None and chosen_member is True,
        exact=candidate_ids is not None and chosen_member is True,
        candidate_scope="outer",
        chosen_member=chosen_member,
    )
    before = payload.get("state_before") if isinstance(payload.get("state_before"), Mapping) else None
    after = payload.get("state_after") if isinstance(payload.get("state_after"), Mapping) else None
    action = (
        {"kind": str(payload.get("decision_kind", "outer_action")), "id": chosen_id}
        if chosen_known
        else None
    )
    reward = _reward(score=payload.get("terminal_value"))
    trajectory = _trajectory_id(payload, payload.get("source_id"))
    return [
        _base_label(
            source_ref=ref,
            source_schema=TRAINING_OBSERVATION_SCHEMA,
            granularity="decision",
            scope=scope,
            source=source,
            trajectory_id=trajectory,
            player_id=player,
            snapshot_id=_state_snapshot_id(before),
            step=payload.get("week"),
            state_before=before,
            action=action,
            state_after=after,
            legal=legal,
            assets=_assets_from_state(before),
            reward=reward,
            exact={
                "state_before": False,
                "action": chosen_known and chosen_member is True,
                "state_after": False,
                "reward": False,
                "score": reward["score"] is not None,
                "terminal": False,
                "transition": False,
            },
            join_status="not_applicable",
            join_reasons=("outer-decision-contract",),
            behavior_only=True,
            full_rl_policy_ready=False,
            ambiguous_fields=ambiguous,
        )
    ]


def _outer_labels(
    payload: Mapping[str, Any],
    *,
    ref: Mapping[str, Any],
    path: Path,
) -> list[dict[str, object]]:
    choices = _list(payload.get("choices"))
    if choices is None:
        return _generic_labels(payload, ref=ref, path=path, force_reason="outer-choices-invalid")
    player, player_ambiguous = _player_id(payload)
    trajectory = _trajectory_id(payload)
    labels: list[dict[str, object]] = []
    for index, raw_choice in enumerate(choices):
        choice = _mapping(raw_choice)
        choice_ref = dict(ref)
        choice_ref["sub_index"] = index
        choice_ref["record_id"] = f"record:{_digest(choice_ref)}"
        number = choice.get("number")
        stage = _stage_for_outer_number(payload.get("produce_id"), number)
        scope, ambiguous = _scope(payload, stage_hint=stage)
        if player_ambiguous:
            ambiguous.append("identity.player_id")
        candidates = _list(choice.get("candidates"))
        candidate_ids = None
        if candidates is not None:
            candidate_ids = [
                item.get("action") if isinstance(item, Mapping) else item
                for item in candidates
            ]
        chosen = choice.get("chosen_action")
        chosen_member = (
            chosen in candidate_ids
            if candidate_ids is not None and isinstance(chosen, str)
            else None
        )
        complete = choice.get("candidate_set_complete") is True and chosen_member is True
        legal = _legal(
            candidate_ids,
            complete=complete,
            exact=complete,
            candidate_scope="outer",
            chosen_member=chosen_member,
        )
        action = (
            {
                "kind": "outer_action",
                "id": chosen,
                "step_type": choice.get("chosen_step_type"),
                "is_sp": choice.get("chosen_is_sp"),
            }
            if isinstance(chosen, str) and chosen
            else None
        )
        reward = _reward(score=payload.get("history_score"))
        labels.append(
            _base_label(
                source_ref=choice_ref,
                source_schema=OUTER_TRAJECTORY_SCHEMA,
                granularity="decision",
                scope=scope,
                source=_source(
                    primary="outer",
                    tags=("outer", "official_replay"),
                    provenance=_list(payload.get("sources")) or (),
                ),
                trajectory_id=trajectory,
                player_id=player,
                snapshot_id=None,
                step=number,
                state_before=None,
                action=action,
                state_after=None,
                legal=legal,
                assets={"loadout": None, "deck": None, "drink": None, "pitem": None},
                reward=reward,
                exact={
                    "state_before": False,
                    "action": chosen_member is True,
                    "state_after": False,
                    "reward": False,
                    "score": reward["score"] is not None,
                    "terminal": False,
                    "transition": False,
                },
                join_status="not_applicable",
                join_reasons=("outer-trajectory-choice",),
                behavior_only=True,
                full_rl_policy_ready=False,
                ambiguous_fields=ambiguous,
            )
        )
    return labels


def _transition_labels(
    payload: Mapping[str, Any],
    *,
    ref: Mapping[str, Any],
    path: Path,
    fragment: bool,
) -> list[dict[str, object]]:
    before = payload.get("state_before") if isinstance(payload.get("state_before"), Mapping) else None
    after = payload.get("state_after") if isinstance(payload.get("state_after"), Mapping) else None
    metadata = _mapping(payload.get("metadata"))
    scope, ambiguous = _scope(payload, state_before=_mapping(before), metadata=metadata)
    player, player_ambiguous = _player_id(payload)
    if player_ambiguous:
        ambiguous.append("identity.player_id")
    trajectory = _trajectory_id(payload, payload.get("source_id"))
    candidate_scope = payload.get("candidate_set_kind", metadata.get("candidate_set_kind"))
    if not isinstance(candidate_scope, str) or not candidate_scope:
        candidate_scope = UNKNOWN
    candidates = _list(payload.get("legal_candidates"))
    action = payload.get("action")
    chosen_matches: tuple[int, ...] | None = None
    if candidates is not None and action is not None:
        selected_candidate_index = metadata.get("selected_candidate_index")
        try:
            chosen_matches = match_legal_candidate_indices(
                action,
                candidates,
                selected_candidate_index=(
                    selected_candidate_index
                    if isinstance(selected_candidate_index, int)
                    and not isinstance(selected_candidate_index, bool)
                    else None
                ),
            )
        except ValueError:
            chosen_matches = None
    chosen_member = len(chosen_matches) == 1 if chosen_matches is not None else None
    if chosen_matches is not None and len(chosen_matches) > 1:
        ambiguous.append("legal.chosen_action")
    scope_complete = candidates is not None and chosen_member is True
    unified_complete = scope_complete and candidate_scope == "unified"
    legal = _legal(
        candidates,
        complete=unified_complete,
        scope_complete=scope_complete,
        exact=scope_complete,
        candidate_scope=candidate_scope,
        chosen_member=chosen_member,
    )
    reward = _reward(
        reward=payload.get("reward"),
        terminal=payload.get("terminal"),
        return_value=payload.get("return"),
    )
    local_exact = before is not None and after is not None and action is not None and scope_complete
    full_rl_policy_ready = (
        not fragment
        and metadata.get("full_rl_policy_ready", payload.get("full_rl_policy_ready")) is True
        and unified_complete
    )
    source_name = payload.get("source")
    runtime_source_mode = metadata.get("runtime_source_mode")
    top_level_is_replay = before.get("isReplay") if isinstance(before, Mapping) else None
    native_runtime = (
        metadata.get("state_authority") == "native-ExamSaveData-runtime"
        and metadata.get("simulator_used") is False
        and runtime_source_mode in {"live", "replay"}
        and type(top_level_is_replay) is bool
        and top_level_is_replay == (runtime_source_mode == "replay")
    )
    source = _source(
        primary=(
            "official_replay"
            if native_runtime and runtime_source_mode == "replay"
            else "live"
            if native_runtime
            else UNKNOWN
        ),
        tags=(
            ("official_replay", "native")
            if native_runtime and runtime_source_mode == "replay"
            else ("live", "native")
            if native_runtime
            else ()
        ),
        provenance=(),
    )
    label = _base_label(
        source_ref=ref,
        source_schema=(FRAGMENT_TRANSITION_SCHEMA if fragment else INNER_TRANSITION_SCHEMA),
        granularity="transition",
        scope=scope,
        source=source,
        trajectory_id=trajectory,
        player_id=player,
        snapshot_id=_state_snapshot_id(before),
        step=payload.get("step"),
        state_before=before,
        action=action,
        state_after=after,
        legal=legal,
        assets=_assets_from_state(before),
        reward=reward,
        exact={
            "state_before": before is not None,
            "action": action is not None and chosen_member is True,
            "state_after": after is not None,
            "reward": reward["reward"] is not None,
            "score": False,
            "terminal": reward["terminal"] is not None,
            "transition": local_exact,
        },
        join_status="not_applicable",
        join_reasons=(
            "fragment-local-boundary" if fragment else "native-transition-boundary",
        ),
        behavior_only=False,
        full_rl_policy_ready=full_rl_policy_ready,
        fragment_incomplete=fragment,
        ambiguous_fields=ambiguous,
    )
    capture_source_id = metadata.get("capture_source_id")
    if isinstance(capture_source_id, str) and capture_source_id:
        label["capture_source_id"] = capture_source_id
    policy_surface = metadata.get("policy_surface")
    if isinstance(policy_surface, str) and policy_surface:
        label["policy_surface"] = policy_surface
    if isinstance(source_name, str) and source_name:
        label[
            "native_source_provenance"
            if native_runtime
            else "untrusted_source_provenance"
        ] = source_name
    return [label]


def _legal_decision_labels(
    payload: Mapping[str, Any],
    *,
    ref: Mapping[str, Any],
    path: Path,
) -> list[dict[str, object]]:
    before = payload.get("state_before") if isinstance(payload.get("state_before"), Mapping) else None
    metadata = _mapping(payload.get("metadata"))
    scope, ambiguous = _scope(payload, state_before=_mapping(before), metadata=metadata)
    player, player_ambiguous = _player_id(payload)
    if player_ambiguous:
        ambiguous.append("identity.player_id")
    candidates = _list(payload.get("legal_candidates"))
    action = payload.get("action")
    chosen_member = action in candidates if candidates is not None and action is not None else None
    complete = metadata.get("candidate_complete") is True and chosen_member is True
    legal = _legal(
        candidates,
        complete=complete,
        exact=complete,
        candidate_scope="unified" if complete else UNKNOWN,
        chosen_member=chosen_member,
    )
    return [
        _base_label(
            source_ref=ref,
            source_schema=LEGAL_DECISION_SCHEMA,
            granularity="snapshot",
            scope=scope,
            source=_source(primary="live", tags=("live", "native")),
            trajectory_id=_trajectory_id(payload, payload.get("source_id")),
            player_id=player,
            snapshot_id=(
                f"snapshot:{payload['boundary_digest']}"
                if isinstance(payload.get("boundary_digest"), str)
                else _state_snapshot_id(before)
            ),
            step=payload.get("step"),
            state_before=before,
            action=action,
            state_after=None,
            legal=legal,
            assets=_assets_from_state(before),
            reward=_reward(),
            exact={
                "state_before": before is not None,
                "action": chosen_member is True,
                "state_after": False,
                "reward": False,
                "score": False,
                "terminal": False,
                "transition": False,
            },
            join_status="not_applicable",
            join_reasons=("native-contextual-bandit",),
            behavior_only=True,
            full_rl_policy_ready=False,
            ambiguous_fields=ambiguous,
        )
    ]


def _runtime_legal_report_labels(
    payload: Mapping[str, Any],
    *,
    ref: Mapping[str, Any],
    path: Path,
) -> list[dict[str, object]]:
    probe = _mapping(payload.get("runtime_legal_action_probe"))
    steps = _list(probe.get("steps"))
    if steps is None:
        return _generic_labels(payload, ref=ref, path=path, force_reason="runtime-legal-steps-invalid")
    stage_identity = dict(_mapping(_mapping(payload.get("stage")).get("identity")))
    if ref.get("file_sha256") == RUNTIME_LEGAL_V2_SHA256:
        # readiness v5 binds this exact immutable artifact hash to the one
        # CardPlayAggressive flow.  No filename-only inference is allowed.
        stage_identity.update(
            {
                "planType": "ProducePlanType_Plan2",
                "mainEffectType": "ProduceExamEffectType_ExamCardPlayAggressive",
            }
        )
    labels: list[dict[str, object]] = []
    report_exact = (
        payload.get("exact") is True
        and payload.get("legal_actions_complete") is True
        and payload.get("passed") is True
    )
    for index, raw_step in enumerate(steps):
        step = _mapping(raw_step)
        step_ref = dict(ref)
        step_ref["sub_index"] = index
        step_ref["record_id"] = f"record:{_digest(step_ref)}"
        scope, ambiguous = _scope(stage_identity, stage_hint=stage_identity.get("stepType"))
        membership = _mapping(step.get("official_chosen_membership"))
        action = membership.get("raw") if isinstance(membership.get("raw"), Mapping) else None
        candidates = _list(step.get("authoritative_legal_actions"))
        step_verified = step.get("step_verified") is True and membership.get("member") is True
        complete = report_exact and candidates is not None and step_verified
        purity = _mapping(step.get("purity"))
        snapshot_evidence = purity.get("before")
        snapshot_id = (
            f"snapshot:{_digest(snapshot_evidence)}"
            if isinstance(snapshot_evidence, Mapping)
            else None
        )
        legal = _legal(
            candidates,
            complete=complete,
            exact=complete,
            candidate_scope="unified",
            chosen_member=membership.get("member") if type(membership.get("member")) is bool else None,
        )
        terminal = step.get("terminal")
        labels.append(
            _base_label(
                source_ref=step_ref,
                source_schema=RUNTIME_LEGAL_SCHEMA,
                granularity="snapshot",
                scope=scope,
                source=_source(
                    primary="official_replay",
                    tags=("official_replay", "native"),
                ),
                trajectory_id=None,
                player_id=None,
                snapshot_id=snapshot_id,
                step=step.get("action_order"),
                state_before=None,
                action=action,
                state_after=None,
                legal=legal,
                assets={"loadout": None, "deck": None, "drink": None, "pitem": None},
                reward=_reward(terminal=terminal),
                exact={
                    "state_before": False,
                    "action": complete,
                    "state_after": False,
                    "reward": False,
                    "score": False,
                    "terminal": type(terminal) is bool,
                    "transition": False,
                },
                join_status="unresolved",
                join_reasons=("runtime-replay-trajectory-id-unavailable",),
                behavior_only=True,
                full_rl_policy_ready=False,
                ambiguous_fields=ambiguous,
                runtime_legal_v2=True,
            )
        )
    return labels


def _runtime_recorder_labels(
    payload: Mapping[str, Any],
    *,
    ref: Mapping[str, Any],
    path: Path,
) -> list[dict[str, object]]:
    body = _mapping(payload.get("body"))
    if body.get("record") != "transition":
        return _generic_labels(
            payload,
            ref=ref,
            path=path,
            force_reason=f"runtime-diagnostic:{body.get('record', UNKNOWN)}",
        )
    before = body.get("state_before") if isinstance(body.get("state_before"), Mapping) else None
    after = body.get("state_after") if isinstance(body.get("state_after"), Mapping) else None
    scope, ambiguous = _scope(payload, state_before=_mapping(before))
    probe = _mapping(body.get("runtime_legal_action_probe"))
    if not probe:
        probe = _mapping(_mapping(body.get("legal_action_candidates")).get("runtime_legal_action_probe"))
    candidates = _list(probe.get("authoritative_legal_actions"))
    membership = _mapping(body.get("official_chosen_membership"))
    action = body.get("action") if isinstance(body.get("action"), Mapping) else None
    legal_exact = probe.get("exact") is True
    complete = legal_exact and probe.get("legal_actions_complete") is True and candidates is not None
    chosen_member = membership.get("member") if type(membership.get("member")) is bool else None
    legal = _legal(
        candidates,
        complete=complete,
        exact=legal_exact,
        candidate_scope="unified" if probe else UNKNOWN,
        chosen_member=chosen_member,
    )
    before_exact = (
        payload.get("exact") is True
        and body.get("state_before_captured") is True
        and body.get("state_before_complete") is True
        and before is not None
    )
    after_exact = (
        payload.get("exact") is True
        and body.get("state_after_captured") is True
        and body.get("state_after_complete") is True
        and after is not None
    )
    action_exact = action is not None and chosen_member is True
    is_replay_values = _collect_named_values(before or {}, ("isReplay", "is_replay"))
    is_replay = True in is_replay_values
    return [
        _base_label(
            source_ref=ref,
            source_schema=RUNTIME_RECORDER_SCHEMA,
            granularity="transition",
            scope=scope,
            source=_source(
                primary="official_replay" if is_replay else "live",
                tags=(("official_replay", "native") if is_replay else ("live", "native")),
            ),
            trajectory_id=None,
            player_id=None,
            snapshot_id=_state_snapshot_id(before),
            step=body.get("action_order"),
            state_before=before,
            action=action,
            state_after=after,
            legal=legal,
            assets=_assets_from_state(before),
            reward=_reward(
                terminal=(body.get("terminal") if body.get("terminal_known") is True else None)
            ),
            exact={
                "state_before": before_exact,
                "action": action_exact,
                "state_after": after_exact,
                "reward": False,
                "score": False,
                "terminal": body.get("terminal_known") is True,
                "transition": (
                    before_exact
                    and action_exact
                    and after_exact
                    and body.get("action_state_exact") is True
                ),
            },
            join_status="unresolved" if is_replay else "not_applicable",
            join_reasons=(
                ("runtime-replay-trajectory-id-unavailable",)
                if is_replay
                else ("live-native-boundary",)
            ),
            behavior_only=True,
            full_rl_policy_ready=False,
            ambiguous_fields=ambiguous,
            runtime_legal_v2=legal_exact and complete,
        )
    ]


def _generic_labels(
    payload: Mapping[str, Any],
    *,
    ref: Mapping[str, Any],
    path: Path,
    force_reason: str | None = None,
) -> list[dict[str, object]]:
    schema = payload.get("schema") if isinstance(payload.get("schema"), str) else UNKNOWN
    before = payload.get("state_before") if isinstance(payload.get("state_before"), Mapping) else None
    after = payload.get("state_after") if isinstance(payload.get("state_after"), Mapping) else None
    action = payload.get("action")
    if not isinstance(action, (Mapping, list, str)):
        action = None
    scope, ambiguous = _scope(payload, state_before=_mapping(before))
    player, player_ambiguous = _player_id(payload)
    if player_ambiguous:
        ambiguous.append("identity.player_id")
    if schema == MODE_RANKING_SCHEMA:
        source = _source(primary="ranking", tags=("ranking", "api"))
    elif schema == LEADERBOARD_QUERY_SCHEMA:
        source = _source(primary="api", tags=("api",))
    else:
        source = _source(primary=UNKNOWN, tags=())
    candidates = _list(payload.get("legal_candidates"))
    legal = _legal(
        candidates,
        complete=payload.get("legal_actions_complete") is True,
        exact=payload.get("exact") is True,
        candidate_scope=UNKNOWN,
        chosen_member=None,
    )
    return [
        _base_label(
            source_ref=ref,
            source_schema=schema,
            granularity="diagnostic",
            scope=scope,
            source=source,
            trajectory_id=_trajectory_id(payload, payload.get("source_id")),
            player_id=player,
            snapshot_id=_state_snapshot_id(before),
            step=payload.get("step", payload.get("sequence")),
            state_before=before,
            action=action,
            state_after=after,
            legal=legal,
            assets=_assets_from_state(before),
            reward=_reward(
                score=payload.get("score", payload.get("terminal_score")),
                reward=payload.get("reward"),
                terminal=payload.get("terminal"),
            ),
            exact={
                "state_before": False,
                "action": False,
                "state_after": False,
                "reward": False,
                "score": False,
                "terminal": False,
                "transition": False,
            },
            join_status="unresolved" if schema in (MODE_RANKING_SCHEMA, LEADERBOARD_QUERY_SCHEMA) else "not_applicable",
            join_reasons=(force_reason or "unsupported-training-schema",),
            behavior_only=True,
            full_rl_policy_ready=False,
            parse_error=force_reason or "unsupported-training-schema",
            ambiguous_fields=ambiguous,
            force_tier="D",
        )
    ]


def _invalid_label(
    *,
    ref: Mapping[str, Any],
    reason: str,
) -> dict[str, object]:
    scope, _ = _scope({})
    return _base_label(
        source_ref=ref,
        source_schema=UNKNOWN,
        granularity="diagnostic",
        scope=scope,
        source=_source(primary=UNKNOWN, tags=()),
        trajectory_id=None,
        player_id=None,
        snapshot_id=None,
        step=None,
        state_before=None,
        action=None,
        state_after=None,
        legal=_legal(
            None,
            complete=False,
            exact=False,
            candidate_scope=UNKNOWN,
            chosen_member=None,
        ),
        assets={"loadout": None, "deck": None, "drink": None, "pitem": None},
        reward=_reward(),
        exact={
            "state_before": False,
            "action": False,
            "state_after": False,
            "reward": False,
            "score": False,
            "terminal": False,
            "transition": False,
        },
        join_status="unresolved",
        join_reasons=(reason,),
        behavior_only=True,
        full_rl_policy_ready=False,
        parse_error=reason,
        force_tier="D",
    )


def labels_for_payload(
    payload: Mapping[str, Any],
    *,
    source_ref: Mapping[str, Any],
    source_path: Path,
) -> list[dict[str, object]]:
    """Project one parsed source object into one or more canonical labels."""

    schema = payload.get("schema")
    if schema == LEADERBOARD_EPISODE_SCHEMA:
        return _episode_labels(payload, ref=source_ref, path=source_path)
    if schema == TRAINING_OBSERVATION_SCHEMA:
        return _observation_labels(payload, ref=source_ref, path=source_path)
    if schema == OUTER_TRAJECTORY_SCHEMA:
        return _outer_labels(payload, ref=source_ref, path=source_path)
    if schema == INNER_TRANSITION_SCHEMA:
        return _transition_labels(payload, ref=source_ref, path=source_path, fragment=False)
    if schema == FRAGMENT_TRANSITION_SCHEMA:
        return _transition_labels(payload, ref=source_ref, path=source_path, fragment=True)
    if schema == LEGAL_DECISION_SCHEMA:
        return _legal_decision_labels(payload, ref=source_ref, path=source_path)
    if schema == RUNTIME_LEGAL_SCHEMA:
        return _runtime_legal_report_labels(payload, ref=source_ref, path=source_path)
    if schema == RUNTIME_RECORDER_SCHEMA:
        return _runtime_recorder_labels(payload, ref=source_ref, path=source_path)
    return _generic_labels(payload, ref=source_ref, path=source_path)


@dataclass(frozen=True, slots=True)
class SourceStats:
    path: str
    sha256: str
    bytes: int
    source_records: int
    labels: int
    invalid_records: int


@dataclass(frozen=True, slots=True)
class TrajectorySplitPolicy:
    path: Path
    sha256: str
    assignments: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(self.path).resolve())
        object.__setattr__(self, "assignments", dict(self.assignments))


def _load_trajectory_split_policy(
    path: str | Path,
) -> TrajectorySplitPolicy:
    source = Path(path).resolve()
    if not source.is_file():
        raise FileNotFoundError(
            f"trajectory split manifest does not exist: {source}"
        )

    def object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(
                    f"duplicate trajectory split manifest key: {key!r}"
                )
            result[key] = value
        return result

    try:
        payload = json.loads(
            source.read_text(encoding="utf-8-sig"),
            parse_constant=_reject_json_constant,
            object_pairs_hook=object_pairs,
        )
    except (json.JSONDecodeError, UnicodeError) as error:
        raise ValueError(
            f"invalid trajectory split manifest: {type(error).__name__}"
        ) from error
    if not isinstance(payload, Mapping) or set(payload) != {
        "policy_version",
        "trajectory_splits",
    }:
        raise ValueError(
            "trajectory split manifest must contain only "
            "policy_version and trajectory_splits"
        )
    if type(payload.get("policy_version")) is not int or payload.get(
        "policy_version"
    ) != TRAJECTORY_SPLIT_POLICY_VERSION:
        raise ValueError("unsupported trajectory split policy_version")
    raw = payload.get("trajectory_splits")
    if not isinstance(raw, Mapping):
        raise ValueError("trajectory_splits must be an object")
    assignments: dict[str, str] = {}
    for trajectory_id, assignment in raw.items():
        if not isinstance(trajectory_id, str) or not trajectory_id.strip():
            raise ValueError("trajectory split ID must be non-empty text")
        if assignment not in TRAJECTORY_SPLIT_ASSIGNMENTS:
            raise ValueError(
                f"invalid trajectory split assignment: {assignment!r}"
            )
        assignments[trajectory_id] = str(assignment)
    return TrajectorySplitPolicy(
        source,
        _sha256_file(source),
        assignments,
    )


def _iter_json_records(path: Path) -> Iterator[tuple[int, Mapping[str, Any] | None, str, str | None]]:
    if path.suffix.casefold() == ".jsonl":
        with path.open("r", encoding="utf-8-sig") as handle:
            for line_number, line in enumerate(handle, 1):
                stripped = line.strip()
                if not stripped:
                    continue
                record_sha = hashlib.sha256(stripped.encode("utf-8")).hexdigest()
                try:
                    value = json.loads(stripped, parse_constant=_reject_json_constant)
                except (json.JSONDecodeError, UnicodeError, ValueError) as error:
                    yield line_number, None, record_sha, f"invalid-json:{type(error).__name__}"
                    continue
                if not isinstance(value, Mapping):
                    yield line_number, None, record_sha, "jsonl-record-not-object"
                    continue
                yield line_number, value, record_sha, None
        return

    raw = path.read_bytes()
    record_sha = hashlib.sha256(raw).hexdigest()
    try:
        value = json.loads(
            raw.decode("utf-8-sig"), parse_constant=_reject_json_constant
        )
    except (json.JSONDecodeError, UnicodeError, ValueError) as error:
        yield 1, None, record_sha, f"invalid-json:{type(error).__name__}"
        return
    if isinstance(value, Mapping):
        yield 1, value, record_sha, None
        return
    if isinstance(value, list):
        for index, item in enumerate(value, 1):
            item_sha = _digest(item)
            if isinstance(item, Mapping):
                yield index, item, item_sha, None
            else:
                yield index, None, item_sha, "json-array-record-not-object"
        return
    yield 1, None, record_sha, "json-root-not-object-or-array"


def _semantic_projection(label: Mapping[str, Any]) -> dict[str, object]:
    scope = dict(_mapping(label.get("scope")))
    # Raw spellings are audit evidence, not semantic identity.  The same
    # canonical scope may arrive once as native integers and once as enums.
    scope.pop("raw", None)
    transition = dict(_mapping(label.get("transition")))
    action = transition.get("action")
    if isinstance(action, Mapping) and action.get("kind") == "outer_action":
        # OuterTrajectory retains step_type/is_sp enrichment that the existing
        # NiaTrainingObservation schema does not.  Chosen action identity is
        # still identical, so optional enrichment must not manufacture an
        # ambiguity between the two projections.
        transition["action"] = {"kind": "outer_action", "id": action.get("id")}
    return {
        "granularity": label.get("granularity"),
        "scope": scope,
        "identity": label.get("identity"),
        "transition": transition,
        "legal": label.get("legal"),
        "assets": label.get("assets"),
        "reward": label.get("reward"),
    }


def _primary_identity(label: Mapping[str, Any], dedupe_id: str) -> str:
    identity = _mapping(label.get("identity"))
    action = _mapping(label.get("transition")).get("action")
    if isinstance(action, Mapping) and action.get("kind") == "outer_action":
        action = {"kind": "outer_action", "id": action.get("id")}
    action_signature = _digest(action)
    snapshot_id = identity.get("snapshot_id")
    if isinstance(snapshot_id, str) and snapshot_id:
        # Simplified accepted-live states can legitimately repeat across runs.
        # A snapshot hash is therefore scoped by trajectory and chosen action,
        # rather than being treated as a globally unique native object.
        return "|".join(
            (
                "snapshot",
                str(identity.get("trajectory_id") or UNKNOWN),
                snapshot_id,
                str(identity.get("step")),
                action_signature,
            )
        )
    trajectory_id = identity.get("trajectory_id")
    if isinstance(trajectory_id, str) and trajectory_id:
        scope = _mapping(label.get("scope"))
        return "|".join(
            (
                "trajectory",
                trajectory_id,
                str(scope.get("stage", UNKNOWN)),
                str(identity.get("step")),
                str(label.get("granularity", UNKNOWN)),
                action_signature,
            )
        )
    return f"semantic|{dedupe_id}"


def _split(label: Mapping[str, Any], dedupe_id: str) -> dict[str, object]:
    identity = _mapping(label.get("identity"))
    player = identity.get("player_id")
    trajectory = identity.get("trajectory_id")
    if isinstance(player, str) and player:
        basis, value, leakage_risk = "player", player, False
    elif isinstance(trajectory, str) and trajectory:
        basis, value, leakage_risk = "trajectory", trajectory, True
    else:
        basis, value, leakage_risk = "record", dedupe_id, True
    group_id = f"split:{_digest({'basis': basis, 'value': value})}"
    bucket = int(hashlib.sha256(group_id.encode("utf-8")).hexdigest()[:8], 16) % 100
    assignment = "train" if bucket < 80 else "validation" if bucket < 90 else "test"
    return {
        "group_id": group_id,
        "basis": basis,
        "assignment": assignment,
        "bucket": bucket,
        "player_leakage_risk": leakage_risk,
    }


def default_existing_sources(project_root: Path = PROJECT_ROOT) -> tuple[Path, ...]:
    """Return the bounded current corpus used by the canonical v1 build.

    The latest v6 merge supersedes its v1-v5 prefix copies.  Distinct native,
    live/outer, transition, fragment, legal-decision and runtime-legal sources
    remain separate inputs so their evidence contracts are not collapsed.
    """

    candidates = [
        project_root
        / "var"
        / "leaderboard_dataset"
        / "v330_nia_background_refresh_v6_incremental_plan1_concentration"
        / "episodes.jsonl",
        project_root
        / "var"
        / "leaderboard_dataset"
        / "v330_nia_native_verified_v1"
        / "episodes.jsonl",
        project_root / "var" / "nia_training" / "behavior_observations.jsonl",
        project_root / "var" / "nia_training" / "inner_transitions.jsonl",
        project_root / "var" / "nia_training" / "fragment_imitation_dynamics.jsonl",
        project_root / "var" / "nia_training" / "legal_decisions.jsonl",
        project_root
        / "var"
        / "coverage"
        / "runtime_legal_verified_probe_132724_v2.json",
    ]
    outer_root = project_root / "var" / "leaderboard_dataset"
    if outer_root.is_dir():
        candidates.extend(sorted(outer_root.glob("*/outer_schedules.jsonl")))
    exact_stage_root = project_root / "var" / "runtime_exact_stage"
    if exact_stage_root.is_dir():
        for transitions_path in sorted(
            exact_stage_root.rglob("inner_transitions.jsonl")
        ):
            report_path = transitions_path.with_name("report.json")
            if not report_path.is_file():
                continue
            try:
                report = json.loads(report_path.read_text(encoding="utf-8-sig"))
            except (json.JSONDecodeError, OSError, UnicodeError, ValueError):
                continue
            report_mapping = _mapping(report)
            output = _mapping(report_mapping.get("output"))
            if not (
                report_mapping.get("schema")
                == "gkms.runtime-exact-stage-dataset.v1"
                and report_mapping.get("dataset_ready") is True
                and output.get("transitions_sha256")
                == _sha256_file(transitions_path)
            ):
                continue
            candidates.append(transitions_path)
    return tuple(path for path in candidates if path.is_file())


def discover_inputs(values: Iterable[str | Path]) -> tuple[Path, ...]:
    result: set[Path] = set()
    for raw in values:
        path = Path(raw).resolve()
        if path.is_file():
            if path.suffix.casefold() not in {".json", ".jsonl"}:
                raise ValueError(f"unsupported input extension: {path}")
            result.add(path)
        elif path.is_dir():
            result.update(
                child.resolve()
                for child in path.rglob("*")
                if child.is_file() and child.suffix.casefold() in {".json", ".jsonl"}
            )
        else:
            raise FileNotFoundError(f"training-label input does not exist: {path}")
    return tuple(sorted(result, key=lambda value: str(value).casefold()))


def build_canonical_training_labels(
    inputs: Iterable[str | Path],
    output_dir: str | Path = DEFAULT_OUTPUT,
    *,
    trajectory_split_manifest: str | Path | None = None,
) -> dict[str, object]:
    """Build labels, manifest and checksums without mutating any input."""

    from .training_quarantine import load_current_training_quarantine
    quarantine = load_current_training_quarantine()

    source_paths = discover_inputs(inputs)
    if not source_paths:
        raise ValueError("canonical training-label build has no input files")
    output_root = Path(output_dir).resolve()
    split_policy = (
        None
        if trajectory_split_manifest is None
        else _load_trajectory_split_policy(trajectory_split_manifest)
    )
    labels_path = output_root / "labels.jsonl"
    manifest_path = output_root / "manifest.json"
    checksums_path = output_root / "checksums.sha256"
    output_targets = {labels_path.resolve(), manifest_path.resolve(), checksums_path.resolve()}
    overlap = output_targets.intersection(path.resolve() for path in source_paths)
    if split_policy is not None and split_policy.path in output_targets:
        overlap.add(split_policy.path)
    if overlap:
        raise ValueError(f"derived output must not overwrite an input: {sorted(map(str, overlap))}")
    output_root.mkdir(parents=True, exist_ok=True)

    tier_counts: Counter[str] = Counter()
    mode_counts: Counter[str] = Counter()
    archetype_counts: Counter[str] = Counter()
    stage_counts: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()
    schema_counts: Counter[str] = Counter()
    dedupe_counts: Counter[str] = Counter()
    split_counts: Counter[str] = Counter()
    join_counts: Counter[str] = Counter()
    compatibility_counts: Counter[str] = Counter()
    unknown_counts: Counter[str] = Counter()
    ambiguous_counts: Counter[str] = Counter()
    input_stats: list[SourceStats] = []
    seen_dedupe: dict[str, str] = {}
    seen_identity: dict[str, str] = {}
    total_labels = 0
    unknown_records = 0
    ambiguous_records = 0
    eligible_count = 0
    trajectory_split_override_count = 0
    quarantined_labels = 0
    quarantined_episodes: set[str] = set()

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".labels.jsonl.", suffix=".tmp", dir=output_root
    )
    temporary_labels = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            for path in source_paths:
                file_sha = _sha256_file(path)
                source_records = 0
                source_labels = 0
                invalid_records = 0
                for position, payload, record_sha, error in _iter_json_records(path):
                    source_records += 1
                    ref = _source_ref(
                        path=path,
                        file_sha256=file_sha,
                        record_sha256=record_sha,
                        position=position,
                    )
                    if error is not None or payload is None:
                        invalid_records += 1
                        projected = [_invalid_label(ref=ref, reason=error or "invalid-record")]
                    else:
                        try:
                            projected = labels_for_payload(
                                payload,
                                source_ref=ref,
                                source_path=path,
                            )
                        except (TypeError, ValueError, OverflowError) as adapter_error:
                            invalid_records += 1
                            projected = [
                                _invalid_label(
                                    ref=ref,
                                    reason=f"adapter-error:{type(adapter_error).__name__}",
                                )
                            ]

                    for label in projected:
                        semantic = _semantic_projection(label)
                        dedupe_digest = _digest(semantic)
                        dedupe_id = f"dedupe:{dedupe_digest}"
                        record_id = str(_mapping(label.get("source_record")).get("record_id"))
                        primary_identity = _primary_identity(label, dedupe_id)
                        previous_content = seen_identity.get(primary_identity)
                        if previous_content is not None and previous_content != dedupe_id:
                            dedupe_status = "ambiguous"
                            ambiguous_fields = list(label.get("ambiguous_fields", []))
                            ambiguous_fields.append("dedupe.primary_identity")
                            label["ambiguous_fields"] = sorted(set(ambiguous_fields))
                        elif dedupe_id in seen_dedupe:
                            dedupe_status = "duplicate"
                        else:
                            dedupe_status = "canonical"
                            seen_dedupe[dedupe_id] = record_id
                            seen_identity[primary_identity] = dedupe_id
                        label["dedupe"] = {
                            "id": dedupe_id,
                            "primary_identity": primary_identity,
                            "status": dedupe_status,
                            "duplicate_of": (
                                seen_dedupe.get(dedupe_id)
                                if dedupe_status == "duplicate"
                                else None
                            ),
                        }
                        split = _split(label, dedupe_id)
                        trajectory_id = _mapping(
                            label.get("identity")
                        ).get("trajectory_id")
                        if (
                            split_policy is not None
                            and isinstance(trajectory_id, str)
                            and trajectory_id in split_policy.assignments
                        ):
                            hash_assignment = split["assignment"]
                            split = {
                                **split,
                                "assignment": split_policy.assignments[
                                    trajectory_id
                                ],
                                "hash_assignment": hash_assignment,
                                "assignment_source": (
                                    "trajectory-split-manifest-v1"
                                ),
                            }
                            trajectory_split_override_count += 1
                        label["split"] = split
                        quality = _mapping(label.get("quality"))
                        join = _mapping(label.get("join"))
                        quality_reasons = set(quality.get("reasons", []))
                        critical_unknown = {
                            "scope.mode",
                            "scope.archetype",
                            "scope.stage",
                            "source.primary",
                            "transition.action",
                        }.intersection(label.get("unknown_fields", []))
                        eligible = (
                            quality.get("tier") in {"A", "B", "C"}
                            and dedupe_status == "canonical"
                            and join.get("status") != "ambiguous"
                            and not label.get("ambiguous_fields")
                            and not critical_unknown
                            and "source-provenance-missing" not in quality_reasons
                        )
                        if (
                            join.get("status") == "unresolved"
                            and "runtime-legal-v2" in label.get("compatibility", [])
                        ):
                            eligible = False
                        excluded = [] if quarantine is None else quarantine.match(label, payload or {}, ref)
                        if excluded:
                            eligible = False
                            quarantined_labels += 1
                            quarantined_episodes.update(excluded)
                            label["training_exclusion"] = {
                                "reason": "user-directed-temporary-episode-quarantine",
                                "episode_ids": excluded,
                                "manifest_sha256": quarantine.manifest_sha256,
                                "source_or_quality_tier_rewritten": False,
                            }
                        quality_value = dict(quality)
                        quality_value["training_eligible"] = eligible
                        label["quality"] = quality_value

                        encoded = _canonical_bytes(label) + b"\n"
                        output.write(encoded)
                        total_labels += 1
                        source_labels += 1
                        tier_counts[str(quality.get("tier", "D"))] += 1
                        scope = _mapping(label.get("scope"))
                        mode_counts[str(scope.get("mode", UNKNOWN))] += 1
                        archetype_counts[str(scope.get("archetype", UNKNOWN))] += 1
                        stage_counts[str(scope.get("stage", UNKNOWN))] += 1
                        source_value = _mapping(label.get("source"))
                        source_counts[str(source_value.get("primary", UNKNOWN))] += 1
                        schema_counts[str(label.get("source_schema", UNKNOWN))] += 1
                        dedupe_counts[dedupe_status] += 1
                        split_counts[str(split["assignment"])] += 1
                        join_counts[str(join.get("status", "unresolved"))] += 1
                        for value in label.get("compatibility", []):
                            compatibility_counts[str(value)] += 1
                        unknown_fields = list(label.get("unknown_fields", []))
                        if unknown_fields:
                            unknown_records += 1
                        for field in unknown_fields:
                            unknown_counts[str(field)] += 1
                        ambiguous_fields = list(label.get("ambiguous_fields", []))
                        if ambiguous_fields:
                            ambiguous_records += 1
                        for field in ambiguous_fields:
                            ambiguous_counts[str(field)] += 1
                        if eligible:
                            eligible_count += 1
                input_stats.append(
                    SourceStats(
                        path=_source_ref(
                            path=path,
                            file_sha256=file_sha,
                            record_sha256=file_sha,
                            position=0,
                        )["path"],
                        sha256=file_sha,
                        bytes=path.stat().st_size,
                        source_records=source_records,
                        labels=source_labels,
                        invalid_records=invalid_records,
                    )
                )
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_labels, labels_path)
    finally:
        if temporary_labels.exists():
            temporary_labels.unlink()

    labels_sha = _sha256_file(labels_path)
    manifest: dict[str, object] = {
        "schema": MANIFEST_SCHEMA,
        "label_schema": LABEL_SCHEMA,
        "contract": {
            "unknown_policy": "fail-closed-retain-row",
            "quality_tiers": {
                "A": "full exact unified-policy transition with numeric reward and known terminal",
                "B": "exact local transition or complete/exact contextual-policy candidate set",
                "C": "reliable observed behavior missing an exact legal or transition boundary",
                "D": "diagnostic, malformed, unsupported, or action-unknown evidence",
            },
            "split_policy": (
                "optional trajectory manifest override; otherwise player, "
                "else trajectory, else record; sha256 80/10/10"
            ),
            "legal_policy": "exact and complete are independent; complete is scoped by candidate_scope",
            "original_inputs_mutated": False,
        },
        "counts": {
            "labels": total_labels,
            "training_eligible": eligible_count,
            "records_with_unknown": unknown_records,
            "records_with_ambiguity": ambiguous_records,
            "by_quality_tier": dict(sorted(tier_counts.items())),
            "by_mode": dict(sorted(mode_counts.items())),
            "by_archetype": dict(sorted(archetype_counts.items())),
            "by_stage": dict(sorted(stage_counts.items())),
            "by_source": dict(sorted(source_counts.items())),
            "by_source_schema": dict(sorted(schema_counts.items())),
            "by_dedupe_status": dict(sorted(dedupe_counts.items())),
            "by_split": dict(sorted(split_counts.items())),
            "by_join_status": dict(sorted(join_counts.items())),
            "by_compatibility": dict(sorted(compatibility_counts.items())),
        },
        "unknown_field_counts": dict(sorted(unknown_counts.items())),
        "ambiguous_field_counts": dict(sorted(ambiguous_counts.items())),
        "inputs": [
            {
                "path": item.path,
                "sha256": item.sha256,
                "bytes": item.bytes,
                "source_records": item.source_records,
                "labels": item.labels,
                "invalid_records": item.invalid_records,
            }
            for item in input_stats
        ],
        "artifacts": {
            "labels": labels_path.name,
            "labels_sha256": labels_sha,
            "labels_bytes": labels_path.stat().st_size,
            "manifest": manifest_path.name,
            "checksums": checksums_path.name,
        },
    }
    if split_policy is not None:
        manifest["trajectory_split_policy"] = {
            "policy_version": TRAJECTORY_SPLIT_POLICY_VERSION,
            "source_path": _source_ref(
                path=split_policy.path,
                file_sha256=split_policy.sha256,
                record_sha256=split_policy.sha256,
                position=0,
            )["path"],
            "source_sha256": split_policy.sha256,
            "mapping_count": len(split_policy.assignments),
            "override_count": trajectory_split_override_count,
        }
    if quarantine is not None:
        # Membership remains in the derived labels; only new training admission
        # changes. Existing frozen datasets/models are never rewritten here.
        manifest["training_quarantine"] = {
            **quarantine.reference(), "labels_excluded": quarantined_labels,
            "matched_episode_ids": sorted(quarantined_episodes),
            "source_records_removed": False, "historical_artifacts_rewritten": False,
        }
    _atomic_write(
        manifest_path,
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        + b"\n",
    )
    manifest_sha = _sha256_file(manifest_path)
    checksum_payload = (
        f"{labels_sha}  {labels_path.name}\n"
        f"{manifest_sha}  {manifest_path.name}\n"
    ).encode("ascii")
    _atomic_write(checksums_path, checksum_payload)
    result = dict(manifest)
    result["artifact_hashes"] = {
        "labels.jsonl": labels_sha,
        "manifest.json": manifest_sha,
        "checksums.sha256": _sha256_file(checksums_path),
    }
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build fail-closed canonical training-label v1 artifacts"
    )
    parser.add_argument(
        "--input",
        action="append",
        default=[],
        help="JSON/JSONL file or directory; repeatable",
    )
    parser.add_argument(
        "--default-existing",
        action="store_true",
        help="include the bounded current v6/native/live/outer/legal corpus",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--trajectory-split-manifest",
        type=Path,
        default=None,
        help=(
            "optional v1 trajectory-to-train/validation/test override; "
            "unmapped trajectories retain the canonical hash split"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    inputs: list[str | Path] = list(args.input)
    if args.default_existing or not inputs:
        inputs.extend(default_existing_sources())
    manifest = build_canonical_training_labels(
        inputs,
        args.output,
        trajectory_split_manifest=args.trajectory_split_manifest,
    )
    summary = {
        "schema": manifest["schema"],
        "counts": manifest["counts"],
        "artifacts": manifest["artifacts"],
        "artifact_hashes": manifest["artifact_hashes"],
    }
    if "trajectory_split_policy" in manifest:
        summary["trajectory_split_policy"] = manifest[
            "trajectory_split_policy"
        ]
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


__all__ = [
    "ARCHETYPE_BY_EFFECT",
    "DEFAULT_OUTPUT",
    "LABEL_SCHEMA",
    "MANIFEST_SCHEMA",
    "QUALITY_TIERS",
    "RUNTIME_LEGAL_SCHEMA",
    "RUNTIME_LEGAL_V2_SHA256",
    "SOURCE_KINDS",
    "TRAJECTORY_SPLIT_ASSIGNMENTS",
    "TRAJECTORY_SPLIT_POLICY_VERSION",
    "TrajectorySplitPolicy",
    "UNKNOWN",
    "build_canonical_training_labels",
    "default_existing_sources",
    "discover_inputs",
    "labels_for_payload",
    "main",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
