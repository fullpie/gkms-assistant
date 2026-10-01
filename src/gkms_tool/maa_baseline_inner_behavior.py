"""Extract behavior-only card choices from completed Maa exam traces.

Maa's completion baseline already records every typed ExamSave hand change.
When exactly one GUID leaves a same-turn Hand, that GUID is an observed card
choice from the preceding settled-hand candidate tuple.  These rows improve
live-domain imitation data, but deliberately carry no legal-set or RL claim.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile

from .master_db import DEFAULT_DATABASE


SCHEMA = "gkms.maa-baseline-inner-behavior.v1"
MANIFEST_SCHEMA = "gkms.maa-baseline-inner-behavior-manifest.v1"
DEFAULT_OUTPUT = (
    Path(__file__).resolve().parents[2]
    / "var"
    / "nia_training"
    / "maa_baseline_inner_behavior.jsonl"
)


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


@dataclass(frozen=True, slots=True)
class MaaBaselineHandCard:
    guid: str
    card_id: str
    upgrade: int

    def __post_init__(self) -> None:
        _text(self.guid, "card guid")
        _text(self.card_id, "card id")
        _integer(self.upgrade, "card upgrade")

    @classmethod
    def from_value(cls, value: object) -> "MaaBaselineHandCard":
        if (
            not isinstance(value, Sequence)
            or isinstance(value, (str, bytes, bytearray))
            or len(value) != 3
        ):
            raise ValueError("progress hand card must be [guid, card_id, upgrade]")
        return cls(
            _text(value[0], "card guid"),
            _text(value[1], "card id"),
            _integer(value[2], "card upgrade"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "guid": self.guid,
            "card_id": self.card_id,
            "upgrade": self.upgrade,
        }


@dataclass(frozen=True, slots=True)
class MaaBaselineInnerBehaviorObservation:
    boundary_digest: str
    source_id: str
    report_path: str
    produce_id: str
    idol_card_id: str
    plan_type: str
    exam_effect_type: str
    stage: str
    turn: int
    action_order: int
    terminal_score: int
    score_before: int | None
    remain_turn_before: int | None
    settled_hand: tuple[MaaBaselineHandCard, ...]
    chosen: MaaBaselineHandCard
    schema: str = SCHEMA

    def __post_init__(self) -> None:
        if self.schema != SCHEMA:
            raise ValueError("unsupported Maa baseline behavior schema")
        if (
            not isinstance(self.boundary_digest, str)
            or len(self.boundary_digest) != 64
        ):
            raise ValueError("boundary_digest must be SHA-256 text")
        for name in (
            "source_id",
            "report_path",
            "produce_id",
            "idol_card_id",
            "plan_type",
            "exam_effect_type",
            "stage",
        ):
            _text(getattr(self, name), name)
        _integer(self.turn, "turn", minimum=1)
        _integer(self.action_order, "action_order")
        _integer(self.terminal_score, "terminal_score")
        for name in ("score_before", "remain_turn_before"):
            value = getattr(self, name)
            if value is not None:
                _integer(value, name)
        hand = tuple(self.settled_hand)
        if not hand or any(not isinstance(value, MaaBaselineHandCard) for value in hand):
            raise ValueError("settled_hand must contain typed cards")
        if len({value.guid for value in hand}) != len(hand):
            raise ValueError("settled_hand GUIDs must be unique")
        if not isinstance(self.chosen, MaaBaselineHandCard):
            raise TypeError("chosen must be a typed card")
        if self.chosen.guid not in {value.guid for value in hand}:
            raise ValueError("chosen card must exist in settled_hand")
        object.__setattr__(self, "settled_hand", hand)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "boundary_digest": self.boundary_digest,
            "source_id": self.source_id,
            "report_path": self.report_path,
            "flow": "|".join(
                (self.produce_id, self.plan_type, self.exam_effect_type)
            ),
            "produce_id": self.produce_id,
            "idol_card_id": self.idol_card_id,
            "plan_type": self.plan_type,
            "exam_effect_type": self.exam_effect_type,
            "stage": self.stage,
            "turn": self.turn,
            "action_order": self.action_order,
            "terminal_score": self.terminal_score,
            "score_before": self.score_before,
            "remain_turn_before": self.remain_turn_before,
            "candidate_set_kind": "settled_hand",
            "candidate_complete": True,
            "settled_hand": [value.to_dict() for value in self.settled_hand],
            "chosen": self.chosen.to_dict(),
            "behavior_only": True,
            "contextual_bandit": True,
            "full_rl_transition": False,
            "reward": None,
            "state_after": None,
        }


def _idol_flow(
    idol_card_id: str,
    *,
    database: str | Path = DEFAULT_DATABASE,
) -> tuple[str, str]:
    with closing(sqlite3.connect(Path(database))) as connection:
        row = connection.execute(
            "SELECT plan_type, exam_effect_type FROM idol_card WHERE id = ?",
            (idol_card_id,),
        ).fetchone()
    if row is None:
        raise ValueError(f"unknown idol card: {idol_card_id}")
    return _text(row[0], "plan_type"), _text(row[1], "exam_effect_type")


def _exam_outcomes(report: Mapping[str, object]) -> Iterable[Mapping[str, object]]:
    result = report.get("result")
    steps = result.get("steps") if isinstance(result, Mapping) else None
    if not isinstance(steps, list):
        return ()
    outcomes: list[Mapping[str, object]] = []
    for step in steps:
        if not isinstance(step, Mapping):
            continue
        outcome = step.get("outcome")
        if not isinstance(outcome, Mapping):
            continue
        inner = outcome.get("inner_imitation")
        if isinstance(inner, Mapping):
            decision = inner.get("decision")
            if isinstance(decision, Mapping):
                baseline = decision.get("baseline_result")
                if isinstance(baseline, Mapping):
                    outcomes.append(
                        {
                            **dict(baseline),
                            "exam_save_identity": outcome.get(
                                "exam_save_identity"
                            ),
                        }
                    )
                    continue
        policy = outcome.get("execution_policy")
        if (
            isinstance(policy, Mapping)
            and policy.get("mode") == "maa-completion-baseline"
        ):
            outcomes.append(outcome)
    return tuple(outcomes)


def _trace_hand(value: object) -> tuple[MaaBaselineHandCard, ...] | None:
    if not isinstance(value, list):
        return None
    try:
        hand = tuple(MaaBaselineHandCard.from_value(row) for row in value)
    except (TypeError, ValueError):
        return None
    if len({value.guid for value in hand}) != len(hand):
        return None
    return hand


def extract_maa_baseline_inner_behavior(
    report: Mapping[str, object],
    *,
    report_path: str | Path,
    database: str | Path = DEFAULT_DATABASE,
) -> tuple[MaaBaselineInnerBehaviorObservation, ...]:
    acceptance = report.get("completion_acceptance")
    if (
        report.get("status") != "completed"
        or report.get("authentic_full_run_completed") is not True
        or not isinstance(acceptance, Mapping)
        or acceptance.get("accepted") is not True
    ):
        return ()
    request = report.get("request")
    if not isinstance(request, Mapping):
        return ()
    produce_id = _text(request.get("produce_id"), "produce_id")
    idol_card_id = _text(request.get("idol_card_id"), "idol_card_id")
    plan_type, exam_effect_type = _idol_flow(
        idol_card_id,
        database=database,
    )
    strategy = report.get("strategy_journal")
    source_id = (
        strategy.get("run_id") if isinstance(strategy, Mapping) else None
    )
    source_id = _text(source_id, "run_id")
    path = str(Path(report_path).resolve())

    observations: list[MaaBaselineInnerBehaviorObservation] = []
    for outcome in _exam_outcomes(report):
        identity = outcome.get("exam_save_identity")
        trace = outcome.get("progress_trace")
        if not isinstance(identity, Mapping) or not isinstance(trace, list):
            continue
        stage = _text(identity.get("step_context_id"), "step_context_id")
        terminal_score = outcome.get("final_score")
        if isinstance(terminal_score, bool) or not isinstance(
            terminal_score, int
        ):
            terminal_score = 0
        action_order = 0
        for left, right in zip(trace, trace[1:]):
            if not isinstance(left, Mapping) or not isinstance(right, Mapping):
                continue
            left_turn = left.get("current_turn")
            right_turn = right.get("current_turn")
            if (
                isinstance(left_turn, bool)
                or not isinstance(left_turn, int)
                or left_turn < 1
                or right_turn != left_turn
            ):
                continue
            hand = _trace_hand(left.get("hand"))
            next_hand = _trace_hand(right.get("hand"))
            if (
                not hand
                or next_hand is None
                or len(next_hand) != len(hand) - 1
            ):
                continue
            next_guids = {value.guid for value in next_hand}
            removed = tuple(value for value in hand if value.guid not in next_guids)
            if len(removed) != 1:
                continue
            chosen = removed[0]
            payload = {
                "source_id": source_id,
                "stage": stage,
                "turn": left_turn,
                "action_order": action_order,
                "hand": [value.to_dict() for value in hand],
                "chosen": chosen.to_dict(),
            }
            observations.append(
                MaaBaselineInnerBehaviorObservation(
                    boundary_digest=_digest(payload),
                    source_id=source_id,
                    report_path=path,
                    produce_id=produce_id,
                    idol_card_id=idol_card_id,
                    plan_type=plan_type,
                    exam_effect_type=exam_effect_type,
                    stage=stage,
                    turn=left_turn,
                    action_order=action_order,
                    terminal_score=terminal_score,
                    score_before=(
                        left.get("score")
                        if isinstance(left.get("score"), int)
                        and not isinstance(left.get("score"), bool)
                        else None
                    ),
                    remain_turn_before=(
                        left.get("remain_turn")
                        if isinstance(left.get("remain_turn"), int)
                        and not isinstance(left.get("remain_turn"), bool)
                        else None
                    ),
                    settled_hand=hand,
                    chosen=chosen,
                )
            )
            action_order += 1
    return tuple(observations)


def build_maa_baseline_inner_behavior_dataset(
    reports: Iterable[str | Path],
    *,
    output: str | Path = DEFAULT_OUTPUT,
    database: str | Path = DEFAULT_DATABASE,
) -> dict[str, object]:
    rows: dict[str, MaaBaselineInnerBehaviorObservation] = {}
    accepted_reports: list[str] = []
    skipped_reports: list[str] = []
    for source in sorted({Path(value).resolve() for value in reports}):
        try:
            raw = json.loads(source.read_text(encoding="utf-8"))
            if not isinstance(raw, Mapping):
                raise ValueError("report root must be an object")
            extracted = extract_maa_baseline_inner_behavior(
                raw,
                report_path=source,
                database=database,
            )
        except (OSError, json.JSONDecodeError, TypeError, ValueError, sqlite3.Error):
            skipped_reports.append(str(source))
            continue
        if not extracted:
            skipped_reports.append(str(source))
            continue
        accepted_reports.append(str(source))
        for value in extracted:
            rows.setdefault(value.boundary_digest, value)

    target = Path(output).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    serialized = "".join(
        _canonical(value.to_dict()) + "\n"
        for value in sorted(
            rows.values(),
            key=lambda row: (
                row.source_id,
                row.stage,
                row.action_order,
                row.boundary_digest,
            ),
        )
    )
    temporary: str | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=str(target.parent),
        )
        with open(descriptor, "w", encoding="utf-8", newline="\n", closefd=True) as stream:
            stream.write(serialized)
        Path(temporary).replace(target)
        temporary = None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)

    values = tuple(rows.values())
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "output": str(target),
        "observation_count": len(values),
        "source_count": len({value.source_id for value in values}),
        "stage_count": len({(value.source_id, value.stage) for value in values}),
        "flow_counts": {
            flow: sum(
                1
                for value in values
                if "|".join(
                    (value.produce_id, value.plan_type, value.exam_effect_type)
                )
                == flow
            )
            for flow in sorted(
                {
                    "|".join(
                        (value.produce_id, value.plan_type, value.exam_effect_type)
                    )
                    for value in values
                }
            )
        },
        "accepted_report_count": len(accepted_reports),
        "skipped_report_count": len(skipped_reports),
        "accepted_reports": accepted_reports,
        "skipped_reports": skipped_reports,
        "candidate_set_kind": "settled_hand",
        "behavior_only": True,
        "full_rl_transition_count": 0,
    }
    manifest_path = target.with_suffix(".manifest.json")
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def append_maa_baseline_inner_behavior_report(
    report_path: str | Path,
    *,
    output: str | Path = DEFAULT_OUTPUT,
    database: str | Path = DEFAULT_DATABASE,
) -> dict[str, object]:
    """Merge one newly completed report into the behavior dataset."""

    target = Path(output).resolve()
    manifest_path = target.with_suffix(".manifest.json")
    existing_reports: list[Path] = []
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            manifest = {}
        values = (
            manifest.get("accepted_reports", ())
            if isinstance(manifest, Mapping)
            else ()
        )
        if isinstance(values, list):
            existing_reports.extend(
                Path(value)
                for value in values
                if isinstance(value, str) and value
            )
    existing_reports.append(Path(report_path).resolve())
    return build_maa_baseline_inner_behavior_dataset(
        existing_reports,
        output=target,
        database=database,
    )


__all__ = [
    "DEFAULT_OUTPUT",
    "MANIFEST_SCHEMA",
    "MaaBaselineHandCard",
    "MaaBaselineInnerBehaviorObservation",
    "SCHEMA",
    "build_maa_baseline_inner_behavior_dataset",
    "append_maa_baseline_inner_behavior_report",
    "extract_maa_baseline_inner_behavior",
]
