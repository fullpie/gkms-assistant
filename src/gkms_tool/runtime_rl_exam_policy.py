"""Private fixed shared actor through the existing live exam owner boundary.

Only numerical projection and policy inference are added. Current DLL reads,
Master binding, primary legality, selector ownership, original UI targets and
pending settlement remain the same host contracts used by the BC policies.
"""
from copy import deepcopy
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace

import torch

from .runtime_gui_exam_policy import (
    RuntimeGuiExamPolicy, RuntimeGuiExamAction, RuntimeGuiExamDecision,
    RuntimeGuiExamBlocker, _actual_secondary_target, _scope,
)
from .runtime_live_exam_input import (prepare_live_primary, prepare_live_secondary_feature_input,
    VerifiedLiveMasterCatalog)
from .runtime_loader_compatibility import load_runtime_loader_compatibility
from .runtime_master_source import load_runtime_master_source
from .runtime_master_features import RuntimeMasterFeatureEncoder
from .runtime_optional_parent_features import RuntimeOptionalParentCardFeatures
from .live_feature_presence import prepare_live_feature_view
from .integrated_exam_bc_model import SecondaryConstraints
from .training_artifact_io import sha256_file
from .observed_empty_collections import PC_SOURCE
from .rl.actor_handoff import (ActorBinding, POLICY_ID, OBJECTIVE_ID, SECONDARY_POLICY_ID,
    capture_primary_lease, resolve_primary_proposal, capture_secondary_lease, resolve_secondary_proposal)
from .rl.contracts import ContractError, DecisionKind, FrozenJSON, State, digest
from .rl.expert_projection import _scope as model_scope
from .rl.game_projection import project_prepared_information
from .rl.native_actor_policy import load_actor_checkpoint, prepare_actor_projector, build_actor_batch
from .rl.offline_policy import OrderedAction
from .rl.private_actor_assets import load_private_actor_descriptor
from .rl.score_prediction import greedy_actions_with_value, from_remaining_value, unavailable


class RuntimeSharedActorPolicy(RuntimeGuiExamPolicy):
    """One frozen actor/run/source; learned main and sequential selector choices."""

    def __init__(self, *, produce_id, idol_card_id, run_id):
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("A stable cultivation run ID is required for private RL")
        from .portable_actor_assets import load_configured_actor_descriptor, initialize_portable_actor_policy
        descriptor = load_configured_actor_descriptor(refresh=True)
        if descriptor is None:
            raise ValueError("The fixed RL model is not qualified and registered")
        if descriptor.get('portable_actor'):
            initialize_portable_actor_policy(self, descriptor, produce_id=produce_id,
                                            idol_card_id=idol_card_id, run_id=run_id)
            return
        spec = descriptor["specification"]
        from .runtime_live_exam_input import validate_runtime_reference_trial
        self.reference_trial = None
        self._reference_preflight_owner = None
        if descriptor.get('runtime_reference_trial') is not None:
            trial = descriptor['runtime_reference_trial']
            self.reference_trial = validate_runtime_reference_trial(trial,
                checkpoint_sha256=spec['checkpoint']['sha256'], runtime_source_sha256=spec['runtime_master_source']['sha256'],
                reference_master_hash=trial['reference_master_hash'], idol_card_id=idol_card_id, produce_id=produce_id)
        self.idol, self.flow_scope = _scope(produce_id, idol_card_id, POLICY_ID)
        self.produce_id, self.idol_card_id, self.run_id = produce_id, idol_card_id, run_id
        self.variant_id = POLICY_ID
        self.model_reference = deepcopy(spec["checkpoint"])
        self.model, self.metadata = load_actor_checkpoint(self.model_reference,
            dataset_identity=spec["dataset_identity"], training_source_code=spec["training_source_code"],
            **({'observed_source_code':spec['observed_source_code']}if 'observed_source_code'in spec else{}))
        self.projector, self.training_projection_id, self.loader_relocation = prepare_actor_projector(
            spec["semantic_sources"], runtime_loader_compatibility=spec.get("runtime_loader_compatibility"),
            runtime_io_equivalence=spec.get("runtime_io_equivalence"),
            max_entities=self.metadata["feature_schema"].max_entities,
            lazy_materials=bool(spec.get('runtime_io_equivalence')))
        if self.projector.schema != self.metadata["feature_schema"]:
            raise ContractError("Live actor feature schema differs from the trained shared model")
        sources = spec["semantic_sources"]
        self.loader_compatibility = self.projector.compatibility
        self.source_resolver = (getattr(self.projector,'material_source_resolver',None)
            or self.loader_compatibility.make_source_resolver())
        self.runtime_source = load_runtime_master_source(spec["runtime_master_source"])
        self.runtime_base_projection_id = self.training_projection_id
        from .rl.actor_projection_equivalence import bind_projection_equivalence
        trained_source, parity_watches = bind_projection_equivalence(spec["projection_equivalence"],
            checkpoint_sha256=self.model_reference["sha256"], dataset_identity=self.metadata["dataset_identity"],
            source_master_hash=self.runtime_source.master_hash, feature_schema_sha256=self.projector.schema.identity,
            runtime_base_projection_id=self.runtime_base_projection_id, source_bindings=self.metadata["source_bindings"],
            runtime_master_source=spec["runtime_master_source"])
        if trained_source.get("source_kind") != PC_SOURCE or trained_source.get("objective_id") != OBJECTIVE_ID:
            raise ContractError("Live actor parity must bind the trained original-PC objective")
        self.training_projection_id = trained_source["projection_id"]
        self.projection_equivalence_reference = deepcopy(spec["projection_equivalence"])
        self.encoder = RuntimeMasterFeatureEncoder(sources["contract_set"], sources["original_shared_encoder"],
            runtime_source=self.runtime_source, source_resolver=self.source_resolver,
            loader_compatibility=self.loader_compatibility,historical_encoder=self.projector.encoder)
        self.catalog = VerifiedLiveMasterCatalog(sources["contract_set"], runtime_encoder=self.encoder,
            **({'reference_trial': self.reference_trial, 'runtime_owner': self._reference_owner}
               if self.reference_trial is not None else {}))
        self.runtime_model_compatibility = self.loader_compatibility.validate_runtime_contract(self.encoder.contract)
        self._model_stat = self._stat()
        from . import live_feature_presence, live_phase_counter_view
        self._feature_view_path = Path(live_feature_presence.__file__).resolve()
        self._feature_view_reference = {"schema": live_feature_presence.SCHEMA,
            "path": str(self._feature_view_path), "sha256": sha256_file(self._feature_view_path)}
        self._feature_view_stat = self._file_stamp(self._feature_view_path)
        dependency = Path(live_phase_counter_view.__file__).resolve()
        self._feature_view_dependencies = ((dependency, self._file_stamp(dependency)),)
        self._feature_view_reference["dependencies"] = [{"schema": live_phase_counter_view.SCHEMA,
            "path": str(dependency), "sha256": sha256_file(dependency)}]
        refs = [descriptor["descriptor_reference"], spec["qualification"], spec["projection_equivalence"], self.model_reference,
                spec["runtime_master_source"], *sources.values(), *spec["training_source_code"].values()]
        refs.extend(spec.get('observed_source_code',{}).values())
        if spec.get("runtime_loader_compatibility"):
            refs.append(spec["runtime_loader_compatibility"])
        if spec.get("runtime_io_equivalence"):
            refs.append(spec["runtime_io_equivalence"])
        refs.extend(getattr(self.projector,'allocation_execution',{}).get('sources',()))
        implementation = Path(__file__).resolve()
        self._implementation_reference = {"path": str(implementation), "sha256": sha256_file(implementation)}
        refs.append(self._implementation_reference)
        self._actor_watches = (*tuple((Path(ref["path"]), self._file_stamp(Path(ref["path"]))) for ref in refs), *parity_watches)
        self.actor_binding = ActorBinding(self.model_reference["sha256"], run_id,
            FrozenJSON.of(self.metadata["model_metadata"]), FrozenJSON.of({
                "dataset_identity": spec["dataset_identity"], "training_projection_id": self.training_projection_id,
                "projection_equivalence": spec["projection_equivalence"],
                "runtime_master_source": spec["runtime_master_source"], "qualification": spec["qualification"]}))
        self._binding = {"loaded": True, **self.actor_binding.policy_identity, "label": descriptor["label"],
            "artifact_sha256": self.model_reference["sha256"], "flow_scope": self.flow_scope.binding,
            "runtime_loader_compatibility_sha256": spec.get("runtime_io_equivalence",
                spec.get("runtime_loader_compatibility", sources["loader_compatibility"]))["sha256"],
            "live_feature_view": deepcopy(self._feature_view_reference), "fixed_weights": True, "private_only": True,
            "live_actor_implementation": deepcopy(self._implementation_reference),
            "runtime_master_package": self.encoder.runtime_master_binding(
                self.runtime_source.execution_master_version, produce_id)["runtime_package_binding"]}
        self._session_generation = self._observed_master_identity = None
        if self.reference_trial is not None:
            if self.reference_trial['reference_master_hash'] != self.runtime_source.master_hash:
                raise ContractError('Reference trial does not use the original qualified runtime source')
            self._binding['runtime_reference_trial'] = deepcopy(self.reference_trial)
            self._binding['runtime_reference_trial_sha256'] = digest(self.reference_trial)
            self._binding['source_equivalence_verified'] = False
            self._binding['training_admitted'] = False
        self._semantic_features = {}
        self.qualification = deepcopy(descriptor["qualification"])
        self.last_secondary_decision = None

    @staticmethod
    def _file_stamp(path):
        value = path.stat()
        return value.st_size, value.st_mtime_ns, value.st_ctime_ns

    def _reference_owner(self):
        trial = self.reference_trial
        if self.run_id.startswith('preflight:'):
            owner = self._reference_preflight_owner
            if not isinstance(owner, dict) or owner.get('run_id') is not None:
                raise ContractError('Reference trial preflight lacks its actual native owner')
            return deepcopy(owner)
        from .runtime_live_exam_input import runtime_reference_trial_owner
        return runtime_reference_trial_owner(trial, run_id=self.run_id,
            idol_card_id=self.idol_card_id, produce_id=self.produce_id)

    def _public_reference_owner(self):
        policy = self.public_reference_material_policy
        if self.run_id.startswith('preflight:'):
            owner = self._public_reference_preflight_owner
            if not isinstance(owner, dict) or owner.get('run_id') is not None:
                raise ContractError('Public material preflight lacks the actual native owner')
            return {'native_owner': deepcopy(owner),
                'engine_identity': deepcopy(self._public_reference_observed_engine_identity), 'expected_binding': None}
        from .run_identity import load_run
        from .runtime_live_exam_input import public_reference_material_owner
        stored = load_run(self.run_id).evidence.get('public_reference_materials')
        if not isinstance(stored, dict) or stored.get('policy') != policy or stored.get('run_id') not in (None, self.run_id):
            raise ContractError('Actual run has another or missing public reference-material policy')
        binding = {**deepcopy(stored), 'run_id': self.run_id}
        owner = public_reference_material_owner(binding, run_id=self.run_id,
            idol_card_id=self.idol_card_id, produce_id=self.produce_id)
        observed_generation = getattr(self, '_public_reference_observed_generation', None)
        if observed_generation is not None and observed_generation != owner['session_generation']:
            raise ContractError('Actual exam observation belongs to another public material owner')
        return {'native_owner': owner,
            'engine_identity': deepcopy(self._public_reference_observed_engine_identity or binding['engine_identity']),
            'expected_binding': binding}

    def preflight_model_context(self, payload):
        if getattr(self, 'public_reference_material_policy', None) is not None:
            owner = payload.get('native_owner') if isinstance(payload, Mapping) else None
            if (not isinstance(owner, Mapping) or set(owner) != {'run_id', 'session_generation', 'game_pid'}
                    or owner.get('run_id') is not None):
                raise ContractError('Public material preflight requires the actual native process/session')
            self._public_reference_preflight_owner = deepcopy(dict(owner))
            self._public_reference_observed_engine_identity = deepcopy(payload.get('engine_identity'))
        if getattr(self,'reference_trial',None) is not None:
            owner = payload.get('native_owner') if isinstance(payload, dict) else None
            if (not isinstance(owner, dict) or set(owner) != {'run_id', 'session_generation', 'game_pid'}
                    or owner['run_id'] is not None or owner['session_generation'] != self.reference_trial['session_generation']
                    or owner['game_pid'] != self.reference_trial['game_pid']):
                raise ContractError('Reference trial requires the actual native pre-AP process/session')
            self._reference_preflight_owner = deepcopy(owner)
        result = super().preflight_model_context(payload)
        if getattr(self, 'public_reference_material_policy', None) is not None:
            binding = deepcopy(self.catalog.reference_material_binding)
            self._binding['public_reference_materials'] = binding
            result.update(public_reference_materials=binding, runtime_policy_binding=self.runtime_policy_binding,
                training_admitted=False, source_equivalence_verified=False, reference_lookup_only=True)
        if getattr(self,'reference_trial',None) is not None:
            result.update(runtime_reference_trial=deepcopy(self.reference_trial), training_admitted=False,
                source_equivalence_verified=False, reference_lookup_only=True)
        return result

    def _validate(self, prepared):
        if any(self._file_stamp(path) != stamp for path, stamp in self._actor_watches):
            raise ValueError("Private RL model or qualification changed during this run")
        if getattr(self,'reference_trial',None) is not None and prepared.observation.get('session_generation') != self.reference_trial['session_generation']:
            raise ContractError('Actual exam observation belongs to another reference-trial session')
        if getattr(self, 'public_reference_material_policy', None) is not None:
            self._public_reference_observed_engine_identity = deepcopy(prepared.observation['engine_identity'])
            self._public_reference_observed_generation = prepared.observation.get('session_generation')
        bound = super()._validate(prepared)
        if getattr(self, 'public_reference_material_policy', None) is not None:
            self._binding['public_reference_materials'] = deepcopy(self.catalog.reference_material_binding)
        return bound

    def _provenance(self, decision_type, binding=None):
        return {"kind": "shared-offline-IQL", **self.runtime_policy_binding,
            "model": deepcopy(self.model_reference), "decision_type": decision_type,
            "dataset_identity": self.metadata["dataset_identity"], "checkpoint_step": self.metadata["checkpoint_step"],
            "feature_schema_sha256": self.projector.schema.identity, "projection_id": self.training_projection_id,
            "runtime_projection_id": self.projector.identity, "runtime_loader_bridge": deepcopy(self.loader_relocation),
            "runtime_base_projection_id": self.runtime_base_projection_id,
            "projection_equivalence": deepcopy(self.projection_equivalence_reference),
            "source_kind": "current-DLL", "teacher_action_used": False, "legacy_rule_fallback": False,
            "automatic_formal_model_activation": False,
            "private_explicit_activation": self.runtime_policy_binding.get('private_only') is True,
            "master_binding": None if binding is None else binding["provenance"],
            "full_coverage": False, "coverage_gaps": deepcopy(self.qualification["coverage_gaps"]),
            "score_prediction_available": True, "score_prediction_calibrated": False,
            "live_workflow_verified": False}

    def _project(self, prepared, bound, *, pre_action_cost=None):
        base, contract = self.encoder._route(bound["source_master_hash"], self.produce_id)
        key = bound["source_master_hash"], self.produce_id
        if key not in self._semantic_features:
            self._semantic_features[key] = RuntimeOptionalParentCardFeatures(base.package,
                active_status_contract=contract.get("observed_active_status_contract"))
        is_secondary = hasattr(prepared, "selected_ordinals")
        projection = prepare_live_feature_view(prepared.state_before, evidence=prepared.feature_evidence,
            current_context=prepared.current_context if is_secondary else None,
            empty_contract=contract["observed_empty_collection_contract"])
        if is_secondary:
            secondary = prepare_live_secondary_feature_input(prepared, projection)
            primary = None
        else:
            actions = [{"type": {"play": 1, "drink": 2, "end_turn": 3}[row["kind"]],
                        "indexes": [] if row["kind"] == "end_turn" else [row["target"]["slot"]]}
                       for row in prepared.original_actions]
            primary = SimpleNamespace(original_actions=actions, legal_candidates=prepared.legal_candidates,
                                      pre_action_cost=pre_action_cost)
            secondary = None
        information, detail = project_prepared_information(projection.state, self._semantic_features[key],
            contract, self.projector.schema, primary=primary, secondary=secondary, source_kind=PC_SOURCE)
        observed = prepared.observation
        state = State(model_scope(projection.state, self.run_id, str(observed["sequence_id"])),
            observed["session_generation"], observed["dto_sha256"],
            DecisionKind.SECONDARY if is_secondary else DecisionKind.MAIN, information, self.actor_binding.identity)
        batch = build_actor_batch(state, detail["candidates"], detail["constraints"], self.projector.schema)
        return state, batch, {**detail, "feature_view_provenance": projection.provenance,
                              "presence_view_ledger": projection.ledger}

    def choose_observation(self, observed):
        try:
            prepared = prepare_live_primary(observed, flow_scope=self.flow_scope)
            if ((observed.native_context or {}).get("idol_card_id") != self.idol_card_id
                    or (observed.native_context or {}).get("exam_config_bound") is not True):
                raise ValueError("Current native idol differs from this fixed model run")
            bound = self._validate(prepared)
            costs = observed.native_snapshot["native_legal_inputs"].get("pre_action_cost")
            state, batch, detail = self._project(prepared, bound, pre_action_cost=costs)
            columns = tuple(range(len(prepared.original_actions)))
            args = {"binding": self.actor_binding, "original_actions": tuple(prepared.original_actions),
                    "candidate_columns": columns, "padded_width": len(columns)}
            lease = capture_primary_lease(state, **args)
            with torch.inference_mode():
                response, value, prediction_error = greedy_actions_with_value(self.model, batch)
            if int(response.lengths[0]) != 1:
                raise ContractError("Main actor must select exactly one legal candidate")
            proposal = resolve_primary_proposal(state, int(response.indices[0, 0]), lease=lease, **args).to_dict()
            self._validate(prepared)
            try:
                prediction = from_remaining_value(value, score=prepared.state_before['parameter'],
                    metadata=self.metadata['model_metadata'], model_sha256=self.actor_binding.model_sha256,
                    run_id=self.run_id, observation=prepared.observation, unavailable_reason=prediction_error)
            except (ValueError, TypeError, KeyError) as error:
                prediction = unavailable(None, 'prediction-input-unavailable:' + type(error).__name__)
            provenance = {**self._provenance("main", bound),
                          'score_prediction_available': prediction.get('available') is True}
            return RuntimeGuiExamDecision(RuntimeGuiExamAction(**proposal["action"]), (), provenance,
                tuple(row["command"] + ":" + str(row["target"].get("slot", "")) for row in prepared.original_actions),
                {**detail, "selected_candidate": proposal["decoder_column"], "observation": prepared.observation,
                    "new_observation_training_qualified": False, "score_prediction": prediction})
        except (OSError, ValueError, TypeError, KeyError, RuntimeError) as error:
            return RuntimeGuiExamDecision(None, (RuntimeGuiExamBlocker("live-RL-input-unqualified", str(error)),),
                self._provenance("main"), metadata={"input_submitted": False, "fallback_used": False})

    def _integrated_secondary(self, snapshot, prepared, bound):
        # The inherited entry already checked pending/model/source/owner. Keep
        # each native click as one existing continuation, never a whole list.
        state, batch, detail = self._project(prepared, bound)
        constraints = SecondaryConstraints.from_native_input(prepared)
        prefix = prepared.selected_ordinals
        constraints.validate_prefix(prefix)
        offered_targets = tuple(None if i in prefix else _actual_secondary_target(snapshot,
            ("card_choice.select", "card_choice.reveal"), prepared.ui_indices[i], prepared)
            for i in range(constraints.offered_count))
        confirm = None
        if snapshot.get("ui_state", {}).get("valid_count") is True and snapshot.get("ui_state", {}).get("confirm_enabled") is True:
            confirm = _actual_secondary_target(snapshot, ("card_choice.confirm",), None, prepared)
        # The original pending is supplied by choose_secondary and retained only
        # for the synchronous callback. It is never rewritten by this adapter.
        pending = self._current_pending
        args = {"binding": self.actor_binding, "constraints": constraints, "selected_ordinals": prefix,
            "offered_ui_indices": prepared.ui_indices, "offered_targets": offered_targets, "confirm_target": confirm,
            "candidate_columns": tuple(range(constraints.offered_count)), "padded_width": constraints.offered_count,
            "pending": FrozenJSON.of(pending), "parent_context": FrozenJSON.of(snapshot["parent_context"]),
            "policy_binding": FrozenJSON.of(self.runtime_policy_binding)}
        lease = capture_secondary_lease(state, **args)
        column = None
        if not constraints.complete(prefix):
            with torch.inference_mode():
                logits, mask = self.model.decoder_logits(batch, OrderedAction.from_sequences([prefix]))
            if not bool(mask[0].any()):
                raise ContractError("Incomplete selector has no legal actor response")
            column = int(logits[0].argmax())
        proposal = resolve_secondary_proposal(state, column, lease=lease, **args).to_dict()
        return proposal["target"], {"status": "ready", "reason": "shared RL selects the next original native response",
            "policy_source": self._provenance("secondary", bound), "trained_secondary_model_used": not constraints.complete(prefix),
            "selected_native_ordinal": proposal["selected_native_ordinal"], "stop_selected": proposal["stop_selected"],
            "ordered_prefix": list(prefix), "feature_metadata": detail, "legacy_rule_fallback": False}

    def choose_secondary(self, snapshot, *, session_generation, pending):
        self._current_pending = deepcopy(pending)
        try:
            return super().choose_secondary(snapshot, session_generation=session_generation, pending=pending)
        except RuntimeError as error:
            detail = {"status": "unavailable", "reason": str(error), "policy_source": self._provenance("secondary"),
                "input_submitted": False, "fallback_used": False}
            self.last_secondary_decision = deepcopy(detail)
            return None, detail
        finally:
            self._current_pending = None


__all__ = ["RuntimeSharedActorPolicy"]
