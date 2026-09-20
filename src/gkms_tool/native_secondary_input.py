"""Pure original-PC secondary observations and separate ordered label binding.

The offered pool is authoritative; inventory is used only to bind its runtime
card payloads. Candidate-query after-state is never an action-after state.
Local shape/pointer consistency is not source, model, or training admission.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import math
import re
from types import MappingProxyType

from .canonical_training_labels import PLAN_BY_NATIVE_VALUE, EFFECT_BY_NATIVE_VALUE, STAGE_BY_NATIVE_VALUE
from .training_artifact_io import canonical_json_bytes

SCHEMA = "gkms.original-pc-secondary-policy-input.v1"
CONTEXT_SCHEMA = "gkms.original-pc-current-selector-context.v1"
NATIVE_METADATA_SHA256 = "9a6bf153c0c42a2768e619cc7d96fa79fcc1d341e9cf9bcbdc8d6bca48812668"
_SEAL = object()
# Original PC ProduceCardPositionType (not ProduceCardMovePositionType).
# In particular position Hold=13, while move destination Hold=7.
_POSITIONS = {0: "Unknown", 1: "None", 2: "Hand", 3: "Deck", 4: "Grave", 5: "Lost",
              6: "DeckAll", 7: "RandomPool", 8: "Playing", 9: "PlayHand", 11: "Target",
              12: "Self", 13: "Hold", 14: "DeckGrave", 15: "NotLost"}
_ZONES = {2: "handList", 3: "deckList", 4: "graveList", 5: "lostList", 13: "holdList"}
_CARD_FIELDS = ("_baseUpgradeCount", "_tmpUpgradeCount", "_supportUpgradeIdList", "_statusEffect",
                "_growEffectExamStartAfterList", "_affectGrowEffectIdList", "_playCount",
                "_staminaConsumptionSpecifyEffectList", "_isMoveProduceExamEffectUseInTurn")
_IDENTITY_FIELDS = {"base_upgrade_count": "_baseUpgradeCount", "temporary_upgrade_count": "_tmpUpgradeCount",
                    "fixed_deck_order": "_fixedDeckOrder", "play_count": "_playCount",
                    "support_upgrade_ids": "_supportUpgradeIdList", "affected_grow_effect_ids": "_affectGrowEffectIdList"}


def _freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    return value


def _plain(value):
    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [_plain(v) for v in value]
    return value


def _same(a, b):
    return canonical_json_bytes(_plain(a)) == canonical_json_bytes(_plain(b))


def _digest(value):
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _integer(value, name, *, minimum=0, maximum=2**31 - 1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(name + " requires an observed integer in range")
    return value


def _positive_pointer(value):
    return isinstance(value, str) and re.fullmatch(r"0x[0-9a-fA-F]+", value) is not None and int(value, 16) > 0


@dataclass(frozen=True, slots=True)
class SecondaryInputGap:
    code: str
    path: str


@dataclass(frozen=True, slots=True, init=False)
class NativeSecondaryInput:
    _record: Mapping
    gaps: tuple[SecondaryInputGap, ...]

    def __init__(self, record, gaps, *, _authority=None):
        if _authority is not _SEAL:
            raise ValueError("Use prepare_native_secondary_input")
        canonical_json_bytes(record)  # reject non-JSON/nonfinite payloads
        object.__setattr__(self, "_record", _freeze(record))
        object.__setattr__(self, "gaps", tuple(dict.fromkeys(gaps)))

    @property
    def state_before(self):
        return _plain(self._record["state_before"])

    @property
    def legal_candidates(self):
        return tuple(_plain(row) for row in self._record["legal_candidates"])

    @property
    def constraints(self):
        return _plain(self._record["constraints"])

    @property
    def current_context(self):
        return _plain(self._record["current_context"])

    @property
    def observation(self):
        return _plain(self._record["observation"])

    @property
    def complete(self):
        """Local input structure only; never training/source admission."""
        return not self.gaps


def _snapshot(value):
    if (not isinstance(value, Mapping) or value.get("schema") != "gkms.original-pc-machine-state-snapshot.v1"
            or value.get("mode") != "direct-native-projection" or value.get("state_complete") is not True
            or value.get("pure") is not True or value.get("read_errors") != [] or value.get("unqualified") != []
            or not isinstance(value.get("state"), Mapping) or value.get("state_sha256") != _digest(value["state"])):
        raise ValueError("Incomplete, mutated or unbound original secondary state")
    return value["state"]


def _current_context(event, supplied, gaps):
    from .native_secondary_binding import VerifiedStoredSecondaryCommandBinding, verified_stored_secondary_context
    if isinstance(supplied, VerifiedStoredSecondaryCommandBinding):
        if event.get("decision_context") is not None:
            raise ValueError("Conflicting stored and live selector context observations")
        context, binding_gaps = verified_stored_secondary_context(supplied, event)
        gaps.extend(SecondaryInputGap(code, path) for code, path in binding_gaps)
        return _current_context_references(context, gaps)
    context = event.get("decision_context") if supplied is None else supplied
    if supplied is not None and event.get("decision_context") is not None and not _same(supplied, event["decision_context"]):
        raise ValueError("Conflicting current selector context observations")
    if context is None:
        gaps.append(SecondaryInputGap("current-command-binding-missing", "decision_context"))
        return None
    binding = context.get("pointer_binding") if isinstance(context, Mapping) else None
    if (not isinstance(context, Mapping) or context.get("schema") != CONTEXT_SCHEMA
            or context.get("complete") is not True or context.get("read_errors") != []
            or context.get("unqualified") != [] or context.get("teacher_log_used") is not False
            or not isinstance(context.get("command"), Mapping) or not isinstance(context.get("effect_context"), Mapping)
            or context.get("command_source") != "actual-OnEffectSelectParameterAsync-command-argument"
            or context.get("effect_field_path") != "_playEffect"
            or not isinstance(binding, Mapping)
            or binding.get("authority") != "actual owned native selector callback arguments"
            or binding.get("context_parameter_matches_sequence") is not True
            or any(not _positive_pointer(event.get(event_key)) or binding.get(context_key) != event[event_key]
                for event_key, context_key in (("command_identity", "command"),
                    ("effect_context_identity", "effect_context"), ("parameter_identity", "parameter")))):
        gaps.append(SecondaryInputGap("current-command-binding-unqualified", "decision_context"))
        return None
    command = context["command"]
    required = ("_playType", "_isCardSelect", "_isCardSelect2", "_playEffect", "_cardSelectSearchId", "_cardSelectSearchId2")
    if (any(k not in command for k in required) or type(command.get("_playType")) is not int
            or any(type(command.get(k)) is not bool for k in ("_isCardSelect", "_isCardSelect2"))
            or command["_playEffect"] is not None and not isinstance(command["_playEffect"], Mapping)
            or any(command[k] is not None and not isinstance(command[k], str) for k in ("_cardSelectSearchId", "_cardSelectSearchId2"))):
        gaps.append(SecondaryInputGap("current-command-payload-incomplete", "decision_context.command"))
        return None
    return _current_context_references(context, gaps)


def _current_context_references(context, gaps):
    references = context.get("references")
    rows = references.get("RefIds") if isinstance(references, Mapping) else None
    if not isinstance(rows, list):
        gaps.append(SecondaryInputGap("current-context-reference-table-missing", "decision_context.references"))
        return None
    by_rid = {}
    for row in rows:
        if (not isinstance(row, Mapping) or type(row.get("rid")) is not int or row["rid"] in by_rid
                or not isinstance(row.get("type"), Mapping) or not isinstance(row.get("data"), Mapping)):
            gaps.append(SecondaryInputGap("current-context-reference-invalid", "decision_context.references"))
            return None
        by_rid[row["rid"]] = row
    stack = [(context["command"], "decision_context.command"), (context["effect_context"], "decision_context.effect_context")]
    visited = set()
    while stack:
        value, path = stack.pop()
        if isinstance(value, Mapping):
            if set(value) == {"rid"}:
                rid = value["rid"]
                if type(rid) is not int or rid >= 0 and rid not in by_rid:
                    gaps.append(SecondaryInputGap("current-context-reference-unresolved", path))
                    return None
                if rid in by_rid and rid not in visited:
                    visited.add(rid); stack.append((by_rid[rid]["data"], path + ".data"))
            else:
                stack.extend((child, path + "." + key) for key, child in value.items())
        elif isinstance(value, list):
            stack.extend((child, f"{path}[{i}]") for i, child in enumerate(value))
    return _plain(_freeze(context))


def _candidate(identity, ordinal, state, gaps):
    path = f"candidates[{ordinal}]"
    if not isinstance(identity, Mapping):
        raise ValueError("Secondary candidate must be an observed object")
    if (type(identity.get("offered_ordinal")) is not int or identity["offered_ordinal"] != ordinal
            or identity.get("card_data_present") is not True or not isinstance(identity.get("id"), str) or not identity["id"]
            or "guid" not in identity or identity["guid"] is not None and not isinstance(identity["guid"], str)
            or identity.get("runtime_class") != "Campus.InGame.Card.ExamCardData"):
        raise ValueError("Secondary offered ordinal/card identity is incomplete or inconsistent")
    for key in ("slot_index", "occurrence_index"):
        if key in identity and (type(identity[key]) is not int or identity[key] != ordinal):
            raise ValueError("Observed offered ordinal aliases disagree")
    upgrade = _integer(identity.get("raw_upgrade_count"), "secondary upgrade")
    zone_type = _integer(identity.get("native_zone_type"), "native zone type")
    zone_index = _integer(identity.get("native_zone_index"), "native zone index", minimum=-(2**31))
    if zone_type in _POSITIONS and identity.get("native_zone_name") != _POSITIONS[zone_type]:
        raise ValueError("Native zone enum/name differs from original PC metadata")
    evaluation = identity.get("evaluation")
    if evaluation is not None and (type(evaluation) not in (int, float) or not math.isfinite(evaluation)):
        raise ValueError("Observed candidate evaluation is invalid")
    field = _ZONES.get(zone_type)
    runtime = None
    if field is None:
        gaps.append(SecondaryInputGap("candidate-runtime-zone-unqualified", path))
    else:
        zone = state.get(field)
        if not isinstance(zone, list):
            gaps.append(SecondaryInputGap("candidate-runtime-zone-unobserved", path + "." + field))
        elif not 0 <= zone_index < len(zone):
            raise ValueError("Secondary native zone index is outside the observed current zone")
        else:
            runtime = zone[zone_index]
            data = runtime.get("_cardData") if isinstance(runtime, Mapping) else None
            if (not isinstance(data, Mapping) or "_guid" not in runtime
                    or data.get("_id") != identity["id"] or type(data.get("_upgradeCount")) is not int
                    or data["_upgradeCount"] != upgrade or runtime["_guid"] != identity["guid"]):
                raise ValueError("Offered candidate does not match its current runtime card position")
            if "native_customize_counts" not in identity or "_customizeCountList" not in data:
                gaps.append(SecondaryInputGap("candidate-native-PT-counts-unobserved", path))
            elif not _same(identity["native_customize_counts"], data["_customizeCountList"]):
                raise ValueError("Offered candidate PT counts differ from its current native card")
            for observed, raw in _IDENTITY_FIELDS.items():
                if observed in identity and raw in runtime and not _same(identity[observed], runtime[raw]):
                    raise ValueError("Offered/current card runtime field differs: " + observed)
            for key in _CARD_FIELDS:
                if key not in runtime:
                    gaps.append(SecondaryInputGap("candidate-runtime-field-unobserved", path + "." + key))
            for key in ("_baseUpgradeCount", "_tmpUpgradeCount", "_playCount"):
                if key in runtime and (type(runtime[key]) is not int or not -(2**31) <= runtime[key] < 2**31):
                    gaps.append(SecondaryInputGap("candidate-runtime-field-invalid", path + "." + key))
            for key in ("_supportUpgradeIdList", "_affectGrowEffectIdList", "_growEffectExamStartAfterList",
                        "_staminaConsumptionSpecifyEffectList"):
                if key in runtime and runtime[key] is not None and not isinstance(runtime[key], list):
                    gaps.append(SecondaryInputGap("candidate-runtime-field-invalid", path + "." + key))
            if "_statusEffect" in runtime and runtime["_statusEffect"] is not None and not isinstance(runtime["_statusEffect"], Mapping):
                gaps.append(SecondaryInputGap("candidate-runtime-field-invalid", path + "._statusEffect"))
            if "_isMoveProduceExamEffectUseInTurn" in runtime and type(runtime["_isMoveProduceExamEffectUseInTurn"]) is not bool:
                gaps.append(SecondaryInputGap("candidate-runtime-field-invalid", path + "._isMoveProduceExamEffectUseInTurn"))
    return {"kind": "effect-card-select", "offered_ordinal": ordinal, "native_zone_type": zone_type,
        "native_zone_name": identity.get("native_zone_name"), "native_zone_index": zone_index,
        "card_id": identity["id"], "upgrade": upgrade, "card_guid": identity["guid"],
        "native_identity": _plain(_freeze(identity)), "runtime_card": _plain(_freeze(runtime)),
        "runtime_position_bound": runtime is not None, "state_zone": field,
        "evaluation": evaluation, "evaluation_observed": evaluation is not None}


def prepare_native_secondary_input(event: Mapping, *, current_command_binding=None) -> NativeSecondaryInput:
    """Decode a locally consistent secondary entry, without any teacher label.

    The caller still owns original source/version qualification. Missing current
    command binding or an unsupported inventory join remains an explicit gap.
    Stored snapshots require a source-verified original AddPlayLog pointer pair.
    """
    if (not isinstance(event, Mapping) or type(event.get("kind")) is not int or event["kind"] != 2
            or type(event.get("phase")) is not int or event["phase"] != 0):
        raise ValueError("Original secondary decision-entry observation required")
    if ("decision_type" in event and event["decision_type"] != "secondary"
            or "native_metadata_sha256" in event and event["native_metadata_sha256"] != NATIVE_METADATA_SHA256):
        raise ValueError("Secondary decision type or declared native metadata version differs")
    order = _integer(event.get("log_index"), "secondary source/queue cursor")
    event_order = _integer(event.get("event_order"), "secondary event order")
    low = _integer(event.get("pick_min"), "secondary minimum", maximum=4096)
    high = _integer(event.get("pick_max"), "secondary maximum", maximum=4096)
    if low > high or type(event.get("is_hand")) is not int or event["is_hand"] not in (0, 1):
        raise ValueError("Original secondary constraints differ")
    state = _snapshot(event.get("snapshot"))
    _snapshot(event.get("after_candidates"))
    before_sha = event["snapshot"]["state_sha256"]
    if before_sha != event["after_candidates"]["state_sha256"]:
        raise ValueError("Secondary candidate query changed the original current state")
    if (not isinstance(state.get("produceId"), str) or not state["produceId"]
            or any(type(state.get(k)) is not int or state[k] < 0 for k in ("planType", "mainEffectType", "stepType", "phase"))
            or state.get("isExamEndComplete") is not False):
        raise ValueError("Secondary mode/flow/stage/current nonterminal state is incomplete")
    candidates = event.get("candidates")
    if (not isinstance(candidates, Mapping) or candidates.get("schema") != "gkms.original-pc-secondary-candidates.v1"
            or candidates.get("read_errors") != [] or candidates.get("purity_verified") is not True
            or candidates.get("state_before_sha256") != before_sha or candidates.get("state_after_sha256") != before_sha
            or type(candidates.get("pick_min")) is not int or candidates["pick_min"] != low
            or type(candidates.get("pick_max")) is not int or candidates["pick_max"] != high
            or type(candidates.get("is_hand")) is not bool or candidates["is_hand"] != bool(event["is_hand"])):
        raise ValueError("Secondary pool/constraint/purity evidence is incomplete or unbound")
    for key in ("command_identity", "effect_context_identity"):
        if key in candidates and candidates[key] != event.get(key):
            raise ValueError("Secondary pool belongs to another callback context")
    pool = candidates.get("candidates")
    if not isinstance(pool, list) or len(pool) > 4096:
        raise ValueError("Complete bounded native offered pool required")
    gaps = []
    for field in _ZONES.values():
        if not isinstance(state.get(field), list):
            gaps.append(SecondaryInputGap("secondary-common-zone-unobserved", field))
    status, references = state.get("status"), state.get("references")
    if (not isinstance(status, Mapping) or not isinstance(status.get("_effectList"), list)
            or not isinstance(references, Mapping) or not isinstance(references.get("RefIds"), list)):
        gaps.append(SecondaryInputGap("secondary-common-status-graph-unobserved", "status/references"))
    forced = candidates.get("selection_mode") == "forced-empty-response"
    if forced:
        if (pool or candidates.get("ordinary_card_pool") is not False
                or candidates.get("forced_empty_entry_qualified") is not True
                or candidates.get("empty_response_basis") != "original-PC-Screen-null-or-empty-early-return"
                or type(candidates.get("container_null")) is not bool
                or any(key in candidates and candidates[key] is not False for key in
                       ("offered_pool_complete", "choices_qualified", "ordinary_pick_count_rule_applies"))):
            raise ValueError("Original forced-empty response cannot be an ordinary card-choice pool")
        if "candidate_list_identity" not in event:
            raise ValueError("Forced-empty response requires the original container identity")
        if candidates["container_null"]:
            if event["candidate_list_identity"] is not None or candidates.get("offered_container_count") is not None:
                raise ValueError("Forced-null container contradicts its observed identity/count")
        elif (not _positive_pointer(event["candidate_list_identity"])
                or type(candidates.get("offered_container_count")) is not int or candidates["offered_container_count"] != 0):
            raise ValueError("Nonnull forced-empty container requires its observed count zero")
        witness = candidates.get("source_action_observation")
        if witness is not None and (not isinstance(witness, Mapping)
                or witness.get("authority") != "original-owned-simulator-minimum-log"
                or type(witness.get("log_index")) is not int or witness["log_index"] != order
                or not _same(witness.get("action"), {"type": 4, "indexes": []})):
            raise ValueError("Original forced-empty source witness differs")
        if "expected_response" in candidates and not _same(candidates["expected_response"], {"type": 4, "indexes": []}):
            raise ValueError("Original forced-empty response declaration differs")
        legal = ()
    else:
        if (not pool or candidates.get("selection_mode") not in (None, "offered-card-pool")
                or candidates.get("offered_pool_complete") is not True or candidates.get("choices_qualified") is not True
                or low > len(pool) or candidates.get("ordinary_card_pool", True) is not True
                or candidates.get("container_null", False) is not False
                or "offered_container_count" in candidates and (type(candidates["offered_container_count"]) is not int
                    or candidates["offered_container_count"] != len(pool))):
            raise ValueError("Ordinary secondary pool/selection constraints are not complete")
        legal = tuple(_candidate(value, i, state, gaps) for i, value in enumerate(pool))
    context = _current_context(event, current_command_binding, gaps)
    stored_context = context is not None and context.get("context_source") == "paired-original-AddPlayLog-command"
    flow = "|".join((state["produceId"], PLAN_BY_NATIVE_VALUE.get(state["planType"], "native-plan:" + str(state["planType"])),
                     EFFECT_BY_NATIVE_VALUE.get(state["mainEffectType"], "native-effect:" + str(state["mainEffectType"]))))
    observation = {"schema": SCHEMA, "authority": "original-PC-secondary-local-observation",
        "source_action_order": order, "event_order": event_order, "state_sha256": before_sha,
        "produce_id": state["produceId"], "plan_type": state["planType"], "main_effect_type": state["mainEffectType"],
        "step_type": state["stepType"], "phase": state["phase"], "flow": flow,
        "stage": STAGE_BY_NATIVE_VALUE.get(state["stepType"], "native-step:" + str(state["stepType"])),
        "run_id": event.get("run_id"), "engine_identity": _plain(_freeze(event.get("engine_identity"))),
        "native_metadata_contract_sha256": NATIVE_METADATA_SHA256,
        "current_context_local_binding_verified": context is not None,
        "last_log_used_as_current_owner": stored_context,
        "current_context_source": context.get("command_source") if context is not None else None,
        "stored_command_source_identity_verified": stored_context,
        "effect_context_payload_observed": context is not None and not stored_context,
        "candidate_query_purity_verified": True,
        "ordinary_offered_pool_complete": not forced, "policy_choice_required": not forced,
        "runtime_cards_bound": all(row["runtime_position_bound"] for row in legal),
        "source_identity_verified": False, "source_master_qualified": False,
        "training_admitted": False, "action_after_state_observed": False, "RL_transition_qualified": False,
        "UI_click_history_observed": False, "raw_state_changed": False, "game_io": False}
    prepared = NativeSecondaryInput({"state_before": state, "legal_candidates": legal, "current_context": context,
        "constraints": {"pick_min": low, "pick_max": high, "is_hand": bool(event["is_hand"]),
            "selection_mode": "forced-empty-response" if forced else "offered-card-pool",
            "offered_count": len(pool), "ordinary_pick_count_rule_applies": not forced,
            "container_null": candidates.get("container_null"),
            "container_count_observed": "offered_container_count" in candidates,
            "offered_container_count": candidates.get("offered_container_count")},
        "observation": observation}, gaps, _authority=_SEAL)
    if stored_context:
        from .native_secondary_binding import _register_prepared_secondary_input
        _register_prepared_secondary_input(current_command_binding, prepared)
    return prepared


def bind_native_secondary_label(prepared: NativeSecondaryInput, source_action: Mapping) -> tuple[int, ...]:
    """Bind an expert response after input/encoding; preserve its exact order.

    This only verifies cursor, ordinary cardinality and offered-ordinal binding.
    It does not qualify source provenance or resolve missing context features.
    """
    if not isinstance(prepared, NativeSecondaryInput) or not isinstance(source_action, Mapping):
        raise ValueError("Prepared secondary input and original source action required")
    if (source_action.get("action_type") != "effect-card-select" or type(source_action.get("order")) is not int
            or source_action["order"] != prepared._record["observation"]["source_action_order"]
            or not isinstance(source_action.get("indexes"), list)
            or any(type(i) is not int or i < 0 for i in source_action["indexes"])):
        raise ValueError("Secondary expert label/cursor/index types differ")
    indexes = source_action["indexes"]
    constraints = prepared._record["constraints"]
    if constraints["selection_mode"] == "forced-empty-response":
        if indexes:
            raise ValueError("Original forced-empty response requires exactly no selected indexes")
    elif (not constraints["pick_min"] <= len(indexes) <= constraints["pick_max"]
            or len(indexes) != len(set(indexes)) or any(i >= len(prepared._record["legal_candidates"]) for i in indexes)):
        raise ValueError("Secondary expert indexes violate the observed ordinary pool/count bounds")
    return tuple(indexes)


__all__ = ["SCHEMA", "NativeSecondaryInput", "SecondaryInputGap", "prepare_native_secondary_input", "bind_native_secondary_label"]
