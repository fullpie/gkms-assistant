"""Anchored, explicitly approximate outer projection for loadout comparisons.

Original replay sources are immutable. Only an isolated native experiment may
consume the returned player overlay. It is NOT a new qualified training replay.
The exam is native; the future cultivation route is held fixed and documented.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
import re

from .account_inventory import AccountInventorySnapshot, ProduceParameters
from .account_loadout import BorrowedSupportCard, LoadoutSelection
from .audition_rules import _load_rows, load_audition_rules
from .audition_turn_schedule import calculate_audition_base_multiplier_permils
from .exam_context import AuditionProgressBonusValues, calculate_audition_bonus_permils
from .passive_catalog import MasterPassiveCatalog, SUPPORT_LEVEL_TABLES
from .passive_runtime import resolve_passive_runtime
from .qualification_verification import source_stamp
from .rl_loadout_advisor import LoadoutRecommendationUnavailable, resolve_loadout_effects

AXES = ("vocal", "dance", "visual")
STAGES = ("Mid1", "Mid2", "Final")
PHASE_START = "ProduceMemoryProduceCardPhaseType_ProduceStart"
PHASE_MID = "ProduceMemoryProduceCardPhaseType_EndAuditionMid"


def file_reference(path):
    path = Path(path).resolve(); data = path.read_bytes()
    return {"path": str(path), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def read_reference(reference):
    path = Path(reference["path"]); data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != reference["sha256"]:
        raise LoadoutRecommendationUnavailable("loadout reference bytes changed: " + path.name)
    return json.loads(data)


def _rank(value):
    if type(value) is int:
        return value
    match = re.search(r"_(\d+)$", str(value))
    if match is None:
        raise ValueError("unresolved original account rank")
    return int(match[1])


class LoadoutReferenceMaster:
    """One bounded Master instance, shared with the existing passive catalog."""
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        # Calibration reads only the audition curve and Setting. Defer the
        # complete support/memory catalog until a loadout actually needs it.
        paths = (*SUPPORT_LEVEL_TABLES.values(), 'MemoryGift.yaml', 'MemoryAbility.yaml',
            'SupportCard.yaml', 'ProduceSkill.yaml', 'ProduceEffect.yaml', 'ProduceTrigger.yaml')
        self._catalog_stamps = {self.directory/name: source_stamp(self.directory/name) for name in paths}
        self._catalog = None
        self._tables = {}
        self._table_stamps = {}

    @property
    def catalog(self):
        def validate():
            if any(source_stamp(path) != stamp for path, stamp in self._catalog_stamps.items()):
                raise LoadoutRecommendationUnavailable('Master passive source changed after binding')
        validate()
        if self._catalog is None:
            catalog = MasterPassiveCatalog.load(self.directory)
            validate()
            self._catalog = catalog
        return self._catalog

    def rows(self, name):
        path = self.directory / (name + '.yaml')
        stamp = source_stamp(path)
        if name in self._table_stamps and self._table_stamps[name] != stamp:
            raise LoadoutRecommendationUnavailable('Master calibration source changed after reading: ' + name)
        if name not in self._tables:
            rows = _load_rows(path)
            if source_stamp(path) != stamp:
                raise LoadoutRecommendationUnavailable('Master calibration source changed while reading: ' + name)
            self._tables[name] = rows
            self._table_stamps[name] = stamp
        return self._tables[name]

    def one(self, table, key):
        matches = [row for row in self.rows(table) if row.get("id") == key]
        if len(matches) != 1:
            raise LoadoutRecommendationUnavailable("Master definition is not unique: " + table + ":" + key)
        return matches[0]

    def idol_parameters(self, idol, level_rank, potential_rank):
        row = self.one("IdolCard", idol)
        values = {axis: int(row["produce" + axis.title()]) for axis in AXES}
        values.update({axis + "_growth": int(row["produce" + axis.title() + "GrowthRatePermil"]) for axis in AXES})
        values["stamina"] = int(row["produceStamina"])
        for table, key, rank in (("IdolCardLevelLimitStatusUp", row["idolCardLevelLimitStatusUpId"], level_rank),
                                ("IdolCardPotential", row["idolCardPotentialId"], potential_rank)):
            for effect in self.rows(table):
                if effect["id"] != key or _rank(effect["rank"]) > rank:
                    continue
                for axis in AXES:
                    values[axis] += int(effect.get("produce" + axis.title(), 0))
                    values[axis + "_growth"] += int(effect.get("produce" + axis.title() + "GrowthRatePermil", 0))
                if any(value.endswith("_ProduceStamina") for value in effect.get("effectTypes", ())):
                    values["stamina"] += int(effect["effectValue"])
        return ProduceParameters(**values)

    def support_events(self, key, level):
        details = {row["id"]: row for row in self.rows("ProduceStepEventDetail")}
        effects = {row["id"]: row for row in self.rows("ProduceEffect")}
        result = []
        for event in self.rows("ProduceEventSupportCard"):
            if event["supportCardId"] != key or int(event["supportCardLevel"]) > level:
                continue
            detail = details[event["produceStepEventDetailId"]]
            result.append({"number": int(event["number"]), "detail_id": detail["id"],
                "effects": [effects[value] for value in detail["produceEffectIds"]],
                "choice_required": bool(detail.get("produceStepEventSuggestionIds"))})
        return tuple(result)


def baseline_inventory(history, master, *, scope):
    """Reconstruct only static getters whose components are explicit in RAW."""
    idol = history["idolCardId"]
    # RAW is protobuf JSON: absent zero enum values retain protocol defaults.
    level_rank, potential_rank = _rank(history.get("levelLimitRank", 0)), _rank(history.get("potentialRank", 0))
    parameters = master.idol_parameters(idol, level_rank, potential_rank)
    supports = history["deckSupportCards"]
    if len(supports) != 6:
        raise LoadoutRecommendationUnavailable("reference requires six observed support slots")
    memories = []
    for index, wrapper in enumerate(history["deckMemories"]):
        row = wrapper.get("memory", wrapper)
        card = deepcopy(row.get("produceCard"))
        if card is not None:
            card.setdefault("upgradeCount", 0); card.setdefault("customizes", [])
        memories.append({"memory_id": "reference-memory-" + str(index), "idol_card_id": row["idolCardId"],
            "plan_type": row["planType"], "produce_card": card,
            "produce_card_phase_type": row["produceCardPhaseType"], "abilities": row.get("abilities", [])})
    if len(memories) != 4:
        raise LoadoutRecommendationUnavailable("reference requires four observed memory slots")
    snapshot = AccountInventorySnapshot.from_dict({"schema": "gkms.account-inventory.v1",
        "source": "dll-user-data-manager", "complete": True, "account_scope": "reference:" + scope,
        "revision": "offline-static-reference", "captured_at": datetime.now(timezone.utc).isoformat(),
        "game_version": "original-reference", "master_version": None,
        "collection_counts": {"idol_cards": 1, "support_cards": 5, "memories": 4},
        "idol_cards": [{"card_id": idol, "level_limit_rank": level_rank, "potential_rank": potential_rank,
            "prima_stella_upgraded_time": 0, "produce_parameters": asdict(parameters)}],
        "support_cards": [{"card_id": row["id"], "level": int(row["level"]),
            "level_limit_rank": _rank(row.get("levelLimitRank", 0)), "stock_quantity": 1,
            "plan_type": master.one("SupportCard", row["id"])["planType"]} for row in supports[:5]],
        "memories": memories})
    rental = BorrowedSupportCard("reference-rental", supports[5]["id"], int(supports[5]["level"]),
        master.one("SupportCard", supports[5]["id"])["planType"],
        (datetime.now(timezone.utc) + timedelta(days=1)).isoformat())
    selection = LoadoutSelection(idol, tuple(row.card_id for row in snapshot.support_cards), rental,
        tuple(row.memory_id for row in snapshot.memories), snapshot.content_digest, snapshot.account_scope, history["produceId"])
    loadout = {"rental_support_cards": [{"rental_key": rental.rental_key, "card_id": rental.card_id,
        "level": rental.level}]}
    effects = resolve_loadout_effects(snapshot, loadout, selection, master.catalog)
    return snapshot, selection, effects


def bonus_calibration(episode, master):
    rules = load_audition_rules(episode["idol_card_id"], produce_id=episode["produce_id"],
        step_type=episode["step_type"], number=int(episode["step_select_number"]), master_dir=master.directory)
    curve = tuple((int(row["parameter"]), int(row["vocalPermil"]), int(row["dancePermil"]), int(row["visualPermil"]))
        for row in master.rows("ProduceExamBattleScoreConfig") if row["id"] == rules.score_config_id)
    setting = master.rows("Setting")[0]
    arguments = {"score_curve": curve, "configured_parameters": (rules.vocal_parameter, rules.dance_parameter,
        rules.visual_parameter), "penalty_min_permille": int(setting["produceExamBattleScorePenaltyMinPermil"]),
        "penalty_max_permille": int(setting["produceExamBattleScorePenaltyMaxPermil"])}
    base = calculate_audition_base_multiplier_permils(attributes=tuple(episode[axis] for axis in AXES), **arguments)
    observed = tuple(episode[axis + "_bonus_permil"] for axis in AXES)
    low = max(1000, math.floor(max((target - 10) * 1000 / value for target, value in zip(observed, base))) - 2)
    high = math.ceil(min(target * 1000 / value for target, value in zip(observed, base))) + 2
    possible = []
    for factor in range(low, min(high, 100_000) + 1):
        actual = calculate_audition_bonus_permils(AuditionProgressBonusValues(*base, factor - 1000, 0, 0))
        if tuple(asdict(actual).values()) == observed:
            possible.append(factor)
    if not possible:
        raise LoadoutRecommendationUnavailable("original audition bonuses cannot be reproduced by anchored common-factor proxy")
    return {**arguments, "base_permils": base, "observed_permils": observed,
        "inferred_combined_factor_interval": [min(possible), max(possible)], "factor_permil": possible[len(possible) // 2],
        "components_observed": False, "original_three_bonuses_exact": True,
        "assumption": "combined vote/star/effect progression held fixed; components not separately inferred"}


def _memory_active(memory, stage_id):
    phase = memory["produce_card_phase_type"]
    if phase == PHASE_START:
        return True
    if phase == PHASE_MID:
        return stage_id != "Mid1"
    raise LoadoutRecommendationUnavailable("unverified inherited-card acquisition phase: " + phase)


def replace_memory_cards(cards, before, after, stage_id):
    if len(before) != len(after):
        raise LoadoutRecommendationUnavailable("memory slot cardinality changed")
    replacements, additions = {}, []
    for previous, proposed in zip(before, after):
        old, new = previous.get("produce_card"), proposed.get("produce_card")
        old_active = old is not None and _memory_active(previous, stage_id)
        new_active = new is not None and _memory_active(proposed, stage_id)
        if old_active:
            matches = [index for index, value in enumerate(cards) if value["id"] == old["id"]]
            if len(matches) != 1 or matches[0] in replacements:
                raise LoadoutRecommendationUnavailable("reference inherited-card lineage ambiguous: " + old["id"])
            # The same inherited variant keeps its observed later upgrades and
            # customizations. Do not strip an unchanged reference continuation.
            # Changed variants retain their own observed account values; applying
            # an old card's customize IDs to a different card would be invalid.
            replacements[matches[0]] = deepcopy(cards[matches[0]] if new == old else new) if new_active else None
        elif new_active:
            additions.append(deepcopy(new))
    result = []
    for index, card in enumerate(cards):
        value = replacements.get(index, card)
        if value is not None:
            result.append(deepcopy(value))
    return result + additions


def _event_scalar_projection(supports, master, fraction):
    resources = dict.fromkeys((*AXES, "produce_points", "stamina"), 0.)
    items, missing = [], []
    mapping = {"ProduceEffectType_" + axis.title() + "Addition": axis for axis in AXES}
    mapping.update(ProduceEffectType_ProducePointAddition="produce_points", ProduceEffectType_StaminaRecoverFix="stamina")
    for support in supports:
        item_set = []
        for event in master.support_events(support["card_id"], support["level"]):
            if event["choice_required"]:
                missing.append("support-event-choice:" + event["detail_id"])
                continue
            for effect in event["effects"]:
                kind = effect["produceEffectType"]
                if kind in mapping and type(effect["effectValueMin"]) is int and effect["effectValueMin"] == effect["effectValueMax"]:
                    resources[mapping[kind]] += float(effect["effectValueMin"]) * fraction
                elif kind == "ProduceEffectType_ProduceReward":
                    for reward in effect["produceRewards"]:
                        if reward["resourceType"] == "ProduceResourceType_ProduceItem":
                            item_set.append(reward["resourceId"])
                        else:
                            missing.append("support-event-reward:" + effect["id"])
                else:
                    missing.append("support-event-transform:" + effect["id"])
        items.append(item_set)
    return resources, items, missing


def route_trigger_counts(schedules, week):
    """Reuse the existing lifecycle trigger mapping for recorded selected steps."""
    from .plan3_passive_scheduler import Plan3PassiveLifecycleEvent
    counts = Counter({"p_trigger-end_audition": 0})
    for axis in AXES:
        for sp in (False, True):
            event = Plan3PassiveLifecycleEvent.end_lesson("known-reference-trigger", axis, sp=sp)
            counts[event.trigger_id] = 0
    for step in schedules:
        if int(step["number"]) >= week:
            continue
        kind = step["selectedStepType"]
        lesson = re.fullmatch(r"ProduceStepType_(?:SelfLesson|Lesson)(Vocal|Dance|Visual)(Normal|Sp)", kind)
        if lesson:
            event = Plan3PassiveLifecycleEvent.end_lesson("reference-week:" + str(step["number"]),
                lesson[1].lower(), sp=lesson[2] == "Sp")
            counts[event.trigger_id] += 1
        if kind in {"ProduceStepType_AuditionMid1", "ProduceStepType_AuditionMid2", "ProduceStepType_AuditionFinal"}:
            counts["p_trigger-end_audition"] += 1
    return dict(counts)


def _route_passive_resources(sources, counts):
    """Only deterministic scalar rules with a known observed trigger count.

    No independence assumption is made for chance passives. Card/drink-trigger
    frequencies and competing cross-trigger finite activation limits abstain.
    """
    resources = dict.fromkeys((*AXES, "produce_points", "stamina"), 0.)
    mapping = {"ProduceEffectType_" + axis.title() + "Addition": axis for axis in AXES}
    mapping.update(ProduceEffectType_ProducePointAddition="produce_points",
        ProduceEffectType_ProducePointAdditionDisableTrigger="produce_points", ProduceEffectType_StaminaRecoverFix="stamina")
    applied, missing = [], []
    for owner in sources:
        for source in owner["passives"]:
            rules = source["rules"]
            limit = source.get("activation_count")
            for rule in rules:
                trigger = rule["trigger_id"]
                if trigger.startswith("p_trigger-produce_start") or rule["effect_type"] not in mapping:
                    continue
                if (trigger not in counts or rule["activation_rate_permil"] != 0
                        or type(rule["effect_value_min"]) is not int
                        or rule["effect_value_min"] != rule["effect_value_max"] or type(limit) is not int
                        or limit < 0 or limit and len({item["trigger_id"] for item in rules}) != 1):
                    missing.append("route-passive-unresolved:" + source["skill_id"] + ":" + trigger)
                    continue
                count = counts[trigger] if limit == 0 else min(limit, counts[trigger])
                amount = rule["effect_value_min"] * count
                resources[mapping[rule["effect_type"]]] += amount
                applied.append({"owner": owner["owner"], "skill_id": source["skill_id"],
                    "effect_id": rule["effect_id"], "trigger_id": trigger, "observed_occurrences": counts[trigger],
                    "activation_limit": limit, "applied_count": count, "scalar_total": amount})
    return resources, applied, missing


def project_loadout_context(context, snapshot, selection, effects, master):
    """Return only player fields for source-anchored isolated native evaluation."""
    if (context["produce_id"] != selection.produce_id or context["idol_card_id"] != selection.idol_card_id):
        raise LoadoutRecommendationUnavailable("loadout reference scope differs from selected idol/mode")
    source = read_reference(context["source"])
    episode = source["source"]["normalized_episode"]
    player = deepcopy(context["original_player"])
    baseline = context["baseline_effects"]
    old, new = dict(baseline["initial_resources"]), effects.initial_resources
    historical_idol = context.get("baseline_idol_parameters")
    current_idol = next(item for item in snapshot.idol_cards if item.card_id == selection.idol_card_id)
    if historical_idol is None or current_idol.produce_parameters is None:
        raise LoadoutRecommendationUnavailable("shared current-idol anchor parameters missing")
    # The historical player's account/dearness bonuses are unavailable. Hold
    # the CURRENT idol getters constant on both sides, so that missing history
    # is never credited as a support or memory improvement.
    for key, value in asdict(current_idol.produce_parameters).items():
        old[key] += value - historical_idol[key]
    gaps = ["outer-route-fixed-to-TRAIN-reference; no learned cultivation controller",
        "growth-scales-observed-route-gains-as-proxy",
        "vote-star-effect-combined-factor-held-fixed",
        "post-acquisition-memory-upgrade-customization-choices-not-transferred-to-changed-variants",
        "historical-idol-parameters-reconstructed-from-Master-rank-tables; no original DLL getters"]
    gaps.append("current-idol-getters-held-common-on-both-loadout-sides; historical-account-bonuses-not-attributed-to-cards")
    fraction = context["route"]["week"] / context["route"]["total_weeks"]
    supports = [{"card_id": key, "level": next(row.level for row in snapshot.support_cards if row.card_id == key)}
        for key in selection.support_card_ids]
    supports.append({"card_id": selection.borrowed_support.card_id, "level": selection.borrowed_support.level})
    previous_events, old_items, old_gaps = _event_scalar_projection(context["baseline_supports"], master, fraction)
    next_events, new_items, new_gaps = _event_scalar_projection(supports, master, fraction)
    counts = context["route"].get("trigger_counts", {})
    prior_passive, prior_rules, prior_missing = _route_passive_resources(baseline["sources"], counts)
    next_passive, next_rules, next_missing = _route_passive_resources(effects.sources, counts)
    gaps.extend((*prior_missing, *next_missing))
    gaps.extend((*old_gaps, *new_gaps, "support-scalar-event-completion-estimated-by-reference-week-fraction"))
    deltas, growth_deltas, sp_deltas = {}, {}, {}
    cap = int(master.one("Produce", selection.produce_id)["idolCardParameterGrowthLimit"])
    for axis in AXES:
        remaining = max(0., player[axis] - old[axis])
        growth_delta = remaining * ((1000 + new[axis + "_growth"]) / (1000 + old[axis + "_growth"]) - 1)
        modifier = "lesson_" + axis + "_sp_change_rate_permil_addition"
        before = baseline["persistent_modifiers"].get("lesson_sp_change_rate_permil_addition", 0) + baseline["persistent_modifiers"].get(modifier, 0)
        after = effects.persistent_modifiers.get("lesson_sp_change_rate_permil_addition", 0) + effects.persistent_modifiers.get(modifier, 0)
        opportunity = context["route"]["sp_opportunities"].get(axis, {"count": 0, "selected_sp": 0})
        count = opportunity["count"]
        old_rate = opportunity["selected_sp"] / count if count else 0.
        new_rate = min(1., max(0., old_rate + (after - before) / 1000))
        extra = context["route"]["sp_extra_parameter"]
        if extra is None:
            sp_delta = 0.
            if count and after != before:
                gaps.append("mode-specific-SP-gain-table-not-resolved:" + selection.produce_id + ":" + axis)
        else:
            sp_delta = count * (new_rate - old_rate) * extra * (1 + new[axis + "_growth"] / 1000)
        # Event-rate bonuses are stored in native permil units.
        old_event = previous_events[axis] * (1 + baseline["persistent_modifiers"].get("support_event_parameter_addition_value_up", 0) / 1000)
        new_event = next_events[axis] * (1 + effects.persistent_modifiers.get("support_event_parameter_addition_value_up", 0) / 1000)
        delta = new[axis] - old[axis] + growth_delta + sp_delta + new_event - old_event + next_passive[axis] - prior_passive[axis]
        player[axis] = min(cap, max(0, round(player[axis] + delta)))
        deltas[axis], growth_deltas[axis], sp_deltas[axis] = delta, growth_delta, sp_delta
    gaps.append("SP-proxy-uses-observed-selection-frequency-and-lowest-static-lesson-gain; no counterfactual route changes")
    maximum_delta = new["stamina"] - old["stamina"]
    player["maxStamina"] = max(1, round(player["maxStamina"] + maximum_delta))
    # Preserve the observed depletion fraction, rather than giving free full HP.
    player["stamina"] = min(player["maxStamina"], max(0, round(context["original_player"]["stamina"] *
        player["maxStamina"] / max(1, context["original_player"]["maxStamina"]))))
    pt_delta = (new["produce_points"] - old["produce_points"] + next_events["produce_points"] - previous_events["produce_points"]
        + next_passive["produce_points"] - prior_passive["produce_points"])
    if pt_delta:
        gaps.append("changed-PT-income-not-yet-converted-to-upgrades-or-purchases:" + str(round(pt_delta, 3)))
    if next_events["stamina"] + next_passive["stamina"] != previous_events["stamina"] + prior_passive["stamina"]:
        gaps.append("event-stamina-recovery-not-propagated-through-reference-weekly-actions")
    player["produceCards"] = replace_memory_cards(player["produceCards"], context["baseline_memories"],
        effects.memory_cards, context["stage_id"])
    original_items = {row["produceItemId"] for row in player.get("produceItems", [])}
    replacements = {}
    for prior, proposed in zip(old_items, new_items):
        observed_prior = [value for value in prior if value in original_items]
        if observed_prior:
            replacements[observed_prior[0]] = proposed
            replacements.update({value: [] for value in observed_prior[1:]})
    retained_items, seen_items = [], set()
    for item in player.get("produceItems", []):
        proposed = replacements.get(item["produceItemId"])
        for value in ([item] if proposed is None else [{"produceItemId": key} for key in proposed]):
            if value["produceItemId"] not in seen_items:
                retained_items.append(value); seen_items.add(value["produceItemId"])
    player["produceItems"] = retained_items
    gaps.append("support-item-event-arrival-follows-original-slot-item-presence; other-event-order-not-simulated")
    player["supportCards"] = []
    for support in supports:
        row = master.one("SupportCard", support["card_id"])
        runtime = resolve_passive_runtime(master.catalog.resolve_support_card(support["card_id"], support["level"]))
        player["supportCards"].append({"supportCardId": support["card_id"],
            "produceCardUpgradePermil": int(row["produceCardUpgradePermil"]) *
                (1000 + runtime.modifiers.support_card_upgrade_probability_up) // 1000})
    calibration = context["bonus_calibration"]
    args = {key: calibration[key] for key in ("score_curve", "configured_parameters", "penalty_min_permille", "penalty_max_permille")}
    args["score_curve"] = tuple(tuple(row) for row in args["score_curve"])
    args["configured_parameters"] = tuple(args["configured_parameters"])
    base = calculate_audition_base_multiplier_permils(attributes=tuple(player[axis] for axis in AXES), **args)
    final = calculate_audition_bonus_permils(AuditionProgressBonusValues(*base, calibration["factor_permil"] - 1000, 0, 0))
    for axis, value in asdict(final).items():
        player[axis + "BonusPermil"] = value
    allowed = (*AXES, *(axis + "BonusPermil" for axis in AXES), "stamina", "maxStamina", "produceCards", "supportCards", "produceItems")
    return {"context_id": context["context_id"], "source": context["source"],
        "player_fields": {key: player[key] for key in allowed}, "limitations": sorted(set(gaps)),
        "effect_evidence": {"method": "source-anchored-outer-delta-v1", "initial_deltas": {key: new[key] - old[key] for key in new},
            "attribute_deltas": deltas, "growth_deltas": growth_deltas, "sp_deltas": sp_deltas,
            "deterministic_route_passives": {"before": prior_rules, "after": next_rules},
            "unconverted_pt_delta": pt_delta, "bonus_calibration": calibration,
            "original_source_mutated": False, "new_training_data_qualified": False}}


def make_reference_context(row, source_reference, master):
    source = read_reference(source_reference)
    metadata = source["source"]; episode = metadata["normalized_episode"]
    raw_ref = {"path": metadata["raw_path"], "sha256": metadata["raw_sha256"]}
    raw = read_reference(raw_ref)
    history = raw["response"]["histories"][metadata["history_index"]]["produceHistory"]
    scope = row["state"]["scope"]
    baseline_snapshot, _, baseline = baseline_inventory(history, master, scope=row["whole_trajectory_id"])
    stage_index = int(metadata["stage_index"]); section_index = int(metadata["section_index"])
    player = deepcopy(source["contest_situation"]["stages"][stage_index]["selfSections"][section_index]["player"])
    player.pop("examActions", None)
    week = next((int(step["number"]) for step in history["schedules"] if step["selectedStepType"] == episode["step_type"]), None)
    if week is None:
        raise LoadoutRecommendationUnavailable("original audition week is not observed")
    opportunities = {}
    for axis in AXES:
        prefix = "ProduceStepType_SelfLesson" + axis.title()
        selected = [step for step in history["schedules"] if int(step["number"]) < week]
        opportunities[axis] = {"count": sum(any(str(kind).startswith(prefix) for kind in step["stepTypes"]) for step in selected),
            "selected_sp": sum(step["selectedStepType"] == prefix + "Sp" for step in selected)}
    lessons = [value for value in master.rows("ProduceStepSelfLesson")
        if str(value["id"]).startswith("self_lesson-" + episode["produce_id"].replace("-", "_") + "-")]
    normal = [int(value["parameter"]) for value in lessons if value["id"].endswith("-normal")]
    sp = [int(value["parameter"]) for value in lessons if value["id"].endswith("-sp")]
    memories = list(baseline.memory_cards)
    replace_memory_cards(player["produceCards"], memories, memories, scope["stage_id"])
    baseline_summary = asdict(baseline)
    # Source YAML is already hash-pinned in the artifact. Repeating its lengthy
    # descriptions and entire Master rows for every stage adds no evidence.
    for source_entry in baseline_summary["sources"]:
        for passive in source_entry["passives"]:
            passive.pop("raw", None)
            for rule in passive["rules"]:
                rule.pop("raw", None)
    return {"context_id": row["episode_id"] + ":seed:" + str(player["seed"]),
        "source": source_reference, "raw_reference": raw_ref, "partition": "train",
        "player_sha256": row["player_sha256"], "whole_trajectory_id": row["whole_trajectory_id"],
        "source_group": row["source_group"], "source_master_hash": row["source_binding"]["source_master_hash"],
        "produce_id": source["produce_id"], "idol_card_id": source["idol_card_id"],
        "flow_id": scope["flow_id"], "stage_id": scope["stage_id"], "seed": player["seed"],
        "original_player": player, "baseline_effects": baseline_summary, "baseline_memories": memories,
        "baseline_idol_parameters": asdict(baseline_snapshot.idol_cards[0].produce_parameters),
        "baseline_supports": [{"card_id": item["id"], "level": int(item["level"])} for item in history["deckSupportCards"]],
        "route": {"week": week, "total_weeks": max(int(step["number"]) for step in history["schedules"]),
            "trigger_counts": route_trigger_counts(history["schedules"], week),
            "sp_opportunities": opportunities, "sp_extra_parameter": max(0, min(sp) - min(normal)) if normal and sp else None},
        "bonus_calibration": bonus_calibration(episode, master),
        "qualified_evidence": {"source_binding": row["source_binding"],
            "qualification": row["provenance"]["sources"]["qualification"],
            "inputs_reference": row["provenance"]["sources"]["inputs_reference"]}}
