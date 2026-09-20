"""Master-backed mode rules, independent of any Plan controller or input owner.

Knowing a mode's catalogue/rules does not enable its native flow. Capability
checks are explicit, and live schedule/control rows remain the action authority.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from functools import lru_cache
import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from .audition_rules import AuditionRules, DEFAULT_MASTER_DIR, FINAL, MID1, MID2
from .nia_static_adapter import _STEP_TYPES
from .route_calendar import AuditionMilestone, load_route_calendar, supported_produce_ids
from .audition_stamina import AuditionStartStamina, audition_start_stamina


SCHEMA = "gkms.runtime-mode-profile.v1"
_STAGES = (MID1, MID2, FINAL)
_AXES = ("vocal", "dance", "visual")
_TABLES = ("Produce", "ProduceGroup", "ProduceSetting", "ExamSetting", "IdolCard",
           "ProduceStepAuditionDifficulty", "ProduceExamBattleConfig")


@dataclass(frozen=True, slots=True)
class MasterSource:
    table: str
    sha256: str


@lru_cache(maxsize=36)
def _read_table(path: str, modified_ns: int, size: int):
    data = Path(path).read_bytes()
    rows = yaml.load(data, Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError(f"Master table must contain object rows: {path}")
    return tuple(rows), hashlib.sha256(data).hexdigest()


def _table(directory: Path, name: str):
    path = (Path(directory) / (name + ".yaml")).resolve()
    stat = path.stat()
    return _read_table(str(path), stat.st_mtime_ns, stat.st_size)


def _one(rows, label: str, predicate):
    values = [row for row in rows if predicate(row)]
    if len(values) != 1:
        raise ValueError(f"{label} must resolve once; found {len(values)}")
    return values[0]


def _int(value: object, label: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be nonempty text")
    return value


@dataclass(frozen=True, slots=True)
class ModeSettings:
    attribute_cap: int
    drink_limit: int
    drink_max_limit: int
    customize_count: int
    legend_card_limit: int
    refresh_stamina_permille: int
    before_audition_refresh_permille: int
    exam_stamina_alert: int
    continue_count: int
    trend_assessment_upper: int
    trend_assessment_lower: int
    interval_upgrade_count: int
    interval_customize_count: int
    selection_memory_required_card_count: int


@dataclass(frozen=True, slots=True)
class ModeLessonFamily:
    family: str
    step_types: tuple[str, ...]
    resolves_in_exam: bool
    reward_resources: tuple[str, ...]
    rules_source: str


@dataclass(frozen=True, slots=True)
class SelectionMemoryRequirement:
    split_type: str
    pair_produce_id: str | None
    required_source_produce_id: str | None
    produces_selection_memory: bool
    requires_selection_memory: bool
    embed_produce_card_id: str | None
    minimum_selected_card_count: int
    # This is its own game identity, not one of the four regular memory slots.
    native_selection_field: str = "ProduceSelectInfo.SelectionMemory"
    inherited_fields: tuple[str, ...] = (
        "userSelectionMemoryId", "produceId", "idolCardId", "characterId",
        "vocal", "dance", "visual", "vocalGrowthRatePermil", "danceGrowthRatePermil",
        "visualGrowthRatePermil", "stamina", "star", "produceCards", "produceItems",
        "produceCustomizeItems",
    )


@dataclass(frozen=True, slots=True)
class RuntimeModeProfile:
    produce_id: str
    group_id: str
    scenario_name: str
    difficulty_name: str
    produce_type: str
    total_steps: int
    setting_id: str
    exam_setting_id: str
    grade_limit: str
    target_resources: tuple[str, ...]
    audition_stages: tuple[str, ...]
    lesson_families: tuple[ModeLessonFamily, ...]
    settings: ModeSettings
    selection_memory: SelectionMemoryRequirement
    required_capabilities: tuple[str, ...]
    master_sources: tuple[MasterSource, ...]
    rule_digest: str
    catalogue_known: bool = True
    rule_schema: str = SCHEMA

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def load_runtime_mode_profile(produce_id: str, *, master_dir: Path = DEFAULT_MASTER_DIR) -> RuntimeModeProfile:
    tables, sources = {}, []
    for name in _TABLES:
        tables[name], digest = _table(master_dir, name)
        sources.append(MasterSource(name, digest))
    mode = _one(tables["Produce"], "Produce/" + produce_id, lambda row: row.get("id") == produce_id)
    group = _one(tables["ProduceGroup"], "ProduceGroup membership/" + produce_id,
                 lambda row: produce_id in row.get("produceIds", ()))
    setting_id = _text(mode.get("produceSettingId"), "produceSettingId")
    exam_id = _text(mode.get("examSettingId"), "examSettingId")
    setting = _one(tables["ProduceSetting"], setting_id, lambda row: row.get("id") == setting_id)
    exam = _one(tables["ExamSetting"], exam_id, lambda row: row.get("id") == exam_id)
    differences = [row for row in tables["ProduceStepAuditionDifficulty"] if row.get("produceId") == produce_id]
    stages = tuple(stage for stage in _STAGES if any(row.get("stepType") == stage for row in differences))
    if not stages or stages[-1] != FINAL or any(row.get("stepType") not in _STAGES for row in differences):
        raise ValueError("mode audition stages are incomplete or unknown")
    configs = {row["id"] for row in tables["ProduceExamBattleConfig"]}
    if any(row.get("produceExamBattleConfigId") not in configs for row in differences):
        raise ValueError("audition references an unknown BattleConfig")
    settings = ModeSettings(*(_int(value, name) for name, value in (
        ("attribute_cap", mode.get("idolCardParameterGrowthLimit")),
        ("drink_limit", setting.get("produceDrinkPossessLimit")),
        ("drink_max_limit", setting.get("produceDrinkPossessMaxLimit")),
        ("customize_count", setting.get("customizeProduceCardCount")),
        ("legend_card_limit", setting.get("maxLegendProduceCardCount")),
        ("refresh_stamina_permille", setting.get("refreshStaminaRecoveryPermil")),
        ("before_audition_refresh_permille", setting.get("beforeAuditionRefreshStaminaRecoveryPermil")),
        ("exam_stamina_alert", setting.get("examStartAlertStaminaThreshold")),
        ("continue_count", setting.get("continueCount")),
        ("trend_assessment_upper", setting.get("produceAuditionTrendAssessmentPermilUpper")),
        ("trend_assessment_lower", setting.get("produceAuditionTrendAssessmentPermilLower")),
        ("interval_upgrade_count", setting.get("stepIntervalUpgradeProduceCardCount")),
        ("interval_customize_count", setting.get("stepIntervalCustomizeProduceCardCount")),
        ("selection_memory_required_card_count", setting.get("selectionMemoryNeedProduceCardCount")),
    )))
    kind = _text(group.get("type"), "ProduceType")
    capabilities = {"produce.prepare", "outer.schedule", "outer.event", "outer.card-choice", "exam.audition",
                    # Family-wide code alone cannot claim a mode has been integrated.
                    f"mode.{produce_id}.runtime-integrated"}
    if kind == "ProduceType_FirstStar":
        family, numbers = ("legend_lesson", range(29, 35)) if settings.legend_card_limit else ("lesson", range(1, 10))
        rewards, resources = ("stats",), ("stats",)
        capabilities |= {"outer." + family, "exam.lesson", "score.first_star", "produce.result.first_star"}
        if settings.legend_card_limit:
            capabilities |= {"cards.legend", "outer.card-customize"}
    elif kind == "ProduceType_NextIdolAudition":
        family, numbers, rewards, resources = "self_lesson", range(19, 25), ("stats",), ("stats", "vote")
        capabilities |= {"outer.self_lesson", "outer.business", "outer.card-customize", "score.nia", "produce.result.nia"}
    elif kind == "ProduceType_HatsuboshiIdolFestival":
        family, numbers, rewards, resources = "open_lesson", range(35, 47), ("stats", "star"), ("stats", "star")
        capabilities |= {"outer.open_lesson", "outer.interval", "outer.card-customize", "cards.legend", "score.hif"}
    else:
        raise ValueError(f"unknown ProduceType rules: {kind}")
    lessons = (ModeLessonFamily(family, tuple(_STEP_TYPES[number] for number in numbers), family in {"lesson", "legend_lesson"},
                               rewards, "ProduceStepType APK enum; ExamExtensions.IsLesson/IsAutoLesson; ProduceGroup/ProduceSetting; route_calendar"),)
    split = _text(mode.get("produceSplitType"), "produceSplitType")
    pair_id = mode.get("splitPairProduceId") or None
    required_source, produces_memory, requires_memory = None, False, False
    if split != "ProduceSplitType_Unknown":
        if kind != "ProduceType_HatsuboshiIdolFestival" or split not in {"ProduceSplitType_Selection", "ProduceSplitType_Final"}:
            raise ValueError("unknown split-mode rules")
        pair = _one(tables["Produce"], "split pair", lambda row: row.get("id") == pair_id)
        expected = "ProduceSplitType_Final" if split == "ProduceSplitType_Selection" else "ProduceSplitType_Selection"
        if pair.get("splitPairProduceId") != produce_id or pair.get("produceSplitType") != expected:
            raise ValueError("split pair is not reciprocal selection/final")
        if pair_id not in group["produceIds"] or pair.get("selectionMemoryEmbedProduceCardId") != mode.get("selectionMemoryEmbedProduceCardId"):
            raise ValueError("split pair group or embedded selection card differs")
        _text(mode.get("selectionMemoryEmbedProduceCardId"), "selectionMemoryEmbedProduceCardId")
        produces_memory = split == "ProduceSplitType_Selection"
        requires_memory = not produces_memory
        required_source = pair_id if requires_memory else None
        capabilities |= ({"produce.result.hif_selection", "selection_memory.save"} if produces_memory else
                         {"produce.result.hif_final", "selection_memory.read", "selection_memory.select"})
    elif pair_id is not None:
        raise ValueError("unsplit mode cannot carry a split pair")
    selection = SelectionMemoryRequirement(split, pair_id, required_source, produces_memory, requires_memory,
        mode.get("selectionMemoryEmbedProduceCardId") or None, settings.selection_memory_required_card_count if requires_memory else 0)
    total = _int(mode.get("steps"), "Master steps", 1)
    if produce_id in supported_produce_ids():
        calendar = load_route_calendar(produce_id, master_dir=Path(master_dir))
        if calendar.total_weeks != total or tuple(item.step_type for item in calendar.milestones) != stages:
            raise ValueError("route calendar differs from current Master")
    resolved = {"mode": mode, "group": group, "setting": setting, "exam": exam,
                "sources": [asdict(source) for source in sources], "schema": SCHEMA}
    digest = hashlib.sha256(json.dumps(resolved, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return RuntimeModeProfile(produce_id, _text(group.get("id"), "group ID"), _text(group.get("name"), "scenario name"),
        _text(mode.get("name"), "difficulty name"), kind, total, setting_id, exam_id,
        _text(group.get("limitGrade"), "grade limit"), resources, stages, lessons, settings, selection,
        tuple(sorted(capabilities)), tuple(sources), digest)


def list_runtime_mode_profiles(*, master_dir: Path = DEFAULT_MASTER_DIR) -> tuple[RuntimeModeProfile, ...]:
    rows, _ = _table(master_dir, "Produce")
    return tuple(load_runtime_mode_profile(_text(row.get("id"), "Produce.id"), master_dir=master_dir) for row in rows)


@dataclass(frozen=True, slots=True)
class ModeCapabilityReport:
    produce_id: str
    catalogue_known: bool
    execution_ready: bool
    missing_capabilities: tuple[str, ...]
    complete_live_run_verified: bool


def check_mode_capabilities(profile: RuntimeModeProfile, *, implemented: Iterable[str] = (),
                            complete_live_run_verified: bool = False) -> ModeCapabilityReport:
    """Consume the caller's scoped implementation ledger; do not infer it."""
    missing = tuple(sorted(set(profile.required_capabilities) - set(implemented)))
    return ModeCapabilityReport(profile.produce_id, profile.catalogue_known, not missing, missing,
                                bool(complete_live_run_verified and not missing))


@dataclass(frozen=True, slots=True)
class ModeAuditionOption:
    rules: AuditionRules
    difficulty_row_id: str
    required_vote_count: int
    vote_count_baseline: int
    star_score_bonus_baseline: int
    required_dearness_level: int
    is_static_npc_score: bool
    audition_type: str


def load_mode_audition_options(produce_id: str, idol_card_id: str, *,
                               master_dir: Path = DEFAULT_MASTER_DIR) -> tuple[ModeAuditionOption, ...]:
    profile = load_runtime_mode_profile(produce_id, master_dir=master_dir)
    rows, _ = _table(master_dir, "ProduceStepAuditionDifficulty")
    configs, _ = _table(master_dir, "ProduceExamBattleConfig")
    settings, _ = _table(master_dir, "ExamSetting")
    setting = _one(settings, profile.exam_setting_id, lambda row: row.get("id") == profile.exam_setting_id)
    idols, _ = _table(master_dir, "IdolCard")
    idol = _one(idols, "IdolCard/" + idol_card_id, lambda row: row.get("id") == idol_card_id)
    difficulty_id = _text(idol.get("produceStepAuditionDifficultyId"), "IdolCard.produceStepAuditionDifficultyId")
    relevant = [row for row in rows if row.get("produceId") == produce_id and row.get("id") == difficulty_id]
    keys = {(row["stepType"], row["number"]) for row in relevant}
    result = []
    for stage, number in sorted(keys, key=lambda key: (_STAGES.index(key[0]), key[1])):
        candidates = [row for row in relevant if row["stepType"] == stage and row["number"] == number]
        row = _one(candidates, "audition composite key", lambda item: True)
        config = _one(configs, "BattleConfig", lambda item: item.get("id") == row["produceExamBattleConfigId"])
        rule = AuditionRules(idol_card_id, produce_id, profile.difficulty_name, stage, _int(number, "audition number", 1),
            _int(row.get("rankThreshold"), "rankThreshold", 1), _int(row.get("parameterBaseLine"), "parameterBaseLine"),
            _int(row.get("baseScore"), "baseScore"), _int(row.get("forceEndScore"), "forceEndScore"),
            str(row["produceExamBattleNpcGroupId"]), str(config["id"]), str(row["produceExamGimmickEffectGroupId"]),
            _int(config.get("turn"), "turns", 1), *(_int(config.get(axis), axis) for axis in _AXES),
            str(config["produceExamBattleScoreConfigId"]), profile.exam_setting_id,
            _int(setting.get("examTurnEndRecoveryStamina"), "examTurnEndRecoveryStamina"),
            _int(setting.get("handLimit"), "handLimit", 1), _int(setting.get("turnStartDistribute"), "turnStartDistribute", 1))
        if type(row.get("isStaticNpcScore")) is not bool:
            raise ValueError("isStaticNpcScore must be boolean")
        result.append(ModeAuditionOption(rule, str(row["id"]), _int(row.get("voteCount"), "voteCount"),
            _int(row.get("voteCountBaseLine"), "voteCountBaseLine"),
            _int(row.get("starScoreBonusBaseLine"), "starScoreBonusBaseLine"),
            _int(row.get("dearnessLevel"), "dearnessLevel"), row["isStaticNpcScore"],
            _text(row.get("auditionType"), "auditionType")))
    if tuple(stage for stage in profile.audition_stages if any(item.rules.step_type == stage for item in result)) != profile.audition_stages:
        raise ValueError(f"idol audition rules do not cover mode {produce_id}: {idol_card_id} -> {difficulty_id}")
    return tuple(result)


@dataclass(frozen=True, slots=True)
class ModeAuditionSchedule:
    milestones: tuple[AuditionMilestone, ...]
    source: str
    complete: bool


def _stage(value):
    return _STEP_TYPES.get(value) if type(value) is int else value


def resolve_mode_audition_schedule(profile: RuntimeModeProfile, *, native_schedule: Sequence[Mapping[str, Any]] | None = None,
                                   character_id: str | None = None,
                                   master_dir: Path = DEFAULT_MASTER_DIR) -> ModeAuditionSchedule:
    """Use native rows when supplied; HIF has no invented calendar fallback."""
    if native_schedule is not None and not isinstance(native_schedule, (list, tuple)):
        raise ValueError("native schedule must be an array")
    if native_schedule:
        milestones, seen_weeks = [], set()
        for row in native_schedule:
            if not isinstance(row, Mapping):
                raise ValueError("native schedule row must be an object")
            week = _int(row.get("stepNumber", row.get("number")), "native schedule week", 1)
            if week > profile.total_steps or week in seen_weeks:
                raise ValueError("native schedule week is duplicated or exceeds Master steps")
            seen_weeks.add(week)
            values = row.get("stepTypes", ())
            if not isinstance(values, (list, tuple)):
                raise ValueError("native schedule stepTypes must be an array")
            stage_types = {_stage(value) for value in (*values, row.get("selectedStepType"))} & set(_STAGES)
            if len(stage_types) > 1:
                raise ValueError("native schedule has ambiguous audition stages")
            if stage_types:
                milestones.append(AuditionMilestone(week, stage_types.pop()))
        milestones.sort(key=lambda item: item.week)
        stage_order = tuple(item.step_type for item in milestones)
        if len(set(stage_order)) != len(stage_order) or any(stage not in profile.audition_stages for stage in stage_order):
            raise ValueError("native audition stages conflict with Master")
        if stage_order != tuple(stage for stage in profile.audition_stages if stage in stage_order):
            raise ValueError("native audition stage order conflicts with Master")
        complete = stage_order == profile.audition_stages
        source = "native collections.schedule"
        if not complete and profile.produce_id in supported_produce_ids():
            # Partial native projections keep their actual milestones; only
            # missing lifecycle nodes reuse the existing sourced calendar.
            calendar = load_route_calendar(profile.produce_id, character_id=character_id, master_dir=Path(master_dir))
            by_stage = {item.step_type: item for item in calendar.milestones}
            by_stage.update({item.step_type: item for item in milestones})
            milestones = sorted(by_stage.values(), key=lambda item: item.week)
            if tuple(item.step_type for item in milestones) != profile.audition_stages or len({item.week for item in milestones}) != len(milestones):
                raise ValueError("native/calendar audition milestones conflict")
            complete = True
            source += "; missing milestones from route_calendar: " + calendar.source_note
        if complete and milestones[-1].week != profile.total_steps:
            raise ValueError("native Final week differs from Master total steps")
        return ModeAuditionSchedule(tuple(milestones), source, complete)
    if profile.produce_id in supported_produce_ids():
        calendar = load_route_calendar(profile.produce_id, character_id=character_id, master_dir=Path(master_dir))
        return ModeAuditionSchedule(calendar.milestones, "route_calendar: " + calendar.source_note, True)
    return ModeAuditionSchedule((), "native collections.schedule required; no static HIF weeks asserted", False)


def selection_memory_blockers(profile: RuntimeModeProfile, selection: Mapping[str, Any] | None, *,
                               idol_card_id: str, expected_memory_id: str | None = None) -> tuple[str, ...]:
    """Validate identity only; never coerce an ordinary memory into HIF input."""
    if not profile.selection_memory.requires_selection_memory:
        return ()
    if not isinstance(selection, Mapping):
        return ("selection-memory-details-missing",)
    blockers = []
    identity = selection.get("userSelectionMemoryId")
    if not isinstance(identity, str) or not identity:
        blockers.append("selection-memory-native-identity-missing")
    elif expected_memory_id is not None and identity != expected_memory_id:
        blockers.append("selection-memory-native-identity-mismatch")
    if selection.get("produceId") != profile.selection_memory.required_source_produce_id:
        blockers.append("selection-memory-source-mode-mismatch")
    if selection.get("idolCardId") != idol_card_id:
        blockers.append("selection-memory-idol-mismatch")
    return tuple(blockers)


def build_mode_rule_context(snapshot, *, produce_id: str, idol_card_id: str,
                            audition_strategy: str = "highest_available",
                            master_dir: Path = DEFAULT_MASTER_DIR) -> dict[str, object]:
    """Resolve mode resources/timing/targets for the existing shared context.

    This is not a complete outer decision, score prediction, unlock oracle, or
    execution support claim. The caller still supplies the real deck/growth and
    must intersect choices with current native controls.
    """
    if audition_strategy not in {"highest_available", "stable_clear"}:
        raise ValueError("unknown audition_strategy")
    raw = snapshot.raw if hasattr(snapshot, "raw") else snapshot
    if not isinstance(raw, Mapping) or not isinstance(raw.get("state"), Mapping):
        raise ValueError("native snapshot state is missing")
    state = raw["state"]
    if state.get("in_progress") is not True or state.get("produce_id") != produce_id:
        raise ValueError("native active mode differs from requested mode")
    progress = raw.get("progress")
    if not isinstance(progress, Mapping) or progress.get("idolCardId") != idol_card_id:
        raise ValueError("native idol differs from requested idol")
    profile = load_runtime_mode_profile(produce_id, master_dir=master_dir)
    idols, _ = _table(master_dir, "IdolCard")
    idol = _one(idols, "IdolCard/" + idol_card_id, lambda row: row.get("id") == idol_card_id)
    character_id = _text(idol.get("characterId"), "IdolCard.characterId")
    week = _int(state.get("week"), "native week")
    if week > profile.total_steps:
        raise ValueError("native week exceeds mode length")
    resources = {axis: _int(state.get(axis), "native " + axis) for axis in _AXES}
    native_values = {name: _int(state.get(name), "native " + name) for name in ("stamina", "max_stamina", "produce_points")}
    if native_values["max_stamina"] < 1 or native_values["stamina"] > native_values["max_stamina"]:
        raise ValueError("native stamina bounds are invalid")
    growth = {axis: _int(progress.get(axis + "GrowthRatePermil", 0), "native " + axis + " growth") for axis in _AXES}
    if "vote" in profile.target_resources:
        resources["vote_count"] = _int(state.get("vote_count"), "native vote_count")
    if "star" in profile.target_resources:
        resources["star"] = _int(state.get("star"), "native star")
        resources["star_permil"] = _int(state.get("star_permil"), "native star_permil")
    collections = raw.get("collections", {})
    if not isinstance(collections, Mapping):
        raise ValueError("native collections must be an object")
    schedule = resolve_mode_audition_schedule(profile, native_schedule=collections.get("schedule"),
        character_id=character_id, master_dir=master_dir)
    if not schedule.complete:
        raise ValueError("mode audition schedule is incomplete; native schedule required")
    completed_current = _stage(state.get("step_type")) in _STAGES and state.get("progress_status") in {4, 9, 10, 11, 12, 13, 14}
    minimum = week + 1 if raw.get("surface") == "schedule" or completed_current else week
    upcoming = next((item for item in schedule.milestones if item.week >= minimum), None)
    if upcoming is None:
        raise ValueError("current mode has no upcoming audition")
    options = load_mode_audition_options(produce_id, idol_card_id, master_dir=master_dir)
    choose = max if audition_strategy == "highest_available" else min
    final = choose((item for item in options if item.rules.step_type == FINAL), key=lambda item: item.rules.number)
    def targets(option):
        return {axis: getattr(option.rules, axis + "_parameter") for axis in _AXES}
    outlooks = []
    for milestone in schedule.milestones:
        if milestone.week < minimum:
            continue
        candidates = [option for option in options if option.rules.step_type == milestone.step_type
                      and option.required_vote_count <= resources.get("vote_count", 0)]
        if not candidates:
            raise ValueError("no Master resource-eligible upcoming audition")
        following = choose(candidates, key=lambda item: item.rules.number)
        outlooks.append({"stage": milestone.step_type, "week": milestone.week,
            "weeks_until": milestone.week - week, "number": following.rules.number,
            "turns": following.rules.turns, "required_vote_count": following.required_vote_count,
            "vote_count_baseline": following.vote_count_baseline,
            "star_score_bonus_baseline": following.star_score_bonus_baseline,
            "required_dearness_level": following.required_dearness_level,
            "is_static_npc_score": following.is_static_npc_score,
            "targets": targets(following), "timing_source": schedule.source,
            "resource_eligibility": "current native resources; future resources not predicted",
            "baseline_kind": "Master parameters/resources; not a predicted score or guaranteed win"})
    context = {
        "rule_version": SCHEMA, "rule_digest": profile.rule_digest,
        "produce_id": produce_id, "idol_card_id": idol_card_id, "character_id": character_id,
        "plan_type": _text(idol.get("planType"), "IdolCard.planType"),
        "exam_effect_type": _text(idol.get("examEffectType"), "IdolCard.examEffectType"),
        "scenario": profile.scenario_name, "difficulty": profile.difficulty_name,
        "week": week, "weeks_remaining": profile.total_steps - week, "total_steps": profile.total_steps,
        "in_progress": True, "step_type": state.get("step_type"), "progress_status": state.get("progress_status"),
        **native_values, **resources, "growth": growth,
        "target_resources": profile.target_resources, "resources": resources,
        "attribute_cap": profile.settings.attribute_cap, "rest_permille": profile.settings.refresh_stamina_permille,
        "stamina_reserve": min(native_values["max_stamina"], profile.settings.exam_stamina_alert),
        "settings": asdict(profile.settings),
        "next_exam": dict(outlooks[0]), "exam_outlooks": outlooks,
        "final_targets": targets(final), "final_target_number": final.rules.number,
        "final_target_is_aspirational": True,
        "target_source": "Master resource-eligible baseline; actual unlock/selection requires native controls",
        "mode_profile": profile.to_dict(),
        "state_source": "native state getters", "growth_source": "native progress protobuf (omitted scalar means zero)",
        "resource_scope": "outer", "surface": raw.get("surface"),
    }
    from .outer_resource_value import stamina_value_context
    context["before_audition_recovery"] = stamina_value_context(context)
    return context
