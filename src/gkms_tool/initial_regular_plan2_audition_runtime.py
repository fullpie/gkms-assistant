"""Plan2 Initial Regular audition NPC/rank terminal provider.

The Plan2 card horizon owns the player's terminal score, but it does not own
the audition opponents.  ``ProduceExamBattleNpcGroup`` only supplies score
ranges and distribution weights for the Initial Regular stages whose
``isStaticNpcScore`` flag is false.  Concrete NPC scores, the effective NPC
multiplier, and their RNG identity therefore stay explicit scenario inputs.

Ranking follows Android ``ExamParameterModel.GetCurrentAuditionRank``: an NPC
outranks the player only when ``npc.CurrentScore > player.Parameter``.  A tie
therefore favours the player.  This module records that static contract; it
does not read or require Android extraction files at runtime.

The battle score curve is retained in the typed Master projection because it
is part of the selected audition config.  It is *not* used to manufacture NPC
scores.  Likewise, reward selection is emitted only from a complete
stage/number/rank rule supplied by the caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction
from functools import lru_cache
from math import isclose
from pathlib import Path
from typing import Any, Final

import yaml

from .audition_rules import (
    DEFAULT_MASTER_DIR,
    FINAL,
    MID1,
    MID2,
    load_audition_rules,
)
from .exam_context import AuditionDifficultyValues, calculate_audition_borders
from .initial_regular_audition_branch_catalog import (
    InitialRegularAuditionScenarioBranch,
)
from .initial_regular_inner_protocol import InitialRegularInnerStageKind
from .initial_regular_plan2_inner_adapter import (
    InitialRegularPlan2AuditionResolution,
    InitialRegularPlan2TerminalContext,
)
from .plan2_exam_mode import PLAN2_AUDITION_EXAM_TYPE


INITIAL_REGULAR_PLAN2_AUDITION_RUNTIME_SCHEMA_VERSION: Final = 1
INITIAL_REGULAR_PRODUCE_ID: Final = "produce-001"
ANDROID_AUDITION_RANK_PREDICATE: Final = "npc.CurrentScore > player.Parameter"

_STEP_TYPE_VALUES: Final = {MID1: 16, MID2: 17, FINAL: 18}
_PLAN2_PLAN_TYPES: Final = frozenset({"plan2", "ProducePlanType_Plan2"})
_UINT32_MAX: Final = 0xFFFFFFFF


def _text(value: object, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        qualifier = "text" if allow_empty else "non-empty text"
        raise ValueError(f"{label} must be {qualifier}")
    return value


def _plain_int(value: object, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


def _optional_uint32(value: int | None, label: str) -> None:
    if value is not None and (
        type(value) is not int or not 0 <= value <= _UINT32_MAX
    ):
        raise ValueError(f"{label} must be UInt32 or None")


@dataclass(frozen=True, slots=True, order=True)
class InitialRegularPlan2AuditionRuntimeIssue:
    code: str
    field: str
    detail: str = ""

    def __post_init__(self) -> None:
        _text(self.code, "issue.code")
        _text(self.field, "issue.field")
        if not isinstance(self.detail, str):
            raise TypeError("issue.detail must be text")


def _issue(
    code: str,
    field: str,
    detail: str = "",
) -> InitialRegularPlan2AuditionRuntimeIssue:
    return InitialRegularPlan2AuditionRuntimeIssue(code, field, detail)


class InitialRegularPlan2NpcScenarioAuthority(StrEnum):
    """Who supplied the already-resolved NPC terminal scores."""

    SERVER_RUNTIME = "server_runtime"
    MASTER_STATIC = "master_static"


@dataclass(frozen=True, slots=True)
class InitialRegularPlan2NpcMasterFact:
    """One numbered member of ``ProduceExamBattleNpcGroup``."""

    npc_group_id: str
    number: int
    character_id: str | None
    mob_id: str | None
    mob_asset_id: str | None
    mob_is_border: bool | None
    score_min: int
    score_max: int
    attribute_permille: tuple[int, int, int]
    phase_permille: tuple[int, int, int]

    def __post_init__(self) -> None:
        _text(self.npc_group_id, "npc_group_id")
        _plain_int(self.number, "npc.number", minimum=1)
        identities = tuple(
            value for value in (self.character_id, self.mob_id) if value
        )
        if len(identities) != 1:
            raise ValueError("NPC fact must have exactly one character or mob identity")
        if self.character_id is not None:
            _text(self.character_id, "npc.character_id")
            if self.mob_asset_id is not None or self.mob_is_border is not None:
                raise ValueError("character NPC must not carry mob metadata")
        else:
            assert self.mob_id is not None
            _text(self.mob_id, "npc.mob_id")
            if self.mob_asset_id is not None:
                _text(self.mob_asset_id, "npc.mob_asset_id")
            if not isinstance(self.mob_is_border, bool):
                raise TypeError("mob_is_border must be boolean for a mob NPC")
        _plain_int(self.score_min, "npc.score_min")
        _plain_int(self.score_max, "npc.score_max")
        if self.score_max < self.score_min:
            raise ValueError("npc score range is reversed")
        for name in ("attribute_permille", "phase_permille"):
            values = tuple(getattr(self, name))
            if len(values) != 3:
                raise ValueError(f"{name} must contain three values")
            for index, value in enumerate(values):
                _plain_int(value, f"{name}[{index}]")
            object.__setattr__(self, name, values)

    @property
    def opponent_id(self) -> str:
        return self.character_id or self.mob_id or ""


@dataclass(frozen=True, slots=True, order=True)
class InitialRegularPlan2BattleScorePoint:
    """One player battle-score curve point, not an NPC score formula."""

    parameter: int
    vocal_permille: int
    dance_permille: int
    visual_permille: int

    def __post_init__(self) -> None:
        for name in (
            "parameter",
            "vocal_permille",
            "dance_permille",
            "visual_permille",
        ):
            _plain_int(getattr(self, name), name)


@dataclass(frozen=True, slots=True)
class InitialRegularPlan2AuditionNpcCatalog:
    """Exact PC Master projection for one Initial Regular audition stage."""

    schema_version: int
    idol_card_id: str
    produce_id: str
    stage_type: str
    number: int
    stage_id: str
    setting_id: str
    npc_group_id: str
    battle_config_id: str
    score_config_id: str
    is_static_npc_score: bool
    step_type_value: int
    limit_turn: int
    clear_border: int
    limit_border: int
    npcs: tuple[InitialRegularPlan2NpcMasterFact, ...]
    score_curve: tuple[InitialRegularPlan2BattleScorePoint, ...]
    configured_parameter_values: tuple[int, int, int] | None = None
    score_penalty_permille: tuple[int, int] | None = None

    def __post_init__(self) -> None:
        if self.schema_version != INITIAL_REGULAR_PLAN2_AUDITION_RUNTIME_SCHEMA_VERSION:
            raise ValueError("unsupported Plan2 audition runtime schema")
        for name in (
            "idol_card_id",
            "produce_id",
            "stage_type",
            "stage_id",
            "setting_id",
            "npc_group_id",
            "battle_config_id",
            "score_config_id",
        ):
            _text(getattr(self, name), name)
        _plain_int(self.number, "number", minimum=1)
        if not isinstance(self.is_static_npc_score, bool):
            raise TypeError("is_static_npc_score must be boolean")
        _plain_int(self.step_type_value, "step_type_value")
        _plain_int(self.limit_turn, "limit_turn", minimum=1)
        _plain_int(self.clear_border, "clear_border", minimum=1)
        _plain_int(self.limit_border, "limit_border", minimum=-1)
        npcs = tuple(self.npcs)
        if not npcs or any(
            not isinstance(value, InitialRegularPlan2NpcMasterFact)
            for value in npcs
        ):
            raise TypeError("npcs must contain typed NPC facts")
        if tuple(value.number for value in npcs) != tuple(range(1, len(npcs) + 1)):
            raise ValueError("NPC numbers must be contiguous from one")
        if any(value.npc_group_id != self.npc_group_id for value in npcs):
            raise ValueError("NPC facts disagree with catalog group")
        curve = tuple(self.score_curve)
        if not curve or any(
            not isinstance(value, InitialRegularPlan2BattleScorePoint)
            for value in curve
        ):
            raise TypeError("score_curve must contain typed score points")
        if tuple(value.parameter for value in curve) != tuple(
            sorted({value.parameter for value in curve})
        ):
            raise ValueError("score curve parameters must be unique and sorted")
        object.__setattr__(self, "npcs", npcs)
        object.__setattr__(self, "score_curve", curve)
        if self.configured_parameter_values is not None:
            configured = tuple(self.configured_parameter_values)
            if len(configured) != 3:
                raise ValueError(
                    "configured_parameter_values must contain three values"
                )
            for index, value in enumerate(configured):
                _plain_int(
                    value,
                    f"configured_parameter_values[{index}]",
                    minimum=1,
                )
            object.__setattr__(self, "configured_parameter_values", configured)
        if self.score_penalty_permille is not None:
            penalties = tuple(self.score_penalty_permille)
            if len(penalties) != 2:
                raise ValueError(
                    "score_penalty_permille must contain minimum and maximum"
                )
            for index, value in enumerate(penalties):
                _plain_int(value, f"score_penalty_permille[{index}]")
            if penalties[1] < penalties[0]:
                raise ValueError("score penalty maximum is below minimum")
            object.__setattr__(self, "score_penalty_permille", penalties)

    @property
    def field_size(self) -> int:
        return len(self.npcs) + 1


@dataclass(frozen=True, slots=True)
class InitialRegularPlan2AuditionNpcCatalogResult:
    catalog: InitialRegularPlan2AuditionNpcCatalog | None
    issues: tuple[InitialRegularPlan2AuditionRuntimeIssue, ...] = ()

    def __post_init__(self) -> None:
        issues = tuple(self.issues)
        if any(
            not isinstance(value, InitialRegularPlan2AuditionRuntimeIssue)
            for value in issues
        ):
            raise TypeError("issues must contain typed audition runtime issues")
        if (self.catalog is None) == (not issues):
            raise ValueError("catalog result must be either ready or blocked")
        object.__setattr__(self, "issues", issues)

    @property
    def ready(self) -> bool:
        return self.catalog is not None and not self.issues


@dataclass(frozen=True, slots=True)
class InitialRegularPlan2NpcTerminalScore:
    """One exact terminal score keyed like ``ExamSaveData.npcDataList``."""

    npc_group_id: str
    number: int
    final_score: int
    turn_scores: tuple[int, ...] | None = None

    def __post_init__(self) -> None:
        _text(self.npc_group_id, "npc_group_id")
        _plain_int(self.number, "npc.number", minimum=1)
        _plain_int(self.final_score, "npc.final_score")
        if self.turn_scores is not None:
            scores = tuple(self.turn_scores)
            if not scores:
                raise ValueError("turn_scores must be non-empty or None")
            for index, value in enumerate(scores):
                _plain_int(value, f"turn_scores[{index}]")
            object.__setattr__(self, "turn_scores", scores)


@dataclass(frozen=True, slots=True)
class InitialRegularPlan2AuditionRewardRule:
    """Exact reward-selection decision for every possible stage rank."""

    stage_type: str
    number: int
    reward_requested_by_rank: tuple[bool, ...]

    def __post_init__(self) -> None:
        _text(self.stage_type, "reward_rule.stage_type")
        _plain_int(self.number, "reward_rule.number", minimum=1)
        values = tuple(self.reward_requested_by_rank)
        if not values or any(type(value) is not bool for value in values):
            raise TypeError("reward_requested_by_rank must contain booleans")
        object.__setattr__(self, "reward_requested_by_rank", values)

    def for_rank(self, rank: int) -> bool:
        _plain_int(rank, "rank", minimum=1)
        if rank > len(self.reward_requested_by_rank):
            raise IndexError("rank is not covered by the reward rule")
        return self.reward_requested_by_rank[rank - 1]


@dataclass(frozen=True, slots=True)
class InitialRegularPlan2AuditionScenario:
    """One exact start/terminal scenario shared by branch and rank helpers.

    Optional fields stay optional so incomplete server captures can be
    represented and returned as typed blockers.  A ready server-owned
    scenario needs all branch fields, an RNG token, the effective NPC
    multiplier, exact terminal scores, and a complete reward rule.
    """

    stage_type: str
    number: int
    npc_group_id: str
    authority: InitialRegularPlan2NpcScenarioAuthority
    probability: Fraction | None
    rng_before: int | None
    turn_parameter_types: tuple[int, ...] | None
    battle_bonus_permille: tuple[int, int, int] | None
    extra_turn: int | None
    is_static_npc_score: bool | None
    npc_score_multiple_permille: int | None
    npc_scores: tuple[InitialRegularPlan2NpcTerminalScore, ...] | None
    reward_rule: InitialRegularPlan2AuditionRewardRule | None
    rng_token: str | None = None
    branch_id: str | None = None

    def __post_init__(self) -> None:
        _text(self.stage_type, "scenario.stage_type")
        _plain_int(self.number, "scenario.number", minimum=1)
        _text(self.npc_group_id, "scenario.npc_group_id")
        if not isinstance(self.authority, InitialRegularPlan2NpcScenarioAuthority):
            raise TypeError("scenario.authority must be typed")
        if self.probability is not None:
            if type(self.probability) is not Fraction:
                raise TypeError("scenario.probability must be Fraction or None")
            if not 0 < self.probability <= 1:
                raise ValueError("scenario.probability must be in (0, 1]")
        _optional_uint32(self.rng_before, "scenario.rng_before")
        if self.turn_parameter_types is not None:
            values = tuple(self.turn_parameter_types)
            if any(type(value) is not int or value not in (1, 2, 3) for value in values):
                raise ValueError("turn_parameter_types must be Vocal/Dance/Visual")
            object.__setattr__(self, "turn_parameter_types", values)
        if self.battle_bonus_permille is not None:
            bonuses = tuple(self.battle_bonus_permille)
            if len(bonuses) != 3:
                raise ValueError("battle_bonus_permille must contain three values")
            for index, value in enumerate(bonuses):
                _plain_int(value, f"battle_bonus_permille[{index}]")
            object.__setattr__(self, "battle_bonus_permille", bonuses)
        if self.extra_turn is not None:
            _plain_int(self.extra_turn, "scenario.extra_turn")
        if self.is_static_npc_score is not None and type(self.is_static_npc_score) is not bool:
            raise TypeError("is_static_npc_score must be boolean or None")
        if self.npc_score_multiple_permille is not None:
            _plain_int(
                self.npc_score_multiple_permille,
                "npc_score_multiple_permille",
            )
        if self.npc_scores is not None:
            scores = tuple(self.npc_scores)
            if any(
                not isinstance(value, InitialRegularPlan2NpcTerminalScore)
                for value in scores
            ):
                raise TypeError("npc_scores must contain typed terminal scores")
            object.__setattr__(self, "npc_scores", scores)
        if self.reward_rule is not None and not isinstance(
            self.reward_rule, InitialRegularPlan2AuditionRewardRule
        ):
            raise TypeError("reward_rule must be typed or None")
        for name in ("rng_token", "branch_id"):
            value = getattr(self, name)
            if value is not None:
                _text(value, f"scenario.{name}")


@dataclass(frozen=True, slots=True)
class InitialRegularPlan2AuditionRuntimeResult:
    catalog: InitialRegularPlan2AuditionNpcCatalog | None
    resolution: InitialRegularPlan2AuditionResolution | None
    player_score: int | None
    npc_final_scores: tuple[int, ...]
    issues: tuple[InitialRegularPlan2AuditionRuntimeIssue, ...]
    trace: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.catalog is not None and not isinstance(
            self.catalog, InitialRegularPlan2AuditionNpcCatalog
        ):
            raise TypeError("catalog must be typed or None")
        if self.resolution is not None and not isinstance(
            self.resolution, InitialRegularPlan2AuditionResolution
        ):
            raise TypeError("resolution must be typed or None")
        if self.player_score is not None:
            _plain_int(self.player_score, "player_score")
        scores = tuple(self.npc_final_scores)
        for index, value in enumerate(scores):
            _plain_int(value, f"npc_final_scores[{index}]")
        issues = tuple(self.issues)
        if any(
            not isinstance(value, InitialRegularPlan2AuditionRuntimeIssue)
            for value in issues
        ):
            raise TypeError("issues must contain typed runtime issues")
        if (self.resolution is None) == (not issues):
            raise ValueError("runtime result must be either ready or blocked")
        object.__setattr__(self, "npc_final_scores", scores)
        object.__setattr__(self, "issues", issues)
        object.__setattr__(self, "trace", tuple(self.trace))

    @property
    def ready(self) -> bool:
        return self.resolution is not None and not self.issues


@dataclass(frozen=True, slots=True)
class InitialRegularPlan2AuditionScenarioBranchResult:
    branch: InitialRegularAuditionScenarioBranch | None
    issues: tuple[InitialRegularPlan2AuditionRuntimeIssue, ...]

    def __post_init__(self) -> None:
        issues = tuple(self.issues)
        if any(
            not isinstance(value, InitialRegularPlan2AuditionRuntimeIssue)
            for value in issues
        ):
            raise TypeError("issues must contain typed runtime issues")
        if (self.branch is None) == (not issues):
            raise ValueError("branch result must be either ready or blocked")
        object.__setattr__(self, "issues", issues)

    @property
    def ready(self) -> bool:
        return self.branch is not None and not self.issues


@lru_cache(maxsize=32)
def _master_rows(path: Path) -> tuple[dict[str, Any], ...]:
    if not path.is_file():
        raise FileNotFoundError(path)
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    payload = yaml.load(path.read_text(encoding="utf-8"), Loader=loader)
    if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
        raise ValueError(f"{path.name} must contain a list of objects")
    return tuple(payload)


def _row_text(row: dict[str, Any], field: str, *, allow_empty: bool = False) -> str:
    return _text(row.get(field), field, allow_empty=allow_empty)


def _row_int(row: dict[str, Any], field: str, *, minimum: int = 0) -> int:
    return _plain_int(row.get(field), field, minimum=minimum)


def load_initial_regular_plan2_audition_npc_catalog(
    idol_card_id: str,
    *,
    stage_type: str,
    number: int = 1,
    produce_id: str = INITIAL_REGULAR_PRODUCE_ID,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> InitialRegularPlan2AuditionNpcCatalogResult:
    """Load the selected stage, NPC group/mobs, and player score curve."""

    try:
        _text(idol_card_id, "idol_card_id")
        _text(stage_type, "stage_type")
        _plain_int(number, "number", minimum=1)
        _text(produce_id, "produce_id")
    except (TypeError, ValueError) as error:
        return InitialRegularPlan2AuditionNpcCatalogResult(
            None,
            (_issue("plan2-audition-catalog-input-invalid", "catalog", str(error)),),
        )
    directory = Path(master_dir).resolve()
    try:
        rules = load_audition_rules(
            idol_card_id,
            produce_id=produce_id,
            step_type=stage_type,
            number=number,
            master_dir=directory,
        )
        difficulty_rows = _master_rows(
            directory / "ProduceStepAuditionDifficulty.yaml"
        )
        exact_difficulty_id = f"p_step_audition_difficulty-{idol_card_id}"
        card_parts = idol_card_id.split("-")
        difficulty_ids = (exact_difficulty_id,)
        if len(card_parts) >= 3 and card_parts[0] == "i_card" and card_parts[1]:
            fallback_id = f"p_step_audition_difficulty-{card_parts[1]}"
            if fallback_id != exact_difficulty_id:
                difficulty_ids = (*difficulty_ids, fallback_id)
        difficulty: tuple[dict[str, Any], ...] = ()
        for difficulty_id in difficulty_ids:
            selected = tuple(
                row
                for row in difficulty_rows
                if row.get("id") == difficulty_id
                and row.get("produceId") == produce_id
                and row.get("stepType") == stage_type
                and row.get("number") == number
                and row.get("produceExamBattleNpcGroupId") == rules.npc_group_id
                and row.get("produceExamBattleConfigId") == rules.battle_config_id
            )
            if selected:
                difficulty = selected
                break
        if len(difficulty) != 1:
            raise KeyError(
                "difficulty identity is not unique: "
                f"{produce_id}/{stage_type}/{number}/{rules.npc_group_id}"
            )
        static_flag = difficulty[0].get("isStaticNpcScore")
        if type(static_flag) is not bool:
            raise ValueError("difficulty isStaticNpcScore must be boolean")

        group_rows = tuple(
            row
            for row in _master_rows(
                directory / "ProduceExamBattleNpcGroup.yaml"
            )
            if row.get("id") == rules.npc_group_id
        )
        if not group_rows:
            raise KeyError(f"NPC group is absent: {rules.npc_group_id}")
        numbers = tuple(_row_int(row, "number", minimum=1) for row in group_rows)
        if len(numbers) != len(set(numbers)):
            raise KeyError(f"NPC group repeats a number: {rules.npc_group_id}")
        if set(numbers) != set(range(1, len(group_rows) + 1)):
            raise ValueError(f"NPC group numbers are not contiguous: {rules.npc_group_id}")

        mob_rows = _master_rows(directory / "ProduceExamBattleNpcMob.yaml")
        mobs_by_id: dict[str, list[dict[str, Any]]] = {}
        for row in mob_rows:
            mob_id = _row_text(row, "id")
            mobs_by_id.setdefault(mob_id, []).append(row)

        npc_facts: list[InitialRegularPlan2NpcMasterFact] = []
        for row in sorted(group_rows, key=lambda value: _row_int(value, "number", minimum=1)):
            character_id = _row_text(row, "characterId", allow_empty=True) or None
            mob_id = _row_text(
                row, "produceExamBattleNpcMobId", allow_empty=True
            ) or None
            mob_asset_id: str | None = None
            mob_is_border: bool | None = None
            if mob_id is not None:
                selected_mobs = mobs_by_id.get(mob_id, [])
                if len(selected_mobs) != 1:
                    raise KeyError(f"NPC mob identity is not unique: {mob_id}")
                mob = selected_mobs[0]
                mob_asset_id = _row_text(mob, "assetId")
                raw_border = mob.get("isBorder")
                if type(raw_border) is not bool:
                    raise ValueError(f"NPC mob isBorder must be boolean: {mob_id}")
                mob_is_border = raw_border
            npc_facts.append(
                InitialRegularPlan2NpcMasterFact(
                    npc_group_id=rules.npc_group_id,
                    number=_row_int(row, "number", minimum=1),
                    character_id=character_id,
                    mob_id=mob_id,
                    mob_asset_id=mob_asset_id,
                    mob_is_border=mob_is_border,
                    score_min=_row_int(row, "scoreMin"),
                    score_max=_row_int(row, "scoreMax"),
                    attribute_permille=(
                        _row_int(row, "vocalPermil"),
                        _row_int(row, "dancePermil"),
                        _row_int(row, "visualPermil"),
                    ),
                    phase_permille=(
                        _row_int(row, "opScorePermil"),
                        _row_int(row, "midScorePermil"),
                        _row_int(row, "edScorePermil"),
                    ),
                )
            )

        score_rows = tuple(
            row
            for row in _master_rows(
                directory / "ProduceExamBattleScoreConfig.yaml"
            )
            if row.get("id") == rules.score_config_id
        )
        if not score_rows:
            raise KeyError(f"battle score config is absent: {rules.score_config_id}")
        score_curve = tuple(
            InitialRegularPlan2BattleScorePoint(
                parameter=_row_int(row, "parameter"),
                vocal_permille=_row_int(row, "vocalPermil"),
                dance_permille=_row_int(row, "dancePermil"),
                visual_permille=_row_int(row, "visualPermil"),
            )
            for row in sorted(score_rows, key=lambda value: _row_int(value, "parameter"))
        )
        setting_rows = _master_rows(directory / "Setting.yaml")
        if len(setting_rows) != 1:
            raise KeyError(f"Setting row is not unique: {len(setting_rows)}")
        score_penalties = (
            _row_int(
                setting_rows[0],
                "produceExamBattleScorePenaltyMinPermil",
            ),
            _row_int(
                setting_rows[0],
                "produceExamBattleScorePenaltyMaxPermil",
            ),
        )
        borders = calculate_audition_borders(
            AuditionDifficultyValues(rules.rank_threshold, rules.force_end_score)
        )
        catalog = InitialRegularPlan2AuditionNpcCatalog(
            schema_version=INITIAL_REGULAR_PLAN2_AUDITION_RUNTIME_SCHEMA_VERSION,
            idol_card_id=idol_card_id,
            produce_id=produce_id,
            stage_type=stage_type,
            number=number,
            stage_id=f"{rules.battle_config_id}:{stage_type}:{number}",
            setting_id=rules.exam_setting_id,
            npc_group_id=rules.npc_group_id,
            battle_config_id=rules.battle_config_id,
            score_config_id=rules.score_config_id,
            is_static_npc_score=static_flag,
            step_type_value=_STEP_TYPE_VALUES[stage_type],
            limit_turn=rules.turns,
            clear_border=borders.clear_border,
            limit_border=borders.limit_border,
            npcs=tuple(npc_facts),
            score_curve=score_curve,
            configured_parameter_values=(
                rules.vocal_parameter,
                rules.dance_parameter,
                rules.visual_parameter,
            ),
            score_penalty_permille=score_penalties,
        )
    except (FileNotFoundError, KeyError, TypeError, ValueError) as error:
        return InitialRegularPlan2AuditionNpcCatalogResult(
            None,
            (
                _issue(
                    "plan2-audition-master-unresolved",
                    "master_dir",
                    f"{type(error).__name__}:{error}",
                ),
            ),
        )
    return InitialRegularPlan2AuditionNpcCatalogResult(catalog, ())


def _append_mismatch(
    issues: list[InitialRegularPlan2AuditionRuntimeIssue],
    field: str,
    actual: object,
    expected: object,
) -> None:
    if actual != expected:
        issues.append(
            _issue(
                "plan2-audition-context-mismatch",
                field,
                f"expected={expected!r}:actual={actual!r}",
            )
        )


def _validate_context(
    context: InitialRegularPlan2TerminalContext,
    scenario: InitialRegularPlan2AuditionScenario,
    catalog: InitialRegularPlan2AuditionNpcCatalog,
) -> tuple[InitialRegularPlan2AuditionRuntimeIssue, ...]:
    issues: list[InitialRegularPlan2AuditionRuntimeIssue] = []
    request = context.request
    facts = context.facts
    if request.stage_kind is not InitialRegularInnerStageKind.AUDITION:
        issues.append(
            _issue(
                "plan2-audition-context-kind-invalid",
                "context.request.stage_kind",
            )
        )
    if request.plan_type not in _PLAN2_PLAN_TYPES:
        issues.append(
            _issue(
                "plan2-audition-context-plan-invalid",
                "context.request.plan_type",
                request.plan_type,
            )
        )
    expected = (
        ("request.stage_type", request.stage_type, catalog.stage_type),
        ("request.stage_id", request.stage_id, catalog.stage_id),
        ("request.setting_id", request.setting_id, catalog.setting_id),
        ("request.exam_type", request.exam_type, PLAN2_AUDITION_EXAM_TYPE),
        ("request.step_type_value", request.step_type_value, catalog.step_type_value),
        ("request.limit_turn", request.limit_turn, catalog.limit_turn),
        ("request.clear_border", request.clear_border, catalog.clear_border),
        ("request.limit_border", request.limit_border, catalog.limit_border),
        ("facts.stage_id", facts.stage_id, catalog.stage_id),
        ("facts.setting_id", facts.setting_id, catalog.setting_id),
        ("facts.exam_type", facts.exam_type, PLAN2_AUDITION_EXAM_TYPE),
        ("facts.step_type_value", facts.step_type_value, catalog.step_type_value),
        ("facts.limit_turn", facts.limit_turn, catalog.limit_turn),
        ("facts.clear_border", facts.clear_border, catalog.clear_border),
        ("facts.limit_border", facts.limit_border, catalog.limit_border),
        ("scenario.stage_type", scenario.stage_type, catalog.stage_type),
        ("scenario.number", scenario.number, catalog.number),
        ("scenario.npc_group_id", scenario.npc_group_id, catalog.npc_group_id),
    )
    for field, actual, wanted in expected:
        _append_mismatch(issues, field, actual, wanted)

    _append_mismatch(issues, "facts.plan_type", facts.plan_type, request.plan_type)
    _append_mismatch(issues, "facts.extra_turn", facts.extra_turn, request.extra_turn)
    _append_mismatch(
        issues,
        "facts.turn_parameter_types",
        facts.turn_parameter_types,
        request.turn_parameter_types,
    )
    _append_mismatch(
        issues,
        "facts.battle_bonus_permille",
        facts.battle_bonus_permille,
        request.battle_bonus_permille,
    )
    _append_mismatch(
        issues,
        "scenario.extra_turn",
        scenario.extra_turn,
        request.extra_turn,
    )
    _append_mismatch(
        issues,
        "scenario.turn_parameter_types",
        scenario.turn_parameter_types,
        request.turn_parameter_types,
    )
    _append_mismatch(
        issues,
        "scenario.battle_bonus_permille",
        scenario.battle_bonus_permille,
        request.battle_bonus_permille,
    )
    _append_mismatch(
        issues,
        "scenario.rng_before",
        scenario.rng_before,
        request.rng_before,
    )
    if scenario.probability is None:
        issues.append(
            _issue(
                "plan2-audition-scenario-field-unresolved",
                "scenario.probability",
            )
        )
    elif request.branch.probability is None or not isclose(
        request.branch.probability,
        float(scenario.probability),
        rel_tol=0.0,
        abs_tol=1e-12,
    ):
        issues.append(
            _issue(
                "plan2-audition-context-mismatch",
                "request.branch.probability",
                f"expected={scenario.probability}:actual={request.branch.probability}",
            )
        )
    if scenario.branch_id is not None:
        _append_mismatch(
            issues,
            "request.branch.branch_id",
            request.branch.branch_id,
            scenario.branch_id,
        )
    if scenario.authority is InitialRegularPlan2NpcScenarioAuthority.SERVER_RUNTIME:
        if scenario.rng_token is None:
            issues.append(
                _issue(
                    "plan2-audition-npc-rng-unresolved",
                    "scenario.rng_token",
                )
            )
        else:
            _append_mismatch(
                issues,
                "request.branch.rng_token",
                request.branch.rng_token,
                scenario.rng_token,
            )
    elif not catalog.is_static_npc_score:
        issues.append(
            _issue(
                "plan2-audition-master-static-authority-invalid",
                "scenario.authority",
                f"npc_group={catalog.npc_group_id}:isStaticNpcScore=false",
            )
        )
    if scenario.is_static_npc_score is None:
        issues.append(
            _issue(
                "plan2-audition-scenario-field-unresolved",
                "scenario.is_static_npc_score",
            )
        )
    else:
        _append_mismatch(
            issues,
            "scenario.is_static_npc_score",
            scenario.is_static_npc_score,
            catalog.is_static_npc_score,
        )
    if scenario.npc_score_multiple_permille is None:
        issues.append(
            _issue(
                "plan2-audition-npc-multiplier-unresolved",
                "scenario.npc_score_multiple_permille",
            )
        )
    total_turns = catalog.limit_turn + (scenario.extra_turn or 0)
    if scenario.turn_parameter_types is None:
        issues.append(
            _issue(
                "plan2-audition-scenario-field-unresolved",
                "scenario.turn_parameter_types",
            )
        )
    elif len(scenario.turn_parameter_types) != total_turns:
        issues.append(
            _issue(
                "plan2-audition-turn-schedule-length-mismatch",
                "scenario.turn_parameter_types",
                f"expected={total_turns}:actual={len(scenario.turn_parameter_types)}",
            )
        )
    if scenario.battle_bonus_permille is None:
        issues.append(
            _issue(
                "plan2-audition-scenario-field-unresolved",
                "scenario.battle_bonus_permille",
            )
        )
    if scenario.rng_before is None:
        issues.append(
            _issue(
                "plan2-audition-npc-rng-unresolved",
                "scenario.rng_before",
            )
        )
    if scenario.extra_turn is None:
        issues.append(
            _issue(
                "plan2-audition-scenario-field-unresolved",
                "scenario.extra_turn",
            )
        )
    if not context.run.completed or context.run.final_state is None:
        issues.append(
            _issue(
                "plan2-audition-terminal-context-incomplete",
                "context.run",
                getattr(context.run, "stop_reason", ""),
            )
        )
    return tuple(dict.fromkeys(issues))


def _resolved_npc_scores(
    scenario: InitialRegularPlan2AuditionScenario,
    catalog: InitialRegularPlan2AuditionNpcCatalog,
) -> tuple[
    tuple[int, ...],
    tuple[InitialRegularPlan2AuditionRuntimeIssue, ...],
]:
    if scenario.npc_scores is None:
        return (), (
            _issue(
                "plan2-audition-npc-scores-unresolved",
                "scenario.npc_scores",
            ),
        )
    tracks = tuple(scenario.npc_scores)
    issues: list[InitialRegularPlan2AuditionRuntimeIssue] = []
    if not tracks:
        issues.append(
            _issue(
                "plan2-audition-npc-scores-unresolved",
                "scenario.npc_scores",
            )
        )
        return (), tuple(issues)
    numbers = tuple(value.number for value in tracks)
    if len(numbers) != len(set(numbers)):
        issues.append(
            _issue(
                "plan2-audition-npc-number-duplicate",
                "scenario.npc_scores",
                repr(numbers),
            )
        )
    expected_numbers = tuple(value.number for value in catalog.npcs)
    if set(numbers) != set(expected_numbers):
        issues.append(
            _issue(
                "plan2-audition-npc-field-incomplete",
                "scenario.npc_scores",
                f"expected={expected_numbers}:actual={numbers}",
            )
        )
    total_turns = catalog.limit_turn + (scenario.extra_turn or 0)
    for index, track in enumerate(tracks):
        if track.npc_group_id != catalog.npc_group_id:
            issues.append(
                _issue(
                    "plan2-audition-npc-group-mismatch",
                    f"scenario.npc_scores[{index}].npc_group_id",
                    f"expected={catalog.npc_group_id}:actual={track.npc_group_id}",
                )
            )
        if track.turn_scores is not None and len(track.turn_scores) != total_turns:
            issues.append(
                _issue(
                    "plan2-audition-npc-turn-scores-length-mismatch",
                    f"scenario.npc_scores[{index}].turn_scores",
                    f"expected={total_turns}:actual={len(track.turn_scores)}",
                )
            )
    if issues:
        return (), tuple(dict.fromkeys(issues))
    by_number = {value.number: value.final_score for value in tracks}
    return tuple(by_number[number] for number in expected_numbers), ()


def resolve_initial_regular_plan2_audition(
    context: InitialRegularPlan2TerminalContext,
    scenario: InitialRegularPlan2AuditionScenario,
    *,
    catalog: InitialRegularPlan2AuditionNpcCatalog | None = None,
    master_dir: Path = DEFAULT_MASTER_DIR,
    produce_id: str = INITIAL_REGULAR_PRODUCE_ID,
) -> InitialRegularPlan2AuditionRuntimeResult:
    """Compute terminal rank without regenerating server-owned NPC state."""

    if not isinstance(context, InitialRegularPlan2TerminalContext):
        raise TypeError("context must be InitialRegularPlan2TerminalContext")
    if not isinstance(scenario, InitialRegularPlan2AuditionScenario):
        raise TypeError("scenario must be InitialRegularPlan2AuditionScenario")
    selected_catalog = catalog
    if selected_catalog is None:
        idol_card_id = context.request.idol_card_id
        if not idol_card_id:
            return InitialRegularPlan2AuditionRuntimeResult(
                None,
                None,
                None,
                (),
                (
                    _issue(
                        "plan2-audition-idol-card-unresolved",
                        "context.request.idol_card_id",
                    ),
                ),
            )
        loaded = load_initial_regular_plan2_audition_npc_catalog(
            idol_card_id,
            stage_type=scenario.stage_type,
            number=scenario.number,
            produce_id=produce_id,
            master_dir=master_dir,
        )
        if not loaded.ready:
            return InitialRegularPlan2AuditionRuntimeResult(
                None,
                None,
                None,
                (),
                loaded.issues,
            )
        selected_catalog = loaded.catalog
    assert selected_catalog is not None
    if catalog is not None and context.request.idol_card_id != catalog.idol_card_id:
        return InitialRegularPlan2AuditionRuntimeResult(
            catalog,
            None,
            None,
            (),
            (
                _issue(
                    "plan2-audition-context-mismatch",
                    "context.request.idol_card_id",
                    f"expected={catalog.idol_card_id}:actual={context.request.idol_card_id}",
                ),
            ),
        )
    context_issues = _validate_context(context, scenario, selected_catalog)
    npc_scores, npc_issues = _resolved_npc_scores(scenario, selected_catalog)
    issues = list(context_issues)
    issues.extend(npc_issues)
    reward_rule = scenario.reward_rule
    if reward_rule is None:
        issues.append(
            _issue(
                "plan2-audition-reward-rule-unresolved",
                "scenario.reward_rule",
            )
        )
    else:
        _append_mismatch(
            issues,
            "scenario.reward_rule.stage_type",
            reward_rule.stage_type,
            selected_catalog.stage_type,
        )
        _append_mismatch(
            issues,
            "scenario.reward_rule.number",
            reward_rule.number,
            selected_catalog.number,
        )
        if len(reward_rule.reward_requested_by_rank) != selected_catalog.field_size:
            issues.append(
                _issue(
                    "plan2-audition-reward-rule-incomplete",
                    "scenario.reward_rule.reward_requested_by_rank",
                    (
                        f"expected={selected_catalog.field_size}:"
                        f"actual={len(reward_rule.reward_requested_by_rank)}"
                    ),
                )
            )
    if issues:
        return InitialRegularPlan2AuditionRuntimeResult(
            selected_catalog,
            None,
            None,
            npc_scores,
            tuple(dict.fromkeys(issues)),
        )
    assert context.run.final_state is not None
    player_score = context.run.final_state.scalar.score
    if type(player_score) is not int or player_score < 0:
        return InitialRegularPlan2AuditionRuntimeResult(
            selected_catalog,
            None,
            None,
            npc_scores,
            (
                _issue(
                    "plan2-audition-player-score-invalid",
                    "context.final_state.scalar.score",
                    repr(player_score),
                ),
            ),
        )
    rank = 1 + sum(score > player_score for score in npc_scores)
    assert reward_rule is not None
    reward_requested = reward_rule.for_rank(rank)
    if reward_requested and rank > selected_catalog.clear_border:
        return InitialRegularPlan2AuditionRuntimeResult(
            selected_catalog,
            None,
            player_score,
            npc_scores,
            (
                _issue(
                    "plan2-audition-reward-on-uncleared-rank",
                    f"scenario.reward_rule.reward_requested_by_rank[{rank - 1}]",
                    f"rank={rank}:clear_border={selected_catalog.clear_border}",
                ),
            ),
        )
    return InitialRegularPlan2AuditionRuntimeResult(
        selected_catalog,
        InitialRegularPlan2AuditionResolution(
            rank=rank,
            reward_requested=reward_requested,
        ),
        player_score,
        npc_scores,
        (),
        (
            f"rank-predicate:{ANDROID_AUDITION_RANK_PREDICATE}",
            f"player-score:{player_score}",
            f"npc-scores:{','.join(str(value) for value in npc_scores)}",
            f"rank:{rank}",
            f"npc-authority:{scenario.authority.value}",
            f"npc-score-multiple-permille:{scenario.npc_score_multiple_permille}",
        ),
    )


@dataclass(frozen=True, slots=True)
class InitialRegularPlan2AuditionRuntimeProvider:
    """Callable adapter provider plus a typed preflight result surface."""

    scenario: InitialRegularPlan2AuditionScenario
    catalog: InitialRegularPlan2AuditionNpcCatalog | None = None
    master_dir: Path = DEFAULT_MASTER_DIR
    produce_id: str = INITIAL_REGULAR_PRODUCE_ID

    def __post_init__(self) -> None:
        if not isinstance(self.scenario, InitialRegularPlan2AuditionScenario):
            raise TypeError("scenario must be typed")
        if self.catalog is not None and not isinstance(
            self.catalog, InitialRegularPlan2AuditionNpcCatalog
        ):
            raise TypeError("catalog must be typed or None")
        object.__setattr__(self, "master_dir", Path(self.master_dir))
        _text(self.produce_id, "produce_id")

    def resolve(
        self,
        context: InitialRegularPlan2TerminalContext,
    ) -> InitialRegularPlan2AuditionRuntimeResult:
        return resolve_initial_regular_plan2_audition(
            context,
            self.scenario,
            catalog=self.catalog,
            master_dir=self.master_dir,
            produce_id=self.produce_id,
        )

    def __call__(
        self,
        context: InitialRegularPlan2TerminalContext,
    ) -> InitialRegularPlan2AuditionResolution | None:
        """Match ``Plan2AuditionResolutionProvider`` and fail closed."""

        return self.resolve(context).resolution


def build_initial_regular_plan2_audition_scenario_branch(
    scenario: InitialRegularPlan2AuditionScenario,
    *,
    idol_card_id: str,
    catalog: InitialRegularPlan2AuditionNpcCatalog | None = None,
    master_dir: Path = DEFAULT_MASTER_DIR,
    produce_id: str = INITIAL_REGULAR_PRODUCE_ID,
) -> InitialRegularPlan2AuditionScenarioBranchResult:
    """Project an exact schedule into the common audition frontier input."""

    if not isinstance(scenario, InitialRegularPlan2AuditionScenario):
        raise TypeError("scenario must be InitialRegularPlan2AuditionScenario")
    selected_catalog = catalog
    if selected_catalog is None:
        loaded = load_initial_regular_plan2_audition_npc_catalog(
            idol_card_id,
            stage_type=scenario.stage_type,
            number=scenario.number,
            produce_id=produce_id,
            master_dir=master_dir,
        )
        if not loaded.ready:
            return InitialRegularPlan2AuditionScenarioBranchResult(
                None, loaded.issues
            )
        selected_catalog = loaded.catalog
    assert selected_catalog is not None
    issues: list[InitialRegularPlan2AuditionRuntimeIssue] = []
    if selected_catalog.idol_card_id != idol_card_id:
        issues.append(
            _issue(
                "plan2-audition-catalog-idol-card-mismatch",
                "idol_card_id",
                f"expected={selected_catalog.idol_card_id}:actual={idol_card_id}",
            )
        )
    for field, actual, expected in (
        ("scenario.stage_type", scenario.stage_type, selected_catalog.stage_type),
        ("scenario.number", scenario.number, selected_catalog.number),
        (
            "scenario.npc_group_id",
            scenario.npc_group_id,
            selected_catalog.npc_group_id,
        ),
        (
            "scenario.is_static_npc_score",
            scenario.is_static_npc_score,
            selected_catalog.is_static_npc_score,
        ),
    ):
        _append_mismatch(issues, field, actual, expected)
    for field in (
        "probability",
        "rng_before",
        "turn_parameter_types",
        "battle_bonus_permille",
        "extra_turn",
    ):
        if getattr(scenario, field) is None:
            issues.append(
                _issue(
                    "plan2-audition-scenario-field-unresolved",
                    f"scenario.{field}",
                )
            )
    if (
        scenario.authority is InitialRegularPlan2NpcScenarioAuthority.SERVER_RUNTIME
        and scenario.rng_token is None
    ):
        issues.append(
            _issue(
                "plan2-audition-npc-rng-unresolved",
                "scenario.rng_token",
            )
        )
    if (
        scenario.extra_turn is not None
        and scenario.turn_parameter_types is not None
        and len(scenario.turn_parameter_types)
        != selected_catalog.limit_turn + scenario.extra_turn
    ):
        issues.append(
            _issue(
                "plan2-audition-turn-schedule-length-mismatch",
                "scenario.turn_parameter_types",
                (
                    f"expected={selected_catalog.limit_turn + scenario.extra_turn}:"
                    f"actual={len(scenario.turn_parameter_types)}"
                ),
            )
        )
    if issues:
        return InitialRegularPlan2AuditionScenarioBranchResult(
            None, tuple(dict.fromkeys(issues))
        )
    assert scenario.probability is not None
    assert scenario.rng_before is not None
    assert scenario.turn_parameter_types is not None
    assert scenario.battle_bonus_permille is not None
    assert scenario.extra_turn is not None
    return InitialRegularPlan2AuditionScenarioBranchResult(
        InitialRegularAuditionScenarioBranch(
            probability=scenario.probability,
            number=scenario.number,
            rng_before=scenario.rng_before,
            turn_parameter_types=scenario.turn_parameter_types,
            battle_bonus_permille=scenario.battle_bonus_permille,
            extra_turn=scenario.extra_turn,
            rng_token=scenario.rng_token,
            branch_id=scenario.branch_id,
        ),
        (),
    )


__all__ = [
    "ANDROID_AUDITION_RANK_PREDICATE",
    "INITIAL_REGULAR_PLAN2_AUDITION_RUNTIME_SCHEMA_VERSION",
    "InitialRegularPlan2AuditionNpcCatalog",
    "InitialRegularPlan2AuditionNpcCatalogResult",
    "InitialRegularPlan2AuditionRewardRule",
    "InitialRegularPlan2AuditionRuntimeIssue",
    "InitialRegularPlan2AuditionRuntimeProvider",
    "InitialRegularPlan2AuditionRuntimeResult",
    "InitialRegularPlan2AuditionScenario",
    "InitialRegularPlan2AuditionScenarioBranchResult",
    "InitialRegularPlan2BattleScorePoint",
    "InitialRegularPlan2NpcMasterFact",
    "InitialRegularPlan2NpcScenarioAuthority",
    "InitialRegularPlan2NpcTerminalScore",
    "build_initial_regular_plan2_audition_scenario_branch",
    "load_initial_regular_plan2_audition_npc_catalog",
    "resolve_initial_regular_plan2_audition",
]
