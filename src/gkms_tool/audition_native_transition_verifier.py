"""Strict predicted-versus-observed verification for native audition actions.

This module deliberately does not reuse the legacy ordered-zone transition
analyzer.  A native horizon prediction already contains the exact next Logic
state, ordered card instances, and RNG state; verification is therefore plain
canonical equality.  No multiset reconciliation, random-state distance search,
or inferred transition is permitted here.

The result is an audit record, not a live-click authorization certificate.
The caller must independently bind the prediction's complete pre state and the
``expected_post_session_transition_id`` to the current live transaction.  A
serialized result, including one reconstructed with ``from_dict``, is ordinary
JSON and must never be treated as an unforgeable credential.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
import hashlib
import json
import math
import re
from typing import Iterable, Mapping

from .audition_horizon import HorizonCardRef, HorizonSearchLimits, VerifiedTurnSchedule
from .audition_native_horizon import (
    NativeAuditionActionPrediction,
    simulate_native_audition_action,
)
from .audition_native_ordered_zones import (
    NativeOrderedZoneEvidenceBinding,
    NativeOrderedZoneState,
)
from .audition_rules import AuditionRules
from .audition_support_runtime import SupportUpgradeRuntimeInput
from .card_search import ProduceCardSearchRule
from .item_rules import EquippedItemRule
from .logic_engine import LogicExamState, MasterCard


NATIVE_AUDITION_TRANSITION_VERIFICATION_SCHEMA_VERSION = 2

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_MISSING = "<missing>"


def _canonical_json(value: Mapping[str, object]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    return value


def _sha256(value: object, name: str) -> str:
    text = _text(value, name)
    if _SHA256_RE.fullmatch(text) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return text


def _optional_index(value: object, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer or null")
    return value


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def _optional_text(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _text(value, name)


def _exact_fields(
    payload: Mapping[str, object], expected: set[str], name: str
) -> None:
    actual = set(payload)
    if actual != expected:
        raise ValueError(
            f"{name} fields are invalid: "
            f"missing={sorted(expected - actual)!r}; "
            f"unknown={sorted(actual - expected)!r}"
        )


def _json_value(value: object, name: str = "value") -> object:
    """Return a deterministic JSON-compatible value without lossy coercion."""

    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{name} must not contain non-finite numbers")
        return value
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, nested in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{name} object keys must be strings")
            result[key] = _json_value(nested, f"{name}.{key}")
        return result
    if isinstance(value, (list, tuple)):
        return [
            _json_value(nested, f"{name}[{index}]")
            for index, nested in enumerate(value)
        ]
    raise ValueError(f"{name} contains a non-JSON value: {type(value).__name__}")


def _string_tuple(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{name} must be a list")
    result = tuple(_text(item, f"{name} entry") for item in value)
    if len(result) != len(set(result)):
        raise ValueError(f"{name} must not contain duplicates")
    return result


def _observed_support_ids(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise TypeError("observed_used_support_ids must be an iterable of IDs")
    try:
        result = tuple(values)
    except TypeError as error:
        raise TypeError(
            "observed_used_support_ids must be an iterable of IDs"
        ) from error
    for value in result:
        _text(value, "observed used support ID")
    if len(result) != len(set(result)):
        raise ValueError("observed_used_support_ids must not contain duplicates")
    return result


def native_zone_semantic_payload(state: NativeOrderedZoneState) -> dict[str, object]:
    """Return all native zone semantics except replaceable evidence binding."""

    if not isinstance(state, NativeOrderedZoneState):
        raise TypeError("state must be NativeOrderedZoneState")
    payload = state.to_dict()
    payload.pop("binding")
    return payload


def native_zone_semantic_digest(state: NativeOrderedZoneState) -> str:
    """Hash exact zone order, GUID runtime, RNG, universe, and pending card."""

    return _digest(native_zone_semantic_payload(state))


def logic_state_payload(state: LogicExamState) -> dict[str, object]:
    """Return every current and future dataclass field of ``LogicExamState``."""

    if not isinstance(state, LogicExamState):
        raise TypeError("state must be LogicExamState")
    value = _json_value(asdict(state), "logic_state")
    if not isinstance(value, dict):
        raise AssertionError("LogicExamState did not serialize as an object")
    return value


def logic_state_digest(state: LogicExamState) -> str:
    return _digest(logic_state_payload(state))


@dataclass(frozen=True, slots=True)
class NativeAuditionTransitionDifference:
    """One exact mismatch; values are canonical JSON data, not inferred facts."""

    code: str
    path: str
    expected: object
    observed: object

    def __post_init__(self) -> None:
        _text(self.code, "difference code")
        _text(self.path, "difference path")
        object.__setattr__(
            self, "expected", _json_value(self.expected, "difference expected")
        )
        object.__setattr__(
            self, "observed", _json_value(self.observed, "difference observed")
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "path": self.path,
            "expected": self.expected,
            "observed": self.observed,
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> "NativeAuditionTransitionDifference":
        if not isinstance(payload, Mapping):
            raise ValueError("transition difference must be an object")
        _exact_fields(
            payload, {"code", "path", "expected", "observed"}, "transition difference"
        )
        return cls(
            code=_text(payload["code"], "difference code"),
            path=_text(payload["path"], "difference path"),
            expected=payload["expected"],
            observed=payload["observed"],
        )


def _compare_exact(
    expected: object,
    observed: object,
    *,
    path: str,
    code: str,
) -> tuple[NativeAuditionTransitionDifference, ...]:
    """Recursively compare JSON values while preserving list order and types."""

    expected = _json_value(expected, f"{path}.expected")
    observed = _json_value(observed, f"{path}.observed")
    differences: list[NativeAuditionTransitionDifference] = []

    def visit(left: object, right: object, current: str) -> None:
        if type(left) is not type(right):
            differences.append(
                NativeAuditionTransitionDifference(code, current, left, right)
            )
            return
        if isinstance(left, dict):
            assert isinstance(right, dict)
            for key in sorted(set(left) | set(right)):
                child = f"{current}.{key}"
                if key not in left:
                    differences.append(
                        NativeAuditionTransitionDifference(
                            code, child, _MISSING, right[key]
                        )
                    )
                elif key not in right:
                    differences.append(
                        NativeAuditionTransitionDifference(
                            code, child, left[key], _MISSING
                        )
                    )
                else:
                    visit(left[key], right[key], child)
            return
        if isinstance(left, list):
            assert isinstance(right, list)
            for index in range(max(len(left), len(right))):
                child = f"{current}[{index}]"
                if index >= len(left):
                    differences.append(
                        NativeAuditionTransitionDifference(
                            code, child, _MISSING, right[index]
                        )
                    )
                elif index >= len(right):
                    differences.append(
                        NativeAuditionTransitionDifference(
                            code, child, left[index], _MISSING
                        )
                    )
                else:
                    visit(left[index], right[index], child)
            return
        if left != right:
            differences.append(
                NativeAuditionTransitionDifference(code, current, left, right)
            )

    visit(expected, observed, path)
    return tuple(differences)


def _action_binding_payload(
    *,
    action_id: str,
    hand_index: int | None,
    selected_guid: str | None,
    prediction_digest: str,
    trusted_prediction_digest: str,
    pre_evidence_digest: str,
    pre_native_semantic_digest: str,
    run_id: str,
    step_context_id: str,
    step_context_digest: str,
    pre_session_transition_id: str,
    expected_post_session_transition_id: str,
) -> dict[str, object]:
    return {
        "action_id": action_id,
        "hand_index": hand_index,
        "selected_guid": selected_guid,
        "prediction_digest": prediction_digest,
        "trusted_prediction_digest": trusted_prediction_digest,
        "pre_evidence_digest": pre_evidence_digest,
        "pre_native_semantic_digest": pre_native_semantic_digest,
        "run_id": run_id,
        "step_context_id": step_context_id,
        "step_context_digest": step_context_digest,
        "pre_session_transition_id": pre_session_transition_id,
        "expected_post_session_transition_id": expected_post_session_transition_id,
    }


@dataclass(frozen=True, slots=True)
class NativeAuditionTransitionVerification:
    """Canonical audit record of one strict next-state comparison.

    This record and :meth:`from_dict` provide deterministic replay/inspection,
    not authenticity.  Live input authority must come from an independent
    transaction certificate that binds the complete pre state and expected post
    session transition ID.
    """

    schema_version: int
    action_id: str
    hand_index: int | None
    selected_guid: str | None
    prediction_digest: str
    trusted_prediction_digest: str
    action_binding_digest: str
    run_id: str
    step_context_id: str
    step_context_digest: str
    observed_run_id: str
    observed_step_context_id: str
    observed_step_context_digest: str
    pre_session_transition_id: str
    expected_post_session_transition_id: str
    observed_session_transition_id: str
    pre_evidence_digest: str
    observed_evidence_digest: str
    pre_binding_digest: str
    observed_binding_digest: str
    pre_native_semantic_digest: str
    predicted_post_logic_digest: str
    observed_post_logic_digest: str
    predicted_post_native_semantic_digest: str
    observed_post_native_semantic_digest: str
    before_used_support_ids: tuple[str, ...]
    predicted_after_used_support_ids: tuple[str, ...]
    observed_after_used_support_ids: tuple[str, ...]
    differences: tuple[NativeAuditionTransitionDifference, ...] = ()

    def __post_init__(self) -> None:
        if (
            _integer(self.schema_version, "schema_version")
            != NATIVE_AUDITION_TRANSITION_VERIFICATION_SCHEMA_VERSION
        ):
            raise ValueError("unsupported native transition verification schema")
        _text(self.action_id, "action_id")
        _optional_index(self.hand_index, "hand_index")
        _optional_text(self.selected_guid, "selected_guid")
        if self.action_id == "END_TURN":
            if self.hand_index is not None or self.selected_guid is not None:
                raise ValueError("END_TURN cannot bind a Hand index or selected GUID")
        elif self.hand_index is None or self.selected_guid is None:
            raise ValueError("card actions require a Hand index and selected GUID")
        for name in (
            "run_id",
            "step_context_id",
            "observed_run_id",
            "observed_step_context_id",
            "pre_session_transition_id",
            "expected_post_session_transition_id",
            "observed_session_transition_id",
        ):
            _text(getattr(self, name), name)
        for name in (
            "prediction_digest",
            "trusted_prediction_digest",
            "action_binding_digest",
            "step_context_digest",
            "observed_step_context_digest",
            "pre_evidence_digest",
            "observed_evidence_digest",
            "pre_binding_digest",
            "observed_binding_digest",
            "pre_native_semantic_digest",
            "predicted_post_logic_digest",
            "observed_post_logic_digest",
            "predicted_post_native_semantic_digest",
            "observed_post_native_semantic_digest",
        ):
            _sha256(getattr(self, name), name)
        for name in (
            "before_used_support_ids",
            "predicted_after_used_support_ids",
            "observed_after_used_support_ids",
        ):
            values = _string_tuple(getattr(self, name), name)
            object.__setattr__(self, name, values)
        differences = tuple(self.differences)
        if any(
            not isinstance(value, NativeAuditionTransitionDifference)
            for value in differences
        ):
            raise TypeError("differences must contain transition differences")
        object.__setattr__(self, "differences", differences)
        expected_binding_digest = _digest(
            _action_binding_payload(
                action_id=self.action_id,
                hand_index=self.hand_index,
                selected_guid=self.selected_guid,
                prediction_digest=self.prediction_digest,
                trusted_prediction_digest=self.trusted_prediction_digest,
                pre_evidence_digest=self.pre_evidence_digest,
                pre_native_semantic_digest=self.pre_native_semantic_digest,
                run_id=self.run_id,
                step_context_id=self.step_context_id,
                step_context_digest=self.step_context_digest,
                pre_session_transition_id=self.pre_session_transition_id,
                expected_post_session_transition_id=(
                    self.expected_post_session_transition_id
                ),
            )
        )
        if self.action_binding_digest != expected_binding_digest:
            raise ValueError("action_binding_digest does not replay from artifact fields")
        if not differences:
            exact_summary = (
                self.prediction_digest == self.trusted_prediction_digest
                and self.predicted_post_logic_digest
                == self.observed_post_logic_digest
                and self.predicted_post_native_semantic_digest
                == self.observed_post_native_semantic_digest
                and self.predicted_after_used_support_ids
                == self.observed_after_used_support_ids
                and self.run_id == self.observed_run_id
                and self.step_context_id == self.observed_step_context_id
                and self.step_context_digest == self.observed_step_context_digest
                and self.pre_session_transition_id
                != self.expected_post_session_transition_id
                and self.observed_session_transition_id
                == self.expected_post_session_transition_id
            )
            if not exact_summary:
                raise ValueError(
                    "zero-difference artifact disagrees with its exact summary"
                )

    @property
    def verified(self) -> bool:
        """Only byte-for-byte semantic agreement is verified."""

        return not self.differences

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "verified": self.verified,
            "action_id": self.action_id,
            "hand_index": self.hand_index,
            "selected_guid": self.selected_guid,
            "prediction_digest": self.prediction_digest,
            "trusted_prediction_digest": self.trusted_prediction_digest,
            "action_binding_digest": self.action_binding_digest,
            "run_id": self.run_id,
            "step_context_id": self.step_context_id,
            "step_context_digest": self.step_context_digest,
            "observed_run_id": self.observed_run_id,
            "observed_step_context_id": self.observed_step_context_id,
            "observed_step_context_digest": self.observed_step_context_digest,
            "pre_session_transition_id": self.pre_session_transition_id,
            "expected_post_session_transition_id": (
                self.expected_post_session_transition_id
            ),
            "observed_session_transition_id": self.observed_session_transition_id,
            "pre_evidence_digest": self.pre_evidence_digest,
            "observed_evidence_digest": self.observed_evidence_digest,
            "pre_binding_digest": self.pre_binding_digest,
            "observed_binding_digest": self.observed_binding_digest,
            "pre_native_semantic_digest": self.pre_native_semantic_digest,
            "predicted_post_logic_digest": self.predicted_post_logic_digest,
            "observed_post_logic_digest": self.observed_post_logic_digest,
            "predicted_post_native_semantic_digest": (
                self.predicted_post_native_semantic_digest
            ),
            "observed_post_native_semantic_digest": (
                self.observed_post_native_semantic_digest
            ),
            "before_used_support_ids": list(self.before_used_support_ids),
            "predicted_after_used_support_ids": list(
                self.predicted_after_used_support_ids
            ),
            "observed_after_used_support_ids": list(
                self.observed_after_used_support_ids
            ),
            "differences": [value.to_dict() for value in self.differences],
        }

    def canonical_json(self) -> str:
        return _canonical_json(self.to_dict())

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> "NativeAuditionTransitionVerification":
        if not isinstance(payload, Mapping):
            raise ValueError("native transition verification must be an object")
        fields = {
            "schema_version",
            "verified",
            "action_id",
            "hand_index",
            "selected_guid",
            "prediction_digest",
            "trusted_prediction_digest",
            "action_binding_digest",
            "run_id",
            "step_context_id",
            "step_context_digest",
            "observed_run_id",
            "observed_step_context_id",
            "observed_step_context_digest",
            "pre_session_transition_id",
            "expected_post_session_transition_id",
            "observed_session_transition_id",
            "pre_evidence_digest",
            "observed_evidence_digest",
            "pre_binding_digest",
            "observed_binding_digest",
            "pre_native_semantic_digest",
            "predicted_post_logic_digest",
            "observed_post_logic_digest",
            "predicted_post_native_semantic_digest",
            "observed_post_native_semantic_digest",
            "before_used_support_ids",
            "predicted_after_used_support_ids",
            "observed_after_used_support_ids",
            "differences",
        }
        _exact_fields(payload, fields, "native transition verification")
        raw_differences = payload["differences"]
        if not isinstance(raw_differences, list):
            raise ValueError("differences must be a list")
        result = cls(
            schema_version=_integer(payload["schema_version"], "schema_version"),
            action_id=_text(payload["action_id"], "action_id"),
            hand_index=_optional_index(payload["hand_index"], "hand_index"),
            selected_guid=_optional_text(payload["selected_guid"], "selected_guid"),
            prediction_digest=_sha256(
                payload["prediction_digest"], "prediction_digest"
            ),
            trusted_prediction_digest=_sha256(
                payload["trusted_prediction_digest"], "trusted_prediction_digest"
            ),
            action_binding_digest=_sha256(
                payload["action_binding_digest"], "action_binding_digest"
            ),
            run_id=_text(payload["run_id"], "run_id"),
            step_context_id=_text(payload["step_context_id"], "step_context_id"),
            step_context_digest=_sha256(
                payload["step_context_digest"], "step_context_digest"
            ),
            observed_run_id=_text(payload["observed_run_id"], "observed_run_id"),
            observed_step_context_id=_text(
                payload["observed_step_context_id"], "observed_step_context_id"
            ),
            observed_step_context_digest=_sha256(
                payload["observed_step_context_digest"],
                "observed_step_context_digest",
            ),
            pre_session_transition_id=_text(
                payload["pre_session_transition_id"], "pre_session_transition_id"
            ),
            expected_post_session_transition_id=_text(
                payload["expected_post_session_transition_id"],
                "expected_post_session_transition_id",
            ),
            observed_session_transition_id=_text(
                payload["observed_session_transition_id"],
                "observed_session_transition_id",
            ),
            pre_evidence_digest=_sha256(
                payload["pre_evidence_digest"], "pre_evidence_digest"
            ),
            observed_evidence_digest=_sha256(
                payload["observed_evidence_digest"], "observed_evidence_digest"
            ),
            pre_binding_digest=_sha256(
                payload["pre_binding_digest"], "pre_binding_digest"
            ),
            observed_binding_digest=_sha256(
                payload["observed_binding_digest"], "observed_binding_digest"
            ),
            pre_native_semantic_digest=_sha256(
                payload["pre_native_semantic_digest"],
                "pre_native_semantic_digest",
            ),
            predicted_post_logic_digest=_sha256(
                payload["predicted_post_logic_digest"],
                "predicted_post_logic_digest",
            ),
            observed_post_logic_digest=_sha256(
                payload["observed_post_logic_digest"],
                "observed_post_logic_digest",
            ),
            predicted_post_native_semantic_digest=_sha256(
                payload["predicted_post_native_semantic_digest"],
                "predicted_post_native_semantic_digest",
            ),
            observed_post_native_semantic_digest=_sha256(
                payload["observed_post_native_semantic_digest"],
                "observed_post_native_semantic_digest",
            ),
            before_used_support_ids=_string_tuple(
                payload["before_used_support_ids"], "before_used_support_ids"
            ),
            predicted_after_used_support_ids=_string_tuple(
                payload["predicted_after_used_support_ids"],
                "predicted_after_used_support_ids",
            ),
            observed_after_used_support_ids=_string_tuple(
                payload["observed_after_used_support_ids"],
                "observed_after_used_support_ids",
            ),
            differences=tuple(
                NativeAuditionTransitionDifference.from_dict(value)
                for value in raw_differences
            ),  # type: ignore[arg-type]
        )
        if not isinstance(payload["verified"], bool):
            raise ValueError("verified must be boolean")
        if payload["verified"] != result.verified:
            raise ValueError("verified disagrees with differences")
        return result


def _binding_difference(
    code: str, path: str, expected: object, observed: object
) -> NativeAuditionTransitionDifference:
    return NativeAuditionTransitionDifference(code, path, expected, observed)


def _validate_action_binding(
    prediction: NativeAuditionActionPrediction,
) -> tuple[NativeAuditionTransitionDifference, ...]:
    differences: list[NativeAuditionTransitionDifference] = []
    if prediction.action_id == "END_TURN":
        return ()
    index = prediction.hand_index
    if index is None or index >= len(prediction.before_zones.hand):
        differences.append(
            _binding_difference(
                "action-binding-hand-index",
                "action.hand_index",
                f"0..{len(prediction.before_zones.hand) - 1}",
                index,
            )
        )
        return tuple(differences)
    selected = prediction.before_zones.hand[index]
    if prediction.selected_guid != selected.guid:
        differences.append(
            _binding_difference(
                "action-binding-selected-guid",
                "action.selected_guid",
                selected.guid,
                prediction.selected_guid,
            )
        )
    expected_action_id = f"{selected.card_id}@{selected.effective_upgrade}"
    if prediction.action_id != expected_action_id:
        differences.append(
            _binding_difference(
                "action-binding-action-id",
                "action.action_id",
                expected_action_id,
                prediction.action_id,
            )
        )
    return tuple(differences)


def _validate_prediction_lineage(
    prediction: NativeAuditionActionPrediction,
) -> tuple[NativeAuditionTransitionDifference, ...]:
    before = prediction.before_zones.binding
    after = prediction.after_zones.binding
    if before == after:
        return ()
    return (
        _binding_difference(
            "prediction-binding-changed",
            "prediction.after_zones.binding",
            before.to_dict(),
            after.to_dict(),
        ),
    )


def _observed_lineage_differences(
    pre: NativeOrderedZoneEvidenceBinding,
    observed: NativeOrderedZoneEvidenceBinding,
    expected_post_session_transition_id: str,
) -> tuple[NativeAuditionTransitionDifference, ...]:
    differences: list[NativeAuditionTransitionDifference] = []
    for field in ("run_id", "step_context_id", "step_context_digest"):
        expected = getattr(pre, field)
        actual = getattr(observed, field)
        if expected != actual:
            differences.append(
                _binding_difference(
                    f"lineage-{field.replace('_', '-')}",
                    f"observed.binding.{field}",
                    expected,
                    actual,
                )
            )
    if expected_post_session_transition_id == pre.session_transition_id:
        differences.append(
            _binding_difference(
                "lineage-expected-session-transition-not-advanced",
                "expected_post_session_transition_id",
                f"different-from:{pre.session_transition_id}",
                expected_post_session_transition_id,
            )
        )
    if observed.session_transition_id != expected_post_session_transition_id:
        differences.append(
            _binding_difference(
                "lineage-session-transition-id",
                "observed.binding.session_transition_id",
                expected_post_session_transition_id,
                observed.session_transition_id,
            )
        )
    return tuple(differences)


def verify_native_audition_transition(
    prediction: NativeAuditionActionPrediction,
    observed_logic_state: LogicExamState,
    observed_zones: NativeOrderedZoneState,
    observed_used_support_ids: Iterable[str],
    *,
    expected_post_session_transition_id: str,
    rules: AuditionRules,
    schedule: VerifiedTurnSchedule,
    catalog: Mapping[HorizonCardRef, MasterCard],
    item: EquippedItemRule,
    limits: HorizonSearchLimits = HorizonSearchLimits(),
    support_upgrades: Mapping[str, SupportUpgradeRuntimeInput]
    | Iterable[SupportUpgradeRuntimeInput] = (),
    support_card_searches: Mapping[str, ProduceCardSearchRule] = {},
) -> NativeAuditionTransitionVerification:
    """Re-simulate and compare one prediction with an observed settled state.

    The trusted prediction and digest are generated here by a fresh public
    :func:`simulate_native_audition_action` call using the supplied prediction's
    before state and action binding.  A caller-supplied digest is never accepted
    as authority.  Incomplete re-simulation fails closed.

    Evidence hashes may change in the post observation, but run and step context
    must remain exact and its session transition ID must equal the independently
    supplied ``expected_post_session_transition_id``.  This function still does
    not authorize a live click: an outer transaction certificate must bind the
    complete prediction before state, static simulator inputs, action, and
    expected post session ID to current live evidence.  The returned serialized
    artifact and :meth:`NativeAuditionTransitionVerification.from_dict` are
    forgeable audit data, not authorization credentials.
    """

    if not isinstance(prediction, NativeAuditionActionPrediction):
        raise TypeError("prediction must be NativeAuditionActionPrediction")
    if not isinstance(observed_logic_state, LogicExamState):
        raise TypeError("observed_logic_state must be LogicExamState")
    if not isinstance(observed_zones, NativeOrderedZoneState):
        raise TypeError("observed_zones must be NativeOrderedZoneState")
    observed_used = _observed_support_ids(observed_used_support_ids)
    expected_post_session_transition_id = _text(
        expected_post_session_transition_id,
        "expected_post_session_transition_id",
    )

    trusted_simulation = simulate_native_audition_action(
        prediction.before_logic_state,
        prediction.before_zones,
        prediction.before_used_support_ids,
        rules,
        schedule,
        catalog,
        item,
        action_id=prediction.action_id,
        hand_index=prediction.hand_index,
        limits=limits,
        support_upgrades=support_upgrades,
        support_card_searches=support_card_searches,
    )
    trusted_prediction = (
        trusted_simulation.prediction
        if trusted_simulation.simulation_complete
        else None
    )
    trusted_digest = (
        trusted_prediction.digest()
        if trusted_prediction is not None
        else _digest({"trusted_resimulation": trusted_simulation.to_dict()})
    )
    expected_prediction = (
        trusted_prediction if trusted_prediction is not None else prediction
    )

    pre_binding = prediction.before_zones.binding
    observed_binding = observed_zones.binding
    pre_semantic_digest = native_zone_semantic_digest(prediction.before_zones)
    prediction_digest = prediction.digest()
    action_binding_digest = _digest(
        _action_binding_payload(
            action_id=prediction.action_id,
            hand_index=prediction.hand_index,
            selected_guid=prediction.selected_guid,
            prediction_digest=prediction_digest,
            trusted_prediction_digest=trusted_digest,
            pre_evidence_digest=pre_binding.local_save_evidence_digest,
            pre_native_semantic_digest=pre_semantic_digest,
            run_id=pre_binding.run_id,
            step_context_id=pre_binding.step_context_id,
            step_context_digest=pre_binding.step_context_digest,
            pre_session_transition_id=pre_binding.session_transition_id,
            expected_post_session_transition_id=(
                expected_post_session_transition_id
            ),
        )
    )

    differences: list[NativeAuditionTransitionDifference] = [
        *(
            ()
            if trusted_prediction is not None
            else (
                _binding_difference(
                    "trusted-resimulation-incomplete",
                    "trusted_resimulation",
                    {"simulation_complete": True},
                    trusted_simulation.to_dict(),
                ),
            )
        ),
        *(
            (
                _binding_difference(
                    "prediction-digest-mismatch",
                    "prediction.digest",
                    trusted_digest,
                    prediction_digest,
                ),
            )
            if prediction_digest != trusted_digest
            else ()
        ),
        *(
            _compare_exact(
                trusted_prediction.to_dict(),
                prediction.to_dict(),
                path="prediction",
                code="prediction-semantic-mismatch",
            )
            if trusted_prediction is not None
            else ()
        ),
        *_validate_action_binding(prediction),
        *_validate_prediction_lineage(prediction),
        *_observed_lineage_differences(
            pre_binding,
            observed_binding,
            expected_post_session_transition_id,
        ),
        *_compare_exact(
            logic_state_payload(expected_prediction.after_logic_state),
            logic_state_payload(observed_logic_state),
            path="logic_state",
            code="logic-state-mismatch",
        ),
        *_compare_exact(
            native_zone_semantic_payload(expected_prediction.after_zones),
            native_zone_semantic_payload(observed_zones),
            path="native_zones",
            code="native-zone-mismatch",
        ),
        *_compare_exact(
            list(expected_prediction.after_used_support_ids),
            list(observed_used),
            path="used_support_ids",
            code="used-support-ids-mismatch",
        ),
    ]

    return NativeAuditionTransitionVerification(
        schema_version=NATIVE_AUDITION_TRANSITION_VERIFICATION_SCHEMA_VERSION,
        action_id=prediction.action_id,
        hand_index=prediction.hand_index,
        selected_guid=prediction.selected_guid,
        prediction_digest=prediction_digest,
        trusted_prediction_digest=trusted_digest,
        action_binding_digest=action_binding_digest,
        run_id=pre_binding.run_id,
        step_context_id=pre_binding.step_context_id,
        step_context_digest=pre_binding.step_context_digest,
        observed_run_id=observed_binding.run_id,
        observed_step_context_id=observed_binding.step_context_id,
        observed_step_context_digest=observed_binding.step_context_digest,
        pre_session_transition_id=pre_binding.session_transition_id,
        expected_post_session_transition_id=expected_post_session_transition_id,
        observed_session_transition_id=observed_binding.session_transition_id,
        pre_evidence_digest=pre_binding.local_save_evidence_digest,
        observed_evidence_digest=observed_binding.local_save_evidence_digest,
        pre_binding_digest=pre_binding.digest(),
        observed_binding_digest=observed_binding.digest(),
        pre_native_semantic_digest=pre_semantic_digest,
        predicted_post_logic_digest=logic_state_digest(
            expected_prediction.after_logic_state
        ),
        observed_post_logic_digest=logic_state_digest(observed_logic_state),
        predicted_post_native_semantic_digest=native_zone_semantic_digest(
            expected_prediction.after_zones
        ),
        observed_post_native_semantic_digest=native_zone_semantic_digest(
            observed_zones
        ),
        before_used_support_ids=prediction.before_used_support_ids,
        predicted_after_used_support_ids=expected_prediction.after_used_support_ids,
        observed_after_used_support_ids=observed_used,
        differences=tuple(differences),
    )


__all__ = [
    "NATIVE_AUDITION_TRANSITION_VERIFICATION_SCHEMA_VERSION",
    "NativeAuditionTransitionDifference",
    "NativeAuditionTransitionVerification",
    "logic_state_digest",
    "logic_state_payload",
    "native_zone_semantic_digest",
    "native_zone_semantic_payload",
    "verify_native_audition_transition",
]
