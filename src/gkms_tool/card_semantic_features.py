"""Versioned card-effect features pinned to an immutable Master snapshot.

The existing pointer MLP consumes these tokens only when its artifact declares
this contract.  Legacy artifacts retain their original ID/slot encoder.  This
module describes observed inputs; it neither simulates effects nor fabricates
runtime legality or counterfactual rewards.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from functools import lru_cache
import json
import math
from pathlib import Path

from .card_data_update import load_card_data_snapshot
from .training_artifact_io import atomic_write, canonical_json_bytes, sha256_file


FEATURE_SCHEMA_V1 = "gkms.card-semantic-features.v1"
FEATURE_SCHEMA_V2 = "gkms.card-semantic-features.v2"
FEATURE_SCHEMA_V3 = "gkms.card-semantic-features.v3"
FEATURE_SCHEMA = "gkms.card-semantic-features.v4"
LEGACY_ENCODING = "field-token-bag-v1"
SEMANTIC_ENCODING_V1 = "field-token-bag+card-semantic-v1"
SEMANTIC_ENCODING_V2 = "field-token-bag+card-semantic-v2"
SEMANTIC_ENCODING_V3 = "field-token-bag+card-semantic-v3"
SEMANTIC_ENCODING = "field-token-bag+card-semantic-v4"
_ENCODINGS = {FEATURE_SCHEMA_V1: SEMANTIC_ENCODING_V1, FEATURE_SCHEMA_V2: SEMANTIC_ENCODING_V2,
              FEATURE_SCHEMA_V3: SEMANTIC_ENCODING_V3, FEATURE_SCHEMA: SEMANTIC_ENCODING}
_EFFECTIVE_SCHEMAS = frozenset({FEATURE_SCHEMA_V2, FEATURE_SCHEMA_V3, FEATURE_SCHEMA})
_LIFECYCLE_SCHEMAS = frozenset({FEATURE_SCHEMA_V3, FEATURE_SCHEMA})
_CARD_SCALARS = (
    "planType", "category", "rarity", "stamina", "forceStamina", "costType",
    "costValue", "isEndTurnLost", "isInitial", "isRestrict", "noDeckDuplication",
    "isConversion", "playMovePositionType", "moveEffectTriggerType",
)
_EFFECT_SCALARS = (
    "effectType", "effectValue1", "effectValue2", "effectCount", "effectTurn",
    "targetUpgradeCount", "targetExamEffectType", "movePositionType", "pickRangeType",
    "pickCountType", "pickCountMin", "pickCountMax", "pickRangeType2",
    "pickCountType2", "pickCountMin2", "pickCountMax2",
)
_KNOWN_EFFECT_FIELDS = frozenset(_EFFECT_SCALARS) | frozenset({
    "id", "targetProduceCardId", "produceCardSearchId", "produceCardSearchId2",
    "pickCountReferenceProduceCardSearchId", "pickCountReferenceProduceCardSearchId2",
    "chainProduceExamEffectId", "chainProduceExamEffectIds", "produceExamStatusEnchantId",
    "produceCardStatusEnchantId", "produceCardGrowEffectIds", "effectGroupIds",
    "produceDescriptions", "customizeProduceDescriptions",
})
_IGNORED = frozenset({
    "id", "assetId", "asset_id", "name", "produceDescriptions",
    "customizeProduceDescriptions", "descriptions_json", "raw_json",
})


def number_tokens(name: str, value: int | float) -> tuple[str, ...]:
    """Exact values plus shared magnitude bins allow nearby values to share features."""
    if isinstance(value, bool) or not math.isfinite(float(value)):
        raise ValueError("card feature number must be finite")
    numeric = float(value)
    exact = str(int(numeric)) if numeric.is_integer() else format(numeric, ".8g")
    magnitude = abs(numeric)
    if numeric == 0:
        return (f"n:{name}:exact:0",)
    return (
        f"n:{name}:exact:{exact}",
        f"n:{name}:sign:{'+' if numeric > 0 else '-' if numeric < 0 else '0'}",
        f"n:{name}:coarse:{math.floor(numeric / 8)}",
        f"n:{name}:log2:{0 if magnitude == 0 else math.floor(math.log2(magnitude))}",
    )


def _tokens(prefix: str, value: object) -> list[str]:
    if type(value) is bool:
        return [f"b:{prefix}:{int(value)}"]
    if isinstance(value, (int, float)):
        return list(number_tokens(prefix, value))
    if isinstance(value, str):
        return [f"s:{prefix}:{value}"] if value else []
    if isinstance(value, Mapping):
        return [token for key, child in sorted(value.items()) if key not in _IGNORED
                for token in _tokens(f"{prefix}.{key}", child)]
    if isinstance(value, list):
        return [token for index, child in enumerate(value)
                for token in _tokens(f"{prefix}[{index}]", child)]
    if value is None:
        return []
    raise ValueError(f"unsupported card feature value: {prefix}")


def _raw(row: Mapping[str, object]) -> Mapping[str, object]:
    value = row.get("raw_json")
    return value if isinstance(value, Mapping) else row


def build_card_semantic_features(snapshot_path: Path, *, schema: str = FEATURE_SCHEMA) -> dict[str, object]:
    if schema not in _ENCODINGS:
        raise ValueError("unsupported card feature schema")
    snapshot = load_card_data_snapshot(snapshot_path)
    tables = snapshot["tables"]
    effects = tables["effect"]
    triggers = tables["produce_exam_trigger"]
    enchants = tables["produce_exam_status_enchant"]
    searches = tables["produce_card_search"]

    def trigger_tokens(identity: object, prefix: str) -> list[str]:
        if not identity:
            return []
        row = triggers.get(str(identity))
        if row is None:
            raise ValueError(f"missing Master trigger: {identity}")
        return _tokens(prefix, _raw(row))

    def effect_tokens(identity: str, prefix: str, visited: tuple[str, ...] = ()) -> list[str]:
        row = effects.get(identity)
        if row is None:
            raise ValueError(f"missing Master exam effect: {identity}")
        raw = _raw(row)
        unknown = sorted(set(raw) - _KNOWN_EFFECT_FIELDS)
        if unknown and schema != FEATURE_SCHEMA:
            raise ValueError(f"unknown effect fields: {identity}: {','.join(unknown)}")
        kind = str(raw.get("effectType", row.get("effect_type", "")))
        if not kind:
            raise ValueError(f"missing effect type: {identity}")
        if identity in visited:
            return [f"effect-cycle:{kind}"]
        if len(visited) >= 12:
            raise ValueError(f"effect chain exceeds feature contract depth: {identity}")
        result = [f"effect-type:{kind}", f"{prefix}:type:{kind}"]
        for key in unknown:
            result.append(f"gap:{prefix}:unknown-Master-effect-field:{key}")
            result.extend(_tokens(f"{prefix}.unknown.{key}", raw[key]))
        result.extend(token for key in _EFFECT_SCALARS if key in raw and key != "effectType"
                      for token in _tokens(f"{prefix}.{key}", raw[key]))
        for key in ("produceCardSearchId", "produceCardSearchId2", "pickCountReferenceProduceCardSearchId", "pickCountReferenceProduceCardSearchId2"):
            search = raw.get(key)
            if search:
                if search not in searches:
                    raise ValueError(f"missing Master card search: {search}")
                result.extend(_tokens(f"{prefix}.{key}", _raw(searches[search])))
        next_visited = (*visited, identity)
        linked = []
        if raw.get("chainProduceExamEffectId"):
            linked.append(raw["chainProduceExamEffectId"])
        linked.extend(raw.get("chainProduceExamEffectIds") or ())
        for index, child in enumerate(linked):
            result.extend(effect_tokens(str(child), f"{prefix}.chain[{index}]", next_visited))
        enchant = raw.get("produceExamStatusEnchantId")
        if enchant:
            if enchant not in enchants:
                raise ValueError(f"missing Master status enchant: {enchant}")
            enchant_row = _raw(enchants[enchant])
            result.extend(trigger_tokens(enchant_row.get("produceExamTriggerId"), prefix + ".enchant.trigger"))
            children = enchant_row.get("produceExamEffectIds") or ()
            for index, child in enumerate(children):
                result.extend(effect_tokens(str(child), f"{prefix}.enchant[{index}]", next_visited))
        # Preserve external grow/status selectors explicitly.  Their runtime
        # counts are separate inputs; this v1 never claims to execute them.
        for key in ("targetProduceCardId", "produceCardStatusEnchantId", "produceCardGrowEffectIds", "effectGroupIds"):
            result.extend(_tokens(prefix + "." + key, raw.get(key)))
        return result

    auxiliary = snapshot.get("semantic_tables", {})
    if schema in _EFFECTIVE_SCHEMAS and not all(name in auxiliary for name in ("produce_card_customize", "produce_card_grow_effect", "produce_exam_gimmick")):
        raise ValueError("semantic v2 requires frozen customization/grow/gimmick tables; refresh card data")
    if schema in _LIFECYCLE_SCHEMAS and not all(name in auxiliary for name in ("produce", "produce_audition")):
        raise ValueError("semantic v3 requires frozen mode/audition lifecycle tables; refresh card data")
    cards, drinks, blockers = {}, {}, []
    for ref, row in tables["card"].items():
        raw = _raw(row)
        try:
            values = [token for key in _CARD_SCALARS if key in raw
                      for token in _tokens("card." + key, raw[key])]
            values.extend(trigger_tokens(raw.get("playProduceExamTriggerId"), "card.trigger"))
            for index, link in enumerate(raw.get("playEffects", row.get("play_effects_json", ()))):
                values.extend(effect_tokens(str(link["produceExamEffectId"]), f"play[{index}]"))
                if schema == FEATURE_SCHEMA:
                    values.extend(trigger_tokens(link.get("produceExamTriggerId"), f"play[{index}].trigger"))
                    values.extend(_tokens(f"play[{index}].isOncePlayEffect", link.get("isOncePlayEffect")))
            for index, child in enumerate(raw.get("moveProduceExamEffectIds") or ()):
                values.extend(effect_tokens(str(child), f"move[{index}]"))
            cards[ref] = {
                "tokens": sorted(set(values)),
                "customize_ids": list(raw.get("produceCardCustomizeIds") or ()),
            }
            if schema in _EFFECTIVE_SCHEMAS:
                rules = {}
                for identity in cards[ref]["customize_ids"]:
                    options = {}
                    for custom in auxiliary["produce_card_customize"].values():
                        if custom["id"] != identity:
                            continue
                        grows = []
                        for grow_id in custom.get("produceCardGrowEffectIds", ()):
                            grow = auxiliary["produce_card_grow_effect"].get(grow_id)
                            if grow is None:
                                raise ValueError(f"missing frozen grow effect: {grow_id}")
                            grows.append(grow)
                        options[str(custom["customizeCount"])] = {
                            "overwrite": custom.get("overwriteProduceCardGrowEffectType"), "grows": grows,
                        }
                    if not options:
                        raise ValueError(f"missing frozen customization: {identity}")
                    rules[identity] = options
                cards[ref].update(
                    base_scalars={key: raw[key] for key in _CARD_SCALARS if key in raw},
                    direct_effects=[_raw(effects[str(link["produceExamEffectId"])])
                                    for link in raw.get("playEffects", row.get("play_effects_json", ()))],
                    customization_rules=rules,
                )
                if schema == FEATURE_SCHEMA:
                    cards[ref]["direct_effect_links"] = deepcopy(raw.get("playEffects", row.get("play_effects_json", ())))
        except (KeyError, TypeError, ValueError) as error:
            blockers.append({"kind": "card", "id": ref, "reason": str(error)})
    drink_links = tables.get("produce_drink_effect", {})
    for identity, row in tables.get("produce_drink", {}).items():
        raw = _raw(row)
        try:
            values = _tokens("drink.plan", raw.get("planType"))
            for index, key in enumerate(raw.get("produceDrinkEffectIds", row.get("produce_drink_effect_ids_json", ()))):
                link = _raw(drink_links[key])
                exam_id = link.get("produceExamEffectId")
                if exam_id:
                    values.extend(effect_tokens(str(exam_id), f"drink[{index}]"))
                produce_id = link.get("produceEffectId")
                if produce_id:
                    values.extend(_tokens(f"drink.produce[{index}]", _raw(tables["produce_effect"][produce_id])))
            drinks[identity] = {"tokens": sorted(set(values))}
        except (KeyError, TypeError, ValueError) as error:
            blockers.append({"kind": "drink", "id": identity, "reason": str(error)})
    gimmicks = {}
    if schema in _EFFECTIVE_SCHEMAS:
        for row in auxiliary["produce_exam_gimmick"].values():
            effect_id = row.get("produceExamEffectId")
            try:
                tokens = _tokens("gimmick", {k: v for k, v in row.items() if k != "produceExamEffectId"})
                tokens.extend(effect_tokens(str(effect_id), "gimmick.effect"))
                gimmicks.setdefault(row["id"], []).append({
                    "turn": row.get("startTurn"), "effect_id": effect_id, "tokens": tokens,
                })
            except (KeyError, TypeError, ValueError) as error:
                blockers.append({"kind": "gimmick", "id": row["id"], "reason": str(error)})
    added_grow_features = {}
    if schema in _LIFECYCLE_SCHEMAS:
        for identity, grow in auxiliary["produce_card_grow_effect"].items():
            if grow.get("effectType") != "ProduceCardGrowEffectType_EffectAdd":
                continue
            child = grow.get("playProduceExamEffectId")
            if child:
                try:
                    added_grow_features[identity] = effect_tokens(str(child), "added-grow")
                except (KeyError, TypeError, ValueError) as error:
                    blockers.append({"kind": "added-grow", "id": identity, "reason": str(error)})
    return {
        "schema": schema, "snapshot_fingerprint": snapshot["fingerprint"],
        "cards": cards, "drinks": drinks, "blockers": blockers,
        **({"gimmicks": gimmicks, "grow_effects": auxiliary["produce_card_grow_effect"]} if schema in _EFFECTIVE_SCHEMAS else {}),
        **({"mode_lifecycle": _build_mode_lifecycle(auxiliary), "added_grow_features": added_grow_features} if schema in _LIFECYCLE_SCHEMAS else {}),
        **({"current_state_schema": "gkms.active-state-features.v2", "historical_summary_policy": "excluded"} if schema == FEATURE_SCHEMA else {}),
        "scope": "frozen Master card/grow/gimmick semantics + current native state" if schema in _EFFECTIVE_SCHEMAS else "Master base/upgrade effect graph + observed runtime customization counts",
        "limits": {"effect_depth": 12, "runtime_customization": "known unconditional effective values; all remaining modifiers explicit" if schema in _EFFECTIVE_SCHEMAS else "observed-counts-not-simulated-effects"},
    }


def write_card_semantic_features(snapshot_path: Path, output_path: Path, *, schema: str = FEATURE_SCHEMA) -> dict[str, object]:
    payload = build_card_semantic_features(snapshot_path, schema=schema)
    atomic_write(output_path, canonical_json_bytes(payload) + b"\n")
    return {"schema": payload["schema"], "snapshot_fingerprint": payload["snapshot_fingerprint"],
            "features_path": str(output_path.resolve()), "features_sha256": sha256_file(output_path)}


@dataclass(frozen=True, slots=True)
class CardSemanticFeatures:
    payload: Mapping[str, object]

    @property
    def schema(self) -> str:
        return str(self.payload["schema"])

    @property
    def encoding(self) -> str:
        return _ENCODINGS[self.schema]

    @property
    def current_state_schema(self) -> str | None:
        if self.schema != FEATURE_SCHEMA:
            return None
        from .active_state_features import SCHEMA
        if self.payload.get("current_state_schema") != SCHEMA:
            raise ValueError("card v4 current-state schema differs")
        return SCHEMA

    @property
    def uses_current_state_features(self) -> bool:
        return self.current_state_schema is not None

    def card_tokens(self, card_id: str, upgrade: int, runtime: Mapping[str, object]) -> tuple[str, ...]:
        if self.uses_current_state_features:
            return _v4_card_tokens(self.payload, card_id, upgrade, runtime)
        ref = f"{card_id}@{upgrade}"
        row = self.payload["cards"].get(ref)
        if row is None:
            raise ValueError(f"card is outside this artifact's semantic catalog: {ref}")
        result = list(row["tokens"])
        data = runtime.get("_cardData", runtime.get("cardData", {}))
        data = data if isinstance(data, Mapping) else {}
        counts = data.get("_customizeCountList", data.get("customizeCountList"))
        if counts is None:
            result.append("runtime.customization:missing")
        else:
            if not isinstance(counts, list) or any(type(value) is not int or value < 0 for value in counts):
                raise ValueError("runtime customization counts are invalid")
            for index, count in enumerate(counts):
                result.extend(number_tokens(f"runtime.customize[{index}]", count))
                if count and index < len(row["customize_ids"]):
                    result.append(f"runtime.customize-family:{row['customize_ids'][index]}")
                elif count:
                    raise ValueError(f"runtime customization is outside artifact catalog: {ref}/{index}")
        for key in ("_baseUpgradeCount", "_tmpUpgradeCount", "_playCount"):
            if key in runtime:
                result.extend(_tokens("runtime." + key, runtime[key]))
        if self.schema in _EFFECTIVE_SCHEMAS:
            active_grow = any(runtime.get(key) for key in (
                "_affectGrowEffectIdList", "_growEffectExamStartAfterList", "_staminaConsumptionSpecifyEffectList",
            ))
            result.extend(_customized_tokens(row, counts, effective_complete=not active_grow))
            for key in ("_affectGrowEffectIdList", "_growEffectExamStartAfterList", "_staminaConsumptionSpecifyEffectList"):
                values = runtime.get(key, ())
                for index, value in enumerate(values):
                    grow = self.payload["grow_effects"].get(value) if isinstance(value, str) else None
                    result.extend(_tokens(f"runtime.{key}[{index}]", grow if grow is not None else value))
        if self.schema in _LIFECYCLE_SCHEMAS and counts is not None:
            for index, count in enumerate(counts):
                if not count:
                    continue
                option = row["customization_rules"][row["customize_ids"][index]][str(count)]
                for grow in option["grows"]:
                    result.extend(self.payload["added_grow_features"].get(grow["id"], ()))
        return tuple(dict.fromkeys("semantic:" + value for value in result))

    def drink_tokens(self, drink_id: str) -> tuple[str, ...]:
        row = self.payload["drinks"].get(drink_id)
        if row is None:
            raise ValueError(f"drink is outside this artifact's semantic catalog: {drink_id}")
        return tuple("semantic:" + token for token in row["tokens"])

    def context_tokens(self, state: Mapping[str, object]) -> tuple[str, ...]:
        if self.uses_current_state_features:
            return _v4_context_tokens(self, state)
        if self.schema not in _EFFECTIVE_SCHEMAS:
            return ()
        tokens = []
        for zone in ("handList", "deckList", "graveList", "lostList", "holdList"):
            rows = state.get(zone)
            if not isinstance(rows, list):
                tokens.append(f"zone:{zone}:missing")
                continue
            histogram = Counter()
            for runtime in rows:
                if not isinstance(runtime, Mapping):
                    raise ValueError("native card zone has a malformed instance")
                data = runtime.get("_cardData", {})
                fields = self.card_tokens(str(data.get("_id", "")), data.get("_upgradeCount", 0), runtime)
                for field in fields:
                    if field.startswith("semantic:effect-type:"):
                        histogram[field.removeprefix("semantic:effect-type:")] += 1
                    elif field.startswith("semantic:n:effective.") and ":exact:" in field:
                        histogram["card-parameter:" + field.removeprefix("semantic:n:")] += 1
            for family, count in sorted(histogram.items()):
                tokens.extend(number_tokens(f"zone.{zone}.{family}.cards", count))
        status = state.get("status")
        refs = state.get("references")
        if not isinstance(status, Mapping) or not isinstance(refs, Mapping):
            tokens.append("active-status:missing")
        else:
            by_rid = {row.get("rid"): row for row in refs.get("RefIds", ()) if isinstance(row, Mapping)}
            for index, link in enumerate(status.get("_effectList", ())):
                rid = link.get("rid") if isinstance(link, Mapping) else None
                if rid not in by_rid:
                    raise ValueError(f"active status reference missing: {rid}")
                row = by_rid[rid]
                tokens.append(f"active-status[{index}]:class:{row.get('type', {}).get('class', 'unknown')}")
                tokens.extend(_native_status_tokens(f"active-status[{index}]", row.get("data", {})))
        current_turn = state.get("currentTurn", state.get("current_turn", 0))
        for index, native in enumerate(state.get("gimmickList", ())):
            if not isinstance(native, Mapping):
                raise ValueError("native gimmick row malformed")
            group, effect = native.get("gimmickGroupId"), native.get("gimmickEffectId")
            matches = [row for row in self.payload["gimmicks"].get(group, ())
                       if row["effect_id"] == effect and row["turn"] == native.get("turn")]
            if len(matches) != 1:
                raise ValueError(f"native gimmick outside frozen catalog: {group}:{effect}:{native.get('turn')}")
            tokens.extend(f"schedule[{index}]:{token}" for token in matches[0]["tokens"])
            tokens.extend(number_tokens(f"schedule[{index}].turns-until", native["turn"] - current_turn))
        if self.schema in _LIFECYCLE_SCHEMAS:
            tokens.extend(_tokens("action-window", _action_window(self.payload, state)))
        return tuple("semantic-context:" + value for value in tokens)

    def action_context_tokens(self, kind: str, state: Mapping[str, object], *, drink_id: str = "") -> tuple[str, ...]:
        """V3 facts and role/state interactions, never a prescribed drink bonus.

        V1/V2 intentionally emit nothing here. No future state, label, reward,
        model probability or simulated result participates in this encoder.
        """
        if self.schema not in _LIFECYCLE_SCHEMAS:
            return ()
        window = _action_window(self.payload, state)
        if self.uses_current_state_features:
            from .active_state_features import extract_active_state_features
            observed = extract_active_state_features(state)
            window = {**window, "current": dict(observed.current_values), "graph_complete": observed.graph_complete}
        value = {"base_card_play_consumption": 0 if kind == "drink" else 1 if kind == "play" else None,
                 "bottle_consumption": int(kind == "drink"), "window": window}
        tokens = _tokens("action." + kind, value)
        if kind != "drink":
            return tuple("semantic-action:" + token for token in tokens)
        drink = self.payload["drinks"].get(drink_id)
        if drink is None:
            raise ValueError("drink outside frozen v3 catalog")
        families = {token.removeprefix("effect-type:") for token in drink["tokens"] if token.startswith("effect-type:")}
        hand_families = []
        for runtime in state.get("handList", ()):
            data = runtime.get("_cardData", {})
            fields = self.card_tokens(str(data.get("_id", "")), data.get("_upgradeCount", 0), runtime)
            hand_families.append({token.removeprefix("semantic:effect-type:") for token in fields if token.startswith("semantic:effect-type:")})
        # These are known effect-family relationships, not a value multiplier.
        # A conditional graph match means possible payoff, not that it fires now.
        stock = {"stamina_gap": window.get("stamina_gap"), "block": state.get("block"),
                 "review": observed.current_values["review"] if self.uses_current_state_features else _active_resource(state, "ReviewStatusEffect", "_turn"),
                 "aggressive": observed.current_values["aggressive"] if self.uses_current_state_features else _active_resource(state, "AggressiveStatusEffect", "_value")}
        for family in sorted(families):
            targets = _DRINK_PAYOFF_FAMILIES.get(family)
            interaction = {"window": window, "observed_resources": stock,
                           "payoff_mapping_known": targets is not None,
                           "hand_cards_with_possible_payoff": None if targets is None else sum(bool(items & targets) for items in hand_families)}
            tokens.extend(_tokens("drink-role." + family, interaction))
        return tuple("semantic-action:" + token for token in tokens)


_STAGE_TYPES = ("ProduceStepType_AuditionMid1", "ProduceStepType_AuditionMid2", "ProduceStepType_AuditionFinal")
_DRINK_PAYOFF_FAMILIES = {
    "ProduceExamEffectType_ExamReview": frozenset({"ProduceExamEffectType_ExamLessonDependExamReview", "ProduceExamEffectType_ExamReviewValueMultiple"}),
    "ProduceExamEffectType_ExamBlock": frozenset({"ProduceExamEffectType_ExamLessonDependBlock", "ProduceExamEffectType_ExamReviewDependExamBlock"}),
    "ProduceExamEffectType_ExamCardPlayAggressive": frozenset({"ProduceExamEffectType_ExamLessonDependExamCardPlayAggressive", "ProduceExamEffectType_ExamBlock", "ProduceExamEffectType_ExamBlockAddMultipleAggressive"}),
    "ProduceExamEffectType_ExamLesson": frozenset(),
    "ProduceExamEffectType_ExamLessonFix": frozenset(),
    "ProduceExamEffectType_ExamStaminaRecoverFix": frozenset(),
}


def _build_mode_lifecycle(auxiliary: Mapping[str, object]) -> dict[str, object]:
    result = {}
    for identity, row in auxiliary["produce"].items():
        stages = {item["stepType"] for item in auxiliary["produce_audition"].values() if item["produceId"] == identity}
        if stages - set(_STAGE_TYPES):
            raise ValueError("unknown audition stage in frozen mode lifecycle")
        result[identity] = {"stages": [stage for stage in _STAGE_TYPES if stage in stages],
                            "split_type": row.get("produceSplitType"), "pair_produce_id": row.get("splitPairProduceId") or None}
    return result


def _action_window(payload: Mapping[str, object], state: Mapping[str, object]) -> dict[str, object]:
    mode = payload["mode_lifecycle"].get(state.get("produceId", state.get("produce_id")))
    raw_stage = state.get("stepType", state.get("step_type_value"))
    stage = {16: _STAGE_TYPES[0], 17: _STAGE_TYPES[1], 18: _STAGE_TYPES[2]}.get(raw_stage, raw_stage)
    stages = [] if mode is None else mode["stages"]
    remaining = state.get("remainTurn", state.get("remain_turn"))
    stamina, maximum = state.get("stamina"), state.get("maxStamina", state.get("max_stamina"))
    inventory = state.get("drinkList")
    limit = state.get("produceDrinkPossessLimit")
    return {"future_auditions_in_current_produce": len(stages) - stages.index(stage) - 1 if stage in stages else None,
            "paired_produce": None if mode is None else bool(mode["pair_produce_id"]),
            "remaining_turns": remaining, "last_turn": remaining == 1 if type(remaining) is int else None,
            "stamina_gap": max(0, maximum - stamina) if type(stamina) is int and type(maximum) is int else None,
            "inventory_count": len(inventory) if isinstance(inventory, list) else None,
            "inventory_full": len(inventory) >= limit if isinstance(inventory, list) and type(limit) is int and limit > 0 else None}


def _active_resource(state: Mapping[str, object], class_name: str, field: str):
    status, references = state.get("status"), state.get("references")
    if not isinstance(status, Mapping) or not isinstance(references, Mapping):
        return None
    active = {row.get("rid") for row in status.get("_effectList", ()) if isinstance(row, Mapping)}
    rows = [row.get("data", {}) for row in references.get("RefIds", ()) if isinstance(row, Mapping)
            and row.get("rid") in active and row.get("type", {}).get("class") == class_name]
    if not rows:
        return 0
    value = rows[0].get(field)
    return value if len(rows) == 1 and type(value) is int and value >= 0 else None


_GROW_TO_EFFECT = {
    "ReviewAdd": ("ExamReview", "effectValue1"), "AggressiveAdd": ("ExamCardPlayAggressive", "effectValue1"),
    "BlockAdd": ("ExamBlock", "effectValue1"), "LessonAdd": ("ExamLesson", "effectValue1"),
    "LessonCountAdd": ("ExamLesson", "effectCount"), "ParameterBuffAdd": ("ExamParameterBuff", "effectValue1"),
    "LessonBuffAdd": ("ExamLessonBuff", "effectValue1"),
    "LessonDependReviewAdd": ("ExamLessonDependExamReview", "effectValue1"),
    "FullPowerPointAdd": ("ExamFullPowerPoint", "effectValue1"),
}


def _customized_tokens(row: Mapping[str, object], counts: object, *, effective_complete: bool = True) -> list[str]:
    if counts is None:
        return ["effective:unavailable-customization-counts"]
    modifiers = []
    complete = effective_complete
    tokens = []
    for index, count in enumerate(counts):
        if not count:
            continue
        identity = row["customize_ids"][index]
        option = row["customization_rules"][identity].get(str(count))
        if option is None:
            raise ValueError(f"customization count outside frozen rules: {identity}:{count}")
        if option["overwrite"] != "ProduceCardGrowEffectType_Unknown":
            complete = False
        for grow in option["grows"]:
            tokens.extend(_tokens(f"modifier[{len(modifiers)}]", grow))
            kind = str(grow.get("effectType", "")).removeprefix("ProduceCardGrowEffectType_")
            neutral = all(not grow.get(key) for key in (
                "playProduceExamTriggerId", "playEffectProduceExamTriggerId", "targetPlayEffectProduceExamTriggerIds",
                "playProduceExamEffectId", "targetPlayProduceExamEffectIds", "produceCardStatusEnchantId", "effectGroupIds",
            )) and grow.get("playMovePositionType") == "ProduceCardMovePositionType_Unknown" and grow.get("costType") == "ExamCostType_Unknown"
            target_present = kind == "CostReduce" and row["base_scalars"].get("costType") == "ExamCostType_Unknown"
            if kind in _GROW_TO_EFFECT:
                target_present = any(effect.get("effectType") == "ProduceExamEffectType_" + _GROW_TO_EFFECT[kind][0]
                                     for effect in row["direct_effects"])
            complete &= neutral and target_present
            modifiers.append((kind, grow.get("value")))
    tokens.append(f"effective:complete:{int(complete)}")
    if not complete:
        return tokens
    card = dict(row["base_scalars"])
    effects = deepcopy(row["direct_effects"])
    for kind, value in modifiers:
        if type(value) is not int or value < 0:
            raise ValueError("frozen grow value is invalid")
        if kind == "CostReduce":
            for key in ("stamina", "forceStamina"):
                if key in card:
                    card[key] = max(0, card[key] - value)
        else:
            effect_kind, key = _GROW_TO_EFFECT[kind]
            for effect in effects:
                if effect.get("effectType") == "ProduceExamEffectType_" + effect_kind:
                    effect[key] = effect.get(key, 0) + value
    tokens.extend(_tokens("effective.card", card))
    for index, effect in enumerate(effects):
        tokens.extend(_tokens(f"effective.play[{index}]", {k: effect[k] for k in _EFFECT_SCALARS if k in effect}))
    return tokens


_NATIVE_STATUS_IGNORED = frozenset({
    "_uid", "_guid", "rid", "_id", "_statusEnchantId", "_startEnchantOwnerId", "_startEnchantOriginId",
    "_triggerCard", "_triggerDrink", "_triggerItem", "_triggerGimmickGroup", "_fromExamEffectIdList",
    "_descriptionReactiveDataTextList", "_descriptionReactiveDataTypeList",
})


# V4 materialization is separate: do not change historical V2/V3 numbers.
# These are direct, unconditional additive fields supported by existing card
# owners. Unknown grow algebra still supplies its exact observed graph tokens.
_V4_GROW_TARGETS = {
    "LessonAdd": ("ExamLesson", "effectValue1"), "LessonCountAdd": ("ExamLesson", "effectCount"),
    "BlockAdd": ("ExamBlock", "effectValue1"), "ReviewAdd": ("ExamReview", "effectValue1"),
    "AggressiveAdd": ("ExamCardPlayAggressive", "effectValue1"), "LessonBuffAdd": ("ExamLessonBuff", "effectValue1"),
    "FullPowerPointAdd": ("ExamFullPowerPoint", "effectValue1"),
}


def _v4_card_tokens(payload, card_id, upgrade, runtime, *, observed_runtime_grow_contract=None):
    from .active_state_features import observed_payload_tokens
    ref = f"{card_id}@{upgrade}"
    row = payload["cards"].get(ref)
    if row is None:
        return ("semantic:s:card.id:"+card_id, f"semantic:n:card.upgrade:exact:{upgrade}",
                "semantic:gap:card-outside-frozen-catalog")
    result = list(row["tokens"])
    if not isinstance(runtime, Mapping):
        return tuple("semantic:"+t for t in (*result,"gap:card-runtime-missing"))
    runtime_fields = ("_baseUpgradeCount", "_tmpUpgradeCount", "_supportUpgradeIdList", "_playCount",
        "_statusEffect", "_affectGrowEffectIdList", "_growEffectExamStartAfterList", "_staminaConsumptionSpecifyEffectList",
        "_isMoveProduceExamEffectUseInTurn")
    for key in runtime_fields:
        if key in runtime:
            result.extend(observed_payload_tokens("runtime."+key.lstrip("_"), runtime[key]))
        else:
            result.append("gap:runtime-field-missing:"+key)
    data = runtime.get("_cardData")
    counts = data.get("_customizeCountList") if isinstance(data, Mapping) else None
    result.extend(observed_payload_tokens("runtime.customization-counts", counts))
    modifiers = []
    complete = True
    if not isinstance(counts, list) or any(type(value) is not int or value < 0 for value in counts):
        result.append("gap:customization-counts-invalid-or-missing")
        complete = False
    else:
        for index,count in enumerate(counts):
            if not count:
                continue
            if index >= len(row["customize_ids"]):
                result.append(f"gap:customization-outside-frozen-catalog:{index}")
                complete = False
                continue
            custom = row["customize_ids"][index]
            option = row["customization_rules"][custom].get(str(count))
            if option is None:
                result.append(f"gap:customization-level-unresolved:{index}:{count}")
                complete = False
                continue
            result.extend(_tokens(f"customization[{index}].overwrite", option["overwrite"]))
            if option["overwrite"] != "ProduceCardGrowEffectType_Unknown":
                complete = False
                result.append("gap:effective-customization-overwrite-unresolved")
            modifiers.extend(option["grows"])
    runtime_modifier_start = len(modifiers)
    if observed_runtime_grow_contract is not None:
        from .observed_runtime_grow import adapt_observed_runtime_grows
        observed = adapt_observed_runtime_grows(runtime, payload=payload, contract=observed_runtime_grow_contract)
        modifiers.extend(observed.normalized_grows)
        result.extend('gap:'+gap for gap in observed.gaps)
        if observed.gaps:
            complete = False
    else:
        for key in ("_affectGrowEffectIdList", "_growEffectExamStartAfterList"):
            raw_grows = runtime.get(key)
            if not isinstance(raw_grows,list):
                complete = False
                continue
            for identity in raw_grows:
                grow = payload["grow_effects"].get(identity) if isinstance(identity,str) else None
                if grow is None:
                    complete = False
                    result.append("gap:effective-runtime-grow-unresolved:"+key)
                else:
                    modifiers.append(grow)
                    complete = False
                    result.append("gap:effective-runtime-grow-application-order-unresolved:"+key)
    # Native card status/grow/specified-cost can have graph-specific limits.
    # Record its exact fields, but do not advertise a total effective value
    # when that graph is active and not materialized by this contract.
    status = runtime.get("_statusEffect")
    if (not isinstance(status,Mapping) or status.get("_id") or status.get("_produceCardGrowEffectIdList")
            or runtime.get("_staminaConsumptionSpecifyEffectList")):
        complete = False
        result.append("gap:effective-native-card-status-unresolved")
    scalars, effects = dict(row["base_scalars"]), deepcopy(row["direct_effects"])
    for index,grow in enumerate(modifiers):
        result.extend(_tokens(f"modifier[{index}]", grow))
        result.extend(payload.get("added_grow_features",{}).get(grow.get("id"), ()))
        kind = str(grow.get("effectType", "")).removeprefix("ProduceCardGrowEffectType_")
        if observed_runtime_grow_contract is not None and index >= runtime_modifier_start:
            from .observed_runtime_grow import direct_runtime_fold_gaps
            runtime_gaps = direct_runtime_fold_gaps(kind, effects, grow.get('value'))
            result.extend('gap:'+gap for gap in runtime_gaps)
            if runtime_gaps:
                complete = False
        neutral = all(not grow.get(key) for key in (
            "playProduceExamTriggerId", "playEffectProduceExamTriggerId", "targetPlayEffectProduceExamTriggerIds",
            "playProduceExamEffectId", "targetPlayProduceExamEffectIds", "produceCardStatusEnchantId", "effectGroupIds"))
        neutral &= grow.get("playMovePositionType") == "ProduceCardMovePositionType_Unknown" and grow.get("costType") == "ExamCostType_Unknown"
        value = grow.get("value")
        if not neutral or type(value) is not int or value < 0:
            complete = False
            result.append("gap:effective-grow-contract-unresolved:"+kind)
            continue
        if kind in {"CostReduce", "CostPenetrateReduce"}:
            target = "stamina" if kind == "CostReduce" else "forceStamina"
            if scalars.get("costType") != "ExamCostType_Unknown" or type(scalars.get(target)) is not int:
                complete = False
                result.append("gap:effective-cost-type-unresolved:"+kind)
            else:
                scalars[target] = max(0, scalars[target]-value)
        elif kind in {"CostLessonBuffReduce", "CostParameterBuffReduce"}:
            expected = {"CostLessonBuffReduce":"ExamCostType_ExamLessonBuff",
                        "CostParameterBuffReduce":"ExamCostType_ExamParameterBuff"}[kind]
            if scalars.get("costType") != expected or type(scalars.get("costValue")) is not int:
                complete = False
                result.append("gap:effective-cost-type-unresolved:"+kind)
            else:
                scalars["costValue"] = max(0, scalars["costValue"]-value)
        elif kind in _V4_GROW_TARGETS:
            target,field = _V4_GROW_TARGETS[kind]
            matching = [effect for effect in effects if effect.get("effectType") == "ProduceExamEffectType_"+target]
            if not matching:
                complete = False
                result.append("gap:effective-grow-target-unresolved:"+kind)
            for effect in matching:
                effect[field] = effect.get(field,0)+value
        else:
            complete = False
            result.append("gap:effective-grow-algebra-unresolved:"+kind)
    result.append("effective:complete:"+str(int(complete)))
    if complete:
        result.extend(_tokens("effective.card",scalars))
        for index,effect in enumerate(effects):
            result.extend(_tokens(f"effective.play[{index}]",{key:effect[key] for key in _EFFECT_SCALARS if key in effect}))
    return tuple(dict.fromkeys("semantic:"+token for token in result))


def _v4_context_tokens(features, state, *, active_status_contract=None):
    from .active_state_features import extract_active_state_features
    observed = extract_active_state_features(state, qualified_status_extension=active_status_contract)
    result = list(observed.tokens)
    for zone in ("handList", "deckList", "graveList", "lostList", "holdList"):
        rows = state.get(zone)
        if not isinstance(rows,list):
            continue  # Missing-zone token and gap are emitted by the shared extractor.
        histogram = Counter()
        for index,card in enumerate(rows):
            if not isinstance(card, Mapping) or not isinstance(card.get("_cardData"),Mapping):
                result.append(f"semantic-context:gap:{zone}[{index}]:card-identity-missing")
                continue
            data = card["_cardData"]
            tokens = features.card_tokens(str(data.get("_id","")), data.get("_upgradeCount"), card)
            for token in tokens:
                if token.startswith(("semantic:effect-type:", "semantic:gap:", "semantic:effective:",
                                     "semantic:n:effective.", "semantic:n:card.", "semantic:s:card.costType:", "semantic:s:card.category:")):
                    histogram[token] += 1
                if zone == "handList" and token.startswith(("semantic:n:runtime.", "semantic:s:runtime.", "semantic:n:modifier[")):
                    result.append(f"semantic-context:hand[{index}]:"+token)
        result.extend(f"semantic-context:zone:{zone}:{token}:cards:{count}" for token,count in sorted(histogram.items()))
    for index,native in enumerate(state.get("gimmickList",()) if isinstance(state.get("gimmickList",()),list) else ()):
        if not isinstance(native,Mapping):
            result.append(f"semantic-context:gap:schedule[{index}]:native-row-invalid")
            continue
        group,effect = native.get("gimmickGroupId"),native.get("gimmickEffectId")
        matches = [row for row in features.payload["gimmicks"].get(group,()) if row["effect_id"]==effect and row["turn"]==native.get("turn")]
        if len(matches)==1:
            result.extend("semantic-context:"+token for token in matches[0]["tokens"])
        else:
            result.append(f"semantic-context:gap:schedule[{index}]:outside-frozen-catalog")
            result.extend("semantic-context:"+token for token in _tokens(f"schedule[{index}]",dict(native)))
        if type(native.get("turn")) is int and type(state.get("currentTurn")) is int:
            result.extend("semantic-context:"+token for token in number_tokens(f"schedule[{index}].turns-until",native["turn"]-state["currentTurn"]))
    result.extend("semantic-context:"+token for token in _tokens("action-window",_action_window(features.payload,state)))
    return tuple(result)


@dataclass(frozen=True, slots=True)
class ActiveStatusSemanticFeatures(CardSemanticFeatures):
    """Compose status recognition with the already selected card/grow encoder."""
    base: CardSemanticFeatures
    active_status_contract: Mapping

    def card_tokens(self, card_id, upgrade, runtime):
        return self.base.card_tokens(card_id, upgrade, runtime)

    def context_tokens(self, state):
        return _v4_context_tokens(self, state, active_status_contract=self.active_status_contract)


def _native_status_tokens(prefix: str, value: object, depth: int = 0) -> list[str]:
    if depth > 12:
        raise ValueError("native status feature depth exceeded")
    if isinstance(value, Mapping):
        return [token for key, child in sorted(value.items()) if key not in _NATIVE_STATUS_IGNORED
                for token in _native_status_tokens(prefix + "." + key.lstrip("_"), child, depth + 1)]
    if isinstance(value, list):
        return [token for index, child in enumerate(value)
                for token in _native_status_tokens(f"{prefix}[{index}]", child, depth + 1)]
    return _tokens(prefix, value)


def validate_card_feature_encoding(contract: object, encoding: object) -> None:
    if contract is None:
        if isinstance(encoding, str) and encoding.startswith("field-token-bag+card-semantic"):
            raise ValueError("card feature encoding/contract mismatch")
    elif not isinstance(contract, Mapping) or _ENCODINGS.get(contract.get("schema")) != encoding:
        raise ValueError("card feature encoding/contract mismatch")


@lru_cache(maxsize=8)
def _load_cached(path: str, expected_sha: str, modified_ns: int, size: int) -> CardSemanticFeatures:
    source = Path(path)
    if sha256_file(source) != expected_sha:
        raise ValueError("card semantic feature file hash mismatch")
    payload = json.loads(source.read_text(encoding="utf-8"))
    if payload.get("schema") not in _ENCODINGS or not isinstance(payload.get("cards"), dict) or not isinstance(payload.get("drinks"), dict):
        raise ValueError("card semantic feature schema mismatch")
    result = CardSemanticFeatures(payload)
    result.current_state_schema  # Validate the new contract, without altering old schemas.
    return result


def load_card_semantic_features(contract: object, *, relative_to: Path | None = None) -> CardSemanticFeatures | None:
    if contract is None:
        return None
    if not isinstance(contract, Mapping) or contract.get("schema") not in _ENCODINGS:
        raise ValueError("unsupported card semantic feature contract")
    path = Path(str(contract["features_path"]))
    if not path.is_absolute():
        if relative_to is None:
            raise ValueError("relative feature path has no artifact root")
        path = relative_to / path
    elif not path.is_file() and relative_to is not None:
        # Older job specs recorded an absolute location.  Relocation may use
        # the adjacent same-name file, but the exact content hash still owns
        # the data; current Master is never substituted.
        path = relative_to / path.name
    stat = path.stat()
    features = _load_cached(str(path.resolve()), str(contract["features_sha256"]), stat.st_mtime_ns, stat.st_size)
    if features.schema != contract["schema"]:
        raise ValueError("card semantic payload/contract schema mismatch")
    if features.payload["snapshot_fingerprint"] != contract.get("snapshot_fingerprint"):
        raise ValueError("card semantic snapshot fingerprint mismatch")
    return features


def copy_card_feature_contract(contract: object, output_root: Path, *, relative_to: Path | None = None) -> dict[str, object] | None:
    features = load_card_semantic_features(contract, relative_to=relative_to)
    if features is None:
        return None
    target = output_root / "card_semantic_features.json"
    atomic_write(target, canonical_json_bytes(features.payload) + b"\n")
    return {"schema": features.schema, "snapshot_fingerprint": features.payload["snapshot_fingerprint"],
            "features_path": target.name, "features_sha256": sha256_file(target)}


__all__ = ["FEATURE_SCHEMA", "FEATURE_SCHEMA_V1", "FEATURE_SCHEMA_V2", "FEATURE_SCHEMA_V3", "SEMANTIC_ENCODING", "SEMANTIC_ENCODING_V1", "SEMANTIC_ENCODING_V2", "SEMANTIC_ENCODING_V3",
           "CardSemanticFeatures", "build_card_semantic_features", "copy_card_feature_contract",
           "load_card_semantic_features", "number_tokens", "validate_card_feature_encoding", "write_card_semantic_features"]
