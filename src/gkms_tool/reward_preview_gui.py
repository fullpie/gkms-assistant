"""Tk-free presentation model for the read-only reward preview workflow."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class RewardWorkflowRow:
    slot: int
    card_id: str
    upgrade: int
    display_name: str
    detail: str
    status: str


@dataclass(frozen=True, slots=True)
class RewardWorkflowView:
    stage: str
    status: str
    state: str
    cards_label: str
    recommendation: str
    confidence: str
    reasons: tuple[str, ...]
    rows: tuple[RewardWorkflowRow, ...]
    capture: Mapping[str, Any]


def should_reset_reward_preview_workflow(
    update_kind: str,
    payload: object | None = None,
) -> bool:
    """Keep state only for status polling or its own successful next frame."""

    del payload
    return update_kind not in {"reward", "status"}


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"reward workflow {label} is missing")
    return value


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"reward workflow {label} is invalid")
    return value


def _plan_label(plan_type: str) -> str:
    prefix = "ProducePlanType_"
    return plan_type[len(prefix) :] if plan_type.startswith(prefix) else plan_type


def _confirmed_summary(confirmed: tuple[Mapping[str, Any], ...]) -> str:
    if not confirmed:
        return "已確定：無"
    return "已確定：" + "、".join(
        f"#{_integer(item.get('slot'), 'confirmed slot')} "
        f"{str(item.get('display_name', '未命名'))}"
        for item in confirmed
    )


def format_reward_preview_workflow(
    result: Mapping[str, Any],
) -> RewardWorkflowView:
    """Validate and format one workflow result without creating GUI actions."""

    if not isinstance(result, Mapping):
        raise TypeError("reward workflow result must be a mapping")
    forbidden = tuple(
        key
        for key in ("action", "collect_action", "confirm_action", "click")
        if key in result
    )
    if forbidden:
        raise ValueError(
            "read-only reward workflow unexpectedly contains executable fields: "
            + ", ".join(forbidden)
        )
    if result.get("collect_allowed") is not False:
        raise ValueError("read-only reward workflow must forbid collection")
    stage = str(result.get("stage", ""))
    if stage not in {"needs_preview", "identified"}:
        raise ValueError(f"unknown reward workflow stage: {stage!r}")
    identity = _mapping(result.get("identity"), "identity")
    plan_type = str(identity.get("plan_type", ""))
    if not plan_type:
        raise ValueError("reward workflow active plan is missing")
    progress = _mapping(result.get("progress"), "progress")
    total_slots = _integer(progress.get("total_slots"), "total slot count")
    if total_slots not in {3, 4}:
        raise ValueError("reward workflow must contain three or four slots")
    confirmed_count = _integer(
        progress.get("confirmed_slots"), "confirmed slot count"
    )
    previewed = progress.get("previewed_slots")
    ambiguous = progress.get("ambiguous_slots")
    unresolved = progress.get("unresolved_slots")
    if (
        not isinstance(previewed, list)
        or not isinstance(ambiguous, list)
        or not isinstance(unresolved, list)
    ):
        raise ValueError("reward workflow preview progress is invalid")
    confirmed_value = result.get("confirmed")
    if not isinstance(confirmed_value, list):
        raise ValueError("reward workflow confirmed identities are missing")
    confirmed = tuple(_mapping(item, "confirmed identity") for item in confirmed_value)
    if len(confirmed) != confirmed_count:
        raise ValueError("reward workflow confirmed count changed")
    confirmed_slots = tuple(
        _integer(item.get("slot"), "confirmed slot") for item in confirmed
    )
    if confirmed_slots != tuple(sorted(set(confirmed_slots))):
        raise ValueError("reward workflow confirmed slots are duplicated or unordered")
    if any(slot < 1 or slot > total_slots for slot in confirmed_slots):
        raise ValueError("reward workflow confirmed slot is outside the layout")
    confidence = (
        f"已確定 {confirmed_count}/{total_slots}｜"
        f"完整預覽 {len(previewed)}/{len(ambiguous)}"
    )
    capture = _mapping(result.get("capture"), "capture")
    method = str(result.get("method", ""))
    reason = str(result.get("reason", ""))
    plan_label = _plan_label(plan_type)

    if stage == "needs_preview":
        if progress.get("complete") is not False or not unresolved:
            raise ValueError("incomplete reward workflow progress is inconsistent")
        probe = _mapping(result.get("next_probe"), "next preview probe")
        next_slot = _integer(probe.get("slot"), "next preview slot")
        if next_slot not in unresolved:
            raise ValueError("next reward preview slot is not unresolved")
        if probe.get("reversible") is not True or probe.get("collect_allowed") is not False:
            raise ValueError("next reward preview probe is not read-only and reversible")
        rows = tuple(
            RewardWorkflowRow(
                slot=_integer(item.get("slot"), "confirmed slot"),
                card_id=str(item.get("card_id", "")),
                upgrade=_integer(item.get("upgrade"), "confirmed upgrade"),
                display_name=str(item.get("display_name", "")),
                detail=(
                    "完整預覽標題"
                    if item.get("match_method")
                    == "exact-localized-master-title"
                    else "高信心縮圖"
                ),
                status="已確定",
            )
            for item in confirmed
        )
        return RewardWorkflowView(
            stage=stage,
            status=f"課後獎勵｜{plan_label}｜需要完整預覽",
            state=_confirmed_summary(confirmed),
            cards_label="已確定槽位（未確定槽位不排名）",
            recommendation=(
                f"下一步：請以可逆選取查看槽位 #{next_slot}；"
                "取得新畫面後再按「讀取技能卡獎勵」。"
            ),
            confidence=confidence,
            reasons=tuple(
                item
                for item in (
                    method,
                    reason,
                    "只讀辨識：選取由使用者或外層 MAA 完成；不會確認或領取。",
                )
                if item
            ),
            rows=rows,
            capture=dict(capture),
        )

    if result.get("next_probe") is not None:
        raise ValueError("identified reward workflow still requests a preview")
    if (
        progress.get("complete") is not True
        or unresolved
        or confirmed_count != total_slots
    ):
        raise ValueError("identified reward workflow progress is inconsistent")
    ranking_value = result.get("ranking")
    if not isinstance(ranking_value, list) or not ranking_value:
        raise ValueError("identified reward workflow ranking is missing")
    ranking = tuple(_mapping(item, "ranking row") for item in ranking_value)
    expected_ranks = tuple(range(1, len(ranking) + 1))
    observed_ranks = tuple(
        _integer(item.get("rank"), "ranking position") for item in ranking
    )
    if observed_ranks != expected_ranks:
        raise ValueError("reward workflow ranking positions are invalid")
    ranked_slots = tuple(
        _integer(item.get("slot"), "ranking slot") for item in ranking
    )
    if len(ranked_slots) != total_slots or len(set(ranked_slots)) != total_slots:
        raise ValueError("reward workflow ranking does not cover every slot")
    if set(ranked_slots) != set(confirmed_slots):
        raise ValueError("reward workflow ranking and confirmed slots differ")
    first = ranking[0]
    rows = tuple(
        RewardWorkflowRow(
            slot=_integer(item.get("slot"), "ranking slot"),
            card_id=str(item.get("card_id", "")),
            upgrade=_integer(item.get("upgrade"), "ranking upgrade"),
            display_name=str(item.get("display_name", "")),
            detail=(
                f"第 {_integer(item.get('rank'), 'ranking position')} 名｜"
                f"Master 評價 {_integer(item.get('evaluation'), 'evaluation')}"
            ),
            status=(
                "排名第一"
                if _integer(item.get("rank"), "ranking position") == 1
                else "已辨識"
            ),
        )
        for item in ranking
    )
    rank_reasons = tuple(
        f"#{_integer(item.get('slot'), 'ranking slot')} "
        f"{str(item.get('display_name', ''))}："
        f"{str(item.get('rank_reason', ''))}"
        for item in ranking
    )
    return RewardWorkflowView(
        stage=stage,
        status=f"課後獎勵｜{plan_label}｜辨識完成（只讀）",
        state=_confirmed_summary(confirmed),
        cards_label="Master 排名（只讀，不提供領取）",
        recommendation=(
            f"Master 排名第一："
            f"#{_integer(first.get('slot'), 'ranking slot')} "
            f"{str(first.get('display_name', ''))}"
            f"（評價 {_integer(first.get('evaluation'), 'evaluation')}）"
        ),
        confidence=confidence,
        reasons=tuple(
            item
            for item in (
                method,
                reason,
                *rank_reasons,
                "辨識已完成；本頁不會建立確認或領取動作。",
            )
            if item
        ),
        rows=rows,
        capture=dict(capture),
    )
