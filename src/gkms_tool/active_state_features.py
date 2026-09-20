"""Versioned, pure features from the *current* native ExamSave status graph.

No Master reads, simulator, historical TurnStart fallback, or legality owner.
Unrecognized payloads keep their observed scalar tokens and a precise gap.
GUIDs/RIDs/UIDs bind references locally and never become learned identities.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path

SCHEMA = "gkms.active-state-features.v2"
FULLPOWER_ADDITIVE_PROOF_SHA256 = "c8999634eedcde9b575b2a9a29cd5ea3557c2d34e604e34da338ccc22f788082"
_ADDITIVE_CLASS = "FullPowerPointAdditiveStatusEffect"
_ADDITIVE_FIELDS = frozenset({"_value", "_turn", "_isTurnLimited", "_isPassingTurnStart", "_uid"})


def _status_extension_body():
    return {"schema": "gkms.source-proven-active-status-recognition.v1",
        "proof_sha256": FULLPOWER_ADDITIVE_PROOF_SHA256,
        "native_core_sha256": "1e730b223cab48c3cf80837d7cd04b1b6b6eb04452135fb01c7d655aaf52017a",
        "native_metadata_sha256": "9a6bf153c0c42a2768e619cc7d96fa79fcc1d341e9cf9bcbdc8d6bca48812668",
        "type": {"class": _ADDITIVE_CLASS, "ns": "Campus.InGame.Exam", "asm": "Assembly-CSharp"},
        "fields": sorted(_ADDITIVE_FIELDS), "native_type_value": 61,
        "meaning": "point-acquisition-additive-permil; not current point balance",
        "projection": "preserve all existing scalar tokens; recognize this typed class only",
        "new_resource_aggregation": False, "raw_state_changed": False, "training_admitted": False}


def active_status_extension_contract(*, proof_path):
    """Bind the original PC constructor/type/getter proof once at preparation."""
    raw = Path(proof_path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != FULLPOWER_ADDITIVE_PROOF_SHA256:
        raise ValueError("Original PC active-status proof differs")
    proof = json.loads(raw)
    expected = _status_extension_body()
    if (proof['source_image']['sha256'] != expected['native_core_sha256']
            or proof['source_metadata']['sha256'] != expected['native_metadata_sha256']
            or proof['class']['native_status_enum_value'] != 61):
        raise ValueError("Original PC active-status type/consumer binding differs")
    return expected


def validate_active_status_extension(contract):
    try:
        matches = isinstance(contract, Mapping) and json.dumps(dict(contract), sort_keys=True, allow_nan=False) == json.dumps(
            _status_extension_body(), sort_keys=True, allow_nan=False)
    except (TypeError, ValueError):
        matches = False
    if not matches:
        raise ValueError("Active-status recognition requires the exact original PC proof contract")


def _extension_recognizes(row, data):
    # This only recognizes the observed class. It does not sum modifiers or
    # add the 500 permil example to the current full-power point balance.
    return (row.get('type') == _status_extension_body()['type'] and set(data) == _ADDITIVE_FIELDS
        and all(type(data[k]) is int and -(2**31) <= data[k] < 2**31 for k in ('_value', '_turn', '_uid'))
        and all(type(data[k]) is bool for k in ('_isTurnLimited', '_isPassingTurnStart')))
_RESOURCES = {
    "focus": ("LessonBuffStatusEffect", "_value"),
    "good_condition_turns": ("ParameterBuffStatusEffect", "_turn"),
    "excellent_condition_turns": ("ParameterBuffMultiplePerTurnStatusEffect", "_turn"),
    # Review's native getter uses inherited _turn as its stack value. Unlike
    # Aggressive it has no _value field (ReviewStatusEffect ctor(int turn)).
    "review": ("ReviewStatusEffect", "_turn"),
    "aggressive": ("AggressiveStatusEffect", "_value"),
    "enthusiasm": ("EnthusiasticStatusEffect", "_value"),
    "full_power_points": ("FullPowerPointStatusEffect", "_value"),
    "additional_plays": ("PlayableValueAddStatusEffect", "_value"),
    "anti_debuff_count": ("AntiDebuffStatusEffect", "_count"),
}
_KNOWN_CLASSES = frozenset(kind for kind, _ in _RESOURCES.values()) | frozenset({
    "TriggerEffectStatusEffect", "LessonParameterMultipleStatusEffect", "LessonParameterMultipleDownStatusEffect",
    "LessonBuffAdditiveStatusEffect", "LessonBuffMultipleStatusEffect", "ReviewAdditiveStatusEffect",
    "AggressiveAdditiveStatusEffect", "EnthusiasticAdditiveStatusEffect", "StaminaConsumptionDownStatusEffect",
    "StaminaConsumptionAddStatusEffect", "StaminaConsumptionDownFixStatusEffect", "BlockRestrictionStatusEffect",
    "SearchPlayCardStaminaConsumptionChangeStatusEffect", "CardSearchEffectPlayCountBuffStatusEffect",
    "PlayCountBuffStatusEffect", "EnthusiasticMultipleStatusEffect",
    "BuffConsumptionDownStatusEffect", "BuffConsumptionAddStatusEffect",
})
_IDENTITIES = frozenset({"rid", "_uid", "_guid", "guid", "_statusUid", "_sourceUid", "_ownerUid"})
_DISPLAY = frozenset({"_descriptionReactiveDataTextList", "_descriptionReactiveDataTypeList", "_produceCardSkinId", "_produceCardSkinAssetId"})
_SOURCES = frozenset({"_triggerCard", "_triggerDrink", "_triggerItem", "_triggerGimmickGroup"})
_KNOWN_FIELDS = frozenset({
    "_uid", "_value", "_count", "_turn", "_isTurnLimited", "_isPassingTurnStart", "_turnCount",
    "_limitCount", "_limitCountInTurn", "_limitCountInTurnRemain", "_phaseCountDictionary", "_trigger", "_effectList",
    "_statusEnchantId", "_isItemDirectEnchant", "_isEncoreEnchant", "_isFromEnchantEffect", "_originType", "_overrideType",
    "_startEnchantOriginId", "_startEnchantOriginLevel", "_startEnchantOriginType", "_startEnchantOwnerId",
    "_fromExamEffectIdList", "_fromExamEffectId", "_searchId", "_targetType", "_isDebuff", "_currentValue",
    *_SOURCES, *_DISPLAY,
})
_ROOT_SCALARS = (
    "produceId", "planType", "mainEffectType", "examType", "stepType", "phase", "currentTurn", "limitTurn", "remainTurn",
    "extraTurn", "parameter", "stamina", "maxStamina", "block", "examCardPlayCount", "turnCardPlayCount",
    "isTurnCardPlayEnd", "isExamEndComplete", "vocalConfigParameter", "danceConfigParameter", "visualConfigParameter",
    "vocalBonusPermil", "danceBonusPermil", "visualBonusPermil", "clearBorder", "limitBorder", "produceDrinkPossessLimit",
    "currentTurnTotalAddParameter", "currentTurnTotalConsumeStamina", "totalDrawCardCount",
    "blockConsumptionSumCount", "reviewConsumptionSumCount",
)


@dataclass(frozen=True)
class ActiveStateFeatureGap:
    code: str
    path: str


@dataclass(frozen=True)
class ActiveStateFeatures:
    tokens: tuple[str, ...]
    current_values: Mapping[str, object]
    gaps: tuple[ActiveStateFeatureGap, ...]
    graph_complete: bool
    schema: str = SCHEMA

    @property
    def complete(self) -> bool:
        return not self.gaps

    def to_dict(self):
        return {"schema": self.schema, "tokens": list(self.tokens), "current_values": dict(self.current_values),
                "gaps": [{"code": gap.code, "path": gap.path} for gap in self.gaps],
                "complete": self.complete, "graph_complete": self.graph_complete}


def _value_tokens(path, value, gaps, depth=0):
    if depth > 12:
        gaps.append(ActiveStateFeatureGap("depth-exceeded", path))
        return [f"gap:{path}:depth-exceeded"]
    if value is None:
        return [f"missing:{path}"]
    if type(value) is bool:
        return [f"b:{path}:{int(value)}"]
    if type(value) in (int, float):
        if not math.isfinite(value):
            gaps.append(ActiveStateFeatureGap("nonfinite-number", path))
            return [f"gap:{path}:nonfinite-number"]
        exact = str(value) if type(value) is int else format(value, ".17g")
        return [f"n:{path}:exact:{exact}", f"n:{path}:coarse:{math.floor(value / 8)}"]
    if isinstance(value, str):
        return [f"s:{path}:{value}"] if value else [f"empty:{path}"]
    if isinstance(value, Mapping):
        result = [f"object:{path}"]
        for key, child in sorted(value.items()):
            if key in _IDENTITIES or key in _DISPLAY:
                continue
            # A reference ID has no stable semantic identity. Nested refs are
            # explicitly surfaced rather than flattened into an ID token.
            if isinstance(child, Mapping) and set(child) == {"rid"}:
                gaps.append(ActiveStateFeatureGap("nested-reference-unresolved", path+"."+key))
                result.append(f"gap:{path}.{key}:nested-reference-unresolved")
            else:
                result.extend(_value_tokens(path+"."+key.lstrip("_"), child, gaps, depth+1))
        return result
    if isinstance(value, (list, tuple)):
        return [f"count:{path}:{len(value)}", *[token for index, child in enumerate(value)
                for token in _value_tokens(f"{path}[{index}]", child, gaps, depth+1)]]
    gaps.append(ActiveStateFeatureGap("invalid-value-type", path))
    return [f"gap:{path}:invalid-value-type"]


def observed_payload_tokens(prefix: str, value: object) -> tuple[str, ...]:
    """Stable scalar/structure features for GUID-local card runtime payloads."""
    gaps = []
    return tuple(_value_tokens(prefix, value, gaps))


def extract_active_state_features(raw_native: Mapping[str, object], *, legal_candidates: Sequence[Mapping] | None = None,
                                  qualified_status_extension: Mapping | None = None) -> ActiveStateFeatures:
    if not isinstance(raw_native, Mapping):
        raise TypeError("active features require the original native state mapping")
    if qualified_status_extension is not None:
        validate_active_status_extension(qualified_status_extension)
    gaps, tokens = [], ["schema:"+SCHEMA]
    current = {}
    for key in _ROOT_SCALARS:
        if key in raw_native:
            tokens.extend(_value_tokens("native."+key, raw_native[key], gaps))
    for key in ("phase", "planType", "isTurnCardPlayEnd", "isExamEndComplete"):
        if key not in raw_native:
            gaps.append(ActiveStateFeatureGap("missing-native-field", key))
    status, refs = raw_native.get("status"), raw_native.get("references")
    links = status.get("_effectList") if isinstance(status, Mapping) else None
    rows = refs.get("RefIds") if isinstance(refs, Mapping) else None
    graph_complete = isinstance(links, list) and isinstance(rows, list)
    by_rid = {}
    if not graph_complete:
        gaps.append(ActiveStateFeatureGap("active-graph-missing", "status._effectList/references.RefIds"))
    else:
        for row in rows:
            if not isinstance(row, Mapping) or type(row.get("rid")) is not int:
                continue  # Inactive malformed references do not replace active evidence.
            by_rid.setdefault(row["rid"], []).append(row)
    active, seen = [], set()
    for index, link in enumerate(links if isinstance(links, list) else ()):
        path = f"active[{index}]"
        rid = link.get("rid") if isinstance(link, Mapping) else None
        matches = by_rid.get(rid, ()) if type(rid) is int else ()
        if type(rid) is not int or rid in seen or len(matches) != 1:
            gaps.append(ActiveStateFeatureGap("active-reference-ambiguous-or-missing", path))
            tokens.append(f"gap:{path}:active-reference-ambiguous-or-missing")
            graph_complete = False
            continue
        seen.add(rid)
        row = matches[0]
        kind = row.get("type", {}).get("class") if isinstance(row.get("type"), Mapping) else None
        data = row.get("data")
        if not isinstance(kind, str) or not isinstance(data, Mapping):
            graph_complete = False
            gaps.append(ActiveStateFeatureGap("active-payload-missing", path))
            continue
        active.append((kind, data))
        tokens.append(f"class:{path}:{kind}")
        extended = qualified_status_extension is not None and _extension_recognizes(row, data)
        if kind not in _KNOWN_CLASSES and not extended:
            gaps.append(ActiveStateFeatureGap("unclassified-status", path+":"+kind))
        for key in sorted(set(data)-_KNOWN_FIELDS):
            gaps.append(ActiveStateFeatureGap("unknown-status-field", path+"."+key))
        for key in ("_turn", "_isTurnLimited", "_isPassingTurnStart"):
            value = data.get(key)
            valid = type(value) is int and value >= -1 if key == "_turn" else type(value) is bool
            if not valid:
                gaps.append(ActiveStateFeatureGap("status-lifetime-incomplete", path+"."+key))
        if type(data.get("_isTurnLimited")) is bool and type(data.get("_turn")) is int and data["_isTurnLimited"] != (data["_turn"] != -1):
            gaps.append(ActiveStateFeatureGap("status-lifetime-conflict", path))
        payload = {key:value for key,value in data.items() if key not in _SOURCES}
        tokens.extend(_value_tokens(path, payload, gaps))
        # Keep semantic parent type/card identity, not memory addresses or the
        # parent's copied stale runtime stats. Native order remains explicit.
        for source in sorted(_SOURCES):
            parent = data.get(source)
            if not isinstance(parent, Mapping):
                continue
            identity = parent.get("_cardData", {}).get("_id") if source == "_triggerCard" else parent.get("_id")
            if identity:
                tokens.append(f"source:{path}:{source}:{identity}")
    tokens.extend(f"class-count:{kind}:{count}" for kind,count in sorted(Counter(kind for kind,_ in active).items()))
    current["active_status_count"] = len(active) if graph_complete else None
    for name,(kind,field) in _RESOURCES.items():
        found = [data for cls,data in active if cls == kind]
        value = None
        if graph_complete and not found:
            value = 0
        elif graph_complete and len(found) == 1 and type(found[0].get(field)) is int and (
                found[0][field] >= 0 or field == "_turn" and found[0][field] == -1 and found[0].get("_isTurnLimited") is False):
            value = found[0][field]
        elif found:
            gaps.append(ActiveStateFeatureGap("resource-ambiguous-or-invalid", name))
        current[name] = value
        if field == "_turn":
            current[name+"_permanent"] = value == -1 if value is not None else None
    idol = status.get("_idolStatusEffect") if isinstance(status, Mapping) else None
    for key in ("_currentType", "_currentStep"):
        value = idol.get(key) if isinstance(idol, Mapping) else None
        current["idol"+key] = value if type(value) is int and value >= 0 else None
        if current["idol"+key] is None:
            gaps.append(ActiveStateFeatureGap("idol-state-missing", "status._idolStatusEffect."+key))
    if isinstance(idol, Mapping):
        tokens.extend(_value_tokens("idol", idol, gaps))
    # Existing plan-neutral native playable budget: the base play is consumed
    # via IsTurnCardPlayEnd, separately from active PlayableValueAdd. Do not
    # subtract TurnCardPlayCount again (an extra play also increments it).
    flag, terminal, phase = raw_native.get("isTurnCardPlayEnd"), raw_native.get("isExamEndComplete"), raw_native.get("phase")
    extra = current["additional_plays"]
    current["plays_remaining"] = ((0 if flag else 1)+extra if graph_complete and type(flag) is bool and type(extra) is int else None)
    current["available_main_plays"] = (0 if terminal is True or type(phase) is int and phase != 6 else
        current["plays_remaining"] if terminal is False and phase == 6 else None)
    if current["plays_remaining"] is None:
        gaps.append(ActiveStateFeatureGap("play-budget-unresolved", "plays_remaining"))
    for zone in ("handList", "deckList", "graveList", "lostList", "holdList", "drinkList"):
        values = raw_native.get(zone)
        current[zone+"_count"] = len(values) if isinstance(values,list) else None
        if not isinstance(values,list):
            gaps.append(ActiveStateFeatureGap("zone-missing", zone))
    if legal_candidates is not None:
        if not isinstance(legal_candidates, Sequence) or isinstance(legal_candidates, (str,bytes)):
            raise TypeError("legal candidates must be a supplied sequence")
        current["supplied_candidate_count"] = len(legal_candidates)
        tokens.extend(_value_tokens("supplied-candidate-kinds", dict(Counter(str(c.get("kind")) for c in legal_candidates)), gaps))
    tokens.extend(_value_tokens("current", current, gaps))
    unique_gaps = tuple(dict.fromkeys(gaps))
    tokens.extend(f"gap:{gap.path}:{gap.code}" for gap in unique_gaps)
    return ActiveStateFeatures(tuple(dict.fromkeys("active-v2:"+token for token in tokens)), current, unique_gaps, graph_complete)


__all__ = ["SCHEMA", "ActiveStateFeatures", "ActiveStateFeatureGap", "extract_active_state_features", "observed_payload_tokens"]
