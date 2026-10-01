"""Versioned ordered Plan 1 runtime loadouts for one fixed outer run.

Each entry remains an independent single-stage
:class:`Plan1RuntimeLoadoutBundle`.  The run artifact serializes no provider
object or mutable usage ledger.  Provisioning re-materializes the selected
entry and creates a fresh HandAdd provider every time.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Final

from .initial_regular_plan1_runtime_loadout import (
    InitialRegularPlan1RuntimeLoadoutProjection,
    Plan1RuntimeLoadoutBundle,
    Plan1RuntimeLoadoutBundleError,
    project_plan1_runtime_loadout_bundle,
)
from .plan1_native_core import FKTN_SSR_PLAN1_BEFORE_ITEM_ID
from .plan1_native_terminal_acceptance import Plan1TerminalFixture


PLAN1_RUN_RUNTIME_LOADOUT_SCHEMA: Final = (
    "gkms-tool.initial-regular-plan1-run-runtime-loadout"
)
PLAN1_RUN_RUNTIME_LOADOUT_SCHEMA_VERSION: Final = 1

_ROOT_FIELDS = {
    "schema",
    "schema_version",
    "run_ref",
    "produce_id",
    "character_id",
    "authentic_localsave",
    "entries",
}


class Plan1RunRuntimeLoadoutError(ValueError):
    """Typed run-level schema, identity, or provisioning failure."""

    def __init__(self, code: str, field: str, detail: str = "") -> None:
        self.code = str(code)
        self.field = str(field)
        self.detail = str(detail)
        super().__init__(
            f"{self.code}:{self.field}"
            + (f": {self.detail}" if self.detail else "")
        )


@dataclass(frozen=True, slots=True)
class Plan1RunRuntimeLoadoutIssue:
    """Pure preflight issue for missing, unknown, or unused stage entries."""

    code: str
    field: str
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not self.code:
            raise ValueError("code must be non-empty text")
        if not isinstance(self.field, str) or not self.field:
            raise ValueError("field must be non-empty text")
        if not isinstance(self.detail, str):
            raise TypeError("detail must be text")


def _error(code: str, field: str, detail: str = "") -> Plan1RunRuntimeLoadoutError:
    return Plan1RunRuntimeLoadoutError(code, field, detail)


def _issue(code: str, field: str, detail: str = "") -> Plan1RunRuntimeLoadoutIssue:
    return Plan1RunRuntimeLoadoutIssue(code, field, detail)


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _error("plan1-run-loadout-identity-missing", field)
    return value


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise _error("plan1-run-loadout-object-required", field)
    if any(not isinstance(key, str) for key in value):
        raise _error("plan1-run-loadout-key-invalid", field)
    return value  # type: ignore[return-value]


def _exact(payload: Mapping[str, object], expected: set[str], field: str) -> None:
    actual = set(payload)
    if actual != expected:
        raise _error(
            "plan1-run-loadout-fields-invalid",
            field,
            f"missing={sorted(expected - actual)};unknown={sorted(actual - expected)}",
        )


def _request_id(entry: Plan1RuntimeLoadoutBundle) -> str:
    return entry.bootstrap.outer_state_identity.external_request_id


def _week(entry: Plan1RuntimeLoadoutBundle) -> int:
    return entry.bootstrap.outer_state_identity.outer_state.week


def _canonical_ids(
    values: Iterable[str], *, field: str
) -> tuple[str, ...]:
    result = tuple(values)
    for index, value in enumerate(result):
        if not isinstance(value, str) or not value:
            raise _error(
                "plan1-run-loadout-identity-missing", f"{field}[{index}]"
            )
    if len(set(result)) != len(result):
        raise _error("plan1-run-loadout-identity-duplicate", field)
    return result


@dataclass(frozen=True, slots=True)
class InitialRegularPlan1RunRuntimeLoadoutBundle:
    """One exact ordered set of independent single-stage runtime bundles."""

    run_ref: str
    produce_id: str
    character_id: str
    entries: tuple[Plan1RuntimeLoadoutBundle, ...]
    authentic_localsave: bool = False
    schema_version: int = PLAN1_RUN_RUNTIME_LOADOUT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != PLAN1_RUN_RUNTIME_LOADOUT_SCHEMA_VERSION:
            raise _error(
                "plan1-run-loadout-schema-version-unsupported", "schema_version"
            )
        _text(self.run_ref, "run_ref")
        _text(self.produce_id, "produce_id")
        _text(self.character_id, "character_id")
        if type(self.authentic_localsave) is not bool or self.authentic_localsave:
            raise _error(
                "plan1-run-loadout-authentic-localsave-forbidden",
                "authentic_localsave",
            )
        entries = tuple(self.entries)
        if not entries:
            raise _error("plan1-run-loadout-entries-missing", "entries")
        if any(not isinstance(entry, Plan1RuntimeLoadoutBundle) for entry in entries):
            raise _error("plan1-run-loadout-entry-invalid", "entries")

        request_ids = tuple(_request_id(entry) for entry in entries)
        if len(set(request_ids)) != len(request_ids):
            raise _error(
                "plan1-run-loadout-request-id-duplicate",
                "entries.bootstrap.outer_state_identity.external_request_id",
            )
        runtime_refs = tuple(entry.runtime_ref for entry in entries)
        if len(set(runtime_refs)) != len(runtime_refs):
            raise _error(
                "plan1-run-loadout-runtime-ref-duplicate",
                "entries.runtime_ref",
            )
        weeks = tuple(_week(entry) for entry in entries)
        if any(left >= right for left, right in zip(weeks, weeks[1:])):
            raise _error(
                "plan1-run-loadout-week-order-invalid",
                "entries.bootstrap.outer_state_identity.outer_state.week",
                str(weeks),
            )
        for index, entry in enumerate(entries):
            if entry.produce_id != self.produce_id:
                raise _error(
                    "plan1-run-loadout-produce-mismatch",
                    f"entries[{index}].produce_id",
                    f"run={self.produce_id}:entry={entry.produce_id}",
                )
            if entry.character_id != self.character_id:
                raise _error(
                    "plan1-run-loadout-character-mismatch",
                    f"entries[{index}].character_id",
                    f"run={self.character_id}:entry={entry.character_id}",
                )
            if (
                entry.mandatory_before_produce_item_id
                != FKTN_SSR_PLAN1_BEFORE_ITEM_ID
            ):
                raise _error(
                    "plan1-run-loadout-before-produce-item-invalid",
                    f"entries[{index}].mandatory_before_produce_item_id",
                )
            if entry.authentic_localsave:
                raise _error(
                    "plan1-run-loadout-authentic-localsave-forbidden",
                    f"entries[{index}].authentic_localsave",
                )
        object.__setattr__(self, "entries", entries)

    @property
    def request_ids(self) -> tuple[str, ...]:
        return tuple(_request_id(entry) for entry in self.entries)

    @property
    def runtime_refs(self) -> tuple[str, ...]:
        return tuple(entry.runtime_ref for entry in self.entries)

    @property
    def weeks(self) -> tuple[int, ...]:
        return tuple(_week(entry) for entry in self.entries)

    def entry_for(self, request_id: str) -> Plan1RuntimeLoadoutBundle | None:
        if not isinstance(request_id, str):
            return None
        return next(
            (entry for entry in self.entries if _request_id(entry) == request_id),
            None,
        )

    def provision_stage(
        self,
        request_id: str,
        fixture: Plan1TerminalFixture,
    ) -> InitialRegularPlan1RuntimeLoadoutProjection:
        """Materialize a fresh bootstrap/loadout and fresh support provider."""

        request_id = _text(request_id, "request_id")
        entry = self.entry_for(request_id)
        if entry is None:
            raise _error(
                "plan1-run-loadout-stage-missing",
                "request_id",
                request_id,
            )
        if not isinstance(fixture, Plan1TerminalFixture):
            raise TypeError("fixture must be Plan1TerminalFixture")
        # Round-trip the immutable data boundary intentionally.  No previously
        # provisioned bootstrap/loadout/provider object enters a later stage.
        fresh_entry = Plan1RuntimeLoadoutBundle.from_dict(entry.to_dict())
        try:
            return project_plan1_runtime_loadout_bundle(
                fresh_entry,
                fixture=fixture,
            )
        except Plan1RuntimeLoadoutBundleError as error:
            raise _error(
                f"plan1-run-loadout-stage:{error.code}",
                error.field,
                error.detail,
            ) from error

    def preflight_issues(
        self,
        *,
        required_request_ids: Iterable[str] | None = None,
        used_request_ids: Iterable[str] | None = None,
    ) -> tuple[Plan1RunRuntimeLoadoutIssue, ...]:
        """Report missing required entries and unused/unknown consumed entries."""

        issues: list[Plan1RunRuntimeLoadoutIssue] = []
        available = set(self.request_ids)
        if required_request_ids is not None:
            required = _canonical_ids(
                required_request_ids, field="required_request_ids"
            )
            for request_id in required:
                if request_id not in available:
                    issues.append(
                        _issue(
                            "plan1-run-loadout-required-stage-missing",
                            "required_request_ids",
                            request_id,
                        )
                    )
        if used_request_ids is not None:
            used = _canonical_ids(used_request_ids, field="used_request_ids")
            used_set = set(used)
            for request_id in used:
                if request_id not in available:
                    issues.append(
                        _issue(
                            "plan1-run-loadout-used-stage-unknown",
                            "used_request_ids",
                            request_id,
                        )
                    )
            for request_id in self.request_ids:
                if request_id not in used_set:
                    issues.append(
                        _issue(
                            "plan1-run-loadout-stage-unused",
                            "entries",
                            request_id,
                        )
                    )
        return tuple(issues)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": PLAN1_RUN_RUNTIME_LOADOUT_SCHEMA,
            "schema_version": self.schema_version,
            "run_ref": self.run_ref,
            "produce_id": self.produce_id,
            "character_id": self.character_id,
            "authentic_localsave": self.authentic_localsave,
            "entries": [entry.to_dict() for entry in self.entries],
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            indent=indent,
        )

    @classmethod
    def from_dict(
        cls, value: Mapping[str, object]
    ) -> "InitialRegularPlan1RunRuntimeLoadoutBundle":
        payload = _mapping(value, "bundle")
        _exact(payload, _ROOT_FIELDS, "bundle")
        if payload["schema"] != PLAN1_RUN_RUNTIME_LOADOUT_SCHEMA:
            raise _error("plan1-run-loadout-schema-unsupported", "schema")
        version = payload["schema_version"]
        if isinstance(version, bool) or not isinstance(version, int):
            raise _error(
                "plan1-run-loadout-schema-version-invalid", "schema_version"
            )
        raw_entries = payload["entries"]
        if isinstance(raw_entries, (str, bytes)) or not isinstance(
            raw_entries, list
        ):
            raise _error("plan1-run-loadout-array-required", "entries")
        entries: list[Plan1RuntimeLoadoutBundle] = []
        for index, raw_entry in enumerate(raw_entries):
            try:
                entries.append(
                    Plan1RuntimeLoadoutBundle.from_dict(
                        _mapping(raw_entry, f"entries[{index}]")
                    )
                )
            except Plan1RuntimeLoadoutBundleError as error:
                raise _error(
                    f"plan1-run-loadout-entry:{error.code}",
                    f"entries[{index}].{error.field}",
                    error.detail,
                ) from error
        authentic = payload["authentic_localsave"]
        if not isinstance(authentic, bool):
            raise _error(
                "plan1-run-loadout-boolean-invalid", "authentic_localsave"
            )
        return cls(
            run_ref=_text(payload["run_ref"], "run_ref"),
            produce_id=_text(payload["produce_id"], "produce_id"),
            character_id=_text(payload["character_id"], "character_id"),
            entries=tuple(entries),
            authentic_localsave=authentic,
            schema_version=version,
        )

    @classmethod
    def from_json(
        cls, value: str
    ) -> "InitialRegularPlan1RunRuntimeLoadoutBundle":
        if not isinstance(value, str):
            raise TypeError("value must be JSON text")
        try:
            payload = json.loads(value)
        except json.JSONDecodeError as error:
            raise _error(
                "plan1-run-loadout-json-invalid", "bundle", str(error)
            ) from error
        return cls.from_dict(_mapping(payload, "bundle"))


def save_initial_regular_plan1_run_runtime_loadout(
    bundle: InitialRegularPlan1RunRuntimeLoadoutBundle,
    path: str | Path,
) -> Path:
    """Save one strict run bundle without any provisioned provider state."""

    if not isinstance(bundle, InitialRegularPlan1RunRuntimeLoadoutBundle):
        raise TypeError(
            "bundle must be InitialRegularPlan1RunRuntimeLoadoutBundle"
        )
    target = Path(path)
    target.write_text(bundle.to_json(indent=2) + "\n", encoding="utf-8")
    return target


def load_initial_regular_plan1_run_runtime_loadout(
    path: str | Path,
) -> InitialRegularPlan1RunRuntimeLoadoutBundle:
    """Load one strict run bundle from disk."""

    return InitialRegularPlan1RunRuntimeLoadoutBundle.from_json(
        Path(path).read_text(encoding="utf-8")
    )


Plan1RunRuntimeLoadoutBundle = InitialRegularPlan1RunRuntimeLoadoutBundle
load_plan1_run_runtime_loadout_bundle = (
    load_initial_regular_plan1_run_runtime_loadout
)
save_plan1_run_runtime_loadout_bundle = (
    save_initial_regular_plan1_run_runtime_loadout
)


__all__ = [
    "PLAN1_RUN_RUNTIME_LOADOUT_SCHEMA",
    "PLAN1_RUN_RUNTIME_LOADOUT_SCHEMA_VERSION",
    "InitialRegularPlan1RunRuntimeLoadoutBundle",
    "Plan1RunRuntimeLoadoutBundle",
    "Plan1RunRuntimeLoadoutError",
    "Plan1RunRuntimeLoadoutIssue",
    "load_initial_regular_plan1_run_runtime_loadout",
    "load_plan1_run_runtime_loadout_bundle",
    "save_initial_regular_plan1_run_runtime_loadout",
    "save_plan1_run_runtime_loadout_bundle",
]
