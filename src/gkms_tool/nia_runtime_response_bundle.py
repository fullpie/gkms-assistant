"""Strict, typed JSON envelope for captured N.I.A. runtime responses.

The static route JSON describes *which* action is selected for a week.  This
module describes the caller-resolved response evidence for that action.  The
envelope intentionally contains only version/authority metadata and
``{week, scenario}`` entries; every field below ``scenario`` is owned by its
corresponding typed scenario module.  Parsing never returns a raw mapping to
the outer kernel.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, TypeAlias

from .initial_regular_nia_event_business_scenario import (
    InitialRegularNiaEventBusinessScenario,
)
from .initial_regular_nia_business_scenario import (
    InitialRegularNiaBusinessScenario,
)
from .initial_regular_nia_customize_scenario import (
    InitialRegularNiaCustomizeScenario,
)
from .initial_regular_nia_interval_scenario import InitialRegularNiaIntervalScenario
from .initial_regular_nia_refresh_scenario import InitialRegularNiaRefreshScenario
from .initial_regular_nia_self_lesson_scenario import (
    InitialRegularNiaSelfLessonScenario,
)
from .initial_regular_supply_scenario_adapter import InitialRegularSupplyScenario


NIA_RUNTIME_RESPONSE_BUNDLE_SCHEMA = "gkms_tool.nia_runtime_response_bundle"
NIA_RUNTIME_RESPONSE_BUNDLE_VERSION = 1
NIA_RUNTIME_RESPONSE_BUNDLE_ACTIONS = (
    "self_lesson",
    "business",
    "customize",
    "event_business",
    "interval",
    "refresh",
    "fan_present",
)
_SUPPORTED_PRODUCE_IDS = frozenset({"produce-004", "produce-005"})
_AUTHORITY_KINDS = frozenset(
    {
        "caller-resolved-server-response",
        "caller-authored-synthetic-exact-response",
        "caller-authored-synthetic-master-only",
    }
)

NiaRuntimeResponseScenario: TypeAlias = (
    InitialRegularNiaSelfLessonScenario
    | InitialRegularNiaBusinessScenario
    | InitialRegularNiaCustomizeScenario
    | InitialRegularNiaEventBusinessScenario
    | InitialRegularNiaIntervalScenario
    | InitialRegularNiaRefreshScenario
    | InitialRegularSupplyScenario
)


class NiaRuntimeResponseBundleError(ValueError):
    """Stable fail-closed error for malformed or mismatched bundle input."""

    def __init__(self, code: str, field: str, detail: str = "") -> None:
        self.code = code
        self.field = field
        self.detail = detail
        suffix = f":{detail}" if detail else ""
        super().__init__(f"{code}:{field}{suffix}")


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise NiaRuntimeResponseBundleError("nia-runtime-response-type", field, "expected object")
    return value


def _exact(value: Mapping[str, object], fields: set[str], field: str) -> None:
    actual = set(value)
    if actual == fields:
        return
    missing = sorted(fields - actual)
    extra = sorted(actual - fields)
    detail: list[str] = []
    if missing:
        detail.append(f"missing={','.join(missing)}")
    if extra:
        detail.append(f"extra={','.join(extra)}")
    raise NiaRuntimeResponseBundleError(
        "nia-runtime-response-unknown-key" if extra else "nia-runtime-response-missing-key",
        field,
        ";".join(detail),
    )


def _text(value: object, field: str, *, empty: bool = False) -> str:
    if not isinstance(value, str):
        raise NiaRuntimeResponseBundleError("nia-runtime-response-type", field, "expected text")
    if not empty and not value:
        raise NiaRuntimeResponseBundleError("nia-runtime-response-invalid-text", field)
    return value


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise NiaRuntimeResponseBundleError(
            "nia-runtime-response-invalid-week",
            field,
            "expected positive integer",
        )
    return value


def _bool(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise NiaRuntimeResponseBundleError("nia-runtime-response-type", field, "expected boolean")
    return value


@dataclass(frozen=True, slots=True)
class NiaRuntimeResponseBundleAuthority:
    """Envelope-level provenance; each scenario retains its own authority."""

    kind: str
    authentic_nia_local_save: bool
    runtime_ref: str

    def __post_init__(self) -> None:
        if self.kind not in _AUTHORITY_KINDS:
            raise ValueError("runtime response bundle authority kind is invalid")
        if self.authentic_nia_local_save:
            raise ValueError("runtime response bundle cannot claim authentic LocalSave")
        if not isinstance(self.runtime_ref, str) or not self.runtime_ref:
            raise ValueError("runtime response bundle runtime_ref is required")

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "authentic_nia_local_save": self.authentic_nia_local_save,
            "runtime_ref": self.runtime_ref,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "NiaRuntimeResponseBundleAuthority":
        value = _mapping(payload, "authority")
        _exact(value, {"kind", "authentic_nia_local_save", "runtime_ref"}, "authority")
        kind = _text(value["kind"], "authority.kind")
        if kind not in _AUTHORITY_KINDS:
            raise NiaRuntimeResponseBundleError(
                "nia-runtime-response-authority-invalid",
                "authority.kind",
                kind,
            )
        authentic = _bool(value["authentic_nia_local_save"], "authority.authentic_nia_local_save")
        if authentic:
            raise NiaRuntimeResponseBundleError(
                "nia-runtime-response-authentic-local-save",
                "authority.authentic_nia_local_save",
                "must be false",
            )
        return cls(
            kind=kind,
            authentic_nia_local_save=False,
            runtime_ref=_text(value["runtime_ref"], "authority.runtime_ref"),
        )


def _typed_map(
    value: Mapping[int, NiaRuntimeResponseScenario],
    expected_type: type[object],
    field_name: str,
    *,
    scenario_has_week: bool = True,
) -> Mapping[int, NiaRuntimeResponseScenario]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a mapping")
    result: dict[int, NiaRuntimeResponseScenario] = {}
    for week, scenario in value.items():
        if isinstance(week, bool) or not isinstance(week, int) or week < 1:
            raise ValueError(f"{field_name} keys must be positive weeks")
        if not isinstance(scenario, expected_type):
            raise TypeError(f"{field_name} values must be typed scenarios")
        if scenario_has_week and getattr(scenario, "week") != week:
            raise ValueError(f"{field_name} key and scenario week disagree")
        result[week] = scenario
    return MappingProxyType(result)


def _entries(
    scenarios: Mapping[int, NiaRuntimeResponseScenario],
) -> list[dict[str, object]]:
    return [
        {"week": week, "scenario": scenarios[week].to_dict()}
        for week in sorted(scenarios)
    ]


@dataclass(frozen=True, slots=True)
class NiaRuntimeResponseBundle:
    """Version-1 envelope projected to typed week maps."""

    produce_id: str
    authority: NiaRuntimeResponseBundleAuthority
    self_lesson_scenarios: Mapping[int, InitialRegularNiaSelfLessonScenario] = field(
        default_factory=dict
    )
    business_scenarios: Mapping[int, InitialRegularNiaBusinessScenario] = field(
        default_factory=dict
    )
    customize_scenarios: Mapping[int, InitialRegularNiaCustomizeScenario] = field(
        default_factory=dict
    )
    event_business_scenarios: Mapping[
        int, InitialRegularNiaEventBusinessScenario
    ] = field(default_factory=dict)
    interval_scenarios: Mapping[int, InitialRegularNiaIntervalScenario] = field(
        default_factory=dict
    )
    refresh_scenarios: Mapping[int, InitialRegularNiaRefreshScenario] = field(
        default_factory=dict
    )
    fan_present_scenarios: Mapping[int, InitialRegularSupplyScenario] = field(
        default_factory=dict
    )

    def __post_init__(self) -> None:
        if self.produce_id not in _SUPPORTED_PRODUCE_IDS:
            raise ValueError("runtime response bundle produce_id is invalid")
        if not isinstance(self.authority, NiaRuntimeResponseBundleAuthority):
            raise TypeError("authority must be typed")
        object.__setattr__(
            self,
            "self_lesson_scenarios",
            _typed_map(self.self_lesson_scenarios, InitialRegularNiaSelfLessonScenario, "self_lesson_scenarios"),
        )
        object.__setattr__(
            self,
            "business_scenarios",
            _typed_map(
                self.business_scenarios,
                InitialRegularNiaBusinessScenario,
                "business_scenarios",
            ),
        )
        object.__setattr__(
            self,
            "customize_scenarios",
            _typed_map(
                self.customize_scenarios,
                InitialRegularNiaCustomizeScenario,
                "customize_scenarios",
            ),
        )
        object.__setattr__(
            self,
            "event_business_scenarios",
            _typed_map(self.event_business_scenarios, InitialRegularNiaEventBusinessScenario, "event_business_scenarios"),
        )
        object.__setattr__(
            self,
            "interval_scenarios",
            _typed_map(self.interval_scenarios, InitialRegularNiaIntervalScenario, "interval_scenarios"),
        )
        object.__setattr__(
            self,
            "refresh_scenarios",
            _typed_map(self.refresh_scenarios, InitialRegularNiaRefreshScenario, "refresh_scenarios"),
        )
        object.__setattr__(
            self,
            "fan_present_scenarios",
            _typed_map(
                self.fan_present_scenarios,
                InitialRegularSupplyScenario,
                "fan_present_scenarios",
                scenario_has_week=False,
            ),
        )
        for week, scenario in self.business_scenarios.items():
            if scenario.produce_id != self.produce_id:
                raise ValueError(
                    "Business scenario produce_id disagrees with bundle:"
                    f"week={week}:bundle={self.produce_id}:"
                    f"scenario={scenario.produce_id}"
                )

    @property
    def scenario_maps(self) -> dict[str, Mapping[int, NiaRuntimeResponseScenario]]:
        """Return action-name → typed week map; no raw JSON mappings."""

        return {
            "self_lesson": self.self_lesson_scenarios,
            "business": self.business_scenarios,
            "customize": self.customize_scenarios,
            "event_business": self.event_business_scenarios,
            "interval": self.interval_scenarios,
            "refresh": self.refresh_scenarios,
            "fan_present": self.fan_present_scenarios,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": NIA_RUNTIME_RESPONSE_BUNDLE_SCHEMA,
            "schema_version": NIA_RUNTIME_RESPONSE_BUNDLE_VERSION,
            "produce_id": self.produce_id,
            "authority": self.authority.to_dict(),
            "responses": {
                "self_lesson": _entries(self.self_lesson_scenarios),
                "business": _entries(self.business_scenarios),
                "customize": _entries(self.customize_scenarios),
                "event_business": _entries(self.event_business_scenarios),
                "interval": _entries(self.interval_scenarios),
                "refresh": _entries(self.refresh_scenarios),
                "fan_present": _entries(self.fan_present_scenarios),
            },
        }

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, object],
        *,
        expected_produce_id: str | None = None,
    ) -> "NiaRuntimeResponseBundle":
        value = _mapping(payload, "runtime response bundle")
        _exact(
            value,
            {"schema", "schema_version", "produce_id", "authority", "responses"},
            "runtime response bundle",
        )
        if value["schema"] != NIA_RUNTIME_RESPONSE_BUNDLE_SCHEMA:
            raise NiaRuntimeResponseBundleError(
                "nia-runtime-response-schema-mismatch",
                "schema",
                repr(value["schema"]),
            )
        version = value["schema_version"]
        if (
            isinstance(version, bool)
            or not isinstance(version, int)
            or version != NIA_RUNTIME_RESPONSE_BUNDLE_VERSION
        ):
            raise NiaRuntimeResponseBundleError(
                "nia-runtime-response-schema-version",
                "schema_version",
                repr(version),
            )
        produce_id = _text(value["produce_id"], "produce_id")
        if produce_id not in _SUPPORTED_PRODUCE_IDS:
            raise NiaRuntimeResponseBundleError(
                "nia-runtime-response-produce-invalid",
                "produce_id",
                produce_id,
            )
        if expected_produce_id is not None and produce_id != expected_produce_id:
            raise NiaRuntimeResponseBundleError(
                "nia-runtime-response-produce-mismatch",
                "produce_id",
                f"expected={expected_produce_id}:actual={produce_id}",
            )
        try:
            authority = NiaRuntimeResponseBundleAuthority.from_dict(
                _mapping(value["authority"], "authority")
            )
        except NiaRuntimeResponseBundleError:
            raise
        except (TypeError, ValueError) as error:
            raise NiaRuntimeResponseBundleError(
                "nia-runtime-response-authority-invalid",
                "authority",
                str(error),
            ) from error
        responses = _mapping(value["responses"], "responses")
        _exact(responses, set(NIA_RUNTIME_RESPONSE_BUNDLE_ACTIONS), "responses")

        maps: dict[str, dict[int, NiaRuntimeResponseScenario]] = {}
        classes: dict[str, type[NiaRuntimeResponseScenario]] = {
            "self_lesson": InitialRegularNiaSelfLessonScenario,
            "business": InitialRegularNiaBusinessScenario,
            "customize": InitialRegularNiaCustomizeScenario,
            "event_business": InitialRegularNiaEventBusinessScenario,
            "interval": InitialRegularNiaIntervalScenario,
            "refresh": InitialRegularNiaRefreshScenario,
            "fan_present": InitialRegularSupplyScenario,
        }
        for action in NIA_RUNTIME_RESPONSE_BUNDLE_ACTIONS:
            raw_entries = responses[action]
            if not isinstance(raw_entries, list):
                raise NiaRuntimeResponseBundleError(
                    "nia-runtime-response-entries-type",
                    f"responses.{action}",
                    "expected list",
                )
            by_week: dict[int, NiaRuntimeResponseScenario] = {}
            scenario_type = classes[action]
            for index, raw_entry in enumerate(raw_entries):
                entry = _mapping(raw_entry, f"responses.{action}[{index}]")
                _exact(entry, {"week", "scenario"}, f"responses.{action}[{index}]")
                week = _positive_int(entry["week"], f"responses.{action}[{index}].week")
                if week in by_week:
                    raise NiaRuntimeResponseBundleError(
                        "nia-runtime-response-week-duplicate",
                        f"responses.{action}[{index}].week",
                        str(week),
                    )
                raw_scenario = _mapping(
                    entry["scenario"], f"responses.{action}[{index}].scenario"
                )
                try:
                    scenario = scenario_type.from_dict(raw_scenario)  # type: ignore[attr-defined]
                except NiaRuntimeResponseBundleError:
                    raise
                except (TypeError, ValueError, KeyError) as error:
                    raise NiaRuntimeResponseBundleError(
                        "nia-runtime-response-scenario-invalid",
                        f"responses.{action}[{index}].scenario",
                        str(error),
                    ) from error
                if action != "fan_present" and getattr(scenario, "week") != week:
                    raise NiaRuntimeResponseBundleError(
                        "nia-runtime-response-week-mismatch",
                        f"responses.{action}[{index}].week",
                        f"entry={week}:scenario={scenario.week}",
                    )
                if (
                    action == "business"
                    and scenario.produce_id != produce_id
                ):
                    raise NiaRuntimeResponseBundleError(
                        "nia-runtime-response-produce-mismatch",
                        f"responses.{action}[{index}].scenario.produce_id",
                        (
                            f"bundle={produce_id}:"
                            f"scenario={scenario.produce_id}"
                        ),
                    )
                by_week[week] = scenario
            maps[action] = by_week
        return cls(
            produce_id=produce_id,
            authority=authority,
            self_lesson_scenarios=maps["self_lesson"],
            business_scenarios=maps["business"],
            customize_scenarios=maps["customize"],
            event_business_scenarios=maps["event_business"],
            interval_scenarios=maps["interval"],
            refresh_scenarios=maps["refresh"],
            fan_present_scenarios=maps["fan_present"],
        )


def load_nia_runtime_response_bundle(
    path: str | Path,
    *,
    expected_produce_id: str | None = None,
) -> NiaRuntimeResponseBundle:
    """Read one strict JSON bundle and return only typed scenario maps."""

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise NiaRuntimeResponseBundleError(
            "nia-runtime-response-json-invalid",
            "path",
            str(error),
        ) from error
    return NiaRuntimeResponseBundle.from_dict(
        _mapping(payload, "runtime response bundle"),
        expected_produce_id=expected_produce_id,
    )


__all__ = [
    "NIA_RUNTIME_RESPONSE_BUNDLE_ACTIONS",
    "NIA_RUNTIME_RESPONSE_BUNDLE_SCHEMA",
    "NIA_RUNTIME_RESPONSE_BUNDLE_VERSION",
    "NiaRuntimeResponseBundle",
    "NiaRuntimeResponseBundleAuthority",
    "NiaRuntimeResponseBundleError",
    "NiaRuntimeResponseScenario",
    "load_nia_runtime_response_bundle",
]
