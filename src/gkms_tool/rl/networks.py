"""Small entity-attention value network. No per-flow or drink policy network."""
from dataclasses import dataclass
import torch
from torch import nn
from .contracts import ContractError, integer
from .features import EntityBatch, FeatureSchema


@dataclass(frozen=True, slots=True)
class NetworkConfig:
    embedding: int = 128
    heads: int = 4
    layers: int = 2
    feedforward: int = 256
    outputs: tuple[str, ...] = ("exam_score",)

    def __post_init__(self):
        for key in ("embedding", "heads", "layers", "feedforward"):
            integer(getattr(self, key), key, 1)
        if self.embedding % self.heads or self.embedding > 512 or self.layers > 8 or self.feedforward > 2048:
            raise ContractError("invalid/budget-exceeding network shape")
        if (type(self.outputs) is not tuple or not self.outputs or len(set(self.outputs)) != len(self.outputs)
                or set(self.outputs) - {"exam_score", "run_goal"}):
            raise ContractError("unknown/duplicate value heads")


class EntityValueNet(nn.Module):
    def __init__(self, schema: FeatureSchema, config: NetworkConfig = NetworkConfig(), *, _value_heads=True):
        super().__init__()
        self.schema, self.config = schema, config
        d = config.embedding
        self.global_proj = nn.Sequential(nn.Linear(schema.global_dim, d), nn.LayerNorm(d), nn.GELU())
        self.entity_proj = nn.Sequential(nn.Linear(schema.entity_dim, d), nn.LayerNorm(d), nn.GELU())
        self.types = nn.Embedding(len(schema.entity_types) + 1, d, padding_idx=0)
        self.zones = nn.Embedding(len(schema.zones) + 1, d, padding_idx=0)
        self.cards = nn.Embedding(len(schema.vocabulary) + 2, d, padding_idx=0)
        layer = nn.TransformerEncoderLayer(d, config.heads, config.feedforward, dropout=0.,
                                           activation="gelu", batch_first=True, norm_first=True)
        self.encoder = nn.TransformerEncoder(layer, config.layers, norm=nn.LayerNorm(d), enable_nested_tensor=False)
        # Encoder clones initially share initial values, not parameters. Reinitialize
        # matrices independently instead of relying on identical layer initializations.
        for parameter in self.encoder.parameters():
            if parameter.dim() > 1:
                nn.init.xavier_uniform_(parameter)
        self.value_heads = nn.ModuleDict({name: nn.Sequential(nn.Linear(d, 64), nn.GELU(), nn.Linear(64, 1))
                                         for name in config.outputs} if _value_heads else {})

    def encode(self, batch: EntityBatch) -> torch.Tensor:
        """Shared global/owner representations; no new parameters or key names."""
        if batch.schema_id != self.schema.identity:
            raise ContractError("network feature schema differs")
        b, n, dim = batch.entity_values.shape
        if (dim != self.schema.entity_dim or batch.global_values.shape != (b, self.schema.global_dim)
                or any(x.shape != (b, n) for x in (batch.type_ids, batch.zone_ids, batch.card_ids, batch.padding_mask))
                or batch.padding_mask.dtype != torch.bool):
            raise ContractError("entity batch shape/mask differs")
        g = self.global_proj(batch.global_values).unsqueeze(1)
        e = self.entity_proj(batch.entity_values) + self.types(batch.type_ids) + self.zones(batch.zone_ids) + self.cards(batch.card_ids)
        seq = torch.cat((g, e), dim=1)
        # The global token is always present, including zero-hand/zero-drink cases.
        mask = torch.cat((torch.zeros(b, 1, device=g.device, dtype=torch.bool), batch.padding_mask), dim=1)
        return self.encoder(seq, src_key_padding_mask=mask)

    def forward(self, batch: EntityBatch) -> dict[str, torch.Tensor]:
        encoded = self.encode(batch)[:, 0]
        result = {name: head(encoded).squeeze(-1) for name, head in self.value_heads.items()}
        if any(not torch.isfinite(value).all() for value in result.values()):
            raise ContractError("nonfinite value output")
        return result
