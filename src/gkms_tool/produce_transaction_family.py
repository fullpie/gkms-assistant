"""Source-neutral Produce transaction-family contract.

This module is deliberately pure data.  It does not inspect the game, route a
screen, execute Maa, or own transaction state.  Its only job is to answer two
stable questions:

* which of the nine outer transaction families owns a ``ProduceStepType``;
* which phase an official ``ProduceApiUtility`` endpoint proves.

Card/drink/item/card-operation/numeric effect continuations are shared child
surfaces.  They are not promoted to outer families here.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Mapping


FAMILY_LESSON: Final = "lesson"
FAMILY_EVENT: Final = "event"
FAMILY_PRESENT: Final = "present"
FAMILY_SHOP: Final = "shop"
FAMILY_REFRESH: Final = "refresh"
FAMILY_BUSINESS: Final = "business"
FAMILY_CUSTOMIZE: Final = "customize"
FAMILY_INTERVAL: Final = "interval"
FAMILY_AUDITION: Final = "audition"

TRANSACTION_FAMILIES: Final = (
    FAMILY_LESSON,
    FAMILY_EVENT,
    FAMILY_PRESENT,
    FAMILY_SHOP,
    FAMILY_REFRESH,
    FAMILY_BUSINESS,
    FAMILY_CUSTOMIZE,
    FAMILY_INTERVAL,
    FAMILY_AUDITION,
)

# Canonical live SelectedAction -> official native transaction family.  These
# are action identities emitted by the shared N.I.A. selected-action chain;
# the mapping is intentionally separate from player-facing labels and from
# ProduceStepType enum projection.
OUTER_ACTION_FAMILY_BY_ACTION: Final[Mapping[str, str]] = MappingProxyType(
    {
        "vocal_lesson": FAMILY_LESSON,
        "dance_lesson": FAMILY_LESSON,
        "visual_lesson": FAMILY_LESSON,
        "activity": FAMILY_EVENT,
        "outing": FAMILY_EVENT,
        "consultation": FAMILY_SHOP,
        "rest": FAMILY_REFRESH,
        "special_guidance": FAMILY_CUSTOMIZE,
        "business": FAMILY_BUSINESS,
        "exam": FAMILY_AUDITION,
    }
)

TERMINAL_NEVER: Final = "never"
TERMINAL_ALWAYS: Final = "always"
TERMINAL_IF_NO_NEXT_STEP: Final = "response-without-next-step"
_TERMINAL_RULES: Final = frozenset(
    {TERMINAL_NEVER, TERMINAL_ALWAYS, TERMINAL_IF_NO_NEXT_STEP}
)

# These may follow any outer family when the response carries such an effect.
# They must never be used to guess or replace the owning outer transaction.
COMMON_EFFECT_CONTINUATIONS: Final = frozenset(
    {"card", "drink", "item", "strengthen", "delete", "change", "numeric"}
)

# Authority: installed PC 3.3.0 global-metadata.dat.  Tokens and parameter
# counts are metadata identities only; this module does not call these methods.
CURRENT_METADATA_SHA256: Final = (
    "9A6BF153C0C42A2768E619CC7D96FA79FCC1D341E9CF9BCBDC8D6BCA48812668"
)
CURRENT_METADATA_TYPE: Final = "Campus.InGame.Produce.ProduceApiUtility"
CURRENT_SHARED_RECEIPT_TOKEN_EVIDENCE: Final[Mapping[str, tuple[str, int]]] = (
    MappingProxyType(
        {
            "Campus.InGame.Produce.ProduceApiUtility.SaveApiResultLog": (
                "0x06003B3D",
                4,
            ),
            "Campus.Common.ProduceLog.ProducePlayLogSaveData.AddLog": (
                "0x06016DA8",
                1,
            ),
        }
    )
)


@dataclass(frozen=True, slots=True)
class EndpointPhase:
    endpoint: str
    family: str
    phase: str
    terminal: str = TERMINAL_NEVER
    repeatable: bool = False
    token: str = ""
    parameter_count: int = 0

    def __post_init__(self) -> None:
        if not self.endpoint or not self.phase:
            raise ValueError("endpoint and phase must be non-empty")
        if self.family not in TRANSACTION_FAMILIES:
            raise ValueError(f"unknown transaction family {self.family!r}")
        if self.terminal not in _TERMINAL_RULES:
            raise ValueError(f"unknown terminal rule {self.terminal!r}")
        if type(self.repeatable) is not bool:
            raise TypeError("repeatable must be boolean")
        if not self.token.startswith("0x06") or len(self.token) != 10:
            raise ValueError("token must be a MethodDef token such as 0x06003B10")
        if isinstance(self.parameter_count, bool) or self.parameter_count < 1:
            raise ValueError("parameter_count must be positive")


@dataclass(frozen=True, slots=True)
class TransactionVariant:
    """One official lifecycle variant within an outer family.

    ``phases`` is the semantic order.  ``endpoints`` contains the actual API
    boundaries that prove those phases; the lesson ``exam`` phase is an inner
    runtime boundary and therefore intentionally has no fake API endpoint.
    Operation endpoints are alternatives and may repeat until the end endpoint.
    """

    name: str
    family: str
    phases: tuple[str, ...]
    endpoints: tuple[str, ...]
    terminal: str

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("variant name must be non-empty")
        if self.family not in TRANSACTION_FAMILIES:
            raise ValueError(f"unknown transaction family {self.family!r}")
        if not self.phases or any(not value for value in self.phases):
            raise ValueError("variant phases must be non-empty")
        if not self.endpoints or any(not value for value in self.endpoints):
            raise ValueError("variant endpoints must be non-empty")
        if self.terminal not in _TERMINAL_RULES:
            raise ValueError(f"unknown terminal rule {self.terminal!r}")


@dataclass(frozen=True, slots=True)
class TransactionFamily:
    family: str
    variants: tuple[TransactionVariant, ...]

    def __post_init__(self) -> None:
        if self.family not in TRANSACTION_FAMILIES:
            raise ValueError(f"unknown transaction family {self.family!r}")
        if not self.variants:
            raise ValueError("transaction family must have at least one variant")
        if any(value.family != self.family for value in self.variants):
            raise ValueError("variant is owned by another transaction family")


def _endpoint(
    endpoint: str,
    family: str,
    phase: str,
    token: str,
    parameter_count: int,
    *,
    terminal: str = TERMINAL_NEVER,
    repeatable: bool = False,
) -> EndpointPhase:
    return EndpointPhase(
        endpoint,
        family,
        phase,
        terminal,
        repeatable,
        token,
        parameter_count,
    )


_ENDPOINT_ROWS: Final = (
    _endpoint("ProduceStepLessonStartRequestAsync", FAMILY_LESSON, "start", "0x06003B10", 1),
    _endpoint("ProduceStepLessonEndRequestAsync", FAMILY_LESSON, "end", "0x06003B11", 10, terminal=TERMINAL_ALWAYS),
    _endpoint("ProduceStepAuditionStartRequestAsync", FAMILY_AUDITION, "start", "0x06003B14", 1),
    _endpoint("ProduceStepAuditionExamEndRequestAsync", FAMILY_AUDITION, "exam-end", "0x06003B16", 13),
    _endpoint("ProduceStepAuditionEndRequestAsync", FAMILY_AUDITION, "end", "0x06003B17", 1, terminal=TERMINAL_ALWAYS),
    _endpoint("ProduceStepShopBuyRequestAsync", FAMILY_SHOP, "operation", "0x06003B18", 2, repeatable=True),
    _endpoint("ProduceStepShopDeleteCardRequestAsync", FAMILY_SHOP, "operation", "0x06003B19", 3, repeatable=True),
    _endpoint("ProduceStepShopUpgradeCardRequestAsync", FAMILY_SHOP, "operation", "0x06003B1A", 3, repeatable=True),
    _endpoint("ProduceStepShopStartRequestAsync", FAMILY_SHOP, "start", "0x06003B1B", 1),
    _endpoint("ProduceStepShopEndRequestAsync", FAMILY_SHOP, "end", "0x06003B1C", 1, terminal=TERMINAL_ALWAYS),
    _endpoint("ProduceStepEventRequestAsync", FAMILY_EVENT, "request", "0x06003B1D", 4, terminal=TERMINAL_IF_NO_NEXT_STEP, repeatable=True),
    _endpoint("ProduceStepRefreshRequestAsync", FAMILY_REFRESH, "response", "0x06003B1E", 1, terminal=TERMINAL_ALWAYS),
    _endpoint("ProduceStepPresentReceiveRequestAsync", FAMILY_PRESENT, "receive", "0x06003B1F", 3, repeatable=True),
    _endpoint("ProduceStepPresentStartRequestAsync", FAMILY_PRESENT, "start", "0x06003B20", 1),
    _endpoint("ProduceStepPresentEndRequestAsync", FAMILY_PRESENT, "end", "0x06003B21", 1, terminal=TERMINAL_ALWAYS),
    _endpoint("ProduceStepSelfLessonStartRequestAsync", FAMILY_LESSON, "start", "0x06003B22", 1),
    _endpoint("ProduceStepSelfLessonEndRequestAsync", FAMILY_LESSON, "end", "0x06003B23", 1, terminal=TERMINAL_ALWAYS),
    _endpoint("ProduceStepCustomizeStartRequestAsync", FAMILY_CUSTOMIZE, "start", "0x06003B24", 1),
    _endpoint("ProduceStepCustomizeSelectRequestAsync", FAMILY_CUSTOMIZE, "select", "0x06003B25", 3, repeatable=True),
    _endpoint("ProduceStepCustomizeEndRequestAsync", FAMILY_CUSTOMIZE, "end", "0x06003B26", 1, terminal=TERMINAL_ALWAYS),
    _endpoint("ProduceStepBusinessStartRequestAsync", FAMILY_BUSINESS, "start", "0x06003B27", 1),
    _endpoint("ProduceStepBusinessSelectRequestAsync", FAMILY_BUSINESS, "select", "0x06003B28", 2, terminal=TERMINAL_ALWAYS),
    _endpoint("ProduceStepOpenLessonStartRequestAsync", FAMILY_LESSON, "start", "0x06003B29", 1),
    _endpoint("ProduceStepOpenLessonEndRequestAsync", FAMILY_LESSON, "end", "0x06003B2A", 1, terminal=TERMINAL_ALWAYS),
    _endpoint("ProduceShopRerollAsync", FAMILY_SHOP, "operation", "0x06003B2E", 1, repeatable=True),
    _endpoint("ProduceStepIntervalStartRequestAsync", FAMILY_INTERVAL, "start", "0x06003B42", 1),
    _endpoint("ProduceStepIntervalEndRequestAsync", FAMILY_INTERVAL, "end", "0x06003B43", 1, terminal=TERMINAL_ALWAYS),
    _endpoint("ProduceIntervalRerollAsync", FAMILY_INTERVAL, "operation", "0x06003B44", 1, repeatable=True),
    _endpoint("ProduceStepIntervalBuyRequestAsync", FAMILY_INTERVAL, "operation", "0x06003B45", 2, repeatable=True),
    _endpoint("ProduceStepIntervalChangeCardRequestAsync", FAMILY_INTERVAL, "operation", "0x06003B46", 3, repeatable=True),
    _endpoint("ProduceStepIntervalUpgradeCardRequestAsync", FAMILY_INTERVAL, "operation", "0x06003B47", 3, repeatable=True),
    _endpoint("ProduceStepIntervalRecoverStaminaRequestAsync", FAMILY_INTERVAL, "operation", "0x06003B48", 3, repeatable=True),
    _endpoint("ProduceStepIntervalCustomizeRequestAsync", FAMILY_INTERVAL, "operation", "0x06003B49", 4, repeatable=True),
)

ENDPOINT_PHASES: Final[Mapping[str, EndpointPhase]] = MappingProxyType(
    {value.endpoint: value for value in _ENDPOINT_ROWS}
)
CURRENT_ENDPOINT_TOKEN_EVIDENCE: Final[Mapping[str, tuple[str, int]]] = (
    MappingProxyType(
        {
            value.endpoint: (value.token, value.parameter_count)
            for value in _ENDPOINT_ROWS
        }
    )
)


def _variant(
    name: str,
    family: str,
    phases: tuple[str, ...],
    endpoints: tuple[str, ...],
    terminal: str = TERMINAL_ALWAYS,
) -> TransactionVariant:
    return TransactionVariant(name, family, phases, endpoints, terminal)


_LESSON_INITIAL = _variant(
    "initial",
    FAMILY_LESSON,
    ("start", "exam", "end"),
    ("ProduceStepLessonStartRequestAsync", "ProduceStepLessonEndRequestAsync"),
)
_LESSON_SELF = _variant(
    "nia-self",
    FAMILY_LESSON,
    ("start", "exam", "end"),
    ("ProduceStepSelfLessonStartRequestAsync", "ProduceStepSelfLessonEndRequestAsync"),
)
_LESSON_OPEN = _variant(
    "open",
    FAMILY_LESSON,
    ("start", "exam", "end"),
    ("ProduceStepOpenLessonStartRequestAsync", "ProduceStepOpenLessonEndRequestAsync"),
)

FAMILY_SPECS: Final[Mapping[str, TransactionFamily]] = MappingProxyType(
    {
        FAMILY_LESSON: TransactionFamily(
            FAMILY_LESSON, (_LESSON_INITIAL, _LESSON_SELF, _LESSON_OPEN)
        ),
        FAMILY_EVENT: TransactionFamily(
            FAMILY_EVENT,
            (
                _variant(
                    "event-next-step-chain",
                    FAMILY_EVENT,
                    ("request", "optional-child-next-step"),
                    ("ProduceStepEventRequestAsync",),
                    TERMINAL_IF_NO_NEXT_STEP,
                ),
            ),
        ),
        FAMILY_PRESENT: TransactionFamily(
            FAMILY_PRESENT,
            (
                _variant(
                    "present",
                    FAMILY_PRESENT,
                    ("start", "receive*", "end"),
                    (
                        "ProduceStepPresentStartRequestAsync",
                        "ProduceStepPresentReceiveRequestAsync",
                        "ProduceStepPresentEndRequestAsync",
                    ),
                ),
            ),
        ),
        FAMILY_SHOP: TransactionFamily(
            FAMILY_SHOP,
            (
                _variant(
                    "shop",
                    FAMILY_SHOP,
                    ("start", "operation*", "end"),
                    (
                        "ProduceStepShopStartRequestAsync",
                        "ProduceStepShopBuyRequestAsync",
                        "ProduceStepShopDeleteCardRequestAsync",
                        "ProduceStepShopUpgradeCardRequestAsync",
                        "ProduceShopRerollAsync",
                        "ProduceStepShopEndRequestAsync",
                    ),
                ),
            ),
        ),
        FAMILY_REFRESH: TransactionFamily(
            FAMILY_REFRESH,
            (
                _variant(
                    "refresh",
                    FAMILY_REFRESH,
                    ("response",),
                    ("ProduceStepRefreshRequestAsync",),
                ),
            ),
        ),
        FAMILY_BUSINESS: TransactionFamily(
            FAMILY_BUSINESS,
            (
                _variant(
                    "business",
                    FAMILY_BUSINESS,
                    ("start", "select"),
                    (
                        "ProduceStepBusinessStartRequestAsync",
                        "ProduceStepBusinessSelectRequestAsync",
                    ),
                ),
            ),
        ),
        FAMILY_CUSTOMIZE: TransactionFamily(
            FAMILY_CUSTOMIZE,
            (
                _variant(
                    "customize",
                    FAMILY_CUSTOMIZE,
                    ("start", "select*", "end"),
                    (
                        "ProduceStepCustomizeStartRequestAsync",
                        "ProduceStepCustomizeSelectRequestAsync",
                        "ProduceStepCustomizeEndRequestAsync",
                    ),
                ),
            ),
        ),
        FAMILY_INTERVAL: TransactionFamily(
            FAMILY_INTERVAL,
            (
                _variant(
                    "interval",
                    FAMILY_INTERVAL,
                    ("start", "operation*", "end"),
                    (
                        "ProduceStepIntervalStartRequestAsync",
                        "ProduceIntervalRerollAsync",
                        "ProduceStepIntervalBuyRequestAsync",
                        "ProduceStepIntervalChangeCardRequestAsync",
                        "ProduceStepIntervalUpgradeCardRequestAsync",
                        "ProduceStepIntervalRecoverStaminaRequestAsync",
                        "ProduceStepIntervalCustomizeRequestAsync",
                        "ProduceStepIntervalEndRequestAsync",
                    ),
                ),
            ),
        ),
        FAMILY_AUDITION: TransactionFamily(
            FAMILY_AUDITION,
            (
                _variant(
                    "audition",
                    FAMILY_AUDITION,
                    ("start", "exam-end", "end"),
                    (
                        "ProduceStepAuditionStartRequestAsync",
                        "ProduceStepAuditionExamEndRequestAsync",
                        "ProduceStepAuditionEndRequestAsync",
                    ),
                ),
            ),
        ),
    }
)


_STEP_NAMES: Final = (
    "ProduceStepType_LessonVocalNormal",
    "ProduceStepType_LessonVocalSp",
    "ProduceStepType_LessonVocalHard",
    "ProduceStepType_LessonDanceNormal",
    "ProduceStepType_LessonDanceSp",
    "ProduceStepType_LessonDanceHard",
    "ProduceStepType_LessonVisualNormal",
    "ProduceStepType_LessonVisualSp",
    "ProduceStepType_LessonVisualHard",
    "ProduceStepType_Event",
    "ProduceStepType_EventActivity",
    "ProduceStepType_EventSchool",
    "ProduceStepType_Shop",
    "ProduceStepType_Refresh",
    "ProduceStepType_Present",
    "ProduceStepType_AuditionMid1",
    "ProduceStepType_AuditionMid2",
    "ProduceStepType_AuditionFinal",
    "ProduceStepType_SelfLessonVocalNormal",
    "ProduceStepType_SelfLessonVocalSp",
    "ProduceStepType_SelfLessonDanceNormal",
    "ProduceStepType_SelfLessonDanceSp",
    "ProduceStepType_SelfLessonVisualNormal",
    "ProduceStepType_SelfLessonVisualSp",
    "ProduceStepType_Business",
    "ProduceStepType_EventBusiness",
    "ProduceStepType_FanPresent",
    "ProduceStepType_Customize",
    "ProduceStepType_LegendLessonVocalNormal",
    "ProduceStepType_LegendLessonVocalSp",
    "ProduceStepType_LegendLessonDanceNormal",
    "ProduceStepType_LegendLessonDanceSp",
    "ProduceStepType_LegendLessonVisualNormal",
    "ProduceStepType_LegendLessonVisualSp",
    "ProduceStepType_OpenLessonVocalNormal",
    "ProduceStepType_OpenLessonVocalSp",
    "ProduceStepType_OpenLessonVocalNormalStar",
    "ProduceStepType_OpenLessonVocalSpStar",
    "ProduceStepType_OpenLessonDanceNormal",
    "ProduceStepType_OpenLessonDanceSp",
    "ProduceStepType_OpenLessonDanceNormalStar",
    "ProduceStepType_OpenLessonDanceSpStar",
    "ProduceStepType_OpenLessonVisualNormal",
    "ProduceStepType_OpenLessonVisualSp",
    "ProduceStepType_OpenLessonVisualNormalStar",
    "ProduceStepType_OpenLessonVisualSpStar",
    "ProduceStepType_Interval",
    "ProduceStepType_EventSchoolVocal",
    "ProduceStepType_EventSchoolDance",
    "ProduceStepType_EventSchoolVisual",
)


def _family_from_step_name(name: str) -> str:
    if "Lesson" in name:
        return FAMILY_LESSON
    if name.startswith("ProduceStepType_Audition"):
        return FAMILY_AUDITION
    if name in {
        "ProduceStepType_Event",
        "ProduceStepType_EventActivity",
        "ProduceStepType_EventSchool",
        "ProduceStepType_EventBusiness",
        "ProduceStepType_EventSchoolVocal",
        "ProduceStepType_EventSchoolDance",
        "ProduceStepType_EventSchoolVisual",
    }:
        return FAMILY_EVENT
    return {
        "ProduceStepType_Shop": FAMILY_SHOP,
        "ProduceStepType_Refresh": FAMILY_REFRESH,
        "ProduceStepType_Present": FAMILY_PRESENT,
        "ProduceStepType_FanPresent": FAMILY_PRESENT,
        "ProduceStepType_Business": FAMILY_BUSINESS,
        "ProduceStepType_Customize": FAMILY_CUSTOMIZE,
        "ProduceStepType_Interval": FAMILY_INTERVAL,
    }[name]


STEP_TYPE_FAMILY_BY_NAME: Final[Mapping[str, str]] = MappingProxyType(
    {name: _family_from_step_name(name) for name in _STEP_NAMES}
)
STEP_TYPE_FAMILY_BY_VALUE: Final[Mapping[int, str]] = MappingProxyType(
    {
        value: STEP_TYPE_FAMILY_BY_NAME[name]
        for value, name in enumerate(_STEP_NAMES, start=1)
    }
)
STEP_TYPE_NAME_BY_VALUE: Final[Mapping[int, str]] = MappingProxyType(
    {value: name for value, name in enumerate(_STEP_NAMES, start=1)}
)


def family_for_step_type(step_type: str | int) -> str | None:
    """Return the owning family, or ``None`` for an unknown value.

    Returning ``None`` is intentional fail-closed behaviour: callers must not
    fall back to running all detectors when a new enum value appears.
    """

    if isinstance(step_type, bool):
        return None
    if isinstance(step_type, int):
        return STEP_TYPE_FAMILY_BY_VALUE.get(step_type)
    if isinstance(step_type, str):
        return STEP_TYPE_FAMILY_BY_NAME.get(step_type)
    return None


def family_for_outer_action(action: object) -> str | None:
    """Map one canonical live outer action to its native receipt family.

    Unknown or non-text values return ``None`` so training joins fail closed;
    callers must not infer a family from a localized label or visible tile.
    """

    if not isinstance(action, str):
        return None
    return OUTER_ACTION_FAMILY_BY_ACTION.get(action)


def phase_for_endpoint(endpoint: str) -> EndpointPhase | None:
    """Return exact endpoint evidence, or ``None`` for an unknown endpoint."""

    if not isinstance(endpoint, str):
        return None
    return ENDPOINT_PHASES.get(endpoint)


__all__ = [
    "COMMON_EFFECT_CONTINUATIONS",
    "CURRENT_ENDPOINT_TOKEN_EVIDENCE",
    "CURRENT_METADATA_SHA256",
    "CURRENT_METADATA_TYPE",
    "CURRENT_SHARED_RECEIPT_TOKEN_EVIDENCE",
    "ENDPOINT_PHASES",
    "EndpointPhase",
    "FAMILY_AUDITION",
    "FAMILY_BUSINESS",
    "FAMILY_CUSTOMIZE",
    "FAMILY_EVENT",
    "FAMILY_INTERVAL",
    "FAMILY_LESSON",
    "FAMILY_PRESENT",
    "FAMILY_REFRESH",
    "FAMILY_SHOP",
    "FAMILY_SPECS",
    "OUTER_ACTION_FAMILY_BY_ACTION",
    "STEP_TYPE_FAMILY_BY_NAME",
    "STEP_TYPE_FAMILY_BY_VALUE",
    "STEP_TYPE_NAME_BY_VALUE",
    "TERMINAL_ALWAYS",
    "TERMINAL_IF_NO_NEXT_STEP",
    "TERMINAL_NEVER",
    "TRANSACTION_FAMILIES",
    "TransactionFamily",
    "TransactionVariant",
    "family_for_step_type",
    "family_for_outer_action",
    "phase_for_endpoint",
]
