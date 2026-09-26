"""Schema-driven entity encoding; source-specific native projection is separate.

Input is reviewed canonical information, not arbitrary native memory. Unknown
fields are rejected instead of silently omitted. IDs are definition IDs from
an explicit vocabulary, never pointer/GUID identity features.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Sequence
from .contracts import ContractError, DecisionKind, State, digest, finite, integer, text


def _names(values, name, allow_empty=False):
    if type(values) is not tuple or (not values and not allow_empty) or len(values) != len(set(values)):
        raise ContractError(f"{name}: unique immutable names required")
    for value in values:
        text(value, name)


@dataclass(frozen=True, slots=True)
class FeatureSchema:
    global_names: tuple[str, ...]
    global_scales: tuple[float, ...]
    entity_names: tuple[str, ...]
    entity_scales: tuple[float, ...]
    flows: tuple[str, ...]
    modes: tuple[str, ...]
    stages: tuple[str, ...]
    entity_types: tuple[str, ...]
    zones: tuple[str, ...]
    vocabulary: tuple[str, ...] = ()
    max_entities: int = 512
    version: str = "gkms.rl.entities.v1"

    def __post_init__(self):
        if self.version != "gkms.rl.entities.v1":
            raise ContractError("unsupported feature schema version")
        for name in ("global_names", "entity_names", "flows", "modes", "stages", "entity_types", "zones", "vocabulary"):
            _names(getattr(self, name), name, name == "vocabulary")
        for prefix in ("global", "entity"):
            scales = getattr(self, prefix + "_scales")
            if type(scales) is not tuple or len(scales) != len(getattr(self, prefix + "_names")):
                raise ContractError("normalization dimensions differ")
            if any(finite(s, "feature scale", 0) == 0 for s in scales):
                raise ContractError("feature scales must be positive")
        integer(self.max_entities, "max_entities", 1)
        if self.max_entities > 4096 or self.global_dim > 4096 or self.entity_dim > 4096:
            raise ContractError("feature schema exceeds allocation budget")

    @property
    def identity(self):
        return digest(asdict(self))

    @property
    def global_dim(self):
        return 2 * len(self.global_names) + len(self.flows) + len(self.modes) + len(self.stages) + len(DecisionKind)

    @property
    def entity_dim(self):
        return 2 * len(self.entity_names) + 1  # multiplicity is not a proportion

    @classmethod
    def from_dict(cls, data):
        values = dict(data)
        for key in ("global_names", "global_scales", "entity_names", "entity_scales", "flows", "modes",
                    "stages", "entity_types", "zones", "vocabulary"):
            if key in values:
                values[key] = tuple(values[key])
        return cls(**values)


def _numeric(values, names, scales):
    if type(values) is not dict or set(values) - set(names):
        raise ContractError("undeclared numeric field; update the reviewed schema")
    vector, present = [], []
    for name, scale in zip(names, scales):
        raw = values.get(name)
        vector.append(0. if raw is None else finite(raw, name) / scale)
        present.append(float(raw is not None))
    return vector + present


def _category(value, choices):
    if value not in choices:
        raise ContractError(f"undeclared category: {value}")
    return [float(x == value) for x in choices]


@dataclass(frozen=True)
class EntityBatch:
    global_values: object
    entity_values: object
    type_ids: object
    zone_ids: object
    card_ids: object
    padding_mask: object
    schema_id: str

    def to(self, device):
        return EntityBatch(*(getattr(self, field).to(device) for field in
                             ("global_values", "entity_values", "type_ids", "zone_ids", "card_ids", "padding_mask")),
                           self.schema_id)


def encode_batch(states: Sequence[State], schema: FeatureSchema) -> EntityBatch:
    import torch
    if not states:
        raise ContractError("empty state batch")
    global_rows, entities = [], []
    for state in states:
        obj = state.information.unpack()
        if type(obj) is not dict or set(obj) != {"schema", "global", "entities"} or obj["schema"] != schema.identity:
            raise ContractError("canonical projection/schema mismatch")
        if type(obj["entities"]) is not list or len(obj["entities"]) > schema.max_entities:
            raise ContractError("entity count exceeds schema; never truncate cards")
        row = _numeric(obj["global"], schema.global_names, schema.global_scales)
        for value, choices in ((state.scope.flow_id, schema.flows), (state.scope.mode_id, schema.modes),
                               (state.scope.stage_id, schema.stages), (state.kind.value, tuple(x.value for x in DecisionKind))):
            row += _category(value, choices)
        global_rows.append(row)
        rows = []
        for item in obj["entities"]:
            if type(item) is not dict or set(item) != {"type", "zone", "definition_id", "count", "values"}:
                raise ContractError("entity fields differ from canonical contract")
            if item["type"] not in schema.entity_types or item["zone"] not in schema.zones:
                raise ContractError("unknown entity type/zone")
            text(item["definition_id"], "definition_id")
            integer(item["count"], "count", 1)
            vector = _numeric(item["values"], schema.entity_names, schema.entity_scales) + [float(item["count"])]
            # 0 is padding, 1 is unknown definition; semantic values remain present.
            cid = schema.vocabulary.index(item["definition_id"]) + 2 if item["definition_id"] in schema.vocabulary else 1
            rows.append((vector, schema.entity_types.index(item["type"]) + 1, schema.zones.index(item["zone"]) + 1, cid))
        entities.append(rows)
    width = max(1, max(map(len, entities)))
    values = torch.zeros(len(states), width, schema.entity_dim, dtype=torch.float32)
    tids = torch.zeros(len(states), width, dtype=torch.long)
    zids, cids = torch.zeros_like(tids), torch.zeros_like(tids)
    mask = torch.ones(len(states), width, dtype=torch.bool)
    for b, rows in enumerate(entities):
        for i, (vector, tid, zid, cid) in enumerate(rows):
            values[b, i] = torch.tensor(vector, dtype=torch.float32)
            tids[b, i], zids[b, i], cids[b, i], mask[b, i] = tid, zid, cid, False
    globals_tensor = torch.tensor(global_rows, dtype=torch.float32)
    if not torch.isfinite(values).all() or not torch.isfinite(globals_tensor).all():
        raise ContractError("feature normalization overflow")
    return EntityBatch(globals_tensor, values, tids, zids, cids, mask, schema.identity)
