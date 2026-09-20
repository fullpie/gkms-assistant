"""Plan2/Common boundary for the shared ``ExamCardUpgrade`` primitive.

The recovered Android executor stores the effect payload only; it has no plan
field.  The typed GUID/search/RNG/temporary-upgrade implementation already
lives in :mod:`gkms_tool.plan3_card_upgrade`, so this module intentionally
contains only a plan boundary, a small Master-row loader, and aliases to that
implementation.  It does not introduce another state model or copy the
mutation algorithm.

Rows without plan metadata are accepted because the Master effect payload is
shared.  A supplied plan tag must be Common or Plan2.  Unsupported plan tags,
unknown rows, unsupported search shapes, unsettled Playing, and unresolved
runtime lineage remain typed pauses from the shared resolver/executor.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path
import sqlite3
from typing import TypeAlias

from .master_db import DEFAULT_DATABASE
from .plan3_card_upgrade import (
    EFFECT_EXAM_CARD_UPGRADE,
    Plan3CardUpgradeBranch,
    Plan3CardUpgradeCandidate,
    Plan3CardUpgradeCandidateResult,
    Plan3CardUpgradeContract,
    Plan3CardUpgradeMutationTrace,
    Plan3CardUpgradePause,
    Plan3CardUpgradeResolution,
    Plan3CardUpgradeResult,
    Plan3CardUpgradeResultStatus,
    Plan3CardUpgradeSelectionRequest,
    Plan3NativeUpgradeEvidence,
    execute_plan3_card_upgrade,
    resolve_plan3_card_upgrade,
    resolve_plan3_card_upgrade_candidates,
)
from .plan3_native_state import Plan3NativeCard, Plan3NativeState


PLAN_COMMON = "ProducePlanType_Common"
PLAN2 = "ProducePlanType_Plan2"
PLAN2_COMMON_PLAN_TYPES = frozenset((PLAN_COMMON, PLAN2))


# These aliases make the standalone surface explicit without creating a
# Plan2-specific state/result implementation.
Plan2CardUpgradeState: TypeAlias = Plan3NativeState
Plan2CardUpgradeCard: TypeAlias = Plan3NativeCard
Plan2CardUpgradeBranch: TypeAlias = Plan3CardUpgradeBranch
Plan2CardUpgradeCandidate: TypeAlias = Plan3CardUpgradeCandidate
Plan2CardUpgradeCandidateResult: TypeAlias = Plan3CardUpgradeCandidateResult
Plan2CardUpgradeContract: TypeAlias = Plan3CardUpgradeContract
Plan2CardUpgradeMutationTrace: TypeAlias = Plan3CardUpgradeMutationTrace
Plan2CardUpgradePause: TypeAlias = Plan3CardUpgradePause
Plan2CardUpgradeResolution: TypeAlias = Plan3CardUpgradeResolution
Plan2CardUpgradeResult: TypeAlias = Plan3CardUpgradeResult
Plan2CardUpgradeResultStatus: TypeAlias = Plan3CardUpgradeResultStatus
Plan2CardUpgradeSelectionRequest: TypeAlias = Plan3CardUpgradeSelectionRequest
Plan2NativeUpgradeEvidence: TypeAlias = Plan3NativeUpgradeEvidence


class Plan2CardUpgradeResolutionError(ValueError):
    """A Plan2/Common boundary tag or row reference is not admissible."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        message = code if not detail else f"{code}:{detail}"
        super().__init__(message)


def _row_value(
    row: Mapping[str, object] | sqlite3.Row, *names: str
) -> object:
    if isinstance(row, Mapping):
        for name in names:
            if name in row:
                return row[name]
        return None
    keys = getattr(row, "keys", None)
    if callable(keys):
        available = set(keys())
        for name in names:
            if name in available:
                return row[name]
    return None


def _require_plan2_common(
    row: Mapping[str, object] | sqlite3.Row | None = None,
    explicit_plan_type: str | None = None,
) -> None:
    observed = explicit_plan_type
    if observed is None and row is not None:
        candidate = _row_value(
            row,
            "plan_type",
            "planType",
            "producePlanType",
            "card_plan_type",
        )
        if candidate is not None:
            observed = candidate if isinstance(candidate, str) else str(candidate)
    if observed is not None and observed not in PLAN2_COMMON_PLAN_TYPES:
        raise Plan2CardUpgradeResolutionError("unsupported-plan", str(observed))


def _effect_reference(
    effect: str | Mapping[str, object] | sqlite3.Row,
    *,
    source_kind: str,
    plan_type: str | None,
) -> str:
    if source_kind not in {"effect", "drink"}:
        raise Plan2CardUpgradeResolutionError(
            "unsupported-source-kind", str(source_kind)
        )
    if isinstance(effect, str):
        _require_plan2_common(explicit_plan_type=plan_type)
        if not effect.strip():
            raise Plan2CardUpgradeResolutionError("invalid-effect-id", "empty")
        return effect
    if not isinstance(effect, (Mapping, sqlite3.Row)):
        raise Plan2CardUpgradeResolutionError(
            "invalid-effect-reference", type(effect).__name__
        )
    _require_plan2_common(effect, plan_type)
    effect_id = _row_value(
        effect,
        "id" if source_kind == "effect" else "source_id",
        "effect_id",
        "source_id",
    )
    if not isinstance(effect_id, str) or not effect_id.strip():
        raise Plan2CardUpgradeResolutionError("invalid-effect-id", "missing")
    effect_type = _row_value(effect, "effect_type", "effectType")
    if effect_type is not None and effect_type != EFFECT_EXAM_CARD_UPGRADE:
        raise Plan2CardUpgradeResolutionError("unexpected-effect-type", str(effect_type))
    raw_json = _row_value(effect, "raw_json")
    if raw_json is not None:
        try:
            raw = json.loads(str(raw_json)) if isinstance(raw_json, str) else raw_json
        except json.JSONDecodeError as error:
            raise Plan2CardUpgradeResolutionError(
                "invalid-master-raw-json", str(error)
            ) from error
        if isinstance(raw, Mapping):
            raw_id = raw.get("id", raw.get("effect_id"))
            if raw_id is None:
                raise Plan2CardUpgradeResolutionError("master-id-missing")
            if raw_id != effect_id:
                raise Plan2CardUpgradeResolutionError("master-id-mismatch")
            raw_type = raw.get("effectType", raw.get("effect_type"))
            if raw_type is not None and raw_type != EFFECT_EXAM_CARD_UPGRADE:
                raise Plan2CardUpgradeResolutionError(
                    "unexpected-effect-type", str(raw_type)
                )
    return effect_id


def load_master_plan2_card_upgrade_rows(
    database: Path = DEFAULT_DATABASE,
) -> tuple[dict[str, object], ...]:
    """Read the local Master rows for this effect family, without rebuilding coverage."""

    try:
        with sqlite3.connect(Path(database)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM effect WHERE effect_type = ? ORDER BY id",
                (EFFECT_EXAM_CARD_UPGRADE,),
            ).fetchall()
    except sqlite3.Error as error:
        raise Plan2CardUpgradeResolutionError(
            "master-effect-read-failed", str(error)
        ) from error
    return tuple(dict(row) for row in rows)


def resolve_plan2_card_upgrade(
    effect: str | Mapping[str, object] | sqlite3.Row,
    *,
    database: Path = DEFAULT_DATABASE,
    source_kind: str = "effect",
    plan_type: str | None = None,
) -> Plan2CardUpgradeResolution:
    """Resolve an ID or local Master row through the shared typed resolver."""

    effect_id = _effect_reference(
        effect, source_kind=source_kind, plan_type=plan_type
    )
    return resolve_plan3_card_upgrade(
        effect_id, database=database, source_kind=source_kind
    )


def try_resolve_plan2_card_upgrade(
    effect: str | Mapping[str, object] | sqlite3.Row,
    *,
    database: Path = DEFAULT_DATABASE,
    source_kind: str = "effect",
    plan_type: str | None = None,
) -> Plan2CardUpgradeResolution:
    """Return a typed pause for boundary errors instead of raising."""

    try:
        return resolve_plan2_card_upgrade(
            effect,
            database=database,
            source_kind=source_kind,
            plan_type=plan_type,
        )
    except Plan2CardUpgradeResolutionError as error:
        effect_id = ""
        if isinstance(effect, str):
            effect_id = effect
        elif isinstance(effect, (Mapping, sqlite3.Row)):
            raw_id = _row_value(effect, "id", "effect_id", "source_id")
            if isinstance(raw_id, str):
                effect_id = raw_id
        return Plan3CardUpgradeResolution(
            pause=Plan3CardUpgradePause(error.code, error.detail, effect_id)
        )


def parse_plan2_card_upgrade_master_row(
    effect: str | Mapping[str, object] | sqlite3.Row,
    *,
    database: Path = DEFAULT_DATABASE,
    source_kind: str = "effect",
    plan_type: str | None = None,
) -> Plan2CardUpgradeContract:
    """Return the shared contract, or fail closed when the row is unresolved."""

    resolution = resolve_plan2_card_upgrade(
        effect,
        database=database,
        source_kind=source_kind,
        plan_type=plan_type,
    )
    if resolution.contract is None:
        assert resolution.pause is not None
        raise Plan2CardUpgradeResolutionError(
            resolution.pause.code, resolution.pause.detail
        )
    return resolution.contract


# Execution and candidate search are aliases so the Plan2 surface cannot
# diverge from the already-tested GUID/zone/RNG/atomic implementation.
execute_plan2_card_upgrade = execute_plan3_card_upgrade
resolve_plan2_card_upgrade_candidates = resolve_plan3_card_upgrade_candidates


__all__ = [
    "EFFECT_EXAM_CARD_UPGRADE",
    "PLAN_COMMON",
    "PLAN2",
    "PLAN2_COMMON_PLAN_TYPES",
    "Plan2CardUpgradeBranch",
    "Plan2CardUpgradeCard",
    "Plan2CardUpgradeCandidate",
    "Plan2CardUpgradeCandidateResult",
    "Plan2CardUpgradeContract",
    "Plan2CardUpgradeMutationTrace",
    "Plan2CardUpgradePause",
    "Plan2CardUpgradeResolution",
    "Plan2CardUpgradeResolutionError",
    "Plan2CardUpgradeResult",
    "Plan2CardUpgradeResultStatus",
    "Plan2CardUpgradeSelectionRequest",
    "Plan2CardUpgradeState",
    "Plan2NativeUpgradeEvidence",
    "Plan3CardUpgradeBranch",
    "Plan3CardUpgradeCandidate",
    "Plan3CardUpgradeCandidateResult",
    "Plan3CardUpgradeContract",
    "Plan3CardUpgradeMutationTrace",
    "Plan3CardUpgradePause",
    "Plan3CardUpgradeResolution",
    "Plan3CardUpgradeResult",
    "Plan3CardUpgradeResultStatus",
    "Plan3CardUpgradeSelectionRequest",
    "Plan3NativeCard",
    "Plan3NativeState",
    "Plan3NativeUpgradeEvidence",
    "execute_plan2_card_upgrade",
    "load_master_plan2_card_upgrade_rows",
    "parse_plan2_card_upgrade_master_row",
    "resolve_plan2_card_upgrade",
    "resolve_plan2_card_upgrade_candidates",
    "try_resolve_plan2_card_upgrade",
]
