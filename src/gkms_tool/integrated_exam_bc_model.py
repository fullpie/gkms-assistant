"""One shared pointer backbone for primary and ordered secondary BC.

These are numerical primitives, not a dataset admission gate or a trainer.
Callers supply source-qualified encodings through one encoder. A secondary
prefix is decoder state, never an observed intermediate UI/game state. Nothing
here loads a game, fits the corpus, registers a model or activates a policy.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
import math

import numpy as np

from . import behavior_cloning as legacy

SCHEMA = "gkms.integrated-exam-bc-primitives.v1"
MAIN, SECONDARY = 0, 1
_ORIGINAL = ("context_embedding", "candidate_embedding", "w1", "b1", "w2", "b2", "w3", "b3")
_ADDITIONAL = ("secondary_w3", "secondary_b3", "decision_type_embedding")


def initialize_from_primary(primary_parameters, *, secondary_seed=20260914):
    """Copy every primary weight and initialize only a small auxiliary head.

    Decision-type vectors begin at exactly zero, preserving the original
    primary logits. The seeded secondary head is untrained. Adam state is not
    implied by these weights and must be freshly initialized by a future fit.
    """
    if (not isinstance(primary_parameters, Mapping) or set(primary_parameters) != set(_ORIGINAL)
            or type(secondary_seed) is not int or secondary_seed < 0):
        raise ValueError("Exact primary pointer weights and a nonnegative seed are required")
    parameters = {key: np.array(primary_parameters[key], copy=True) for key in _ORIGINAL}
    try:
        dimension = parameters["context_embedding"].shape[1]
        hidden = parameters["w3"].shape[0]
    except IndexError as exc:
        raise ValueError("Malformed primary parameter dimensions") from exc
    rng = np.random.default_rng(secondary_seed)
    parameters["secondary_w3"] = rng.normal(0.0, 0.02, size=hidden).astype(np.float32)
    parameters["secondary_b3"] = np.zeros(1, dtype=np.float32)
    parameters["decision_type_embedding"] = np.zeros((2, dimension), dtype=np.float32)
    validate_parameters(parameters, finite=True)
    return parameters


def validate_parameters(parameters, *, finite=False):
    if not isinstance(parameters, Mapping) or set(parameters) != set(_ORIGINAL + _ADDITIONAL):
        raise ValueError("A single integrated primary/secondary parameter set is required")
    try:
        ctx, candidate = parameters["context_embedding"], parameters["candidate_embedding"]
        if ctx.ndim != 2 or candidate.ndim != 2:
            raise ValueError("Pointer embeddings must be matrices")
        dimension = ctx.shape[1]; h1 = parameters["b1"].shape[0]; h2 = parameters["b2"].shape[0]
        shapes = {"context_embedding": (ctx.shape[0], dimension), "candidate_embedding": (candidate.shape[0], dimension),
            "w1": (3 * dimension, h1), "b1": (h1,), "w2": (h1, h2), "b2": (h2,),
            "w3": (h2,), "b3": (1,), "secondary_w3": (h2,), "secondary_b3": (1,),
            "decision_type_embedding": (2, dimension)}
        if min(ctx.shape[0], candidate.shape[0], dimension, h1, h2) <= 0:
            raise ValueError("Empty integrated model dimensions")
        for name, shape in shapes.items():
            value = parameters[name]
            if not isinstance(value, np.ndarray) or value.dtype != np.float32 or value.shape != shape:
                raise ValueError("Integrated parameter shape/dtype differs: " + name)
            if finite and not np.all(np.isfinite(value)):
                raise ValueError("Nonfinite integrated parameter: " + name)
    except (AttributeError, IndexError, TypeError) as exc:
        raise ValueError("Malformed integrated parameter set") from exc


@dataclass(frozen=True)
class PointerBatch:
    context_indices: np.ndarray
    context_mask: np.ndarray
    candidate_indices: np.ndarray
    candidate_token_mask: np.ndarray
    candidate_mask: np.ndarray
    decision_types: np.ndarray
    candidate_ordinals: tuple[tuple[int, ...], ...] | None = None


def _integer_tokens(row):
    values = np.asarray(row)
    if (values.ndim != 1 or not values.size or values.dtype.kind not in "iu"
            or isinstance(row, (tuple, list)) and any(isinstance(x, (bool, np.bool_)) for x in row)
            or np.any(values < 0) or np.any(values > 2**31 - 1)):
        raise ValueError("Nonempty int32 pointer-index rows are required")
    return values.tolist()


def make_batch(context_rows, candidate_rows, decision_types, *, candidate_ordinals=None):
    """Pad only this requested numerical batch using the existing conventions."""
    contexts = [_integer_tokens(row) for row in context_rows]
    candidates = [[_integer_tokens(row) for row in decision] for decision in candidate_rows]
    types = np.asarray(decision_types)
    if (not contexts or len(contexts) != len(candidates) or types.shape != (len(contexts),)
            or types.dtype.kind not in "iu" or np.any((types != MAIN) & (types != SECONDARY))
            or any(not rows for rows in candidates)
            or isinstance(decision_types, (list, tuple)) and any(isinstance(v, (bool, np.bool_)) for v in decision_types)):
        raise ValueError("Batch rows and observed decision types must align")
    ctx, ctx_mask = legacy._pad_token_rows(contexts)
    cand, cand_tokens, cand_mask = legacy._pad_candidate_token_rows(candidates)
    orders = tuple(tuple(range(len(row))) for row in candidates) if candidate_ordinals is None else tuple(tuple(row) for row in candidate_ordinals)
    if (len(orders) != len(candidates) or any(len(order) != len(row) or len(set(order)) != len(order)
            or any(type(i) is not int or i < 0 for i in order) for order, row in zip(orders, candidates))):
        raise ValueError("Candidate ordinal bindings must match the exact encoded row order")
    return PointerBatch(ctx, ctx_mask, cand, cand_tokens, cand_mask, types.astype(np.int32), orders)


def _validate_batch(batch, parameters):
    if not isinstance(batch, PointerBatch):
        raise ValueError("An explicit integrated pointer batch is required")
    ctx, candidate = batch.context_indices, batch.candidate_indices
    if (ctx.ndim != 2 or candidate.ndim != 3 or not len(ctx) or candidate.shape[0] != len(ctx)
            or ctx.dtype != np.int32 or candidate.dtype != np.int32
            or batch.context_mask.shape != ctx.shape or batch.candidate_token_mask.shape != candidate.shape
            or batch.candidate_mask.shape != candidate.shape[:2] or batch.decision_types.shape != (len(ctx),)
            or batch.decision_types.dtype.kind not in "iu"
            or np.any((batch.decision_types != MAIN) & (batch.decision_types != SECONDARY))):
        raise ValueError("Integrated index/mask/type shapes differ")
    for mask in (batch.context_mask, batch.candidate_token_mask, batch.candidate_mask):
        if mask.dtype != np.float32 or np.any((mask != 0) & (mask != 1)):
            raise ValueError("Explicit float32 zero/one masks are required")
    if (np.any(batch.context_mask.sum(axis=1) == 0) or np.any(batch.candidate_mask.sum(axis=1) == 0)
            or np.any((batch.candidate_token_mask.sum(axis=2) > 0) != (batch.candidate_mask > 0))):
        raise ValueError("Every live candidate/context needs tokens; padded candidates must have none")
    if (np.any(ctx < 0) or np.any(ctx >= len(parameters["context_embedding"]))
            or np.any(candidate < 0) or np.any(candidate >= len(parameters["candidate_embedding"]))):
        raise ValueError("Integrated pointer indices exceed their embedding tables")


@dataclass(frozen=True)
class PointerOutput:
    logits: np.ndarray
    probabilities: np.ndarray


def _forward(parameters, batch):
    validate_parameters(parameters)
    _validate_batch(batch, parameters)
    # Keep the original expression order for the shared numerical backbone.
    context, denominator = legacy._context_vector(parameters["context_embedding"], batch.context_indices, batch.context_mask)
    type_vectors = parameters["decision_type_embedding"][batch.decision_types]
    if np.any(type_vectors):
        context = context + type_vectors
    # Omitting an exact zero addition also preserves signed-zero bits at init.
    candidate_denominator = np.sqrt(np.maximum(batch.candidate_token_mask.sum(axis=2, keepdims=True), 1.0)).astype(np.float32)
    candidate_values = parameters["candidate_embedding"][batch.candidate_indices] * batch.candidate_token_mask[..., None]
    candidates = candidate_values.sum(axis=2) / candidate_denominator
    repeated = np.broadcast_to(context[:, None, :], candidates.shape)
    features = np.concatenate((repeated, candidates, repeated * candidates), axis=2)
    z1 = features @ parameters["w1"] + parameters["b1"]
    h1 = np.maximum(z1, 0.0)
    z2 = h1 @ parameters["w2"] + parameters["b2"]
    h2 = np.maximum(z2, 0.0)
    main_scores = h2 @ parameters["w3"] + float(parameters["b3"][0])
    secondary_scores = h2 @ parameters["secondary_w3"] + float(parameters["secondary_b3"][0])
    scores = np.where(batch.decision_types[:, None] == MAIN, main_scores, secondary_scores)
    scores = np.where(batch.candidate_mask > 0, scores, -1.0e9)
    maximum = scores.max(axis=1, keepdims=True)
    exp = np.exp(scores - maximum) * batch.candidate_mask
    probabilities = exp / np.maximum(exp.sum(axis=1, keepdims=True), 1.0e-12)
    if not np.all(np.isfinite(scores)) or not np.all(np.isfinite(probabilities)):
        raise ValueError("Nonfinite integrated pointer output")
    return PointerOutput(scores, probabilities), {"context": context, "denominator": denominator,
        "candidates": candidates, "candidate_denominator": candidate_denominator,
        "features": features, "z1": z1, "h1": h1, "z2": z2, "h2": h2}


def forward(parameters, batch):
    """Use one shared backbone and the head selected by the observed type."""
    return _forward(parameters, batch)[0]


def loss_and_gradients(parameters, batch, targets, *, weights=None, task_weights=(1.0, 1.0)):
    """Task-balanced BC loss; both heads backpropagate into the same parameters.

    Weights are caller-supplied source/trajectory weights, not rewards. Each
    present decision type is normalized by its own weight sum; task_weights
    combine those losses. A main-only batch retains the original weighted loss.
    """
    result, cache = _forward(parameters, batch)
    labels = np.asarray(targets)
    if (labels.shape != (len(batch.context_indices),) or labels.dtype.kind not in "iu"
            or isinstance(targets, (list, tuple)) and any(isinstance(x, (bool, np.bool_)) for x in targets)
            or np.any(labels < 0) or np.any(labels >= batch.candidate_mask.shape[1])):
        raise ValueError("BC targets must be integer indexes into this decision's candidates")
    labels = labels.astype(np.int32)
    if np.any(batch.candidate_mask[np.arange(len(labels)), labels] != 1):
        raise ValueError("BC target points to padding or a masked candidate")
    if weights is None:
        weights = np.ones(len(labels), dtype=np.float32)
    raw_weights, raw_tasks = np.asarray(weights), np.asarray(task_weights)
    if (raw_weights.dtype.kind not in "iuf" or raw_tasks.dtype.kind not in "iuf"
            or isinstance(weights, (tuple, list)) and any(isinstance(x, (bool, np.bool_)) for x in weights)
            or isinstance(task_weights, (tuple, list)) and any(isinstance(x, (bool, np.bool_)) for x in task_weights)):
        raise ValueError("Source and task weights must be numbers, not Boolean flags")
    weights = np.asarray(raw_weights, dtype=np.float32)
    tasks = np.asarray(raw_tasks, dtype=np.float32)
    if (weights.shape != labels.shape or not np.all(np.isfinite(weights)) or np.any(weights <= 0)
            or tasks.shape != (2,) or not np.all(np.isfinite(tasks)) or np.any(tasks <= 0)):
        raise ValueError("Finite positive source and task weights are required")
    selected = result.probabilities[np.arange(len(labels)), labels]
    nll = -np.log(np.maximum(selected, 1.0e-12))
    present = np.unique(batch.decision_types)
    task_total = float(tasks[present].sum())
    factors = np.zeros(len(labels), dtype=np.float32)
    losses = {}
    loss = 0.0
    for kind in present:
        mask = batch.decision_types == kind
        total = float(weights[mask].sum())
        losses["main" if kind == MAIN else "secondary"] = float((nll[mask] * weights[mask]).sum() / total)
        coefficient = float(tasks[kind]) / task_total
        factors[mask] = weights[mask] / total * coefficient
        loss += losses["main" if kind == MAIN else "secondary"] * coefficient
    dscores = result.probabilities.copy()
    dscores[np.arange(len(labels)), labels] -= 1.0
    dscores *= factors[:, None]
    dscores *= batch.candidate_mask
    gradients = {}
    for kind, w3, b3 in ((MAIN, "w3", "b3"), (SECONDARY, "secondary_w3", "secondary_b3")):
        local = dscores * (batch.decision_types == kind)[:, None]
        gradients[w3] = np.einsum("bmh,bm->h", cache["h2"], local).astype(np.float32)
        gradients[b3] = np.asarray([local.sum()], dtype=np.float32)
    heads = np.where(batch.decision_types[:, None] == MAIN, parameters["w3"], parameters["secondary_w3"])
    dh2 = dscores[..., None] * heads[:, None, :]
    dz2 = dh2 * (cache["z2"] > 0)
    gradients["w2"] = np.einsum("bmi,bmj->ij", cache["h1"], dz2).astype(np.float32)
    gradients["b2"] = dz2.sum(axis=(0, 1)).astype(np.float32)
    dh1 = dz2 @ parameters["w2"].T
    dz1 = dh1 * (cache["z1"] > 0)
    gradients["w1"] = np.einsum("bmi,bmj->ij", cache["features"], dz1).astype(np.float32)
    gradients["b1"] = dz1.sum(axis=(0, 1)).astype(np.float32)
    dfeatures = dz1 @ parameters["w1"].T
    dimension = cache["context"].shape[1]
    dcontext = dfeatures[:, :, :dimension]
    dcandidate = dfeatures[:, :, dimension:dimension * 2]
    dproduct = dfeatures[:, :, dimension * 2:]
    dcontext = (dcontext + dproduct * cache["candidates"]) * batch.candidate_mask[..., None]
    dcandidate = (dcandidate + dproduct * cache["context"][:, None, :]) * batch.candidate_mask[..., None]
    dcontext = dcontext.sum(axis=1)
    types_gradient = np.zeros_like(parameters["decision_type_embedding"])
    np.add.at(types_gradient, batch.decision_types, dcontext)
    gradients["decision_type_embedding"] = types_gradient
    context_gradient = np.zeros_like(parameters["context_embedding"])
    per_token = (dcontext[:, None, :] / cache["denominator"][:, :, None]) * batch.context_mask[..., None]
    np.add.at(context_gradient, batch.context_indices.reshape(-1), per_token.reshape(-1, dimension))
    candidate_gradient = np.zeros_like(parameters["candidate_embedding"])
    per_candidate_token = (dcandidate[:, :, None, :] / cache["candidate_denominator"][:, :, :, None]) * batch.candidate_token_mask[..., None]
    np.add.at(candidate_gradient, batch.candidate_indices.reshape(-1), per_candidate_token.reshape(-1, dimension))
    gradients["context_embedding"] = context_gradient
    gradients["candidate_embedding"] = candidate_gradient
    if not math.isfinite(loss) or any(not np.all(np.isfinite(g)) for g in gradients.values()):
        raise ValueError("Nonfinite integrated BC loss or gradients")
    return loss, gradients, {"loss_by_decision_type": losses, "task_weights": tasks.tolist(),
        "shared_parameter_set": True, "source_qualification_granted": False}


@dataclass(frozen=True)
class SecondaryConstraints:
    minimum: int
    maximum: int
    offered_count: int
    selection_mode: str = "offered-card-pool"

    def __post_init__(self):
        if any(type(v) is not int or not 0 <= v <= 4096 for v in (self.minimum, self.maximum, self.offered_count)) or self.minimum > self.maximum:
            raise ValueError("Original native secondary bounds are invalid")
        if self.selection_mode == "forced-empty-response":
            if self.offered_count != 0:
                raise ValueError("Forced-empty cannot contain offered cards")
        elif self.selection_mode != "offered-card-pool" or self.offered_count == 0 or self.minimum > self.offered_count:
            raise ValueError("Ordinary secondary pool cannot satisfy the native minimum")

    @classmethod
    def from_native_input(cls, prepared):
        from .native_secondary_input import NativeSecondaryInput
        if not isinstance(prepared, NativeSecondaryInput):
            raise ValueError("Use the native secondary structural decoder input, not a claimed source dictionary")
        constraints = prepared.constraints
        count = len(prepared.legal_candidates)
        if constraints.get("offered_count") != count:
            raise ValueError("Prepared native pool count differs from its constraints")
        return cls(constraints["pick_min"], constraints["pick_max"], count, constraints["selection_mode"])

    def validate_prefix(self, prefix):
        values = tuple(prefix)
        if (any(type(v) is not int or not 0 <= v < self.offered_count for v in values)
                or len(values) != len(set(values)) or len(values) > self.maximum):
            raise ValueError("Derived prefix must preserve unique original offered ordinals")
        if self.selection_mode == "forced-empty-response" and values:
            raise ValueError("Forced-empty has only one native response")
        return values

    def complete(self, prefix):
        prefix = self.validate_prefix(prefix)
        return self.selection_mode == "forced-empty-response" or len(prefix) == min(self.maximum, self.offered_count)

    def choices(self, prefix):
        prefix = self.validate_prefix(prefix)
        if self.complete(prefix):
            return ()
        available = tuple(i for i in range(self.offered_count) if i not in prefix)
        # The virtual STOP is never an offered ordinal or part of a native reply.
        return (*available, self.offered_count) if len(prefix) >= self.minimum else available


@dataclass(frozen=True)
class SecondaryStep:
    constraints: SecondaryConstraints
    derived_prefix: tuple[int, ...]
    choice_ordinals: tuple[int, ...]
    prefix_origin: str
    decision_type: str = "secondary"
    intermediate_native_state_observed: bool = False

    @property
    def stop_ordinal(self):
        return self.constraints.offered_count


@dataclass(frozen=True)
class SecondarySupervision:
    step: SecondaryStep
    target_ordinal: int
    target_index: int


def secondary_teacher_steps(constraints, ordered_teacher_ordinals):
    """Factor a retained teacher array; never fabricate UI/prefix snapshots."""
    teacher = constraints.validate_prefix(ordered_teacher_ordinals)
    if constraints.selection_mode == "forced-empty-response":
        return ()
    if not constraints.minimum <= len(teacher) <= constraints.maximum:
        raise ValueError("Teacher response length violates the original native constraints")
    rows = []; prefix = ()
    for target in teacher:
        choices = constraints.choices(prefix)
        if target not in choices:
            raise ValueError("Teacher ordinal is unavailable at its preserved prefix")
        step = SecondaryStep(constraints, prefix, choices, "derived-teacher-forcing-prefix")
        rows.append(SecondarySupervision(step, target, choices.index(target)))
        prefix = (*prefix, target)
    if not constraints.complete(prefix):
        choices = constraints.choices(prefix)
        stop = constraints.offered_count
        if stop not in choices:
            raise ValueError("Teacher response ends before native minimum")
        rows.append(SecondarySupervision(SecondaryStep(constraints, prefix, choices,
            "derived-teacher-forcing-prefix"), stop, choices.index(stop)))
    return tuple(rows)


def decode_secondary(constraints, score_step):
    """Decode one ordered array; no game state is advanced between prefix steps."""
    prefix = (); trace = []
    while not constraints.complete(prefix):
        choices = constraints.choices(prefix)
        step = SecondaryStep(constraints, prefix, choices, "derived-policy-decoder-prefix")
        raw = np.asarray(score_step(step))
        if raw.shape != (len(choices),) or raw.dtype.kind not in "iuf" or not np.all(np.isfinite(raw)):
            raise ValueError("One finite score per currently legal decoder choice is required")
        index = int(raw.argmax())
        ordinal = choices[index]
        trace.append({"derived_prefix": list(prefix), "choice_ordinals": list(choices),
            "chosen_ordinal": ordinal, "stop_chosen": ordinal == constraints.offered_count,
            "intermediate_native_state_observed": False})
        if ordinal == constraints.offered_count:
            break
        prefix = (*prefix, ordinal)
    forced = constraints.selection_mode == "forced-empty-response"
    if not forced and not constraints.minimum <= len(prefix) <= constraints.maximum:
        raise ValueError("Decoded response violates original native bounds")
    return {"action": {"type": 4, "indexes": list(prefix)}, "decoder_trace": trace,
        "selection_mode": constraints.selection_mode, "policy_choice_required": not forced and bool(trace),
        "native_minimum_preserved": constraints.minimum, "native_maximum_preserved": constraints.maximum}


class IntegratedExamBcPolicy:
    """Single decision interface over one encoder and one parameter set.

    The encoder callback accepts ``(observation, secondary_step_or_None)`` and
    returns a one-row PointerBatch. A qualified secondary decoder input is in
    ``observation['secondary_input']``. Primary native responses are the actual
    ``observation['candidates']['legal_actions']``. This primitive is not an
    artifact/source qualification or permission to register an active model.
    """
    def __init__(self, parameters, encoder: Callable):
        validate_parameters(parameters, finite=True)
        self.parameters, self.encoder = parameters, encoder

    def _scores(self, observation, step, kind, count):
        batch = self.encoder(observation, step)
        expected_order = tuple(range(count)) if step is None else step.choice_ordinals
        if (not isinstance(batch, PointerBatch) or batch.decision_types.tolist() != [kind]
                or batch.candidate_mask.shape != (1, count) or np.any(batch.candidate_mask != 1)
                or batch.candidate_ordinals != (expected_order,)):
            raise ValueError("Common encoder changed this decision's type, candidates or ordinal order")
        return forward(self.parameters, batch).logits[0]

    def decide(self, observation):
        kind = observation.get("decision_type")
        if kind == "main":
            actions = observation["candidates"]["legal_actions"]
            if not isinstance(actions, (list, tuple)) or not actions:
                raise ValueError("Actual primary native responses are required")
            scores = self._scores(observation, None, MAIN, len(actions))
            chosen = deepcopy(dict(actions[int(scores.argmax())]))
            result = {"action": {key: chosen[key] for key in ("type", "indexes")}, "decision_type": "main"}
        elif kind == "secondary":
            prepared = observation["secondary_input"]
            constraints = SecondaryConstraints.from_native_input(prepared)
            if constraints.selection_mode != "forced-empty-response" and prepared.gaps:
                raise ValueError("Secondary input gaps must be resolved before learned decoding")
            result = decode_secondary(constraints,
                lambda step: self._scores(observation, step, SECONDARY, len(step.choice_ordinals)))
            result["decision_type"] = "secondary"
            result["input_gaps"] = [{"code": gap.code, "path": gap.path} for gap in prepared.gaps]
        else:
            raise ValueError("Unknown native decision type requires its explicit shared interface contract")
        result["policy_source"] = {"kind": "integrated-exam-bc-primitive", "schema": SCHEMA,
            "shared_parameter_set": True, "joint_training_admission_granted": False,
            "runtime_activation_allowed": False,
            "learned_model_used": kind == "main" or result.get("policy_choice_required") is True}
        return result


__all__ = ["SCHEMA", "MAIN", "SECONDARY", "PointerBatch", "PointerOutput", "make_batch",
    "initialize_from_primary", "validate_parameters", "forward", "loss_and_gradients",
    "SecondaryConstraints", "SecondaryStep", "SecondarySupervision", "secondary_teacher_steps",
    "decode_secondary", "IntegratedExamBcPolicy"]
