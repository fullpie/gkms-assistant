"""Extract de-identified NIA outer candidate/choice rows from 3.3.0 histories.

Unlike audition replay episodes, ``produceHistory.schedules`` contains both the
selected step and the complete server-returned step list for each schedule
number.  A history is retained only when all three audition situations are
present; an incomplete history is discarded as one unit.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
from .application_paths import game_file
import tempfile
from typing import Any, Mapping, Sequence

from .nia_route_profile import nia_stage_positions
from .leaderboard_replay import (
    LeaderboardRawHistoryCollection,
    LeaderboardRawHistoryRecord,
    _leaderboard_query_failure_code,
    _sources_include_directory,
    collect_list_latest_history_sources,
    list_leaderboard_raw_sources,
)
from .overview_actions import ACTIVITY, DANCE_LESSON, REST, VOCAL_LESSON, VISUAL_LESSON
from .route_calendar import (
    BUSINESS,
    CONSULTATION,
    OUTING,
    SPECIAL_GUIDANCE,
    load_route_calendar,
)


SCHEMA = "gkms.leaderboard-outer-trajectory.v1"
PRIOR_SCHEMA = "gkms.leaderboard-outer-choice-prior.v1"
DEFAULT_RAW_HISTORY = game_file('gakumas-local/dump-files/leaderboard-replay/list-latest-history-1787277616592.json')
DEFAULT_OUTPUT = (
    Path(__file__).resolve().parents[2]
    / "var"
    / "leaderboard_dataset"
    / "v330_nia_first_capture"
    / "outer_schedules.jsonl"
)
_AUDITIONS = (
    "ProduceStepType_AuditionMid1",
    "ProduceStepType_AuditionMid2",
    "ProduceStepType_AuditionFinal",
)
_STEP_ACTION = {
    "ProduceStepType_Business": (BUSINESS, False),
    # These aliases are bound by same-week PC 3.3.0 observations from the
    # exact leaderboard-scope run: W3 FanPresent/EventActivity/Refresh maps
    # bijectively to activity/outing/rest, and W6 EventActivity/Shop/Refresh
    # maps to outing/consultation/rest.  The raw enum remains stored alongside
    # the canonical UI action for auditability.
    "ProduceStepType_FanPresent": (ACTIVITY, False),
    "ProduceStepType_EventActivity": (OUTING, False),
    "ProduceStepType_Shop": (CONSULTATION, False),
    "ProduceStepType_Customize": (SPECIAL_GUIDANCE, False),
    "ProduceStepType_Refresh": (REST, False),
    "ProduceStepType_SelfLessonVocalNormal": (VOCAL_LESSON, False),
    "ProduceStepType_SelfLessonVocalSp": (VOCAL_LESSON, True),
    "ProduceStepType_SelfLessonDanceNormal": (DANCE_LESSON, False),
    "ProduceStepType_SelfLessonDanceSp": (DANCE_LESSON, True),
    "ProduceStepType_SelfLessonVisualNormal": (VISUAL_LESSON, False),
    "ProduceStepType_SelfLessonVisualSp": (VISUAL_LESSON, True),
}


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class LeaderboardOuterCandidate:
    step_type: str
    action: str
    is_sp: bool


@dataclass(frozen=True, slots=True)
class LeaderboardOuterChoice:
    number: int
    chosen_step_type: str
    chosen_action: str
    chosen_is_sp: bool
    candidates: tuple[LeaderboardOuterCandidate, ...]
    candidate_set_complete: bool = True


@dataclass(frozen=True, slots=True)
class LeaderboardOuterTrajectory:
    trajectory_id: str
    produce_id: str
    idol_card_id: str
    character_id: str
    plan_type: str
    exam_effect_type: str
    app_version: str
    master_version: str
    master_hash: str
    # Provenance is carried from the immutable raw collection all the way to
    # each schedule observation.  A trajectory can be de-identified while its
    # source bytes remain auditable by hash.
    source_sha256: str
    history_score: int
    choices: tuple[LeaderboardOuterChoice, ...]
    sources: tuple[str, ...] = ()
    schema: str = SCHEMA

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @property
    def source_hash(self) -> str:
        """Alias retained for callers that use the shorter provenance name."""

        return self.source_sha256


@dataclass(frozen=True, slots=True)
class LeaderboardOuterChoicePrior:
    trajectories: tuple[LeaderboardOuterTrajectory, ...]
    schema: str = PRIOR_SCHEMA

    @property
    def trajectory_count(self) -> int:
        return len(self.trajectories)

    @property
    def observation_count(self) -> int:
        return sum(len(value.choices) for value in self.trajectories)

    def applies_to(
        self,
        *,
        produce_id: str,
        idol_card_id: str,
        exam_effect_type: str,
    ) -> bool:
        return bool(self.trajectories) and all(
            value.produce_id == produce_id
            and value.idol_card_id == idol_card_id
            and value.exam_effect_type == exam_effect_type
            for value in self.trajectories
        )

    def rank(
        self,
        features: Mapping[str, object],
        legal_actions: Sequence[str],
    ) -> tuple[str, ...]:
        week = features.get("week")
        if isinstance(week, bool) or not isinstance(week, int):
            return ()
        legal = tuple(dict.fromkeys(action for action in legal_actions if isinstance(action, str)))
        legal_set = set(legal)
        scores = {action: 0 for action in legal}
        for trajectory in self.trajectories:
            for choice in trajectory.choices:
                if choice.number != week:
                    continue
                candidate_actions = {value.action for value in choice.candidates}
                if legal_set != candidate_actions:
                    continue
                if choice.chosen_action in scores:
                    scores[choice.chosen_action] += 1
        if not scores or max(scores.values(), default=0) <= 0:
            return ()
        order = {action: index for index, action in enumerate(legal)}
        return tuple(sorted(legal, key=lambda action: (-scores[action], order[action])))


def _contest_identity(history: Mapping[str, Any]) -> Mapping[str, str] | None:
    auditions = history.get("auditions")
    if not isinstance(auditions, list) or len(auditions) != 3:
        return None
    if {value.get("stepType") for value in auditions if isinstance(value, Mapping)} != set(_AUDITIONS):
        return None
    identities: list[dict[str, str]] = []
    for audition in auditions:
        if not isinstance(audition, Mapping):
            return None
        situation = audition.get("examContestSituation")
        if not isinstance(situation, Mapping):
            return None
        stages = situation.get("stages")
        if not isinstance(stages, list) or len(stages) != 1 or not isinstance(stages[0], Mapping):
            return None
        sections = stages[0].get("selfSections")
        if not isinstance(sections, list) or len(sections) != 1 or not isinstance(sections[0], Mapping):
            return None
        player = sections[0].get("player")
        if not isinstance(player, Mapping) or not isinstance(player.get("examActions"), list) or not player["examActions"]:
            return None
        identity = {
            "app_version": str(situation.get("appVersion", "")),
            "master_version": str(situation.get("masterVersion", "")),
            "master_hash": str(situation.get("masterHash", "")),
            "plan_type": str(stages[0].get("planType", "")),
            "character_id": str(player.get("characterId", "")),
            "idol_card_id": str(player.get("idolCardId", "")),
            "exam_effect_type": str(player.get("examEffectType", "")),
        }
        if any(not value for value in identity.values()):
            return None
        identities.append(identity)
    return identities[0] if all(value == identities[0] for value in identities) else None


def _route_shape(produce_id: str) -> tuple[int, Mapping[int, str]] | None:
    """Return the authoritative week/stage shape for one N.I.A. mode.

    The leaderboard response is a raw source, so its schedule length and
    audition rows must never be inferred from a previously observed mode.  The
    shared route calendar validates the current Master step count while the
    N.I.A. route profile pins the three mode-specific audition positions.  A
    disagreement is treated as unsupported data and the history is discarded.
    """

    try:
        calendar = load_route_calendar(produce_id)
        profile = nia_stage_positions(produce_id)
    except (FileNotFoundError, KeyError, OSError, TypeError, ValueError):
        return None

    try:
        profile_stages = tuple(
            sorted(
                ((week, phase) for week, phase in profile.values()),
                key=lambda item: item[1],
            )
        )
    except (TypeError, ValueError):
        return None
    profile_weeks = tuple(week for week, _phase in profile_stages)
    calendar_stages = tuple(
        (milestone.week, milestone.step_type) for milestone in calendar.milestones
    )
    if (
        len(profile_weeks) != len(_AUDITIONS)
        or len(set(profile_weeks)) != len(profile_weeks)
        or tuple(phase for _week, phase in profile_stages) != (1, 2, 3)
        or calendar.total_weeks != max(profile_weeks, default=0)
        or tuple(week for week, _step_type in calendar_stages)
        != profile_weeks
        or tuple(step_type for _week, step_type in calendar_stages) != _AUDITIONS
    ):
        return None
    return calendar.total_weeks, dict(calendar_stages)


def _parse_outer_history(
    history: Mapping[str, Any],
) -> tuple[str, Mapping[str, str], tuple[LeaderboardOuterChoice, ...]] | None:
    """Validate and project one de-identified ``produceHistory`` object.

    This is deliberately history-atomic: a malformed schedule, identity, or
    route shape returns ``None`` for the whole history.  Callers never receive
    a partial list of weekly choices.
    """

    produce_id = history.get("produceId")
    if not isinstance(produce_id, str) or not produce_id:
        return None
    score = history.get("score", 0)
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return None
    route_shape = _route_shape(produce_id)
    if route_shape is None:
        return None
    total_weeks, audition_by_week = route_shape
    audition_weeks = set(audition_by_week)
    identity = _contest_identity(history)
    if identity is None:
        return None
    schedules = history.get("schedules")
    if not isinstance(schedules, list) or len(schedules) != total_weeks:
        return None
    choices: list[LeaderboardOuterChoice] = []
    schedule_numbers: set[int] = set()
    malformed = False
    for row in schedules:
        if not isinstance(row, Mapping):
            malformed = True
            break
        number = row.get("number")
        selected = row.get("selectedStepType")
        step_types = row.get("stepTypes")
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or not 1 <= number <= total_weeks
            or number in schedule_numbers
            or not isinstance(selected, str)
            or not isinstance(step_types, list)
            or selected not in step_types
            or any(not isinstance(value, str) for value in step_types)
        ):
            malformed = True
            break
        schedule_numbers.add(number)
        if number in audition_weeks:
            if selected != audition_by_week[number]:
                malformed = True
                break
            continue
        if selected in _AUDITIONS:
            # An audition at an unprofiled week would otherwise be silently
            # dropped and could make an incomplete raw schedule look valid.
            malformed = True
            break
        if selected not in _STEP_ACTION or any(
            value not in _STEP_ACTION for value in step_types if value not in _AUDITIONS
        ):
            malformed = True
            break
        mapped = tuple(
            LeaderboardOuterCandidate(value, *_STEP_ACTION[value])
            for value in step_types
            if value not in _AUDITIONS
        )
        chosen_action, chosen_sp = _STEP_ACTION[selected]
        choices.append(
            LeaderboardOuterChoice(
                number,
                selected,
                chosen_action,
                chosen_sp,
                mapped,
            )
        )
    if (
        malformed
        or schedule_numbers != set(range(1, total_weeks + 1))
        or len(choices) != total_weeks - len(audition_weeks)
    ):
        return None
    return produce_id, identity, tuple(choices)


def _outer_history_validator(history: Mapping[str, Any]) -> str | None:
    """Collection-level validator used to drop malformed outer histories."""

    return None if _parse_outer_history(history) is not None else "incomplete-outer-history"


def _minimal_outer_collection(source: str | Path | Sequence[str | Path]) -> LeaderboardRawHistoryCollection | None:
    """Compatibility bridge for tiny schedule-only test captures.

    Real ListLatestHistory captures go through ``collect_list_latest_history_sources``
    and therefore require replay-complete histories.  Older offline fixtures
    intentionally contain only the outer schedule and contest identity.  They
    remain useful for route-shape tests, so this narrow fallback projects them
    without weakening the normal collector or accepting a missing contest
    identity/schedule.
    """

    try:
        raw_sources = list_leaderboard_raw_sources(source)
    except (FileNotFoundError, TypeError, ValueError):
        return None
    records: list[LeaderboardRawHistoryRecord] = []
    seen_source_hashes: set[str] = set()
    seen_history_identities: set[str] = set()
    duplicate_history_count = 0
    source_files: list[dict[str, object]] = []
    skipped_failed_sources: list[dict[str, object]] = []
    allow_failed_query_result_skip = _sources_include_directory(source)
    for raw_source in raw_sources:
        path = raw_source.path
        try:
            data = path.read_bytes()
            root = json.loads(data.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if isinstance(root, Mapping):
            failed_code = _leaderboard_query_failure_code(root)
            if failed_code is not None:
                if not allow_failed_query_result_skip:
                    return None
                skipped_failed_sources.append(
                    {
                        "path": raw_source.name,
                        "error_code": failed_code,
                    }
                )
                continue
        histories = root.get("histories") if isinstance(root, Mapping) else None
        if not isinstance(histories, list) or not histories:
            return None
        source_hash = hashlib.sha256(data).hexdigest()
        duplicate_raw = source_hash in seen_source_hashes
        seen_source_hashes.add(source_hash)
        source_files.append(
            {
                "name": raw_source.name,
                "path": raw_source.name,
                "sha256": source_hash,
                "source_type": "ListLatestHistory",
                "history_count": len(histories),
                "duplicate_raw": duplicate_raw,
            }
        )
        for index, wrapper in enumerate(histories):
            history = wrapper.get("produceHistory") if isinstance(wrapper, Mapping) else None
            if not isinstance(history, Mapping) or _parse_outer_history(history) is None:
                continue
            history_identity = "history:" + hashlib.sha256(
                _canonical(history)
            ).hexdigest()
            if history_identity in seen_history_identities:
                duplicate_history_count += 1
                continue
            seen_history_identities.add(history_identity)
            records.append(
                LeaderboardRawHistoryRecord(
                    source_name=raw_source.name,
                    source_sha256=source_hash,
                    source_type="ListLatestHistory",
                    history_index=index,
                    history_identity=history_identity,
                    history=dict(history),
                    sources=("list_latest_history",),
                )
            )
    if not records:
        return None
    return LeaderboardRawHistoryCollection(
        records=tuple(records),
        dropped=(),
        source_files=tuple(source_files),
        source_file_count=len(raw_sources),
        unique_source_hash_count=len(seen_source_hashes),
        duplicate_source_count=0,
        duplicate_history_count=duplicate_history_count,
        skipped_failed_sources=tuple(skipped_failed_sources),
    )


def _outer_collection(
    source: str | Path | Sequence[str | Path] | LeaderboardRawHistoryCollection,
    *,
    allow_schedule_only_fallback: bool = True,
) -> LeaderboardRawHistoryCollection:
    if isinstance(source, LeaderboardRawHistoryCollection):
        return source
    collection = collect_list_latest_history_sources(
        source,
        validator=_outer_history_validator,
    )
    if collection.records:
        return collection
    if allow_schedule_only_fallback:
        fallback = _minimal_outer_collection(source)
        if fallback is not None:
            return fallback
    return collection


def collect_leaderboard_outer_history_sources(
    source: str | Path | Sequence[str | Path] | LeaderboardRawHistoryCollection = DEFAULT_RAW_HISTORY,
) -> LeaderboardRawHistoryCollection:
    """Return the immutable, de-duplicated history collection used by outer.

    The public helper lets formal training manifests include the same
    source-hash and history-drop audit as the standalone outer writer without
    reimplementing collection policy.
    """

    return _outer_collection(source, allow_schedule_only_fallback=False)


def extract_leaderboard_outer_trajectories(
    source: str | Path | Sequence[str | Path] | LeaderboardRawHistoryCollection = DEFAULT_RAW_HISTORY,
    *,
    allow_schedule_only_fallback: bool = True,
) -> tuple[LeaderboardOuterTrajectory, ...]:
    collection = _outer_collection(
        source,
        allow_schedule_only_fallback=allow_schedule_only_fallback,
    )
    result: list[LeaderboardOuterTrajectory] = []
    for record in collection.records:
        history = record.history
        parsed = _parse_outer_history(history)
        if parsed is None:
            continue
        produce_id, identity, choices = parsed
        payload = {
            "produce_id": produce_id,
            "idol_card_id": identity["idol_card_id"],
            "history_score": history.get("score", 0),
            "choices": [asdict(value) for value in choices],
        }
        trajectory_id = "outer-trajectory:" + hashlib.sha256(_canonical(payload)).hexdigest()
        result.append(
            LeaderboardOuterTrajectory(
                trajectory_id=trajectory_id,
                produce_id=produce_id,
                idol_card_id=identity["idol_card_id"],
                character_id=identity["character_id"],
                plan_type=identity["plan_type"],
                exam_effect_type=identity["exam_effect_type"],
                app_version=identity["app_version"],
                master_version=identity["master_version"],
                master_hash=identity["master_hash"],
                source_sha256=record.source_sha256,
                history_score=int(history.get("score", 0)),
                choices=choices,
                sources=record.sources,
            )
        )
    if not result:
        raise ValueError("leaderboard response has no complete outer trajectories")
    return tuple(result)


@lru_cache(maxsize=2)
def _prior_cached(source_text: str) -> LeaderboardOuterChoicePrior:
    return LeaderboardOuterChoicePrior(
        extract_leaderboard_outer_trajectories(
            source_text,
            allow_schedule_only_fallback=False,
        )
    )


def load_leaderboard_outer_choice_prior(
    source: str | Path | Sequence[str | Path] | LeaderboardRawHistoryCollection = DEFAULT_RAW_HISTORY,
) -> LeaderboardOuterChoicePrior:
    if isinstance(source, LeaderboardRawHistoryCollection):
        return LeaderboardOuterChoicePrior(
            extract_leaderboard_outer_trajectories(
                source,
                allow_schedule_only_fallback=False,
            )
        )
    if isinstance(source, (str, Path)):
        return _prior_cached(str(Path(source).resolve()))
    # Lists are intentionally not used as an lru-cache key: callers can pass
    # an explicit ordered list and the collection itself handles byte/history
    # de-duplication deterministically.
    return LeaderboardOuterChoicePrior(
        extract_leaderboard_outer_trajectories(
            source,
            allow_schedule_only_fallback=False,
        )
    )


def _atomic_write_bytes(target: Path, payload: bytes) -> None:
    """Write one canonical artifact without exposing a partial destination."""

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_name = stream.name
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, target)
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def write_leaderboard_outer_dataset(
    output: str | Path = DEFAULT_OUTPUT,
    *,
    source: str | Path | Sequence[str | Path] | LeaderboardRawHistoryCollection = DEFAULT_RAW_HISTORY,
) -> dict[str, object]:
    target = Path(output)
    if not isinstance(source, LeaderboardRawHistoryCollection):
        try:
            raw_sources = list_leaderboard_raw_sources(source)
        except (FileNotFoundError, TypeError, ValueError):
            # Extraction below retains the established error and compatibility
            # path for legacy fixtures; this branch is only a target safety
            # check for valid paths.
            raw_sources = ()
        target_resolved = target.resolve()
        manifest_resolved = target.with_suffix(".manifest.json").resolve()
        for raw_source in raw_sources:
            if raw_source.path.resolve() in {target_resolved, manifest_resolved}:
                raise ValueError("outer dataset output must not overwrite a raw source")
    collection = _outer_collection(source, allow_schedule_only_fallback=False)
    trajectories = tuple(
        sorted(
            extract_leaderboard_outer_trajectories(collection),
            key=lambda value: (
                value.produce_id,
                value.idol_card_id,
                value.source_sha256,
                value.trajectory_id,
            ),
        )
    )
    text = "".join(
        json.dumps(value.to_dict(), ensure_ascii=False, sort_keys=True) + "\n"
        for value in trajectories
    )
    payload = text.encode("utf-8")
    _atomic_write_bytes(target, payload)
    manifest = {
        "schema": "gkms.leaderboard-outer-manifest.v1",
        "trajectory_count": len(trajectories),
        "observation_count": sum(len(value.choices) for value in trajectories),
        "candidate_sets_complete": True,
        "output_path": str(target.resolve()),
        "output_sha256": hashlib.sha256(payload).hexdigest(),
        "source_collection": collection.to_manifest(),
        "source_sha256": sorted({value.source_sha256 for value in trajectories}),
    }
    manifest_path = target.with_suffix(".manifest.json")
    manifest["manifest_path"] = str(manifest_path.resolve())
    _atomic_write_bytes(
        manifest_path,
        (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    return manifest


__all__ = [
    "DEFAULT_OUTPUT",
    "DEFAULT_RAW_HISTORY",
    "LeaderboardOuterChoicePrior",
    "collect_leaderboard_outer_history_sources",
    "extract_leaderboard_outer_trajectories",
    "load_leaderboard_outer_choice_prior",
    "write_leaderboard_outer_dataset",
]
