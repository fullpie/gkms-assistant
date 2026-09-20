"""One pure, deck-level counterfactual value for native produce choices.

This is an explicit finite-horizon *ranking proxy*, not an exam score forecast.
It reuses the existing Master loader/customization catalog. Resource producers,
converters, draw dilution and costs are valued together in the complete deck;
there is no Plan-specific outer controller or independent single-card ranking.
An optional caller-bound model oracle can replace the proxy for both decks.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
from functools import lru_cache
from itertools import combinations
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import threading
import yaml
from typing import Any, Callable, Mapping, Sequence

from .logic_engine import MasterCard, MasterCardEffect, load_master_card, load_master_effect
from .master_db import DEFAULT_DATABASE
from .passive_catalog import DEFAULT_MASTER_DIR
from .outer_pt_value import produce_point_opportunity_cost
from .outer_effect_valuation import EffectFeatures, GAINS, CONVERTERS, COSTS, project_effect, project_master_card, materialize, master_row


class DeckValueError(ValueError):
    pass


@dataclass(frozen=True)
class DeckValueDelta:
    value: float
    before_value: float
    after_value: float
    reasons: tuple[str, ...]
    unscored_effects: tuple[str, ...]
    context_digest: str
    method: str = "semantic-deck-horizon-v1"
    retained_indexes: tuple[int, ...] = ()
    plan_progress: Mapping[str, Any] | None = None
    valuation: Mapping[str, Any] | None = None

    @property
    def ranking_key(self) -> tuple[bool, float, float]:
        """Bound the historical hint relative to the mutation's utility.

        A reference never makes a negative net-utility PT purchase beneficial.
        Unknown effects retain semantic ordering rather than claiming a plan
        matching card is necessarily useful. This is a heuristic, not a forecast.
        """
        progress = self.plan_progress or {}
        plan_delta = (float(progress["value"]) if self.value > 0 and not (self.valuation or {}).get("mutation_unresolved_effects")
                      and progress.get("status") == "compared" else 0.0)
        # Utility leads. The historical composition is a bounded preference
        # within 2% of the mutation's scale, never a separate primary objective.
        hint = max(-1., min(1., plan_delta)) * .02 * max(1., abs(self.value))
        return self.value > 0, self.value + hint, self.value


def _number(value: Any, name: str, default: float = 0.0) -> float:
    if value is None:
        return default
    if type(value) not in (int, float) or not math.isfinite(value):
        raise DeckValueError(f"{name} must be a finite number")
    return float(value)


def _int(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise DeckValueError(f"{name} must be a nonnegative integer")
    return value


def card_customizations(row: Mapping[str, Any]) -> tuple[tuple[str, int], ...]:
    """Preserve native customization slots/counts, including empty proto defaults."""
    if "customizes" in row:
        values = row["customizes"]
        if not isinstance(values, (list, tuple)):
            raise DeckValueError("card customizes must be an array")
        pairs = []
        for value in values:
            if not isinstance(value, Mapping):
                raise DeckValueError("card customize must be a structured row")
            identity = value.get("id", value.get("produceCardCustomizeId", value.get("customize_id")))
            count = _int(value.get("customizeCount", value.get("customize_count", 0)), "customize count")
            if not isinstance(identity, str) or not identity:
                raise DeckValueError("card customize id is missing")
            pairs.append((identity, count))
    else:
        ids, counts = row.get("customize_ids", ()), row.get("customize_counts", ())
        if not isinstance(ids, (list, tuple)) or not isinstance(counts, (list, tuple)):
            raise DeckValueError("card customization slot/count lineage is incomplete")
        # A new reward card exposes the Master's available option IDs while
        # its actual applied-count list is explicitly empty. Those available
        # options are not purchases. Missing or partial nonempty counts still
        # require the original complete slot lineage.
        if "customize_counts" in row and not counts:
            counts = (0,) * len(ids)
        if len(ids) != len(counts):
            raise DeckValueError("card customization slot/count lineage is incomplete")
        pairs = [(str(identity), _int(count, "customize count")) for identity, count in zip(ids, counts)]
    if len({identity for identity, _ in pairs}) != len(pairs):
        raise DeckValueError("duplicate card customization slot")
    return tuple((identity, count) for identity, count in pairs if count)


def normalize_deck(deck: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    if not isinstance(deck, (list, tuple)) or any(not isinstance(row, Mapping) for row in deck):
        raise DeckValueError("complete observed deck must be an array, never a guessed empty deck")
    result, numbers, identities = [], set(), set()
    for row in deck:
        deleted = row.get("deleted", False)  # protobuf bool default
        if type(deleted) is not bool:
            raise DeckValueError("card deleted flag must be boolean")
        if deleted:
            continue
        card_id = row.get("produceCardId", row.get("card_id"))
        if not isinstance(card_id, str) or not card_id:
            raise DeckValueError("deck card id is missing")
        upgrade = _int(row.get("upgradeCount", row.get("upgrade", 0)), "deck upgrade")
        value = dict(row)
        value.update(card_id=card_id, upgrade=upgrade,
                     customizes=[{"id": identity, "customizeCount": count}
                                 for identity, count in card_customizations(row)])
        # A missing Number is protobuf zero only for actual proto rows. Projected
        # additions deliberately have no Number/GUID and carry a proposed ID.
        number = row.get("number", row.get("deck_number"))
        if number is None and "produceCardId" in row and not str(row.get("instance_id", "")).startswith("proposed:"):
            number = 0
        if number is not None:
            number = _int(number, "deck number")
            if number in numbers:
                raise DeckValueError("duplicate real deck Number")
            numbers.add(number)
            value["deck_number"] = number
        identity = row.get("instance_id")
        if identity is not None:
            if not isinstance(identity, str) or not identity or identity in identities:
                raise DeckValueError("duplicate or invalid deck instance_id")
            identities.add(identity)
        result.append(value)
    return tuple(result)


def _fingerprint(path: Path) -> tuple[int, int]:
    try:
        stat = path.stat()
        return stat.st_mtime_ns, stat.st_size
    except FileNotFoundError:
        return 0, 0


@lru_cache(maxsize=4096)
def _master(card_id: str, upgrade: int, database: Path, fingerprint: tuple[int, int]) -> MasterCard:
    if not database.is_file():
        raise DeckValueError(f"Master database unavailable: {database}")
    return load_master_card(card_id, upgrade, database)


@lru_cache(maxsize=4096)
def _raw_card(card_id: str, upgrade: int, database: Path, fingerprint: tuple[int, int]) -> Mapping[str, Any]:
    if not database.is_file():
        return {}
    with sqlite3.connect(f"file:{database.resolve().as_posix()}?mode=ro", uri=True) as connection:
        row = connection.execute("SELECT raw_json FROM card WHERE id=? AND upgrade_count=?", (card_id, upgrade)).fetchone()
    return json.loads(row[0]) if row else {}


_CUSTOMIZATION_VERSIONS = {}
_CUSTOMIZATION_LOCK = threading.Lock()


def _customization_indexes(master_dir: Path):
    # This is the very same cached Master index used by the native exam rules,
    # not a second catalog or a per-card whitelist.
    from .plan3_native_state import _runtime_master_indexes
    master_dir = master_dir.resolve()
    fingerprint = tuple(_fingerprint(master_dir / name) for name in
                        ("ProduceCardGrowEffect.yaml", "ProduceCardStatusEnchant.yaml",
                         "ProduceExamTrigger.yaml", "ProduceCardCustomize.yaml"))
    with _CUSTOMIZATION_LOCK:
        if _CUSTOMIZATION_VERSIONS.get(master_dir) != fingerprint:
            _runtime_master_indexes.cache_clear()
            _CUSTOMIZATION_VERSIONS[master_dir] = fingerprint
        return _runtime_master_indexes(master_dir)


def available_card_customizations(row: Mapping[str, Any], *, database=DEFAULT_DATABASE,
                                  master_dir=DEFAULT_MASTER_DIR) -> tuple[dict[str, Any], ...]:
    """Next concrete Master option per slot; native UI still owns eligibility."""
    value = normalize_deck([row])[0]
    raw = _raw_card(value["card_id"], value["upgrade"], Path(database), _fingerprint(Path(database)))
    ids = raw.get("produceCardCustomizeIds", ())
    counts = dict(card_customizations(value))
    limit = raw.get("maxCustomizeCount")
    if limit is not None:
        if type(limit) is not int or limit < 0:
            raise DeckValueError("Master card customization limit is invalid")
        if sum(counts.values()) >= limit:
            return ()
    grow, _, _, custom = _customization_indexes(Path(master_dir))
    result = []
    for identity in ids:
        count = counts.get(identity, 0) + 1
        definition = custom.get(f"{identity}\0{count}")
        if not definition:
            continue
        effects = [grow[effect_id] for effect_id in definition.get("produceCardGrowEffectIds", ()) if effect_id in grow]
        if len(effects) != len(definition.get("produceCardGrowEffectIds", ())):
            continue
        result.append({**definition, "customize_id": identity, "customize_count": count,
                       "produce_points": definition.get("producePoint", 0), "grow_effects": effects})
    return tuple(result)


def with_card_customization(row: Mapping[str, Any], option: Mapping[str, Any]) -> dict[str, Any]:
    """Replace the selected slot's total level, never append its previous total."""
    result = dict(row)
    identity = option.get("customize_id", option.get("id"))
    count = _int(option.get("customize_count", option.get("customizeCount")), "customize option count")
    if not isinstance(identity, str) or not identity or count == 0:
        raise DeckValueError("customize option identity/count is invalid")
    slots = dict(card_customizations(row))
    if count != slots.get(identity, 0) + 1:
        raise DeckValueError("customize option must advance the actual slot by one")
    slots[identity] = count
    result["customizes"] = [{"id": key, "customizeCount": value} for key, value in slots.items()]
    observed = dict(row.get("customize_definitions", {}))
    observed[identity] = dict(option)
    result["customize_definitions"] = observed
    return result


# Card, drink and customization effects share one structural projection.
_Features = EffectFeatures
_GAINS = GAINS
_CONVERTERS = CONVERTERS
_COSTS = COSTS
def _effect(features: _Features, effect: MasterCardEffect, *, factor=1.0, depth=0, database=DEFAULT_DATABASE):
    project_effect(features, effect, database=database, factor=factor)


def _validated_added_effects(master: MasterCard, definition: Mapping[str, Any], grows, database: Path,
                             master_dir: Path) -> dict[str, MasterCardEffect]:
    """Use the native ordered customization parser for this exact Master slot.

    UI option projections can omit neutral fields. Their supplied semantic
    fields must still agree with the full Master rows; an unknown conditional
    rewrite is not made unconditional merely because it carries EffectAdd.
    """
    from .audition_local_save_state import CanonicalJsonValue, empty_local_save_exam_card_runtime_state
    from .plan3_native_state import _NEUTRAL_GROW_MASTER_FIELDS, parse_plan3_runtime_customization_ordered

    identity = definition.get("customize_id", definition.get("id"))
    count = definition.get("customize_count", definition.get("customizeCount"))
    raw = _raw_card(master.id, master.upgrade, database, _fingerprint(database))
    ids = raw.get("produceCardCustomizeIds", ())
    if identity not in ids or type(count) is not int or count <= 0:
        raise DeckValueError("EffectAdd has no exact native customization slot")
    grow_index, _, _, customize_index = _customization_indexes(master_dir)
    exact = customize_index.get(f"{identity}\0{count}")
    if exact is None or [row.get("id") for row in grows] != exact.get("produceCardGrowEffectIds"):
        raise DeckValueError("EffectAdd source grow order differs from Master")
    if ("overwriteProduceCardGrowEffectType" in definition and
            definition["overwriteProduceCardGrowEffectType"] != exact.get("overwriteProduceCardGrowEffectType")):
        raise DeckValueError("EffectAdd display family differs from Master")
    allowed_fields = set(_NEUTRAL_GROW_MASTER_FIELDS) | {"id", "effectType", "value", "targetPlayEffectProduceExamTriggerIdList"}
    for observed in grows:
        actual = grow_index[observed["id"]]
        if set(actual) - allowed_fields or set(observed) - allowed_fields:
            raise DeckValueError("EffectAdd has an unknown grow field")
        if any(key in actual and actual[key] != value for key, value in observed.items()):
            raise DeckValueError("EffectAdd observed semantic fields differ from Master")
        # The shared parser recognizes the legacy singular-List spelling too.
        # Reject the current plural-Ids target field before that parser so a
        # target rewrite cannot be admitted as an ordinary appended effect.
        if actual.get("targetPlayEffectProduceExamTriggerIds"):
            raise DeckValueError("EffectAdd conditional target rewrite is unsupported")
    counts = [count if value == identity else 0 for value in ids]
    runtime = replace(empty_local_save_exam_card_runtime_state(),
                      customize_count_list=CanonicalJsonValue.from_value(counts, "deck-value-customization"))
    parsed = parse_plan3_runtime_customization_ordered(master.id, master.upgrade, runtime,
        database=database, master_dir=master_dir, allowed_types=frozenset({"ProduceCardGrowEffectType_EffectAdd"}))
    return {entry.grow_effect_id: load_master_effect(entry.added_effect.id, database)
            for entry in parsed.effects if entry.added_effect is not None}


def _grow(features: _Features, grow: Mapping[str, Any], master: MasterCard, database: Path,
          *, added_effect: MasterCardEffect | None = None):
    kind = str(grow.get("effectType", "")).removeprefix("ProduceCardGrowEffectType_")
    value = _number(grow.get("value", 0), "grow value")
    previous_unknown = set(features.unknown)
    # Scalar grow restrictions are the same neutral-field contract used by the
    # native customization compiler; unsupported conditional rewrites are named.
    from .plan3_native_state import _NEUTRAL_GROW_MASTER_FIELDS
    if kind == "EffectAdd" and added_effect is None:
        features.unknown.add(f"grow-added-effect-not-validated:{grow.get('id')}")
        return
    conditional = any(grow.get(key, expected) != expected for key, expected in _NEUTRAL_GROW_MASTER_FIELDS.items()
                      if not (kind == "EffectAdd" and key in {"playProduceExamEffectId", "effectGroupIds"}))
    if conditional:
        features.unknown.add(f"conditional-grow:{grow.get('id', kind)}")
    elif kind == "LessonAdd":
        features.direct += value * features.hits
    elif kind == "LessonCountAdd":
        direct_effects = [effect for effect in master.effects if effect.effect_type == "ProduceExamEffectType_ExamLesson"]
        features.direct += sum(effect.value1 for effect in direct_effects) * value
        features.hits += len(direct_effects) * value
        for effect in master.effects:
            resource = _CONVERTERS.get(effect.effect_type.removeprefix("ProduceExamEffectType_"))
            if resource:
                features.converters[resource] += effect.value1 / 1000 * value
    elif kind in {"ReviewAdd", "BlockAdd", "AggressiveAdd", "FullPowerPointAdd"}:
        resource = {"ReviewAdd": "review", "BlockAdd": "block", "AggressiveAdd": "motivation", "FullPowerPointAdd": "full_power"}[kind]
        if features.gains[resource] > 0:
            features.gains[resource] += value
        else:
            features.unknown.add(f"grow-target-not-resolved:{grow.get('id', kind)}")
    elif kind == "LessonDependExamReviewAdd":
        features.converters["review"] += value / 1000
    elif kind in {"CostParameterBuffReduce", "CostLessonBuffReduce"}:
        # Same cost-type contract as the native Plan1 customization compiler.
        expected = {"CostParameterBuffReduce": "ExamParameterBuff", "CostLessonBuffReduce": "ExamLessonBuff"}[kind]
        if master.cost_type.removeprefix("ExamCostType_") != expected:
            features.unknown.add(f"grow-cost-type-mismatch:{grow.get('id', kind)}")
        else:
            resource = _COSTS[expected]
            features.spend[resource] = max(0, features.spend[resource] - value)
    elif kind in {"CostReduce", "CostAdd", "CostPenetrateReduce", "CostPenetrateAdd"}:
        delta = value * (-1 if kind.endswith("Reduce") else 1)
        resource = _COSTS.get(master.cost_type.removeprefix("ExamCostType_")) if "Penetrate" not in kind else None
        if resource:
            features.spend[resource] = max(0, features.spend[resource] + delta)
        elif "Penetrate" in kind:
            features.force_stamina = max(0, features.force_stamina + delta)
        else:
            features.stamina = max(0, features.stamina + delta)
    elif kind == "InitialAdd":
        features.initial = True
    elif kind == "EffectAdd":
        _effect(features, added_effect, database=database)
    else:
        features.unknown.add(f"grow:{grow.get('id', kind)}:{kind}")
    features.terms.append({"grow_effect_id": grow.get("id"), "grow_effect_type": kind,
        "value": value, "modeled": features.unknown == previous_unknown,
        "unresolved": sorted(features.unknown - previous_unknown)})


def _features(row: Mapping[str, Any], database: Path, master_dir: Path) -> _Features:
    result = _Features()
    try:
        card = _master(row["card_id"], row["upgrade"], database, _fingerprint(database))
    except (KeyError, ValueError, sqlite3.Error) as error:
        result.unknown.add(f"master-card-missing:{row['card_id']}+{row['upgrade']}:{type(error).__name__}")
        result.residual_prior = .03 * _number(row.get("evaluation", 0), "native evaluation")
        return result
    project_master_card(result,card,database=database)
    customs = card_customizations(row)
    if customs:
        indexes = None
        for identity, count in customs:
            observed = row.get("customize_definitions", {}).get(identity)
            if observed and observed.get("customize_count", observed.get("customizeCount")) == count:
                definition, grows = observed, observed.get("grow_effects", ())
            else:
                try:
                    indexes = indexes or _customization_indexes(master_dir)
                    definition = indexes[3].get(f"{identity}\0{count}")
                    grows = [indexes[0].get(grow_id) for grow_id in definition.get("produceCardGrowEffectIds", ())] if definition else []
                except (ValueError, FileNotFoundError):
                    definition, grows = None, []
            if not definition or not grows or any(grow is None for grow in grows):
                result.unknown.add(f"customize-master-missing:{identity}+{count}")
                continue
            added_effects = {}
            if any(grow.get("effectType") == "ProduceCardGrowEffectType_EffectAdd" for grow in grows):
                try:
                    added_effects = _validated_added_effects(card, definition, grows, database, master_dir)
                except (KeyError, ValueError, OSError, sqlite3.Error) as error:
                    result.unknown.add(f"customize-effect-add-unresolved:{identity}:{error}")
                    continue
            overwrite = definition.get("overwriteProduceCardGrowEffectType", "ProduceCardGrowEffectType_Unknown")
            if overwrite not in {"", "ProduceCardGrowEffectType_Unknown", 0} and not added_effects:
                result.unknown.add(f"customize-overwrite:{identity}:{overwrite}")
                continue
            for grow in grows:
                _grow(result, grow, card, database, added_effect=added_effects.get(grow.get("id")))
    # A small explicit residual prior for unmodelled mechanics avoids declaring
    # a new card worthless, but never replaces whole-deck resource interaction.
    if result.unknown:
        result.residual_prior = .03 * card.evaluation
    return result


@lru_cache(maxsize=8)
def _drink_catalog(database: Path, fingerprint):
    from .drink_catalog import load_drink_catalog
    return load_drink_catalog(database)


def _drink_features(identity: str, database: Path) -> _Features:
    """One inventory use, never a card or an extra draw-pool entry."""
    result = _Features()
    drink = _drink_catalog(database, _fingerprint(database)).get_drink(identity)
    for ref in drink.effect_refs:
        effect = ref.effect
        if effect.source_kind != "exam":
            result.unknown.add(f"drink-outer-effect:{identity}:{effect.source_id}")
            continue
        _effect(result, MasterCardEffect(effect.source_id, effect.effect_type, effect.effect_value1,
            effect.effect_value2, effect.effect_count, effect.effect_turn, effect.produce_exam_status_enchant_id,
            effect.chain_produce_exam_effect_id, trigger_id=effect.produce_exam_trigger_id), database=database)
    return result


def context_from_native(raw: Mapping[str, Any]) -> dict[str, Any]:
    result = {}
    for name in ("progress", "state", "card_value_context"):
        value = raw.get(name)
        if isinstance(value, Mapping):
            result.update(value)
    for name in ("next_exam", "next_audition", "exam_outlooks", "targets", "flow", "style", "produce_id", "idol_card_id",
                 "scenario", "difficulty", "mode", "mode_profile", "rules", "rule_version", "plan_type", "exam_effect_type"):
        if name in raw:
            result[name] = raw[name]
    return result


def _horizon(context: Mapping[str, Any]) -> tuple[float, float, tuple[str, ...]]:
    exam = context.get("next_exam", context.get("next_audition", {}))
    exam = exam if isinstance(exam, Mapping) else {}
    turns = exam.get("turns", context.get("exam_turns"))
    if turns is None:
        # Explicit modelling prior, not a made-up observed exam duration.
        duration, source = 10.0, "unobserved exam turns: 10-turn ranking prior"
    else:
        duration = max(1., min(100., _number(turns, "next exam turns")))
        source = f"observed next-exam horizon {duration:g} turns"
    weeks = exam.get("weeks_until", context.get("next_audition_in_weeks"))
    proximity = 1.0 if weeks is None else 1.0 / (1.0 + .08 * max(0., _number(weeks, "weeks until exam")))
    return duration, proximity, (source,)


def _permanent_deck_contexts(context: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    """A permanent change survives each remaining Exam; consumables do not.

    Outlooks are supplied by the mode profile. Each stage has one difficulty,
    and every evaluation keeps the same observed deck/resources. No future
    card acquisition, resource growth or qualification is assumed.
    """
    outlooks = context.get("exam_outlooks")
    if outlooks is None:
        return (context,)
    if not isinstance(outlooks, (list, tuple)) or not outlooks:
        raise DeckValueError("remaining exam outlooks must be a nonempty array")
    stages, result = set(), []
    for exam in outlooks:
        if not isinstance(exam, Mapping):
            raise DeckValueError("remaining exam outlook must be structured")
        stage = exam.get("stage")
        if not isinstance(stage, str) or not stage or stage in stages:
            raise DeckValueError("remaining exam outlook must identify one unique stage")
        stages.add(stage)
        if _number(exam.get("turns"), "remaining exam turns") <= 0:
            raise DeckValueError("remaining exam turns must be positive")
        if _number(exam.get("weeks_until"), "remaining exam distance", -1) < 0:
            raise DeckValueError("remaining exam must have a nonnegative week distance")
        result.append({**context, "next_exam": dict(exam)})
    return tuple(result)


def _materialized_features(deck, context, database, master_dir, drink_ids=()):
    raw = [_features(row, database, master_dir) for row in deck]
    drinks = [_drink_features(identity, database) for identity in drink_ids]
    horizon, _, _ = _horizon(context)
    n = max(1, len(raw));shares = Counter(x.category for x in raw)
    baseline_uses = horizon / n
    stocks = Counter()
    for feature in raw:
        for resource, gain in feature.gains.items():stocks[resource] += .5 * gain * min(1. if feature.lost else 3., baseline_uses)
    for feature in drinks:
        for resource, gain in feature.gains.items():stocks[resource] += .5 * gain
    environment = {"stocks": stocks, "category_shares": {k: v / n for k, v in shares.items()},"deck_count":n,
        "plays_per_turn": 1 + min(.75, sum(x.extra_play for x in raw) / n)}
    features = []
    for f in raw:
        # A Lost installer cannot trigger itself after leaving the hand. Its
        # category is removed from the future-event opportunity pool.
        future_shares = shares.copy()
        if f.lost:future_shares[f.category] -= 1
        total_future = max(1, n - int(f.lost))
        local = {**environment, "category_shares": {k: v / total_future for k, v in future_shares.items()},
            "source_uses": min(1. if f.lost else 3., baseline_uses)}
        features.append(materialize(f, database=database, environment=local, horizon=horizon))
    consumables = [materialize(f, database=database, environment=environment, horizon=horizon,
        available=max(0, horizon - 1)) for f in drinks]
    return features, consumables


@lru_cache(maxsize=32)
def _exam_rates(database, db_stamp, master_dir, setting_stamp, produce_id):
    # Rates are Master settings; only their average exposure is estimated.
    # Context-free unit tests retain an explicitly named generic prior.
    result = {"condition": .5, "condition_per_turn": .1, "stamina_down": .5, "stamina_up": 1.,
        "source": "unbound-standard-settings-prior"}
    try:
        with sqlite3.connect("file:" + database.resolve().as_posix() + "?mode=ro", uri=True) as connection:
            row = connection.execute("SELECT raw_json FROM produce_mode WHERE id=?", (produce_id,)).fetchone()
        if row is None:return result
        identity = json.loads(row[0])["examSettingId"]
        settings = yaml.load((master_dir / "ExamSetting.yaml").read_text(encoding="utf-8"), Loader=yaml.CSafeLoader)
        actual = next(r for r in settings if r["id"] == identity)
        return {"condition": actual["examParameterBuffPermil"] / 1000 - 1,
            "condition_per_turn": actual["examParameterBuffMultiplePerTurnPermil"] / 1000,
            "stamina_down": actual["examStaminaConsumptionDownPermil"] / 1000,
            "stamina_up": actual["examStaminaConsumptionAddPermil"] / 1000,
            "source": f"Master produce_mode/{produce_id}->ExamSetting/{identity}"}
    except (OSError, ValueError, KeyError, StopIteration, sqlite3.Error):
        return result


def _value(deck, context, database, master_dir, drink_ids=()):
    features, consumables = _materialized_features(deck, context, database, master_dir, drink_ids)
    unknown = set().union(*(item.unknown for item in (*features, *consumables))) if features or consumables else set()
    horizon, proximity, horizon_notes = _horizon(context)
    rates = _exam_rates(database, _fingerprint(database), master_dir,
        _fingerprint(master_dir / "ExamSetting.yaml"), context.get("produce_id", context.get("produceId", "")))
    count = len(features)
    if not count:
        return 0., {}, unknown, horizon_notes
    # Finite play budget shared by all cards. Adding a card changes every
    # existing card's access and expected use, so duplicate/dilution is joint.
    represented_count=count+sum(item.generated_slots for item in features)
    extra = sum(item.extra_play for item in features) / represented_count
    horizon += min(horizon * .3, sum(item.extra_turn for item in (*features, *consumables)))
    budget = horizon * (1 + min(.75, extra)) + sum(item.extra_play for item in consumables)
    draws = 3 + 2 * max(0, horizon - 1) + min(horizon, sum(item.draw for item in (*features, *consumables)))
    access = min(1., draws / represented_count)
    use = [min(1. if item.lost else 3., budget / represented_count) * (1 if item.initial else access) for item in features]
    gains, spend, converters, amplify = Counter(), Counter(), Counter(), Counter()
    direct = hits = stamina = force_stamina = recovery = prior = direct_stamina_damage = 0.
    installation_stamina = installation_force = 0.
    multiple = condition_multiple = down_turns = up_turns = free_plays = upgrades = fixed_reduction = 0.
    for item, plays in (*zip(features, use), *((item, 1.) for item in consumables)):
        gains.update({key: value * plays for key, value in item.gains.items()})
        spend.update({key: value * plays for key, value in item.spend.items()})
        converters.update({key: value * plays for key, value in item.converters.items()})
        amplify.update({key: value * plays for key, value in item.amplify.items()})
        direct += item.direct * plays
        hits += item.hits * plays
        stamina += item.stamina * plays
        force_stamina += item.force_stamina * plays
        direct_stamina_damage += item.direct_stamina_damage * plays
        recovery += item.recovery * plays
        prior += item.residual_prior * plays
        multiple += item.lesson_multiple_turns * plays
        condition_multiple += item.condition_multiple_turns * plays
        down_turns += item.stamina_down_turns * plays
        up_turns += item.stamina_up_turns * plays
        free_plays += item.free_plays * plays
        upgrades += item.hand_upgrades * plays
        fixed_reduction += item.fixed_stamina_reduction * plays
        if item.fixed_stamina_reduction:
            # The installer pays before installing its new reduction. Excluding
            # all of its own estimated costs is conservative for repeat uses.
            installation_stamina += item.stamina * plays
            installation_force += max(0.,item.force_stamina-item.direct_stamina_damage) * plays
    # Half-horizon resource availability is a deliberately bounded producer /
    # converter proxy. It is not an ordered simulator or a claimed exact score.
    stock = {key: .5 * max(0., value - spend[key]) * (1 + min(2., amplify[key] * .35))
             for key, value in gains.items()}
    stock["block"] = stock.get("block", 0) + converters["motivation_to_block"] * stock.get("motivation", 0) * .5
    stock["review"] = stock.get("review", 0) + converters["block_to_review"] * stock.get("block", 0) * .5
    conversion = sum(value * stock.get(key, 0) for key, value in converters.items()
                     if key not in {"motivation_to_block", "block_to_review"})
    conversion += stock.get("concentration", 0) * hits
    condition = min(1., gains["condition"] / max(1, horizon))
    score_proxy = (direct + conversion) * (1 + rates["condition"] * condition +
        rates["condition_per_turn"] * stock.get("condition", 0) * condition * min(1., condition_multiple / horizon))
    score_proxy += stock.get("review", 0) * horizon * .6
    # Full-power supply has value through the deck's attack production, not a
    # fixed standalone card weight. Exact stance timing remains model territory.
    score_proxy += direct * .35 * (1 - math.exp(-max(0, gains["full_power"] - spend["full_power"]) / 10.))
    score_proxy += direct * .01 * stock.get("enthusiasm", 0)
    score_proxy *= 1 + min(2., multiple / max(1., horizon))
    # Both duration status benefits and costs retain their actual payload.
    # Exposure is the declared average-turn estimate, not claimed timing.
    stamina_before_modifiers=stamina
    stamina *= max(0., 1 - rates["stamina_down"] * min(1., down_turns / horizon))
    stamina *= 1 + rates["stamina_up"] * min(1., up_turns / horizon)
    stamina = max(0., stamina - min(free_plays, budget) * stamina / max(1., budget))
    # Fixed reduction follows the percentage/card-cost stage. Its benefit is
    # capped by payable costs and does not create stamina or reduce resource costs.
    protected_normal=installation_stamina*stamina/max(1.,stamina_before_modifiers)
    fixed_force_saved=min(max(0.,force_stamina-direct_stamina_damage-installation_force),fixed_reduction)
    force_stamina-=fixed_force_saved
    fixed_normal_saved=min(max(0.,stamina-protected_normal),max(0.,fixed_reduction-fixed_force_saved))
    stamina-=fixed_normal_saved
    # Hand-all upgrade values actual available Master upgrade deltas, rather
    # than a fixed rarity bonus. It cannot upgrade already upgraded cards.
    if upgrades:
        upgrade_gain = 0.
        for row, feature in zip(deck, features):
            if row["upgrade"] != 0:continue
            try:
                raised = _features({**row, "upgrade": 1, "upgradeCount": 1}, database, master_dir)
                realized = materialize(raised, database=database,
                    environment={"stocks": stock, "category_shares": {}, "plays_per_turn": budget / horizon}, horizon=horizon)
                gain = max(0., realized.direct - feature.direct) + sum(max(0., realized.gains[k] - feature.gains[k]) *
                    (hits / max(1., budget) if k == "concentration" else .5) for k in realized.gains)
                upgrade_gain += gain
            except (ValueError, KeyError):continue
        score_proxy += min(1., upgrades * min(3, count) / count) * upgrade_gain
    max_stamina = max(1., _number(context.get("max_stamina", context.get("maxStamina")), "max stamina", 30))
    current = max(0., _number(context.get("stamina"), "stamina", max_stamina))
    scarcity = 1 + 3 * (1 - min(1., current / max_stamina))
    shield_used = min(stamina, gains["block"])
    uncovered = max(0., stamina - shield_used + force_stamina - recovery)
    affordability = min(1., (current + recovery + shield_used) / max(1., stamina + force_stamina))
    deficits = sum(max(0., value - gains[key]) for key, value in spend.items())
    exam = context.get("next_exam", context.get("next_audition", {}))
    exam = exam if isinstance(exam, Mapping) else {}
    targets = exam.get("targets", exam.get("attribute_targets", {}))
    weights = exam.get("attribute_weights", {})
    targets = targets if isinstance(targets, Mapping) else {}
    weights = weights if isinstance(weights, Mapping) else {}
    gaps = []
    for axis in ("vocal", "dance", "visual"):
        target = _number(targets.get(axis), f"next exam {axis} target")
        observed = context.get(axis)
        if target > 0 and observed is not None:
            gap = max(0., 1 - _number(observed, axis) / target)
            gaps.append((gap, max(0., _number(weights.get(axis), f"{axis} exam weight", 1))))
    # A deficit does not invent attribute gain from a skill card. It increases
    # the relative urgency of the deck's exam production against scarce costs.
    urgency = 1 + .5 * sum(gap * weight for gap, weight in gaps) / max(1., sum(weight for _, weight in gaps))
    utility = proximity * ((score_proxy + prior) * urgency * (.4 + .6 * affordability) - 2 * deficits - scarcity * uncovered)
    detail = {"deck_count": count, "estimated_generated_card_share":represented_count-count,
              "estimated_play_budget":budget,"estimated_play_uses_including_generated":sum(p*(1+f.generated_slots) for f,p in zip(features,use)),
              "fixed_stamina_cost_saved":fixed_force_saved+fixed_normal_saved,"access": access, "resource_supply": dict(gains),
              "resource_demand": dict(spend), "resource_conversion": conversion,
              "stamina_uncovered": uncovered, "exam_target_urgency": urgency,
              "effect_settings": rates,
              "card_features": [{"card_id": row["card_id"], "upgrade": row["upgrade"],
                  "terms": feature.terms, "unresolved": sorted(feature.unknown)} for row, feature in zip(deck, features)],
              "drink_features": [{"drink_id": identity, "terms": f.terms, "unresolved": sorted(f.unknown)}
                  for identity, f in zip(drink_ids, consumables)],
              "timing_assumptions": sorted({x for f in (*features, *consumables) for x in f.assumptions})}
    if gaps:
        horizon_notes += (f"next-exam observed attribute gaps: utility urgency {urgency:.3f}",)
    return utility, detail, unknown, horizon_notes


def evaluate_deck_delta(before_deck, after_deck, context, *, database=DEFAULT_DATABASE,
                        model_value: Callable | None = None) -> DeckValueDelta:
    """Compare two complete decks in one context; no game I/O or state mutation.

    ``produce_point_cost``/``stamina_cost`` belong to this mutation only. Outer
    event policies that already price those costs must not pass them again.
    ``model_value(deck, context)`` may return a finite number in caller-defined
    utility units; use the same oracle/context for both counterfactual decks.
    """
    if not isinstance(context, Mapping):
        raise DeckValueError("deck context must be an object")
    before, after = normalize_deck(before_deck), normalize_deck(after_deck)
    context = dict(context)
    digest = hashlib.sha256(json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    method = "semantic-deck-horizon-v1"
    mutation_unknown, shared_unknown, term_evidence = set(), set(), []
    if model_value is not None:
        base = _number(model_value(before, context), "model before value")
        updated = _number(model_value(after, context), "model after value")
        notes, unknown = ["same caller-bound deck model and context for both counterfactuals"], set()
        method = "caller-bound-deck-model"
    else:
        database, master_dir = Path(database), Path(context.get("master_dir", DEFAULT_MASTER_DIR))
        contexts = _permanent_deck_contexts(context)
        base, updated, unknown = 0.0, 0.0, set()
        notes = [f"complete deck {len(before)} -> {len(after)} cards; shared draw/play budget"]
        resource_changed, cost_changed = False, False
        for outlook in contexts:
            prior, old, unknown1, horizon_notes = _value(before, outlook, database, master_dir)
            following, new, unknown2, _ = _value(after, outlook, database, master_dir)
            old_counts = Counter(issue for card in old.get("card_features", ()) for issue in card["unresolved"])
            new_counts = Counter(issue for card in new.get("card_features", ()) for issue in card["unresolved"])
            mutation_unknown.update(issue for issue in old_counts.keys() | new_counts.keys()
                if old_counts[issue] != new_counts[issue])
            shared_unknown.update(issue for issue in old_counts.keys() & new_counts.keys()
                if old_counts[issue] == new_counts[issue])
            term_evidence.append({"stage": (outlook.get("next_exam") or {}).get("stage"),
                "before": old.get("card_features", []), "after": new.get("card_features", []),
                "effect_settings": old.get("effect_settings"),
                "timing_assumptions": sorted(set(old.get("timing_assumptions", []) + new.get("timing_assumptions", [])))})
            base += prior
            updated += following
            unknown.update(unknown1 | unknown2)
            notes.extend(horizon_notes)
            resource_changed |= (old.get("resource_supply") != new.get("resource_supply") or
                                 old.get("resource_demand") != new.get("resource_demand"))
            cost_changed |= old.get("stamina_uncovered") != new.get("stamina_uncovered")
            if len(contexts) > 1:
                exam = outlook["next_exam"]
                notes.append(f"remaining {exam['stage']} difficulty {exam.get('number')}: deck utility delta {following-prior:.3f}")
        if len(contexts) > 1:
            method = "semantic-remaining-exams-v1"
            notes.append("persistent deck change across remaining Exams; current observed resources; no future draws or growth forecast")
        if resource_changed:
            notes.append("joint resource production, spending and conversion changed")
        if cost_changed:
            notes.append("card costs evaluated against current stamina, recovery and block supply")
        notes.append("semantic finite-horizon ranking proxy; not a predicted exam/run score")
    points = max(0., _number(context.get("produce_point_cost"), "produce point cost"))
    stamina_cost = max(0., _number(context.get("stamina_cost"), "mutation stamina cost"))
    wallet = max(0., _number(context.get("produce_points", context.get("producePoint")), "produce points", points))
    reserve = max(0., _number(context.get("produce_point_reserve"), "produce point reserve", 40))
    spending_context = context.get("produce_point_spending_context")
    cost = produce_point_opportunity_cost(points, wallet, reserve, spending_context)
    cost += stamina_cost * (1 + 10 / max(1., _number(context.get("stamina"), "stamina", 30)))
    if cost:
        updated -= cost
    if points or stamina_cost:
        notes.append(f"actual mutation cost: PT {points:g}, stamina {stamina_cost:g}; opportunity utility {cost:.3f}")
    if points and isinstance(spending_context, Mapping) and spending_context.get("terminal_reserve_eligible") is True:
        notes.append("last observed activity choice: charge reserve consumption; terminal PT conversion remains unknown")
    if unknown:
        notes.append("unresolved effect contributions are unknown; utility is partial and not a complete expected benefit")
    if not all(math.isfinite(value) for value in (base, updated)):
        raise DeckValueError("deck utility is not finite")
    progress = None
    if context.get("deck_plan") is not None:
        # Local import avoids a cycle: DeckPlan reuses the customization parser.
        from .deck_plan import DeckPlan, evaluate_plan_progress

        try:
            plan = DeckPlan.from_dict(context["deck_plan"])
            if (plan.scope != tuple(context.get(key) for key in ("produce_id", "plan_type", "exam_effect_type"))
                    or plan.idol_card_id != context.get("idol_card_id")):
                raise ValueError("adopted-plan-context-mismatch")
            progress = evaluate_plan_progress(before, after, plan,
                stage=(context.get("next_exam") or {}).get("stage")).to_dict()
            notes.append("same adopted plan; structural progress ranked separately from net utility")
        except (ValueError, TypeError, KeyError) as error:
            progress = {"status": "unavailable", "reason": str(error), "value": 0.0}
    raw_delta = updated - base
    destructive_or_paid = len(after) < len(before) or points > 0 or stamina_cost > 0
    comparison_ready = not mutation_unknown
    # A partial projection is not evidence that deleting an unresolved benefit
    # or paying for an unresolved mutation creates positive utility.
    value = min(0., raw_delta) if destructive_or_paid and not comparison_ready else raw_delta
    valuation = {"schema": "gkms.outer-effect-valuation.v2", "unit": "heuristic-deck-utility",
        "predicts_exam_score": False, "comparison_ready": comparison_ready,
        "mutation_unresolved_effects": sorted(mutation_unknown), "shared_context_unresolved_effects": sorted(shared_unknown),
        "partial_utility_delta": raw_delta, "uncertain_destructive_gain_not_authorized": destructive_or_paid and not comparison_ready,
        "effect_evidence": term_evidence}
    if destructive_or_paid and not comparison_ready:
        notes.append("positive deletion/PT gain cannot be inferred from missing effect benefits; candidate requires explicit fallback")
    return DeckValueDelta(value, base, updated, tuple(notes), tuple(sorted(unknown)), digest, method,
                          plan_progress=progress, valuation=valuation)


def evaluate_drink_inventory_delta(deck, before_drinks, added_drinks, context, *, database=DEFAULT_DATABASE) -> DeckValueDelta:
    """Value a consumable portfolio with fixed cards and actual capacity.

    If full, report the best possible retained portfolio, not an assertion that
    the game has already replaced a slot. A later native overwrite selector
    remains responsible for choosing/discarding that exact inventory instance.
    """
    if not isinstance(context, Mapping):
        raise DeckValueError("drink reward context must be an object")
    cards = normalize_deck(deck)
    for values in (before_drinks, added_drinks):
        if not isinstance(values, (list, tuple)) or any(not isinstance(value, str) or not value for value in values):
            raise DeckValueError("observed drink inventory and reward must be ID arrays")
    if not added_drinks:
        raise DeckValueError("drink reward is empty")
    capacity = _int(context.get("drink_capacity"), "observed drink capacity")
    if capacity <= 0 or len(before_drinks) > capacity:
        raise DeckValueError("current drink inventory exceeds its observed capacity")
    if capacity > 12 or len(added_drinks) > 8:
        raise DeckValueError("drink portfolio exceeds bounded recommendation capacity")
    database, master_dir = Path(database), Path(context.get("master_dir", DEFAULT_MASTER_DIR))
    before, detail, unknown, horizon = _value(cards, context, database, master_dir, before_drinks)
    all_ids = (*before_drinks, *added_drinks)
    indexes = tuple(range(len(all_ids)))
    if len(all_ids) > capacity and math.comb(len(all_ids), capacity) > 1024:
        raise DeckValueError("drink replacement alternatives exceed bounded recommendation search")
    subsets = (indexes,) if len(all_ids) <= capacity else combinations(indexes, capacity)
    best, retained, after_detail = -math.inf, (), {}
    portfolio_evidence = []
    owned_unresolved = Counter((row["drink_id"], issue) for row in detail.get("drink_features", []) for issue in row["unresolved"])
    best_key = None
    best_unresolved = Counter()
    for subset in subsets:
        value, info, gaps, _ = _value(cards, context, database, master_dir, [all_ids[index] for index in subset])
        unknown.update(gaps)
        retained_unresolved = Counter((row["drink_id"], issue) for row in info.get("drink_features", []) for issue in row["unresolved"])
        unresolved_discard = tuple(sorted((owned_unresolved - retained_unresolved).elements()))
        # Existing unknown benefits cannot become zero simply because another
        # bottle has an easily modeled effect. Preserve that choice as partial.
        key = (not unresolved_discard, value)
        portfolio_evidence.append({"retained_indexes": list(subset), "partial_utility": value,
            "discarded_unresolved_benefits": unresolved_discard, "effect_evidence": info.get("drink_features", [])})
        if best_key is None or key > best_key:
            best_key = key
            best_unresolved = retained_unresolved
            best, retained, after_detail = value, tuple(subset), info
    payload = {**context, "drink_inventory_before": list(before_drinks), "reward_drinks": list(added_drinks)}
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    notes = [f"fixed actual deck {len(cards)} cards; drinks are one-use resources, not draw-pool entries", *horizon,
             f"observed drink slots {len(before_drinks)}/{capacity}; reward count {len(added_drinks)}",
             "joint consumable supplies and deck converters; existing inventory included",
             "semantic ranking utility, not predicted exam score or exact drink-use timing"]
    if len(all_ids) > capacity:
        notes.append("full capacity: potential best retention requires a later native replacement choice")
        notes.append("projected retained inventory indexes: " + ",".join(map(str, retained)))
    if unknown:
        notes.append("unscored drink/deck mechanics are explicitly listed")
    mutation_unresolved = sorted(identity for identity in owned_unresolved.keys() | best_unresolved.keys()
        if owned_unresolved[identity] != best_unresolved[identity])
    comparison_ready = not mutation_unresolved
    partial_delta = best - before
    paid_unresolved = not comparison_ready and context.get("produce_point_cost", 0) > 0
    value = min(0., partial_delta) if paid_unresolved else partial_delta
    return DeckValueDelta(value, before, best, tuple(notes), tuple(sorted(unknown)), digest,
        "semantic-drink-portfolio-v1", retained_indexes=retained,
        valuation={"schema": "gkms.outer-effect-valuation.v2", "unit": "heuristic-deck-utility", "predicts_exam_score": False,
            "comparison_ready": comparison_ready, "partial_utility_delta": partial_delta,
            "mutation_unresolved_effects": mutation_unresolved,
            "uncertain_purchase_gain_not_authorized": paid_unresolved,
            "portfolio_evidence": portfolio_evidence, "unresolved_owned_benefit_preserved": bool(owned_unresolved),
            "effect_evidence": after_detail.get("drink_features", []), "effect_settings": after_detail.get("effect_settings")})
