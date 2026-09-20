"""Master effect graphs for the shared outer *utility estimate*.

This is not the battle reducer. Quantities and graph edges are authoritative;
activation timing is an explicit finite-horizon estimate. Unsupported shapes
remain unresolved, never a zero-valued benefit declared fully modeled.
"""
from collections import Counter
from dataclasses import dataclass, field, replace
from functools import lru_cache
import json
from pathlib import Path
import sqlite3

from .logic_engine import MasterCardEffect, load_master_effect, load_master_card


@dataclass
class EffectFeatures:
    direct: float = 0.
    hits: float = 0.
    gains: Counter = field(default_factory=Counter)
    converters: Counter = field(default_factory=Counter)
    spend: Counter = field(default_factory=Counter)
    amplify: Counter = field(default_factory=Counter)
    draw: float = 0.
    extra_play: float = 0.
    extra_turn: float = 0.
    stamina: float = 0.
    force_stamina: float = 0.
    recovery: float = 0.
    initial: bool = False
    lost: bool = False
    residual_prior: float = 0.
    unknown: set = field(default_factory=set)
    schedules: list = field(default_factory=list)
    terms: list = field(default_factory=list)
    assumptions: list = field(default_factory=list)
    category: str = ""
    lesson_multiple_turns: float = 0.
    condition_multiple_turns: float = 0.
    stamina_down_turns: float = 0.
    stamina_up_turns: float = 0.
    free_plays: float = 0.
    hand_upgrades: float = 0.
    fixed_stamina_reduction: float = 0.
    generated_slots: float = 0.
    direct_stamina_damage: float = 0.


GAINS = {"ExamReview": "review", "ExamCardPlayAggressive": "motivation", "ExamBlock": "block",
    "ExamBlockFix": "block", "ExamLessonBuff": "concentration", "ExamFullPowerPoint": "full_power",
    "ExamEnthusiasticAdditive": "enthusiasm"}
CONVERTERS = {"ExamLessonDependExamReview": "review", "ExamLessonDependBlock": "block",
    "ExamLessonDependExamCardPlayAggressive": "motivation", "ExamLessonDependParameterBuff": "condition"}
MULTIPLY = {"ExamReviewValueMultiple": "review", "ExamReviewMultiple": "review",
    "ExamAggressiveValueMultiple": "motivation", "ExamBlockValueMultiple": "block",
    "ExamLessonBuffMultiple": "concentration", "ExamReviewAdditive": "review",
    "ExamAggressiveAdditive": "motivation", "ExamLessonBuffAdditive": "concentration",
    "ExamFullPowerPointAdditive": "full_power", "ExamEnthusiasticMultiple": "enthusiasm"}
SCALARS = ("direct", "hits", "draw", "extra_play", "extra_turn", "stamina", "force_stamina", "recovery",
    "residual_prior", "lesson_multiple_turns", "condition_multiple_turns", "stamina_down_turns", "stamina_up_turns",
    "free_plays", "hand_upgrades", "fixed_stamina_reduction", "generated_slots", "direct_stamina_damage")
COUNTERS = ("gains", "converters", "spend", "amplify")
COSTS = {"ExamReview": "review", "ExamCardPlayAggressive": "motivation", "ExamBlock": "block",
         "ExamLessonBuff": "concentration", "ExamParameterBuff": "condition", "ExamFullPowerPoint": "full_power"}


@lru_cache(maxsize=8192)
def _read(path, stamp, table, identity):
    if table not in {"effect", "produce_exam_trigger", "produce_exam_status_enchant", "produce_card_search"}:
        raise ValueError("unrecognized Master graph table")
    try:
        with sqlite3.connect("file:" + path.as_posix() + "?mode=ro", uri=True) as connection:
            row = connection.execute(f"SELECT raw_json FROM {table} WHERE id=?", (identity,)).fetchone()
        return json.loads(row[0]) if row else None
    except (sqlite3.Error, OSError, ValueError):
        return None


def master_row(database, table, identity):
    path = Path(database).resolve()
    try:s = path.stat();stamp = (s.st_mtime_ns, s.st_size)
    except OSError:return None
    return _read(path, stamp, table, identity)


@lru_cache(maxsize=8192)
def _child_effect(path, stamp, identity):
    return load_master_effect(identity, path)


def _merge(target, source, factor=1.):
    for key in SCALARS:setattr(target, key, getattr(target, key) + factor * getattr(source, key))
    for key in COUNTERS:getattr(target, key).update({k: v * factor for k, v in getattr(source, key).items()})
    target.unknown.update(source.unknown)
    target.terms.extend(source.terms)
    target.assumptions.extend(source.assumptions)


def project_master_card(features, card, *, database, ancestry=()):
    """The same card effects/costs for owned cards and generated descendants."""
    key=f'card:{card.id}@{card.upgrade}'
    if key in ancestry or len(ancestry)>=12:
        features.unknown.add(f'generated-card-cycle:{key}');return
    ancestry=(*ancestry,key)
    features.category=card.category
    features.stamina,features.force_stamina=card.stamina_cost,card.force_stamina_cost
    cost=card.cost_type.removeprefix('ExamCostType_')
    if cost in COSTS:features.spend[COSTS[cost]]+=card.cost_value
    elif cost not in {'Unknown','','Stamina'}:features.unknown.add(f'cost:{card.id}:{cost}')
    features.lost=card.move_position_type.endswith('_Lost')
    for effect in card.effects:project_effect(features,effect,database=database,ancestry=ancestry)
    if card.play_trigger_id:
        features.schedules.append({'kind':'card-eligibility','effect_id':card.id,'trigger_id':card.play_trigger_id})
    if card.produce_card_status_enchant_id or card.move_effect_ids:
        features.unknown.add(f'card-movement-or-status:{card.id}')


def _created_card(features,effect,raw,*,database,factor,ancestry):
    from .plan3_card_create_id import resolve_card_create_contract,CardCreateResolutionError
    try:
        contract=resolve_card_create_contract(raw)
        if contract.count_min!=1 or contract.count_max!=1 or contract.search2_id or contract.pick_count_type!='ProducePickCountType_Unknown':
            features.unknown.add(f'generated-card-count-or-search:{effect.id}');return
        if any((effect.value1,effect.value2,effect.effect_count,effect.effect_turn)) or any(raw.get(k) for k in
            ('produceCardSearchId','produceCardStatusEnchantId','produceCardGrowEffectIds')):
            features.unknown.add(f'generated-card-extra-payload:{effect.id}');return
        card=load_master_card(contract.card_id,contract.upgrade,Path(database))
    except (CardCreateResolutionError,OSError,ValueError,KeyError,sqlite3.Error) as error:
        features.unknown.add(f'generated-card-master:{effect.id}:{error}');return
    child=EffectFeatures()
    project_master_card(child,card,database=database,ancestry=ancestry)
    if card.is_end_turn_lost:
        child.unknown.add(f'generated-card-end-turn-expiry:{card.id}')
    features.schedules.append({'kind':'card-create','effect_id':effect.id,'destination':contract.destination,
        'card_id':card.id,'upgrade':card.upgrade,'count':1,'factor':factor,'children':[child]})


def project_effect(features, effect, *, database, factor=1., ancestry=()):
    """Compile ordered Master children; no trigger is declared always firing."""
    if effect.id in ancestry or len(ancestry) >= 12:
        features.unknown.add(f"effect-cycle:{effect.id}");return
    ancestry = (*ancestry, effect.id)
    if effect.once:
        child = EffectFeatures()
        project_effect(child, replace(effect, once=False), database=database, factor=factor, ancestry=ancestry[:-1])
        features.schedules.append({"kind": "once", "effect_id": effect.id, "children": [child]})
        return
    kind = effect.effect_type.removeprefix("ProduceExamEffectType_")
    raw = master_row(database, "effect", effect.id) or {}
    children = []
    if kind == "ExamEffectTimer":
        ids = raw.get("chainProduceExamEffectIds", []) or ([effect.chain_effect_id] if effect.chain_effect_id else [])
        if effect.value1 < 1 or effect.value2 or effect.effect_count != 1 or not ids:
            features.unknown.add(f"timer-shape:{effect.id}");return
        schedule = {"kind": "timer", "delay": effect.value1, "effect_id": effect.id}
    elif kind == "ExamStatusEnchant":
        status = master_row(database, "produce_exam_status_enchant", effect.status_enchant_id)
        if status is None:
            features.unknown.add(f"persistent-status-missing:{effect.id}");return
        ids = status.get("produceExamEffectIds", [])
        schedule = {"kind": "persistent", "trigger_id": status.get("produceExamTriggerId", ""),
            "turns": effect.effect_turn, "limit": effect.effect_count, "effect_id": effect.id}
    elif effect.trigger_id:
        child = EffectFeatures()
        project_effect(child, replace(effect, trigger_id=""), database=database, factor=factor, ancestry=ancestry[:-1])
        features.schedules.append({"kind": "conditional", "trigger_id": effect.trigger_id,
            "effect_id": effect.id, "children": [child]})
        return
    elif effect.status_enchant_id or effect.chain_effect_id or raw.get("chainProduceExamEffectIds"):
        features.unknown.add(f"unresolved-effect-graph:{effect.id}:{kind}");return
    else:ids = None
    if ids is not None:
        if not ids:
            features.unknown.add(f"empty-effect-graph:{effect.id}");return
        for identity in ids:
            try:
                path = Path(database).resolve();stat = path.stat()
                child_effect = _child_effect(path, (stat.st_mtime_ns, stat.st_size), identity)
            except (OSError, sqlite3.Error, ValueError, KeyError):
                features.unknown.add(f"effect-child-missing:{effect.id}:{identity}");continue
            child = EffectFeatures()
            project_effect(child, child_effect, database=database, factor=factor, ancestry=ancestry)
            children.append(child)
        schedule["children"] = children
        features.schedules.append(schedule)
        return
    v1, v2, count, turn = effect.value1, effect.value2, max(1, effect.effect_count), effect.effect_turn
    quantity_source = "value1"
    if kind in GAINS:
        features.gains[GAINS[kind]] += v1 * factor
        if kind == "ExamBlock":features.converters["motivation_to_block"] += factor
    elif kind == "ExamParameterBuff":features.gains["condition"] += turn * factor;quantity_source = "turn"
    elif kind in {"ExamLesson", "ExamLessonFix"}:features.direct += v1 * count * factor;features.hits += count * factor
    elif kind in CONVERTERS:features.converters[CONVERTERS[kind]] += v1 / 1000 * count * factor;features.hits += count * factor
    elif kind == "ExamLessonAddMultipleParameterBuff":
        features.direct += v1 * count * factor;features.hits += count * factor
        features.schedules.append({"kind": "condition-score", "effect_id": effect.id,
            "value": v1 * count * v2 / 1000 * factor})
    elif kind == "ExamBlockAddMultipleAggressive":
        features.gains["block"] += v1 * count * factor;features.converters["motivation_to_block"] += (1 + v2 / 1000) * count * factor
    elif kind == "ExamReviewDependExamBlock":features.converters["block_to_review"] += v1 / 1000 * factor
    elif kind in MULTIPLY:features.amplify[MULTIPLY[kind]] += v1 / 1000 * factor
    elif kind == "ExamCardDraw":features.draw += v1 * factor
    elif kind == "ExamPlayableValueAdd" and effect.effect_count > 0:
        features.extra_play += effect.effect_count * factor;quantity_source = "effect_count"
    elif kind == "ExamExtraTurn" and (v1, v2, effect.effect_count, turn) == (0, 0, 0, 0):
        features.extra_turn += factor;quantity_source = "native-zero-payload-increment-one"
    elif kind in {"ExamLessonValueMultiple", "ExamParameterBuffMultiplePerTurn", "ExamStaminaConsumptionDown", "ExamStaminaConsumptionAdd"}:
        if turn == 0 or turn < -1:
            features.unknown.add(f"status-duration:{effect.id}");return
        duration = {"ExamLessonValueMultiple": "lesson_multiple_turns", "ExamParameterBuffMultiplePerTurn": "condition_multiple_turns",
            "ExamStaminaConsumptionDown": "stamina_down_turns", "ExamStaminaConsumptionAdd": "stamina_up_turns"}[kind]
        features.schedules.append({"kind": "duration", "effect_id": effect.id, "turns": turn,
            "metric": duration, "rate": (v1 / 1000 if kind == "ExamLessonValueMultiple" else 1.) * factor})
    elif kind == "ExamStaminaRecoverFix":features.recovery += v1 * factor
    elif kind == "ExamStaminaConsumptionDownFix":
        from .plan2_native_catalog_stamina import StaminaConsumptionDownFixEffect
        try:StaminaConsumptionDownFixEffect(effect.id,v1,v2,effect.effect_count,turn)
        except ValueError:
            features.unknown.add(f'fixed-stamina-reduction-shape:{effect.id}');return
        features.schedules.append({'kind':'fixed-stamina-reduction','effect_id':effect.id,'value':v1*factor})
    elif kind == "ExamCardCreateId":
        _created_card(features,effect,raw,database=database,factor=factor,ancestry=ancestry)
    elif kind == "ExamStaminaReduceFix":
        features.force_stamina += v1 * factor
        features.direct_stamina_damage += v1 * factor
    elif kind in {"ExamReviewReduce", "ExamAggressiveReduce"}:features.spend["review" if kind == "ExamReviewReduce" else "motivation"] += v1 * factor
    elif kind == "ExamSearchPlayCardStaminaConsumptionChange" and v1 == 0 and effect.effect_count > 0 and raw.get("produceCardSearchId") == "p_card_search-deck_all":
        features.free_plays += effect.effect_count * factor;quantity_source = "effect_count"
    elif kind == "ExamCardUpgrade" and raw.get("produceCardSearchId") == "p_card_search-hand" and raw.get("pickRangeType") == "ProducePickRangeType_All":
        features.hand_upgrades += factor;quantity_source = "native-hand-all-upgrade"
    elif kind == "Unknown" and not any((v1, v2, turn, effect.effect_count)):return
    else:
        features.unknown.add(f"effect:{effect.id}:{kind}");return
    features.terms.append({"effect_id": effect.id, "effect_type": kind, "value1": v1, "value2": v2,
        "count": effect.effect_count, "turn": turn, "quantity_source": quantity_source, "modeled": True})


def _trigger(trigger_id, *, database, environment, window, persistent):
    raw = master_row(database, "produce_exam_trigger", trigger_id)
    if raw is None:return None, f"trigger-missing:{trigger_id}"
    phases = raw.get("phaseTypes", [])
    if len(phases) != 1:return None, f"trigger-phase-shape:{trigger_id}"
    phase = phases[0].removeprefix("ProduceExamPhaseType_")
    if phase in {"ExamEndTurn", "ExamStartTurn", "ExamTurnInterval", "StartPlay"}:events = window
    elif phase in {"ExamCardPlay", "ExamCardPlayAfter", "ExamPlayCountInterval", "ExamPlayTurnCountInterval"}:events = window * environment["plays_per_turn"]
    elif phase in {"None", "ExamStartExam"}:events = 1.
    else:return None, f"trigger-event-context:{trigger_id}:{phase}"
    probability = 1.
    if raw.get("fieldStatusCheckTypes"):
        return None, f"trigger-comparison-operator-unresolved:{trigger_id}"
    search = raw.get("produceCardSearchId")
    if search:
        sr = master_row(database, "produce_card_search", search)
        if not sr:return None, f"trigger-search-unresolved:{trigger_id}:{search}"
        categories = sr.get("cardCategories", [])
        if sr.get("produceCardIds") or sr.get("upgradeCounts"):
            return None, f"trigger-card-identity-context:{trigger_id}:{search}"
        if categories:
            probability *= sum(environment["category_shares"].get(c, 0.) for c in categories)
        if sr.get("cardSearchTag") or sr.get("effectGroupIds") or sr.get("isSelf") or sr.get("isCustomized") or sr.get("cardRarities"):
            return None, f"trigger-search-filter-unresolved:{trigger_id}:{search}"
        if sr.get("cardPositionType") not in ("ProduceCardPositionType_Playing", "ProduceCardPositionType_Target"):
            return None, f"trigger-search-zone-unresolved:{trigger_id}:{search}"
        if raw.get("lowerSearchCount", 0) not in (0, 1) or raw.get("upperSearchCount", 0) not in (0, 1):
            return None, f"trigger-search-count-unresolved:{trigger_id}:{search}"
    types, values = raw.get("fieldStatusTypes", []), raw.get("fieldStatusValues", [])
    if raw.get("fieldStatusProduceCardSearchIds") or raw.get("effectTypes") or raw.get("cardMovePositionType") not in (None, "", "ProduceCardMovePositionType_Unknown"):
        return None, f"trigger-runtime-predicate:{trigger_id}"
    stocks = environment["stocks"]
    fields = {"ParameterBuff": "condition", "LessonBuffUp": "concentration", "ReviewUp": "review",
        "CardPlayAggressiveUp": "motivation", "BlockUp": "block"}
    for i, field in enumerate(types):
        name = field.removeprefix("ProduceExamFieldStatusType_")
        if name not in fields:return None, f"trigger-field-unresolved:{trigger_id}:{name}"
        threshold = max(1., values[i] if i < len(values) else 1.)
        probability *= min(1., max(0., stocks.get(fields[name], 0.)) / threshold)
    if raw.get("phaseValues"):
        if len(raw["phaseValues"]) != 1:return None, f"trigger-phase-arguments:{trigger_id}"
        if phase not in {"ExamPlayCountInterval", "ExamPlayTurnCountInterval", "ExamTurnInterval"}:
            return None, f"trigger-phase-arguments:{trigger_id}"
        interval = raw["phaseValues"][0]
        if type(interval) is not int or interval < 1:return None, f"trigger-interval-invalid:{trigger_id}"
        events /= interval
    if raw.get("lessonType") not in (None, "", "ProduceStepLessonType_Unknown"):
        return None, f"trigger-lesson-context:{trigger_id}"
    return (events if persistent else 1.) * probability, None


def materialize(features, *, database, environment, horizon, available=None):
    """Resolve delayed/recurring contributions with disclosed timing estimates.

    Activation ranges are opportunity ranges, not confidence intervals or
    guaranteed numerical value bounds. Unknown predicates remain unresolved.
    """
    uniform_position = available is None
    available = max(0., (horizon - 1) / 2) if uniform_position else max(0., available)
    result = EffectFeatures(category=features.category, lost=features.lost, initial=features.initial)
    _merge(result, features)
    eligibility = 1.
    for schedule in features.schedules:
        kind, eid = schedule["kind"], schedule["effect_id"]
        if kind=='fixed-stamina-reduction':
            payments=available*max(0.,environment['plays_per_turn'])
            result.fixed_stamina_reduction+=schedule['value']*payments
            result.terms.append({'effect_id':eid,'timing':kind,'modeled':True,'fixed_reduction_per_payment':schedule['value'],
                'estimated_future_payments':payments,'native_lifetime':'permanent-until-exam-end',
                'activation_opportunity_range':[0.,max(0.,horizon-1)*environment['plays_per_turn']]})
            result.assumptions.append('fixed reduction: uniform installation position; subsequent payments only; capped by estimated payable costs')
            continue
        if kind=='card-create':
            size=max(1.,environment.get('deck_count',1.));future_draws=2*available
            destination=schedule['destination']
            if destination=='hand':exposure=min(1.,available+max(0.,result.extra_play))
            elif destination=='deck_first':exposure=min(1.,available)
            elif destination=='deck_random':exposure=min(1.,future_draws/size)
            else:exposure=min(1.,max(0.,future_draws-size)/size)
            window=max(0.,available-(0. if destination=='hand' else 1.))
            child=materialize(schedule['children'][0],database=database,environment=environment,horizon=horizon,available=window)
            # Resolving a child is not a reason to assume it is free or played
            # immediately. Its represented share dilutes the same play budget.
            weight=exposure*schedule['factor']
            _merge(result,child,weight)
            result.generated_slots+=weight
            result.terms.append({'effect_id':eid,'timing':kind,'modeled':not child.unknown,
                'target_card_id':schedule['card_id'],'target_upgrade':schedule['upgrade'],'created_count':1,
                'destination':destination,'estimated_generated_card_share':weight,'activation_opportunity_range':[0.,schedule['factor']],
                'shares_existing_play_budget':True,'executes_on_creation':False})
            result.assumptions.append('generated card: finite placement/draw exposure and uniform card-use share; hand capacity is not observed; no free execution')
            continue
        if kind == "duration":
            turns = available if schedule["turns"] == -1 else min(available + 1, schedule["turns"])
            setattr(result, schedule["metric"], getattr(result, schedule["metric"]) + max(0., turns) * schedule["rate"])
            result.terms.append({"effect_id": eid, "timing": kind, "expected_active_turns": turns});continue
        if kind == "condition-score":
            rate = min(1., environment["stocks"].get("condition", 0.))
            result.direct += schedule["value"] * rate
            result.terms.append({"effect_id": eid, "timing": kind, "condition_presence_estimate": rate});continue
        if kind == "once":
            activations = 1 / max(1., environment.get("source_uses", 1.))
            window = available
        elif kind == "timer":
            delay = schedule["delay"]
            # Uniform acquisition/use-position proxy; last-turn uses cannot
            # realize a next-turn child. A delay beyond the exam pays nothing.
            activations = max(0., (horizon - delay) / horizon) if uniform_position else float(available >= delay)
            window = max(0., available - delay)
        else:
            window = available
            if kind == "persistent" and schedule["turns"] > 0:window = min(window, schedule["turns"])
            activations, error = _trigger(schedule["trigger_id"], database=database,
                environment=environment, window=window, persistent=kind == "persistent")
            if error:
                result.unknown.add(error);result.terms.append({"effect_id": eid, "timing": kind, "modeled": False, "reason": error});continue
            if kind == "card-eligibility":
                eligibility *= activations
            if kind == "persistent" and schedule["limit"] > 0:activations = min(activations, schedule["limit"])
        result.assumptions.append("uniform-use-position; deck-share event rate; resource thresholds estimated, not observed")
        opportunity = min(1., activations) if kind in {"timer", "once", "conditional", "card-eligibility"} else window * max(1., environment["plays_per_turn"])
        if kind == "persistent" and schedule["limit"] > 0:opportunity = min(opportunity, schedule["limit"])
        result.terms.append({"effect_id": eid, "timing": kind, "modeled": True,
            "expected_activations": activations, "activation_opportunity_range": [0., max(activations, opportunity)],
            "available_turns_estimate": window, "delay": schedule.get("delay"), "trigger_id": schedule.get("trigger_id")})
        for child in schedule.get("children", []):
            if kind == "timer" and activations == 0:
                # Proven outside this horizon; neither its benefit nor its
                # unresolved future mechanics can affect the comparison.
                continue
            realized = materialize(child, database=database, environment=environment, horizon=horizon, available=window)
            contributions = {key: [0., max(0., getattr(realized, key)) * max(activations, opportunity)]
                for key in ("direct", "hits", "draw", "extra_play", "extra_turn", "recovery") if getattr(realized, key)}
            contributions.update({f"gain:{key}": [0., max(0., value) * max(activations, opportunity)]
                for key, value in realized.gains.items() if value})
            result.terms.append({"effect_id": eid, "timing": kind,
                "effect_contribution_scenario_range": contributions,
                "unresolved_contribution_range": None if realized.unknown else {},
                "range_unit": "effect quantities before battle modifiers; not a score confidence interval"})
            _merge(result, realized, activations)
    if eligibility != 1.:
        # A conditional card's cost and benefits occur together; dropping only
        # its rewards while retaining a guaranteed cost creates a false loss.
        for key in SCALARS:setattr(result, key, getattr(result, key) * eligibility)
        for key in COUNTERS:
            values = getattr(result, key)
            for k in tuple(values):values[k] *= eligibility
    return result
