"""N.I.A.-bound Plan 3 native legal-candidate provider.

This module is deliberately an adapter around the existing N.I.A. composition
and search preparation path.  It never uses the ordinary Plan 3 gimmick path:
``prepare_nia_plan3_audition_runtime`` composes the outer Produce log with the
decoded ExamSave, validates the profile execution gate, and installs the same
``_nia_search_extensions`` used by the live N.I.A. advisor before the exact
root enumerator is called.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .master_db import DEFAULT_DATABASE
from .nia_audition_advisor import prepare_nia_plan3_audition_runtime
from .plan3_audition_search_bridge import bind_plan3_audition_state
from .plan3_drink import Plan3DrinkInventory, load_plan3_drink_inventory
from .plan3_engine import DEFAULT_MASTER_DIR, load_plan3_exam_settings
from .plan3_local_save_bridge import DecodedPlan3LocalSave
from .plan3_native_search import (
    PLAN3_NATIVE_ENUMERATOR_AUTHORITY,
    Plan3NativeLegalCandidate,
    Plan3NativeLegalCandidateEnumeration,
    enumerate_plan3_native_legal_candidates,
)
from .plan3_native_search_bridge import DEFAULT_SUPPORT_CARD_MASTER


OuterReader = Callable[[], object]


@dataclass(frozen=True, slots=True)
class NiaPlan3CandidateScope:
    """Composite identity bound by outer log, ExamSave and Master."""

    produce_id: str
    idol_card_id: str
    step_type: str
    audition_number: int
    difficulty_row_id: str
    outer_log_index: int
    outer_detail_index: int
    outer_line_index: int
    outer_latest_week_marker: int | None
    gimmick_group_id: str

    def __post_init__(self) -> None:
        for name in (
            "produce_id",
            "idol_card_id",
            "step_type",
            "difficulty_row_id",
            "gimmick_group_id",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"NIA Plan3 scope {name} must be non-empty text")
        if (
            not isinstance(self.audition_number, int)
            or isinstance(self.audition_number, bool)
            or self.audition_number < 1
        ):
            raise ValueError("NIA Plan3 audition_number must be positive")
        for name in ("outer_log_index", "outer_detail_index", "outer_line_index"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"NIA Plan3 {name} must be non-negative")
        if self.outer_latest_week_marker is not None and (
            not isinstance(self.outer_latest_week_marker, int)
            or isinstance(self.outer_latest_week_marker, bool)
        ):
            raise ValueError("outer_latest_week_marker must be an integer or None")

    def to_dict(self) -> dict[str, object]:
        return {
            "produce_id": self.produce_id,
            "idol_card_id": self.idol_card_id,
            "step_type": self.step_type,
            "audition_number": self.audition_number,
            "difficulty_row_id": self.difficulty_row_id,
            "outer_identity": {
                "log_index": self.outer_log_index,
                "detail_index": self.outer_detail_index,
                "line_index": self.outer_line_index,
                "latest_week_marker": self.outer_latest_week_marker,
            },
            "gimmick_group_id": self.gimmick_group_id,
        }


@dataclass(frozen=True, slots=True)
class NiaPlan3NativeCandidateResult:
    """Provider output: either a complete enumeration or auditable blockers."""

    enumeration: Plan3NativeLegalCandidateEnumeration
    scope: NiaPlan3CandidateScope | None = None

    @property
    def complete(self) -> bool:
        return self.enumeration.complete

    @property
    def candidates(self) -> tuple[Plan3NativeLegalCandidate, ...]:
        return self.enumeration.candidates

    @property
    def blockers(self) -> tuple[str, ...]:
        return self.enumeration.blockers

    def to_dict(self) -> dict[str, object]:
        return {
            "scope": None if self.scope is None else self.scope.to_dict(),
            "enumeration": self.enumeration.to_dict(),
        }


def _blocked(*blockers: str) -> NiaPlan3NativeCandidateResult:
    values = tuple(dict.fromkeys(value for value in blockers if value))
    if not values:
        values = ("nia-plan3-candidate-provider-blocked",)
    return NiaPlan3NativeCandidateResult(
        enumeration=Plan3NativeLegalCandidateEnumeration(
            candidates=(),
            authority=PLAN3_NATIVE_ENUMERATOR_AUTHORITY,
            complete=False,
            blockers=values,
        )
    )


def _scope_from_composition(composition: object) -> NiaPlan3CandidateScope:
    difficulty = composition.difficulty_key
    event = composition.audition_select_event
    profile = composition.projection.profile
    rules = None if profile is None else profile.rules
    gimmick_group_id = None if rules is None else rules.gimmick_group_id
    return NiaPlan3CandidateScope(
        produce_id=difficulty.produce_id,
        idol_card_id=composition.idol_card_id,
        step_type=difficulty.step_type,
        audition_number=difficulty.number,
        difficulty_row_id=difficulty.row_id,
        outer_log_index=event.log_index,
        outer_detail_index=event.detail_index,
        outer_line_index=event.line_index,
        outer_latest_week_marker=composition.outer_snapshot.latest_week_marker,
        gimmick_group_id=gimmick_group_id,
    )


@dataclass(frozen=True, slots=True)
class NiaPlan3NativeCandidateProvider:
    """Callable NIA-bound provider suitable for a Plan3 step observer."""

    outer_reader: OuterReader
    expected_produce_id: str | None = None
    expected_idol_card_id: str | None = None
    expected_outer_log_index: int | None = None
    prefer_exam_save_runtime: bool = True
    include_drinks: bool = True
    include_turn_end: bool = True
    database: Path = DEFAULT_DATABASE
    support_card_master: Path = DEFAULT_SUPPORT_CARD_MASTER
    master_dir: Path = DEFAULT_MASTER_DIR

    def __post_init__(self) -> None:
        if not callable(self.outer_reader):
            raise TypeError("outer_reader must be callable")
        for name in ("expected_produce_id", "expected_idol_card_id"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError(f"{name} must be non-empty text or None")
        if self.expected_outer_log_index is not None and (
            not isinstance(self.expected_outer_log_index, int)
            or isinstance(self.expected_outer_log_index, bool)
            or self.expected_outer_log_index < 0
        ):
            raise ValueError("expected_outer_log_index must be non-negative or None")
        if type(self.prefer_exam_save_runtime) is not bool:
            raise TypeError("prefer_exam_save_runtime must be bool")
        if type(self.include_drinks) is not bool:
            raise TypeError("include_drinks must be bool")
        if type(self.include_turn_end) is not bool:
            raise TypeError("include_turn_end must be bool")
        object.__setattr__(self, "database", Path(self.database))
        object.__setattr__(self, "support_card_master", Path(self.support_card_master))
        object.__setattr__(self, "master_dir", Path(self.master_dir))

    def __call__(
        self,
        decoded: DecodedPlan3LocalSave,
    ) -> NiaPlan3NativeCandidateResult:
        if not isinstance(decoded, DecodedPlan3LocalSave):
            return _blocked("decoded-plan3-local-save-required")
        try:
            outer = self.outer_reader()
            composition, context, prepared = prepare_nia_plan3_audition_runtime(
                outer,
                decoded,
                prefer_exam_save_runtime=self.prefer_exam_save_runtime,
                database=self.database,
                support_card_master=self.support_card_master,
                master_dir=self.master_dir,
            )
            scope = _scope_from_composition(composition)
            identity_blockers: list[str] = []
            if (
                self.expected_produce_id is not None
                and scope.produce_id != self.expected_produce_id
            ):
                identity_blockers.append(
                    "produce-id-mismatch:"
                    f"{scope.produce_id}!={self.expected_produce_id}"
                )
            if (
                self.expected_idol_card_id is not None
                and scope.idol_card_id != self.expected_idol_card_id
            ):
                identity_blockers.append(
                    "idol-card-id-mismatch:"
                    f"{scope.idol_card_id}!={self.expected_idol_card_id}"
                )
            if (
                self.expected_outer_log_index is not None
                and scope.outer_log_index != self.expected_outer_log_index
            ):
                identity_blockers.append(
                    "outer-audition-selection-mismatch:"
                    f"{scope.outer_log_index}!={self.expected_outer_log_index}"
                )
            if identity_blockers:
                return NiaPlan3NativeCandidateResult(
                    enumeration=Plan3NativeLegalCandidateEnumeration(
                        candidates=(),
                        authority=PLAN3_NATIVE_ENUMERATOR_AUTHORITY,
                        complete=False,
                        blockers=tuple(identity_blockers),
                    ),
                    scope=scope,
                )

            if not decoded.exam_state.is_native_actionable_settled:
                return NiaPlan3NativeCandidateResult(
                    enumeration=Plan3NativeLegalCandidateEnumeration(
                        candidates=(),
                        authority=PLAN3_NATIVE_ENUMERATOR_AUTHORITY,
                        complete=False,
                        blockers=("exam-save-not-actionable-settled",),
                    ),
                    scope=scope,
                )
            if prepared.issues:
                return NiaPlan3NativeCandidateResult(
                    enumeration=Plan3NativeLegalCandidateEnumeration(
                        candidates=(),
                        authority=PLAN3_NATIVE_ENUMERATOR_AUTHORITY,
                        complete=False,
                        blockers=tuple(
                            f"prepared:{issue.code}:{issue.detail}".rstrip(":")
                            for issue in prepared.issues
                        ),
                    ),
                    scope=scope,
                )
            if prepared.native_state is None or not prepared.projection.exact:
                return NiaPlan3NativeCandidateResult(
                    enumeration=Plan3NativeLegalCandidateEnumeration(
                        candidates=(),
                        authority=PLAN3_NATIVE_ENUMERATOR_AUTHORITY,
                        complete=False,
                        blockers=("nia-native-state-or-projection-incomplete",),
                    ),
                    scope=scope,
                )
            if context.full_horizon_gaps:
                return NiaPlan3NativeCandidateResult(
                    enumeration=Plan3NativeLegalCandidateEnumeration(
                        candidates=(),
                        authority=PLAN3_NATIVE_ENUMERATOR_AUTHORITY,
                        complete=False,
                        blockers=tuple(
                            f"nia-horizon-gap:{gap.code}" for gap in context.full_horizon_gaps
                        ),
                    ),
                    scope=scope,
                )

            state = bind_plan3_audition_state(prepared.projection.state, context)
            settings = load_plan3_exam_settings(
                decoded.exam_state.setting_id,
                master_dir=self.master_dir,
            )
            inventory = (
                load_plan3_drink_inventory(
                    context.drink_ids,
                    master_dir=self.master_dir,
                    database=self.database,
                )
                if self.include_drinks
                else Plan3DrinkInventory()
            )
            force_end_score = context.force_end_score if context.force_end_score > 0 else 0
            recovery = (
                context.turn_end_stamina_recovery
                if force_end_score > 0
                else 0
            )
            enumeration = enumerate_plan3_native_legal_candidates(
                state,
                prepared.native_state,
                settings=settings,
                database=self.database,
                support_upgrades=prepared.support_upgrades,
                support_card_searches=dict(prepared.support_card_searches),
                battle_parameter_schedule=tuple(
                    frame.parameter_type for frame in context.remaining_frames
                ),
                battle_ranking_resolved=True,
                include_drinks=self.include_drinks,
                include_turn_end=self.include_turn_end,
                force_end_score=force_end_score,
                force_end_stamina_recovery=recovery,
                drink_inventory=inventory,
                turn_start_extension=prepared.turn_start_extension,
                accepted_play_extension=prepared.accepted_play_extension,
                initial_turn_start_extension_state=(
                    prepared.initial_turn_start_extension_state
                ),
            )
            return NiaPlan3NativeCandidateResult(
                enumeration=enumeration,
                scope=scope,
            )
        except (KeyError, OSError, TypeError, ValueError) as error:
            return _blocked(f"nia-preparation-failed:{type(error).__name__}:{error}")


def build_nia_plan3_native_candidate_provider(
    outer_reader: OuterReader,
    *,
    expected_produce_id: str | None = None,
    expected_idol_card_id: str | None = None,
    expected_outer_log_index: int | None = None,
    prefer_exam_save_runtime: bool = True,
    include_drinks: bool = True,
    include_turn_end: bool = True,
    database: str | Path = DEFAULT_DATABASE,
    support_card_master: str | Path = DEFAULT_SUPPORT_CARD_MASTER,
    master_dir: str | Path = DEFAULT_MASTER_DIR,
) -> NiaPlan3NativeCandidateProvider:
    """Build the callable provider used by a Plan3 step observer."""

    return NiaPlan3NativeCandidateProvider(
        outer_reader=outer_reader,
        expected_produce_id=expected_produce_id,
        expected_idol_card_id=expected_idol_card_id,
        expected_outer_log_index=expected_outer_log_index,
        prefer_exam_save_runtime=prefer_exam_save_runtime,
        include_drinks=include_drinks,
        include_turn_end=include_turn_end,
        database=Path(database),
        support_card_master=Path(support_card_master),
        master_dir=Path(master_dir),
    )


__all__ = [
    "NiaPlan3CandidateScope",
    "NiaPlan3NativeCandidateProvider",
    "NiaPlan3NativeCandidateResult",
    "build_nia_plan3_native_candidate_provider",
]
