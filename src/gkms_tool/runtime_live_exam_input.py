"""Read-only adapters for actual DLL exam observations.

These values share numerical model inputs with the standalone engine, but keep
their own live ownership and version provenance. They are never replay events,
training-qualified rows, or permission to submit an action.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from .canonical_training_labels import PLAN_BY_NATIVE_VALUE, EFFECT_BY_NATIVE_VALUE, STAGE_BY_NATIVE_VALUE
from .native_secondary_input import NativeSecondaryInput, _candidate, _freeze
from .observed_empty_collections import NATIVE_CORE_SHA256, NATIVE_METADATA_SHA256
from .observed_primary_identity import normalize_observed_primary_candidates
from .live_pc_consumer_contract import validate_live_pc_consumer_identity
from .training_artifact_io import canonical_json_bytes, sha256_file

SCHEMA = "gkms.current-dll-exam-policy-input.v1"
DTO_SCHEMA = "gkms.live-exam-model-observation.v1"
_SEAL = object()
_ZONES = {2: "handList", 3: "deckList", 4: "graveList", 5: "lostList", 13: "holdList"}
_ZONE_NAMES = {2: "Hand", 3: "Deck", 4: "Grave", 5: "Lost", 13: "Hold"}
MASTER_INVENTORY_REFERENCE = {
    "path": str(Path(__file__).resolve().parents[2] / "var/research/fullpower_reconstruction_20260909/master_version_inventory.json"),
    "sha256": "27e053ed41fb23c60c0618861a067d0252c0ad260ea457f7edad8d8a2c797d4a"}


def _require(ok, message):
    if not ok:
        raise ValueError(message)


def _same(a, b):
    return canonical_json_bytes(a) == canonical_json_bytes(b)


def _digest(value):
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _pointer(value):
    _require(type(value) in (str, int) and bool(value), "Actual native pointer identity required")
    try:
        result = int(value, 16 if value.lower().startswith("0x") else 10) if isinstance(value, str) else value
    except ValueError as error:
        raise ValueError("Malformed native pointer identity") from error
    _require(result > 0, "Null native owner identity")
    return result


def _common(snapshot, session_generation, kind, flow_scope=None):
    _require(isinstance(snapshot, Mapping) and isinstance(session_generation, str) and bool(session_generation),
        "Live snapshot and current DLL session generation required")
    dto = snapshot.get("exam_model_observation")
    _require(isinstance(dto, Mapping) and dto.get("schema") == DTO_SCHEMA and dto.get("decision_type") == kind,
        "Complete current-DLL model observation required")
    _require(dto.get("complete") is True and dto.get("read_errors") == [], "Live model observation has read gaps")
    engine = dto.get("engine_identity", {})
    consumer = validate_live_pc_consumer_identity(engine)
    purity = dto.get("purity", {})
    _require(all(purity.get(key) is True for key in ("state_equal", "owner_stable", "execution_master_stable")),
        "Live observation mutated state, Master or owner")
    before, after = dto.get("state_before"), dto.get("state_after")
    _require(isinstance(before, Mapping) and _same(before, after), "Before/after original ExamSaveData differs")
    # DLL JSON number spelling is not Python's canonical spelling. Verify its
    # two hashes agree, then independently record the exact parsed-state digest.
    native_before, native_after = purity.get("before_native_sha256"), purity.get("after_native_sha256")
    _require(isinstance(native_before, str) and len(native_before) == 64 and native_before == native_after
        and purity.get("hash_encoding") == "nlohmann-json-dump", "Native purity hash pair is absent or inconsistent")
    _pointer(dto.get("sequence_id")); _pointer(dto.get("parameter_id"))
    _require(snapshot.get("revision") is not None, "Revision-bound native snapshot required")
    _require(before.get("produceId") in ("produce-004", "produce-005")
        and before.get("stepType") in (16, 17, 18) and before.get("isExamEndComplete") is False,
        "These completed BC models cover NIA Pro/Master three exams only")
    pair = before.get("planType"), before.get("mainEffectType")
    from .local_flow_trial import LiveFlowScope, SUPPORTED_NATIVE_FLOW_PAIRS
    _require(all(type(value) is int for value in pair) and pair in SUPPORTED_NATIVE_FLOW_PAIRS,
        "Unknown actual native model flow")
    actual_flow = "|".join((before["produceId"], PLAN_BY_NATIVE_VALUE[pair[0]], EFFECT_BY_NATIVE_VALUE[pair[1]]))
    if flow_scope is not None:
        _require(type(flow_scope) is LiveFlowScope, "Verified local model flow scope required")
        flow_scope.validate_unchanged()
        _require(flow_scope.binding["flow"] == actual_flow, "Actual native flow differs from the selected model run scope")
    _require(all(isinstance(before.get(k), list) for k in _ZONES.values())
        and isinstance(before.get("drinkList"), list) and isinstance(before.get("status"), Mapping)
        and isinstance(before.get("references", {}).get("RefIds"), list), "Full original state/card/reference domains required")
    observation = {"schema": SCHEMA, "authority": "current-DLL-owned-exam-observation",
        "session_generation": session_generation, "revision": snapshot["revision"],
        "sequence_id": dto["sequence_id"], "parameter_id": dto["parameter_id"],
        "engine_identity": deepcopy(engine), "execution_master": deepcopy(dto.get("execution_master")),
        "pc_consumer_compatibility": consumer,
        "state_sha256": _digest(before), "dto_sha256": _digest(dto),
        "native_purity_hash": native_before, "native_purity_hash_encoding": purity["hash_encoding"],
        "training_admitted": False, "action_after_state_observed": False, "RL_transition_qualified": False,
        "game_io": False, "input_authority": False,
        "flow": actual_flow,
        "stage": STAGE_BY_NATIVE_VALUE[before["stepType"]]}
    return dto, deepcopy(before), observation


@dataclass(frozen=True)
class RuntimeLivePrimaryInput:
    state_before: Mapping
    legal_candidates: tuple[Mapping, ...]
    original_actions: tuple[Mapping, ...]
    observation: Mapping
    feature_evidence: Mapping | None = None


def _feature_evidence(dto):
    return {key: deepcopy(dto.get(key)) for key in (
        "sequence_id", "parameter_id", "save_object_id", "save_object_id_after", "purity", "reference_presence", "reference_presence_after",
        "command_presence", "command_presence_after", "command_native_sha256", "command_native_sha256_after", "live_counters")} | {
            "command_id": dto.get("context_binding", {}).get("command_id")}


def prepare_live_primary(observed, *, flow_scope=None):
    """Bind one RuntimeExamOutcome to the actual settled DLL primary mask."""
    _require(getattr(observed, "status", None) == "observed", "Live primary outcome is not an observed gateway state")
    snapshot = observed.native_snapshot
    dto, state, observation = _common(snapshot, observed.native_session_generation, "main", flow_scope)
    _require(_same(state, snapshot.get("exam_save")) and _same(state, observed.raw_state),
        "Model state differs from the actual gateway snapshot")
    _require(_pointer(snapshot.get("sequence_id")) == _pointer(dto["sequence_id"]), "Main sequence owner differs")
    _require(snapshot.get("phase") == 6 and state.get("phase") == 6 and snapshot.get("busy") is False
        and snapshot.get("queue_empty") is True and snapshot.get("terminal") is False
        and snapshot.get("turn_card_play_end") is False and snapshot.get("is_replay") is False,
        "Main input is not at the existing settled native boundary")
    mask = dto.get("native_legal_inputs")
    _require(isinstance(mask, Mapping) and _same(mask, snapshot.get("native_legal_inputs"))
        and mask.get("schema") == "gkms.native-exam-legal-inputs.v1" and mask.get("scope") == "primary-inputs-only"
        and mask.get("complete") is True and mask.get("boundary_ready") is True and mask.get("unknown_checks") == [],
        "Actual complete DLL primary legality mask required")
    expected, candidates = [], []
    for family, zone, kind, key in (("hand", "handList", "play", "card_guid"), ("drinks", "drinkList", "drink", "drink_id")):
        rows = mask.get(family)
        _require(isinstance(rows, list) and len(rows) == len(state[zone]), "Native predicates do not cover current " + family)
        for slot, row in enumerate(rows):
            _require(type(row.get("slot")) is int and row["slot"] == slot and type(row.get("can_use")) is bool,
                "Observed native predicate slot/result is incomplete")
            identity = state[zone][slot].get("_guid" if kind == "play" else "_id")
            _require(isinstance(identity, str) and bool(identity) and row.get(key) == identity,
                "DLL executable identity differs from the actual current slot")
            if not row["can_use"]:
                continue
            expected.append((kind, "exam." + kind, {"slot": slot, key: identity}))
            if kind == "play":
                data = state[zone][slot]["_cardData"]
                candidates.append({"kind": "use-hand", "action_type": "use-hand", "slot_index": slot,
                    "legal": True, "card_guid": identity, "card_id": data["_id"], "upgrade": data["_upgradeCount"]})
            else:
                candidates.append({"kind": "use-drink", "action_type": "use-drink", "slot_index": slot,
                    "legal": True, "drink_id": identity})
    _require(mask.get("end_turn", {}).get("can_use") is True, "Observed native end-turn boundary required")
    expected.append(("end_turn", "exam.end_turn", {}))
    candidates.append({"kind": "turn-end", "action_type": "turn-end", "slot_index": 0,
        "legal": True, "action_id": "END_TURN"})
    actions = mask.get("legal_actions")
    _require(isinstance(actions, list) and len(actions) == len(expected), "Native legal action denominator changed")
    _require(all((row.get("kind"), row.get("command"), row.get("target")) == wanted
        for row, wanted in zip(actions, expected)), "Native legal action order/target differs from predicates")
    normalize_observed_primary_candidates(state, candidates)
    observation.update(primary_mask_complete=True, source_kind="current-DLL", model_source_bound=False)
    return RuntimeLivePrimaryInput(state, tuple(candidates), tuple(deepcopy(actions)), observation, _feature_evidence(dto))


def _resolved(value, references, seen=()):
    """Compare object payloads across independent JsonUtility reference domains."""
    if isinstance(value, Mapping):
        if set(value) == {"rid"}:
            rid = value["rid"]
            matches = [r for r in references if r.get("rid") == rid]
            _require(type(rid) is int and rid not in seen and len(matches) == 1,
                "Unresolved or cyclic live serialized reference")
            return {"type": matches[0]["type"], "data": _resolved(matches[0]["data"], references, (*seen, rid))}
        return {k: _resolved(v, references, seen) for k, v in value.items() if k != "references"}
    if isinstance(value, list):
        return [_resolved(x, references, seen) for x in value]
    return value


class RuntimeNativeSecondaryInput(NativeSecondaryInput):
    """Same numerical value interface, separately sealed live provenance."""
    __slots__ = ()

    def __init__(self, record, gaps, *, _authority=None):
        _require(_authority is _SEAL, "Use prepare_live_secondary for current DLL input")
        canonical_json_bytes(record)
        object.__setattr__(self, "_record", _freeze(record))
        object.__setattr__(self, "gaps", tuple(gaps))

    @property
    def ui_indices(self):
        return tuple(self._record["ui_indices"])

    @property
    def selected_ordinals(self):
        return tuple(self._record["selected_ordinals"])

    @property
    def feature_evidence(self):
        from .native_secondary_input import _plain
        return _plain(self._record.get("feature_evidence"))


def prepare_live_secondary(snapshot, *, session_generation, pending, flow_scope=None):
    """Validate the already owned pending selector; never submit or settle it."""
    dto, state, observation = _common(snapshot, session_generation, "secondary", flow_scope)
    _require(isinstance(pending, Mapping) and pending.get("schema") == "gkms.runtime-pending-exam.v1"
        and pending.get("request", {}).get("session_generation") == session_generation,
        "Same-session pending primary transaction required for secondary input")
    parent, binding = snapshot.get("parent_context"), dto.get("context_binding", {})
    _require(snapshot.get("exam_continuation") is True and snapshot.get("actions_complete") is True
        and snapshot.get("busy") is False and isinstance(parent, Mapping) and _same(parent, dto.get("parent_context")),
        "Current native selector/pending owner is unavailable")
    _require(_pointer(parent.get("sequence_id")) == _pointer(dto["sequence_id"]) == _pointer(pending.get("sequence_id"))
        and _pointer(binding.get("sequence_id")) == _pointer(dto["sequence_id"])
        and _pointer(binding.get("parameter_id")) == _pointer(dto["parameter_id"])
        and _pointer(binding.get("command_id")) == _pointer(parent.get("current_command_native_id"))
        and _pointer(binding.get("handler_runner_id")) == _pointer(parent.get("selector_owner", {}).get("handler_runner_id"))
        and binding.get("disposed") is False, "Secondary actual native context owner differs")
    _pointer(binding.get("effect_context_id"))
    for key, source in (("card_guid", "source_card_guid"), ("drink_id", "source_drink_id")):
        _require(pending.get(key) is None or parent.get(source) == pending[key], "Pending played resource differs from selector parent")
    limits, rows, ui = dto.get("constraints", {}), dto.get("candidate_cards"), snapshot.get("ui_state", {})
    low, high = limits.get("pick_min"), limits.get("pick_max")
    _require(type(low) is int and type(high) is int and 0 <= low <= high <= 4096
        and type(limits.get("is_hand")) is bool and bool(limits.get("is_hand_source")), "Actual native selector constraints/isHand required")
    _require(isinstance(rows, list) and 0 < len(rows) <= 4096 and limits.get("offered_count") == len(rows)
        and low <= len(rows) and ui.get("minimum") == low and ui.get("maximum") == high,
        "Ordinary complete native offered pool and matching UI bounds required")
    # Empty native selections complete inside the game's original handler and
    # do not open this UI. Never fabricate a forced-empty UI response here.
    ui_pool = dto.get("ui_pool")
    _require(isinstance(ui_pool, list) and len(ui_pool) == len(rows)
        and len(ui.get("candidates", [])) == len(rows), "Native/UI offered pool denominator differs")
    legal, gaps, ui_indices = [], [], []
    state_refs = state["references"]["RefIds"]
    for ordinal, row in enumerate(rows):
        _require(type(row.get("native_ordinal")) is int and row["native_ordinal"] == ordinal,
            "Native offered ordinal changed")
        zone, index, ui_index = row.get("native_zone_type"), row.get("native_zone_index"), row.get("ui_index")
        _require(type(zone) is int and zone in _ZONES and type(index) is int and 0 <= index < len(state[_ZONES[zone]])
            and type(ui_index) is int and 0 <= ui_index < len(rows) and ui_index not in ui_indices,
            "Native zone/position/UI pointer binding is incomplete or ambiguous")
        _require(row.get("ui_binding_source") in ("same-native-card-object", "same-observed-cardData-object"),
            "Actual native card pointer must bind the UI candidate")
        _pointer(row.get("native_object_id")); _pointer(row.get("position_object_id"))
        card = row.get("card")
        _require(isinstance(card, Mapping), "Actual candidate ExamCardData serialization required")
        runtime = state[_ZONES[zone]][index]
        _require(_same(_resolved(card, card.get("references", {}).get("RefIds", [])), _resolved(runtime, state_refs)),
            "Native offered card differs from its original state zone payload")
        data = runtime["_cardData"]
        identity = {"offered_ordinal": ordinal, "card_data_present": True, "id": data["_id"],
            "raw_upgrade_count": data["_upgradeCount"], "guid": runtime["_guid"],
            "runtime_class": "Campus.InGame.Card.ExamCardData", "native_customize_counts": data["_customizeCountList"],
            "native_zone_type": zone, "native_zone_name": _ZONE_NAMES[zone], "native_zone_index": index}
        legal.append(_candidate(identity, ordinal, state, gaps)); ui_indices.append(ui_index)
    selected = dto.get("selected_ui_indices")
    _require(isinstance(selected, list) and all(type(x) is int and x in ui_indices for x in selected)
        and len(set(selected)) == len(selected) and len(selected) <= high
        and ui.get("selected_count") == len(selected), "Original selected UI index order is invalid")
    _require(dto.get("selected_index_source") == "original ProduceCardSelectorOverlayPresenter.GetSelectIndex 06005e75",
        "Original native selection order getter required")
    for index, row in enumerate(ui_pool):
        _require(row.get("index") == index and type(row.get("selected")) is bool
            and row["selected"] == (index in selected), "UI selection flags differ from original ordered indexes")
    command_blob = dto.get("command")
    _require(isinstance(command_blob, Mapping), "Actual current native command serialization required")
    command = {k: deepcopy(v) for k, v in command_blob.items() if k != "references"}
    references = deepcopy(command_blob.get("references", {"RefIds": []}))
    _resolved(command, references.get("RefIds", []))
    current_context = {"command": command, "references": references,
        "command_source": "current-DLL-pending-OnEffectSelectParameterAsync-command",
        "pointer_binding": deepcopy(binding), "effect_context_payload_observed": False}
    observation.update(current_context_local_binding_verified=True, effect_context_payload_observed=False,
        current_context_source=current_context["command_source"], last_log_used_as_current_owner=False,
        pending_request_id=pending["request"]["request_id"], UI_click_history_observed=True,
        ordinary_offered_pool_complete=True, model_source_bound=False)
    return RuntimeNativeSecondaryInput({"state_before": state, "legal_candidates": legal, "current_context": current_context,
        "constraints": {"pick_min": low, "pick_max": high, "offered_count": len(rows), "is_hand": limits["is_hand"],
            "selection_mode": "offered-card-pool", "ordinary_pick_count_rule_applies": True},
        "observation": observation, "ui_indices": ui_indices,
        "selected_ordinals": [ui_indices.index(x) for x in selected], "feature_evidence": _feature_evidence(dto)}, gaps, _authority=_SEAL)


def prepare_live_secondary_feature_input(prepared, view):
    """Separate feature-only values from the immutable actual DLL observation."""
    from .live_feature_presence import LiveFeatureView
    from .native_secondary_input import _plain
    _require(type(prepared) is RuntimeNativeSecondaryInput and type(view) is LiveFeatureView,
        "A verified live secondary observation and sealed feature view are required")
    provenance = view.provenance
    _require(provenance["raw_state_sha256"] == _digest(prepared.state_before)
        and provenance["raw_current_context_sha256"] == _digest(prepared.current_context),
        "Live feature view belongs to another actual state or command context")
    record = _plain(prepared._record)
    record["state_before"], record["current_context"] = view.state, view.current_context
    for candidate in record["legal_candidates"]:
        candidate["runtime_card"] = deepcopy(record["state_before"][candidate["state_zone"]][candidate["native_zone_index"]])
    record["observation"]["feature_view"] = provenance
    record["observation"]["feature_view_is_raw_native_snapshot"] = False
    record["observation"]["original_raw_state_sha256"] = provenance["raw_state_sha256"]
    return RuntimeNativeSecondaryInput(record, prepared.gaps, _authority=_SEAL)


class VerifiedLiveMasterCatalog:
    """Resolve actual MasterManager versions through pinned recorded pairs.

    The native Master hash remains unobserved. The returned hash is an explicit
    unique archive-version binding, never an invented DLL observation. Unknown
    versions and ambiguous historical mappings are rejected.
    """
    def __init__(self, contract_set_reference, inventory_reference=None, *, runtime_encoder=None):
        from .native_structure_contract_set import validate_native_structure_contract_set
        self._stamps = []
        self.contract_set = self._read(contract_set_reference)
        validate_native_structure_contract_set(self.contract_set)
        self._runtime_encoder = runtime_encoder
        if runtime_encoder is not None:
            from .runtime_master_features import RuntimeMasterFeatureEncoder
            _require(type(runtime_encoder) is RuntimeMasterFeatureEncoder
                and _same(runtime_encoder.contract_set, self.contract_set),
                "Runtime Master routes must preserve the exact trained contract set")
            runtime_encoder.validate_runtime_source()
        self.inventory_reference = deepcopy(inventory_reference or MASTER_INVENTORY_REFERENCE)
        inventory = self._read(self.inventory_reference)
        _require(inventory.get("schema") == "gkms.replay-master-version-inventory.v1", "Historical Master inventory schema differs")
        dataset = {"path": inventory["source_dataset"], "sha256": inventory["source_dataset_sha256"]}
        self._check(dataset)
        counts = defaultdict(Counter)
        with Path(dataset["path"]).open(encoding="utf-8") as stream:
            for line in stream:
                row = json.loads(line)
                counts[row["master_hash"]][row["master_version"]] += 1
        _require(set(counts) == set(inventory["versions"]), "Recorded Master inventory denominator differs")
        self._versions = defaultdict(set)
        for master, versions in counts.items():
            observed = inventory["versions"][master]
            _require(observed.get("master_hash") == master and dict(versions) == observed.get("master_versions")
                and sum(versions.values()) == observed.get("episode_count"), "Recorded Master version counts differ")
            for version in versions:
                self._versions[version].add(master)
        self._check(dataset)
        self._databases = {}
        for contract in self.contract_set["contracts"].values():
            native = contract["native_structure_contract"]
            master = native["source_master_hash"]
            archive = self._read(native["source_archive_manifest_reference"])
            _require(archive.get("master_hash") == master and archive.get("upstream_commit_subject") == master
                and archive.get("upstream_commit") in inventory["versions"][master]["matching_upstream_commits"],
                "Master archive does not bind the recorded upstream identity")
            source_inventory = self._read(native["source_inventory_reference"])
            rows = [x for x in source_inventory["versions"] if x["master_hash"] == master]
            _require(len(rows) == 1 and rows[0]["source_manifest"] == native["source_archive_manifest_reference"],
                "Model source Master archive differs from recorded version binding")
            database = rows[0]["source_database"]
            self._check(database)
            self._databases[master] = deepcopy(database)
        self.validate_unchanged()

    def _check(self, reference):
        path = Path(reference["path"]).resolve()
        before = path.stat()
        _require(sha256_file(path) == reference.get("sha256"), "Live model source reference SHA differs: " + str(path))
        after = path.stat()
        stamp = (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        _require(stamp == (before.st_size, before.st_mtime_ns, before.st_ctime_ns), "Source changed during verification")
        self._stamps.append((path, stamp))

    def _read(self, reference):
        self._check(reference)
        value = json.loads(Path(reference["path"]).read_bytes())
        self.validate_unchanged()
        return value

    def validate_unchanged(self):
        for path, expected in self._stamps:
            stat = path.stat()
            _require((stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns) == expected, "Model source changed during live policy execution")
        runtime_encoder = getattr(self, '_runtime_encoder', None)
        if runtime_encoder is not None:
            runtime_encoder.validate_runtime_source()

    def resolve(self, execution_master, produce_id):
        from .native_structure_contract_set import resolve_native_structure_contract
        self.validate_unchanged()
        _require(isinstance(execution_master, Mapping)
            and execution_master.get("schema") == "gkms.native-execution-master-observation.v1"
            and execution_master.get("authority") == "native-existing-MasterManager"
            and execution_master.get("ready") is True and execution_master.get("manager_count") == 1
            and execution_master.get("master_update_succeeded") is True
            and execution_master.get("master_tables_initialized") is True, "Current native MasterManager is not ready")
        _pointer(execution_master.get("manager_instance_id"))
        version = execution_master.get("execution_master_version")
        runtime_encoder = self._runtime_encoder
        if runtime_encoder is not None:
            bound = runtime_encoder.runtime_master_binding(version, produce_id)
            if bound is not None:
                _require(version not in self._versions,
                    "Explicit runtime Master version conflicts with a historical source binding")
                _require(execution_master.get("execution_master_hash") in (None, bound["source_master_hash"]),
                    "Observed Master hash contradicts the explicit current source binding")
                bound["provenance"].update(execution_master=deepcopy(execution_master),
                    native_master_hash_observed=execution_master.get("execution_master_hash") is not None)
                return bound
        matches = self._versions.get(version, set())
        _require(len(matches) == 1, "Unknown or ambiguous current Master version: " + str(version))
        master = next(iter(matches))
        _require(execution_master.get("execution_master_hash") in (None, master), "Observed Master hash contradicts version binding")
        contract = resolve_native_structure_contract(self.contract_set, source_master_hash=master,
            produce_id=produce_id, native_core_sha256=NATIVE_CORE_SHA256, native_metadata_sha256=NATIVE_METADATA_SHA256)
        return {"source_master_hash": master, "contract": contract, "source_database": deepcopy(self._databases[master]),
            "provenance": {"authority": "unique-recorded-Master-version-to-model-archive-binding",
                "execution_master": deepcopy(execution_master), "inventory_reference": deepcopy(self.inventory_reference),
                "native_master_hash_observed": execution_master.get("execution_master_hash") is not None,
                "training_admitted": False, "live_policy_win_proven": False}}


__all__ = ["prepare_live_primary", "prepare_live_secondary", "RuntimeLivePrimaryInput",
    "RuntimeNativeSecondaryInput", "VerifiedLiveMasterCatalog", "MASTER_INVENTORY_REFERENCE", "prepare_live_secondary_feature_input"]
