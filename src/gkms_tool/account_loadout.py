"""Stable-ID loadout constraints and proposals for the DLL selection boundary."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from itertools import combinations, product
import json
import math
from pathlib import Path
from typing import Callable, Iterable, Mapping

from .account_inventory import AccountInventorySnapshot, ProduceParameters
from .loadout_snapshot import _atomic_write_json
from .application_paths import state_root

DEFAULT_CONSTRAINTS_PATH = state_root() / "account_inventory/loadout_constraints.json"


@dataclass(frozen=True, slots=True)
class LoadoutConstraints:
    locked_support_ids: tuple[str, ...] = ()
    locked_memory_ids: tuple[str, ...] = ()
    excluded_support_ids: tuple[str, ...] = ()
    excluded_memory_ids: tuple[str, ...] = ()
    locked_rental_key: str | None = None

    def __post_init__(self) -> None:
        for name in ("locked_support_ids", "locked_memory_ids", "excluded_support_ids", "excluded_memory_ids"):
            values = tuple(getattr(self, name))
            if any(not isinstance(value, str) or not value for value in values) or len(set(values)) != len(values):
                raise ValueError(f"{name} must contain unique non-empty IDs")
            object.__setattr__(self, name, values)
        if len(self.locked_support_ids) > 5 or len(self.locked_memory_ids) > 4:
            raise ValueError("cannot lock more than five owned supports or four memories")
        if set(self.locked_support_ids) & set(self.excluded_support_ids) or set(self.locked_memory_ids) & set(self.excluded_memory_ids):
            raise ValueError("a card cannot be both locked and excluded")
        if self.locked_rental_key is not None and not self.locked_rental_key:
            raise ValueError("locked_rental_key cannot be empty")

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> LoadoutConstraints:
        for key in ("locked_support_ids", "locked_memory_ids", "excluded_support_ids", "excluded_memory_ids"):
            if not isinstance(data.get(key, ()), (list, tuple)):
                raise ValueError(f"{key} must be an array")
        return cls(**{key: tuple(data.get(key, ())) for key in (
            "locked_support_ids", "locked_memory_ids", "excluded_support_ids", "excluded_memory_ids")},
            locked_rental_key=data.get("locked_rental_key"))  # type: ignore[arg-type]


def save_loadout_constraints(path: Path, constraints: LoadoutConstraints, *, account_scope: str,
                             inventory_digest: str, produce_id: str, idol_card_id: str) -> None:
    for name, value in (("account_scope", account_scope), ("inventory_digest", inventory_digest),
                        ("produce_id", produce_id), ("idol_card_id", idol_card_id)):
        if not isinstance(value, str) or not value:
            raise ValueError(f"{name} must be non-empty text")
    _atomic_write_json(path, {"schema": "gkms.account-loadout-constraints.v1",
        "account_scope": account_scope, "inventory_digest": inventory_digest,
        "produce_id": produce_id, "idol_card_id": idol_card_id,
        "constraints": asdict(constraints)})


def load_loadout_constraints(path: Path, *, account_scope: str) -> LoadoutConstraints:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != "gkms.account-loadout-constraints.v1" or data.get("account_scope") != account_scope:
        raise ValueError("loadout constraints schema/account mismatch")
    return LoadoutConstraints.from_dict(data["constraints"])


@dataclass(frozen=True, slots=True)
class BorrowedSupportCard:
    rental_key: str
    card_id: str
    level: int
    plan_type: str
    expires_at: str

    def __post_init__(self) -> None:
        for key in ("rental_key", "card_id", "plan_type", "expires_at"):
            if not isinstance(getattr(self, key), str) or not getattr(self, key):
                raise ValueError(f"borrowed support {key} requires non-empty text")
        if type(self.level) is not int or self.level < 1:
            raise ValueError("borrowed support level must be a positive integer")
        if datetime.fromisoformat(self.expires_at.replace("Z", "+00:00")).tzinfo is None:
            raise ValueError("borrowed support expires_at requires timezone")


@dataclass(frozen=True, slots=True)
class LoadoutSelection:
    idol_card_id: str
    support_card_ids: tuple[str, ...]
    borrowed_support: BorrowedSupportCard
    memory_ids: tuple[str, ...]
    inventory_digest: str
    account_scope: str
    produce_id: str

    def __post_init__(self) -> None:
        for name in ("idol_card_id", "inventory_digest", "account_scope", "produce_id"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"{name} requires non-empty text")
        if not isinstance(self.borrowed_support, BorrowedSupportCard):
            raise ValueError("borrowed_support must be an observed borrowed card")
        for name in ("support_card_ids", "memory_ids"):
            values = getattr(self, name)
            if not isinstance(values, (list, tuple)) or any(not isinstance(key, str) or not key for key in values):
                raise ValueError(f"{name} must be an array of non-empty IDs")
            object.__setattr__(self, name, tuple(values))

    def to_command_payload(self) -> dict[str, object]:
        return {"account_scope": self.account_scope, "inventory_digest": self.inventory_digest,
                "produce_id": self.produce_id, "idol_card_id": self.idol_card_id,
                "support_card_ids": list(self.support_card_ids),
                "rental_key": self.borrowed_support.rental_key,
                "memory_ids": list(self.memory_ids)}


def validate_selection(snapshot: AccountInventorySnapshot, selection: LoadoutSelection, *,
                       now: datetime | None = None, constraints: LoadoutConstraints | None = None,
                       section: str | None = None) -> None:
    if section not in (None, "support"):
        raise ValueError("unsupported partial loadout section")
    if selection.account_scope != snapshot.account_scope or selection.inventory_digest != snapshot.content_digest:
        raise ValueError("selection account/inventory changed; recompute before applying")
    if selection.idol_card_id not in {row.card_id for row in snapshot.idol_cards}:
        raise ValueError("selected idol is not owned")
    owned = set(selection.support_card_ids)
    memories = set(selection.memory_ids)
    if len(selection.support_card_ids) != 5 or len(owned) != 5:
        raise ValueError("exactly five unique owned supports required")
    if section == "support" and memories:
        raise ValueError("support-only proposal must not choose memory instances")
    if section is None and (len(selection.memory_ids) != 4 or len(memories) != 4):
        raise ValueError("exactly four unique memory instances required")
    if not owned <= {row.card_id for row in snapshot.support_cards} or not memories <= {row.memory_id for row in snapshot.memories}:
        raise ValueError("selection contains unavailable owned instances")
    if selection.borrowed_support.card_id in owned:
        raise ValueError("borrowed support duplicates an owned support in the loadout")
    expires = datetime.fromisoformat(selection.borrowed_support.expires_at.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc) if now is None else now
    if expires.tzinfo is None or now.tzinfo is None or expires <= now:
        raise ValueError("borrowed support availability expired")
    locks = constraints or LoadoutConstraints()
    if not set(locks.locked_support_ids) <= owned or (section is None and not set(locks.locked_memory_ids) <= memories):
        raise ValueError("selection does not retain locked cards")
    if set(locks.excluded_support_ids) & owned or set(locks.excluded_memory_ids) & memories:
        raise ValueError("selection contains excluded cards")
    if locks.locked_rental_key is not None and selection.borrowed_support.rental_key != locks.locked_rental_key:
        raise ValueError("selection does not retain locked borrowed support")


def save_loadout_selection(path: Path, selection: LoadoutSelection) -> None:
    """Save a local proposal; this does not claim the game applied it."""
    _atomic_write_json(path, {"schema": "gkms.account-loadout-selection.v1",
                              "status": "pending-game-application", **asdict(selection)})


def rank_account_loadouts(snapshot: AccountInventorySnapshot, *, idol_card_id: str,
                          produce_id: str, borrowed: Iterable[BorrowedSupportCard],
                          score: Callable[[LoadoutSelection], float],
                          constraints: LoadoutConstraints = LoadoutConstraints(),
                          support_candidates: Iterable[str] | None = None,
                          memory_candidates: Iterable[str] | None = None,
                          now: datetime | None = None, maximum_combinations: int = 100000,
                          limit: int = 3) -> tuple[tuple[LoadoutSelection, float], ...]:
    """Rank entire legal 6+4 combinations with the caller's existing policy.

    Candidate shortlists are explicit and retain all locks. This module does
    not pretend support level or a BC probability is a predicted game score.
    A search exceeding the budget abstains before calling the scoring model.
    """
    if maximum_combinations < 1 or limit < 1:
        raise ValueError("search budget and result limit must be positive")
    available_support = {row.card_id for row in snapshot.support_cards}
    available_memory = {row.memory_id for row in snapshot.memories if row.candidate is not None}
    supports = set(available_support if support_candidates is None else support_candidates)
    memories = set(available_memory if memory_candidates is None else memory_candidates)
    supports |= set(constraints.locked_support_ids)
    memories |= set(constraints.locked_memory_ids)
    if not supports <= available_support or not memories <= available_memory:
        raise ValueError("candidate shortlist or locks contain unavailable instances")
    supports -= set(constraints.excluded_support_ids)
    memories -= set(constraints.excluded_memory_ids)
    locked_supports = tuple(constraints.locked_support_ids)
    locked_memories = tuple(constraints.locked_memory_ids)
    remaining_supports = sorted(supports - set(locked_supports))
    remaining_memories = sorted(memories - set(locked_memories))
    rentals = tuple(row for row in borrowed if constraints.locked_rental_key in (None, row.rental_key))
    support_n = 5 - len(locked_supports)
    memory_n = 4 - len(locked_memories)
    if len(remaining_supports) < support_n or len(remaining_memories) < memory_n or not rentals:
        return ()
    count = math.comb(len(remaining_supports), support_n) * math.comb(len(remaining_memories), memory_n) * len(rentals)
    if count > maximum_combinations:
        raise ValueError(f"loadout search requires policy shortlists: {count} > {maximum_combinations}")
    results: list[tuple[LoadoutSelection, float]] = []
    for support_set, memory_set, rental in product(combinations(remaining_supports, support_n),
                                                  combinations(remaining_memories, memory_n), rentals):
        selection = LoadoutSelection(idol_card_id, locked_supports + support_set, rental,
                                     locked_memories + memory_set, snapshot.content_digest,
                                     snapshot.account_scope, produce_id)
        try:
            validate_selection(snapshot, selection, now=now, constraints=constraints)
        except ValueError:
            continue
        value = score(selection)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("loadout score must be finite numeric evidence")
        results.append((selection, float(value)))
    return tuple(sorted(results, key=lambda pair: (-pair[1], pair[0].support_card_ids,
                       pair[0].memory_ids, pair[0].borrowed_support.rental_key))[:limit])


def read_available_loadout(client=None) -> dict[str, object]:
    """Read current scene resources through DLL under the shared input owner."""
    from .controller_client import _serialized_controller_request
    from .runtime_command_client import RuntimeCommandClient
    client = RuntimeCommandClient() if client is None else client
    with _serialized_controller_request(30):
        result = client.execute("read_loadout", timeout=15).require_ok()
    data = result.raw.get("loadout")
    if not isinstance(data, Mapping) or data.get("schema") != "gkms.account-loadout.v1":
        raise ValueError("DLL loadout snapshot unavailable or schema mismatch")
    result_data = dict(data)
    captured = datetime.fromisoformat(str(data["captured_at"]).replace("Z", "+00:00"))
    if captured.tzinfo is None:
        raise ValueError("DLL loadout timestamp requires timezone")
    # This is a host freshness budget for the observed game rental cache,
    # not a claim that the rental has been revalidated by a remote server.
    expires = (captured + timedelta(minutes=5)).isoformat()
    result_data["rental_support_cards"] = [
        {**dict(row), "expires_at": expires, "availability_source": "dll-current-rental-cache"}
        for row in data.get("rental_support_cards", ()) if isinstance(row, Mapping)]
    return result_data


def _pending_loadout_path(client) -> Path:
    return Path(client.root) / "pending_loadout.json"


def _release_completed_action(client, request) -> None:
    release = getattr(client, "release_action", None)
    if callable(release):
        release(request)


def poll_account_loadout_pending(client=None):
    """Poll the original request only; never create or resubmit an operation."""
    from .runtime_command_client import RuntimeCommandClient, RuntimeCommandRequest
    client = RuntimeCommandClient() if client is None else client
    path = _pending_loadout_path(client)
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    request = RuntimeCommandRequest(**{key: data[key] for key in (
        "request_id", "session_generation", "command", "expected_revision", "target")})
    result = client.poll_result(request)
    if result is not None and (result.status == "rejected" or
                              (result.status == "submitted" and result.raw.get("applied") is True)):
        _release_completed_action(client, request)
        path.unlink(missing_ok=True)
    return result


def reconcile_account_loadout_pending(client=None) -> dict[str, object]:
    """Explicit readback reconciliation, allowing a new user-selected operation.

    A final receipt or changed process generation is required. No operation
    is retried, and matching current state is not attributed to an unknown
    earlier command. Unresolved operations in the same generation stay held.
    """
    from .controller_client import _serialized_controller_request
    from .runtime_command_client import RuntimeCommandClient, RuntimeCommandPending, RuntimeCommandRequest
    client = RuntimeCommandClient() if client is None else client
    with _serialized_controller_request(30):
        path = _pending_loadout_path(client)
        if not path.is_file():
            return {"pending": False, "ready_for_new_selection": True,
                    "loadout": read_available_loadout(client)}
        data = json.loads(path.read_text(encoding="utf-8"))
        request = RuntimeCommandRequest(**{key: data[key] for key in (
            "request_id", "session_generation", "command", "expected_revision", "target")})
        receipt = client.poll_result(request)
        generation_changed = client.read_status()["session_generation"] != request.session_generation
        if receipt is None and not generation_changed:
            raise RuntimeCommandPending(request, "original loadout has no final receipt; continue polling its request ID")
        # This managed-thread read executes after the synchronous setter's
        # final receipt (or in the replacement process), so it is a new state
        # observation, not evidence inferred from the timed-out request.
        live = read_available_loadout(client)
        target = request.target or {}
        owned = [row["card_id"] for row in live.get("support_cards", ())
                 if isinstance(row, Mapping) and row.get("is_rental") is False]
        rental_keys = [row.get("rental_key") for row in live.get("support_cards", ())
                       if isinstance(row, Mapping) and row.get("is_rental") is True]
        memories = [row["memory_id"] for row in live.get("memories", ())
                    if isinstance(row, Mapping) and row.get("is_rental") is False]
        matches = (all(live.get(key) == target.get(key) for key in ("account_scope", "produce_id", "idol_card_id"))
                   and len(owned) == 5 and set(owned) == set(target.get("support_card_ids", ()))
                   and len(memories) == 4 and set(memories) == set(target.get("memory_ids", ()))
                   and rental_keys == [target.get("rental_key")])
        record = {"schema": "gkms.account-loadout-reconciliation.v1",
                  "request_id": request.request_id,
                  "original_status": None if receipt is None else receipt.status,
                  "process_generation_changed": generation_changed,
                  "current_selection_matches_target": matches,
                  "pending": False, "ready_for_new_selection": True,
                  "loadout": live}
        _atomic_write_json(Path(client.root) / "reconciled_loadouts" / f"{request.request_id}.json", record)
        _release_completed_action(client, request)
        path.unlink()
        return record


def apply_account_loadout(snapshot: AccountInventorySnapshot, selection: LoadoutSelection, client=None, *, section=None):
    """Compare and apply through the normal DLL setters with one input owner.

    Return RuntimeCommandResult. Only submitted + raw.applied is a confirmed
    scene selection. Pending exceptions retain their request ID for polling.
    """
    from .controller_client import _serialized_controller_request
    from .runtime_command_client import RuntimeCommandClient, RuntimeCommandPending, RuntimeCommandRequest
    client = RuntimeCommandClient() if client is None else client
    with _serialized_controller_request(30):
        pending_path = _pending_loadout_path(client)
        if pending_path.is_file():
            poll_account_loadout_pending(client)
            if pending_path.is_file():
                data = json.loads(pending_path.read_text(encoding="utf-8"))
                request = RuntimeCommandRequest(**{key: data[key] for key in (
                    "request_id", "session_generation", "command", "expected_revision", "target")})
                raise RuntimeCommandPending(request, "prior loadout outcome requires reconciliation")
        validate_selection(snapshot, selection, section=section)
        live = read_available_loadout(client)
        if section is not None and live.get("active_section") != section:
            raise ValueError("partial loadout target does not belong to the active page")
        if live.get("account_scope") != selection.account_scope or live.get("produce_id") != selection.produce_id:
            raise ValueError("current game account/produce differs from selected loadout")
        rentals = {row["rental_key"]: row for row in live["rental_support_cards"]}
        current_rental = rentals.get(selection.borrowed_support.rental_key)
        if current_rental is None or current_rental["card_id"] != selection.borrowed_support.card_id or current_rental["level"] != selection.borrowed_support.level:
            raise ValueError("borrowed support changed; refresh the recommendation")
        target = selection.to_command_payload()
        if section == "support":
            target.pop("memory_ids")
            target["phase"] = "support"
        target["inventory_revision"] = snapshot.revision
        try:
            result = client.execute("loadout.apply", target=target,
                expected_revision=live["revision"], timeout=15)
        except RuntimeCommandPending as error:
            _atomic_write_json(pending_path, error.request.to_dict())
            raise
        if result.status == "unknown" or (result.status == "submitted" and result.raw.get("applied") is not True):
            _atomic_write_json(pending_path, result.request.to_dict())
        elif result.status == "submitted" and result.raw.get("applied") is True:
            _release_completed_action(client, getattr(result, "request", None))
        return result


def apply_native_memory_locks(snapshot: AccountInventorySnapshot, expected_loadout, overrides, client=None):
    """Apply only explicit locks to a completed native auto deck, retaining other slots."""
    from .controller_client import _serialized_controller_request
    from .runtime_command_client import RuntimeCommandClient, RuntimeCommandPending, RuntimeCommandRequest
    from .runtime_memory_autoselect import native_memory_ids
    client = RuntimeCommandClient() if client is None else client
    with _serialized_controller_request(30):
        pending_path = _pending_loadout_path(client)
        if pending_path.is_file():
            poll_account_loadout_pending(client)
            if pending_path.is_file():
                data = json.loads(pending_path.read_text(encoding="utf-8"))
                request = RuntimeCommandRequest(**{key: data[key] for key in (
                    "request_id", "session_generation", "command", "expected_revision", "target")})
                raise RuntimeCommandPending(request, "prior loadout outcome requires reconciliation")
        live = read_available_loadout(client)
        if (live.get("active_section") != "memory" or live.get("revision") != expected_loadout.get("revision")
                or live.get("account_scope") != snapshot.account_scope):
            raise ValueError("native memory auto deck changed before applying explicit locks")
        automatic = live.get("memory_auto") or {}
        if (automatic.get("schema") != "gkms.memory-auto-selection.v1" or automatic.get("phase") != "succeeded"
                or automatic.get("confirmed") is not True or automatic.get("owner_bound") is not True
                or type(automatic.get("operation_serial")) is not int
                or automatic["operation_serial"] <= 0
                or any(automatic.get(k) != live.get(k) for k in ("account_scope", "produce_id", "idol_card_id"))):
            raise ValueError("memory locks require this exact completed game auto-selection")
        current = native_memory_ids(live)
        owned = {row.memory_id for row in snapshot.memories}
        seen = set()
        for row in overrides:
            position = row.get("position")
            if (type(position) is not int or position not in range(4) or position in seen
                    or current[position] != row.get("before_memory_id") or row.get("locked_memory_id") not in owned):
                raise ValueError("explicit memory lock slot or owned identity differs")
            seen.add(position)
        if not seen:
            raise ValueError("empty lock overlay is not a game operation")
        target = {key: live[key] for key in ("account_scope", "produce_id", "idol_card_id")}
        target.update(phase="memory", inventory_revision=snapshot.revision,
            memory_auto_operation_serial=automatic["operation_serial"], memory_slot_overrides=list(overrides))
        try:
            result = client.execute("loadout.apply", target=target, expected_revision=live["revision"], timeout=15)
        except RuntimeCommandPending as error:
            _atomic_write_json(pending_path, error.request.to_dict())
            raise
        if result.status == "unknown" or (result.status == "submitted" and result.raw.get("applied") is not True):
            _atomic_write_json(pending_path, result.request.to_dict())
        elif result.status == "submitted" and result.raw.get("applied") is True:
            _release_completed_action(client, getattr(result, "request", None))
        return result


@dataclass(frozen=True, slots=True)
class InitialLoadoutRecommendation:
    selection: LoadoutSelection
    ranking_score: float
    reasons: tuple[str, ...]
    method: str = "initial-parameters-sp-and-memory-replay-prior"


def recommend_initial_loadouts(snapshot: AccountInventorySnapshot, loadout: Mapping[str, object], *,
                               constraints: LoadoutConstraints = LoadoutConstraints(),
                               idol_card_id: str | None = None, produce_id: str | None = None,
                               catalog=None, prior=None, database: Path | None = None,
                               limit: int = 3, _include_memory: bool = True) -> tuple[InitialLoadoutRecommendation, ...]:
    """Usable bounded baseline, explicitly not a whole-run score predictor.

    Ranks known initial stats/growth/stamina/P and deterministic SP bonuses in
    the selected idol's growth directions. Existing four-memory joint replay
    evidence adds a within-pool relative-rank preference. Shortlists (10 own
    supports, 12 memories, 3 rentals, 24 combinations per component) bound the
    search and always retain user locks. Future event rewards are not invented.
    """
    from .leaderboard_memory_loadout_prior import DEFAULT_PRIOR_ARTIFACT, load_memory_loadout_prior_artifact
    from .master_db import DEFAULT_DATABASE, get_idol_profile
    from .passive_catalog import MasterPassiveCatalog
    database = DEFAULT_DATABASE if database is None else database
    catalog = MasterPassiveCatalog.load() if catalog is None else catalog
    idol_card_id = str(loadout.get("idol_card_id", "")) if idol_card_id is None else idol_card_id
    produce_id = str(loadout.get("produce_id", "")) if produce_id is None else produce_id
    if loadout.get("account_scope") != snapshot.account_scope:
        raise ValueError("loadout and inventory accounts differ")
    profile = get_idol_profile(idol_card_id, database)
    owned_idol = next((row for row in snapshot.idol_cards if row.card_id == idol_card_id), None)
    if profile is None or owned_idol is None:
        raise ValueError("selected owned idol is missing from the current Master")
    plan_type = profile.plan_type
    scope = dict(produce_id=produce_id, plan_type=plan_type,
                 exam_effect_type=profile.exam_effect_type, idol_card_id=idol_card_id)
    from .portable_outer_assets import asset_directory
    if not _include_memory:
        prior = None
    elif prior is None and asset_directory() is not None:
        # Public recommendation requires its verified aggregate prior. Missing
        # or changed assets must not silently drop the existing preference.
        prior = load_memory_loadout_prior_artifact(DEFAULT_PRIOR_ARTIFACT, **scope)
    elif prior is None and DEFAULT_PRIOR_ARTIFACT.is_file():
        try:
            prior = load_memory_loadout_prior_artifact(DEFAULT_PRIOR_ARTIFACT, **scope)
        except (KeyError, OSError, TypeError, ValueError):
            prior = None
    if prior is not None and not prior.applies_to(**scope):
        raise ValueError("memory replay prior does not match selected cultivation scope")
    growth = tuple(getattr(owned_idol.produce_parameters or profile, key)
                   for key in ("vocal_growth", "dance_growth", "visual_growth"))
    total_growth = sum(growth)
    weights = tuple(value / total_growth for value in growth) if total_growth else (1 / 3,) * 3
    keys = ("vocal", "dance", "visual", "vocal_growth", "dance_growth", "visual_growth", "stamina", "produce_points", "sp")
    effect_keys = {
        "ProduceEffectType_VocalAddition": "vocal", "ProduceEffectType_DanceAddition": "dance",
        "ProduceEffectType_VisualAddition": "visual", "ProduceEffectType_MaxStaminaAddition": "stamina",
        "ProduceEffectType_VocalGrowthRateAddition": "vocal_growth",
        "ProduceEffectType_DanceGrowthRateAddition": "dance_growth",
        "ProduceEffectType_VisualGrowthRateAddition": "visual_growth",
        "ProduceEffectType_ProducePointAdditionDisableTrigger": "produce_points",
        "ProduceEffectType_LessonSpChangeRatePermilAddition": "sp",
    }
    directional_sp = {f"ProduceEffectType_Lesson{axis}SpChangeRatePermilAddition": weight
                      for axis, weight in zip(("Vocal", "Dance", "Visual"), weights)}

    def features(sources, observed=None):
        values = dict.fromkeys(keys, 0.0)
        if observed is not None:
            values.update({key: float(value) for key, value in asdict(observed).items()})
        skipped = 0
        for source in sources:
            if not source.fully_supported:
                skipped += 1
                continue
            for rule in source.rules:
                if not rule.deterministic:
                    skipped += 1
                    continue
                effect = rule.effect_type
                key = effect_keys.get(effect)
                if effect in directional_sp:
                    values["sp"] += rule.effect_value_min * directional_sp[effect]
                elif key is not None:
                    # Actual seven IUserCard values already include their
                    # initial rules; do not count those rules a second time.
                    if observed is None or key not in asdict(observed):
                        values[key] += float(rule.effect_value_min)
        return values, skipped

    def merge(*vectors):
        return {key: sum(value[key] for value in vectors) for key in keys}

    def value(vector):
        return (sum(weights[i] * vector[key] for i, key in enumerate(keys[:3])) +
                .4 * sum(weights[i] * vector[key] for i, key in enumerate(keys[3:6])) +
                2 * vector["stamina"] + .1 * vector["produce_points"] +
                .12 * min(1000, max(0, vector["sp"])))

    def resolve_support(card_id, level, observed=None):
        try:
            return features(catalog.resolve_support_card(card_id, level), observed)
        except (KeyError, ValueError):
            return features((), observed)[0], 1

    supports = {row.card_id: row for row in snapshot.support_cards
                if row.plan_type in (plan_type, "ProducePlanType_Common") and row.card_id not in constraints.excluded_support_ids}
    memories = {row.memory_id: row for row in snapshot.memories
                if row.candidate is not None and row.plan_type in (plan_type, "ProducePlanType_Common") and row.memory_id not in constraints.excluded_memory_ids}
    if not _include_memory:
        memories = {}
    if not set(constraints.locked_support_ids) <= supports.keys() or (_include_memory and not set(constraints.locked_memory_ids) <= memories.keys()):
        raise ValueError("a locked card is unavailable or incompatible with the selected plan")
    support_features = {key: resolve_support(key, row.level, row.produce_parameters) for key, row in supports.items()}
    memory_sources = {}
    memory_features = {}
    memory_missing = {}
    for key, row in memories.items():
        sources = []
        missing = 0
        for ability, level in row.abilities:
            try:
                sources.append(catalog.resolve_memory_ability(ability, level))
            except (KeyError, ValueError):
                missing += 1
        memory_sources[key] = tuple(sources)
        memory_missing[key] = missing
        vector, skipped = features(sources)
        memory_features[key] = (vector, skipped + missing)
    single_prior = {key: 0 if prior is None else prior.score_for(row.candidate) for key, row in memories.items()}
    prior_values = sorted(set(single_prior.values()))
    prior_ranks = {score: rank / max(1, len(prior_values) - 1) for rank, score in enumerate(prior_values)}
    ranked_memory_prior = {key: prior_ranks[score] for key, score in single_prior.items()}

    def shortlist(available, locked, scores, size):
        fixed = tuple(locked)
        return fixed + tuple(sorted(set(available) - set(fixed), key=lambda key: (-scores[key], key))[:max(0, size - len(fixed))])
    support_ids = shortlist(supports, constraints.locked_support_ids,
        {key: value(vector) for key, (vector, _) in support_features.items()}, 10)
    memory_ids = shortlist(memories, constraints.locked_memory_ids if _include_memory else (),
        {key: value(vector) + (30 * ranked_memory_prior[key] if prior is not None else 0)
         for key, (vector, _) in memory_features.items()}, 12)
    if len(support_ids) < 5 or (_include_memory and len(memory_ids) < 4):
        return ()
    captured = datetime.fromisoformat(str(loadout["captured_at"]).replace("Z", "+00:00"))
    rental_rows = []
    for raw in loadout.get("rental_support_cards", ()):
        if raw["plan_type"] not in (plan_type, "ProducePlanType_Common"):
            continue
        if constraints.locked_rental_key not in (None, raw["rental_key"]):
            continue
        rental = BorrowedSupportCard(raw["rental_key"], raw["card_id"], raw["level"], raw["plan_type"],
            raw.get("expires_at", (captured + timedelta(minutes=5)).isoformat()))
        rental_features = resolve_support(rental.card_id, rental.level, ProduceParameters.from_dict(raw.get("produce_parameters")))
        rental_rows.append((rental, rental_features))
    rental_rows.sort(key=lambda row: (-value(row[1][0]), row[0].rental_key))
    rental_rows = rental_rows[:3]
    fixed_memories = set(constraints.locked_memory_ids)
    memory_combos = []
    for selected in combinations(memory_ids, 4):
        if not fixed_memories <= set(selected):
            continue
        # Master marks unique-activation abilities. Only the highest-level
        # selected instance of the same ability contributes to this baseline.
        sources, unique = [], {}
        for key in selected:
            for source in memory_sources[key]:
                if source.raw.get("source", {}).get("isUniqueActivation") is True:
                    if source.source_id not in unique or unique[source.source_id].source_level < source.source_level:
                        unique[source.source_id] = source
                else:
                    sources.append(source)
        vector, skipped = features((*sources, *unique.values()))
        joint_prior = None if prior is None else prior.combination_score_for(tuple(memories[key].candidate for key in selected))
        prior_score = 0.0 if joint_prior is None or joint_prior.abstained else float(joint_prior.total_score)
        missing = sum(memory_missing[key] for key in selected)
        memory_combos.append((selected, vector, skipped + missing, prior_score))
    if not _include_memory:
        memory_combos = [((), dict.fromkeys(keys, 0.0), 0, 0.0)]
    ordered_prior = sorted({row[3] for row in memory_combos})
    normalized_prior = {score: index / max(1, len(ordered_prior) - 1) for index, score in enumerate(ordered_prior)}
    memory_combos.sort(key=lambda row: (-(value(row[1]) + 50 * normalized_prior[row[3]]), row[0]))
    memory_combos = memory_combos[:24]
    fixed_supports = set(constraints.locked_support_ids)
    digest = snapshot.content_digest
    recommendations = []
    for rental, (rental_vector, rental_skipped) in rental_rows:
        support_combos = []
        for selected in combinations(support_ids, 5):
            if not fixed_supports <= set(selected) or rental.card_id in selected:
                continue
            vector = merge(*(support_features[key][0] for key in selected), rental_vector)
            skipped = sum(support_features[key][1] for key in selected) + rental_skipped
            support_combos.append((selected, vector, skipped))
        support_combos.sort(key=lambda row: (-value(row[1]), row[0]))
        for selected_supports, support_vector, support_skipped in support_combos[:24]:
            for selected_memories, memory_vector, memory_skipped, prior_score in memory_combos:
                selection = LoadoutSelection(idol_card_id, selected_supports, rental, selected_memories,
                                             digest, snapshot.account_scope, produce_id)
                try:
                    validate_selection(snapshot, selection, constraints=constraints,
                        section=None if _include_memory else "support")
                except ValueError:
                    continue
                vector = merge(support_vector, memory_vector)
                score = value(vector) + 50 * normalized_prior[prior_score]
                reasons = (
                    "快速初始加成與已知 SP 加成排序；不是整場預估分數。",
                    "培育方向 Vo / Da / Vi：" + " / ".join(f"{weight:.0%}" for weight in weights),
                    f"已知初始屬性 Vo {vector['vocal']:g} / Da {vector['dance']:g} / Vi {vector['visual']:g}；體力 {vector['stamina']:g}；P 點 {vector['produce_points']:g}。",
                    f"已知 SP 加成（依培育方向加權）：{vector['sp'] / 10:g}%。",
                    "四回憶組合使用同範圍 Replay 聯合先驗。" if prior is not None else "目前沒有同範圍四回憶 Replay 先驗；只使用已知初始加成。",
                    f"{support_skipped + memory_skipped} 個未知、非確定或未支援的被動來源未估收益；後續事件與整場模擬仍待進階評估。",
                    "搜尋保留鎖定卡；候選上限為 10 支援、12 回憶、3 借卡，並非全牌庫窮舉最優解。",
                )
                if not _include_memory:
                    reasons = (*reasons[:4], "僅推薦支援卡；回憶由遊戲內自動編成處理，不使用回憶先驗。",
                        f"{support_skipped} 個未知或非確定支援被動未估收益。", "保留指定支援與借卡鎖定。")
                recommendations.append(InitialLoadoutRecommendation(selection, round(score, 6), reasons,
                    "initial-parameters-sp-and-memory-replay-prior" if _include_memory else "initial-support-parameters-sp-only"))
    recommendations.sort(key=lambda row: (-row.ranking_score, row.selection.support_card_ids,
                                         row.selection.memory_ids, row.selection.borrowed_support.rental_key))
    return tuple(recommendations[:limit])


def recommend_initial_supports(snapshot, loadout, **kwargs):
    """Reuse the original support/SP scoring without choosing any memories."""
    if "_include_memory" in kwargs or "prior" in kwargs:
        raise ValueError("support recommendation cannot accept a memory policy")
    return recommend_initial_loadouts(snapshot, loadout, _include_memory=False, **kwargs)
