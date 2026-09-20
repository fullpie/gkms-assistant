"""Shared native state/card representation for integrated primary/secondary BC.

The primary path delegates to the unchanged pure primary kernel. Secondary
inputs need an actual owned callback or the sealed original command binding;
local shape flags alone cannot qualify them. This module encodes features, not
labels, optimizer policy, source admission for fitting, or game transitions.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
import json

from . import shared_bc_features as shared
from .behavior_cloning import _hash_index
from .integrated_exam_bc_model import MAIN, SECONDARY, SecondaryConstraints, SecondaryStep, make_batch
from .native_policy_features import OwnedNativePolicySource, verify_primary_kernel_bridge
from .native_secondary_binding import validate_bound_secondary_input
from .native_secondary_input import NativeSecondaryInput, prepare_native_secondary_input
from .native_structure_contract_set import validate_native_structure_contract_set, resolve_native_structure_contract
from .native_structure_features import NativeStructureCardSemanticFeatures, load_native_structure_package, _emit
from .observed_empty_collections import PC_SOURCE, NATIVE_CORE_SHA256, NATIVE_METADATA_SHA256, _semantic_view
from .training_artifact_io import canonical_json_bytes, sha256_file

SCHEMA = "gkms.integrated-exam-bc-native-features.v1"
SPEC_SCHEMA = "gkms.integrated-exam-bc-feature-encoder-spec.v1"
_SOURCE_NAMES = ("integrated_exam_bc_features.py", "integrated_exam_bc_model.py", "native_secondary_input.py",
    "native_secondary_binding.py", "native_structure_features.py", "native_policy_features.py",
    "shared_bc_features.py", "observed_empty_collections.py", "active_state_features.py", "card_semantic_features.py",
    "behavior_cloning.py", "training_artifact_io.py", "native_structure_contract_set.py")


def _source_references():
    return {name: _reference(Path(__file__).with_name(name)) for name in _SOURCE_NAMES}
_COMMAND_FIELDS = frozenset({"_playType", "_phaseType", "_playIndex", "_playCardPositionType", "_isCardSelect",
    "_isSkipForCalculateForecast", "_selectIndex", "_playEffect", "_effectTriggerId", "_cardSelectSearchId",
    "_isCardSelect2", "_cardSelectSearchId2", "_playingCard", "_isPlayingMoveCardEffect", "_remainCanPlayCardCount",
    "_playingItem", "_playingDrink", "_playingGimmick", "_originEffectIndex", "_startEnchantOriginId",
    "_startEnchantOriginLevel", "_startEnchantOwnerId", "_startEnchantOriginType", "_enchantEffectUid",
    "_playingEnchantIsTriggerActive", "_useEffectUidList", "_isPlayingGimmick", "_assetId", "_supportCardIds",
    "_isSeparateStart", "_isUsePlayableCardCount", "_isConsumeCost", "_cardGuids", "_isManual", "_triggeredGrowEffectAffectLists"})
_EFFECT_FIELDS = frozenset({"_id", "_effectType", "_effectValue1", "_effectValue2", "_effectCount", "_judgeTargetIndex",
    "_effectTurn", "_targetProduceCardId", "_targetUpgradeCount", "_cardSearchId", "_cardSearchId2",
    "_pickCountReferenceProduceCardSearchId", "_movePositionType", "_pickRangeType", "_pickCount", "_pickCountMin",
    "_pickCountMax", "_pickCountType", "_pickCountReferenceProduceCardSearchId2", "_pickRangeType2", "_pickCountMin2",
    "_pickCountMax2", "_pickCountType2", "_chainEffectId", "_chainEffectIdList", "_statusEnchantId",
    "_targetExamEffectType", "_effectGroupIdList", "_cardGrowEffectIdList"})
_EFFECT_INTS = frozenset({"_effectType", "_effectValue1", "_effectValue2", "_effectCount", "_judgeTargetIndex",
    "_effectTurn", "_targetUpgradeCount", "_movePositionType", "_pickRangeType", "_pickCount", "_pickCountMin",
    "_pickCountMax", "_pickCountType", "_pickRangeType2", "_pickCountMin2", "_pickCountMax2", "_pickCountType2", "_targetExamEffectType"})
_COMMAND_BOOLS = frozenset({"_isCardSelect", "_isCardSelect2", "_isSkipForCalculateForecast", "_isPlayingMoveCardEffect",
    "_playingEnchantIsTriggerActive", "_isPlayingGimmick", "_isSeparateStart", "_isUsePlayableCardCount", "_isConsumeCost", "_isManual"})
_COMMAND_INTS = frozenset({"_playType", "_phaseType", "_playIndex", "_playCardPositionType", "_remainCanPlayCardCount",
    "_originEffectIndex", "_startEnchantOriginLevel", "_startEnchantOriginType", "_enchantEffectUid"})


def _read(reference):
    if not isinstance(reference, Mapping) or not isinstance(reference.get("path"), str):
        raise ValueError("Hash-bound feature source reference required")
    path = Path(reference["path"])
    if sha256_file(path) != reference.get("sha256"):
        raise ValueError("Integrated feature source SHA differs")
    return json.loads(path.read_bytes())


def _reference(path):
    path = Path(path).resolve()
    return {"path": str(path), "sha256": sha256_file(path)}


def _int32(value):
    return type(value) is int and -(2**31) <= value < 2**31


class IntegratedFeatureGap(ValueError):
    def __init__(self, gaps, metadata):
        self.gaps = tuple(sorted(set(gaps)))
        self.metadata = deepcopy(metadata)
        super().__init__("Required integrated native feature gaps: " + "; ".join(self.gaps[:12]))


@dataclass(frozen=True)
class EncodedIntegratedStep:
    decision_type: int
    context_tokens: tuple[str, ...]
    candidate_tokens: tuple[tuple[str, ...], ...]
    context_indices: tuple[int, ...]
    candidate_indices: tuple[tuple[int, ...], ...]
    candidate_ordinals: tuple[int, ...]
    contract_sha256: str
    required_gaps: tuple[str, ...]
    metadata: Mapping
    bypass_response: Mapping | None = None

    def to_pointer_batch(self):
        if self.bypass_response is not None:
            raise ValueError("Protocol completion has no learned pointer row")
        return make_batch([self.context_indices], [self.candidate_indices], [self.decision_type],
            candidate_ordinals=[self.candidate_ordinals])


def _resolve_references(value, rows, path, gaps, visited=(), depth=0):
    if depth > 40:
        gaps.append(path + ":reference-depth-exceeded"); return None
    if isinstance(value, Mapping):
        if set(value) == {"rid"}:
            rid = value["rid"]
            found = [x for x in rows if isinstance(x, Mapping) and type(x.get("rid")) is int and x["rid"] == rid]
            if type(rid) is not int or rid in visited or len(found) != 1 or not isinstance(found[0].get("data"), Mapping):
                gaps.append(path + ":reference-unresolved-or-cycle"); return None
            return _resolve_references(found[0]["data"], rows, path, gaps, (*visited, rid), depth + 1)
        return {key: _resolve_references(child, rows, path + "." + key, gaps, visited, depth + 1) for key, child in value.items()}
    if isinstance(value, list):
        return [_resolve_references(child, rows, f"{path}[{i}]", gaps, visited, depth + 1) for i, child in enumerate(value)]
    return value


def _card_owner_view(card, empty_contract, owner_path):
    # The existing helper traverses zone-shaped carriers. Here the carrier is
    # ONLY a way to apply its proven ExamCardData field view to a declared
    # command parent. It is never passed to the state encoder or called a real
    # hand; all ledger paths are rebound to the actual command field.
    view, ledger = _semantic_view({"handList": [card]}, PC_SOURCE,
        inactive_status_contract=empty_contract.get("inactive_card_status_contract"),
        optional_identifiers_contract=empty_contract.get("optional_effect_identifiers_contract"))
    for row in ledger:
        row["path"] = row["path"].replace("/handList/0", owner_path, 1)
    return view["handList"][0], ledger


def _command_tokens(features, command, state_references, empty_contract):
    tokens, gaps, views = [], [], []
    if not isinstance(command, Mapping):
        return (), ("current-command-missing",), ()
    command = _resolve_references(command, state_references, "command", gaps)
    missing, extra = _COMMAND_FIELDS - set(command), set(command) - _COMMAND_FIELDS
    gaps.extend("command:field-missing:" + key for key in missing)
    gaps.extend("command:field-unclassified:" + key for key in extra)
    for key in _COMMAND_INTS:
        if not _int32(command.get(key)): gaps.append("command:int32-invalid:" + key)
    for key in _COMMAND_BOOLS:
        if type(command.get(key)) is not bool: gaps.append("command:bool-invalid:" + key)
    if command.get("_isCardSelect") is not True or command.get("_isCardSelect2") is not False:
        gaps.append("command:second-or-other-selector-context-unqualified")
    effect = command.get("_playEffect")
    if not isinstance(effect, Mapping):
        gaps.append("command:current-effect-missing")
    else:
        gaps.extend("command.effect:field-missing:" + key for key in _EFFECT_FIELDS - set(effect))
        gaps.extend("command.effect:field-unclassified:" + key for key in set(effect) - _EFFECT_FIELDS)
        for key in _EFFECT_INTS:
            if not _int32(effect.get(key)): gaps.append("command.effect:int32-invalid:" + key)
    # Response bookkeeping is not a teacher feature or a portable identity.
    for key in ("_selectIndex", "_cardGuids", "_useEffectUidList"):
        value = command.get(key)
        if value not in (None, []): gaps.append("command:nonempty-response-or-identity-field-unqualified:" + key)
    if command.get("_enchantEffectUid") != 0:
        gaps.append("command:enchant-effect-uid-binding-unqualified")
    learned = {key: value for key, value in command.items() if key not in
        {"_selectIndex", "_cardGuids", "_useEffectUidList", "_enchantEffectUid", "_assetId", "_playingCard", "_playingDrink", "_playingItem", "_playingGimmick"}}
    _emit("joint.secondary.command", learned, tokens, gaps)
    features._native_links(learned, "joint.secondary.command", tokens, gaps)
    for key, table in (("_cardSelectSearchId", "produce_card_search"), ("_cardSelectSearchId2", "produce_card_search"),
                       ("_effectTriggerId", "produce_exam_trigger")):
        value = command.get(key)
        if value is not None and not isinstance(value, str):
            gaps.append("command:reference-type-invalid:" + key)
        elif value:
            features._graph(table, value, "joint.secondary.command." + key + ".definition", tokens, gaps)
    for key in ("_playingCard", "_playingDrink", "_playingItem", "_playingGimmick"):
        parent = command.get(key)
        prefix = "joint.secondary.parent." + key
        if parent is None:
            _emit(prefix, None, tokens, gaps); continue
        if not isinstance(parent, Mapping):
            gaps.append(prefix + ":parent-shape-invalid"); continue
        if key == "_playingCard":
            parent, ledger = _card_owner_view(parent, empty_contract, "/command/_playingCard")
            views.extend(ledger)
            data = parent.get("_cardData")
            if not isinstance(data, Mapping):
                gaps.append(prefix + ":runtime-card-data-missing"); continue
            card, card_gaps = features._card(data.get("_id"), data.get("_upgradeCount"), parent)
            tokens.extend(prefix + ":" + x for x in card)
            gaps.extend(prefix + ":" + x for x in card_gaps)
        else:
            _emit(prefix, parent, tokens, gaps)
            identity = parent.get("_id")
            if key in ("_playingDrink", "_playingItem"):
                features._graph("produce_drink" if key == "_playingDrink" else "produce_item", identity,
                    prefix + ".definition", tokens, gaps)
            else:
                features._native_links(parent, prefix, tokens, gaps)
    return tuple(tokens), tuple(sorted(set(gaps))), tuple(views)


class IntegratedExamFeatureEncoder:
    def __init__(self, contract_set_reference, original_shared_reference, *, source_resolver=None, loader_compatibility=None):
        self._input_references = {"contract_set": deepcopy(dict(contract_set_reference)),
            "original_shared_encoder": deepcopy(dict(original_shared_reference))}
        self._contract_set = _read(contract_set_reference)
        validate_native_structure_contract_set(self._contract_set)
        self._bridge = verify_primary_kernel_bridge(self._contract_set, original_shared_reference, loader_compatibility=loader_compatibility)
        self._layout = deepcopy(self._contract_set["pointer_layout"])
        self._features = {}
        for sha, contract in self._contract_set["contracts"].items():
            native = contract["native_structure_contract"]
            package = load_native_structure_package(source_master_hash=native["source_master_hash"],
                source_inventory_reference=native["source_inventory_reference"], native_schema_reference=native["native_schema_reference"],
                catalog_reference=native["catalog_reference"], snapshot_reference=native["snapshot_reference"], source_resolver=source_resolver)
            self._features[sha] = NativeStructureCardSemanticFeatures(package,
                active_status_contract=contract.get("observed_active_status_contract"))
        source_references = _source_references()
        self._provenance = {"inputs": deepcopy(self._input_references), "primary_kernel_bridge": self._bridge,
            "encoder_sources": source_references}
        body = {"schema": SCHEMA, "primary_contract_set_file_sha256": contract_set_reference["sha256"],
            "primary_contract_set_sha256": self._contract_set["contract_set_sha256"],
            "primary_kernel_bridge": {"original_shared_sha256": original_shared_reference["sha256"],
                "kernel_AST_sha256": self._bridge["kernel_AST_sha256"],
                "runtime_source_hashes": {name: ref["sha256"] for name, ref in self._bridge["runtime_sources"].items()},
                "exact_kernel_AST_equal": self._bridge["exact_kernel_AST_equal"],
                "other_shared_definitions_equal": self._bridge["other_shared_definitions_equal"]},
            "pointer_layout": self._layout, "decision_types": {"main": MAIN, "secondary": SECONDARY},
            "secondary_required": ["source-bound-current-command", "actual-before-state-and-all-card-zones",
                "complete-native-offered-pool-runtime-joins", "command-effect-search-parent-graphs", "native-constraints"],
            "optional_effect_context_payload_consumed": False, "second_selector_branch_qualified": False,
            "prefix_semantics": "derived-decoder-prefix; no intermediate-native-state claim",
            "STOP": "virtual ordinal offered_count; only when native min satisfied and not protocol-complete",
            "source_identity_tokens_in_model": False, "teacher_label_or_total_decoder_length_consumed": False,
            "original_main_vectors_retagged": False, "training_admitted": False,
            "source_hashes": {name: ref["sha256"] for name, ref in source_references.items()}}
        self._contract = {**body, "contract_sha256": shared.digest(body)}

    @property
    def contract(self):
        return deepcopy(self._contract)

    @property
    def contract_set(self):
        return deepcopy(self._contract_set)

    @property
    def layout(self):
        return deepcopy(self._layout)

    @property
    def provenance(self):
        return deepcopy(self._provenance)

    @property
    def spec(self):
        return {"schema": SPEC_SCHEMA, **deepcopy(self._input_references),
            "feature_contract_sha256": self._contract["contract_sha256"],
            "encoder_source_hashes": deepcopy(self._contract["source_hashes"])}

    def _route(self, master, produce):
        contract = resolve_native_structure_contract(self._contract_set, source_master_hash=master, produce_id=produce,
            native_core_sha256=NATIVE_CORE_SHA256, native_metadata_sha256=NATIVE_METADATA_SHA256)
        return self._features[contract["contract_sha256"]], contract

    def _result(self, kind, context, candidates, ordinals, metadata, optional=(), bypass=None):
        contexts = tuple(_hash_index(token, self._layout["context_buckets"]) for token in context)
        candidate_indices = tuple(tuple(dict.fromkeys(_hash_index("exact-candidate-field:" + token,
            self._layout["candidate_buckets"]) for token in row)) for row in candidates)
        if candidates and len(set(candidate_indices)) != len(candidates):
            raise IntegratedFeatureGap(("candidate-pointer-hash-collision",), metadata)
        meta = {**deepcopy(metadata), "feature_contract_sha256": self._contract["contract_sha256"],
            "encoder_provenance_sha256": shared.digest(self._provenance),
            "decision_type": kind, "candidate_ordinals": list(ordinals), "representation_complete": bypass is None,
            "mandatory_gaps": [], "optional_derived_diagnostics": list(optional), "training_admitted": False,
            "action_after_state_observed": False, "RL_transition_qualified": False,
            "context_tokens_sha256": shared.digest(context), "candidate_tokens_sha256": shared.digest(candidates),
            "pointer_context_indices_sha256": shared.digest(contexts), "pointer_candidate_indices_sha256": shared.digest(candidate_indices)}
        meta["feature_sha256"] = shared.digest({"contract": self._contract["contract_sha256"], "context": context,
            "candidates": candidates, "ordinals": ordinals, "decision_type": kind})
        return EncodedIntegratedStep(kind, tuple(context), tuple(tuple(row) for row in candidates), contexts,
            candidate_indices, tuple(ordinals), self._contract["contract_sha256"], (), meta, bypass)

    def encode_primary(self, observation, *, source):
        if type(source) is not OwnedNativePolicySource:
            raise ValueError("Primary encoding needs its actual owned policy source")
        prepared = source.primary(observation)
        features, contract = self._route(source.master, source.produce)
        empty = contract["observed_empty_collection_contract"]
        state, ledger = _semantic_view(prepared.state_before, PC_SOURCE,
            inactive_status_contract=empty.get("inactive_card_status_contract"),
            optional_identifiers_contract=empty.get("optional_effect_identifiers_contract"))
        flow = "|".join((state["produceId"], shared.PLAN_BY_NATIVE_VALUE[state["planType"]], shared.EFFECT_BY_NATIVE_VALUE[state["mainEffectType"]]))
        encoded = shared.encode_primary_feature_view(raw_state=prepared.state_before, state_before=state,
            legal_candidates=prepared.legal_candidates, flow=flow, stage=shared.STAGE_BY_NATIVE_VALUE[state["stepType"]],
            native_mask_complete=True, candidate_scope=shared.PRIMARY_SCOPE, features=features, contract=contract)
        gaps = [*encoded.coverage["contract_gaps"], *encoded.coverage["semantic_gap_tokens"]]
        metadata = {"source_kind": "owned-native-policy-run", "source": deepcopy(source.identity["source_reference"]),
            "run_id": source.identity["run_id"], "raw_state_sha256": encoded.state_sha256, "flow": flow, "stage": encoded.stage,
            "original_primary_contract_sha256": contract["contract_sha256"], "candidate_bindings": list(encoded.candidate_bindings),
            "semantic_view_ledger": ledger, "decoder_step": 0}
        if not encoded.coverage["representation_complete"] or gaps:
            raise IntegratedFeatureGap(gaps or ["primary-representation-incomplete"], metadata)
        source.validate_owner(observation, "main")
        return self._result(MAIN, encoded.context_tokens, encoded.candidate_tokens,
            tuple(range(len(encoded.candidate_tokens))), metadata, encoded.coverage.get("optional_derived_diagnostics", ()))

    def encode_secondary(self, prepared, binding, *, derived_prefix=(), prefix_origin="derived-policy-decoder-prefix"):
        witness = validate_bound_secondary_input(binding, prepared)
        original = _read(witness["source"])
        context = prepared.current_context
        if (context["source_master_hash"] != original["expected"]["master_hash"]
                or prepared.state_before["produceId"] != original["produce_id"]):
            raise ValueError("Source-bound secondary Master or mode differs")
        reference = witness["event_reference"]
        metadata = {**witness, "source_kind": "sealed-original-secondary", "source_order": reference["source_order"],
            "source_decision_id": f"original-pc-secondary:{witness['source']['sha256']}:{reference['source_order']}"}
        result = self._secondary(prepared, original["expected"]["master_hash"], metadata, derived_prefix, prefix_origin)
        validate_bound_secondary_input(binding, prepared)
        return result

    def encode_live_secondary(self, observation, *, source, derived_prefix=(), prefix_origin="derived-policy-decoder-prefix"):
        if type(source) is not OwnedNativePolicySource:
            raise ValueError("Live secondary encoding requires the actual owned policy source")
        source.validate_owner(observation, "secondary")
        prepared = prepare_native_secondary_input(observation)
        if prepared.state_before["produceId"] != source.produce:
            raise ValueError("Live secondary mode differs from its initialized source")
        metadata = {"source_kind": "owned-native-policy-run", "source": deepcopy(source.identity["source_reference"]),
            "run_id": source.identity["run_id"], "event_order": observation["event_order"],
            "source_decision_id": f"owned-pc-secondary:{source.identity['run_id']}:{observation['event_order']}",
            "observation_sha256": shared.digest(observation), "source_identity_verified_for_run": True}
        result = self._secondary(prepared, source.master, metadata, derived_prefix, prefix_origin)
        source.validate_owner(observation, "secondary")
        return result

    def _secondary(self, prepared, master, metadata, prefix, origin):
        if not isinstance(prepared, NativeSecondaryInput):
            raise ValueError("Native structural secondary input required")
        constraints = SecondaryConstraints.from_native_input(prepared)
        prefix = constraints.validate_prefix(prefix)
        if origin not in ("derived-policy-decoder-prefix", "derived-teacher-forcing-prefix"):
            raise ValueError("Prefix must be explicitly decoder-derived, never an observed intermediate state")
        state = prepared.state_before
        flow, stage = prepared.observation["flow"], prepared.observation["stage"]
        metadata = {**metadata, "source_master_hash": master, "raw_state_sha256": shared.digest(state), "flow": flow, "stage": stage,
            "derived_prefix": list(prefix), "prefix_origin": origin, "decoder_step": len(prefix),
            "native_constraints": prepared.constraints, "input_gaps": [{"code": g.code, "path": g.path} for g in prepared.gaps],
            "effect_context_payload_observed": prepared.observation["effect_context_payload_observed"],
            "optional_effect_context_payload_consumed": False}
        if constraints.complete(prefix):
            return self._result(SECONDARY, (), (), (), metadata, bypass={"type": 4, "indexes": list(prefix)})
        if prepared.gaps:
            raise IntegratedFeatureGap([g.code + ":" + g.path for g in prepared.gaps], metadata)
        features, contract = self._route(master, state["produceId"])
        if (type(state.get("planType")) is not int or type(state.get("mainEffectType")) is not int
                or (state["planType"], state["mainEffectType"]) not in shared.PLAN_EFFECTS
                or type(state.get("stepType")) is not int or state["stepType"] not in contract["native_stages"]
                or state.get("isExamEndComplete") is not False):
            raise IntegratedFeatureGap(["secondary-mode-flow-stage-or-terminal-scope"], metadata)
        empty = contract["observed_empty_collection_contract"]
        view, ledger = _semantic_view(state, PC_SOURCE,
            inactive_status_contract=empty.get("inactive_card_status_contract"),
            optional_identifiers_contract=empty.get("optional_effect_identifiers_contract"))
        context_tokens, gaps, optional = features._context(view)
        tokens, required = list(context_tokens), list(gaps)
        point = view.get("producePoint")
        if type(point) is not int or point < 0: required.append("producePoint-unobserved-or-invalid")
        _emit("joint.current.producePoint", point, tokens, required)
        command = prepared.current_context["command"]
        refs = prepared.current_context.get("references", {}).get("RefIds")
        if not isinstance(refs, list): raise IntegratedFeatureGap(["current-command-reference-domain-missing"], metadata)
        extra, extra_gaps, parent_views = _command_tokens(features, command, refs, empty)
        tokens.extend(extra); required.extend(extra_gaps)
        _emit("joint.secondary.constraints", {"pick_min": constraints.minimum, "pick_max": constraints.maximum,
            "offered_count": constraints.offered_count, "is_hand": prepared.constraints["is_hand"]}, tokens, required)
        _emit("joint.secondary.derived_prefix", list(prefix), tokens, required)
        pool_tokens = []
        for ordinal, candidate in enumerate(prepared.legal_candidates):
            if candidate["offered_ordinal"] != ordinal or candidate["runtime_position_bound"] is not True:
                required.append("candidate-runtime-ordinal-binding-invalid"); continue
            runtime = view[candidate["state_zone"]][candidate["native_zone_index"]]
            original = state[candidate["state_zone"]][candidate["native_zone_index"]]
            if canonical_json_bytes(original) != canonical_json_bytes(candidate["runtime_card"]):
                required.append("candidate-runtime-payload-differs-from-actual-zone")
            card, card_gaps = features._card(candidate["card_id"], candidate["upgrade"], runtime)
            fields = ("kind:secondary-card", f"offered-ordinal:{ordinal}", f"native-zone:{candidate['native_zone_name']}",
                f"native-zone-index:{candidate['native_zone_index']}", *card)
            pool_tokens.append(fields)
            tokens.extend(f"joint.secondary.offered[{ordinal}]:" + token for token in fields)
            required.extend(f"candidate[{ordinal}]:" + gap for gap in card_gaps)
        for position, ordinal in enumerate(prefix):
            tokens.extend(f"joint.secondary.selected_prefix[{position}]:" + token for token in pool_tokens[ordinal])
        choices = constraints.choices(prefix)
        fields = tuple(("kind:secondary-stop", "joint.secondary.STOP:virtual-not-native-ordinal")
            if ordinal == constraints.offered_count else pool_tokens[ordinal] for ordinal in choices)
        if required: raise IntegratedFeatureGap(required, metadata)
        metadata.update(original_primary_contract_sha256=contract["contract_sha256"], semantic_view_sha256=shared.digest(view),
            semantic_view_ledger=ledger, parent_card_view_ledger=list(parent_views),
            candidate_bindings=[{k: c[k] for k in ("offered_ordinal", "card_id", "upgrade", "card_guid", "native_zone_type", "native_zone_name", "native_zone_index")} for c in prepared.legal_candidates])
        return self._result(SECONDARY, tuple(tokens), fields, choices, metadata, optional)

    def __call__(self, observation, step=None):
        if observation.get("decision_type") == "main":
            if step is not None: raise ValueError("Primary decisions do not have a secondary prefix")
            return self.encode_primary(observation["native_observation"], source=observation["owned_source"]).to_pointer_batch()
        if observation.get("decision_type") != "secondary" or not isinstance(step, SecondaryStep):
            raise ValueError("An explicit secondary decoder step is required")
        prepared = observation["secondary_input"]
        if step.constraints != SecondaryConstraints.from_native_input(prepared):
            raise ValueError("Decoder constraints differ from the source-bound original pool")
        if "stored_command_binding" in observation:
            encoded = self.encode_secondary(prepared, observation["stored_command_binding"],
                derived_prefix=step.derived_prefix, prefix_origin=step.prefix_origin)
        else:
            encoded = self.encode_live_secondary(observation["native_observation"], source=observation["owned_source"],
                derived_prefix=step.derived_prefix, prefix_origin=step.prefix_origin)
        if encoded.candidate_ordinals != step.choice_ordinals:
            raise ValueError("Decoder choice order differs from the encoded source pool")
        return encoded.to_pointer_batch()


def load_integrated_exam_feature_encoder(spec):
    if (not isinstance(spec, Mapping) or spec.get("schema") != SPEC_SCHEMA
            or set(spec) != {"schema", "contract_set", "original_shared_encoder", "feature_contract_sha256", "encoder_source_hashes"}):
        raise ValueError("A complete pinned integrated feature encoder specification is required")
    current = {name: ref["sha256"] for name, ref in _source_references().items()}
    if current != spec["encoder_source_hashes"]:
        raise ValueError("Integrated encoder source bytes differ from the pinned specification")
    encoder = IntegratedExamFeatureEncoder(spec["contract_set"], spec["original_shared_encoder"])
    if encoder.contract["contract_sha256"] != spec["feature_contract_sha256"]:
        raise ValueError("Integrated feature contract differs from the pinned specification")
    return encoder


__all__ = ["SCHEMA", "SPEC_SCHEMA", "IntegratedFeatureGap", "EncodedIntegratedStep", "IntegratedExamFeatureEncoder",
           "load_integrated_exam_feature_encoder"]
