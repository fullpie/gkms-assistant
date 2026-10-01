"""Complete DLL account inventories and versioned Master passive resolution.

The native bridge reads UserDataManager collections, never visible card tiles.
This adapter preserves real memory IDs and personal upgrades, rejects partial
reads, and reuses MasterPassiveCatalog rather than maintaining another catalog.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping

from .leaderboard_memory_loadout_prior import MemoryCardCandidate
from .passive_catalog import MasterPassiveCatalog, PassiveSource

SCHEMA = "gkms.account-inventory.v1"
SOURCE = "dll-user-data-manager"
PARAMETER_FIELDS = ("vocal", "dance", "visual", "vocal_growth", "dance_growth", "visual_growth", "stamina")


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    return value


def _integer(value: Any, name: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _object(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _rows(value: Any, name: str) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be an array")
    return tuple(_object(row, name) for row in value)


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ProduceParameters:
    """Observed IUserCard StatusUp/growth getters, not a second additive bonus."""
    vocal: int
    dance: int
    visual: int
    vocal_growth: int
    dance_growth: int
    visual_growth: int
    stamina: int

    @classmethod
    def from_dict(cls, value: Any) -> ProduceParameters | None:
        if value is None:
            return None
        data = _object(value, "produce_parameters")
        return cls(**{name: _integer(data.get(name), name) for name in PARAMETER_FIELDS})


@dataclass(frozen=True, slots=True)
class AccountIdolCard:
    card_id: str
    level_limit_rank: int
    potential_rank: int
    prima_stella_upgraded_time: int
    produce_parameters: ProduceParameters | None = None

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> AccountIdolCard:
        return cls(_text(row.get("card_id"), "idol.card_id"),
                   _integer(row.get("level_limit_rank"), "idol.level_limit_rank"),
                   _integer(row.get("potential_rank"), "idol.potential_rank"),
                   _integer(row.get("prima_stella_upgraded_time"), "idol.prima_stella_upgraded_time"),
                   ProduceParameters.from_dict(row.get("produce_parameters")))


@dataclass(frozen=True, slots=True)
class AccountSupportCard:
    card_id: str
    level: int
    level_limit_rank: int
    stock_quantity: int
    plan_type: str
    produce_parameters: ProduceParameters | None = None

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> AccountSupportCard:
        return cls(_text(row.get("card_id"), "support.card_id"),
                   _integer(row.get("level"), "support.level", 1),
                   _integer(row.get("level_limit_rank"), "support.level_limit_rank"),
                   _integer(row.get("stock_quantity"), "support.stock_quantity"),
                   _text(row.get("plan_type"), "support.plan_type"),
                   ProduceParameters.from_dict(row.get("produce_parameters")))


@dataclass(frozen=True, slots=True)
class AccountMemory:
    memory_id: str
    idol_card_id: str
    plan_type: str
    candidate: MemoryCardCandidate | None
    # Keep abilities even when a genuine memory has no inherited ProduceCard.
    abilities: tuple[tuple[str, int], ...]
    phase_type: str

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> AccountMemory:
        identity = _text(row.get("memory_id"), "memory.memory_id")
        phase = _text(row.get("produce_card_phase_type"), "memory.phase_type")
        abilities = tuple((_text(a.get("id"), "ability.id"), _integer(a.get("level"), "ability.level"))
                          for a in _rows(row.get("abilities"), "memory.abilities"))
        candidate = None
        if row.get("produce_card") is not None:
            card = _object(row["produce_card"], "memory.produce_card")
            # No defaults for missing inherited-card upgrade/customization evidence.
            _integer(card.get("upgradeCount"), "memory.upgradeCount")
            _rows(card.get("customizes"), "memory.customizes")
            candidate = MemoryCardCandidate.from_mapping({
                "memory_id": identity, "produce_card": dict(card),
                "produce_card_phase_type": phase,
                "abilities": [{"id": key, "level": level} for key, level in abilities],
            })
        return cls(identity, _text(row.get("idol_card_id"), "memory.idol_card_id"),
                   _text(row.get("plan_type"), "memory.plan_type"), candidate, abilities, phase)

    def to_dict(self) -> dict[str, Any]:
        return {"memory_id": self.memory_id, "idol_card_id": self.idol_card_id,
                "plan_type": self.plan_type, "produce_card_phase_type": self.phase_type,
                "produce_card": None if self.candidate is None else self.candidate.to_dict()["produce_card"],
                "abilities": [{"id": key, "level": level} for key, level in self.abilities]}


@dataclass(frozen=True, slots=True)
class AccountInventorySnapshot:
    account_scope: str
    revision: str
    captured_at: str
    game_version: str
    master_version: str | None
    idol_cards: tuple[AccountIdolCard, ...]
    support_cards: tuple[AccountSupportCard, ...]
    memories: tuple[AccountMemory, ...]

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> AccountInventorySnapshot:
        data = _object(payload, "account inventory")
        if data.get("schema") != SCHEMA or data.get("source") != SOURCE:
            raise ValueError("account inventory schema/source mismatch")
        if data.get("complete") is not True:
            raise ValueError("account inventory incomplete; previous snapshot must be retained")
        captured = _text(data.get("captured_at"), "captured_at")
        timestamp = datetime.fromisoformat(captured.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            raise ValueError("captured_at requires timezone")
        counts = _object(data.get("collection_counts"), "collection_counts")
        parsed: dict[str, tuple[Any, ...]] = {}
        for name, factory, key in (("idol_cards", AccountIdolCard.from_dict, "card_id"),
                                   ("support_cards", AccountSupportCard.from_dict, "card_id"),
                                   ("memories", AccountMemory.from_dict, "memory_id")):
            rows = _rows(data.get(name), name)
            if _integer(counts.get(name), f"collection_counts.{name}") != len(rows):
                raise ValueError(f"{name} count mismatch")
            parsed[name] = tuple(factory(row) for row in rows)
            if len({getattr(row, key) for row in parsed[name]}) != len(rows):
                raise ValueError(f"{name} duplicate identity")
        master = data.get("master_version")
        if master is not None:
            _text(master, "master_version")
        return cls(_text(data.get("account_scope"), "account_scope"),
                   _text(data.get("revision"), "revision"), captured,
                   _text(data.get("game_version"), "game_version"), master, **parsed)

    def to_dict(self) -> dict[str, Any]:
        return {"schema": SCHEMA, "source": SOURCE, "complete": True,
                "account_scope": self.account_scope, "revision": self.revision,
                "captured_at": self.captured_at, "game_version": self.game_version,
                "master_version": self.master_version,
                "collection_counts": {name: len(getattr(self, name)) for name in ("idol_cards", "support_cards", "memories")},
                "idol_cards": [asdict(row) for row in self.idol_cards],
                "support_cards": [asdict(row) for row in self.support_cards],
                "memories": [row.to_dict() for row in self.memories]}

    @property
    def content_digest(self) -> str:
        data = self.to_dict()
        for name in ("revision", "captured_at"):
            data.pop(name)
        # A different iteration order does not mean the account changed.
        for name, key in (("idol_cards", "card_id"), ("support_cards", "card_id"), ("memories", "memory_id")):
            data[name] = sorted(data[name], key=lambda row: row[key])
        return _digest(data)


def inventory_changes(previous: AccountInventorySnapshot | None, current: AccountInventorySnapshot) -> dict[str, dict[str, list[str]]]:
    if previous is not None and previous.account_scope != current.account_scope:
        raise ValueError("account scope mismatch; do not merge different accounts")
    result = {}
    for name, key in (("idol_cards", "card_id"), ("support_cards", "card_id"), ("memories", "memory_id")):
        before = {} if previous is None else {row[key]: row for row in previous.to_dict()[name]}
        after = {row[key]: row for row in current.to_dict()[name]}
        result[name] = {"added": sorted(after.keys() - before.keys()),
                        "removed": sorted(before.keys() - after.keys()),
                        "changed": sorted(k for k in before.keys() & after.keys() if before[k] != after[k])}
    return result


def load_account_inventory(path: Path) -> AccountInventorySnapshot | None:
    return None if not path.is_file() else AccountInventorySnapshot.from_dict(json.loads(path.read_text(encoding="utf-8")))


def save_account_inventory(path: Path, snapshot: AccountInventorySnapshot) -> dict[str, dict[str, list[str]]]:
    # Round-trip validation also protects callers constructing dataclasses directly.
    snapshot = AccountInventorySnapshot.from_dict(snapshot.to_dict())
    changes = inventory_changes(load_account_inventory(path), snapshot)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(snapshot.to_dict(), stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return changes


@dataclass(frozen=True, slots=True)
class ResolvedAccountResource:
    """Passive scoring coverage; blockers never mean the inventory is unreadable."""
    identity: str
    cache_key: str
    passives: tuple[PassiveSource, ...]
    observed_parameters: ProduceParameters | None
    blockers: tuple[str, ...]

    @property
    def effect_details(self) -> tuple[dict[str, Any], ...]:
        """Original structured semantics for effects not yet valued by ranking."""
        return tuple({"skill_id": source.skill_id, "skill_level": source.skill_level,
                      "effect_type": rule.effect_type, "trigger_id": rule.trigger_id,
                      "activation_rate_permil": rule.activation_rate_permil,
                      "value_min": rule.effect_value_min, "value_max": rule.effect_value_max,
                      "included_in_initial_catalog": source.fully_supported and rule.deterministic,
                      "recommendation_limitations": list(source.all_unsupported_rules)}
                     for source in self.passives for rule in source.rules)


class AccountEffectiveValuesResolver:
    """Changed instances invalidate themselves; ordinary refresh needs no retrain."""
    def __init__(self, catalog: MasterPassiveCatalog, *, master_digest: str) -> None:
        self.catalog = catalog
        self.master_digest = _text(master_digest, "master_digest")
        self._cache: dict[str, ResolvedAccountResource] = {}

    def resolve(self, snapshot: AccountInventorySnapshot) -> dict[str, ResolvedAccountResource]:
        results = {}
        for kind, rows in (("idol", snapshot.idol_cards), ("support", snapshot.support_cards), ("memory", snapshot.memories)):
            for row in rows:
                identity = row.memory_id if isinstance(row, AccountMemory) else row.card_id
                data = row.to_dict() if isinstance(row, AccountMemory) else asdict(row)
                cache_key = _digest((snapshot.account_scope, snapshot.game_version,
                                     snapshot.master_version, self.master_digest, kind, data))
                if cache_key not in self._cache:
                    sources: tuple[PassiveSource, ...] = ()
                    blockers = []
                    try:
                        if isinstance(row, AccountSupportCard):
                            sources = self.catalog.resolve_support_card(row.card_id, row.level)
                        elif isinstance(row, AccountMemory):
                            sources = tuple(self.catalog.resolve_memory_ability(key, level) for key, level in row.abilities)
                        elif row.produce_parameters is None:
                            blockers.append("idol-effective-parameters-not-observed")
                    except (KeyError, ValueError) as error:
                        blockers.append(f"master-resolution:{error}")
                    blockers.extend(reason for source in sources for reason in source.all_unsupported_rules)
                    self._cache[cache_key] = ResolvedAccountResource(identity, cache_key, sources,
                        getattr(row, "produce_parameters", None), tuple(dict.fromkeys(blockers)))
                results[f"{kind}:{identity}"] = self._cache[cache_key]
        return results
