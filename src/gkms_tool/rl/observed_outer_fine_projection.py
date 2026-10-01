"""Observed fine OUTER data -> the shared semantic entity representation.

No action consequence is predicted here. Only the current recorded observation,
recorded earlier history, and complete currently legal controls are inputs.
Native pointers/GUIDs, policy rankings, future states and return labels are not
features. Static definitions reuse the existing source-qualified host codec.
"""
from __future__ import annotations

from collections.abc import Mapping
from collections import OrderedDict
from copy import deepcopy
from dataclasses import asdict
import hashlib
import importlib.util
import json
from pathlib import Path
import re

from .contracts import ContractError, DecisionKind, FrozenJSON, Scope, State, digest
from .semantic_entity_encoding import ENTITY_FEATURE_NAMES, encode_semantic_tokens
from .native_information_view import public_semantic_tokens
from .observed_outer_projection import (ACTION_IDS, OUTER_TYPES, OUTER_ZONES, OUTER_OBJECTIVE,
    RESOURCE_MAP, observed_context_tokens, schedule_choice_tokens)
from ..native_structure_features import _emit

VERSION = "gkms.rl.observed-outer-fine-projection.v1"
FINE_IMPLEMENTATION_MODULES = (
    "gkms_tool.rl.observed_outer_fine_projection", "gkms_tool.rl.observed_outer_projection",
    "gkms_tool.rl.semantic_entity_encoding", "gkms_tool.native_structure_features",
    "gkms_tool.card_semantic_features", "gkms_tool.card_data_update", "gkms_tool.il2cpp_metadata",
    "gkms_tool.runtime_optional_parent_features", "gkms_tool.runtime_master_source",
    "gkms_tool.native_structure_contract_set", "gkms_tool.shared_bc_features",
    "gkms_tool.live_pc_consumer_contract", "gkms_tool.artifact_source_resolver",
    "gkms_tool.training_artifact_io", "gkms_tool.qualification_verification",
    "gkms_tool.portable_outer_assets", "gkms_tool.nia_outer_resource_model",
    "gkms_tool.runtime_outer_policy", "gkms_tool.lesson_stamina_quote",
    "gkms_tool.route_calendar", "gkms_tool.runtime_mode_profile",
    "gkms_tool.rl.native_information_view",
    "gkms_tool.rl.observed_initial_loadout", "gkms_tool.passive_catalog",
    "gkms_tool.outer_behavior_candidate", "gkms_tool.nia_route_profile",
    "gkms_tool.nia_static_adapter",
    "gkms_tool.deck_plan", "gkms_tool.deck_value",
)
SELECTION_SEMANTICS = "recorded-native-legal-controls; no-pool-invention"
STATE_FIELDS = ("week", "step_type", "progress_status", "stamina", "max_stamina", "produce_points",
                "vocal", "dance", "visual", "vote_count", "star", "star_permil", "in_progress")
PROGRESS_FIELDS = (
    "vocalGrowthRatePermil", "danceGrowthRatePermil", "visualGrowthRatePermil",
    "lessonVocalSpChangeRatePermil", "lessonDanceSpChangeRatePermil", "lessonVisualSpChangeRatePermil",
    "selfLessonTypeStaminaPermils", "produceDrinkPossessLimit", "producePointLimit",
    "staminaRecoverValueRatePermil", "staminaRecoverDisableTurn", "eventActivityProducePointPermil",
    "auditionEffectParameterBonusPermil", "eventBusinessVoteCountPermil",
    "produceCardRemainSelectRerollCount", "produceCardCustomizeCount", "produceCardCustomizeDiscountPermil",
)
UI_FIELDS = ("family", "phase", "selection_type", "minimum", "maximum", "selected_count", "limit_count",
             "remaining_customize_count", "remaining_new_card_slots", "selected_card_remaining_count",
             "selected_card_customizations_remaining", "selected_card_customization_available",
             "selecting_card", "selecting_customize", "valid_count", "keeps_new_drink")
_LOCAL = re.compile(r"(?:instance|fingerprint|session|generation|revision|guid|userMemory|imagePath|assetId|skin|callback|source_ref|sha256)", re.I)
_GUID = re.compile(r"^(?:0x[0-9a-f]+(?:[:][0-9]+)?|[0-9a-f]{8}-[0-9a-f-]{27,})$", re.I)
_DISPLAY = {"name", "text", "produceDescriptions", "customizeProduceDescriptions", "evaluation",
            "ranking_score", "score", "source", "reason", "source_policy", "policy", "grade", "power",
            "index", "list_index", "deck_number", "deckNumber", "localNumber", "local_number",
            "number", "business_number", "selected_index"}
_TARGET_FIELDS = {"action_id", "resource_type", "upgrade", "customize_count", "price", "quantity",
                  "step_type", "business_type", "is_new", "selected", "selected_before",
                  "free_count", "remaining_count"}
_CARD_FIELDS = {"upgradeCount", "upgrade", "customizeCountList", "customize_counts", "deleted", "isDeleted",
                "originType", "customizing", "current_customize_count", "max_customize_count",
                "card_customizations_remaining", "customization_available", "can_customize", "lack_cost",
                "restricted", "selected", "realized", "quantity"}


def fine_implementation_references():
    """Exact narrow helper closure, separate from checkpoint numeric core pins."""
    result = {}
    for name in FINE_IMPLEMENTATION_MODULES:
        spec = importlib.util.find_spec(name)
        if spec is None or spec.origin is None: raise ContractError("fine projection dependency is missing: " + name)
        path = Path(spec.origin).resolve()
        raw = path.read_bytes()
        result[name] = {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
    return result


class FineOuterSemanticSource:
    """One existing semantic package plus its recorded outer Master tables.

    Supplemental tables are verified lazily once, indexed once, and watched on
    reuse. This reads definitions and actual counters; it never evaluates an
    effect, invents its future pool, or loads a model/checkpoint.
    """
    def __init__(self, features, outer_manifest_reference, *, validate_materials=None):
        from ..qualification_verification import source_stamp
        self.features, self.package, self.payload = features, features.package, features.payload
        self.reference = deepcopy(outer_manifest_reference)
        manifest = Path(self.reference["path"]).resolve()
        raw = manifest.read_bytes()
        if hashlib.sha256(raw).hexdigest() != self.reference["sha256"]:
            raise ContractError("observed outer material manifest SHA differs")
        self.manifest = json.loads(raw)
        if self.manifest.get("master_hash") != features.package.contract["source_master_hash"]:
            raise ContractError("outer tables and semantic package use different Masters")
        self.root, self.master_dir = manifest.parent, manifest.parent / "Master"
        self._watches, self._tables = {manifest: source_stamp(manifest)}, {}
        self.implementation = fine_implementation_references()
        for reference in self.implementation.values():
            self._watches[Path(reference["path"])] = source_stamp(reference["path"])
        self._graphs = OrderedDict()
        self._validate_materials = validate_materials

    def validate_unchanged(self):
        from ..qualification_verification import source_stamp
        if self._validate_materials is not None:
            self._validate_materials()
        if any(source_stamp(path) != stamp for path, stamp in self._watches.items()):
            raise ContractError("observed outer material source changed")

    def qualified_source_paths(self):
        """Consumed material closure for one-time signed feature indexing."""
        self.validate_unchanged()
        return tuple(dict.fromkeys((*self._watches, *getattr(self, "_additional_source_paths", ()))))

    def _table_rows(self, name):
        from ..qualification_verification import source_stamp
        from ..portable_outer_assets import _verified_file
        from ..nia_outer_resource_model import _master_rows
        if name not in self._tables:
            self.validate_unchanged()
            reference = self.manifest.get("tables", {}).get(name)
            if reference is None: raise ContractError("outer source table not in recorded manifest: " + name)
            path, _ = _verified_file(self.root, reference)
            self._watches[path] = source_stamp(path)
            self._tables[name] = tuple(_master_rows(path))
            self.validate_unchanged()
        return self._tables[name]

    def _one(self, name, identity, *, level=None):
        key = name + ":index"
        rows = self._table_rows(name)
        if key not in self._tables:
            indexed = {}
            for row in rows:
                indexed.setdefault((row.get("id"), row.get("level")), []).append(row)
            self._tables[key] = indexed
        matches = self._tables[key].get((identity, level), [])
        if len(matches) != 1: raise ContractError("outer source identity/level does not resolve: " + name + "/" + str(identity))
        return matches[0]

    def _graph(self, table, identity, prefix, tokens, gaps, visited=()):
        key = (table, identity, prefix, visited)
        found = self._graphs.get(key)
        if found is None:
            cached_tokens, cached_gaps = [], []
            self.features._graph(table, identity, prefix, cached_tokens, cached_gaps, visited)
            found = (tuple(cached_tokens), tuple(cached_gaps))
            self._graphs[key] = found
            if len(self._graphs) > 256: self._graphs.popitem(last=False)
        else:
            self._graphs.move_to_end(key)
        tokens.extend(found[0]); gaps.extend(found[1])

    def _links(self, value, prefix, tokens, gaps, visited=()):
        self.features._links(value, prefix, tokens, gaps, visited)

    def skill_tokens(self, skill, prefix):
        definition = self._one("ProduceSkill.yaml", skill["id"], level=skill["level"])
        tokens = _tokens(prefix + ".definition", definition)
        for slot in (1, 2, 3):
            effect, trigger = definition.get("produceEffectId" + str(slot)), definition.get("produceTriggerId" + str(slot))
            if effect:
                tokens += _tokens(prefix + f".effect[{slot}]", self._one("ProduceEffect.yaml", effect))
            if trigger:
                tokens += _tokens(prefix + f".trigger[{slot}]", self._one("ProduceTrigger.yaml", trigger))
        return tokens

    def passive_tokens(self, raw):
        """Include every owned skill's actual level, trigger quota and definition."""
        result = []
        for support in raw.get("collections", {}).get("support_cards", []):
            aligned = support.get("produceSkillTriggerCounts", [])
            for index, skill in enumerate(support.get("produceSkills", [])):
                current = dict(skill)
                if "triggerCount" not in current and index < len(aligned):
                    value = aligned[index]
                    if not isinstance(value, str) or not value.isdigit():
                        raise ContractError("observed aligned support trigger counter is invalid")
                    current["triggerCount"] = int(value)
                result.append(_tokens("outer.support_skill.current", current) + self.skill_tokens(current, "outer.support_skill"))
        for key in ("characterProduceSkills", "idolCardProduceSkills"):
            for skill in raw.get("progress", {}).get(key, []):
                result.append(_tokens("outer." + key + ".current", skill) + self.skill_tokens(skill, "outer." + key))
        for memory in raw.get("collections", {}).get("memories", []):
            # Current abilities hold triggerCount; nested source memory contains
            # unrelated image/GUID/grade and is deliberately never encoded.
            for ability in memory.get("abilities", []):
                definition = self._one("MemoryAbility.yaml", ability["id"], level=ability["level"])
                skill = {"id": definition["skillId"], "level": ability["level"]}
                result.append(_tokens("outer.memory_ability.current", ability) +
                              _tokens("outer.memory_ability.definition", definition) + self.skill_tokens(skill, "outer.memory_ability.skill"))
        return result

    def scope_context(self, raw):
        """Reuse existing source calendar for phase, without selecting difficulty."""
        from ..route_calendar import load_route_calendar
        from ..runtime_mode_profile import _stage
        self.validate_unchanged()
        progress, state = raw["progress"], raw["state"]
        idol = self._one("IdolCard.yaml", progress["idolCardId"])
        # Calendar reads these existing tables; verify exactly those authorities.
        for name in ("Produce.yaml", "ProduceGroup.yaml", "ProduceSetting.yaml"):
            self._table_rows(name)
        calendar = load_route_calendar(state["produce_id"], character_id=idol["characterId"], master_dir=self.master_dir)
        completed = state.get("step_type") in (16, 17, 18) and state.get("progress_status") in (4, 9, 10, 11, 12, 13, 14)
        minimum = state["week"] + (1 if raw.get("surface") == "schedule" or completed else 0)
        milestone = next((m for m in calendar.milestones if m.week >= minimum), None)
        if milestone is None: raise ContractError("no remaining phase for nonterminal observed input")
        phase = {"ProduceStepType_AuditionMid1": "Mid1", "ProduceStepType_AuditionMid2": "Mid2", "ProduceStepType_AuditionFinal": "Final"}[_stage(milestone.step_type)]
        self.validate_unchanged()
        return {"flow_id": idol["planType"] + "|" + idol["examEffectType"],
                "mode_id": {"produce-004": "nia-pro", "produce-005": "nia-master"}[state["produce_id"]],
                "stage_id": phase, "source_master_hash": self.package.contract["source_master_hash"],
                "source_binding": {"outer_manifest": self.reference, "semantic_package": self.package.contract["contract_sha256"],
                                   "projection_implementation": self.implementation}}

    def current_candidate_tokens(self, raw, target):
        """Describe an offered action's present cost/base terms, never its result."""
        from ..runtime_outer_policy import _LESSONS, _event_suggestion_point_cost
        result = []
        if target["action_id"] == "schedule.choose" and target.get("step_type") in _LESSONS:
            from ..lesson_stamina_quote import build_lesson_stamina_quote, TABLES
            for name in TABLES: self._table_rows(name)
            quote = build_lesson_stamina_quote(raw, target, self.master_dir)
            lesson = self._one("ProduceStepSelfLesson.yaml", quote["lesson_id"])
            # Only entry payment is a cost now. EndLesson effects are represented
            # by current item/skill definitions, not a manufactured after-state.
            result += _tokens("outer.offered.lesson", lesson)
            result += _tokens("outer.offered.entry_cost", {"stamina": quote["entry_stamina_cost"]})
        if target["action_id"] == "event.choose":
            candidate = next(x for x in raw["legal_actions"] if x["target"] == target)
            suggestion = candidate.get("evidence", {}).get("suggestion", {})
            # Native protobuf omitted zero is not confused with a quoted wallet.
            base = suggestion.get("producePoint", 0)
            price, _ = _event_suggestion_point_cost(raw, base)
            result += _tokens("outer.offered.entry_cost", {"produce_points": price})
        return result


def build_fine_semantic_source(runtime_master_source_reference, outer_manifest_reference, *, source_relocations_reference=None,
                               historical_master_manifest_reference=None):
    """Reuse the existing verified material factory; no native/model execution."""
    if historical_master_manifest_reference is not None:
        from .observed_initial_loadout import build_initial_loadout_source, SOURCE_SCHEMA
        spec = {"schema": SOURCE_SCHEMA, "runtime_master_source": runtime_master_source_reference,
                "historical_master_manifest": historical_master_manifest_reference}
        if source_relocations_reference is not None: spec["source_relocations"] = source_relocations_reference
        source = build_initial_loadout_source(spec)
        if source.reference["sha256"] != outer_manifest_reference["sha256"]:
            raise ContractError("older fine material archive differs from the explicitly bound source")
        return source
    from ..runtime_master_source import load_runtime_master_source
    from ..runtime_optional_parent_features import RuntimeOptionalParentCardFeatures
    from ..artifact_source_resolver import ArtifactSourceResolver
    source = load_runtime_master_source(runtime_master_source_reference)
    resolver = ArtifactSourceResolver(source_relocations_reference) if source_relocations_reference is not None else None
    features = RuntimeOptionalParentCardFeatures(source.package(source_resolver=resolver))
    def validate():
        source.validate_unchanged()
        features.validate_definition_extension()
        if resolver is not None: resolver.validate_unchanged()
    result = FineOuterSemanticSource(features, outer_manifest_reference, validate_materials=validate)
    paths = [path for path, _ in source._watched]
    paths += [path for path, _ in features._extension_files]
    if resolver is not None:
        paths += [resolver._manifest_path, *(path for path, _ in resolver._manifest_destinations)]
        paths += [resolved.physical_path for resolved in resolver._resolutions.values()]
    result._additional_source_paths = tuple(dict.fromkeys(Path(path).resolve() for path in paths))
    return result


def _clean(value):
    """Filter a known semantic owner, not an arbitrary whole native observation."""
    if isinstance(value, Mapping):
        return {key: _clean(child) for key, child in value.items()
                if key not in _DISPLAY and not _LOCAL.search(key) and not key.startswith("hidden")}
    if isinstance(value, (list, tuple)):
        return [_clean(x) for x in value]
    if isinstance(value, str) and _GUID.fullmatch(value):
        return None
    return value


def _tokens(prefix, value):
    result, gaps = [], []
    _emit(prefix, _clean(value), result, gaps)
    if gaps:
        raise ContractError("invalid observed semantic input: " + str(gaps[:3]))
    return result


def _missing_fields(source, fields):
    return {key: source.get(key) for key in fields}


def _action_terms(target):
    terms = {key: value for key, value in target.items() if key in _TARGET_FIELDS}
    if target.get("action_id", "").startswith("audition.") and "number" in target:
        terms["audition_tier"] = target["number"]
    if target.get("action_id") == "ui.navigation" and target.get("button_id") == "schedule.refresh":
        terms["schedule_action"] = "rest"
    return terms


def _graph(features, table, identity, prefix):
    values, gaps = [], []
    features._graph(table, identity, prefix, values, gaps)
    if gaps:
        raise ContractError("source-qualified fine definition unavailable: " + str(gaps[:3]))
    return values


def _card_tokens(card, features):
    identity = card.get("produceCardId", card.get("card_id", card.get("resource_id")))
    if not isinstance(identity, str) or not identity:
        raise ContractError("observed card identity is missing")
    upgrade = card.get("upgradeCount", card.get("upgrade", 0))
    if type(upgrade) is not int or upgrade < 0:
        raise ContractError("observed card upgrade must be nonnegative")
    tokens = _tokens("outer.card.current", {k: card[k] for k in _CARD_FIELDS if k in card})
    counts = card.get("customizeCountList", card.get("customize_counts", []))
    if not isinstance(counts, list) or any(type(x) is not int or x < 0 for x in counts):
        raise ContractError("observed customization counts are invalid")
    static = features.payload["cards"].get(f"{identity}@{upgrade}")
    if not isinstance(static, Mapping):
        raise ContractError("fine card definition unavailable in the existing qualified payload")
    # This is exactly the source-known portion of the existing host _card
    # codec. No absent exam runtime object/status/lineage is manufactured.
    tokens += ["semantic:" + value for value in static["tokens"]]
    tokens += _graph(features, "card", f"{identity}@{upgrade}", "native.base")
    ids = card.get("customize_ids", static.get("customize_ids", []))
    if len(counts) > len(ids):
        raise ContractError("observed customization has no source-bound option")
    gaps = []
    _emit("native.pt.counts", counts, tokens, gaps)
    for index, count in enumerate(counts):
        tokens.append(f"native.pt.option[{index}]:id:{ids[index]}")
        if count:
            selected = static.get("customization_rules", {}).get(ids[index], {}).get(str(count))
            if not isinstance(selected, Mapping):
                raise ContractError("observed customization rule is missing from its original semantic payload")
            _emit(f"native.pt.selected[{index}]", selected, tokens, gaps)
            features._links(selected, f"native.pt.selected[{index}]", tokens, gaps)
            tokens += _graph(features, "produce_card_customize", f"{ids[index]}@{count}", f"native.pt.source[{index}]")
    if gaps: raise ContractError("source-known card customization semantics are incomplete: " + str(gaps[:3]))
    return list(public_semantic_tokens(tokens))


def _drink_tokens(identity, features):
    # game_projection splits the real drink owner and removes only its slot
    # ordinal, leaving this exact namespace for its known ID/definition.
    return _tokens("native.inventory.drink", {"_id": identity}) + _graph(
        features, "produce_drink", identity, "native.inventory.drink.definition")


def _item_definition_tokens(identity, features):
    return _tokens("native.item", {"_id": identity}) + _graph(
        features, "produce_item", identity, "native.item.definition")


def _action_kind(target, ui):
    name = target["action_id"]
    if name == "schedule.choose": return "schedule"
    if name == "ui.navigation" and target.get("button_id") == "schedule.refresh": return "schedule"
    if name.startswith(("event.", "business.")): return "event"
    if name.startswith("customize."):
        return "decline" if name in ("customize.finish", "customize.back_to_cards") else "customize"
    if name == "card_choice.select":
        return {"Upgrade": "upgrade", "Delete": "remove", "Duplicate": "duplicate", "Change": "select"}.get(ui.get("selection_type"), "select")
    if name == "card_choice.reveal":
        # A known off-screen row has its actual native realization callback.
        # Keep that control distinct in action_id/realized features; do not
        # pretend it is already a submitted delete/upgrade/select operation.
        return "select"
    if name.startswith(("shop.", "interval.")):
        return "decline" if name.endswith(("finish", "cancel_operation")) else "buy"
    if name.startswith("drink_choice.") or name.startswith("reward.drink_capacity"):
        return "replace_drink"
    if name == "reward.select":
        return {1: "take_card", 3: "take_drink"}.get(target.get("resource_type"), "select")
    if name.endswith(("skip", "cancel", "back", "finish")): return "decline"
    if name.startswith("audition."): return "select"
    if name.endswith(("confirm", "receive", "confirm_skip", "confirm_skip_new")): return "confirm"
    raise ContractError("fine action has no shared action type: " + name)


def _current_match(raw, target):
    ui = raw.get("ui_state", {})
    name = target["action_id"]
    if name.startswith(("shop.", "interval.")):
        values = [r for r in ui.get("products", ()) if r.get("product_instance_id") == target.get("product_instance_id") and target.get("product_instance_id")]
        if not values and ui.get("phase") == "confirm_purchase":
            values = [r for r in ui.get("products", ()) if r.get("selected") is True]
    elif name in ("customize.select_option", "customize.execute"):
        values = [r for r in ui.get("customizes", ()) if r.get("customize_id") == target.get("customize_id")]
    elif name.startswith("business."):
        values = [r for r in ui.get("folders", ()) if all(r.get(key) == target.get(key)
                  for key in ("business_number", "business_type", "index"))]
    else:
        values = [r for r in ui.get("candidates", ()) if r.get("index") == target.get("index") and target.get("index") is not None]
    if len(values) > 1:
        raise ContractError("observed candidate does not resolve uniquely")
    return values[0] if values else {}


def _candidate_tokens(raw, candidate, features):
    target, ui = candidate["target"], raw.get("ui_state", {})
    current = _current_match(raw, target)
    tokens = _tokens("outer.action", _action_terms(target))
    tokens += _tokens("outer.offered", current)
    evidence = candidate.get("evidence", {})
    tokens += _tokens("outer.offered.evidence", evidence)
    if target.get("action_id") == "schedule.choose":
        from ..nia_static_adapter import _PARAMETER_TYPES
        parameter = evidence.get("parameter_type")
        if parameter is not None:
            if type(parameter) is not int or parameter not in _PARAMETER_TYPES:
                raise ContractError("observed schedule sub-parameter has no source mapping")
            parameter = _PARAMETER_TYPES[parameter]
        tokens += schedule_choice_tokens(target["step_type"], step_sub_parameter_type=parameter)
    elif target.get("action_id") == "ui.navigation" and target.get("button_id") == "schedule.refresh":
        tokens += schedule_choice_tokens("ProduceStepType_Refresh")
    card_id = target.get("card_id") or current.get("card_id")
    resource = target.get("resource_type", current.get("resource_type"))
    resource_id = target.get("resource_id", current.get("resource_id"))
    if card_id or resource == 1:
        card = {**current, "card_id": card_id or resource_id,
                "upgrade": target.get("upgrade", current.get("upgrade", 0))}
        tokens += _card_tokens(card, features)
    if resource in (2, 3) and resource_id:
        tokens += _item_definition_tokens(resource_id, features) if resource == 2 else _drink_tokens(resource_id, features)
    if target.get("drink_id"):
        tokens += _drink_tokens(target["drink_id"], features)
    if target.get("selected_reward_id"):
        # The new reward and discarded owned drink are distinct semantic
        # owners. The pending reward has its own canonical entity below;
        # merging both native drink definitions would conflict scalar paths.
        tokens += _tokens("outer.offered.selected_reward", {"id": target["selected_reward_id"],
                          "quantity": target.get("selected_reward_quantity")})
    if target.get("customize_id"):
        count = target.get("customize_count", current.get("customize_count"))
        if type(count) is not int or count < 1:
            raise ContractError("actual selected customization level is missing")
        tokens += _graph(features, "produce_card_customize", f"{target['customize_id']}@{count}", "outer.offered.customize")
    suggestion = evidence.get("suggestion", {})
    for index, identity in enumerate(suggestion.get("produceEffectIds", ())):
        tokens += _graph(features, "produce_effect", identity, f"outer.offered.event_effect[{index}]")
    if hasattr(features, "current_candidate_tokens"):
        tokens += features.current_candidate_tokens(raw, target)
    return tokens


def _source_guard(row, features, scope_context):
    if features is None or scope_context is None:
        raise ContractError("fine observed inputs require explicit qualified semantic materials and scope")
    required = {"flow_id", "mode_id", "stage_id", "source_master_hash", "source_binding"}
    if not required <= set(scope_context):
        raise ContractError("fine scope has missing source-bound context fields")
    actual = features.package.contract["source_master_hash"]
    if actual != scope_context["source_master_hash"]:
        raise ContractError("fine scope and semantic materials use different Masters")
    preflights = row.get("provenance", {}).get("observed_asset_preflights", [])
    masters = {x["value"].get("master_hash") for x in preflights if x["value"].get("required") is True}
    legacy = row.get("provenance", {}).get("recorded_material_binding")
    if legacy is not None:
        if (masters or legacy.get("kind") != "recorded-native-Master-in-continuous-Produce"
                or legacy.get("source_master_hash") != actual or not legacy.get("native_master_witnesses")
                or not legacy.get("before_ap_receipt") or legacy.get("unrecorded_outer_preflight_claimed") is not False
                or legacy.get("archive", {}).get("sha256") != getattr(features, "reference", {}).get("sha256")):
            raise ContractError("older observed native Master/Produce authority differs")
    elif masters != {actual}:
        raise ContractError("original observed run has no matching single Master authority")
    if legacy is None and hasattr(features, "reference"):
        manifests = {x["value"].get("manifest_sha256") for x in preflights if x["value"].get("required") is True}
        if manifests != {features.reference["sha256"]}:
            raise ContractError("outer supplemental manifest differs from the original observed preflight")
    if hasattr(features, "validate_unchanged"):
        features.validate_unchanged()


def fine_decision_subtype(raw):
    """Actor rule category from the observation, never the chosen action label.

    Native screen/family/selection fields carry the detailed state semantics.
    Every recorded fine pool selects one currently legal control. In particular,
    mixed rewards cannot reveal their eventual resource type before selection.
    """
    if not isinstance(raw, Mapping) or not isinstance(raw.get("legal_actions"), list):
        raise ContractError("fine decision subtype requires the observed legal control pool")
    return "outer-native-control"


def native_decision_week(row,raw,*,next_observation=False):
    """Decision clock from native week and already-selected weekly intent.

    A pending confirmation can still show the previous completed native week.
    Only earlier observed actions can supply the pending next week; the current
    teacher action is visible exclusively when projecting its next observation.
    """
    week=raw['state']['week']
    if type(week)is not int or week<0:raise ContractError('observed native week must be nonnegative')
    current=row['provenance']['original_run_step_ordinal']
    past=list(row.get('observed_history',()))
    if any(type(x.get('run_step_ordinal'))is not int or x['run_step_ordinal']>=current for x in past):
        raise ContractError('observed history contains current/future actions')
    if next_observation:
        past.append({'chosen_action':row['chosen_action'],'state_before':row['observation']['state']})
    decision=week+(1 if raw.get('surface')=='schedule'else 0)
    for prior in past:
        target=prior.get('chosen_action',{})
        if not(target.get('action_id')=='schedule.choose'or
               target.get('action_id')=='ui.navigation'and target.get('button_id')=='schedule.refresh'):
            continue
        before=prior.get('state_before',{}).get('week')
        if type(before)is not int or before<0:raise ContractError('earlier schedule intent lacks its native week')
        if before>week:raise ContractError('earlier schedule intent is beyond the current native week')
        decision=max(decision,before+1)
    return decision


def serialized_fine_projection(projected):
    """JSON derivative; source qualification signs this once, never per batch."""
    state = projected["state"]
    return {**{k: deepcopy(v) for k, v in projected.items() if k != "state"},
            "state": {"scope": asdict(state.scope), "session_generation": state.session_generation,
                      "revision": state.revision, "kind": state.kind.value,
                      "information": state.information.unpack(), "binding_id": state.binding_id}}


def prepared_fine_projection(row, schema, *, next_observation=False):
    """Decode the qualified derivative without opening or loading any Master."""
    prepared = row.get("prepared_projection") or {}
    if prepared.get("schema") != "gkms.rl.prepared-observed-fine.v1" or prepared.get("feature_schema_sha256") != schema.identity:
        raise ContractError("source-indexed fine projection and exact schema required")
    value = prepared.get("next" if next_observation else "current")
    if value is None:
        raise ContractError("terminal observed fine successor is deliberately unused")
    if value.get("objective_id") != OUTER_OBJECTIVE or value.get("decision_subtype") != "outer-native-control" or value.get("selection_semantics") != SELECTION_SEMANTICS:
        raise ContractError("prepared fine objective/control contract differs")
    raw = value["state"]
    state = State(Scope(**raw["scope"]), raw["session_generation"], raw["revision"], DecisionKind(raw["kind"]),
                  FrozenJSON.of(raw["information"]), raw["binding_id"])
    if state.kind is not DecisionKind.OUTER or raw["information"].get("schema") != schema.identity:
        raise ContractError("prepared fine state kind/schema differs")
    if not next_observation and [x["target"] for x in value["candidates"]] != [x["target"] for x in row["candidates"]]:
        raise ContractError("prepared fine candidates differ from their actual recorded pool")
    return {**value, "state": state}


def project_fine_observation(row, schema, *, next_observation=False, semantic_features=None, scope_context=None):
    """Project a source-verified native row; caller owns original-file qualification."""
    if row.get("schema") != "gkms.observed-outer-transition.v1" or row.get("objective", {}).get("id") != OUTER_OBJECTIVE:
        raise ContractError("explicit observed fine row and Produce-rating objective required")
    if schema.entity_names != ENTITY_FEATURE_NAMES or not set(OUTER_TYPES) <= set(schema.entity_types) or not set(OUTER_ZONES) <= set(schema.zones):
        raise ContractError("append-only shared observed OUTER feature schema required")
    if row.get("action_form") == "observed-selector-sequence" and not next_observation:
        raise ContractError("observed selector sequence requires the explicit prefix adapter")
    _source_guard(row, semantic_features, scope_context)
    raw = row.get("next_observation" if next_observation else "observation")
    if not isinstance(raw, Mapping) or raw.get("actions_complete") is not True or raw.get("busy") is not False:
        raise ContractError("complete current native observation required")
    if next_observation and row.get("done"):
        raise ContractError("terminal Produce rating is a target, not another actor input")
    state, progress = raw.get("state", {}), raw.get("progress", {})
    collections, ui = raw.get("collections", {}), raw.get("ui_state", {})
    if state.get("produce_id") != {"nia-pro": "produce-004", "nia-master": "produce-005"}.get(scope_context["mode_id"]):
        raise ContractError("observed mode differs from qualified scope")
    if hasattr(semantic_features, "scope_context") and semantic_features.scope_context(raw) != scope_context:
        raise ContractError("fine scope does not match this exact observed phase/source")
    entities = []
    def add(kind, zone, tokens):
        entities.append({"type": kind, "zone": zone, "definition_id": "observed-" + kind,
                         "count": 1, "values": encode_semantic_tokens(public_semantic_tokens(tokens))})
    context_tokens = _tokens("outer.current", _missing_fields(state, STATE_FIELDS))
    context_tokens += _tokens("outer.modifiers", _missing_fields(progress, PROGRESS_FIELDS))
    context_tokens += _tokens("outer.selection", _missing_fields(ui, UI_FIELDS))
    context_tokens += _tokens("outer.screen", {"surface": raw.get("surface"), "screen_type": raw.get("screen_type")})
    context_tokens += _tokens("outer.decision_subtype", fine_decision_subtype(raw))
    from ..nia_route_profile import nia_final_week
    plan_type, exam_effect_type = scope_context["flow_id"].split("|", 1)
    # The native schedule shows the completed week; replay schedules number
    # the decision being taken. Reuse the host's existing week convention.
    decision_week = native_decision_week(row,raw,next_observation=next_observation)
    phase = {"Mid1":1, "Mid2":2, "Final":3}[scope_context["stage_id"]]
    context_tokens += observed_context_tokens(
        {"produce_id": state["produce_id"], "plan_type": plan_type,
         "exam_effect_type": exam_effect_type, "idol_card_id": progress.get("idolCardId")},
        decision_week, phase,
        nia_final_week(state["produce_id"]), {key: state.get(key) for key in RESOURCE_MAP})
    add("outer_context", "outer_current", context_tokens)
    cards = collections.get("cards")
    if not isinstance(cards, list): raise ContractError("current observed deck is missing")
    for card in cards:
        if card.get("deleted") is not True and card.get("isDeleted") is not True:
            add("card", "deckList", _card_tokens(card, semantic_features))
    for drink in progress.get("produceDrinkIds", []):
        add("drink", "inventory", _drink_tokens(drink, semantic_features))
    if ui.get("family") == "drink_inventory":
        for item in ui.get("items", []):
            identity = item.get("drink_id")
            if not identity: raise ContractError("visible drink retained-set pool identity is missing")
            add("drink", "outer_offered", _tokens("outer.visible_drink", item) +
                _drink_tokens(identity, semantic_features))
    pending_drinks = {candidate.get("target", {}).get("selected_reward_id")
                      for candidate in raw.get("legal_actions", [])}
    for identity in sorted(pending_drinks - {None}):
        add("drink", "outer_offered", _tokens("outer.visible_drink", {"role": "pending-reward"}) +
            _drink_tokens(identity, semantic_features))
    for item in progress.get("produceItems", []):
        item_id = item.get("produceItemId")
        add("item", "inventory", _tokens("outer.item.current", item) + _item_definition_tokens(item_id, semantic_features))
    # Actual owner scalars and all source-resolved skill/trigger definitions.
    for support in collections.get("support_cards", []):
        add("support", "inventory", _tokens("outer.support.current", support) +
            _tokens("native.support", {"_supportCardId": support.get("supportCardId")}))
    for key in ("characterProduceSkills", "idolCardProduceSkills"):
        for skill in progress.get(key, []):
            add("context", "outer_current", _tokens("outer." + key, skill))
    for memory in collections.get("memories", []):
        add("context", "outer_current", _tokens("outer.memory.current", {"abilities": memory.get("abilities", [])}))
    if not hasattr(semantic_features, "passive_tokens"):
        if collections.get("support_cards") or collections.get("memories") or progress.get("characterProduceSkills") or progress.get("idolCardProduceSkills"):
            raise ContractError("owned outer skill definitions require their qualified supplementary source")
    else:
        for tokens in semantic_features.passive_tokens(raw):
            add("context", "outer_current", tokens)
    for effect in collections.get("effects", []):
        tokens = _tokens("outer.pending_effect.current", effect)
        if effect.get("produceEffectId"):
            tokens += _graph(semantic_features, "produce_effect", effect["produceEffectId"], "outer.pending_effect.definition")
        add("context", "outer_current", tokens)
    history = deepcopy(row.get("observed_history", []))
    if not isinstance(history, list): raise ContractError("causal observed history must be a list")
    current_ordinal = row["provenance"]["original_run_step_ordinal"]
    boundary_ordinal = row["provenance"].get("next_original_run_step_ordinal") if next_observation else current_ordinal
    if type(boundary_ordinal) is not int or (next_observation and boundary_ordinal <= current_ordinal):
        raise ContractError("exact original observation boundary ordinal required")
    if any(type(x.get("run_step_ordinal")) is not int or x["run_step_ordinal"] >= current_ordinal for x in history):
        raise ContractError("observed history contains current/future actions")
    if [x["run_step_ordinal"] for x in history] != sorted({x["run_step_ordinal"] for x in history}):
        raise ContractError("observed history is repeated or reordered")
    if next_observation:
        history.append({"run_step_ordinal": current_ordinal, "decision_kind": row["decision_kind"],
                        "state_before": row["observation"]["state"], "chosen_action": row["chosen_action"],
                        "state_after": raw["state"], "completed_auditions": row["provenance"].get("completed_auditions", [])})
    for prior in history:
        values = {"run_step_ordinal": prior["run_step_ordinal"],
                  "relative_age": boundary_ordinal-prior["run_step_ordinal"],
                  "state_before": _missing_fields(prior["state_before"], STATE_FIELDS),
                  "state_after": _missing_fields(prior["state_after"], STATE_FIELDS),
                  "chosen_action": _action_terms(prior["chosen_action"]),
                  "completed_auditions": [{"step_type": result["step_type"], "observed_score": result["score"],
                    "is_win": result.get("is_win"), "audition_tier": result.get("selected_number")}
                    for result in prior.get("completed_auditions", [])]}
        from .observed_outer_projection import (audition_milestone_tokens,settled_schedule_history_tokens,
                                               completed_audition_result_tokens)
        tokens = _tokens("outer.past", values)
        target = prior["chosen_action"]
        completed = prior.get("completed_auditions", [])
        selected_step = (target.get("step_type") if target.get("action_id") == "schedule.choose" else
                         14 if target.get("action_id") == "ui.navigation" and target.get("button_id") == "schedule.refresh" else None)
        selected_week = prior["state_before"].get("week")
        if selected_step is not None and type(selected_week) is int:
            tokens += settled_schedule_history_tokens(selected_step,selected_week+1,decision_week)
        elif completed and type(completed[-1].get("week")) is int:
            tokens += settled_schedule_history_tokens(completed[-1]["step_type"],completed[-1]["week"],decision_week)
        if selected_step is not None or completed:
            latest = completed[-1] if completed else {}
            tokens += completed_audition_result_tokens(score=latest.get("score"),tier=latest.get("selected_number"))
        for index, result in enumerate(prior.get("completed_auditions", [])):
            tokens += audition_milestone_tokens(result.get("start_state"), semantic_source=semantic_features, occurrence=index)
        add("outer_history", "outer_history", tokens)
    candidates = []
    for candidate in raw.get("legal_actions", []):
        target = candidate.get("target")
        if not isinstance(target, Mapping): raise ContractError("native offered control has no exact target")
        tokens = _candidate_tokens(raw, candidate, semantic_features)
        candidates.append({"type": ACTION_IDS[_action_kind(target, ui)], "values": encode_semantic_tokens(public_semantic_tokens(tokens)),
                           "target": deepcopy(target), "observed_offered": True, "resource_feasibility": None})
        add("outer_offer", "outer_offered", tokens)
    if not candidates or len({digest(x["target"]) for x in candidates}) != len(candidates):
        raise ContractError("complete unique observed legal candidate set required")
    if len(entities) > schema.max_entities:
        raise ContractError("fine observation exceeds entity budget; no truncation allowed")
    globals_ = {destination: state.get(source) for source, destination in (
        ("stamina", "stamina"), ("max_stamina", "maxStamina"), ("produce_points", "producePoint"),
        ("vocal", "parameterVocal"), ("dance", "parameterDance"), ("visual", "parameterVisual")) if destination in schema.global_names}
    scope = Scope(row["provenance"]["trajectory_id"], None, scope_context["mode_id"], scope_context["flow_id"], scope_context["stage_id"])
    information = {"schema": schema.identity, "global": globals_, "entities": entities}
    state = State(scope, "recorded-outer:" + row["provenance"]["session_generation"], digest(raw), DecisionKind.OUTER,
                  FrozenJSON.of(information), digest({"projection": VERSION, "schema": schema.identity,
                  "objective": OUTER_OBJECTIVE, "source_binding": scope_context["source_binding"]}))
    if hasattr(semantic_features, "validate_unchanged"):
        semantic_features.validate_unchanged()
    return {"state": state, "candidates": candidates,
            "decision_subtype": fine_decision_subtype(raw),
            "selection_semantics": SELECTION_SEMANTICS, "objective_id": OUTER_OBJECTIVE,
            "observed_feature_scope": "current native values, qualified card/drink/item and owned skill/trigger definitions; no future projection"}


def project_fine_selection_prefixes(row, schema, *, semantic_features, scope_context):
    """Preserve actual dynamic prefix pools, without inventing all kept sets."""
    if row.get("action_form") != "observed-selector-sequence" or not row.get("selection_prefixes"):
        raise ContractError("an actual recorded selector sequence is required")
    result = []
    for prefix in row["selection_prefixes"]:
        raw = prefix["observation"]
        if raw.get("legal_actions") != prefix["legal_actions"] or sum(x["target"] == prefix["chosen_target"] for x in prefix["legal_actions"]) != 1:
            raise ContractError("selector prefix chosen action differs from its observed dynamic pool")
        derived = {**row, "observation": raw, "candidates": prefix["legal_actions"],
                   "chosen_action": prefix["chosen_target"], "action_form": "recorded-native-target"}
        projection = project_fine_observation(derived, schema, semantic_features=semantic_features, scope_context=scope_context)
        result.append({**projection, "chosen_action": deepcopy(prefix["chosen_target"]), "source_ref": deepcopy(prefix["source_ref"])})
    return {"prefixes": result, "selection_commit": deepcopy(row.get("selection_commit")),
            "independent_transition_claimed": False, "macro_return_not_duplicated": True,
            "sequence_semantics": "actual ordered prefix legality; final committed retained set"}
