"""Small pure bindings for NIA Pro Plan1's observed native decision state."""
from __future__ import annotations

from pathlib import Path

from .leaderboard_replay import LeaderboardReplayAction
from .master_db import DEFAULT_DATABASE
from .nia_plan1_native_sidecar import Plan1NativeLegalCandidate
from .plan1_runtime_simulator_bridge import (_apply_plan1_runtime_drink, _drink_inventory,
                                           Plan1RuntimeStateProjection)


def require_plan1_policy_projection(projection):
    if projection.state is None:
        raise ValueError("native Plan1 projection unavailable")
    blockers = tuple(getattr(projection, "blockers", ()))
    if blockers:
        raise ValueError("native Plan1 projection has unresolved semantics: " + ";".join(
            f"{item.code}:{item.detail}" for item in blockers))
    return projection.state


def build_plan1_master_drink_candidate_provider(projection, *, database=DEFAULT_DATABASE):
    """Compile/evaluate every actual slot; never publish a supported subset.

    The legacy ReplayAction class is only a typed proposed UseDrink(slot)
    operation for the pure reducer. No replay record or after-state is invented
    or promoted. Selection-dependent/unsupported drinks remain explicit gaps.
    """
    if not isinstance(projection, Plan1RuntimeStateProjection) or projection.parsed is None:
        raise TypeError("drink candidates require the exact native Plan1 projection")
    bound_state = require_plan1_policy_projection(projection)
    if projection.parsed.root_runtime is None:
        raise ValueError("native Plan1 drink inventory is missing")
    raw = projection.parsed.root_runtime.opaque_fields.to_value()
    if not isinstance(raw.get("drinkList"), list):
        raise ValueError("native Plan1 drinkList must be explicitly observed")
    inventory = _drink_inventory(projection.parsed)
    database = Path(database)

    def provider(state, observed_inventory):
        if state != bound_state or tuple(observed_inventory) != inventory:
            raise ValueError("drink provider does not bind this native state/inventory")
        candidates = []
        for slot in inventory:
            blockers = []
            _index, drink, applied, after = _apply_plan1_runtime_drink(
                projection, LeaderboardReplayAction(slot.slot_index, "use-drink", (slot.slot_index,)),
                database=database, settings=None, blockers=blockers)
            if blockers or drink is None or applied is None or after is None:
                detail = ";".join(f"{value.code}:{value.detail}" for value in blockers)
                raise ValueError(f"native Plan1 drink slot {slot.slot_index} ({slot.drink_id}) unsupported: {detail}")
            if not applied.compiled_effects:
                raise ValueError(f"native Plan1 drink {slot.drink_id} has no resolved Master effects")
            candidates.append(Plan1NativeLegalCandidate.drink(slot))
        return tuple(candidates)
    return provider
