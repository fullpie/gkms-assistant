"""Maa-only double-frame observation for generated-card battle animations.

Master/native simulation remains the source of the legal generated-card set.
These screenshots provide an earlier visible identity observation while the
game is still animating; the later ExamSave remains the only native GUID and
settlement authority.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
import time
from typing import Any, Final

from .card_identity import CardNameCatalog, normalize_card_text
from .audition_native_ordered_zones import NativeOrderedCardInstance
from .plan2_native_horizon import (
    Plan2NativeAction,
    Plan2NativeDrinkAction,
    Plan2NativeOfflineAction,
    Plan2NativeProgramCatalog,
)
from .plan2_native_program_catalog import (
    Plan2MasterGeneratedCreateOperation,
    Plan2MasterTriggeredOperation,
)
from .text_recognizer import PaddleLineRecognizer, TextRecognition


GENERATED_CARD_ANIMATION_INTERVAL_SECONDS: Final = 0.6
_CARD_TITLE_CROPS: Final = (
    (140, 470, 580, 550),
    (140, 560, 580, 640),
    (140, 650, 580, 730),
    (140, 740, 580, 820),
    (140, 810, 580, 900),
    (140, 900, 580, 980),
)

CommandSender = Callable[..., Mapping[str, Any]]
Sleep = Callable[[float], None]


@dataclass(frozen=True, slots=True)
class Plan2GeneratedCardAnimationCandidate:
    card_id: str
    upgrade: int
    symbolic_guid: str | None
    zone: str
    aliases: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "card_id": self.card_id,
            "upgrade": self.upgrade,
            "symbolic_guid": self.symbolic_guid,
            "zone": self.zone,
            "aliases": list(self.aliases),
        }


@dataclass(frozen=True, slots=True)
class Plan2GeneratedCardMasterExpectation:
    source_kind: str
    source_id: str
    effect_ids: tuple[str, ...]
    candidates: tuple[tuple[str, int], ...]
    detached_force_play: bool = False

    def __post_init__(self) -> None:
        if self.source_kind not in {"play", "drink"}:
            raise ValueError("unsupported generated-card source kind")
        if not self.source_id:
            raise ValueError("generated-card source id must be non-empty")
        if not self.effect_ids or any(not value for value in self.effect_ids):
            raise ValueError("generated-card effect ids must be non-empty")
        if not self.candidates or any(
            not card_id or upgrade < 0 for card_id, upgrade in self.candidates
        ):
            raise ValueError("generated-card candidates must be non-empty")

    def to_dict(self) -> dict[str, object]:
        return {
            "source_kind": self.source_kind,
            "source_id": self.source_id,
            "effect_ids": list(self.effect_ids),
            "candidates": [
                {"card_id": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.candidates
            ],
            "detached_force_play": self.detached_force_play,
        }


def _eligible_pool_candidates(
    pool: object,
    *,
    ignored_card_ids: tuple[str, ...] = (),
) -> tuple[tuple[str, int], ...]:
    ignored = frozenset(ignored_card_ids)
    values = getattr(pool, "authoritative_candidates", ())
    return tuple(
        dict.fromkeys(
            (
                str(candidate.card_id),
                int(candidate.upgrade_count),
            )
            for candidate in values
            if candidate.plan_type
            in {"ProducePlanType_Common", "ProducePlanType_Plan2"}
            or candidate.card_id in ignored
        )
    )


@lru_cache(maxsize=1)
def _plan2_card_create_search_candidates() -> tuple[tuple[str, int], ...]:
    from .plan2_card_create_search import (
        load_master_plan2_card_create_search_rows,
        resolve_plan2_card_create_search_contract,
    )

    row = load_master_plan2_card_create_search_rows()[0]
    contract = resolve_plan2_card_create_search_contract(row)
    return _eligible_pool_candidates(contract.pool)


def master_generated_card_expectation_for_play(
    card: NativeOrderedCardInstance,
    catalog: Plan2NativeProgramCatalog,
) -> Plan2GeneratedCardMasterExpectation | None:
    if not isinstance(card, NativeOrderedCardInstance):
        raise TypeError("play card must be a native ordered card")
    if not isinstance(catalog, Plan2NativeProgramCatalog):
        raise TypeError("catalog must be a Plan2 native program catalog")
    program = catalog.get(card)
    executor = None if program is None else program.native_playing_executor
    operations = () if executor is None else executor.operations
    effect_ids: list[str] = []
    candidates: list[tuple[str, int]] = []
    for raw in operations:
        operation = (
            raw.operation if isinstance(raw, Plan2MasterTriggeredOperation) else raw
        )
        if not isinstance(operation, Plan2MasterGeneratedCreateOperation):
            continue
        generated = operation.program
        effect_ids.append(generated.effect_id)
        if generated.operation == "card-create-id" and generated.target_card_id:
            candidates.append((generated.target_card_id, generated.target_upgrade))
        elif generated.operation == "card-create-search":
            candidates.extend(_plan2_card_create_search_candidates())
    unique = tuple(dict.fromkeys(candidates))
    if not effect_ids or not unique:
        return None
    return Plan2GeneratedCardMasterExpectation(
        "play",
        card.guid,
        tuple(dict.fromkeys(effect_ids)),
        unique,
    )


def master_generated_card_expectation_for_drink(
    drink_id: str,
    *,
    instance_id: str,
) -> Plan2GeneratedCardMasterExpectation | None:
    if not drink_id or not instance_id:
        raise ValueError("drink id and instance id must be non-empty")
    from .drink_catalog import load_drink_catalog
    from .initial_regular_plan2_drink_runtime import (
        compile_plan2_native_drink_instance,
    )

    drink = load_drink_catalog().get_drink(drink_id)
    compilation = compile_plan2_native_drink_instance(
        drink,
        instance_id=instance_id,
    )
    if not compilation.supported or compilation.instance is None:
        return None
    effect_ids: list[str] = []
    candidates: list[tuple[str, int]] = []
    detached = False
    for effect in compilation.instance.effects:
        if effect.card_create_program is not None:
            effect_ids.append(effect.effect_id)
            candidates.extend(
                _eligible_pool_candidates(effect.card_create_program.pool)
            )
        if effect.force_play_random_pool_program is not None:
            effect_ids.append(effect.effect_id)
            detached = True
            candidates.extend(
                _eligible_pool_candidates(
                    effect.force_play_random_pool_program.selector.pool
                )
            )
    unique = tuple(dict.fromkeys(candidates))
    if not effect_ids or not unique:
        return None
    return Plan2GeneratedCardMasterExpectation(
        "drink",
        instance_id,
        tuple(dict.fromkeys(effect_ids)),
        unique,
        detached_force_play=detached,
    )


def _candidate_aliases(
    catalog: CardNameCatalog,
    *,
    card_id: str,
    upgrade: int,
) -> tuple[str, ...]:
    exact = tuple(
        entry
        for entry in catalog.entries
        if entry.card_id == card_id and entry.upgrade == upgrade
    )
    entries = exact or tuple(
        entry for entry in catalog.entries if entry.card_id == card_id
    )
    return tuple(
        dict.fromkeys(
            value
            for entry in entries
            for value in (entry.official_name, entry.display_name)
            if value.strip()
        )
    )


def _capture_path(capture: Mapping[str, Any]) -> Path:
    value = capture.get("png_path")
    if not isinstance(value, str) or not value:
        raise ValueError("generated-card animation capture has no PNG path")
    path = Path(value).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def _validate_capture_pair(
    first: Mapping[str, Any],
    second: Mapping[str, Any],
) -> None:
    _capture_path(first)
    _capture_path(second)
    for key in ("hwnd", "pid"):
        if first.get(key) != second.get(key):
            raise ValueError(f"generated-card animation capture changed {key}")
    first_timestamp = first.get("timestamp")
    second_timestamp = second.get("timestamp")
    if (
        isinstance(first_timestamp, bool)
        or not isinstance(first_timestamp, (int, float))
        or isinstance(second_timestamp, bool)
        or not isinstance(second_timestamp, (int, float))
        or second_timestamp <= first_timestamp
    ):
        raise ValueError("generated-card animation capture timestamps are invalid")


def _text_similarity(observed: str, alias: str) -> float:
    left = normalize_card_text(observed)
    right = normalize_card_text(alias)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    if left in right or right in left:
        return min(len(left), len(right)) / max(len(left), len(right))
    return SequenceMatcher(None, left, right).ratio()


def _best_candidate_match(
    samples: Sequence[tuple[int, tuple[int, int, int, int], TextRecognition]],
    candidates: Sequence[Plan2GeneratedCardAnimationCandidate],
) -> tuple[dict[str, object] | None, list[dict[str, object]]]:
    rows: list[dict[str, object]] = []
    best_by_frame: dict[int, tuple[float, dict[str, object]]] = {}
    for frame, crop, recognition in samples:
        row: dict[str, object] = {
            "frame": frame,
            "crop": list(crop),
            "text": recognition.text,
            "confidence": recognition.confidence,
        }
        row_best = None
        row_similarity = 0.0
        for candidate in candidates:
            similarity = max(
                (
                    _text_similarity(recognition.text, alias)
                    for alias in candidate.aliases
                ),
                default=0.0,
            )
            if similarity > row_similarity:
                row_similarity = similarity
                row_best = candidate
        if row_best is not None:
            combined = row_similarity * recognition.confidence
            row["candidate_card_id"] = row_best.card_id
            row["candidate_upgrade"] = row_best.upgrade
            row["similarity"] = row_similarity
            row["combined_score"] = combined
            if (
                recognition.confidence >= 0.45
                and row_similarity >= 0.62
                and (
                    frame not in best_by_frame
                    or combined > best_by_frame[frame][0]
                )
            ):
                best_by_frame[frame] = (
                    combined,
                    {
                        "card_id": row_best.card_id,
                        "upgrade": row_best.upgrade,
                        "symbolic_guid": row_best.symbolic_guid,
                        "zone": row_best.zone,
                        "text": recognition.text,
                        "ocr_confidence": recognition.confidence,
                        "similarity": row_similarity,
                        "combined_score": combined,
                        "frame": frame,
                        "crop": list(crop),
                    },
                )
        rows.append(row)
    if set(best_by_frame) != {1, 2}:
        return None, rows
    first = best_by_frame[1][1]
    second = best_by_frame[2][1]
    if (first["card_id"], first["upgrade"]) != (
        second["card_id"],
        second["upgrade"],
    ):
        return None, rows
    strongest = max((first, second), key=lambda value: value["combined_score"])
    return {
        **strongest,
        "confirmed_frames": [1, 2],
        "minimum_combined_score": min(
            best_by_frame[1][0],
            best_by_frame[2][0],
        ),
    }, rows


def observe_plan2_generated_card_animation(
    action: Plan2NativeOfflineAction,
    expectation: Plan2GeneratedCardMasterExpectation,
    *,
    predicted_generated_cards: Sequence[tuple[object, str]] = (),
    command_sender: CommandSender | None = None,
    sleep: Sleep = time.sleep,
    interval_seconds: float = GENERATED_CARD_ANIMATION_INTERVAL_SECONDS,
    recognizer: PaddleLineRecognizer | None = None,
    catalog: CardNameCatalog | None = None,
) -> Mapping[str, object]:
    """Capture two Maa frames for one Master-proven generation action."""

    if not isinstance(action, (Plan2NativeAction, Plan2NativeDrinkAction)):
        raise TypeError("action must be a typed Plan2 offline action")
    if not isinstance(expectation, Plan2GeneratedCardMasterExpectation):
        raise TypeError("expectation must be typed Master generation evidence")
    if action.kind != expectation.source_kind:
        raise ValueError("generation expectation does not match action kind")
    if not callable(sleep):
        raise TypeError("sleep must be callable")
    if interval_seconds < 0 or interval_seconds > 5:
        raise ValueError("generated-card animation interval is out of range")
    if command_sender is None:
        from .controller_client import send_command

        command_sender = send_command
    if recognizer is None:
        from .live_source import _live_text_recognizer

        recognizer = _live_text_recognizer()
    if catalog is None:
        catalog = CardNameCatalog.load()

    predicted_by_ref: dict[tuple[str, int], tuple[object, str]] = {}
    for card, zone_name in predicted_generated_cards:
        predicted_by_ref[
            (
                str(getattr(card, "card_id")),
                int(getattr(card, "effective_upgrade")),
            )
        ] = (card, zone_name)
    candidates = tuple(
        Plan2GeneratedCardAnimationCandidate(
            card_id=card_id,
            upgrade=upgrade,
            symbolic_guid=(
                None
                if (card_id, upgrade) not in predicted_by_ref
                else str(getattr(predicted_by_ref[(card_id, upgrade)][0], "guid"))
            ),
            zone=(
                "detached"
                if (card_id, upgrade) not in predicted_by_ref
                else predicted_by_ref[(card_id, upgrade)][1]
            ),
            aliases=_candidate_aliases(
                catalog,
                card_id=card_id,
                upgrade=upgrade,
            ),
        )
        for card_id, upgrade in expectation.candidates
    )
    first = dict(command_sender("capture_screen_once", timeout=15.0))
    sleep(interval_seconds)
    second = dict(command_sender("capture_screen_once", timeout=15.0))
    _validate_capture_pair(first, second)

    samples = tuple(
        (
            frame,
            crop,
            recognizer.recognize_path(_capture_path(capture), crop),
        )
        for frame, capture in enumerate((first, second), start=1)
        for crop in _CARD_TITLE_CROPS
    )
    observed, ocr_rows = _best_candidate_match(samples, candidates)
    return {
        "schema": "gkms.plan2-generated-card-animation.v1",
        "status": "observed" if observed is not None else "captured-unresolved",
        "action_id": action.action_id,
        "interval_seconds": interval_seconds,
        "static_authority": "master-generated-card-candidate-set",
        "visual_authority": "maa-background-double-frame-diagnostic",
        "native_guid_authority": "later-examsave-only",
        "master_expectation": expectation.to_dict(),
        "candidates": [value.to_dict() for value in candidates],
        "captures": [first, second],
        "observed_card": observed,
        "ocr_samples": ocr_rows,
    }


__all__ = [
    "GENERATED_CARD_ANIMATION_INTERVAL_SECONDS",
    "Plan2GeneratedCardAnimationCandidate",
    "observe_plan2_generated_card_animation",
]
