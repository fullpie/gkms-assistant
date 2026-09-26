"""Pure shared-IQL proposal preparation; no inference, qualification or game IO.

Call after the existing host validates its observation, Master, original primary
actions and secondary targets (``_actual_secondary_target``). The caller also
owns the reviewed live-to-State projection and actor batch. These helpers bind
those supplied objects; they cannot authenticate a DLL or qualify model weights.
The reserved policy identifiers are deliberately absent from the live catalog.
"""
from dataclasses import asdict, dataclass

from ..integrated_exam_bc_model import SecondaryConstraints
from .contracts import ContractError, DecisionKind, FrozenJSON, State, digest, finite, integer, sha256, text
from .runtime import bind_primary_actions
from .simulator import SimulationCancelled

MODEL_KIND = "gkms.rl.shared-offline-policy.v1"
OBJECTIVE_ID = "remaining_return_iql_v1"
POLICY_ID = "rl_shared_iql"
SECONDARY_POLICY_ID = "rl-exam-shared-iql-v1"


def validate_actor_metadata(metadata):
    """Validate the existing OfflinePolicyNet.checkpoint_metadata shape only."""
    frozen = metadata if isinstance(metadata, FrozenJSON) else FrozenJSON.of(metadata)
    value = frozen.unpack()
    expected = {"model_kind": MODEL_KIND, "objective_id": OBJECTIVE_ID,
        "q_action": "complete-ordered-response", "candidate_index_semantics": "batch-columns-not-native-ordinals",
        "value_semantics": "expectile-remaining-return-not-total-score-expectation"}
    if type(value) is not dict or set(value) != {*expected, "gamma", "score_scale", "feature_schema_sha256", "network", "candidate_dim"}:
        raise ContractError("complete shared actor metadata required")
    if any(value[key] != wanted for key, wanted in expected.items()):
        raise ContractError("shared actor model/objective/decoder semantics differ")
    if finite(value["gamma"], "gamma") != 1 or finite(value["score_scale"], "score scale") != 10000:
        raise ContractError("shared actor remaining-return scale differs")
    sha256(value["feature_schema_sha256"], "feature schema")
    if integer(value["candidate_dim"], "candidate dimension", 1) > 4096:
        raise ContractError("candidate dimension exceeds model budget")
    network = value["network"]
    if type(network) is not dict or set(network) != {"embedding", "heads", "layers", "feedforward"}:
        raise ContractError("shared actor network metadata differs")
    for name, number in network.items():
        integer(number, name, 1)
    if (network["embedding"] % network["heads"] or network["embedding"] > 512
            or network["layers"] > 8 or network["feedforward"] > 2048):
        raise ContractError("shared actor network metadata exceeds model budget")
    return frozen


def _object(value, name):
    if not isinstance(value, FrozenJSON) or type(value.unpack()) is not dict or not value.unpack():
        raise ContractError(name + " must be a nonempty immutable object")
    return value.unpack()


@dataclass(frozen=True, slots=True)
class ActorBinding:
    """Frozen supplied model/run/source identity, never a loaded-policy claim."""
    model_sha256: str
    run_id: str
    metadata: FrozenJSON
    source_binding: FrozenJSON

    def __post_init__(self):
        sha256(self.model_sha256, "actor artifact")
        text(self.run_id, "run ID")
        if not isinstance(self.metadata, FrozenJSON):
            raise ContractError("immutable model metadata required")
        validate_actor_metadata(self.metadata)
        _object(self.source_binding, "reviewed source/projection binding")

    @property
    def identity(self):
        return digest({"model_sha256": self.model_sha256, "run_id": self.run_id,
            "metadata": self.metadata.sha256, "source_binding": self.source_binding.sha256})

    @property
    def policy_identity(self):
        return {"variant_id": POLICY_ID, "objective_id": OBJECTIVE_ID, "run_id": self.run_id,
            "model_sha256": self.model_sha256, "actor_binding_sha256": self.identity}


@dataclass(frozen=True, slots=True)
class ActorLease:
    root_identity: str
    binding_id: str
    context: FrozenJSON


@dataclass(frozen=True, slots=True)
class ActorProposal:
    lease: ActorLease
    payload: FrozenJSON

    def to_dict(self):
        return self.payload.unpack()


def _cancelled(cancelled):
    if not callable(cancelled):
        raise ContractError("cancelled must be callable")
    if cancelled():
        raise SimulationCancelled("stop requested")


def _state(state, binding, kind):
    if (not isinstance(state, State) or state.kind is not kind or not isinstance(binding, ActorBinding)
            or state.binding_id != binding.identity or state.scope.run_id != binding.run_id):
        raise ContractError("current actor state/model/run/source binding differs")
    information = state.information.unpack()
    if (type(information) is not dict
            or information.get("schema") != binding.metadata.unpack()["feature_schema_sha256"]):
        raise ContractError("current actor state feature schema differs from model metadata")


def _columns(columns, width, count):
    integer(width, "padded candidate width")
    if (width > 4096 or type(columns) is not tuple or len(columns) != count
            or any(type(i) is not int or not 0 <= i < width for i in columns)
            or columns != tuple(sorted(set(columns)))):
        raise ContractError("complete ordered legal batch columns required; padding is not a candidate")


def _lease(state, binding, context):
    return ActorLease(state.identity, binding.identity, FrozenJSON.of(context))


def _same_lease(lease, fresh):
    if not isinstance(lease, ActorLease) or lease != fresh:
        raise ContractError("actor proposal belongs to stale state, candidates or pending owner")


def _proposal(lease, binding, payload, cancelled):
    result = ActorProposal(lease, FrozenJSON.of({**payload, "policy": POLICY_ID,
        "objective_id": OBJECTIVE_ID, "binding_id": binding.identity, "root_identity": lease.root_identity,
        "model_metadata_sha256": binding.metadata.sha256, "input_submitted": False,
        "live_qualification_granted": False, "score_prediction_available": False}))
    _cancelled(cancelled)
    return result


def capture_primary_lease(state, *, binding, original_actions, candidate_columns, padded_width,
                          cancelled=lambda: False):
    """Freeze host-validated original_actions in their encoded candidate order."""
    _cancelled(cancelled)
    _state(state, binding, DecisionKind.MAIN)
    bind_primary_actions(state, original_actions)
    _columns(candidate_columns, padded_width, len(original_actions))
    return _lease(state, binding, {"original_actions": original_actions,
        "candidate_columns": candidate_columns, "padded_width": padded_width})


def resolve_primary_proposal(state, decoder_column, *, lease, binding, original_actions,
                             candidate_columns, padded_width, cancelled=lambda: False):
    fresh = capture_primary_lease(state, binding=binding, original_actions=original_actions,
        candidate_columns=candidate_columns, padded_width=padded_width, cancelled=cancelled)
    _same_lease(lease, fresh)
    integer(decoder_column, "actor decoder column")
    if decoder_column not in candidate_columns:
        raise ContractError("primary actor selected padding, STOP or an unoffered column")
    selected = original_actions[candidate_columns.index(decoder_column)]
    target = selected["target"]
    return _proposal(fresh, binding, {"target": target, "decoder_column": decoder_column,
        "action": {"kind": selected["kind"], "slot_index": target.get("slot"),
            "card_guid": target.get("card_guid"), "drink_id": target.get("drink_id"),
            "selected_card_guid": None, "secondary_policy": SECONDARY_POLICY_ID}}, cancelled)


def capture_secondary_lease(state, *, binding, constraints, selected_ordinals, offered_ui_indices,
                            offered_targets, confirm_target, candidate_columns, padded_width,
                            pending, parent_context, policy_binding, cancelled=lambda: False):
    """Freeze one selector after host validation, without creating native targets.

    offered_targets is in the complete original offered order. Use None where
    the current host has no selectable target (including already selected rows).
    Each non-None target and confirm_target must come from the host's existing
    _actual_secondary_target validation. The complete offered pool stays in the
    actor batch; decoder masking, not removal/reindexing, excludes the prefix.
    The lease context supplies decoder_prefix_columns for decoder_logits before
    inference; its ordered values must not be sorted or treated as native IDs.
    """
    _cancelled(cancelled)
    _state(state, binding, DecisionKind.SECONDARY)
    if not isinstance(constraints, SecondaryConstraints) or type(selected_ordinals) is not tuple:
        raise ContractError("native constraints and original ordered prefix required")
    try:
        constraints.validate_prefix(selected_ordinals)
    except ValueError as error:
        raise ContractError(str(error)) from error
    count = constraints.offered_count
    _columns(candidate_columns, padded_width, count)
    if (type(offered_ui_indices) is not tuple or len(offered_ui_indices) != count
            or any(type(i) is not int or i < 0 for i in offered_ui_indices)
            or len(set(offered_ui_indices)) != count or type(offered_targets) is not tuple
            or len(offered_targets) != count):
        raise ContractError("complete original ordinal/UI/target mapping required")
    p = _object(pending, "pending transaction")
    parent = _object(parent_context, "selector parent")
    policy = _object(policy_binding, "pending policy binding")
    request = p.get("request")
    if (p.get("schema") != "gkms.runtime-pending-exam.v1" or type(request) is not dict
            or request.get("session_generation") != state.session_generation):
        raise ContractError("same-session pending transaction required")
    text(request.get("request_id"), "pending request ID")
    if (any(policy.get(key) != value for key, value in binding.policy_identity.items())
            or p.get("model_policy_binding") != policy):
        raise ContractError("pending model/run/actor identity differs")
    if (parent.get("sequence_id") is None or p.get("sequence_id") != parent["sequence_id"]
            or type(p.get("source_execution_master")) is not dict or not p["source_execution_master"]):
        raise ContractError("pending original source/selector owner missing or changed")
    for key, source in (("card_guid", "source_card_guid"), ("drink_id", "source_drink_id")):
        if p.get(key) is not None and p[key] != parent.get(source):
            raise ContractError("pending played resource differs from selector parent")
    for ordinal, target in enumerate(offered_targets):
        if target is None:
            continue
        if (type(target) is not dict or target.get("action_id") not in ("card_choice.select", "card_choice.reveal")
                or target.get("index") != offered_ui_indices[ordinal] or type(target.get("index")) is not int
                or ordinal in selected_ordinals or target.get("selected_before") is not False
                or target.get("exam_continuation") is not True or target.get("parent_context") != parent):
            raise ContractError("current offered target differs from native ordinal/parent/prefix")
    if confirm_target is not None and (type(confirm_target) is not dict
            or confirm_target.get("action_id") != "card_choice.confirm"
            or confirm_target.get("exam_continuation") is not True
            or confirm_target.get("parent_context") != parent
            or type(confirm_target.get("selected_count")) is not int
            or confirm_target["selected_count"] != len(selected_ordinals)):
        raise ContractError("current native confirmation differs from original prefix/parent")
    owner = {"request": request,
        **{key: p.get(key) for key in ("model_policy_binding", "source_execution_master", "sequence_id",
            "card_guid", "drink_id", "source_card_id", "source_card_upgrade")}}
    return _lease(state, binding, {"constraints": asdict(constraints), "selected_ordinals": selected_ordinals,
        "decoder_prefix_columns": tuple(candidate_columns[i] for i in selected_ordinals),
        "offered_ui_indices": offered_ui_indices, "offered_targets": offered_targets, "confirm_target": confirm_target,
        "candidate_columns": candidate_columns, "padded_width": padded_width, "owner": owner, "parent_context": parent})


def resolve_secondary_proposal(state, decoder_column, *, lease, binding, constraints, selected_ordinals,
                               offered_ui_indices, offered_targets, confirm_target, candidate_columns,
                               padded_width, pending, parent_context, policy_binding, cancelled=lambda: False):
    """Map one next decoder column; None means a completed/forced-empty response.

    Padded-width STOP is accepted only when the original constraints offer STOP.
    A completed response has no actor choice and requires an actual confirm
    target. No entire ordered response is submitted as a single native click.
    """
    fresh = capture_secondary_lease(state, binding=binding, constraints=constraints,
        selected_ordinals=selected_ordinals, offered_ui_indices=offered_ui_indices, offered_targets=offered_targets,
        confirm_target=confirm_target, candidate_columns=candidate_columns, padded_width=padded_width,
        pending=pending, parent_context=parent_context, policy_binding=policy_binding, cancelled=cancelled)
    _same_lease(lease, fresh)
    complete = constraints.complete(selected_ordinals)
    choices = constraints.choices(selected_ordinals)
    ordinal = None
    if complete:
        if decoder_column is not None:
            raise ContractError("completed native response has no actor decoder choice")
        stop = True
    else:
        integer(decoder_column, "secondary decoder column")
        stop = decoder_column == padded_width
        if stop:
            if constraints.offered_count not in choices:
                raise ContractError("actor STOP precedes the native minimum")
        else:
            if decoder_column not in candidate_columns:
                raise ContractError("secondary actor selected padding or an unoffered column")
            ordinal = candidate_columns.index(decoder_column)
            if ordinal not in choices:
                raise ContractError("secondary actor repeats an already selected ordinal")
    target = confirm_target if stop else offered_targets[ordinal]
    if target is None:
        raise ContractError("actual current native target unavailable; no command synthesized")
    return _proposal(fresh, binding, {"target": target, "decoder_column": decoder_column,
        "selected_native_ordinal": ordinal, "stop_selected": stop, "actor_decision_used": not complete,
        "ordered_prefix": selected_ordinals, "decoder_prefix_columns": tuple(candidate_columns[i] for i in selected_ordinals),
        "pending_request_id": pending.unpack()["request"]["request_id"],
        "secondary_policy_id": SECONDARY_POLICY_ID}, cancelled)


__all__ = ["ActorBinding", "ActorLease", "ActorProposal", "MODEL_KIND", "OBJECTIVE_ID", "POLICY_ID",
    "SECONDARY_POLICY_ID", "validate_actor_metadata", "capture_primary_lease", "resolve_primary_proposal",
    "capture_secondary_lease", "resolve_secondary_proposal"]
