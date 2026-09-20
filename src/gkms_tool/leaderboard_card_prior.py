"""Learn a bounded Plan2 reward-card prior from complete leaderboard runs.

Leaderboard 3.3.0 replay rows describe auditions, not the outer weekly route.
They can therefore support deck/card preferences, but they cannot label lesson,
work, outing, or rest choices.  This module deliberately exposes only a small
additive reward-card bonus.  Runtime OCR/Master identity and legality remain the
authority for which cards are actually selectable.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
from statistics import fmean
from typing import Any, Mapping

from .leaderboard_replay_adapter import read_leaderboard_episode


# Keep the historical Plan2 schema names for compatibility.  Scope fields are
# now explicit and are allowed to describe every NIA archetype.
SCHEMA = "gkms.plan2-leaderboard-card-prior.v1"
GENERIC_SCHEMA = "gkms.leaderboard-card-prior.v1"
HIERARCHICAL_SCHEMA = "gkms.plan2-leaderboard-card-prior-hierarchical.v1"
GENERIC_HIERARCHICAL_SCHEMA = "gkms.leaderboard-card-prior-hierarchical.v1"
PLAN2 = "ProducePlanType_Plan2"
REVIEW = "ProduceExamEffectType_ExamReview"
_LEGACY_LEADERBOARD_EPISODES = (
    Path(__file__).resolve().parents[2]
    / "var"
    / "leaderboard_dataset"
    / "v330_nia_all_modes_multicard_v1"
    / "episodes.jsonl"
)
_FIVE_ARCHETYPE_LEADERBOARD_EPISODES = (
    Path(__file__).resolve().parents[2]
    / "var"
    / "leaderboard_dataset"
    / "v330_nia_five_archetype_all_modes_v1"
    / "episodes.jsonl"
)
_BACKGROUND_REFRESH_LEADERBOARD_EPISODES = (
    Path(__file__).resolve().parents[2]
    / "var"
    / "leaderboard_dataset"
    / "v330_nia_background_refresh_v1"
    / "episodes.jsonl"
)
# Prefer the latest atomically published background corpus.  Installed copies
# without that local artifact retain the stable five-archetype/Plan2 fallback.
DEFAULT_LEADERBOARD_EPISODES = (
    _BACKGROUND_REFRESH_LEADERBOARD_EPISODES
    if _BACKGROUND_REFRESH_LEADERBOARD_EPISODES.is_file()
    else (
        _FIVE_ARCHETYPE_LEADERBOARD_EPISODES
        if _FIVE_ARCHETYPE_LEADERBOARD_EPISODES.is_file()
        else _LEGACY_LEADERBOARD_EPISODES
    )
)
_STAGES = (
    "ProduceStepType_AuditionMid1",
    "ProduceStepType_AuditionMid2",
    "ProduceStepType_AuditionFinal",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class LeaderboardCardPriorStat:
    card_id: str
    trajectory_support: int
    copy_count: int
    mean_copy_count: float
    upgraded_copy_count: int
    mean_terminal_score: float
    bonus: int

    def __post_init__(self) -> None:
        if not self.card_id:
            raise ValueError("card prior requires a card ID")
        if not 0 <= self.upgraded_copy_count <= self.copy_count:
            raise ValueError("card prior upgraded copies are invalid")
        if not 1 <= self.trajectory_support <= self.copy_count:
            raise ValueError("card prior support is invalid")
        if not math.isfinite(self.mean_copy_count) or self.mean_copy_count <= 0:
            raise ValueError("card prior mean copy count is invalid")
        if not 0 <= self.bonus <= 300:
            raise ValueError("card prior bonus must be bounded to 0..300")

    @property
    def support(self) -> int:
        """Compatibility alias used by telemetry and prior consumers."""

        return self.trajectory_support


@dataclass(frozen=True, slots=True)
class Plan2LeaderboardCardPrior:
    source_path: Path
    source_sha256: str
    trajectory_count: int
    final_episode_count: int
    master_hashes: tuple[str, ...]
    app_versions: tuple[str, ...]
    produce_ids: tuple[str, ...]
    idol_card_ids: tuple[str, ...]
    character_ids: tuple[str, ...]
    statistics: Mapping[str, LeaderboardCardPriorStat]
    plan_type: str = PLAN2
    exam_effect_type: str = REVIEW
    schema: str = SCHEMA
    scope_kind: str = "exact"

    def __post_init__(self) -> None:
        if self.schema not in {SCHEMA, GENERIC_SCHEMA}:
            raise ValueError("unsupported leaderboard card prior identity")
        if not isinstance(self.plan_type, str) or not self.plan_type:
            raise ValueError("leaderboard card prior requires a plan scope")
        if not isinstance(self.exam_effect_type, str) or not self.exam_effect_type:
            raise ValueError("leaderboard card prior requires an exam-effect scope")
        if self.scope_kind not in {"exact", "broad"}:
            raise ValueError("unsupported leaderboard card prior scope")
        if self.trajectory_count < 3 or self.final_episode_count != self.trajectory_count:
            raise ValueError("leaderboard prior requires at least three complete trajectories")
        if len(self.source_sha256) != 64:
            raise ValueError("leaderboard prior source hash is invalid")

    @property
    def produce_id(self) -> str:
        if len(self.produce_ids) != 1:
            raise ValueError("leaderboard prior has no single produce scope")
        return self.produce_ids[0]

    @property
    def idol_card_id(self) -> str:
        if len(self.idol_card_ids) != 1:
            raise ValueError("leaderboard prior has no single idol scope")
        return self.idol_card_ids[0]

    @property
    def scope(self) -> tuple[str, str, str, str | None]:
        """Return ``(produce, plan, effect, optional exact idol)``."""

        return (
            self.produce_id,
            self.plan_type,
            self.exam_effect_type,
            self.idol_card_id if self.scope_kind == "exact" else None,
        )

    def score_for(self, card_id: str, upgrade: int = 0) -> int:
        stat = self.statistics.get(card_id)
        if stat is None:
            return 0
        upgrade_bonus = 0
        if upgrade > 0 and stat.upgraded_copy_count > 0:
            upgrade_bonus = min(
                20,
                round(20 * stat.upgraded_copy_count / stat.copy_count),
            )
        return min(300, stat.bonus + upgrade_bonus)

    def statistic_for(
        self,
        card_id: str,
    ) -> tuple[LeaderboardCardPriorStat | None, str | None]:
        """Return this prior's statistic and its evidence scope.

        The two-value form keeps the old exact prior useful to callers while
        allowing the hierarchical wrapper to report whether a card came from
        exact-idol or cross-idol evidence.
        """

        stat = self.statistics.get(card_id)
        return (stat, self.scope_kind) if stat is not None else (None, None)

    def composition_score_for(
        self,
        card_id: str,
        upgrade: int = 0,
        current_count: int = 0,
        future_guaranteed_count: int = 0,
    ) -> int:
        """Return the prior bonus only for the remaining final-deck gap.

        Final replay decks are the useful observable here: they tell us how
        many copies a successful player finished with, without pretending the
        replay can identify whether each copy came from a memory, support
        effect, or a reward choice.  ``current_count`` is therefore keyed by
        stable ``card_id`` and callers may include any already guaranteed
        future copies.  Once the observed average target is met, this signal
        abstains instead of penalising the card.
        """

        for name, value in (
            ("current_count", current_count),
            ("future_guaranteed_count", future_guaranteed_count),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        stat = self.statistics.get(card_id)
        if stat is None:
            return 0
        gap = max(
            0.0,
            stat.mean_copy_count - current_count - future_guaranteed_count,
        )
        if gap <= 0:
            return 0
        base_bonus = self.score_for(card_id, upgrade)
        return max(0, min(300, round(base_bonus * min(1.0, gap))))

    def applies_to(
        self,
        *,
        produce_id: str,
        idol_card_id: str,
        plan_type: str = PLAN2,
        exam_effect_type: str,
    ) -> bool:
        """Require complete mode scope; never cross producer, plan, or effect."""

        if self.scope_kind == "broad":
            return (
                plan_type == self.plan_type
                and
                exam_effect_type == self.exam_effect_type
                and self.produce_ids == (produce_id,)
            )
        return (
            plan_type == self.plan_type
            and
            exam_effect_type == self.exam_effect_type
            and self.produce_ids == (produce_id,)
            and self.idol_card_ids == (idol_card_id,)
        )

    def summary(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "scope_kind": self.scope_kind,
            "source_path": str(self.source_path),
            "source_sha256": self.source_sha256,
            "trajectory_count": self.trajectory_count,
            "final_episode_count": self.final_episode_count,
            "produce_ids": list(self.produce_ids),
            "plan_type": self.plan_type,
            "exam_effect_type": self.exam_effect_type,
            "master_hashes": list(self.master_hashes),
            "app_versions": list(self.app_versions),
            "produce_ids": list(self.produce_ids),
            "idol_card_ids": list(self.idol_card_ids),
            "character_ids": list(self.character_ids),
            "card_count": len(self.statistics),
        }


@dataclass(frozen=True, slots=True)
class Plan2HierarchicalLeaderboardCardPrior:
    """Select exact-idol composition evidence before same-scope broad evidence.

    ``exact`` is retained whenever the requested ``(produce, plan, idol, effect)``
    scope has at least three complete successful trajectories.  ``broad`` is
    always restricted to the same ``produce_id + plan_type + exam_effect_type`` scope and is
    consulted only for cards absent from ``exact``.  This object deliberately
    exposes the same scoring surface as :class:`Plan2LeaderboardCardPrior`, so
    existing reward and rollout callers need no loader-specific branching.
    """

    requested_produce_id: str
    requested_idol_card_id: str
    requested_exam_effect_type: str
    exact: Plan2LeaderboardCardPrior | None
    broad: Plan2LeaderboardCardPrior | None
    schema: str = HIERARCHICAL_SCHEMA
    plan_type: str = PLAN2

    def __post_init__(self) -> None:
        if self.schema not in {HIERARCHICAL_SCHEMA, GENERIC_HIERARCHICAL_SCHEMA}:
            raise ValueError("unsupported hierarchical leaderboard prior identity")
        if not isinstance(self.plan_type, str) or not self.plan_type:
            raise ValueError("hierarchical leaderboard prior requires a plan scope")
        if not all(
            isinstance(value, str) and value
            for value in (
                self.requested_produce_id,
                self.requested_exam_effect_type,
            )
        ):
            raise ValueError("hierarchical leaderboard prior requires a produce/effect request")
        if self.exact is None and self.broad is None:
            raise ValueError("hierarchical leaderboard prior has no evidence")
        for name, prior in (("exact", self.exact), ("broad", self.broad)):
            if prior is None:
                continue
            if (
                prior.plan_type != self.plan_type
                or prior.exam_effect_type != self.requested_exam_effect_type
            ):
                raise ValueError(f"hierarchical {name} prior has unsupported scope")
            if prior.produce_ids != (self.requested_produce_id,):
                raise ValueError(f"hierarchical {name} prior crosses produce scope")
        if self.exact is not None and (
            not self.requested_idol_card_id
            or self.exact.idol_card_ids != (self.requested_idol_card_id,)
        ):
            raise ValueError("hierarchical exact prior crosses idol scope")

    @property
    def scope_kind(self) -> str:
        return "hierarchical"

    @property
    def evidence_scope(self) -> str:
        """Human-readable selected-scope label for telemetry callers."""

        return "exact" if self.exact is not None else "broad"

    @property
    def source_path(self) -> Path:
        prior = self.exact or self.broad
        assert prior is not None
        return prior.source_path

    @property
    def source_sha256(self) -> str:
        prior = self.exact or self.broad
        assert prior is not None
        return prior.source_sha256

    @property
    def exam_effect_type(self) -> str:
        return self.requested_exam_effect_type

    @property
    def trajectory_count(self) -> int:
        prior = self.exact or self.broad
        assert prior is not None
        return prior.trajectory_count

    @property
    def final_episode_count(self) -> int:
        prior = self.exact or self.broad
        assert prior is not None
        return prior.final_episode_count

    @property
    def master_hashes(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    value
                    for prior in (self.exact, self.broad)
                    if prior is not None
                    for value in prior.master_hashes
                }
            )
        )

    @property
    def app_versions(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    value
                    for prior in (self.exact, self.broad)
                    if prior is not None
                    for value in prior.app_versions
                }
            )
        )

    @property
    def produce_ids(self) -> tuple[str, ...]:
        return (self.requested_produce_id,)

    @property
    def idol_card_ids(self) -> tuple[str, ...]:
        return (
            (self.requested_idol_card_id,)
            if self.requested_idol_card_id
            else ()
        )

    @property
    def scope(self) -> tuple[str, str, str, str | None]:
        """Return the requested hierarchical scope in canonical order."""

        return (
            self.requested_produce_id,
            self.plan_type,
            self.requested_exam_effect_type,
            self.requested_idol_card_id or None,
        )

    @property
    def character_ids(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    value
                    for prior in (self.exact, self.broad)
                    if prior is not None
                    for value in prior.character_ids
                }
            )
        )

    @property
    def statistics(self) -> Mapping[str, LeaderboardCardPriorStat]:
        """Merged view with exact stats taking precedence over broad stats."""

        values: dict[str, LeaderboardCardPriorStat] = {}
        if self.broad is not None:
            values.update(self.broad.statistics)
        if self.exact is not None:
            values.update(self.exact.statistics)
        return values

    def statistic_for(
        self,
        card_id: str,
    ) -> tuple[LeaderboardCardPriorStat | None, str | None]:
        if self.exact is not None:
            stat = self.exact.statistics.get(card_id)
            if stat is not None:
                return stat, "exact"
        if self.broad is not None:
            stat = self.broad.statistics.get(card_id)
            if stat is not None:
                return stat, "broad"
        return None, None

    def scope_for(self, card_id: str) -> str | None:
        return self.statistic_for(card_id)[1]

    def score_for(self, card_id: str, upgrade: int = 0) -> int:
        stat, _ = self.statistic_for(card_id)
        if stat is None:
            return 0
        upgrade_bonus = 0
        if upgrade > 0 and stat.upgraded_copy_count > 0:
            upgrade_bonus = min(
                20,
                round(20 * stat.upgraded_copy_count / stat.copy_count),
            )
        return min(300, stat.bonus + upgrade_bonus)

    def composition_score_for(
        self,
        card_id: str,
        upgrade: int = 0,
        current_count: int = 0,
        future_guaranteed_count: int = 0,
    ) -> int:
        for name, value in (
            ("current_count", current_count),
            ("future_guaranteed_count", future_guaranteed_count),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        stat, _ = self.statistic_for(card_id)
        if stat is None:
            return 0
        gap = max(
            0.0,
            stat.mean_copy_count - current_count - future_guaranteed_count,
        )
        if gap <= 0:
            return 0
        return max(0, min(300, round(self.score_for(card_id, upgrade) * min(1.0, gap))))

    def applies_to(
        self,
        *,
        produce_id: str,
        idol_card_id: str,
        plan_type: str = PLAN2,
        exam_effect_type: str,
    ) -> bool:
        return (
            produce_id == self.requested_produce_id
            and (
                not self.requested_idol_card_id
                or idol_card_id == self.requested_idol_card_id
            )
            and plan_type == self.plan_type
            and exam_effect_type == self.requested_exam_effect_type
        )

    def summary(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "scope_kind": "hierarchical",
            "selected_scope": {
                "produce_id": self.requested_produce_id,
                "plan_type": self.plan_type,
                "idol_card_id": self.requested_idol_card_id,
                "exam_effect_type": self.requested_exam_effect_type,
            },
            "exact": None if self.exact is None else self.exact.summary(),
            "broad": None if self.broad is None else self.broad.summary(),
            "card_count": len(self.statistics),
        }


def _read_episode_source(path: Path) -> tuple[Any, ...]:
    if not path.is_file():
        raise FileNotFoundError(path)
    rows: list[Mapping[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        raw = json.loads(line)
        if not isinstance(raw, Mapping):
            raise ValueError(f"leaderboard episode line {line_number} is not an object")
        rows.append(raw)
    if not rows:
        raise ValueError("leaderboard episode source is empty")
    return tuple(read_leaderboard_episode(row) for row in rows)


def _complete_finals_for_scope(
    episodes: tuple[Any, ...],
    scope: tuple[str, str, str, str],
    *,
    broad: bool = False,
    strict: bool = True,
) -> tuple[Any, ...]:
    """Return one successful Final row per complete trajectory in ``scope``.

    Broad aggregation keys by ``(idol, trajectory_id)`` as well as the
    trajectory ID.  This keeps every trajectory equally weighted even if two
    source idols happen to reuse an identifier, and never groups by the
    complete memory/deck loadout.  Hierarchical broad collection may skip
    malformed/incomplete trajectories; exact loading remains strict.
    """

    grouped: dict[object, list[Any]] = defaultdict(list)
    for episode in episodes:
        if episode.produce_id != scope[0] or episode.plan_type != scope[1]:
            continue
        if episode.exam_effect_type != scope[2]:
            continue
        if not broad and episode.idol_card_id != scope[3]:
            continue
        key: object = (
            (episode.idol_card_id, episode.trajectory_id)
            if broad
            else episode.trajectory_id
        )
        grouped[key].append(episode)

    finals: list[Any] = []
    for key, members in sorted(grouped.items(), key=lambda item: repr(item[0])):
        label = key[1] if isinstance(key, tuple) else key
        if len(members) != 3 or {str(value.step_type) for value in members} != set(_STAGES):
            if strict:
                raise ValueError(f"leaderboard trajectory is incomplete: {label}")
            continue
        if any(value.rank != 1 or value.terminal_score <= 0 for value in members):
            if strict:
                raise ValueError(
                    f"leaderboard trajectory is not a successful rank-1 run: {label}"
                )
            continue
        finals.append(
            next(value for value in members if str(value.step_type) == _STAGES[-1])
        )
    return tuple(finals)


def _build_prior(
    path: Path,
    source_sha256: str,
    finals: tuple[Any, ...],
    *,
    scope_kind: str,
    plan_type: str = PLAN2,
    exam_effect_type: str = REVIEW,
) -> Plan2LeaderboardCardPrior:
    if len(finals) < 3:
        raise ValueError("leaderboard prior has fewer than three complete trajectories")
    maximum_score = max(value.terminal_score for value in finals)
    support: dict[str, int] = defaultdict(int)
    copies: dict[str, int] = defaultdict(int)
    upgraded: dict[str, int] = defaultdict(int)
    scores: dict[str, list[int]] = defaultdict(list)
    for episode in finals:
        seen: set[str] = set()
        for card in episode.produce_cards:
            card_id = card.get("id") if isinstance(card, Mapping) else None
            if not isinstance(card_id, str) or not card_id:
                raise ValueError("leaderboard final deck contains an invalid card ID")
            raw_upgrade = card.get("upgradeCount", 0)
            if isinstance(raw_upgrade, bool) or not isinstance(raw_upgrade, int) or raw_upgrade < 0:
                raise ValueError(f"leaderboard card upgrade is invalid: {card_id}")
            copies[card_id] += 1
            upgraded[card_id] += int(raw_upgrade > 0)
            seen.add(card_id)
        for card_id in seen:
            support[card_id] += 1
            scores[card_id].append(episode.terminal_score)

    statistics: dict[str, LeaderboardCardPriorStat] = {}
    trajectory_count = len(finals)
    for card_id in sorted(support):
        # One occurrence in the complete trajectory set is too little evidence
        # to alter a live choice.  It remains visible for telemetry, but is
        # neutral instead of becoming a penalty or a guessed recommendation.
        if support[card_id] < 2:
            bonus = 0
        else:
            support_bonus = round(180 * support[card_id] / trajectory_count)
            performance_bonus = round(80 * fmean(scores[card_id]) / maximum_score)
            upgrade_bonus = round(40 * upgraded[card_id] / copies[card_id])
            bonus = min(300, support_bonus + performance_bonus + upgrade_bonus)
        statistics[card_id] = LeaderboardCardPriorStat(
            card_id=card_id,
            trajectory_support=support[card_id],
            copy_count=copies[card_id],
            mean_copy_count=copies[card_id] / trajectory_count,
            upgraded_copy_count=upgraded[card_id],
            mean_terminal_score=fmean(scores[card_id]),
            bonus=bonus,
        )

    return Plan2LeaderboardCardPrior(
        source_path=path,
        source_sha256=source_sha256,
        trajectory_count=trajectory_count,
        final_episode_count=len(finals),
        master_hashes=tuple(sorted({value.master_hash for value in finals})),
        app_versions=tuple(sorted({value.app_version for value in finals})),
        produce_ids=tuple(sorted({value.produce_id for value in finals})),
        idol_card_ids=tuple(sorted({value.idol_card_id for value in finals})),
        character_ids=tuple(sorted({value.character_id for value in finals})),
        statistics=statistics,
        plan_type=plan_type,
        exam_effect_type=exam_effect_type,
        schema=SCHEMA if plan_type == PLAN2 and exam_effect_type == REVIEW else GENERIC_SCHEMA,
        scope_kind=scope_kind,
    )


@lru_cache(maxsize=32)
def _load_cached(
    path_text: str,
    produce_id: str,
    idol_card_id: str,
    exam_effect_type: str,
) -> Plan2LeaderboardCardPrior:
    path = Path(path_text)
    episodes = _read_episode_source(path)
    requested = (produce_id, PLAN2, exam_effect_type, idol_card_id)
    available_scopes = {
        (value.produce_id, value.plan_type, value.exam_effect_type, value.idol_card_id)
        for value in episodes
        if value.plan_type == PLAN2 and value.exam_effect_type == REVIEW
    }
    if produce_id and idol_card_id and exam_effect_type:
        selected_scope = requested
        if selected_scope not in available_scopes:
            raise ValueError("leaderboard card prior has no matching exact scope")
    else:
        if produce_id or idol_card_id or exam_effect_type:
            raise ValueError("leaderboard card prior requires a complete exact scope")
        if len(available_scopes) != 1:
            raise ValueError(
                "multi-scope leaderboard episodes require an explicit exact scope"
            )
        selected_scope = next(iter(available_scopes))
    finals = _complete_finals_for_scope(episodes, selected_scope)
    return _build_prior(
        path,
        _sha256(path),
        finals,
        scope_kind="exact",
        plan_type=PLAN2,
        exam_effect_type=REVIEW,
    )


@lru_cache(maxsize=32)
def _load_hierarchical_cached(
    path_text: str,
    produce_id: str,
    plan_type: str,
    idol_card_id: str,
    exam_effect_type: str,
) -> Plan2HierarchicalLeaderboardCardPrior:
    path = Path(path_text)
    episodes = _read_episode_source(path)
    if not all((produce_id, plan_type, exam_effect_type)):
        raise ValueError("hierarchical leaderboard card prior requires produce, plan and effect scope")
    requested = (produce_id, plan_type, exam_effect_type, idol_card_id)
    source_sha256 = _sha256(path)
    exact_finals = (
        _complete_finals_for_scope(episodes, requested)
        if idol_card_id
        else ()
    )
    exact = (
        _build_prior(
            path,
            source_sha256,
            exact_finals,
            scope_kind="exact",
            plan_type=plan_type,
            exam_effect_type=exam_effect_type,
        )
        if len(exact_finals) >= 3
        else None
    )
    broad_scope = (produce_id, plan_type, exam_effect_type, "")
    broad_finals = _complete_finals_for_scope(
        episodes,
        broad_scope,
        broad=True,
        strict=False,
    )
    broad = (
        _build_prior(
            path,
            source_sha256,
            broad_finals,
            scope_kind="broad",
            plan_type=plan_type,
            exam_effect_type=exam_effect_type,
        )
        if len(broad_finals) >= 3
        else None
    )
    return Plan2HierarchicalLeaderboardCardPrior(
        requested_produce_id=produce_id,
        requested_idol_card_id=idol_card_id,
        requested_exam_effect_type=exam_effect_type,
        exact=exact,
        broad=broad,
        schema=(
            HIERARCHICAL_SCHEMA
            if plan_type == PLAN2 and exam_effect_type == REVIEW
            else GENERIC_HIERARCHICAL_SCHEMA
        ),
        plan_type=plan_type,
    )


def load_plan2_leaderboard_card_prior(
    source: str | Path = DEFAULT_LEADERBOARD_EPISODES,
    *,
    produce_id: str = "",
    idol_card_id: str = "",
    exam_effect_type: str = "",
) -> Plan2LeaderboardCardPrior:
    return _load_cached(
        str(Path(source).resolve()),
        produce_id,
        idol_card_id,
        exam_effect_type,
    )


def load_hierarchical_plan2_leaderboard_card_prior(
    source: str | Path = DEFAULT_LEADERBOARD_EPISODES,
    *,
    produce_id: str = "",
    idol_card_id: str = "",
    exam_effect_type: str = "",
) -> Plan2HierarchicalLeaderboardCardPrior:
    """Load exact evidence with a same-produce broad composition fallback.

    The legacy :func:`load_plan2_leaderboard_card_prior` remains exact-only.
    This loader is intentionally explicit about the active idol and effect so
    a caller cannot accidentally create a cross-producer or cross-archetype
    prior.  Broad statistics are computed from one Final deck per successful
    trajectory, with no complete-loadout branch weighting.
    """

    # Preserve the historical Plan2 API contract.  The generic loader below
    # is the explicit entry point for Aggressive and the other four scopes.
    if exam_effect_type != REVIEW:
        raise ValueError("legacy Plan2 prior API only supports Review")

    return _load_hierarchical_cached(
        str(Path(source).resolve()),
        produce_id,
        PLAN2,
        idol_card_id,
        exam_effect_type,
    )


@lru_cache(maxsize=32)
def _load_generic_cached(
    path_text: str,
    produce_id: str,
    plan_type: str,
    idol_card_id: str,
    exam_effect_type: str,
) -> Plan2LeaderboardCardPrior:
    """Load one explicit scope, optionally broad across idol cards.

    The broad form is still bounded by all three non-idol dimensions.  It is
    therefore safe to use for Anomaly/Concentration/Good Impression/Motivation
    and does not silently borrow a Plan2 Review deck for another archetype.
    """

    if not all((produce_id, plan_type, exam_effect_type)):
        raise ValueError("leaderboard card prior requires produce, plan and effect scope")
    path = Path(path_text)
    episodes = _read_episode_source(path)
    scope = (produce_id, plan_type, exam_effect_type, idol_card_id)
    available = {
        (value.produce_id, value.plan_type, value.exam_effect_type, value.idol_card_id)
        for value in episodes
    }
    if idol_card_id and scope not in available:
        raise ValueError("leaderboard card prior has no matching exact scope")
    finals = _complete_finals_for_scope(
        episodes,
        scope,
        broad=not bool(idol_card_id),
        strict=True,
    )
    return _build_prior(
        path,
        _sha256(path),
        finals,
        scope_kind="exact" if idol_card_id else "broad",
        plan_type=plan_type,
        exam_effect_type=exam_effect_type,
    )


def load_leaderboard_card_prior(
    source: str | Path = DEFAULT_LEADERBOARD_EPISODES,
    *,
    produce_id: str,
    plan_type: str,
    exam_effect_type: str,
    idol_card_id: str = "",
) -> Plan2LeaderboardCardPrior:
    """Load a final-deck prior for an explicit archetype scope.

    ``idol_card_id`` is optional: empty means broad same-producer/plan/effect
    evidence, while a value selects one idol-card trajectory group.
    """

    return _load_generic_cached(
        str(Path(source).resolve()),
        produce_id,
        plan_type,
        idol_card_id,
        exam_effect_type,
    )


def load_hierarchical_leaderboard_card_prior(
    source: str | Path = DEFAULT_LEADERBOARD_EPISODES,
    *,
    produce_id: str,
    plan_type: str,
    exam_effect_type: str,
    idol_card_id: str = "",
) -> Plan2HierarchicalLeaderboardCardPrior:
    """Load exact-idol evidence with same-scope broad fallback."""

    return _load_hierarchical_cached(
        str(Path(source).resolve()),
        produce_id,
        plan_type,
        idol_card_id,
        exam_effect_type,
    )


def try_load_default_leaderboard_card_prior(
    *,
    produce_id: str,
    plan_type: str,
    exam_effect_type: str,
    idol_card_id: str = "",
) -> Plan2LeaderboardCardPrior | None:
    try:
        return load_leaderboard_card_prior(
            produce_id=produce_id,
            plan_type=plan_type,
            exam_effect_type=exam_effect_type,
            idol_card_id=idol_card_id,
        )
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
        return None


def try_load_default_hierarchical_leaderboard_card_prior(
    *,
    produce_id: str,
    plan_type: str,
    exam_effect_type: str,
    idol_card_id: str = "",
) -> Plan2HierarchicalLeaderboardCardPrior | None:
    try:
        return load_hierarchical_leaderboard_card_prior(
            produce_id=produce_id,
            plan_type=plan_type,
            exam_effect_type=exam_effect_type,
            idol_card_id=idol_card_id,
        )
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
        return None


# A discoverable alias for callers that prefer the verb-first naming style.
load_plan2_hierarchical_leaderboard_card_prior = (
    load_hierarchical_plan2_leaderboard_card_prior
)
load_plan2_leaderboard_card_prior_hierarchical = (
    load_hierarchical_plan2_leaderboard_card_prior
)


def try_load_default_plan2_leaderboard_card_prior(
    *,
    produce_id: str = "",
    idol_card_id: str = "",
    exam_effect_type: str = "",
) -> Plan2LeaderboardCardPrior | Plan2HierarchicalLeaderboardCardPrior | None:
    try:
        return load_hierarchical_plan2_leaderboard_card_prior(
            produce_id=produce_id,
            idol_card_id=idol_card_id,
            exam_effect_type=exam_effect_type,
        )
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
        return None


def try_load_default_hierarchical_plan2_leaderboard_card_prior(
    *,
    produce_id: str = "",
    idol_card_id: str = "",
    exam_effect_type: str = "",
) -> Plan2HierarchicalLeaderboardCardPrior | None:
    """Best-effort default hierarchical loader for explicit live scope."""

    try:
        return load_hierarchical_plan2_leaderboard_card_prior(
            produce_id=produce_id,
            idol_card_id=idol_card_id,
            exam_effect_type=exam_effect_type,
        )
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
        return None


try_load_default_plan2_leaderboard_card_prior_hierarchical = (
    try_load_default_hierarchical_plan2_leaderboard_card_prior
)

# Keep both common class-name orders importable for downstream integrations.
LeaderboardCardPrior = Plan2LeaderboardCardPrior
HierarchicalLeaderboardCardPrior = Plan2HierarchicalLeaderboardCardPrior
HierarchicalPlan2LeaderboardCardPrior = Plan2HierarchicalLeaderboardCardPrior


__all__ = [
    "DEFAULT_LEADERBOARD_EPISODES",
    "GENERIC_HIERARCHICAL_SCHEMA",
    "GENERIC_SCHEMA",
    "HIERARCHICAL_SCHEMA",
    "HierarchicalLeaderboardCardPrior",
    "HierarchicalPlan2LeaderboardCardPrior",
    "LeaderboardCardPrior",
    "LeaderboardCardPriorStat",
    "Plan2LeaderboardCardPrior",
    "Plan2HierarchicalLeaderboardCardPrior",
    "SCHEMA",
    "load_hierarchical_plan2_leaderboard_card_prior",
    "load_hierarchical_leaderboard_card_prior",
    "load_leaderboard_card_prior",
    "load_plan2_leaderboard_card_prior",
    "load_plan2_hierarchical_leaderboard_card_prior",
    "load_plan2_leaderboard_card_prior_hierarchical",
    "try_load_default_hierarchical_plan2_leaderboard_card_prior",
    "try_load_default_hierarchical_leaderboard_card_prior",
    "try_load_default_leaderboard_card_prior",
    "try_load_default_plan2_leaderboard_card_prior",
    "try_load_default_plan2_leaderboard_card_prior_hierarchical",
]
