"""Authoritative full-deck snapshots reconstructed from the visible card panel.

The future UI reader is responsible for opening the in-game lower-right card
panel, scrolling every page, and removing overlap between page observations.
This module deliberately starts after that visual step: it validates the
resulting card multiset, persists it, and compares it with the run's shadow
state.  It never reads game-process memory.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DECK_SNAPSHOT_PATH = PROJECT_ROOT / "var" / "deck_snapshot.json"
DECK_SNAPSHOT_SCHEMA_VERSION = 1
DEFAULT_AUTHORITATIVE_CONFIDENCE = 0.80
DEFAULT_SELECTED_CARD_DETAIL_CONFIDENCE = 0.98


@dataclass(frozen=True, slots=True)
class DeckCardStack:
    card_id: str
    upgrade: int
    count: int = 1
    confidence: float = 1.0

    @property
    def key(self) -> tuple[str, int]:
        return self.card_id, self.upgrade

    def validate(self) -> None:
        if not self.card_id:
            raise ValueError("牌組卡牌 ID 不可為空")
        if self.upgrade < 0:
            raise ValueError(f"牌組卡牌強化值不可為負數：{self.card_id}")
        if self.count < 1:
            raise ValueError(f"牌組卡牌張數必須至少為 1：{self.card_id}")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"牌組卡牌辨識信心超出 0..1：{self.card_id}")


def normalize_card_stacks(
    cards: Iterable[DeckCardStack],
) -> tuple[DeckCardStack, ...]:
    """Merge repeated rows of the same card while retaining worst confidence."""

    counts: Counter[tuple[str, int]] = Counter()
    confidences: dict[tuple[str, int], float] = {}
    for card in cards:
        card.validate()
        counts[card.key] += card.count
        confidences[card.key] = min(
            confidences.get(card.key, 1.0), card.confidence
        )
    return tuple(
        DeckCardStack(
            card_id=card_id,
            upgrade=upgrade,
            count=counts[(card_id, upgrade)],
            confidence=confidences[(card_id, upgrade)],
        )
        for card_id, upgrade in sorted(counts)
    )


@dataclass(frozen=True, slots=True)
class DeckSnapshot:
    produce_id: str
    step_type: str
    stage_number: int
    cards: tuple[DeckCardStack, ...]
    complete: bool
    unresolved_slots: int = 0
    source: str = "visible-deck-panel"

    @property
    def total_cards(self) -> int:
        return sum(card.count for card in self.cards)

    @property
    def minimum_confidence(self) -> float:
        return min((card.confidence for card in self.cards), default=0.0)

    def contains_card_id(self, card_id: str) -> bool:
        if not card_id:
            raise ValueError("required card ID is empty")
        return any(card.card_id == card_id and card.count > 0 for card in self.cards)

    def validate(self) -> None:
        if not self.produce_id:
            raise ValueError("牌組快照缺少 produce_id")
        if not self.step_type:
            raise ValueError("牌組快照缺少 step_type")
        if self.stage_number < 1:
            raise ValueError("牌組快照 stage_number 必須至少為 1")
        if self.unresolved_slots < 0:
            raise ValueError("牌組快照 unresolved_slots 不可為負數")
        if not self.source:
            raise ValueError("牌組快照缺少 source")
        normalized = normalize_card_stacks(self.cards)
        if normalized != self.cards:
            raise ValueError("牌組快照 cards 必須先正規化並依 ID 排序")

    def is_authoritative(
        self, *, confidence_threshold: float = DEFAULT_AUTHORITATIVE_CONFIDENCE
    ) -> bool:
        if not 0.0 <= confidence_threshold <= 1.0:
            raise ValueError("confidence_threshold 必須介於 0 與 1")
        return (
            self.complete
            and self.unresolved_slots == 0
            and self.total_cards > 0
            and self.minimum_confidence >= confidence_threshold
        )

    def to_dict(self) -> dict[str, object]:
        self.validate()
        return {
            "schema_version": DECK_SNAPSHOT_SCHEMA_VERSION,
            "produce_id": self.produce_id,
            "step_type": self.step_type,
            "stage_number": self.stage_number,
            "cards": [asdict(card) for card in self.cards],
            "complete": self.complete,
            "unresolved_slots": self.unresolved_slots,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "DeckSnapshot":
        expected_fields = {
            "schema_version",
            "produce_id",
            "step_type",
            "stage_number",
            "cards",
            "complete",
            "unresolved_slots",
            "source",
        }
        if set(payload) != expected_fields:
            raise ValueError("deck snapshot fields are invalid")
        if not isinstance(payload.get("produce_id"), str):
            raise ValueError("deck snapshot produce_id must be a string")
        if not isinstance(payload.get("step_type"), str):
            raise ValueError("deck snapshot step_type must be a string")
        if type(payload.get("stage_number")) is not int:
            raise ValueError("deck snapshot stage_number must be an integer")
        if type(payload.get("complete")) is not bool:
            raise ValueError("deck snapshot complete must be a boolean")
        if type(payload.get("unresolved_slots")) is not int:
            raise ValueError("deck snapshot unresolved_slots must be an integer")
        if not isinstance(payload.get("source"), str):
            raise ValueError("deck snapshot source must be a string")
        if payload.get("schema_version") != DECK_SNAPSHOT_SCHEMA_VERSION:
            raise ValueError("不支援的牌組快照格式")
        raw_cards = payload.get("cards")
        if not isinstance(raw_cards, list) or not all(
            isinstance(item, Mapping) for item in raw_cards
        ):
            raise ValueError("牌組快照 cards 格式錯誤")
        for item in raw_cards:
            if set(item) != {"card_id", "upgrade", "count", "confidence"}:
                raise ValueError("deck card fields are invalid")
            if not isinstance(item.get("card_id"), str):
                raise ValueError("deck card card_id must be a string")
            if type(item.get("upgrade")) is not int:
                raise ValueError("deck card upgrade must be an integer")
            if type(item.get("count")) is not int:
                raise ValueError("deck card count must be an integer")
            confidence = item.get("confidence")
            if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
                raise ValueError("deck card confidence must be numeric")
        cards = tuple(
            DeckCardStack(
                card_id=item["card_id"],
                upgrade=item["upgrade"],
                count=item["count"],
                confidence=float(item["confidence"]),
            )
            for item in raw_cards
        )
        snapshot = cls(
            produce_id=payload["produce_id"],
            step_type=payload["step_type"],
            stage_number=payload["stage_number"],
            cards=cards,
            complete=payload["complete"],
            unresolved_slots=payload["unresolved_slots"],
            source=payload["source"],
        )
        snapshot.validate()
        return snapshot


def build_deck_snapshot(
    *,
    produce_id: str,
    step_type: str,
    stage_number: int,
    cards: Iterable[DeckCardStack],
    complete: bool,
    unresolved_slots: int = 0,
    source: str = "visible-deck-panel",
) -> DeckSnapshot:
    snapshot = DeckSnapshot(
        produce_id=produce_id,
        step_type=step_type,
        stage_number=stage_number,
        cards=normalize_card_stacks(cards),
        complete=complete,
        unresolved_slots=unresolved_slots,
        source=source,
    )
    snapshot.validate()
    return snapshot


def _compact_catalog_text(value: str) -> str:
    """Compare catalog titles exactly while ignoring only display whitespace."""

    return "".join(value.split())


@dataclass(frozen=True, slots=True)
class SelectedCardDetailEvidence:
    """One OCR reading of the title on an explicitly selected owned card.

    The UI reader must resolve ``observed_title`` against the local card
    catalog before constructing this value.  Keeping both strings makes the
    later deck correction auditable and prevents a bare card ID from being
    treated as title evidence.
    """

    run_id: str
    produce_id: str
    step_type: str
    stage_number: int
    card_id: str
    upgrade: int
    observed_title: str
    catalog_title: str
    catalog_card_id: str
    catalog_upgrade: int
    ocr_confidence: float
    evidence_path: str

    @property
    def exact_catalog_match(self) -> bool:
        return bool(
            _compact_catalog_text(self.observed_title)
            and _compact_catalog_text(self.observed_title)
            == _compact_catalog_text(self.catalog_title)
        )

    def validate(self) -> None:
        if not self.run_id or not self.produce_id or not self.step_type:
            raise ValueError("selected card detail identity is incomplete")
        if self.stage_number < 1:
            raise ValueError("selected card detail stage_number must be positive")
        if not self.card_id or self.upgrade < 0:
            raise ValueError("selected card detail card identity is invalid")
        if not self.observed_title or not self.catalog_title:
            raise ValueError("selected card detail titles are required")
        if (
            self.catalog_card_id != self.card_id
            or self.catalog_upgrade != self.upgrade
        ):
            raise ValueError("selected card detail catalog identity does not match")
        if not self.exact_catalog_match:
            raise ValueError("selected card detail OCR is not an exact catalog match")
        if not 0.0 <= self.ocr_confidence <= 1.0:
            raise ValueError("selected card detail OCR confidence must be 0..1")
        if not self.evidence_path:
            raise ValueError("selected card detail evidence path is required")

    def to_dict(self) -> dict[str, object]:
        self.validate()
        return {
            "run_id": self.run_id,
            "produce_id": self.produce_id,
            "step_type": self.step_type,
            "stage_number": self.stage_number,
            "card_id": self.card_id,
            "upgrade": self.upgrade,
            "observed_title": self.observed_title,
            "catalog_title": self.catalog_title,
            "catalog_card_id": self.catalog_card_id,
            "catalog_upgrade": self.catalog_upgrade,
            "exact_catalog_match": self.exact_catalog_match,
            "ocr_confidence": self.ocr_confidence,
            "evidence_path": self.evidence_path,
        }


@dataclass(frozen=True, slots=True)
class DeckUpgradeReconciliation:
    """An explicit, one-copy upgrade correction backed by title evidence.

    This is intentionally a pure result: callers must persist both this diff
    and ``corrected_snapshot`` themselves.  It never writes a run artifact or
    changes a shadow state as a side effect.
    """

    run_id: str
    evidence: SelectedCardDetailEvidence
    previous_stack: DeckCardStack
    corrected_stack: DeckCardStack
    corrected_snapshot: DeckSnapshot

    @property
    def deck_delta(self) -> Mapping[str, int]:
        return {
            f"{self.previous_stack.card_id}@{self.previous_stack.upgrade}": -1,
            f"{self.corrected_stack.card_id}@{self.corrected_stack.upgrade}": 1,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": "selected_card_detail_upgrade_reconciliation",
            "accepted": True,
            "run_id": self.run_id,
            "snapshot_identity": {
                "produce_id": self.corrected_snapshot.produce_id,
                "step_type": self.corrected_snapshot.step_type,
                "stage_number": self.corrected_snapshot.stage_number,
            },
            "evidence": self.evidence.to_dict(),
            "previous_stack": asdict(self.previous_stack),
            "corrected_stack": asdict(self.corrected_stack),
            "deck_delta": dict(self.deck_delta),
            "corrected_snapshot": self.corrected_snapshot.to_dict(),
        }


def reconcile_selected_card_detail_upgrade(
    snapshot: DeckSnapshot,
    evidence: SelectedCardDetailEvidence,
    *,
    expected_run_id: str,
    confidence_threshold: float = DEFAULT_SELECTED_CARD_DETAIL_CONFIDENCE,
) -> DeckUpgradeReconciliation:
    """Correct exactly one wrongly-read upgrade, or reject without mutation.

    An owned-deck tile can occasionally identify the artwork correctly but
    miss its tiny ``+`` overlay.  A selected-detail title is stronger evidence
    for the card identity but does not identify an arbitrary duplicate, so we
    only permit the correction when the old stack has exactly one copy and the
    target upgrade is absent.  Every other shape is deliberately ambiguous.
    """

    snapshot.validate()
    evidence.validate()
    if not expected_run_id or evidence.run_id != expected_run_id:
        raise ValueError("selected card detail belongs to a different run")
    if not 0.0 <= confidence_threshold <= 1.0:
        raise ValueError("confidence_threshold must be 0..1")
    if evidence.ocr_confidence < confidence_threshold:
        raise ValueError("selected card detail OCR confidence is too low")
    if (
        evidence.produce_id != snapshot.produce_id
        or evidence.step_type != snapshot.step_type
        or evidence.stage_number != snapshot.stage_number
    ):
        raise ValueError("selected card detail stage identity does not match deck snapshot")

    same_card = tuple(card for card in snapshot.cards if card.card_id == evidence.card_id)
    if not same_card:
        raise ValueError("selected card detail card is absent from deck snapshot")
    source = tuple(card for card in same_card if card.upgrade != evidence.upgrade)
    target = tuple(card for card in same_card if card.upgrade == evidence.upgrade)
    if len(source) != 1 or source[0].count != 1:
        raise ValueError("selected card detail does not identify a unique movable copy")
    if target:
        raise ValueError("selected card detail target upgrade is already present and ambiguous")

    previous = source[0]
    corrected = DeckCardStack(
        card_id=previous.card_id,
        upgrade=evidence.upgrade,
        count=1,
        # The lower confidence remains visible in the snapshot; the detail
        # evidence is represented separately in the reconciliation artifact.
        confidence=previous.confidence,
    )
    cards = tuple(
        corrected if card == previous else card
        for card in snapshot.cards
    )
    corrected_snapshot = replace(snapshot, cards=normalize_card_stacks(cards))
    corrected_snapshot.validate()
    if corrected_snapshot.total_cards != snapshot.total_cards:
        raise AssertionError("selected card detail correction changed deck size")
    return DeckUpgradeReconciliation(
        run_id=expected_run_id,
        evidence=evidence,
        previous_stack=previous,
        corrected_stack=corrected,
        corrected_snapshot=corrected_snapshot,
    )


@dataclass(frozen=True, slots=True)
class DeckReconciliation:
    exact_match: bool
    observed_authoritative: bool
    expected_total: int
    observed_total: int
    added: tuple[DeckCardStack, ...]
    removed: tuple[DeckCardStack, ...]

    @property
    def may_replace_shadow_state(self) -> bool:
        return self.observed_authoritative


def _counter(cards: Iterable[DeckCardStack]) -> Counter[tuple[str, int]]:
    return Counter(
        {
            card.key: card.count
            for card in normalize_card_stacks(cards)
        }
    )


def reconcile_deck(
    expected: Iterable[DeckCardStack],
    observed: DeckSnapshot,
    *,
    confidence_threshold: float = DEFAULT_AUTHORITATIVE_CONFIDENCE,
) -> DeckReconciliation:
    """Compare shadow state with one visible full-deck panel snapshot."""

    observed.validate()
    expected_counts = _counter(expected)
    observed_counts = _counter(observed.cards)
    added_counts = observed_counts - expected_counts
    removed_counts = expected_counts - observed_counts
    confidence_by_key = {card.key: card.confidence for card in observed.cards}
    added = tuple(
        DeckCardStack(
            card_id=card_id,
            upgrade=upgrade,
            count=count,
            confidence=confidence_by_key[(card_id, upgrade)],
        )
        for (card_id, upgrade), count in sorted(added_counts.items())
    )
    removed = tuple(
        DeckCardStack(card_id, upgrade, count)
        for (card_id, upgrade), count in sorted(removed_counts.items())
    )
    return DeckReconciliation(
        exact_match=not added and not removed,
        observed_authoritative=observed.is_authoritative(
            confidence_threshold=confidence_threshold
        ),
        expected_total=sum(expected_counts.values()),
        observed_total=sum(observed_counts.values()),
        added=added,
        removed=removed,
    )


def load_deck_snapshot(
    path: Path = DEFAULT_DECK_SNAPSHOT_PATH,
) -> DeckSnapshot | None:
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("牌組快照根節點必須是 object")
    return DeckSnapshot.from_dict(payload)


def save_deck_snapshot(
    snapshot: DeckSnapshot,
    path: Path = DEFAULT_DECK_SNAPSHOT_PATH,
) -> None:
    snapshot.validate()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
