"""Load one exact audition stage from the extracted local Master tables."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
from .application_paths import master_directory
DEFAULT_MASTER_DIR = master_directory()
MID1 = "ProduceStepType_AuditionMid1"
MID2 = "ProduceStepType_AuditionMid2"
FINAL = "ProduceStepType_AuditionFinal"
_STEP_ORDER = {MID1: 1, MID2: 2, FINAL: 3}


def _difficulty_stage_ids(idol_card_id: str) -> tuple[str, ...]:
    """Return exact-card then legacy character-level difficulty ids."""

    exact = f"p_step_audition_difficulty-{idol_card_id}"
    parts = idol_card_id.split("-")
    if len(parts) >= 3 and parts[0] == "i_card" and parts[1]:
        character = f"p_step_audition_difficulty-{parts[1]}"
        if character != exact:
            return exact, character
    return (exact,)


@dataclass(frozen=True, slots=True)
class AuditionRules:
    idol_card_id: str
    produce_id: str
    mode_name: str
    step_type: str
    number: int
    rank_threshold: int
    parameter_base_line: int
    base_score: int
    force_end_score: int
    npc_group_id: str
    battle_config_id: str
    gimmick_group_id: str
    turns: int
    vocal_parameter: int
    dance_parameter: int
    visual_parameter: int
    score_config_id: str
    exam_setting_id: str
    turn_end_stamina_recovery: int
    hand_limit: int
    turn_start_distribute: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @property
    def stage_key(self) -> tuple[str, int]:
        return self.step_type, self.number

    @property
    def is_final(self) -> bool:
        return self.step_type == FINAL


@lru_cache(maxsize=16)
def _load_rows(path: Path) -> tuple[dict[str, Any], ...]:
    if not path.is_file():
        raise FileNotFoundError(f"找不到 Master 表：{path}")
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    payload = yaml.load(path.read_text(encoding="utf-8"), Loader=loader)
    if not isinstance(payload, list):
        raise ValueError(f"Master 表不是 list：{path.name}")
    return tuple(row for row in payload if isinstance(row, dict))


def _one(rows: tuple[dict[str, Any], ...], label: str, predicate: object) -> dict[str, Any]:
    selected = [row for row in rows if predicate(row)]  # type: ignore[operator]
    if len(selected) != 1:
        raise KeyError(f"{label} 預期一筆，實際 {len(selected)} 筆")
    return selected[0]


def load_audition_rules(
    idol_card_id: str,
    *,
    produce_id: str = "produce-001",
    step_type: str = MID1,
    number: int = 1,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> AuditionRules:
    """Resolve the repeated-ID row using its real composite key.

    ``ProduceStepAuditionDifficulty.id`` is intentionally repeated across
    modes and stages.  The safe key is id + produceId + stepType + number.
    """

    directory = master_dir.resolve()
    difficulty_rows = _load_rows(
        directory / "ProduceStepAuditionDifficulty.yaml"
    )
    stage = None
    stage_id = ""
    for candidate_id in _difficulty_stage_ids(idol_card_id):
        selected = [
            row
            for row in difficulty_rows
            if (
                row.get("id") == candidate_id
                and row.get("produceId") == produce_id
                and row.get("stepType") == step_type
                and int(row.get("number", 0)) == number
            )
        ]
        if len(selected) > 1:
            raise KeyError(
                "audition difficulty did not resolve uniquely: "
                f"{candidate_id} / {produce_id} / {step_type} / {number}"
            )
        if selected:
            stage = selected[0]
            stage_id = candidate_id
            break
    if stage is None:
        raise KeyError(
            "audition difficulty was not found for exact card or character: "
            f"{idol_card_id} / {produce_id} / {step_type} / {number}"
        )
    config_id = str(stage.get("produceExamBattleConfigId", ""))
    config = _one(
        _load_rows(directory / "ProduceExamBattleConfig.yaml"),
        "audition battle config",
        lambda row: row.get("id") == config_id,
    )
    produce = _one(
        _load_rows(directory / "Produce.yaml"),
        "produce mode",
        lambda row: row.get("id") == produce_id,
    )
    setting_id = str(produce.get("examSettingId", ""))
    setting = _one(
        _load_rows(directory / "ExamSetting.yaml"),
        "exam setting",
        lambda row: row.get("id") == setting_id,
    )

    rules = AuditionRules(
        idol_card_id=idol_card_id,
        produce_id=produce_id,
        mode_name=str(produce.get("name", "")),
        step_type=step_type,
        number=number,
        rank_threshold=int(stage.get("rankThreshold", 0)),
        parameter_base_line=int(stage.get("parameterBaseLine", 0)),
        base_score=int(stage.get("baseScore", 0)),
        force_end_score=int(stage.get("forceEndScore", 0)),
        npc_group_id=str(stage.get("produceExamBattleNpcGroupId", "")),
        battle_config_id=config_id,
        gimmick_group_id=str(stage.get("produceExamGimmickEffectGroupId", "")),
        turns=int(config.get("turn", 0)),
        vocal_parameter=int(config.get("vocal", 0)),
        dance_parameter=int(config.get("dance", 0)),
        visual_parameter=int(config.get("visual", 0)),
        score_config_id=str(config.get("produceExamBattleScoreConfigId", "")),
        exam_setting_id=setting_id,
        turn_end_stamina_recovery=int(
            setting.get("examTurnEndRecoveryStamina", 0)
        ),
        hand_limit=int(setting.get("handLimit", 0)),
        turn_start_distribute=int(setting.get("turnStartDistribute", 0)),
    )
    if rules.turns < 1:
        raise ValueError(f"考試回合數無效：{rules.battle_config_id}")
    if rules.rank_threshold < 1:
        raise ValueError(f"排名門檻無效：{stage_id}")
    return rules


def list_audition_rules(
    idol_card_id: str,
    *,
    produce_id: str = "produce-001",
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> tuple[AuditionRules, ...]:
    """Return every mid/final stage variant for one run in lifecycle order.

    N.I.A has several difficulty-number variants for the same lifecycle stage;
    they are returned separately because choosing one without observing the UI
    would silently bind the solver to the wrong battle configuration.
    """

    directory = master_dir.resolve()
    all_rows = _load_rows(directory / "ProduceStepAuditionDifficulty.yaml")
    selected_rows: tuple[dict[str, Any], ...] = ()
    for stage_id in _difficulty_stage_ids(idol_card_id):
        selected_rows = tuple(
            row
            for row in all_rows
            if row.get("id") == stage_id and row.get("produceId") == produce_id
        )
        if selected_rows:
            break
    identities = {
        (str(row.get("stepType", "")), int(row.get("number", 0)))
        for row in selected_rows
    }
    identities = {
        identity
        for identity in identities
        if identity[0] in _STEP_ORDER and identity[1] >= 1
    }
    return tuple(
        load_audition_rules(
            idol_card_id,
            produce_id=produce_id,
            step_type=step_type,
            number=number,
            master_dir=directory,
        )
        for step_type, number in sorted(
            identities,
            key=lambda identity: (_STEP_ORDER[identity[0]], identity[1]),
        )
    )


def next_audition_stage_options(
    idol_card_id: str,
    *,
    produce_id: str,
    current_step_type: str,
    current_number: int,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> tuple[AuditionRules, ...]:
    """Return all variants of the lifecycle stage after ``current``.

    An empty tuple means the current stage is the final audition.  More than
    one result means the UI must identify which difficulty was selected.
    """

    rules = list_audition_rules(
        idol_card_id,
        produce_id=produce_id,
        master_dir=master_dir,
    )
    current = next(
        (
            item
            for item in rules
            if item.step_type == current_step_type
            and item.number == current_number
        ),
        None,
    )
    if current is None:
        raise KeyError(
            "找不到目前演出階段："
            f"{produce_id} / {current_step_type} / {current_number}"
        )
    current_order = _STEP_ORDER[current.step_type]
    later_orders = sorted(
        {
            _STEP_ORDER[item.step_type]
            for item in rules
            if _STEP_ORDER[item.step_type] > current_order
        }
    )
    if not later_orders:
        return ()
    next_order = later_orders[0]
    return tuple(
        item for item in rules if _STEP_ORDER[item.step_type] == next_order
    )
