"""Validate the original PC decision envelope without projecting or running it.

The full snapshot, offered pool and selector context stay separate. In particular
an ordered secondary response is ONE native action, not a succession of invented
intermediate game states. These envelopes still contain private/hidden fields;
only a reviewed information-state projector may turn them into model inputs.
"""
from __future__ import annotations

from dataclasses import dataclass

from .contracts import ContractError, FrozenJSON, digest, integer, sha256, text

OBSERVATION_SCHEMA = "gkms.original-pc-policy-decision.v1"
SNAPSHOT_SCHEMA = "gkms.original-pc-machine-state-snapshot.v1"
CONTEXT_SCHEMA = "gkms.original-pc-current-selector-context.v1"
MAX_CHOICES = 4096


def checked_snapshot(value: dict, *, expected_sha256: str) -> FrozenJSON:
    """Verify actual serialized bytes, not just the presence of a hash string."""
    sha256(expected_sha256, "expected native state")
    if (type(value) is not dict or value.get("schema") != SNAPSHOT_SCHEMA
            or value.get("mode") != "direct-native-projection"
            or value.get("state_complete") is not True or value.get("pure") is not True
            or value.get("read_errors") != [] or value.get("unqualified") != []):
        raise ContractError("complete direct-native snapshot required")
    raw = value.get("state")
    if (type(raw) is not dict or value.get("state_sha256") != expected_sha256
            or digest(raw) != expected_sha256):
        raise ContractError("native snapshot bytes/hash differ")
    if type(raw.get("isExamEndComplete")) is not bool:
        raise ContractError("native terminal marker missing")
    return FrozenJSON.of(value)


def _pointer(value, name):
    # Compare native owner references locally; never feed them to a network.
    text(value, name)
    try:
        number = int(value, 16 if value.startswith("0x") else 10)
    except ValueError as error:
        raise ContractError("invalid native owner reference") from error
    if not 0 < number < 2**64:
        raise ContractError("invalid native owner reference")
    return number


def _action(value):
    if (type(value) is not dict or set(value) != {"type", "indexes"}
            or type(value["type"]) is not int or value["type"] not in (1, 2, 3, 4)
            or type(value["indexes"]) is not list or len(value["indexes"]) > MAX_CHOICES
            or any(type(i) is not int or i < 0 for i in value["indexes"])):
        raise ContractError("invalid native type/indexes action")
    return FrozenJSON.of(value)


@dataclass(frozen=True, slots=True)
class NativeDecision:
    """A checked envelope, not a native clone or feature-admission certificate."""
    observation: FrozenJSON

    @property
    def identity(self):
        return self.observation.sha256

    @property
    def kind(self):
        return self.observation.unpack()["decision_type"]

    @property
    def state_sha256(self):
        return self.observation.unpack()["snapshot"]["state_sha256"]

    def validate_action(self, action):
        candidate = _action(action)
        action = candidate.unpack()
        obs = self.observation.unpack()
        pool = obs["candidates"]
        if self.kind == "main":
            allowed = ({"type": r["type"], "indexes": r["indexes"]} for r in pool["legal_actions"])
            if candidate.unpack() not in allowed:
                raise ContractError("action is not in the current native main pool")
        else:
            indexes = action["indexes"]
            if action["type"] != 4:
                raise ContractError("secondary response requires native type 4")
            if pool["selection_mode"] == "forced-empty-response":
                if indexes:
                    raise ContractError("forced-empty requires exactly []")
            elif (len(set(indexes)) != len(indexes)
                  or not pool["pick_min"] <= len(indexes) <= pool["pick_max"]
                  or any(i >= len(pool["candidates"]) for i in indexes)):
                raise ContractError("secondary response violates offered ordinals/count")
        return candidate


def inspect_native_decision(observation, *, expected_observation_sha256):
    """Bind an original callback to its snapshot, legal pool and selector owner.

    expected_observation_sha256 comes from the accepted execution journal. This
    function verifies protocol consistency; the source engine/metadata/Master and
    any information projection still need their separate qualification.
    """
    sha256(expected_observation_sha256, "expected native observation")
    frozen = FrozenJSON.of(observation)
    if frozen.sha256 != expected_observation_sha256:
        raise ContractError("native observation digest differs from its receipt")
    obs = frozen.unpack()
    if (type(obs) is not dict or obs.get("schema") != OBSERVATION_SCHEMA
            or obs.get("decision_type") not in {"main", "secondary"}
            or obs.get("teacher_action_present") is not False):
        raise ContractError("original owned policy decision required")
    run_id = text(obs.get("run_id"), "native run")
    engine = obs.get("engine_identity")
    if type(engine) is not dict or engine.get("run_id") != run_id:
        raise ContractError("native engine/run identity differs")
    integer(engine.get("worker_pid"), "native worker PID", 1)
    before = obs.get("snapshot")
    if type(before) is not dict:
        raise ContractError("native snapshot object required")
    state_sha = sha256(before.get("state_sha256"), "native state")
    checked_snapshot(before, expected_sha256=state_sha)
    checked_snapshot(obs.get("after_candidates"), expected_sha256=state_sha)
    if before["state"]["isExamEndComplete"]:
        raise ContractError("decision callback cannot be a completed exam")
    pool = obs.get("candidates")
    if (type(pool) is not dict or pool.get("purity_verified") is not True
            or pool.get("state_before_sha256") != state_sha
            or pool.get("state_after_sha256") != state_sha or pool.get("read_errors") != []):
        raise ContractError("offered pool lacks matching native purity receipt")
    kind = obs["decision_type"]
    if kind == "main":
        if (pool.get("schema") != "gkms.original-pc-main-candidates.v1"
                or pool.get("complete") is not True or type(before["state"].get("phase")) is not int
                or before["state"]["phase"] != 6):
            raise ContractError("complete native Main pool required")
        actions = pool.get("legal_actions")
        if type(actions) is not list or not 0 < len(actions) <= MAX_CHOICES:
            raise ContractError("bounded nonempty native legal action list required")
        seen = set()
        for row in actions:
            if type(row) is not dict or "type" not in row or "indexes" not in row:
                raise ContractError("malformed native main candidate")
            action = _action({"type": row["type"], "indexes": row["indexes"]})
            if row["type"] not in (1, 2, 3) or len(row["indexes"]) != (0 if row["type"] == 3 else 1):
                raise ContractError("main candidate has incompatible native action shape")
            if action.encoded in seen:
                raise ContractError("duplicate native main candidate")
            seen.add(action.encoded)
    else:
        if pool.get("schema") != "gkms.original-pc-secondary-candidates.v1":
            raise ContractError("native secondary pool required")
        minimum = integer(pool.get("pick_min"), "pick_min")
        maximum = integer(pool.get("pick_max"), "pick_max")
        rows = pool.get("candidates")
        if (minimum > maximum or maximum > MAX_CHOICES or type(rows) is not list
                or len(rows) > MAX_CHOICES or type(pool.get("is_hand")) is not bool):
            raise ContractError("invalid native secondary bounds/pool")
        if pool.get("selection_mode") == "forced-empty-response":
            if rows or pool.get("forced_empty_entry_qualified") is not True:
                raise ContractError("unqualified native forced-empty response")
        elif (pool.get("selection_mode") != "offered-card-pool" or not rows
              or pool.get("choices_qualified") is not True or pool.get("offered_pool_complete") is not True
              or minimum > len(rows)):
            raise ContractError("incomplete native offered-card pool")
        else:
            for i, row in enumerate(rows):
                if type(row) is not dict or type(row.get("offered_ordinal")) is not int or row["offered_ordinal"] != i:
                    raise ContractError("offered ordinal does not match native pool order")
        context = obs.get("decision_context")
        if (type(context) is not dict or context.get("schema") != CONTEXT_SCHEMA
                or context.get("complete") is not True or context.get("read_errors") != []
                or context.get("unqualified") != [] or type(context.get("command")) is not dict
                or type(context.get("effect_context")) is not dict):
            raise ContractError("complete native selector command/context required")
        owners = context.get("pointer_binding")
        if type(owners) is not dict or owners.get("context_parameter_matches_sequence") is not True:
            raise ContractError("selector context is not bound to the original sequence")
        for name in ("command", "effect_context", "parameter"):
            if _pointer(owners.get(name), name) != _pointer(obs.get(name + "_identity"), name):
                raise ContractError("selector callback owner differs")
        if pool["selection_mode"] != "forced-empty-response":
            for name in ("command", "effect_context"):
                if _pointer(pool.get(name + "_identity"), name) != _pointer(owners[name], name):
                    raise ContractError("selector offered pool belongs to another callback")
    return NativeDecision(frozen)
