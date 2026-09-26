"""Pure proposal/target adaptation. Does not enable a live policy or submit IO.

Call only after the host's existing source/legality/selector preparation and a
reviewed live-to-State projection. These helpers preserve those supplied targets;
they cannot authenticate a DLL observation, qualify a model or replace Gateway.
"""
from dataclasses import dataclass
from .contracts import (Action, ActionKind, Binding, ContractError, DecisionKind,
                        FrozenJSON, State, finite, integer, text)
from .planner import SearchConfig, SearchResult
from .simulator import SimulationCancelled, validate_actions

SECONDARY_POLICY_ID = "rl-exam-value-v1"  # reserved; not added to Gateway's allowlist


def _check_cancelled(cancelled):
    if not callable(cancelled):
        raise ContractError("cancelled must be callable")
    if cancelled():
        raise SimulationCancelled("stop requested")


def validate_search_binding(root: State, binding: Binding, config: SearchConfig):
    if not isinstance(root, State) or not isinstance(binding, Binding) or not isinstance(config, SearchConfig):
        raise ContractError("typed state/model/planner binding required")
    if root.binding_id != binding.identity or config.identity != binding.planner_sha256:
        raise ContractError("state/model/planner binding differs")


def resolve_proposal(current: State, current_actions, result: SearchResult, *, binding: Binding,
                     config: SearchConfig, cancelled=lambda: False) -> dict:
    _check_cancelled(cancelled)
    validate_search_binding(current, binding, config)
    validate_actions(current, current_actions)
    if (not isinstance(result, SearchResult) or result.status != "ready"
            or not isinstance(result.action, Action) or result.root_identity != current.identity):
        raise ContractError("search result unavailable or stale")
    result.action.validate_at(current)
    matches = [a for a in current_actions if a == result.action]
    if len(matches) != 1:
        raise ContractError("chosen action no longer appears in exact current legal set")
    if (type(result.root_values) is not tuple or not result.root_values
            or any(type(row) is not tuple or len(row) != 2 for row in result.root_values)):
        raise ContractError("immutable complete root estimates required")
    for key, value in result.root_values:
        text(key, "root candidate ID")
        finite(value, "root estimate")
    if len({k for k, _v in result.root_values}) != len(result.root_values):
        raise ContractError("duplicate root estimates")
    integer(result.completed_depth, "completed comparison depth", 1)
    if result.completed_depth > config.max_depth:
        raise ContractError("completed depth exceeds configured search")
    integer(result.nodes, "search nodes")
    integer(result.samples, "search samples")
    finite(result.elapsed_ms, "search elapsed", 0)
    if set(k for k, _v in result.root_values) != {a.candidate_id for a in current_actions}:
        raise ContractError("result did not cover every current root candidate")
    best_id = min(result.root_values, key=lambda row: (-row[1], row[0]))[0]
    if result.action.candidate_id != best_id:
        raise ContractError("selected action disagrees with the completed root comparison")
    target = matches[0].target.unpack()
    if type(target) is not dict:
        raise ContractError("native target must be an object")
    _check_cancelled(cancelled)
    return {"target": target, "policy": "rl_search", "binding_id": binding.identity,
            "root_identity": current.identity, "input_submitted": False,
            "live_qualification_granted": False}


def _kind(state, expected):
    if not isinstance(state, State) or state.kind is not expected:
        raise ContractError("wrong exam decision boundary")


def _action(state, kind, candidate_id, target):
    return Action(kind, candidate_id, FrozenJSON.of(target), state.identity,
                  state.session_generation, state.revision)


def bind_primary_actions(state: State, original_actions) -> tuple[Action, ...]:
    """Freeze RuntimeLivePrimaryInput.original_actions AFTER host validation.

    Native prepare_live_primary remains responsible for observed masks, current
    source/owner, exact slots and prices. Do not use standalone replay indexes
    as live targets. No card identity is inferred from display order here.
    """
    _kind(state, DecisionKind.MAIN)
    if type(original_actions) is not tuple or not original_actions:
        raise ContractError("complete original primary action tuple required")
    actions = []
    kinds = {"play": ActionKind.PLAY, "drink": ActionKind.DRINK, "end_turn": ActionKind.END}
    for row in original_actions:
        if type(row) is not dict or row.get("kind") not in kinds:
            raise ContractError("unknown original primary action")
        kind, target = row["kind"], row.get("target")
        if row.get("command") != "exam." + kind or type(target) is not dict:
            raise ContractError("original primary command/target differs")
        if kind == "end_turn":
            if target:
                raise ContractError("end_turn must preserve the original empty target")
        else:
            key = "card_guid" if kind == "play" else "drink_id"
            if set(target) != {"slot", key}:
                raise ContractError("original primary target fields differ")
            integer(target["slot"], "original slot")
            text(target[key], "original resource identity")
        actions.append(_action(state, kinds[kind], row["command"]+":"+str(target.get("slot", "")), target))
    return validate_actions(state, tuple(actions))


def resolve_primary_proposal(current, current_actions, result, *, original_actions,
                             binding, config, cancelled=lambda: False) -> dict:
    """Return arguments for the existing host action record, not an IO call."""
    _check_cancelled(cancelled)
    if current_actions != bind_primary_actions(current, original_actions):
        raise ContractError("primary proposal mapping differs from the current original pool")
    answer = resolve_proposal(current, current_actions, result, binding=binding, config=config, cancelled=cancelled)
    target = answer["target"]
    answer["action"] = {"kind": result.action.kind.value, "slot_index": target.get("slot"),
        "card_guid": target.get("card_guid"), "drink_id": target.get("drink_id"),
        "selected_card_guid": None, "secondary_policy": SECONDARY_POLICY_ID}
    answer["score_prediction_available"] = False  # root maximum is not a settled-state prediction
    _check_cancelled(cancelled)
    return answer


def _object(value, name):
    if not isinstance(value, FrozenJSON) or type(value.unpack()) is not dict:
        raise ContractError(name + " must be an immutable object")
    return value.unpack()


@dataclass(frozen=True, slots=True)
class SelectorLease:
    """Capture this search's pending identity, never a new transaction owner."""
    root_identity: str
    owner: FrozenJSON
    parent_context: FrozenJSON


def capture_selector_lease(state, *, pending, parent_context, policy_binding, binding, config):
    """Call after prepare_live_secondary; does not replace its native checks.

    Pending ownership metadata stays outside State.information/network input.
    Later resolution checks this same lease before returning a target. Original
    revision, Master and selector constraints must still be checked by Gateway.
    """
    _kind(state, DecisionKind.SECONDARY)
    validate_search_binding(state, binding, config)
    p = _object(pending, "pending transaction")
    parent = _object(parent_context, "parent context")
    policy = _object(policy_binding, "policy binding")
    request = p.get("request")
    if (p.get("schema") != "gkms.runtime-pending-exam.v1" or type(request) is not dict
            or request.get("session_generation") != state.session_generation):
        raise ContractError("secondary requires the original same-session pending request")
    text(request.get("request_id"), "pending request ID")
    if (policy.get("variant_id") != "rl_search" or policy.get("run_id") != state.scope.run_id
            or policy.get("model_sha256") != binding.model_sha256
            or p.get("model_policy_binding") != policy):
        raise ContractError("pending model/run binding differs from RL")
    if (not parent or parent.get("sequence_id") is None or p.get("sequence_id") != parent.get("sequence_id")
            or type(p.get("source_execution_master")) is not dict or not p["source_execution_master"]):
        raise ContractError("pending source/selector owner is missing or different")
    # Capture identity fields, not mutable receipt/progress timestamps.
    owner = {"request": {k: request[k] for k in ("request_id", "session_generation")},
             **{k: p.get(k) for k in ("model_policy_binding", "source_execution_master", "sequence_id",
                                       "card_guid", "drink_id", "source_card_id", "source_card_upgrade")}}
    return SelectorLease(state.identity, FrozenJSON.of(owner), parent_context)


def bind_secondary_actions(state, validated_targets, *, parent_context) -> tuple[Action, ...]:
    """Freeze targets already checked by the host's _actual_secondary_target.

    The host retains min/max, ordered native ordinal/UI mapping, restrictions
    and source validation. This helper must not generate a fake STOP for an
    empty UI, or turn reveal into select. Every emitted target is a copy of a
    supplied current target, including parent_context and instance identity.
    """
    _kind(state, DecisionKind.SECONDARY)
    parent = _object(parent_context, "parent context")
    if type(validated_targets) is not tuple or not validated_targets:
        raise ContractError("nonempty validated secondary target tuple required")
    actions = []
    for target in validated_targets:
        if (type(target) is not dict or target.get("exam_continuation") is not True
                or target.get("parent_context") != parent):
            raise ContractError("secondary target belongs to another selector")
        name = target.get("action_id")
        if name in ("card_choice.select", "card_choice.reveal"):
            index = integer(target.get("index"), "secondary UI index")
            if target.get("selected_before") is not False:
                raise ContractError("secondary target was already selected or unobserved")
            kind, key = ActionKind.SELECT, str(index)
        elif name == "card_choice.confirm":
            integer(target.get("selected_count"), "confirmed selected count")
            kind, key = ActionKind.CONFIRM, ""
        else:
            raise ContractError("unrecognized secondary command")
        actions.append(_action(state, kind, name+":"+key, target))
    return validate_actions(state, tuple(actions))


def resolve_secondary_proposal(current, current_actions, result, *, validated_targets, lease,
                               pending, parent_context, policy_binding, binding, config,
                               cancelled=lambda: False) -> dict:
    _check_cancelled(cancelled)
    fresh = capture_selector_lease(current, pending=pending, parent_context=parent_context,
                                  policy_binding=policy_binding, binding=binding, config=config)
    if not isinstance(lease, SelectorLease) or fresh != lease:
        raise ContractError("secondary search belongs to another pending request/root")
    if current_actions != bind_secondary_actions(current, validated_targets, parent_context=parent_context):
        raise ContractError("secondary proposal mapping differs from the current validated pool")
    answer = resolve_proposal(current, current_actions, result, binding=binding, config=config, cancelled=cancelled)
    answer.update(pending_request_id=fresh.owner.unpack()["request"]["request_id"],
                  secondary_policy_id=SECONDARY_POLICY_ID, score_prediction_available=False)
    _check_cancelled(cancelled)
    return answer
