"""Plan-neutral identity binding for a native playing-card GUID."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


_MISSING = object()


@dataclass(frozen=True, slots=True, order=True)
class PlayingCardIdentityIssue:
    field: str
    reason: str


@dataclass(frozen=True, slots=True)
class PlayingCardIdentityResolution:
    playing_guid: str
    native_card: Any | None
    issues: tuple[PlayingCardIdentityIssue, ...] = ()

    @property
    def resolved(self) -> bool:
        return not self.issues


def resolve_playing_card_identity(
    native_state: object,
    playing_guid: str,
    playing_card: object,
    *,
    reason_context: str,
) -> PlayingCardIdentityResolution:
    """Bind GUID -> native Hand card -> typed id/effective upgrade.

    The identity remains a Hand identity at the ``ExamCardPlay`` capture
    point: native ``SetPlayingCard`` has run, but physical card movement is a
    later command.  Missing evidence is unresolved, never a search miss.
    """

    issues: list[PlayingCardIdentityIssue] = []
    native_card: Any | None = None
    if not isinstance(playing_guid, str) or not playing_guid:
        issues.append(
            PlayingCardIdentityIssue(
                "playing_guid", f"playing-guid-invalid:{reason_context}"
            )
        )
    if not issues:
        lookup = getattr(native_state, "card_by_guid", None)
        if not callable(lookup):
            issues.append(
                PlayingCardIdentityIssue(
                    "native_state",
                    f"native-guid-lookup-unavailable:{reason_context}",
                )
            )
        else:
            try:
                native_card = lookup(playing_guid)
            except Exception:
                issues.append(
                    PlayingCardIdentityIssue(
                        "playing_guid", f"playing-guid-not-found:{playing_guid}"
                    )
                )
    if not issues:
        hand = getattr(native_state, "hand", _MISSING)
        if hand is _MISSING:
            issues.append(
                PlayingCardIdentityIssue(
                    "native_state.hand",
                    f"native-hand-unavailable:{reason_context}",
                )
            )
        else:
            try:
                is_in_hand = any(
                    getattr(card, "guid", None) == playing_guid for card in hand
                )
            except TypeError:
                is_in_hand = False
                issues.append(
                    PlayingCardIdentityIssue(
                        "native_state.hand",
                        f"native-hand-invalid:{reason_context}",
                    )
                )
            if not issues and not is_in_hand:
                issues.append(
                    PlayingCardIdentityIssue(
                        "playing_guid", f"playing-guid-not-in-hand:{playing_guid}"
                    )
                )
    if not issues:
        card_id = getattr(playing_card, "id", _MISSING)
        card_upgrade = getattr(playing_card, "upgrade", _MISSING)
        if card_id is _MISSING or card_upgrade is _MISSING:
            issues.append(
                PlayingCardIdentityIssue(
                    "playing_card",
                    f"playing-card-typed-fields-unavailable:{reason_context}",
                )
            )
        elif getattr(native_card, "card_id", _MISSING) != card_id:
            issues.append(
                PlayingCardIdentityIssue(
                    "playing_card.id", f"native-card-id-mismatch:{playing_guid}"
                )
            )
        elif getattr(native_card, "effective_upgrade", _MISSING) != card_upgrade:
            issues.append(
                PlayingCardIdentityIssue(
                    "playing_card.upgrade",
                    f"native-card-upgrade-mismatch:{playing_guid}",
                )
            )
    return PlayingCardIdentityResolution(
        playing_guid=playing_guid,
        native_card=native_card,
        issues=tuple(issues),
    )


__all__ = [
    "PlayingCardIdentityIssue",
    "PlayingCardIdentityResolution",
    "resolve_playing_card_identity",
]
