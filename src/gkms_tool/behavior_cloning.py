"""NumPy behavior-cloning models for frozen GKMS outer and replay data.

The outer policy is a masked candidate-pointer network.  The replay policy is
an action-prefix classifier over syntax tokens; replay indexes are never
interpreted as card identities.  Both models are shadow-only artifacts.
"""

from __future__ import annotations

from .card_semantic_features import (
    FEATURE_SCHEMA as CARD_FEATURE_SCHEMA, SEMANTIC_ENCODING,
    copy_card_feature_contract, load_card_semantic_features,
    validate_card_feature_encoding,
)

import argparse
import hashlib
import json
import math
import os
import tempfile
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import numpy as np

from .canonical_training_labels import LABEL_SCHEMA
from .exact_policy_contract import (
    strict_exact_candidate_index,
    validate_exact_legal_candidates,
)
from .nia_inner_transition_dataset import match_legal_candidate_indices
from .training_artifact_io import (
    canonical_json_bytes as _canonical_bytes,
    sha256_file as _sha256_file,
)
from .training_spec import (
    DEFAULT_OUTPUT as DEFAULT_SPEC_PATH,
    DEFAULT_V2_OUTPUT as DEFAULT_EXACT_SPEC_PATH,
    SPEC_V2_SCHEMA,
    SPEC_SCOPED_SCHEMA,
    build_scoped_training_spec,
)


OUTER_REPORT_SCHEMA: Final = "gkms.outer-behavior-cloning-report.v1"
REPLAY_REPORT_SCHEMA: Final = "gkms.replay-sequence-cloning-report.v1"
UNIFIED_REPORT_SCHEMA: Final = "gkms.unified-behavior-cloning-smoke.v1"
EXACT_REPORT_SCHEMA: Final = "gkms.exact-main-action-behavior-cloning-report.v1"
MODEL_SCHEMA: Final = "gkms.numpy-behavior-cloning-model.v1"
PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
DEFAULT_FROZEN_ROOT: Final = (
    PROJECT_ROOT / "var" / "training_dataset" / "frozen_v1"
)
DEFAULT_MODEL_ROOT: Final = PROJECT_ROOT / "var" / "models" / "behavior_cloning_v1"
DEFAULT_EXACT_STAGES: Final = (
    PROJECT_ROOT
    / "var"
    / "offline_rl"
    / "exact_main_action_baseline_20260829_v2_frozen"
    / "stages.jsonl"
)

SPLIT_CODES: Final = {"train": 0, "validation": 1, "test": 2}
SPLIT_NAMES: Final = {value: key for key, value in SPLIT_CODES.items()}


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _hash_index(token: str, buckets: int) -> int:
    if buckets < 2:
        raise ValueError("feature hash buckets must be at least two")
    value = int(hashlib.sha256(token.encode("utf-8")).hexdigest()[:16], 16)
    return 1 + value % (buckets - 1)


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(
                json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode(
                    "utf-8"
                )
                + b"\n"
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _atomic_npz(path: Path, arrays: Mapping[str, np.ndarray]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            np.savez_compressed(handle, **arrays)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _candidate_key(value: object) -> str:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, Mapping):
        for key in ("id", "action_id", "step_type", "card_id", "drink_id"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
    return "json:" + hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _chosen_outer_key(action: Mapping[str, Any]) -> str | None:
    for key in ("id", "action_id", "step_type", "card_id", "drink_id"):
        value = action.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _numeric_token(name: str, value: int | float) -> str:
    if not math.isfinite(float(value)):
        return f"num:{name}:nonfinite"
    magnitude = abs(float(value))
    if magnitude <= 20:
        bucket = round(float(value), 1)
    elif magnitude <= 1000:
        bucket = int(round(float(value) / 10.0) * 10)
    else:
        bucket = int(round(float(value) / 100.0) * 100)
    return f"num:{name}:{bucket}"


def _state_tokens(state: object) -> list[str]:
    if not isinstance(state, Mapping):
        return ["state:missing"]
    result: list[str] = []
    for key, value in sorted(state.items()):
        if isinstance(value, bool):
            result.append(f"state:{key}:{int(value)}")
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            result.append(_numeric_token(f"state:{key}", value))
        elif isinstance(value, str) and value and len(value) <= 96:
            result.append(f"state:{key}:{value}")
    return result or ["state:present-no-scalars"]


def _outer_context_tokens(row: Mapping[str, Any], candidate_keys: Sequence[str]) -> list[str]:
    scope = _mapping(row.get("scope"))
    identity = _mapping(row.get("identity"))
    transition = _mapping(row.get("transition"))
    return build_outer_context_tokens(
        mode=scope.get("mode"),
        produce_id=scope.get("produce_id"),
        plan_type=scope.get("plan_type"),
        exam_effect_type=scope.get("exam_effect_type"),
        archetype=scope.get("archetype"),
        stage=scope.get("stage"),
        idol_card_id=scope.get("idol_card_id"),
        character_id=scope.get("character_id"),
        step=identity.get("step"),
        source_schema=row.get("source_schema"),
        candidate_keys=candidate_keys,
        state_before=transition.get("state_before"),
    )


def build_outer_context_tokens(
    *,
    mode: object,
    produce_id: object,
    plan_type: object,
    exam_effect_type: object,
    archetype: object,
    stage: object,
    idol_card_id: object,
    character_id: object,
    step: object,
    source_schema: object,
    candidate_keys: Sequence[str],
    state_before: object,
) -> list[str]:
    result = [
        "bias",
        f"mode:{mode}",
        f"produce:{produce_id}",
        f"plan:{plan_type}",
        f"effect:{exam_effect_type}",
        f"archetype:{archetype}",
        f"stage:{stage}",
        f"idol:{idol_card_id}",
        f"character:{character_id}",
        f"step:{step}",
        f"source-schema:{source_schema}",
        f"candidate-count:{len(candidate_keys)}",
    ]
    result.extend(f"candidate-present:{value}" for value in sorted(candidate_keys))
    result.extend(_state_tokens(state_before))
    return result


def _pad_token_rows(rows: Sequence[Sequence[int]]) -> tuple[np.ndarray, np.ndarray]:
    width = max((len(row) for row in rows), default=1)
    indices = np.zeros((len(rows), width), dtype=np.int32)
    mask = np.zeros((len(rows), width), dtype=np.float32)
    for index, row in enumerate(rows):
        if row:
            indices[index, : len(row)] = row
            mask[index, : len(row)] = 1.0
    return indices, mask


def _pad_candidate_token_rows(
    rows: Sequence[Sequence[Sequence[int]]],
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    candidate_width = max((len(row) for row in rows), default=1)
    token_width = max(
        (len(tokens) for row in rows for tokens in row),
        default=1,
    )
    indices = np.zeros((len(rows), candidate_width, token_width), dtype=np.int32)
    token_mask = np.zeros_like(indices, dtype=np.float32)
    candidate_mask = np.zeros((len(rows), candidate_width), dtype=np.float32)
    for row_index, row in enumerate(rows):
        for candidate_index, tokens in enumerate(row):
            if not tokens:
                continue
            indices[row_index, candidate_index, : len(tokens)] = tokens
            token_mask[row_index, candidate_index, : len(tokens)] = 1.0
            candidate_mask[row_index, candidate_index] = 1.0
    return indices, token_mask, candidate_mask


@dataclass(frozen=True, slots=True)
class OuterDataset:
    context_indices: np.ndarray
    context_mask: np.ndarray
    candidate_indices: np.ndarray
    candidate_token_mask: np.ndarray
    candidate_mask: np.ndarray
    targets: np.ndarray
    splits: np.ndarray
    weights: np.ndarray
    flow_ids: np.ndarray
    flow_names: tuple[str, ...]
    candidate_keys: tuple[tuple[str, ...], ...]
    chosen_keys: tuple[str, ...]
    trajectory_ids: tuple[str, ...]
    context_buckets: int
    candidate_buckets: int

    @property
    def size(self) -> int:
        return int(self.targets.shape[0])


def load_outer_dataset(
    labels_path: Path,
    *,
    context_buckets: int = 4096,
    candidate_buckets: int = 1024,
) -> OuterDataset:
    context_rows: list[list[int]] = []
    candidate_rows: list[list[list[int]]] = []
    target_rows: list[int] = []
    split_rows: list[int] = []
    flow_values: list[str] = []
    candidate_key_rows: list[tuple[str, ...]] = []
    chosen_values: list[str] = []
    trajectories: list[str] = []
    trajectory_split: dict[str, int] = {}
    for line_number, line in enumerate(
        Path(labels_path).open("r", encoding="utf-8-sig"), start=1
    ):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, Mapping) or row.get("schema") != LABEL_SCHEMA:
            raise ValueError(f"invalid canonical label at line {line_number}")
        legal = _mapping(row.get("legal"))
        if not (
            _mapping(row.get("dedupe")).get("status") == "canonical"
            and row.get("granularity") == "decision"
            and legal.get("candidate_scope") == "outer"
            and legal.get("complete") is True
            and legal.get("exact") is True
            and legal.get("chosen_member") is True
            and _mapping(row.get("quality")).get("training_eligible") is True
        ):
            continue
        candidates = legal.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            continue
        keys = tuple(_candidate_key(value) for value in candidates)
        chosen = _chosen_outer_key(
            _mapping(_mapping(row.get("transition")).get("action"))
        )
        matches = [index for index, key in enumerate(keys) if key == chosen]
        if len(matches) != 1:
            raise ValueError(
                f"outer chosen candidate is not unique at line {line_number}"
            )
        split = _mapping(row.get("split")).get("assignment")
        if split not in SPLIT_CODES:
            raise ValueError(f"outer split is invalid at line {line_number}")
        scope = _mapping(row.get("scope"))
        flow = "|".join(
            str(scope.get(key))
            for key in ("produce_id", "plan_type", "exam_effect_type")
        )
        trajectory_id = _mapping(row.get("identity")).get("trajectory_id")
        if not isinstance(trajectory_id, str) or not trajectory_id:
            raise ValueError(f"outer row has no trajectory at line {line_number}")
        split_code = SPLIT_CODES[str(split)]
        previous_split = trajectory_split.setdefault(trajectory_id, split_code)
        if previous_split != split_code:
            raise ValueError("one outer trajectory crosses dataset splits")
        tokens = _outer_context_tokens(row, keys)
        context_rows.append(
            [_hash_index(value, context_buckets) for value in tokens]
        )
        candidate_rows.append(
            [
                [_hash_index(f"candidate:{value}", candidate_buckets)]
                for value in keys
            ]
        )
        target_rows.append(matches[0])
        split_rows.append(split_code)
        flow_values.append(flow)
        candidate_key_rows.append(keys)
        chosen_values.append(str(chosen))
        trajectories.append(trajectory_id)
    if not target_rows:
        raise ValueError("outer behavior dataset is empty")
    context_indices, context_mask = _pad_token_rows(context_rows)
    candidate_indices, candidate_token_mask, candidate_mask = (
        _pad_candidate_token_rows(candidate_rows)
    )
    flow_names = tuple(sorted(set(flow_values)))
    flow_index = {value: index for index, value in enumerate(flow_names)}
    flow_ids = np.asarray([flow_index[value] for value in flow_values], dtype=np.int16)
    splits = np.asarray(split_rows, dtype=np.int8)
    trajectory_counts = Counter(
        (int(split), trajectory)
        for split, trajectory in zip(splits.tolist(), trajectories, strict=True)
    )
    flow_counts = Counter(
        (int(split), int(flow))
        for split, flow in zip(splits.tolist(), flow_ids.tolist(), strict=True)
    )
    weights = np.asarray(
        [
            1.0
            / trajectory_counts[(int(split), trajectory)]
            / math.sqrt(flow_counts[(int(split), int(flow))])
            for split, trajectory, flow in zip(
                splits.tolist(), trajectories, flow_ids.tolist(), strict=True
            )
        ],
        dtype=np.float32,
    )
    train_mean = float(weights[splits == SPLIT_CODES["train"]].mean())
    if train_mean > 0:
        weights /= train_mean
    return OuterDataset(
        context_indices=context_indices,
        context_mask=context_mask,
        candidate_indices=candidate_indices,
        candidate_token_mask=candidate_token_mask,
        candidate_mask=candidate_mask,
        targets=np.asarray(target_rows, dtype=np.int16),
        splits=splits,
        weights=weights,
        flow_ids=flow_ids,
        flow_names=flow_names,
        candidate_keys=tuple(candidate_key_rows),
        chosen_keys=tuple(chosen_values),
        trajectory_ids=tuple(trajectories),
        context_buckets=context_buckets,
        candidate_buckets=candidate_buckets,
    )


def _state_card_semantics(state: object) -> dict[str, tuple[str, object]]:
    result: dict[str, tuple[str, object]] = {}
    zones = _mapping(_mapping(state).get("zones"))
    for entries in zones.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            guid = entry.get("guid")
            card_id = entry.get("card_id")
            if isinstance(guid, str) and guid and isinstance(card_id, str) and card_id:
                result[guid] = (
                    card_id,
                    entry.get("effective_upgrade", entry.get("base_upgrade", "unknown")),
                )
    return result


def _unified_candidate_semantic(
    value: object,
    state_cards: Mapping[str, tuple[str, object]],
) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, Mapping):
        return _candidate_key(value)
    kind = str(value.get("kind", value.get("action_type", "unknown")))
    if kind in {"play", "card", "use-hand", "use_hand"}:
        guid = value.get("guid", value.get("card_guid"))
        state_card = state_cards.get(str(guid)) if isinstance(guid, str) else None
        card_id = value.get("card_id", value.get("id"))
        upgrade = value.get("upgrade", value.get("upgrade_count"))
        if state_card is not None:
            if not isinstance(card_id, str) or not card_id:
                card_id = state_card[0]
            if upgrade is None:
                upgrade = state_card[1]
        return "|".join(
            (
                "play",
                str(card_id if card_id is not None else "unknown"),
                str(upgrade if upgrade is not None else "unknown"),
            )
        )
    if kind in {"drink", "use-drink", "use_drink"}:
        return "drink|" + str(value.get("drink_id", value.get("id", "unknown")))
    if kind in {"end_turn", "turn-end", "turn_end"}:
        return "end-turn"
    return "semantic:" + _digest_without_runtime_identity(value)


def _digest_without_runtime_identity(value: Mapping[str, Any]) -> str:
    semantic = {
        str(key): item
        for key, item in value.items()
        if key
        not in {
            "guid",
            "card_guid",
            "action_id",
            "instance_id",
            "slot",
            "slot_index",
            "hand_slot",
            "index",
        }
    }
    return hashlib.sha256(_canonical_bytes(semantic)).hexdigest()


def _unified_context_tokens(row: Mapping[str, Any]) -> list[str]:
    scope = _mapping(row.get("scope"))
    transition = _mapping(row.get("transition"))
    result = [
        "bias",
        f"produce:{scope.get('produce_id')}",
        f"plan:{scope.get('plan_type')}",
        f"effect:{scope.get('exam_effect_type')}",
        f"archetype:{scope.get('archetype')}",
        f"stage:{scope.get('stage')}",
        f"source-schema:{row.get('source_schema')}",
    ]
    result.extend(_state_tokens(transition.get("state_before")))
    return result


def _exact_card_runtime_by_guid(state: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for zone in (
        "handList",
        "deckList",
        "graveList",
        "lostList",
        "holdList",
        "removedCardList",
    ):
        entries = state.get(zone)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            guid = entry.get("_guid", entry.get("guid"))
            if isinstance(guid, str) and guid:
                result[guid] = entry
    return result


def exact_candidate_semantics(
    state_before: Mapping[str, Any],
    legal_candidates: Sequence[object],
    *,
    observed_slot_binding: bool = False,
) -> tuple[str, ...]:
    """Create stable candidate features while keeping GUIDs binding-only."""

    if type(observed_slot_binding) is not bool:
        raise ValueError('Observed slot lookup must be explicitly selected')
    cards = {} if observed_slot_binding else _exact_card_runtime_by_guid(state_before)
    result: list[str] = []
    for position, raw in enumerate(legal_candidates):
        candidate = _mapping(raw)
        kind = str(candidate.get("kind", candidate.get("action_type", "unknown")))
        slot = candidate.get("slot_index", position)
        if kind in {"play", "card", "use-hand", "use_hand"}:
            guid = candidate.get("card_guid", candidate.get("guid"))
            if observed_slot_binding:
                from .observed_primary_identity import observed_hand_card
                runtime = observed_hand_card(state_before, candidate)
            else:
                runtime = cards.get(str(guid), {}) if isinstance(guid, str) else {}
            card_data = _mapping(runtime.get("_cardData"))
            card_id = candidate.get("card_id", card_data.get("_id", "unknown"))
            upgrade = candidate.get("upgrade", card_data.get("_upgradeCount", "unknown"))
            play_count = runtime.get("_playCount", "unknown")
            result.append(
                "|".join(
                    (
                        "use-hand",
                        f"card:{card_id}",
                        f"upgrade:{upgrade}",
                        f"play-count:{play_count}",
                        f"slot:{slot}",
                    )
                )
            )
        elif kind in {"drink", "use-drink", "use_drink"}:
            result.append(
                "|".join(
                    (
                        "use-drink",
                        f"drink:{candidate.get('drink_id', candidate.get('id', 'unknown'))}",
                        f"slot:{slot}",
                    )
                )
            )
        elif kind in {"end_turn", "turn-end", "turn_end"}:
            result.append("turn-end")
        else:
            result.append(
                "|".join(
                    (
                        "other",
                        _digest_without_runtime_identity(candidate),
                        f"slot:{slot}",
                    )
                )
            )
    return tuple(result)


def exact_candidate_field_tokens(
    state_before: Mapping[str, Any],
    legal_candidates: Sequence[object],
    *,
    semantic_features: Any | None = None,
    observed_slot_binding: bool = False,
) -> tuple[tuple[str, ...], ...]:
    if type(observed_slot_binding) is not bool:
        raise ValueError('Observed slot lookup must be explicitly selected')
    cards = {} if observed_slot_binding else _exact_card_runtime_by_guid(state_before)
    result: list[tuple[str, ...]] = []
    for position, raw in enumerate(legal_candidates):
        candidate = _mapping(raw)
        kind = str(candidate.get("kind", candidate.get("action_type", "unknown")))
        slot = candidate.get("slot_index", position)
        if kind in {"play", "card", "use-hand", "use_hand"}:
            guid = candidate.get("card_guid", candidate.get("guid"))
            if observed_slot_binding:
                from .observed_primary_identity import observed_hand_card
                runtime = observed_hand_card(state_before, candidate)
            else:
                runtime = cards.get(str(guid), {}) if isinstance(guid, str) else {}
            card_data = _mapping(runtime.get("_cardData"))
            card_id = str(candidate.get("card_id", card_data.get("_id", "unknown")))
            upgrade = candidate.get("upgrade", card_data.get("_upgradeCount", "unknown"))
            play_count = runtime.get("_playCount", "unknown")
            card_class = "-".join(card_id.split("-")[:3])
            result.append(
                (
                    "kind:use-hand",
                    f"slot:{slot}",
                    f"kind-slot:use-hand:{slot}",
                    f"upgrade:{upgrade}",
                    f"play-count:{play_count}",
                    f"class:{card_class}",
                    f"card:{card_id}",
                )
            )
            if semantic_features is not None:
                result[-1] = (*result[-1], *semantic_features.card_tokens(card_id, upgrade, runtime))
                result[-1] = (*result[-1], *semantic_features.action_context_tokens("play", state_before))
        elif kind in {"drink", "use-drink", "use_drink"}:
            result.append(
                (
                    "kind:use-drink",
                    f"slot:{slot}",
                    f"kind-slot:use-drink:{slot}",
                    f"drink:{candidate.get('drink_id', candidate.get('id', 'unknown'))}",
                )
            )
            if semantic_features is not None:
                drink_id = str(candidate.get("drink_id", candidate.get("id", "unknown")))
                result[-1] = (*result[-1], *semantic_features.drink_tokens(drink_id))
                result[-1] = (*result[-1], *semantic_features.action_context_tokens("drink", state_before, drink_id=drink_id))
        elif kind in {"end_turn", "turn-end", "turn_end"}:
            result.append(("kind:turn-end",))
            if semantic_features is not None:
                result[-1] = (*result[-1], *semantic_features.action_context_tokens("end_turn", state_before))
        else:
            result.append(("kind:other", f"slot:{slot}"))
    return tuple(result)


def _exact_candidate_context_semantics(
    field_tokens: Sequence[Sequence[str]],
) -> tuple[str, ...]:
    retained_prefixes = (
        "kind:",
        "slot:",
        "upgrade:",
        "play-count:",
    )
    return tuple(
        "|".join(
            value
            for value in tokens
            if value.startswith(retained_prefixes)
        )
        for tokens in field_tokens
    )


def build_exact_main_action_context_tokens(
    *,
    state_before: Mapping[str, Any],
    flow: str,
    stage: str,
    candidate_semantics: Sequence[str],
    semantic_features: Any | None = None,
) -> list[str]:
    parts = flow.split("|")
    produce_id = parts[0] if len(parts) == 3 else "unknown"
    plan_type = parts[1] if len(parts) == 3 else "unknown"
    effect_type = parts[2] if len(parts) == 3 else "unknown"
    result = [
        "bias",
        f"produce:{produce_id}",
        f"plan:{plan_type}",
        f"effect:{effect_type}",
        f"stage:{stage}",
        f"candidate-count:{len(candidate_semantics)}",
    ]
    current_state_features = bool(getattr(semantic_features, "uses_current_state_features", False))
    if not current_state_features:
        result.extend(_state_tokens(state_before))
    for zone in ("handList", "deckList", "graveList", "lostList", "holdList"):
        entries = state_before.get(zone)
        if isinstance(entries, list):
            result.append(f"zone-count:{zone}:{len(entries)}")
    hand = state_before.get("handList")
    if isinstance(hand, list):
        for slot, value in enumerate(hand):
            if not isinstance(value, Mapping):
                continue
            card_data = _mapping(value.get("_cardData", value.get("cardData")))
            result.append(
                "|".join(
                    (
                        "hand-card",
                        f"id:{card_data.get('_id', card_data.get('id', 'unknown'))}",
                        f"upgrade:{card_data.get('_upgradeCount', card_data.get('upgradeCount', 'unknown'))}",
                        f"play-count:{value.get('_playCount', value.get('playCount', 'unknown'))}",
                        f"slot:{slot}",
                    )
                )
            )
    drinks = state_before.get("drinkList")
    if isinstance(drinks, list):
        for slot, value in enumerate(drinks):
            if isinstance(value, Mapping):
                result.append(
                    f"drink-slot:{slot}:{value.get('_id', value.get('id', 'unknown'))}"
                )
    # v4 derives current effects from the active native reference graph. Keep
    # the historical TurnStart summary only for artifacts trained on it;
    # otherwise its presence in old samples and absence in DLL observations
    # creates a training/inference mismatch despite identical current state.
    statuses = None if current_state_features else state_before.get("currentTurnStartStatusList")
    if isinstance(statuses, list):
        result.extend(f"status:{value}" for value in statuses if isinstance(value, str))
    status_data = None if current_state_features else state_before.get("currentTurnStartStatusEffectDataList")
    if isinstance(status_data, list):
        for value in status_data:
            if not isinstance(value, Mapping):
                continue
            result.append(
                "|".join(
                    (
                        "status-data",
                        f"type:{value.get('type', 'unknown')}",
                        f"value:{value.get('value', 'unknown')}",
                        f"turn:{value.get('turn', 'unknown')}",
                    )
                )
            )
    items = state_before.get("itemList")
    if isinstance(items, list):
        for value in items:
            if not isinstance(value, Mapping):
                continue
            result.append(
                "|".join(
                    (
                        "item",
                        f"id:{value.get('_id', value.get('id', 'unknown'))}",
                        f"fire:{value.get('_fireCount', value.get('fireCount', 'unknown'))}",
                        f"reaction:{value.get('_reactionCount', value.get('reactionCount', 'unknown'))}",
                    )
                )
            )
    result.extend(f"candidate-present:{value}" for value in sorted(candidate_semantics))
    if semantic_features is not None:
        result.append("card-feature-schema:" + semantic_features.schema)
        for slot, runtime in enumerate(hand if isinstance(hand, list) else ()):
            if not isinstance(runtime, Mapping):
                continue
            data = _mapping(runtime.get("_cardData", runtime.get("cardData")))
            card_id = str(data.get("_id", data.get("id", "unknown")))
            upgrade = data.get("_upgradeCount", data.get("upgradeCount", 0))
            # Candidate vectors carry the full effect graph.  Context keeps
            # the hand's effect families so hundreds of constant graph fields
            # cannot drown the observable turn/buff/resource state.
            result.extend(f"hand-semantic:{slot}:{token}" for token in
                          semantic_features.card_tokens(card_id, upgrade, runtime)
                          if token.startswith("semantic:effect-type:"))
        result.extend(semantic_features.context_tokens(state_before))
    return result


def load_exact_main_action_dataset(
    stages_path: Path,
    *,
    context_buckets: int = 4096,
    candidate_buckets: int = 65536,
    semantic_features: Any | None = None,
    shared_primary_feature_contract: Mapping[str, Any] | None = None,
) -> tuple[OuterDataset, dict[str, object]]:
    """Load only frozen exact main actions; state_after and reward stay out of features."""

    context_rows: list[list[int]] = []
    candidate_rows: list[list[list[int]]] = []
    targets: list[int] = []
    splits: list[int] = []
    flows: list[str] = []
    candidate_keys: list[tuple[str, ...]] = []
    chosen_keys: list[str] = []
    trajectories: list[str] = []
    trajectory_splits: dict[str, int] = {}
    action_kinds: Counter[str] = Counter()
    stage_count = 0
    shared_gaps: Counter[str] = Counter()
    shared_cells: Counter[str] = Counter()
    if shared_primary_feature_contract is not None:
        from .shared_bc_features import validate_shared_primary_contract
        validate_shared_primary_contract(shared_primary_feature_contract, semantic_features)
    with Path(stages_path).open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            stage_row = json.loads(line)
            if not isinstance(stage_row, Mapping) or stage_row.get("schema") != (
                "gkms.offline-rl-stage.v1"
            ):
                raise ValueError(f"invalid exact BC stage at line {line_number}")
            flow = stage_row.get("flow")
            stage = stage_row.get("stage")
            trajectory = stage_row.get("trajectory_id")
            split = stage_row.get("split")
            transitions = stage_row.get("transitions")
            if not (
                isinstance(flow, str)
                and flow
                and isinstance(stage, str)
                and stage
                and isinstance(trajectory, str)
                and trajectory
                and split in SPLIT_CODES
                and isinstance(transitions, list)
                and transitions
            ):
                raise ValueError(f"incomplete exact BC stage at line {line_number}")
            split_code = SPLIT_CODES[str(split)]
            group_trajectory = trajectory
            if shared_primary_feature_contract is not None:
                group_trajectory = stage_row.get('whole_trajectory_id')
                if not isinstance(group_trajectory, str) or not group_trajectory:
                    raise ValueError('Shared BC stage requires its original whole-cultivation grouping identity')
            previous_split = trajectory_splits.setdefault(group_trajectory, split_code)
            if previous_split != split_code:
                raise ValueError("one exact BC trajectory crosses dataset splits")
            stage_count += 1
            for transition in transitions:
                if not isinstance(transition, Mapping):
                    raise ValueError("exact BC transition must be an object")
                state_before = transition.get("state_before")
                action = transition.get("action")
                legal_candidates = transition.get("legal_candidates")
                if not (
                    isinstance(state_before, Mapping)
                    and isinstance(action, Mapping)
                    and isinstance(legal_candidates, list)
                    and legal_candidates
                ):
                    raise ValueError("exact BC transition lacks state/action/legal set")
                if shared_primary_feature_contract is not None:
                    from .shared_bc_features import shared_primary_candidate_index
                    target = shared_primary_candidate_index(state_before, action, legal_candidates, shared_primary_feature_contract)
                else:
                    target = strict_exact_candidate_index(state_before, action, legal_candidates)
                if shared_primary_feature_contract is not None:
                    from .shared_bc_features import encode_shared_bc_primary, PRIMARY_SCOPE
                    encoded = encode_shared_bc_primary(state_before=state_before, legal_candidates=legal_candidates,
                        flow=flow, stage=stage, native_mask_complete=True, candidate_scope=PRIMARY_SCOPE,
                        features=semantic_features, contract=shared_primary_feature_contract)
                    semantics = encoded.candidate_semantics
                    candidate_fields = encoded.candidate_tokens
                    tokens = encoded.context_tokens
                    shared_gaps.update(encoded.coverage['semantic_gap_tokens'])
                    shared_gaps.update(encoded.coverage['contract_gaps'])
                    shared_cells[flow + '|' + stage] += 1
                else:
                    semantics = exact_candidate_semantics(state_before, legal_candidates)
                if len(set(semantics)) != len(semantics):
                    raise ValueError("exact BC candidate semantics are not unique")
                if shared_primary_feature_contract is None:
                    candidate_fields = exact_candidate_field_tokens(
                        state_before, legal_candidates, semantic_features=semantic_features,
                    )
                    tokens = build_exact_main_action_context_tokens(
                        state_before=state_before,
                        flow=flow,
                        stage=stage,
                        candidate_semantics=_exact_candidate_context_semantics(
                            candidate_fields
                        ),
                        semantic_features=semantic_features,
                    )
                context_rows.append(
                    [_hash_index(value, context_buckets) for value in tokens]
                )
                hashed_candidates = [
                    list(
                        dict.fromkeys(
                            _hash_index(
                                f"exact-candidate-field:{field}",
                                candidate_buckets,
                            )
                            for field in fields
                        )
                    )
                    for fields in candidate_fields
                ]
                if len({tuple(value) for value in hashed_candidates}) != len(
                    hashed_candidates
                ):
                    raise ValueError("exact BC candidate hash collision")
                candidate_rows.append(hashed_candidates)
                targets.append(target)
                splits.append(split_code)
                flows.append(flow)
                candidate_keys.append(semantics)
                chosen_keys.append(semantics[target])
                trajectories.append(group_trajectory)
                action_kinds[
                    str(action.get("kind", action.get("action_type", "unknown")))
                ] += 1
    if not targets:
        raise ValueError("exact main-action BC dataset is empty")
    context_indices, context_mask = _pad_token_rows(context_rows)
    candidate_indices, candidate_token_mask, candidate_mask = (
        _pad_candidate_token_rows(candidate_rows)
    )
    flow_names = tuple(sorted(set(flows)))
    flow_index = {value: index for index, value in enumerate(flow_names)}
    flow_ids = np.asarray([flow_index[value] for value in flows], dtype=np.int16)
    split_values = np.asarray(splits, dtype=np.int8)
    trajectory_counts = Counter(
        (split, trajectory)
        for split, trajectory in zip(splits, trajectories, strict=True)
    )
    flow_counts = Counter(
        (split, int(flow_id))
        for split, flow_id in zip(splits, flow_ids.tolist(), strict=True)
    )
    weights = np.asarray(
        [
            1.0
            / trajectory_counts[(split, trajectory)]
            / math.sqrt(flow_counts[(split, int(flow_id))])
            for split, trajectory, flow_id in zip(
                splits, trajectories, flow_ids.tolist(), strict=True
            )
        ],
        dtype=np.float32,
    )
    train_mean = float(weights[split_values == SPLIT_CODES["train"]].mean())
    if train_mean > 0:
        weights /= train_mean
    dataset = OuterDataset(
        context_indices=context_indices,
        context_mask=context_mask,
        candidate_indices=candidate_indices,
        candidate_token_mask=candidate_token_mask,
        candidate_mask=candidate_mask,
        targets=np.asarray(targets, dtype=np.int16),
        splits=split_values,
        weights=weights,
        flow_ids=flow_ids,
        flow_names=flow_names,
        candidate_keys=tuple(candidate_keys),
        chosen_keys=tuple(chosen_keys),
        trajectory_ids=tuple(trajectories),
        context_buckets=context_buckets,
        candidate_buckets=candidate_buckets,
    )
    return dataset, {
        "stage_count": stage_count,
        "trajectory_count": len(trajectory_splits),
        "action_kind_counts": dict(sorted(action_kinds.items())),
        "legal_binding_count": dataset.size,
        "candidate_semantic_collision_rows": 0,
        "candidate_hash_collision_rows": 0,
        "candidate_encoding": (shared_primary_feature_contract['encoding'] if shared_primary_feature_contract is not None
            else semantic_features.encoding if semantic_features is not None else "field-token-bag-v1"),
        **({"shared_primary_feature_contract": dict(shared_primary_feature_contract),
            "shared_feature_gap_counts": dict(shared_gaps), "shared_flow_stage_row_counts": dict(shared_cells),
            "new_training_admitted": False}
           if shared_primary_feature_contract is not None else {}),
    }


def load_unified_state_dataset(
    labels_path: Path,
    *,
    context_buckets: int = 4096,
    candidate_buckets: int = 1024,
) -> tuple[OuterDataset, int]:
    context_rows: list[list[int]] = []
    candidate_rows: list[list[list[int]]] = []
    targets: list[int] = []
    splits: list[int] = []
    flows: list[str] = []
    candidate_keys: list[tuple[str, ...]] = []
    chosen_keys: list[str] = []
    trajectories: list[str] = []
    semantic_collision_rows = 0
    for line_number, line in enumerate(
        Path(labels_path).open("r", encoding="utf-8-sig"), start=1
    ):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, Mapping) or row.get("schema") != LABEL_SCHEMA:
            raise ValueError(f"invalid canonical label at line {line_number}")
        schema = row.get("source_schema")
        legal = _mapping(row.get("legal"))
        transition = _mapping(row.get("transition"))
        if not (
            _mapping(row.get("dedupe")).get("status") == "canonical"
            and schema
            in {"gkms.nia-inner-transition.v1", "gkms.nia-legal-decision.v1"}
            and legal.get("candidate_scope") == "unified"
            and legal.get("complete") is True
            and legal.get("exact") is True
            and legal.get("chosen_member") is True
            and isinstance(transition.get("state_before"), Mapping)
        ):
            continue
        candidates = legal.get("candidates")
        action = transition.get("action")
        if not isinstance(candidates, list) or action is None:
            continue
        matches = match_legal_candidate_indices(action, candidates)
        if len(matches) != 1:
            raise ValueError(f"unified action binding failed at line {line_number}")
        state_cards = _state_card_semantics(transition.get("state_before"))
        semantics = tuple(
            _unified_candidate_semantic(value, state_cards) for value in candidates
        )
        if len(set(semantics)) != len(semantics):
            semantic_collision_rows += 1
        scope = _mapping(row.get("scope"))
        flow = "|".join(
            str(scope.get(key))
            for key in ("produce_id", "plan_type", "exam_effect_type")
        )
        split = _mapping(row.get("split")).get("assignment")
        if split not in SPLIT_CODES:
            raise ValueError(f"unified split is invalid at line {line_number}")
        trajectory = _mapping(row.get("identity")).get("trajectory_id")
        if not isinstance(trajectory, str) or not trajectory:
            raise ValueError(f"unified trajectory is invalid at line {line_number}")
        tokens = _unified_context_tokens(row)
        tokens.extend(f"candidate-present:{value}" for value in sorted(semantics))
        context_rows.append(
            [_hash_index(value, context_buckets) for value in tokens]
        )
        candidate_rows.append(
            [
                [_hash_index(f"unified-candidate:{value}", candidate_buckets)]
                for value in semantics
            ]
        )
        targets.append(matches[0])
        splits.append(SPLIT_CODES[str(split)])
        flows.append(flow)
        candidate_keys.append(semantics)
        chosen_keys.append(semantics[matches[0]])
        trajectories.append(trajectory)
    if not targets:
        raise ValueError("unified state BC dataset is empty")
    context_indices, context_mask = _pad_token_rows(context_rows)
    candidate_indices, candidate_token_mask, candidate_mask = (
        _pad_candidate_token_rows(candidate_rows)
    )
    flow_names = tuple(sorted(set(flows)))
    flow_index = {value: index for index, value in enumerate(flow_names)}
    dataset = OuterDataset(
        context_indices=context_indices,
        context_mask=context_mask,
        candidate_indices=candidate_indices,
        candidate_token_mask=candidate_token_mask,
        candidate_mask=candidate_mask,
        targets=np.asarray(targets, dtype=np.int16),
        splits=np.asarray(splits, dtype=np.int8),
        weights=np.ones(len(targets), dtype=np.float32),
        flow_ids=np.asarray([flow_index[value] for value in flows], dtype=np.int16),
        flow_names=flow_names,
        candidate_keys=tuple(candidate_keys),
        chosen_keys=tuple(chosen_keys),
        trajectory_ids=tuple(trajectories),
        context_buckets=context_buckets,
        candidate_buckets=candidate_buckets,
    )
    return dataset, semantic_collision_rows


def _xavier(rng: np.random.Generator, shape: tuple[int, ...]) -> np.ndarray:
    if len(shape) < 2:
        scale = 0.01
    else:
        scale = math.sqrt(2.0 / max(1, shape[0] + shape[1]))
    return rng.normal(0.0, scale, size=shape).astype(np.float32)


def _init_outer_parameters(
    *,
    context_buckets: int,
    candidate_buckets: int,
    embedding_dim: int,
    hidden1: int,
    hidden2: int,
    seed: int,
) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(seed)
    return {
        "context_embedding": rng.normal(
            0.0, 0.02, size=(context_buckets, embedding_dim)
        ).astype(np.float32),
        "candidate_embedding": rng.normal(
            0.0, 0.02, size=(candidate_buckets, embedding_dim)
        ).astype(np.float32),
        "w1": _xavier(rng, (embedding_dim * 3, hidden1)),
        "b1": np.zeros(hidden1, dtype=np.float32),
        "w2": _xavier(rng, (hidden1, hidden2)),
        "b2": np.zeros(hidden2, dtype=np.float32),
        "w3": _xavier(rng, (hidden2, 1)).reshape(hidden2),
        "b3": np.zeros(1, dtype=np.float32),
    }


def _context_vector(
    embedding: np.ndarray,
    indices: np.ndarray,
    mask: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    counts = np.maximum(mask.sum(axis=1, keepdims=True), 1.0)
    denominator = np.sqrt(counts).astype(np.float32)
    values = embedding[indices] * mask[..., None]
    return values.sum(axis=1) / denominator, denominator


def _outer_forward(
    parameters: Mapping[str, np.ndarray],
    context_indices: np.ndarray,
    context_mask: np.ndarray,
    candidate_indices: np.ndarray,
    candidate_token_mask: np.ndarray,
    candidate_mask: np.ndarray,
    *,
    cache: bool,
) -> tuple[np.ndarray, dict[str, np.ndarray] | None]:
    context, denominator = _context_vector(
        parameters["context_embedding"], context_indices, context_mask
    )
    candidate_denominator = np.sqrt(
        np.maximum(candidate_token_mask.sum(axis=2, keepdims=True), 1.0)
    ).astype(np.float32)
    candidate_values = (
        parameters["candidate_embedding"][candidate_indices]
        * candidate_token_mask[..., None]
    )
    candidates = candidate_values.sum(axis=2) / candidate_denominator
    repeated = np.broadcast_to(context[:, None, :], candidates.shape)
    features = np.concatenate((repeated, candidates, repeated * candidates), axis=2)
    z1 = features @ parameters["w1"] + parameters["b1"]
    h1 = np.maximum(z1, 0.0)
    z2 = h1 @ parameters["w2"] + parameters["b2"]
    h2 = np.maximum(z2, 0.0)
    scores = h2 @ parameters["w3"] + float(parameters["b3"][0])
    scores = np.where(candidate_mask > 0, scores, -1.0e9)
    maximum = scores.max(axis=1, keepdims=True)
    exp = np.exp(scores - maximum) * candidate_mask
    probabilities = exp / np.maximum(exp.sum(axis=1, keepdims=True), 1.0e-12)
    if not cache:
        return probabilities, None
    return probabilities, {
        "context": context,
        "denominator": denominator,
        "candidates": candidates,
        "candidate_denominator": candidate_denominator,
        "candidate_token_mask": candidate_token_mask,
        "features": features,
        "z1": z1,
        "h1": h1,
        "z2": z2,
        "h2": h2,
    }


def _outer_gradients(
    parameters: Mapping[str, np.ndarray],
    batch: tuple[np.ndarray, ...],
) -> tuple[float, dict[str, np.ndarray]]:
    (
        context_indices,
        context_mask,
        candidate_indices,
        candidate_token_mask,
        candidate_mask,
        targets,
        weights,
    ) = batch
    probabilities, cache = _outer_forward(
        parameters,
        context_indices,
        context_mask,
        candidate_indices,
        candidate_token_mask,
        candidate_mask,
        cache=True,
    )
    assert cache is not None
    selected = probabilities[np.arange(targets.shape[0]), targets]
    weight_sum = max(float(weights.sum()), 1.0e-12)
    loss = float((-np.log(np.maximum(selected, 1.0e-12)) * weights).sum() / weight_sum)
    dscores = probabilities.copy()
    dscores[np.arange(targets.shape[0]), targets] -= 1.0
    dscores *= (weights / weight_sum)[:, None]
    dscores *= candidate_mask

    h2 = cache["h2"]
    gradients: dict[str, np.ndarray] = {}
    gradients["w3"] = np.einsum("bmh,bm->h", h2, dscores).astype(np.float32)
    gradients["b3"] = np.asarray([dscores.sum()], dtype=np.float32)
    dh2 = dscores[..., None] * parameters["w3"]
    dz2 = dh2 * (cache["z2"] > 0)
    gradients["w2"] = np.einsum("bmi,bmj->ij", cache["h1"], dz2).astype(
        np.float32
    )
    gradients["b2"] = dz2.sum(axis=(0, 1)).astype(np.float32)
    dh1 = dz2 @ parameters["w2"].T
    dz1 = dh1 * (cache["z1"] > 0)
    gradients["w1"] = np.einsum("bmi,bmj->ij", cache["features"], dz1).astype(
        np.float32
    )
    gradients["b1"] = dz1.sum(axis=(0, 1)).astype(np.float32)
    dfeatures = dz1 @ parameters["w1"].T
    embedding_dim = cache["context"].shape[1]
    dcontext = dfeatures[:, :, :embedding_dim]
    dcandidate = dfeatures[:, :, embedding_dim : embedding_dim * 2]
    dproduct = dfeatures[:, :, embedding_dim * 2 :]
    dcontext = (dcontext + dproduct * cache["candidates"]) * candidate_mask[..., None]
    dcandidate = (
        dcandidate + dproduct * cache["context"][:, None, :]
    ) * candidate_mask[..., None]
    dcontext = dcontext.sum(axis=1)

    context_gradient = np.zeros_like(parameters["context_embedding"])
    per_token = (
        dcontext[:, None, :] / cache["denominator"][:, :, None]
    ) * context_mask[..., None]
    np.add.at(
        context_gradient,
        context_indices.reshape(-1),
        per_token.reshape(-1, embedding_dim),
    )
    candidate_gradient = np.zeros_like(parameters["candidate_embedding"])
    per_candidate_token = (
        dcandidate[:, :, None, :]
        / cache["candidate_denominator"][:, :, :, None]
    ) * cache["candidate_token_mask"][..., None]
    np.add.at(
        candidate_gradient,
        candidate_indices.reshape(-1),
        per_candidate_token.reshape(-1, embedding_dim),
    )
    gradients["context_embedding"] = context_gradient
    gradients["candidate_embedding"] = candidate_gradient
    return loss, gradients


class _Adam:
    def __init__(self, parameters: Mapping[str, np.ndarray], learning_rate: float):
        self.learning_rate = float(learning_rate)
        self.first = {key: np.zeros_like(value) for key, value in parameters.items()}
        self.second = {key: np.zeros_like(value) for key, value in parameters.items()}
        self.step = 0

    def update(
        self,
        parameters: dict[str, np.ndarray],
        gradients: Mapping[str, np.ndarray],
        *,
        clip_norm: float = 5.0,
    ) -> None:
        self.step += 1
        squared = sum(float(np.square(value, dtype=np.float64).sum()) for value in gradients.values())
        norm = math.sqrt(max(squared, 0.0))
        scale = min(1.0, clip_norm / max(norm, 1.0e-12))
        beta1, beta2 = 0.9, 0.999
        for key, parameter in parameters.items():
            gradient = gradients[key] * scale
            self.first[key] = beta1 * self.first[key] + (1.0 - beta1) * gradient
            self.second[key] = beta2 * self.second[key] + (1.0 - beta2) * np.square(
                gradient
            )
            first_hat = self.first[key] / (1.0 - beta1**self.step)
            second_hat = self.second[key] / (1.0 - beta2**self.step)
            parameter -= self.learning_rate * first_hat / (
                np.sqrt(second_hat) + 1.0e-8
            )


def _outer_predictions(
    parameters: Mapping[str, np.ndarray],
    dataset: OuterDataset,
    indices: np.ndarray,
    *,
    batch_size: int = 4096,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    predictions: list[np.ndarray] = []
    nll_values: list[np.ndarray] = []
    reciprocal: list[np.ndarray] = []
    for start in range(0, indices.shape[0], batch_size):
        batch_indices = indices[start : start + batch_size]
        probabilities, _cache = _outer_forward(
            parameters,
            dataset.context_indices[batch_indices],
            dataset.context_mask[batch_indices],
            dataset.candidate_indices[batch_indices],
            dataset.candidate_token_mask[batch_indices],
            dataset.candidate_mask[batch_indices],
            cache=False,
        )
        targets = dataset.targets[batch_indices]
        predictions.append(probabilities.argmax(axis=1))
        selected = probabilities[np.arange(targets.shape[0]), targets]
        nll_values.append(-np.log(np.maximum(selected, 1.0e-12)))
        order = np.argsort(-probabilities, axis=1)
        ranks = (order == targets[:, None]).argmax(axis=1) + 1
        reciprocal.append(1.0 / ranks)
    return (
        np.concatenate(predictions),
        np.concatenate(nll_values),
        np.concatenate(reciprocal),
    )


def _macro_flow_accuracy(
    predictions: np.ndarray,
    targets: np.ndarray,
    flow_ids: np.ndarray,
    flow_names: Sequence[str],
) -> tuple[float, dict[str, float]]:
    values: dict[str, float] = {}
    for index, name in enumerate(flow_names):
        mask = flow_ids == index
        if mask.any():
            values[name] = float((predictions[mask] == targets[mask]).mean())
    return (float(np.mean(tuple(values.values()))) if values else 0.0, values)


def _outer_frequency_baseline(
    dataset: OuterDataset,
    train_indices: np.ndarray,
    eval_indices: np.ndarray,
) -> tuple[np.ndarray, float, dict[str, float]]:
    frequency: Counter[tuple[int, str]] = Counter()
    for index in train_indices.tolist():
        frequency[(int(dataset.flow_ids[index]), dataset.chosen_keys[index])] += 1
    predictions: list[int] = []
    for index in eval_indices.tolist():
        flow = int(dataset.flow_ids[index])
        candidates = dataset.candidate_keys[index]
        predictions.append(
            max(
                range(len(candidates)),
                key=lambda position: (
                    frequency[(flow, candidates[position])],
                    candidates[position],
                ),
            )
        )
    result = np.asarray(predictions, dtype=np.int16)
    macro, per_flow = _macro_flow_accuracy(
        result,
        dataset.targets[eval_indices],
        dataset.flow_ids[eval_indices],
        dataset.flow_names,
    )
    return result, macro, per_flow


def _maximum_per_flow_drop(
    measured: Mapping[str, object], reference: Mapping[str, object]
) -> float:
    """Return the largest top-1 regression against a per-flow reference.

    This is shared by exact-model checkpoint selection and final acceptance so
    training cannot select a lower-NLL checkpoint that is already known to
    violate the same flow-balanced validation contract used by the artifact.
    """

    measured_flows = _mapping(measured.get("per_flow_top1"))
    reference_flows = _mapping(reference.get("per_flow_top1"))
    return max(
        (
            float(reference_flows.get(flow, 0.0)) - float(value)
            for flow, value in measured_flows.items()
        ),
        default=0.0,
    )


def evaluate_outer_model(
    parameters: Mapping[str, np.ndarray],
    dataset: OuterDataset,
    split_name: str,
) -> dict[str, object]:
    code = SPLIT_CODES[split_name]
    indices = np.flatnonzero(dataset.splits == code)
    predictions, nll, reciprocal = _outer_predictions(parameters, dataset, indices)
    targets = dataset.targets[indices]
    macro, per_flow = _macro_flow_accuracy(
        predictions,
        targets,
        dataset.flow_ids[indices],
        dataset.flow_names,
    )
    top1 = float((predictions == targets).mean()) if targets.size else 0.0
    top3_hits = []
    for start in range(0, indices.shape[0], 4096):
        batch_indices = indices[start : start + 4096]
        probabilities, _ = _outer_forward(
            parameters,
            dataset.context_indices[batch_indices],
            dataset.context_mask[batch_indices],
            dataset.candidate_indices[batch_indices],
            dataset.candidate_token_mask[batch_indices],
            dataset.candidate_mask[batch_indices],
            cache=False,
        )
        targets_batch = dataset.targets[batch_indices]
        top = np.argsort(-probabilities, axis=1)[:, :3]
        top3_hits.append((top == targets_batch[:, None]).any(axis=1))
    return {
        "split": split_name,
        "count": int(indices.shape[0]),
        "top1": top1,
        "top3": float(np.concatenate(top3_hits).mean()) if top3_hits else 0.0,
        "macro_flow_top1": macro,
        "per_flow_top1": per_flow,
        "nll": float(nll.mean()) if nll.size else None,
        "mrr": float(reciprocal.mean()) if reciprocal.size else None,
        "illegal_rate": 0.0,
    }


def train_outer_model(
    dataset: OuterDataset,
    *,
    epochs: int = 20,
    batch_size: int = 512,
    learning_rate: float = 0.001,
    seed: int = 20260828,
    patience: int = 5,
    maximum_validation_per_flow_drop_pp: float | None = None,
    training_callback: Any | None = None,
) -> tuple[dict[str, np.ndarray], dict[str, object]]:
    parameters = _init_outer_parameters(
        context_buckets=dataset.context_buckets,
        candidate_buckets=dataset.candidate_buckets,
        embedding_dim=32,
        hidden1=128,
        hidden2=64,
        seed=seed,
    )
    optimizer = _Adam(parameters, learning_rate)
    rng = np.random.default_rng(seed)
    train_indices = np.flatnonzero(dataset.splits == SPLIT_CODES["train"])
    validation_indices = np.flatnonzero(
        dataset.splits == SPLIT_CODES["validation"]
    )
    test_indices = np.flatnonzero(dataset.splits == SPLIT_CODES["test"])
    validation_baseline = _outer_frequency_baseline(
        dataset, train_indices, validation_indices
    )
    test_baseline = _outer_frequency_baseline(
        dataset, train_indices, test_indices
    )
    best_parameters = {key: value.copy() for key, value in parameters.items()}
    best_nll = float("inf")
    constrained_parameters: dict[str, np.ndarray] | None = None
    constrained_nll = float("inf")
    constrained_epoch: int | None = None
    stale = 0
    history: list[dict[str, object]] = []
    for epoch in range(1, epochs + 1):
        if training_callback is not None:
            training_callback({"phase": "bc", "epoch": epoch, "epochs": epochs})
        shuffled = rng.permutation(train_indices)
        losses: list[float] = []
        for start in range(0, shuffled.shape[0], batch_size):
            indices = shuffled[start : start + batch_size]
            loss, gradients = _outer_gradients(
                parameters,
                (
                    dataset.context_indices[indices],
                    dataset.context_mask[indices],
                    dataset.candidate_indices[indices],
                    dataset.candidate_token_mask[indices],
                    dataset.candidate_mask[indices],
                    dataset.targets[indices],
                    dataset.weights[indices],
                ),
            )
            optimizer.update(parameters, gradients)
            losses.append(loss)
        validation = evaluate_outer_model(parameters, dataset, "validation")
        validation_nll = float(validation["nll"] or float("inf"))
        validation_drop_pp = 100.0 * _maximum_per_flow_drop(
            validation,
            {
                "per_flow_top1": validation_baseline[2],
            },
        )
        checkpoint_eligible = (
            maximum_validation_per_flow_drop_pp is None
            or validation_drop_pp <= maximum_validation_per_flow_drop_pp
        )
        history.append(
            {
                "epoch": epoch,
                "train_loss": float(np.mean(losses)),
                "validation_nll": validation_nll,
                "validation_macro_top1": validation["macro_flow_top1"],
                "validation_maximum_per_flow_drop_pp": validation_drop_pp,
                "checkpoint_eligible": checkpoint_eligible,
            }
        )
        print(
            f"outer epoch={epoch} train_loss={np.mean(losses):.6f} "
            f"val_nll={validation_nll:.6f} "
            f"val_macro={float(validation['macro_flow_top1']):.4f} "
            f"val_flow_drop_pp={validation_drop_pp:.4f}",
            flush=True,
        )
        if checkpoint_eligible and validation_nll + 1.0e-6 < constrained_nll:
            constrained_nll = validation_nll
            constrained_parameters = {
                key: value.copy() for key, value in parameters.items()
            }
            constrained_epoch = epoch
        if validation_nll + 1.0e-6 < best_nll:
            best_nll = validation_nll
            best_parameters = {key: value.copy() for key, value in parameters.items()}
            stale = 0
        else:
            stale += 1
            if stale >= patience:
                break
    selection_mode = "minimum-validation-nll"
    if (
        maximum_validation_per_flow_drop_pp is not None
        and constrained_parameters is not None
    ):
        best_parameters = constrained_parameters
        selection_mode = "minimum-validation-nll-with-per-flow-non-regression"
    metrics = {
        "history": history,
        "checkpoint_selection": {
            "mode": selection_mode,
            "maximum_validation_per_flow_drop_pp": (
                maximum_validation_per_flow_drop_pp
            ),
            "selected_epoch": (
                constrained_epoch if constrained_parameters is not None else None
            ),
            "eligible_checkpoint_found": constrained_parameters is not None,
        },
        "train": evaluate_outer_model(best_parameters, dataset, "train"),
        "validation": evaluate_outer_model(
            best_parameters, dataset, "validation"
        ),
        "test": evaluate_outer_model(best_parameters, dataset, "test"),
        "validation_frequency_baseline": {
            "macro_flow_top1": validation_baseline[1],
            "per_flow_top1": validation_baseline[2],
        },
        "test_frequency_baseline": {
            "macro_flow_top1": test_baseline[1],
            "per_flow_top1": test_baseline[2],
        },
    }
    return best_parameters, metrics


def train_unified_overfit_smoke(
    dataset: OuterDataset,
    *,
    epochs: int = 400,
    learning_rate: float = 0.005,
    seed: int = 20260828,
) -> tuple[dict[str, np.ndarray], dict[str, object]]:
    parameters = _init_outer_parameters(
        context_buckets=dataset.context_buckets,
        candidate_buckets=dataset.candidate_buckets,
        embedding_dim=32,
        hidden1=128,
        hidden2=64,
        seed=seed,
    )
    optimizer = _Adam(parameters, learning_rate)
    indices = np.arange(dataset.size)
    history: list[dict[str, object]] = []
    for epoch in range(1, epochs + 1):
        loss, gradients = _outer_gradients(
            parameters,
            (
                dataset.context_indices[indices],
                dataset.context_mask[indices],
                dataset.candidate_indices[indices],
                dataset.candidate_token_mask[indices],
                dataset.candidate_mask[indices],
                dataset.targets[indices],
                dataset.weights[indices],
            ),
        )
        optimizer.update(parameters, gradients)
        probabilities, _ = _outer_forward(
            parameters,
            dataset.context_indices,
            dataset.context_mask,
            dataset.candidate_indices,
            dataset.candidate_token_mask,
            dataset.candidate_mask,
            cache=False,
        )
        predictions = probabilities.argmax(axis=1)
        accuracy = float((predictions == dataset.targets).mean())
        if epoch == 1 or epoch % 25 == 0 or accuracy == 1.0:
            history.append({"epoch": epoch, "loss": loss, "accuracy": accuracy})
        if accuracy == 1.0 and loss < 0.02:
            break

    original, _ = _outer_forward(
        parameters,
        dataset.context_indices,
        dataset.context_mask,
        dataset.candidate_indices,
        dataset.candidate_token_mask,
        dataset.candidate_mask,
        cache=False,
    )
    permuted_indices = dataset.candidate_indices.copy()
    permuted_token_mask = dataset.candidate_token_mask.copy()
    permuted_mask = dataset.candidate_mask.copy()
    inverse_positions: list[list[int]] = []
    for row_index in range(dataset.size):
        count = int(dataset.candidate_mask[row_index].sum())
        order = list(reversed(range(count)))
        permuted_indices[row_index, :count] = dataset.candidate_indices[
            row_index, order
        ]
        permuted_token_mask[row_index, :count] = dataset.candidate_token_mask[
            row_index, order
        ]
        permuted_mask[row_index, :count] = 1.0
        inverse_positions.append(order)
    permuted, _ = _outer_forward(
        parameters,
        dataset.context_indices,
        dataset.context_mask,
        permuted_indices,
        permuted_token_mask,
        permuted_mask,
        cache=False,
    )
    maximum_delta = 0.0
    for row_index, order in enumerate(inverse_positions):
        restored = np.zeros_like(original[row_index])
        for new_position, old_position in enumerate(order):
            restored[old_position] = permuted[row_index, new_position]
        maximum_delta = max(
            maximum_delta,
            float(np.max(np.abs(restored - original[row_index]))),
        )
    predictions = original.argmax(axis=1)
    return parameters, {
        "history": history,
        "epochs_ran": epoch,
        "overfit_accuracy": float((predictions == dataset.targets).mean()),
        "permutation_max_probability_delta": maximum_delta,
        "permutation_invariant": maximum_delta <= 1.0e-6,
    }


def _replay_action_token(action: Mapping[str, Any]) -> str:
    action_type = action.get("action_type")
    if not isinstance(action_type, str) or not action_type:
        action_type = "unknown"
    indexes = action.get("indexes")
    if not isinstance(indexes, list):
        indexes = []
    ordered = ",".join(str(value) for value in indexes)
    return f"{action_type}|{ordered}"


def _replay_static_tokens(row: Mapping[str, Any]) -> list[str]:
    freeze = _mapping(row.get("freeze"))
    replay_training = _mapping(row.get("replay_training"))
    observable_static = replay_training.get("input_feature_schema") == "gkms.replay-native-observable-static.v1"
    result = [
        "bias",
        f"produce:{row.get('produce_id')}",
        f"plan:{row.get('plan_type')}",
        f"effect:{row.get('exam_effect_type')}",
        f"stage:{row.get('step_type')}",
        f"character:{row.get('character_id')}",
        f"idol:{row.get('idol_card_id')}",
        f"flow:{replay_training.get('flow', freeze.get('flow'))}",
        f"deck-count:{len(row.get('produce_cards', [])) if isinstance(row.get('produce_cards'), list) else 0}",
        f"drink-count:{len(row.get('produce_drink_ids', [])) if isinstance(row.get('produce_drink_ids'), list) else 0}",
        f"support-count:{len(row.get('support_cards', [])) if isinstance(row.get('support_cards'), list) else 0}",
        f"memory-count:{len(row.get('memory_loadout', [])) if isinstance(row.get('memory_loadout'), list) else 0}",
    ]
    if observable_static:
        result = [token for token in result if not token.startswith(("deck-count:", "drink-count:", "support-count:", "memory-count:"))]
    for key in (
        "stamina",
        "max_stamina",
        "vocal",
        "dance",
        "visual",
        "vocal_bonus_permil",
        "dance_bonus_permil",
        "visual_bonus_permil",
        "limit_turn",
    ):
        if observable_static and key == "stamina":
            continue
        value = row.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            result.append(_numeric_token(key, value))
    return result


def build_replay_context_tokens(row: Mapping[str, Any], action_prefix: Sequence[Mapping[str, Any]]) -> list[str]:
    """Shared history-BC input: static context and already observed actions only."""
    ordinal = len(action_prefix)
    tokens = [*_replay_static_tokens(row), f"order:{min(ordinal, 63)}",
        f"order-bucket:{min(ordinal // 4, 15)}", f"prefix-length:{ordinal}"]
    for offset, action in enumerate(reversed(action_prefix[-3:]), start=1):
        tokens.append(f"prev-{offset}:{_replay_action_token(action)}")
    return tokens


@dataclass(frozen=True, slots=True)
class ReplayDataset:
    context_indices: np.ndarray
    context_mask: np.ndarray
    family_targets: np.ndarray
    selector_targets: np.ndarray
    splits: np.ndarray
    weights: np.ndarray
    flow_ids: np.ndarray
    flow_names: tuple[str, ...]
    target_tokens: tuple[str, ...]
    family_tokens: tuple[str, ...]
    selector_tokens: tuple[str, ...]
    allowed_selector_mask: np.ndarray
    episode_ids: tuple[str, ...]
    context_buckets: int

    @property
    def size(self) -> int:
        return int(self.family_targets.shape[0])


def load_replay_dataset(
    episodes_path: Path,
    *,
    context_buckets: int = 4096,
    require_provenance: bool = True,
    flow_scope: Sequence[str] | None = None,
    context_builder: Any | None = None,
) -> ReplayDataset:
    if flow_scope is not None:
        from .training_spec import declared_training_scope
        allowed_flows = frozenset(declared_training_scope(flow_scope)["flows"])
    else:
        allowed_flows = None
    raw_examples: list[tuple[list[str], str, int, str, str, int]] = []
    target_values: set[str] = set()
    trajectory_splits: dict[str, int] = {}
    for line_number, line in enumerate(
        Path(episodes_path).open("r", encoding="utf-8-sig"), start=1
    ):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, Mapping):
            raise ValueError(f"invalid frozen replay row at line {line_number}")
        freeze = _mapping(row.get("freeze"))
        training = _mapping(row.get("replay_training"))
        if allowed_flows is None:
            if freeze.get("training_scope") != "five_archetype":
                continue
        else:
            actual_flow = "|".join(str(row.get(key)) for key in ("produce_id", "plan_type", "exam_effect_type"))
            if (training.get("schema") != "gkms.replay-training-row.v1"
                    or training.get("source_authority") != "official-history-action-sequence"
                    or training.get("input_feature_schema") != "gkms.replay-native-observable-static.v1"
                    or training.get("flow") != actual_flow or actual_flow not in allowed_flows):
                raise ValueError("scoped replay row lacks matching original History scope/provenance")
        if require_provenance and not row.get("sources"):
            continue
        split = _mapping(freeze.get("split")).get("assignment")
        if split not in SPLIT_CODES:
            raise ValueError(f"replay split is invalid at line {line_number}")
        flow = training.get("flow") if allowed_flows is not None else freeze.get("flow")
        episode_id = freeze.get("episode_id")
        trajectory_id = row.get("trajectory_id")
        if (
            not isinstance(flow, str)
            or not isinstance(episode_id, str)
            or not isinstance(trajectory_id, str)
            or not trajectory_id
        ):
            raise ValueError(f"replay identity is invalid at line {line_number}")
        split_code = SPLIT_CODES[str(split)]
        previous_split = trajectory_splits.setdefault(trajectory_id, split_code)
        if previous_split != split_code:
            raise ValueError("one replay trajectory crosses dataset splits")
        actions = row.get("actions")
        if not isinstance(actions, list) or not actions:
            continue
        action_count = len(actions)
        for ordinal, raw_action in enumerate(actions):
            if not isinstance(raw_action, Mapping):
                raise ValueError(f"replay action is invalid at line {line_number}")
            target = _replay_action_token(raw_action)
            tokens = (build_replay_context_tokens if context_builder is None else context_builder)(row, actions[:ordinal])
            raw_examples.append(
                (
                    tokens,
                    target,
                    split_code,
                    flow,
                    episode_id,
                    action_count,
                )
            )
            target_values.add(target)
    if not raw_examples:
        raise ValueError("replay sequence dataset is empty")
    target_tokens = tuple(sorted(target_values))
    family_tokens = tuple(sorted({value.split("|", 1)[0] for value in target_tokens}))
    selector_tokens = tuple(sorted({value.split("|", 1)[1] for value in target_tokens}))
    family_index = {value: index for index, value in enumerate(family_tokens)}
    selector_index = {value: index for index, value in enumerate(selector_tokens)}
    allowed_selector_mask = np.zeros(
        (len(family_tokens), len(selector_tokens)), dtype=np.float32
    )
    for token in target_tokens:
        family, selector = token.split("|", 1)
        allowed_selector_mask[family_index[family], selector_index[selector]] = 1.0
    flow_names = tuple(sorted({value[3] for value in raw_examples}))
    flow_index = {value: index for index, value in enumerate(flow_names)}
    token_rows = [
        [_hash_index(value, context_buckets) for value in example[0]]
        for example in raw_examples
    ]
    context_indices, context_mask = _pad_token_rows(token_rows)
    splits = np.asarray([value[2] for value in raw_examples], dtype=np.int8)
    weights = np.asarray([1.0 / value[5] for value in raw_examples], dtype=np.float32)
    train_mean = float(weights[splits == SPLIT_CODES["train"]].mean())
    weights /= max(train_mean, 1.0e-12)
    return ReplayDataset(
        context_indices=context_indices,
        context_mask=context_mask,
        family_targets=np.asarray(
            [family_index[value[1].split("|", 1)[0]] for value in raw_examples],
            dtype=np.int16,
        ),
        selector_targets=np.asarray(
            [selector_index[value[1].split("|", 1)[1]] for value in raw_examples],
            dtype=np.int16,
        ),
        splits=splits,
        weights=weights,
        flow_ids=np.asarray(
            [flow_index[value[3]] for value in raw_examples], dtype=np.int16
        ),
        flow_names=flow_names,
        target_tokens=target_tokens,
        family_tokens=family_tokens,
        selector_tokens=selector_tokens,
        allowed_selector_mask=allowed_selector_mask,
        episode_ids=tuple(value[4] for value in raw_examples),
        context_buckets=context_buckets,
    )


def _init_replay_parameters(
    dataset: ReplayDataset,
    *,
    seed: int,
) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(seed)
    return {
        "context_embedding": rng.normal(
            0.0, 0.02, size=(dataset.context_buckets, 32)
        ).astype(np.float32),
        "w1": _xavier(rng, (32, 256)),
        "b1": np.zeros(256, dtype=np.float32),
        "w2": _xavier(rng, (256, 128)),
        "b2": np.zeros(128, dtype=np.float32),
        "family_w": _xavier(rng, (128, len(dataset.family_tokens))),
        "family_b": np.zeros(len(dataset.family_tokens), dtype=np.float32),
        "selector_w": _xavier(rng, (128, len(dataset.selector_tokens))),
        "selector_b": np.zeros(len(dataset.selector_tokens), dtype=np.float32),
    }


def _replay_forward(
    parameters: Mapping[str, np.ndarray],
    context_indices: np.ndarray,
    context_mask: np.ndarray,
    *,
    cache: bool,
) -> tuple[np.ndarray, np.ndarray, dict[str, np.ndarray] | None]:
    context, denominator = _context_vector(
        parameters["context_embedding"], context_indices, context_mask
    )
    z1 = context @ parameters["w1"] + parameters["b1"]
    h1 = np.maximum(z1, 0.0)
    z2 = h1 @ parameters["w2"] + parameters["b2"]
    h2 = np.maximum(z2, 0.0)
    family_logits = h2 @ parameters["family_w"] + parameters["family_b"]
    selector_logits = h2 @ parameters["selector_w"] + parameters["selector_b"]
    family_maximum = family_logits.max(axis=1, keepdims=True)
    family_exp = np.exp(family_logits - family_maximum)
    family_probabilities = family_exp / np.maximum(
        family_exp.sum(axis=1, keepdims=True), 1.0e-12
    )
    if not cache:
        return family_probabilities, selector_logits, None
    return family_probabilities, selector_logits, {
        "context": context,
        "denominator": denominator,
        "z1": z1,
        "h1": h1,
        "z2": z2,
        "h2": h2,
    }


def _masked_selector_probabilities(
    logits: np.ndarray,
    mask: np.ndarray,
) -> np.ndarray:
    masked = np.where(mask > 0, logits, -1.0e9)
    maximum = masked.max(axis=1, keepdims=True)
    exp = np.exp(masked - maximum) * mask
    return exp / np.maximum(exp.sum(axis=1, keepdims=True), 1.0e-12)


def _replay_gradients(
    parameters: Mapping[str, np.ndarray],
    batch: tuple[np.ndarray, ...],
) -> tuple[float, dict[str, np.ndarray]]:
    (
        context_indices,
        context_mask,
        family_targets,
        selector_targets,
        weights,
        allowed_selector_mask,
    ) = batch
    family_probabilities, selector_logits, cache = _replay_forward(
        parameters, context_indices, context_mask, cache=True
    )
    assert cache is not None
    selector_mask = allowed_selector_mask[family_targets]
    selector_probabilities = _masked_selector_probabilities(
        selector_logits, selector_mask
    )
    selected_family = family_probabilities[
        np.arange(family_targets.shape[0]), family_targets
    ]
    selected_selector = selector_probabilities[
        np.arange(selector_targets.shape[0]), selector_targets
    ]
    weight_sum = max(float(weights.sum()), 1.0e-12)
    loss = float(
        (
            -np.log(np.maximum(selected_family, 1.0e-12))
            - np.log(np.maximum(selected_selector, 1.0e-12))
        ).dot(weights)
        / weight_sum
    )
    dfamily = family_probabilities.copy()
    dfamily[np.arange(family_targets.shape[0]), family_targets] -= 1.0
    dfamily *= (weights / weight_sum)[:, None]
    dselector = selector_probabilities.copy()
    dselector[np.arange(selector_targets.shape[0]), selector_targets] -= 1.0
    dselector *= (weights / weight_sum)[:, None]
    dselector *= selector_mask
    gradients: dict[str, np.ndarray] = {}
    gradients["family_w"] = (cache["h2"].T @ dfamily).astype(np.float32)
    gradients["family_b"] = dfamily.sum(axis=0).astype(np.float32)
    gradients["selector_w"] = (cache["h2"].T @ dselector).astype(np.float32)
    gradients["selector_b"] = dselector.sum(axis=0).astype(np.float32)
    dh2 = (
        dfamily @ parameters["family_w"].T
        + dselector @ parameters["selector_w"].T
    )
    dz2 = dh2 * (cache["z2"] > 0)
    gradients["w2"] = (cache["h1"].T @ dz2).astype(np.float32)
    gradients["b2"] = dz2.sum(axis=0).astype(np.float32)
    dh1 = dz2 @ parameters["w2"].T
    dz1 = dh1 * (cache["z1"] > 0)
    gradients["w1"] = (cache["context"].T @ dz1).astype(np.float32)
    gradients["b1"] = dz1.sum(axis=0).astype(np.float32)
    dcontext = dz1 @ parameters["w1"].T
    embedding_gradient = np.zeros_like(parameters["context_embedding"])
    per_token = (
        dcontext[:, None, :] / cache["denominator"][:, :, None]
    ) * context_mask[..., None]
    np.add.at(
        embedding_gradient,
        context_indices.reshape(-1),
        per_token.reshape(-1, dcontext.shape[1]),
    )
    gradients["context_embedding"] = embedding_gradient
    return loss, gradients


def _replay_metrics(
    parameters: Mapping[str, np.ndarray],
    dataset: ReplayDataset,
    split_name: str,
    *,
    batch_size: int = 4096,
) -> dict[str, object]:
    indices = np.flatnonzero(dataset.splits == SPLIT_CODES[split_name])
    family_predictions: list[np.ndarray] = []
    selector_predictions: list[np.ndarray] = []
    nll: list[np.ndarray] = []
    for start in range(0, indices.shape[0], batch_size):
        selected_indices = indices[start : start + batch_size]
        family_probabilities, selector_logits, _ = _replay_forward(
            parameters,
            dataset.context_indices[selected_indices],
            dataset.context_mask[selected_indices],
            cache=False,
        )
        family_targets = dataset.family_targets[selected_indices]
        selector_targets = dataset.selector_targets[selected_indices]
        family_prediction = family_probabilities.argmax(axis=1)
        selector_mask = dataset.allowed_selector_mask[family_prediction]
        selector_probabilities = _masked_selector_probabilities(
            selector_logits, selector_mask
        )
        selector_prediction = selector_probabilities.argmax(axis=1)
        family_predictions.append(family_prediction)
        selector_predictions.append(selector_prediction)
        true_selector_probabilities = _masked_selector_probabilities(
            selector_logits,
            dataset.allowed_selector_mask[family_targets],
        )
        nll.append(
            -np.log(
                np.maximum(
                    family_probabilities[
                        np.arange(family_targets.shape[0]), family_targets
                    ],
                    1.0e-12,
                )
            )
            - np.log(
                np.maximum(
                    true_selector_probabilities[
                        np.arange(selector_targets.shape[0]), selector_targets
                    ],
                    1.0e-12,
                )
            )
        )
    family_prediction = np.concatenate(family_predictions)
    selector_prediction = np.concatenate(selector_predictions)
    family_target = dataset.family_targets[indices]
    selector_target = dataset.selector_targets[indices]
    token_correct = (family_prediction == family_target) & (
        selector_prediction == selector_target
    )
    macro, per_flow = _macro_flow_accuracy(
        token_correct.astype(np.int8),
        np.ones(token_correct.shape[0], dtype=np.int8),
        dataset.flow_ids[indices],
        dataset.flow_names,
    )
    return {
        "split": split_name,
        "count": int(indices.shape[0]),
        "token_top1": float(token_correct.mean()),
        "family_top1": float((family_prediction == family_target).mean()),
        "selector_top1": float((selector_prediction == selector_target).mean()),
        "macro_flow_token_top1": macro,
        "per_flow_token_top1": per_flow,
        "nll": float(np.concatenate(nll).mean()),
    }


def _replay_frequency_baseline(
    dataset: ReplayDataset,
    train_indices: np.ndarray,
    eval_indices: np.ndarray,
) -> dict[str, object]:
    family_frequency: Counter[tuple[int, int]] = Counter()
    selector_frequency: Counter[tuple[int, int, int]] = Counter()
    for index in train_indices.tolist():
        flow = int(dataset.flow_ids[index])
        family = int(dataset.family_targets[index])
        selector = int(dataset.selector_targets[index])
        family_frequency[(flow, family)] += 1
        selector_frequency[(flow, family, selector)] += 1
    family_predictions: list[int] = []
    selector_predictions: list[int] = []
    for index in eval_indices.tolist():
        flow = int(dataset.flow_ids[index])
        family = max(
            range(len(dataset.family_tokens)),
            key=lambda value: (family_frequency[(flow, value)], -value),
        )
        allowed = np.flatnonzero(dataset.allowed_selector_mask[family] > 0)
        selector = max(
            allowed.tolist(),
            key=lambda value: (selector_frequency[(flow, family, value)], -value),
        )
        family_predictions.append(family)
        selector_predictions.append(selector)
    family_prediction = np.asarray(family_predictions, dtype=np.int16)
    selector_prediction = np.asarray(selector_predictions, dtype=np.int16)
    family_target = dataset.family_targets[eval_indices]
    selector_target = dataset.selector_targets[eval_indices]
    token_correct = (family_prediction == family_target) & (
        selector_prediction == selector_target
    )
    macro, per_flow = _macro_flow_accuracy(
        token_correct.astype(np.int8),
        np.ones(token_correct.shape[0], dtype=np.int8),
        dataset.flow_ids[eval_indices],
        dataset.flow_names,
    )
    return {
        "count": int(eval_indices.shape[0]),
        "token_top1": float(token_correct.mean()),
        "family_top1": float((family_prediction == family_target).mean()),
        "macro_flow_token_top1": macro,
        "per_flow_token_top1": per_flow,
    }


def train_replay_model(
    dataset: ReplayDataset,
    *,
    epochs: int = 12,
    batch_size: int = 1024,
    learning_rate: float = 0.001,
    seed: int = 20260828,
    patience: int = 4,
) -> tuple[dict[str, np.ndarray], dict[str, object]]:
    parameters = _init_replay_parameters(dataset, seed=seed)
    optimizer = _Adam(parameters, learning_rate)
    rng = np.random.default_rng(seed)
    train_indices = np.flatnonzero(dataset.splits == SPLIT_CODES["train"])
    best_parameters = {key: value.copy() for key, value in parameters.items()}
    best_nll = float("inf")
    stale = 0
    history: list[dict[str, object]] = []
    for epoch in range(1, epochs + 1):
        shuffled = rng.permutation(train_indices)
        losses: list[float] = []
        for start in range(0, shuffled.shape[0], batch_size):
            indices = shuffled[start : start + batch_size]
            loss, gradients = _replay_gradients(
                parameters,
                (
                    dataset.context_indices[indices],
                    dataset.context_mask[indices],
                    dataset.family_targets[indices],
                    dataset.selector_targets[indices],
                    dataset.weights[indices],
                    dataset.allowed_selector_mask,
                ),
            )
            optimizer.update(parameters, gradients)
            losses.append(loss)
        validation = _replay_metrics(parameters, dataset, "validation")
        validation_nll = float(validation["nll"])
        history.append(
            {
                "epoch": epoch,
                "train_loss": float(np.mean(losses)),
                "validation_nll": validation_nll,
                "validation_macro_top1": validation["macro_flow_token_top1"],
            }
        )
        print(
            f"replay epoch={epoch} train_loss={np.mean(losses):.6f} "
            f"val_nll={validation_nll:.6f} "
            f"val_macro={float(validation['macro_flow_token_top1']):.4f}",
            flush=True,
        )
        if validation_nll + 1.0e-6 < best_nll:
            best_nll = validation_nll
            best_parameters = {key: value.copy() for key, value in parameters.items()}
            stale = 0
        else:
            stale += 1
            if stale >= patience:
                break
    validation_indices = np.flatnonzero(
        dataset.splits == SPLIT_CODES["validation"]
    )
    test_indices = np.flatnonzero(dataset.splits == SPLIT_CODES["test"])
    return best_parameters, {
        "history": history,
        "train": _replay_metrics(best_parameters, dataset, "train"),
        "validation": _replay_metrics(best_parameters, dataset, "validation"),
        "test": _replay_metrics(best_parameters, dataset, "test"),
        "validation_frequency_baseline": _replay_frequency_baseline(
            dataset, train_indices, validation_indices
        ),
        "test_frequency_baseline": _replay_frequency_baseline(
            dataset, train_indices, test_indices
        ),
    }


def _model_arrays(
    parameters: Mapping[str, np.ndarray],
    metadata: Mapping[str, object],
) -> dict[str, np.ndarray]:
    result = {key: value for key, value in parameters.items()}
    result["metadata_json"] = np.asarray(
        json.dumps(metadata, ensure_ascii=False, sort_keys=True), dtype=np.str_
    )
    return result


def load_model_artifact(
    path: Path,
) -> tuple[dict[str, np.ndarray], dict[str, Any], dict[str, np.ndarray]]:
    with np.load(Path(path), allow_pickle=False) as archive:
        if "metadata_json" not in archive.files:
            raise ValueError("behavior-cloning artifact has no metadata")
        metadata = json.loads(str(archive["metadata_json"].item()))
        if not isinstance(metadata, dict) or metadata.get("schema") != MODEL_SCHEMA:
            raise ValueError("behavior-cloning artifact metadata mismatch")
        extras = {
            key: archive[key].copy()
            for key in archive.files
            if key in {"allowed_selector_mask"}
        }
        parameters = {
            key: archive[key].copy()
            for key in archive.files
            if key not in {"metadata_json", *extras}
        }
    contract = metadata.get("card_feature_contract")
    shared = metadata.get('shared_primary_feature_contract')
    if shared is None:
        validate_card_feature_encoding(contract, metadata.get("candidate_encoding"))
        features = load_card_semantic_features(contract, relative_to=Path(path).parent)
    else:
        from .shared_bc_features import validate_shared_primary_contract, require_shared_model_contract
        features = load_card_semantic_features(contract, relative_to=Path(path).parent)
        validate_shared_primary_contract(shared, features, card_feature_contract=contract)
        require_shared_model_contract(metadata, shared)
    validate_current_state_feature_metadata(features, metadata)
    return parameters, metadata, extras


def validate_current_state_feature_metadata(features, metadata):
    """Never reinterpret existing weights using a different current-state encoder."""
    expected = getattr(features, "current_state_schema", None)
    if metadata.get("current_state_feature_schema") != expected:
        raise ValueError("model current-state feature schema differs from its feature artifact")


def score_pointer_candidates(
    model_path: Path,
    *,
    context_tokens: Sequence[str],
    candidate_semantics: Sequence[str],
    candidate_field_tokens: Sequence[Sequence[str]] | None = None,
    candidate_feature_schema: str | None = None,
    shared_primary_features: object | None = None,
) -> tuple[float, ...]:
    if not candidate_semantics:
        raise ValueError("pointer scoring requires at least one candidate")
    parameters, metadata, _extras = load_model_artifact(model_path)
    shared = metadata.get('shared_primary_feature_contract')
    if shared is not None:
        from .shared_bc_features import SharedPrimaryFeatures
        if (not isinstance(shared_primary_features, SharedPrimaryFeatures)
                or shared_primary_features.contract_sha256 != shared['contract_sha256']
                or tuple(context_tokens) != shared_primary_features.context_tokens
                or tuple(candidate_semantics) != shared_primary_features.candidate_semantics
                or tuple(tuple(x) for x in candidate_field_tokens or ()) != shared_primary_features.candidate_tokens):
            raise ValueError('Shared pointer scoring requires the current single-encoder feature pack')
    elif shared_primary_features is not None:
        raise ValueError('Legacy pointer weights cannot consume a shared primary feature pack')
    contract = metadata.get("card_feature_contract")
    if (contract is not None and candidate_feature_schema != contract["schema"]) or (
        contract is None and candidate_feature_schema is not None
    ):
        raise ValueError("pointer caller and model card feature schema differ")
    if metadata.get("kind") not in {
        "outer-candidate-pointer",
        "exact-main-action-candidate-pointer",
        "unified-candidate-pointer-overfit-smoke",
    }:
        raise ValueError("artifact is not a pointer model")
    context_buckets = int(metadata["context_buckets"])
    candidate_buckets = int(metadata["candidate_buckets"])
    candidate_prefix = str(metadata.get("candidate_feature_prefix", "candidate:"))
    context_indices, context_mask = _pad_token_rows(
        [[_hash_index(value, context_buckets) for value in context_tokens]]
    )
    fields = (
        tuple((value,) for value in candidate_semantics)
        if candidate_field_tokens is None
        else tuple(tuple(value) for value in candidate_field_tokens)
    )
    if len(fields) != len(candidate_semantics) or any(not value for value in fields):
        raise ValueError("pointer candidate field tokens are incomplete")
    encoded_candidates = [
        list(
            dict.fromkeys(
                _hash_index(candidate_prefix + field, candidate_buckets)
                for field in values
            )
        )
        for values in fields
    ]
    if (
        metadata.get("kind") == "exact-main-action-candidate-pointer"
        and len({tuple(value) for value in encoded_candidates})
        != len(encoded_candidates)
    ):
        raise ValueError("exact pointer candidates collide in the model hash space")
    candidate_indices, candidate_token_mask, candidate_mask = (
        _pad_candidate_token_rows([encoded_candidates])
    )
    probabilities, _ = _outer_forward(
        parameters,
        context_indices,
        context_mask,
        candidate_indices,
        candidate_token_mask,
        candidate_mask,
        cache=False,
    )
    return tuple(float(value) for value in probabilities[0])


def score_exact_main_action_candidates(
    model_path: Path,
    *,
    state_before: Mapping[str, Any],
    flow: str,
    stage: str,
    legal_candidates: Sequence[object],
) -> tuple[float, ...]:
    _parameters, metadata, _extras = load_model_artifact(model_path)
    semantic_features = load_card_semantic_features(
        metadata.get("card_feature_contract"), relative_to=Path(model_path).parent,
    )
    shared = metadata.get('shared_primary_feature_contract')
    if shared is not None:
        from .shared_bc_features import encode_shared_bc_primary, PRIMARY_SCOPE
        encoded = encode_shared_bc_primary(state_before=state_before, legal_candidates=legal_candidates,
            flow=flow, stage=stage, native_mask_complete=True, candidate_scope=PRIMARY_SCOPE,
            features=semantic_features, contract=shared)
        return score_pointer_candidates(model_path, context_tokens=encoded.context_tokens,
            candidate_semantics=encoded.candidate_semantics, candidate_field_tokens=encoded.candidate_tokens,
            candidate_feature_schema=semantic_features.schema, shared_primary_features=encoded)
    validate_exact_legal_candidates(state_before, legal_candidates)
    semantics = exact_candidate_semantics(state_before, legal_candidates)
    fields = exact_candidate_field_tokens(state_before, legal_candidates, semantic_features=semantic_features)
    context = build_exact_main_action_context_tokens(
        state_before=state_before,
        flow=flow,
        stage=stage,
        candidate_semantics=_exact_candidate_context_semantics(fields),
        semantic_features=semantic_features,
    )
    return score_pointer_candidates(
        model_path,
        context_tokens=context,
        candidate_semantics=semantics,
        candidate_field_tokens=fields,
        candidate_feature_schema=semantic_features.schema if semantic_features is not None else None,
    )


def predict_replay_syntax(
    model_path: Path,
    *,
    context_tokens: Sequence[str],
) -> dict[str, object]:
    parameters, metadata, extras = load_model_artifact(model_path)
    if metadata.get("kind") != "replay-hierarchical-action-prefix":
        raise ValueError("artifact is not a replay hierarchy model")
    allowed = extras.get("allowed_selector_mask")
    if allowed is None:
        raise ValueError("replay artifact has no selector mask")
    context_buckets = int(metadata["context_buckets"])
    context_indices, context_mask = _pad_token_rows(
        [[_hash_index(value, context_buckets) for value in context_tokens]]
    )
    family_probabilities, selector_logits, _ = _replay_forward(
        parameters, context_indices, context_mask, cache=False
    )
    family_index = int(family_probabilities[0].argmax())
    selector_probabilities = _masked_selector_probabilities(
        selector_logits, allowed[[family_index]]
    )
    selector_index = int(selector_probabilities[0].argmax())
    family_tokens = metadata.get("family_tokens")
    selector_tokens = metadata.get("selector_tokens")
    if not isinstance(family_tokens, list) or not isinstance(selector_tokens, list):
        raise ValueError("replay artifact token metadata is invalid")
    return {
        "action_family": family_tokens[family_index],
        "ordered_indexes": selector_tokens[selector_index],
        "family_probability": float(family_probabilities[0, family_index]),
        "selector_probability": float(selector_probabilities[0, selector_index]),
        "syntax_only": True,
        "runtime_promotion_allowed": False,
    }


def train_outer_artifact(
    *,
    labels_path: Path,
    spec_path: Path,
    output_root: Path = DEFAULT_MODEL_ROOT,
    epochs: int = 20,
    batch_size: int = 512,
) -> dict[str, object]:
    dataset = load_outer_dataset(labels_path)
    parameters, metrics = train_outer_model(
        dataset, epochs=epochs, batch_size=batch_size
    )
    output_root = Path(output_root)
    model_path = output_root / "outer_bc_model.npz"
    metadata = {
        "schema": MODEL_SCHEMA,
        "kind": "outer-candidate-pointer",
        "context_buckets": dataset.context_buckets,
        "candidate_buckets": dataset.candidate_buckets,
        "embedding_dim": 32,
        "hidden": [128, 64],
        "candidate_feature_prefix": "candidate:",
        "shadow_only": True,
    }
    _atomic_npz(model_path, _model_arrays(parameters, metadata))
    validation = _mapping(metrics.get("validation"))
    baseline = _mapping(metrics.get("validation_frequency_baseline"))
    per_flow = _mapping(validation.get("per_flow_top1"))
    baseline_flow = _mapping(baseline.get("per_flow_top1"))
    maximum_drop = max(
        (
            float(baseline_flow.get(flow, 0.0)) - float(value)
            for flow, value in per_flow.items()
        ),
        default=0.0,
    )
    acceptance = {
        "macro_top1_at_least_0_60": float(
            validation.get("macro_flow_top1", 0.0)
        )
        >= 0.60,
        "improvement_over_frequency_baseline_pp": 100.0
        * (
            float(validation.get("macro_flow_top1", 0.0))
            - float(baseline.get("macro_flow_top1", 0.0))
        ),
        "maximum_per_flow_drop_pp": 100.0 * maximum_drop,
        "illegal_rate_zero": float(validation.get("illegal_rate", 1.0)) == 0.0,
        "runtime_promotion_allowed": False,
    }
    acceptance["passed"] = bool(
        acceptance["macro_top1_at_least_0_60"]
        and acceptance["improvement_over_frequency_baseline_pp"] >= 2.0
        and acceptance["maximum_per_flow_drop_pp"] <= 5.0
        and acceptance["illegal_rate_zero"]
    )
    report: dict[str, object] = {
        "schema": OUTER_REPORT_SCHEMA,
        "dataset": {
            "labels_path": str(labels_path),
            "labels_sha256": _sha256_file(Path(labels_path)),
            "spec_path": str(spec_path),
            "spec_sha256": _sha256_file(Path(spec_path)),
            "example_count": dataset.size,
            "split_counts": {
                name: int((dataset.splits == code).sum())
                for name, code in SPLIT_CODES.items()
            },
        },
        "model": {**metadata, "path": str(model_path)},
        "metrics": metrics,
        "acceptance": acceptance,
        "default_enabled": False,
        "shadow_only": True,
        "artifacts": {"model_sha256": _sha256_file(model_path)},
    }
    report_path = output_root / "outer_bc_report.json"
    _atomic_json(report_path, report)
    report["report_file_sha256"] = _sha256_file(report_path)
    return report


def train_exact_main_action_artifact(
    *,
    stages_path: Path,
    spec_path: Path,
    output_root: Path = DEFAULT_MODEL_ROOT,
    epochs: int = 100,
    batch_size: int = 64,
    seed: int = 20260829,
    training_callback: Any | None = None,
) -> dict[str, object]:
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8-sig"))
    if not isinstance(spec, Mapping) or spec.get("schema") not in {SPEC_V2_SCHEMA, SPEC_SCOPED_SCHEMA}:
        raise ValueError("exact main-action BC requires training spec v2 or scoped spec")
    exact_lock = _mapping(_mapping(spec.get("dataset_lock")).get("exact_exam"))
    actual_stages_sha = _sha256_file(Path(stages_path))
    if exact_lock.get("stages_sha256") != actual_stages_sha:
        raise ValueError("exact main-action BC stages do not match training spec v2")
    if spec.get("schema") == SPEC_SCOPED_SCHEMA:
        scope = _mapping(spec.get("training_scope"))
        rebuilt = build_scoped_training_spec(exact_rl_root=Path(stages_path).parent, flows=scope.get("flows", []),
            enable_offline_rl=_mapping(_mapping(spec.get("objectives")).get("offline_rl")).get("learner_enabled", False))
        # Re-read locked manifests and all trajectory/source split bindings at
        # training time. Merely changing a scope label never qualifies data.
        for key in ("training_scope", "dataset_lock", "inventory", "objectives"):
            if spec.get(key) != rebuilt[key]:
                raise ValueError(f"scoped exact BC {key} no longer matches frozen evidence")
    feature_contract = spec.get("card_feature_contract")
    semantic_features = load_card_semantic_features(feature_contract, relative_to=Path(spec_path).parent)
    shared = spec.get('shared_primary_feature_contract')
    if shared is not None:
        from .shared_bc_features import validate_shared_primary_contract
        validate_shared_primary_contract(shared, semantic_features, card_feature_contract=feature_contract)
        if shared.get('diagnostic_only') is True:
            raise ValueError('Diagnostic-only shared feature contract cannot enter fitting')
    dataset, loader_audit = load_exact_main_action_dataset(stages_path, semantic_features=semantic_features,
        shared_primary_feature_contract=shared)
    exact_inventory = _mapping(_mapping(spec.get("inventory")).get("exact_exam"))
    exact_objective = _mapping(_mapping(spec.get("objectives")).get("exact_main_action_bc"))
    expected_splits = _mapping(exact_inventory.get("split_transition_counts"))
    actual_splits = {
        name: int((dataset.splits == code).sum())
        for name, code in SPLIT_CODES.items()
    }
    if (
        exact_inventory.get("transition_count") != dataset.size
        or exact_objective.get("rows") != dataset.size
        or dict(expected_splits) != actual_splits
    ):
        raise ValueError("exact main-action BC cohort does not match training spec v2")
    shared_evidence = None
    if shared is not None:
        from .training_spec import validate_shared_primary_training_evidence
        shared_evidence = validate_shared_primary_training_evidence(spec, spec_path=Path(spec_path), stages_path=Path(stages_path),
            loader_audit=loader_audit)
    parameters, metrics = train_outer_model(
        dataset,
        epochs=epochs,
        batch_size=batch_size,
        seed=seed,
        patience=15,
        maximum_validation_per_flow_drop_pp=5.0,
        training_callback=training_callback,
    )
    output_root = Path(output_root)
    model_path = output_root / "exact_main_action_bc_model.npz"
    model_feature_contract = copy_card_feature_contract(feature_contract, output_root, relative_to=Path(spec_path).parent)
    metadata = {
        "schema": MODEL_SCHEMA,
        "kind": "exact-main-action-candidate-pointer",
        "context_buckets": dataset.context_buckets,
        "candidate_buckets": dataset.candidate_buckets,
        "embedding_dim": 32,
        "hidden": [128, 64],
        "candidate_feature_prefix": "exact-candidate-field:",
        "candidate_encoding": shared['encoding'] if shared is not None else semantic_features.encoding if semantic_features is not None else "field-token-bag-v1",
        "input_contract": "native-state-before+authoritative-ordered-legal-candidates-v1",
        "action_surface": "main-action-v1",
        "guid_policy": "binding-only",
        "seed": seed,
        "optimizer": "Adam",
        "learning_rate": 0.001,
        "batch_size": batch_size,
        "max_epochs": epochs,
        "shadow_only": True,
    }
    if model_feature_contract is not None:
        metadata["card_feature_contract"] = model_feature_contract
    if shared is not None:
        metadata['shared_primary_feature_contract'] = dict(shared)
        metadata['shared_primary_training_evidence'] = shared_evidence
    current_state_schema = getattr(semantic_features, "current_state_schema", None)
    if current_state_schema is not None:
        metadata["current_state_feature_schema"] = current_state_schema
    if spec.get("schema") == SPEC_SCOPED_SCHEMA:
        metadata["training_scope"] = dict(_mapping(spec.get("training_scope")))
        metadata["training_spec_sha256"] = _sha256_file(Path(spec_path))
        metadata["stages_sha256"] = actual_stages_sha
    _atomic_npz(model_path, _model_arrays(parameters, metadata))
    validation = _mapping(metrics.get("validation"))
    baseline = _mapping(metrics.get("validation_frequency_baseline"))
    validation_improvement = 100.0 * (
        float(validation.get("macro_flow_top1", 0.0))
        - float(baseline.get("macro_flow_top1", 0.0))
    )
    test = _mapping(metrics.get("test"))
    test_baseline = _mapping(metrics.get("test_frequency_baseline"))
    test_improvement = 100.0 * (
        float(test.get("macro_flow_top1", 0.0))
        - float(test_baseline.get("macro_flow_top1", 0.0))
    )

    validation_maximum_drop = 100.0 * _maximum_per_flow_drop(validation, baseline)
    test_maximum_drop = 100.0 * _maximum_per_flow_drop(test, test_baseline)
    binding_rate = float(loader_audit["legal_binding_count"]) / float(dataset.size)
    blockers: list[str] = []
    if binding_rate != 1.0:
        blockers.append("strict-legal-binding-rate-below-one")
    if loader_audit["candidate_semantic_collision_rows"] != 0:
        blockers.append("candidate-semantic-collision")
    if loader_audit["candidate_hash_collision_rows"] != 0:
        blockers.append("candidate-hash-collision")
    minimum_improvement = 0.0 if semantic_features is not None else 2.0
    if validation_improvement < minimum_improvement:
        blockers.append("validation-below-baseline" if semantic_features is not None else "validation-improvement-below-2pp")
    if validation_maximum_drop > 5.0:
        blockers.append("validation-per-flow-drop-above-5pp")
    if test_improvement < 0.0:
        blockers.append("test-improvement-below-baseline")
    if test_maximum_drop > 5.0:
        blockers.append("test-per-flow-drop-above-5pp")
    if spec.get("schema") == SPEC_SCOPED_SCHEMA and exact_inventory.get("all_flow_stage_splits_complete") is not True:
        blockers.append("declared-flow-stage-split-coverage-incomplete")
    shadow_ready = not blockers
    acceptance = {
        "trained": True,
        "exact_cohort_only": True,
        "strict_legal_binding_rate": binding_rate,
        "candidate_semantic_collision_rows": loader_audit[
            "candidate_semantic_collision_rows"
        ],
        "candidate_hash_collision_rows": loader_audit[
            "candidate_hash_collision_rows"
        ],
        "validation_improvement_over_frequency_baseline_pp": validation_improvement,
        "validation_maximum_per_flow_drop_pp": validation_maximum_drop,
        "test_improvement_over_frequency_baseline_pp": test_improvement,
        "test_maximum_per_flow_drop_pp": test_maximum_drop,
        "shadow_ready": shadow_ready,
        "shadow_blockers": blockers,
        "runtime_promotion_allowed": False,
        "generalization_claimed": False,
    }
    if semantic_features is not None:
        acceptance["policy"] = semantic_features.schema.removeprefix("gkms.card-").replace("-features.", "-") + "-heldout-non-regression"
    report: dict[str, object] = {
        "schema": EXACT_REPORT_SCHEMA,
        "dataset": {
            "stages_path": str(stages_path),
            "stages_sha256": actual_stages_sha,
            "spec_path": str(spec_path),
            "spec_sha256": _sha256_file(Path(spec_path)),
            "example_count": dataset.size,
            "split_counts": {
                name: int((dataset.splits == code).sum())
                for name, code in SPLIT_CODES.items()
            },
            **loader_audit,
        },
        "model": {**metadata, "path": str(model_path)},
        "metrics": metrics,
        "acceptance": acceptance,
        "default_enabled": False,
        "shadow_only": True,
        "artifacts": {"model_sha256": _sha256_file(model_path)},
    }
    report_path = output_root / "exact_main_action_bc_report.json"
    _atomic_json(report_path, report)
    report["report_file_sha256"] = _sha256_file(report_path)
    return report


def train_replay_artifact(
    *,
    episodes_path: Path,
    spec_path: Path,
    output_root: Path = DEFAULT_MODEL_ROOT,
    epochs: int = 12,
    batch_size: int = 1024,
    flow_scope: Sequence[str] | None = None,
    seed: int = 20260828,
) -> dict[str, object]:
    scoped_spec = None
    if flow_scope is not None:
        scoped_spec = json.loads(Path(spec_path).read_text(encoding="utf-8-sig"))
        if (scoped_spec.get("schema") != "gkms.replay-training-spec.v1"
                or scoped_spec.get("training_scope", {}).get("flows") != sorted(flow_scope)
                or scoped_spec.get("input_feature_schema") != "gkms.replay-native-observable-static.v1"
                or scoped_spec.get("dataset", {}).get("episodes_sha256") != _sha256_file(Path(episodes_path))):
            raise ValueError("scoped replay BC training spec/input binding mismatch")
    dataset = load_replay_dataset(episodes_path, require_provenance=True, flow_scope=flow_scope)
    if scoped_spec is not None and (dataset.size != scoped_spec["dataset"]["action_count"]
            or {name: int((dataset.splits == code).sum()) for name, code in SPLIT_CODES.items()} != scoped_spec["dataset"]["split_action_counts"]):
        raise ValueError("scoped replay BC cohort/split count mismatch")
    parameters, metrics = train_replay_model(
        dataset, epochs=epochs, batch_size=batch_size, seed=seed,
    )
    output_root = Path(output_root)
    model_path = output_root / "replay_bc_model.npz"
    metadata = {
        "schema": MODEL_SCHEMA,
        "kind": "replay-hierarchical-action-prefix",
        "context_buckets": dataset.context_buckets,
        "embedding_dim": 32,
        "hidden": [256, 128],
        "target_tokens": list(dataset.target_tokens),
        "family_tokens": list(dataset.family_tokens),
        "selector_tokens": list(dataset.selector_tokens),
        "syntax_only": True,
        "shadow_only": True,
    }
    if scoped_spec is not None:
        metadata.update(training_scope=scoped_spec["training_scope"], training_spec_sha256=_sha256_file(Path(spec_path)),
            episodes_sha256=_sha256_file(Path(episodes_path)), source_authority="official-history-action-sequence",
            input_contract="history-static-context-and-past-three-observed-actions-v1", seed=seed,
            context_builder="gkms_tool.behavior_cloning.build_replay_context_tokens")
        metadata["input_feature_schema"] = scoped_spec["input_feature_schema"]
    model_arrays = _model_arrays(parameters, metadata)
    model_arrays["allowed_selector_mask"] = dataset.allowed_selector_mask
    _atomic_npz(model_path, model_arrays)
    validation = _mapping(metrics.get("validation"))
    baseline = _mapping(metrics.get("validation_frequency_baseline"))
    token_improvement = 100.0 * (
        float(validation.get("macro_flow_token_top1", 0.0))
        - float(baseline.get("macro_flow_token_top1", 0.0))
    )
    family_improvement = 100.0 * (
        float(validation.get("family_top1", 0.0))
        - float(baseline.get("family_top1", 0.0))
    )
    report: dict[str, object] = {
        "schema": REPLAY_REPORT_SCHEMA,
        "dataset": {
            "episodes_path": str(episodes_path),
            "episodes_sha256": _sha256_file(Path(episodes_path)),
            "spec_path": str(spec_path),
            "spec_sha256": _sha256_file(Path(spec_path)),
            "action_example_count": dataset.size,
            "target_token_count": len(dataset.target_tokens),
            "family_token_count": len(dataset.family_tokens),
            "selector_token_count": len(dataset.selector_tokens),
            "split_counts": {
                name: int((dataset.splits == code).sum())
                for name, code in SPLIT_CODES.items()
            },
        },
        "model": {**metadata, "path": str(model_path)},
        "metrics": metrics,
        "acceptance": {
            "trained": True,
            "validation_macro_token_improvement_pp": token_improvement,
            "validation_family_improvement_pp": family_improvement,
            "syntax_prior_accepted": token_improvement >= 2.0
            and family_improvement >= 0.0,
            "runtime_promotion_allowed": False,
            "reason": "syntax-only replay prior has no exact live state/legal set",
        },
        "default_enabled": False,
        "shadow_only": True,
        "artifacts": {"model_sha256": _sha256_file(model_path)},
    }
    report_path = output_root / "replay_bc_report.json"
    _atomic_json(report_path, report)
    report["report_file_sha256"] = _sha256_file(report_path)
    return report


def train_unified_smoke_artifact(
    *,
    labels_path: Path,
    spec_path: Path,
    output_root: Path = DEFAULT_MODEL_ROOT,
    epochs: int = 400,
) -> dict[str, object]:
    dataset, semantic_collision_rows = load_unified_state_dataset(labels_path)
    parameters, metrics = train_unified_overfit_smoke(dataset, epochs=epochs)
    output_root = Path(output_root)
    model_path = output_root / "unified_bc_smoke_model.npz"
    metadata = {
        "schema": MODEL_SCHEMA,
        "kind": "unified-candidate-pointer-overfit-smoke",
        "context_buckets": dataset.context_buckets,
        "candidate_buckets": dataset.candidate_buckets,
        "embedding_dim": 32,
        "hidden": [128, 64],
        "candidate_feature_prefix": "unified-candidate:",
        "diagnostic_only": True,
        "shadow_only": True,
    }
    _atomic_npz(model_path, _model_arrays(parameters, metadata))
    report: dict[str, object] = {
        "schema": UNIFIED_REPORT_SCHEMA,
        "dataset": {
            "labels_path": str(labels_path),
            "labels_sha256": _sha256_file(Path(labels_path)),
            "spec_path": str(spec_path),
            "spec_sha256": _sha256_file(Path(spec_path)),
            "row_count": dataset.size,
            "flow_count": len(dataset.flow_names),
            "split_counts": {
                name: int((dataset.splits == code).sum())
                for name, code in SPLIT_CODES.items()
            },
            "semantic_collision_rows": semantic_collision_rows,
        },
        "model": {**metadata, "path": str(model_path)},
        "metrics": metrics,
        "acceptance": {
            "pipeline_smoke_passed": metrics["overfit_accuracy"] == 1.0
            and metrics["permutation_invariant"] is True,
            "generalization_claimed": False,
            "runtime_promotion_allowed": False,
        },
        "artifacts": {"model_sha256": _sha256_file(model_path)},
        "default_enabled": False,
        "shadow_only": True,
    }
    report_path = output_root / "unified_bc_smoke_report.json"
    _atomic_json(report_path, report)
    report["report_file_sha256"] = _sha256_file(report_path)
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Train frozen GKMS behavior-cloning models")
    parser.add_argument(
        "objective",
        choices=("outer", "replay", "exact", "unified", "all"),
        nargs="?",
        default="all",
    )
    parser.add_argument("--frozen-root", type=Path, default=DEFAULT_FROZEN_ROOT)
    parser.add_argument("--spec", type=Path, default=DEFAULT_SPEC_PATH)
    parser.add_argument("--exact-spec", type=Path, default=DEFAULT_EXACT_SPEC_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_MODEL_ROOT)
    parser.add_argument("--exact-stages", type=Path, default=DEFAULT_EXACT_STAGES)
    parser.add_argument("--outer-epochs", type=int, default=20)
    parser.add_argument("--replay-epochs", type=int, default=12)
    parser.add_argument("--outer-batch-size", type=int, default=512)
    parser.add_argument("--replay-batch-size", type=int, default=1024)
    parser.add_argument("--exact-epochs", type=int, default=100)
    parser.add_argument("--exact-batch-size", type=int, default=64)
    parser.add_argument("--unified-epochs", type=int, default=400)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    results: dict[str, object] = {}
    if args.objective in {"outer", "all"}:
        results["outer"] = train_outer_artifact(
            labels_path=args.frozen_root / "canonical" / "labels.jsonl",
            spec_path=args.spec,
            output_root=args.output,
            epochs=args.outer_epochs,
            batch_size=args.outer_batch_size,
        )
    if args.objective in {"replay", "all"}:
        results["replay"] = train_replay_artifact(
            episodes_path=args.frozen_root / "episodes.jsonl",
            spec_path=args.spec,
            output_root=args.output,
            epochs=args.replay_epochs,
            batch_size=args.replay_batch_size,
        )
    if args.objective in {"exact", "all"}:
        results["exact"] = train_exact_main_action_artifact(
            stages_path=args.exact_stages,
            spec_path=args.exact_spec,
            output_root=args.output,
            epochs=args.exact_epochs,
            batch_size=args.exact_batch_size,
        )
    if args.objective in {"unified", "all"}:
        results["unified"] = train_unified_smoke_artifact(
            labels_path=args.frozen_root / "canonical" / "labels.jsonl",
            spec_path=args.spec,
            output_root=args.output,
            epochs=args.unified_epochs,
        )
    print(
        json.dumps(
            results,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            default=lambda value: value.item() if isinstance(value, np.generic) else value,
        )
    )
    return 0


__all__ = [
    "DEFAULT_EXACT_STAGES",
    "EXACT_REPORT_SCHEMA",
    "MODEL_SCHEMA",
    "OUTER_REPORT_SCHEMA",
    "REPLAY_REPORT_SCHEMA",
    "UNIFIED_REPORT_SCHEMA",
    "OuterDataset",
    "ReplayDataset",
    "build_outer_context_tokens",
    "build_exact_main_action_context_tokens",
    "exact_candidate_field_tokens",
    "exact_candidate_semantics",
    "evaluate_outer_model",
    "load_outer_dataset",
    "load_model_artifact",
    "load_exact_main_action_dataset",
    "load_replay_dataset",
    "load_unified_state_dataset",
    "main",
    "predict_replay_syntax",
    "score_pointer_candidates",
    "score_exact_main_action_candidates",
    "train_exact_main_action_artifact",
    "train_outer_artifact",
    "train_outer_model",
    "train_replay_artifact",
    "train_replay_model",
    "train_unified_overfit_smoke",
    "train_unified_smoke_artifact",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
