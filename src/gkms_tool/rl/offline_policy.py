"""GPU-ready shared entity policy and pure IL/IQL update functions.

One observed decision and its complete ordered response form ONE Q transition.
Secondary prefixes and STOP are decoder state, never synthetic game states.
Candidate indices below are local batch columns; callers retain the separate
native target mapping. STOP's padded column is never returned as a native ID.

The explicit objective is remaining score return / 10000, gamma=1. Expectile V
is not the old exam_score head or an expected total-score display. Source/group
qualification and reward construction remain with the existing data pipeline.
Losses follow offline_rl_learner's twin-Q, expectile and clipped AWR contracts;
this module has no dataset loader, training loop, service or game access.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
import math

import torch
from torch import nn
from torch.nn import functional as F

from ..integrated_exam_bc_model import SecondaryConstraints, secondary_teacher_steps
from .contracts import ContractError, finite, integer
from .features import EntityBatch
from .networks import EntityValueNet, NetworkConfig

MODEL_KIND = "gkms.rl.shared-offline-policy.v1"
OBJECTIVE_ID = "remaining_return_iql_v1"
SCORE_SCALE = 10000.0
GAMMA = 1.0
MAIN, SECONDARY = 0, 1
_STATE_FIELDS = ("global_values", "entity_values", "type_ids", "zone_ids", "card_ids", "padding_mask")


def _select_states(states, indices):
    return EntityBatch(*(getattr(states, name).index_select(0, indices) for name in _STATE_FIELDS), states.schema_id)


@dataclass(frozen=True)
class OrderedAction:
    indices: torch.Tensor  # [B,L], selected local columns only; -1 padding
    lengths: torch.Tensor  # [B], empty secondary responses remain empty

    def to(self, device):
        return OrderedAction(self.indices.to(device), self.lengths.to(device))

    @classmethod
    def from_sequences(cls, sequences, *, device="cpu"):
        rows = tuple(tuple(row) for row in sequences)
        if not rows or any(len(row) > 4096 or any(type(i) is not int or i < 0 for i in row) for row in rows):
            raise ContractError("bounded nonnegative ordered candidate columns required")
        values = torch.full((len(rows), max(map(len, rows))), -1, dtype=torch.long, device=device)
        for index, row in enumerate(rows):
            if row:
                values[index, :len(row)] = torch.tensor(row, dtype=torch.long, device=device)
        return cls(values, torch.tensor([len(row) for row in rows], dtype=torch.long, device=device))


@dataclass(frozen=True)
class OfflinePolicyBatch:
    states: EntityBatch
    candidate_values: torch.Tensor  # [B,C,D], already public per-owner semantics
    candidate_types: torch.Tensor  # [B,C], native action families 1/2/3/4; 0 padding
    legal_mask: torch.Tensor  # [B,C], never infer legality from the inventory
    decision_types: torch.Tensor  # [B], MAIN=0 / SECONDARY=1
    minimum: torch.Tensor  # [B], main requires 1
    maximum: torch.Tensor  # [B], main requires 1
    forced_empty: torch.Tensor  # [B], actual native forced-empty protocol

    def to(self, device):
        return OfflinePolicyBatch(self.states.to(device), *(getattr(self, name).to(device) for name in
            ("candidate_values", "candidate_types", "legal_mask", "decision_types", "minimum", "maximum", "forced_empty")))


@dataclass(frozen=True)
class _Rule:
    columns: tuple[int, ...]
    secondary: SecondaryConstraints | None

    def local_prefix(self, columns):
        if any(column not in self.columns for column in columns):
            raise ContractError("expert/prefix action is outside the current legal candidates")
        return tuple(self.columns.index(column) for column in columns)

    def choices(self, prefix, stop_column):
        local = self.local_prefix(prefix)
        if self.secondary is None:
            if len(prefix) > 1:
                raise ContractError("main action has exactly one choice")
            return self.columns if not prefix else ()
        try:
            choices = self.secondary.choices(local)
        except ValueError as error:
            raise ContractError(str(error)) from error
        return tuple(stop_column if i == self.secondary.offered_count else self.columns[i] for i in choices)


def _batch_rules(batch, *, candidate_dim, device):
    if not isinstance(batch, OfflinePolicyBatch) or not isinstance(batch.states, EntityBatch):
        raise ContractError("an explicit offline policy batch is required")
    values = batch.candidate_values
    if (not isinstance(values, torch.Tensor) or values.ndim != 3 or values.shape[0] == 0
            or values.shape[2] != candidate_dim or values.shape[1] > 4096 or values.dtype != torch.float32):
        raise ContractError("candidate semantic tensor shape/dtype differs")
    b, c, _ = values.shape
    expected = {"candidate_types": ((b, c), torch.long), "legal_mask": ((b, c), torch.bool),
                "decision_types": ((b,), torch.long), "minimum": ((b,), torch.long),
                "maximum": ((b,), torch.long), "forced_empty": ((b,), torch.bool)}
    for name, (shape, dtype) in expected.items():
        value = getattr(batch, name)
        if not isinstance(value, torch.Tensor) or value.shape != shape or value.dtype != dtype or value.device != device:
            raise ContractError("offline policy tensor shape/dtype/device differs: " + name)
    if (values.device != device or not torch.isfinite(values).all()
            or batch.states.global_values.shape[0] != b
            or any(getattr(batch.states, name).device != device for name in _STATE_FIELDS)
            or torch.any((batch.candidate_types < 0) | (batch.candidate_types > 4))):
        raise ContractError("offline policy state/candidate values or device differ")
    masks = batch.legal_mask.detach().cpu().tolist()
    types = batch.candidate_types.detach().cpu().tolist()
    decisions = batch.decision_types.detach().cpu().tolist()
    lows, highs = batch.minimum.detach().cpu().tolist(), batch.maximum.detach().cpu().tolist()
    forced = batch.forced_empty.detach().cpu().tolist()
    rules = []
    for mask, kinds, decision, low, high, empty in zip(masks, types, decisions, lows, highs, forced):
        columns = tuple(i for i, legal in enumerate(mask) if legal)
        if decision == MAIN:
            if not columns or empty or (low, high) != (1, 1) or any(kinds[i] not in (1, 2, 3) for i in columns):
                raise ContractError("main requires a nonempty legal pool of play/drink/end and one action")
            constraints = None
        elif decision == SECONDARY:
            if any(kinds[i] != 4 for i in columns):
                raise ContractError("secondary candidates must have action type 4")
            try:
                constraints = SecondaryConstraints(low, high, len(columns),
                    "forced-empty-response" if empty else "offered-card-pool")
            except ValueError as error:
                raise ContractError(str(error)) from error
        else:
            raise ContractError("unknown offline decision type")
        rules.append(_Rule(columns, constraints))
    return tuple(rules)


def _action_rows(actions, rules, *, device, complete):
    if (not isinstance(actions, OrderedAction) or not isinstance(actions.indices, torch.Tensor)
            or not isinstance(actions.lengths, torch.Tensor) or actions.indices.ndim != 2
            or actions.indices.shape[0] != len(rules) or actions.indices.shape[1] > 4096
            or actions.indices.dtype != torch.long or actions.lengths.shape != (len(rules),)
            or actions.lengths.dtype != torch.long or actions.indices.device != device or actions.lengths.device != device):
        raise ContractError("ordered action tensor shape/dtype/device differs")
    lengths = actions.lengths.detach().cpu().tolist()
    padded = actions.indices.detach().cpu().tolist()
    rows, plans = [], []
    for values, length, rule in zip(padded, lengths, rules):
        if not 0 <= length <= len(values) or any(value != -1 for value in values[length:]):
            raise ContractError("ordered action length/padding differs")
        row = tuple(values[:length]); local = rule.local_prefix(row)
        if rule.secondary is None:
            if (complete and length != 1) or (not complete and length > 1):
                raise ContractError("main action needs exactly one expert choice")
            plan = row
        else:
            try:
                rule.secondary.validate_prefix(local)
                supervision = secondary_teacher_steps(rule.secondary, local) if complete else ()
            except ValueError as error:
                raise ContractError(str(error)) from error
            # None is an internal STOP sentinel, not a candidate/native ordinal.
            plan = tuple(None if step.target_ordinal == rule.secondary.offered_count
                         else rule.columns[step.target_ordinal] for step in supervision)
        rows.append(row); plans.append(plan)
    return tuple(rows), tuple(plans)


class OfflinePolicyNet(EntityValueNet):
    model_kind = MODEL_KIND
    objective_id = OBJECTIVE_ID
    gamma = GAMMA
    score_scale = SCORE_SCALE

    def __init__(self, schema, network=NetworkConfig(), *, candidate_dim=512):
        integer(candidate_dim, "candidate semantic dimensions", 1)
        if candidate_dim > 4096:
            raise ContractError("candidate semantic dimensions exceed budget")
        # Same encoder modules and encode implementation; no old total-score head.
        super().__init__(schema, network, _value_heads=False)
        self.candidate_dim = candidate_dim
        d = network.embedding
        self.candidate_proj = nn.Sequential(nn.Linear(candidate_dim, d), nn.LayerNorm(d), nn.GELU())
        self.action_types = nn.Embedding(5, d, padding_idx=0)
        self.actor_init = nn.Linear(d, d)
        self.actor_gru = nn.GRUCell(d, d)
        self.actor_query = nn.Linear(d, d, bias=False)
        self.actor_key = nn.Linear(d, d, bias=False)
        self.actor_score = nn.Linear(d, 1)
        self.actor_stop = nn.Linear(d, 1)
        self.action_init = nn.Linear(d, d)
        self.action_gru = nn.GRUCell(d, d)
        self.q1 = nn.Sequential(nn.Linear(2 * d, 64), nn.GELU(), nn.Linear(64, 1))
        self.q2 = nn.Sequential(nn.Linear(2 * d, 64), nn.GELU(), nn.Linear(64, 1))
        self.expectile_v = nn.Sequential(nn.Linear(d, 64), nn.GELU(), nn.Linear(64, 1))

    def checkpoint_metadata(self):
        network = asdict(self.config); network.pop("outputs")
        return {"model_kind": MODEL_KIND, "objective_id": OBJECTIVE_ID, "gamma": GAMMA, "score_scale": SCORE_SCALE,
                "feature_schema_sha256": self.schema.identity, "network": network, "candidate_dim": self.candidate_dim,
                "q_action": "complete-ordered-response", "candidate_index_semantics": "batch-columns-not-native-ordinals",
                "value_semantics": "expectile-remaining-return-not-total-score-expectation"}

    def _encode_policy(self, batch):
        device = self.global_proj[0].weight.device
        rules = _batch_rules(batch, candidate_dim=self.candidate_dim, device=device)
        context = self.encode(batch.states)[:, 0]
        candidates = self.candidate_proj(batch.candidate_values) + self.action_types(batch.candidate_types)
        return context, candidates, rules

    def state_value(self, states):
        value = self.expectile_v(self.encode(states)[:, 0]).squeeze(-1)
        if not torch.isfinite(value).all():
            raise ContractError("nonfinite remaining-return value")
        return value

    def _action_state(self, context, candidates, rows, gru, initial):
        hidden = torch.tanh(initial(context))
        for step in range(max(map(len, rows), default=0)):
            valid = torch.tensor([step < len(row) for row in rows], dtype=torch.bool, device=context.device)
            indexes = torch.tensor([row[step] if step < len(row) else 0 for row in rows], dtype=torch.long, device=context.device)
            selected = candidates[torch.arange(len(rows), device=context.device), indexes]
            hidden = torch.where(valid[:, None], gru(selected, hidden), hidden)
        return hidden

    def _q_values(self, encoding, rows):
        context, candidates, _ = encoding
        action = self._action_state(context, candidates, rows, self.action_gru, self.action_init)
        joined = torch.cat((context, action), dim=-1)
        values = self.q1(joined).squeeze(-1), self.q2(joined).squeeze(-1)
        if any(not torch.isfinite(value).all() for value in values):
            raise ContractError("nonfinite remaining-return Q")
        return values

    def q_values(self, batch, actions):
        encoding = self._encode_policy(batch)
        rows, _ = _action_rows(actions, encoding[2], device=encoding[0].device, complete=True)
        return self._q_values(encoding, rows)

    def forward(self, batch, actions=None):
        encoding = self._encode_policy(batch)
        result = {"expectile_v": self.expectile_v(encoding[0]).squeeze(-1)}
        if actions is not None:
            rows, _ = _action_rows(actions, encoding[2], device=encoding[0].device, complete=True)
            result["q1"], result["q2"] = self._q_values(encoding, rows)
        if any(not torch.isfinite(value).all() for value in result.values()):
            raise ContractError("nonfinite offline policy output")
        return result

    def _logits(self, hidden, candidates):
        scores = self.actor_score(torch.tanh(self.actor_query(hidden)[:, None] + self.actor_key(candidates))).squeeze(-1)
        result = torch.cat((scores, self.actor_stop(hidden)), dim=1)
        if not torch.isfinite(result).all():
            raise ContractError("nonfinite actor logits")
        return result

    @staticmethod
    def _mask(rules, prefixes, width, device):
        mask = torch.zeros((len(rules), width + 1), dtype=torch.bool, device=device)
        for index, (rule, prefix) in enumerate(zip(rules, prefixes)):
            choices = rule.choices(prefix, width)
            if choices:
                mask[index, list(choices)] = True
        return mask

    def decoder_logits(self, batch, prefixes=None):
        context, candidates, rules = self._encode_policy(batch)
        prefixes = prefixes or OrderedAction.from_sequences([()] * len(rules), device=context.device)
        rows, _ = _action_rows(prefixes, rules, device=context.device, complete=False)
        hidden = self._action_state(context, candidates, rows, self.actor_gru, self.actor_init)
        mask = self._mask(rules, rows, candidates.shape[1], context.device)
        return self._logits(hidden, candidates).masked_fill(~mask, -torch.inf), mask

    def _teacher_statistics(self, encoding, rows, plans, *, anchor=None, anchor_encoding=None):
        context, candidates, rules = encoding
        b, width = candidates.shape[:2]
        hidden = torch.tanh(self.actor_init(context))
        with torch.no_grad():
            other = None if anchor is None else torch.tanh(anchor.actor_init(anchor_encoding[0]))
        nll = context.sum(dim=-1) * 0
        kl = nll.clone()
        prefixes = [() for _ in rules]
        for step in range(max(map(len, plans), default=0)):
            active = torch.tensor([step < len(plan) for plan in plans], dtype=torch.bool, device=context.device)
            targets = torch.tensor([width if step >= len(plan) or plan[step] is None else plan[step]
                                    for plan in plans], dtype=torch.long, device=context.device)
            mask = self._mask(rules, prefixes, width, context.device)
            # Finished rows have no decoder choice. A private computational pad
            # avoids all-inf softmax; it contributes no probability/loss/action.
            mask[~active, width] = True
            logp = F.log_softmax(self._logits(hidden, candidates).masked_fill(~mask, -torch.inf), dim=-1)
            nll = nll + torch.where(active, -logp.gather(1, targets[:, None]).squeeze(1), 0.)
            if anchor is not None:
                with torch.no_grad():
                    anchor_logp = F.log_softmax(anchor._logits(other, anchor_encoding[1]).masked_fill(~mask, -torch.inf), dim=-1)
                difference = torch.where(mask, anchor_logp - logp, torch.zeros_like(logp))
                kl = kl + torch.where(active, (anchor_logp.exp() * difference).sum(dim=-1), 0.)
            chosen = active & (targets != width)
            if width:
                indexes = targets.clamp(max=width - 1)
                selected = candidates[torch.arange(b, device=context.device), indexes]
                hidden = torch.where(chosen[:, None], self.actor_gru(selected, hidden), hidden)
                if anchor is not None:
                    with torch.no_grad():
                        selected_anchor = anchor_encoding[1][torch.arange(b, device=context.device), indexes]
                        other = torch.where(chosen[:, None], anchor.actor_gru(selected_anchor, other), other)
            for index, plan in enumerate(plans):
                if step < len(plan) and plan[step] is not None:
                    prefixes[index] = (*prefixes[index], plan[step])
        return {"nll": nll, "anchor_kl": kl,
                "steps": torch.tensor([len(plan) for plan in plans], dtype=torch.long, device=context.device)}

    def teacher_statistics(self, batch, actions):
        encoding = self._encode_policy(batch)
        rows, plans = _action_rows(actions, encoding[2], device=encoding[0].device, complete=True)
        return self._teacher_statistics(encoding, rows, plans)

    @torch.no_grad()
    def greedy_actions(self, batch):
        context, candidates, rules = self._encode_policy(batch)
        hidden = torch.tanh(self.actor_init(context))
        prefixes, stopped = [() for _ in rules], [False] * len(rules)
        width = candidates.shape[1]
        for _ in range(width + 1):
            mask = self._mask(rules, prefixes, width, context.device)
            for index, stop in enumerate(stopped):
                if stop:
                    mask[index] = False
            active = mask.any(dim=-1)
            if not active.any():
                break
            mask[~active, width] = True
            targets = self._logits(hidden, candidates).masked_fill(~mask, -torch.inf).argmax(dim=-1)
            chosen = active & (targets != width)
            if width:
                selected = candidates[torch.arange(len(rules), device=context.device), targets.clamp(max=width - 1)]
                hidden = torch.where(chosen[:, None], self.actor_gru(selected, hidden), hidden)
            for index, (enabled, target) in enumerate(zip(active.cpu().tolist(), targets.cpu().tolist())):
                if enabled and target == width:
                    stopped[index] = True
                elif enabled:
                    prefixes[index] = (*prefixes[index], target)
        result = OrderedAction.from_sequences(prefixes, device=context.device)
        _action_rows(result, rules, device=context.device, complete=True)
        return result


def _matching_model(model, other):
    if (not isinstance(model, OfflinePolicyNet) or not isinstance(other, OfflinePolicyNet) or model is other
            or model.checkpoint_metadata() != other.checkpoint_metadata()):
        raise ContractError("a separate shared-offline-policy model with matching objective/layout is required")


def make_target_model(model):
    if not isinstance(model, OfflinePolicyNet):
        raise ContractError("shared offline policy required")
    return deepcopy(model).eval().requires_grad_(False)


def _target_parameters(target, model):
    _matching_model(model, target)
    source, destination = dict(model.named_parameters()), dict(target.named_parameters())
    if source.keys() != destination.keys() or any(source[k].shape != destination[k].shape
            or source[k].device != destination[k].device or source[k].data_ptr() == destination[k].data_ptr() for k in source):
        raise ContractError("target parameters differ in shape/device or share storage")
    return source, destination


@torch.no_grad()
def polyak_update(target, model, tau=.005):
    if not 0 <= finite(tau, "Polyak tau") <= 1:
        raise ContractError("Polyak tau must be in [0,1]")
    source, destination = _target_parameters(target, model)
    for key, value in destination.items():
        value.lerp_(source[key], tau)
    target.eval()


def _weights(values, rows, device):
    if values is None:
        return torch.ones(rows, dtype=torch.float32, device=device)
    if (not isinstance(values, torch.Tensor) or values.shape != (rows,) or values.device != device
            or not values.is_floating_point() or not torch.isfinite(values).all() or torch.any(values < 0) or values.sum() <= 0):
        raise ContractError("finite nonnegative decision weights with positive total required")
    return values.detach()


def _weighted(values, weights):
    return (values * weights).sum() / weights.sum().clamp_min(1e-12)


def offline_il_loss(model, batch, actions, *, weights=None):
    statistics = model.teacher_statistics(batch, actions)
    weight = _weights(weights, len(statistics["nll"]), statistics["nll"].device)
    eligible = (statistics["steps"] > 0).to(weight.dtype)
    # Same per-original-decision weighting as the existing secondary IL groups.
    loss = _weighted(statistics["nll"] / statistics["steps"].clamp_min(1), weight * eligible)
    return {"loss": loss, "actor_loss": loss, "actor_decisions": eligible.sum()}


def offline_iql_loss(model, target_model, batch, actions, next_batch, rewards, terminal, *, weights=None,
                     expectile=.7, inverse_temperature=3., advantage_clip=100., anchor_model=None, anchor_kl_weight=0.):
    """Gamma-one TD over normalized score deltas; terminal never bootstraps.

    next_batch may be EntityBatch, OfflinePolicyBatch or None for all-terminal.
    Only real nonterminal next STATES are encoded; no next candidate pool is
    required by IQL. Anchor KL is conditional on the retained teacher prefixes.
    """
    _matching_model(model, target_model)
    if not .5 < finite(expectile, "expectile") < 1:
        raise ContractError("IQL expectile must be between .5 and 1")
    if (finite(inverse_temperature, "inverse temperature", 0) <= 0
            or finite(advantage_clip, "advantage clip", 1) < 1
            or finite(anchor_kl_weight, "anchor KL weight", 0) < 0):
        raise ContractError("invalid IQL loss configuration")
    if anchor_model is None and anchor_kl_weight:
        raise ContractError("anchor KL needs an explicit frozen anchor model")
    encoding = model._encode_policy(batch)
    context, _, rules = encoding
    rows, plans = _action_rows(actions, rules, device=context.device, complete=True)
    b = len(rows)
    if (not isinstance(rewards, torch.Tensor) or rewards.shape != (b,) or rewards.device != context.device
            or not rewards.is_floating_point() or not torch.isfinite(rewards).all()
            or not isinstance(terminal, torch.Tensor) or terminal.shape != (b,) or terminal.dtype != torch.bool
            or terminal.device != context.device):
        raise ContractError("normalized rewards and explicit terminal mask must match the batch")
    weight = _weights(weights, b, context.device)
    with torch.no_grad():
        q1_target, q2_target = target_model.q_values(batch, actions)
        qbar = torch.minimum(q1_target, q2_target)
        next_value = torch.zeros_like(rewards)
        indexes = (~terminal).nonzero(as_tuple=False).flatten()
        if indexes.numel():
            states = next_batch.states if isinstance(next_batch, OfflinePolicyBatch) else next_batch
            if (not isinstance(states, EntityBatch) or states.global_values.shape[0] != b
                    or any(getattr(states, name).device != context.device for name in _STATE_FIELDS)):
                raise ContractError("nonterminal transition needs its actual next state batch")
            next_value[indexes] = model.state_value(_select_states(states, indexes))
        td_target = rewards.detach() + next_value
        anchor_encoding = None
        if anchor_model is not None:
            _matching_model(model, anchor_model)
            anchor_encoding = anchor_model._encode_policy(batch)
    value = model.expectile_v(context).squeeze(-1)
    residual = qbar - value
    expectile_weight = torch.where(residual.detach() >= 0, expectile, 1. - expectile)
    value_loss = _weighted(expectile_weight * residual.square(), weight)
    q1, q2 = model._q_values(encoding, rows)
    q_loss = _weighted(.5 * ((q1 - td_target).square() + (q2 - td_target).square()), weight)
    statistics = model._teacher_statistics(encoding, rows, plans, anchor=anchor_model, anchor_encoding=anchor_encoding)
    actor_weights = weight * (statistics["steps"] > 0).to(weight.dtype)
    advantage = (qbar - value).detach()
    awr = (inverse_temperature * advantage).clamp(min=-30., max=math.log(advantage_clip)).exp()
    awr = awr / _weighted(awr, actor_weights).clamp_min(1e-6)
    actor_loss = _weighted(awr * statistics["nll"] + anchor_kl_weight * statistics["anchor_kl"], actor_weights)
    total = q_loss + value_loss + actor_loss
    if not torch.isfinite(total):
        raise ContractError("nonfinite shared offline IQL loss")
    return {"loss": total, "q_loss": q_loss, "value_loss": value_loss, "actor_loss": actor_loss,
            "td_target": td_target, "target_q": qbar, "advantage": advantage,
            "actor_decisions": (statistics["steps"] > 0).sum()}


def offline_training_step(model, optimizer, batch, actions, *, mode, target_model=None, next_batch=None,
                          rewards=None, terminal=None, gradient_clip=1., target_tau=.005, **loss_options):
    """One caller-owned update only; no schedules, IO, model activation or loop."""
    if finite(gradient_clip, "gradient clip", 0) <= 0:
        raise ContractError("positive gradient clip required")
    if mode == "iql":
        if not 0 <= finite(target_tau, "Polyak tau") <= 1:
            raise ContractError("Polyak tau must be in [0,1]")
        _target_parameters(target_model, model)
    model.train(); optimizer.zero_grad(set_to_none=True)
    if mode == "il":
        losses = offline_il_loss(model, batch, actions, **loss_options)
    elif mode == "iql":
        losses = offline_iql_loss(model, target_model, batch, actions, next_batch, rewards, terminal, **loss_options)
    else:
        raise ContractError("offline update mode must be il or iql")
    updated = mode == "iql" or bool(losses["actor_decisions"].item())
    if updated:
        if not torch.isfinite(losses["loss"]):
            raise ContractError("nonfinite offline loss")
        losses["loss"].backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), gradient_clip, error_if_nonfinite=True)
        optimizer.step()
        if mode == "iql":
            polyak_update(target_model, model, target_tau)
    return {"model_kind": MODEL_KIND, "objective_id": OBJECTIVE_ID, "updated": updated,
            **{name: float(value.detach()) for name, value in losses.items() if value.ndim == 0}}


__all__ = ["MODEL_KIND", "OBJECTIVE_ID", "SCORE_SCALE", "GAMMA", "OfflinePolicyBatch", "OrderedAction",
           "OfflinePolicyNet", "offline_il_loss", "offline_iql_loss", "offline_training_step",
           "make_target_model", "polyak_update"]
