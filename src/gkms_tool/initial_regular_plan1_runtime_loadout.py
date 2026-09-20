"""Versioned caller-owned runtime bundle for one Initial Regular Plan 1 stage.

This first slice deliberately contains only the authoritative stage bootstrap
and ordered HandAdd support runtime.  Memory/passive/drink runtime is outside
the schema.  The mandatory FKTN SSR beforeProduce item is represented only by
its fixed Master-derived ID and is checked against an already-built fixture;
JSON can neither provide nor replace its effects or remaining-use state.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
import json
from pathlib import Path
from typing import Final

from .audition_support_runtime import (
    SupportUpgradeRuntimeInput,
    support_lesson_type_matches,
)
from .card_search import ProduceCardSearchRule
from .initial_regular_inner_protocol import InitialRegularInnerStageKind
from .initial_regular_plan1_stage_bootstrap import (
    Plan1StageBootstrap,
    Plan1StageBootstrapAuthority,
    Plan1StageOuterIdentityScope,
    Plan1StageOuterStateIdentity,
)
from .plan1_hand_add_support_provider import (
    Plan1HandAddSupportLoadout,
    Plan1HandAddSupportProvider,
    Plan1HandAddSupportProviderError,
    build_plan1_hand_add_support_provider,
)
from .plan1_native_core import (
    FKTN_SSR_PLAN1_BEFORE_ITEM_ID,
    Plan1ScalarState,
    validate_fktn_ssr_plan1_equipped_item_runtime,
)
from .plan1_native_terminal_acceptance import Plan1TerminalFixture
from .produce_rollout import AttributeValues, ProduceRolloutState


PLAN1_RUNTIME_LOADOUT_SCHEMA: Final = (
    "gkms-tool.initial-regular-plan1-runtime-loadout"
)
PLAN1_RUNTIME_LOADOUT_SCHEMA_VERSION: Final = 1

_ROOT_FIELDS = {
    "schema",
    "schema_version",
    "authority",
    "runtime_ref",
    "produce_id",
    "character_id",
    "authentic_localsave",
    "mandatory_before_produce_item_id",
    "bootstrap",
    "hand_add_support_loadout",
}
_FORBIDDEN_RUNTIME_FIELDS = {
    "memory",
    "memories",
    "memory_passives",
    "passive",
    "passives",
    "passive_session_refs",
    "drink",
    "drinks",
    "drink_session_refs",
}
_BOOTSTRAP_FIELDS = {
    "authority",
    "authority_detail",
    "outer_state_identity",
    "initial_state",
    "attributes",
    "battle_bonus_permille",
    "lesson_buff_multiple_permille",
    "rng_before",
}
_OUTER_IDENTITY_FIELDS = {
    "scope",
    "external_request_id",
    "action_id",
    "stage_kind",
    "stage_type",
    "outer_state",
}
_OUTER_STATE_FIELDS = {
    "schema",
    "schema_version",
    "mode",
    "mode_id",
    "character_id",
    "week",
    "total_weeks",
    "phase",
    "stamina",
    "max_stamina",
    "produce_points",
    "attributes",
    "deck",
    "item_session_refs",
    "excluded_reward_card_ids",
    "chance_history",
    "lifecycle",
    "pending",
    "reward_context",
    "reward_offers",
    "remaining_reward_excludes",
}
_HAND_ADD_FIELDS = {
    "supports",
    "support_card_searches",
    "lesson_type",
    "random_state",
    "rng_authority",
    "used_support_ids",
}
_SUPPORT_FIELDS = {
    "support_id",
    "lesson_type",
    "runtime_permil",
    "loadout_order",
    "card_search_id",
}
_SEARCH_TUPLE_TEXT_FIELDS = {
    "card_rarities",
    "produce_card_ids",
    "card_categories",
    "effect_group_ids",
}
_SEARCH_TUPLE_INT_FIELDS = {"upgrade_counts"}
_SEARCH_BOOL_FIELDS = {"is_self", "is_customized"}
_SEARCH_INT_FIELDS = {"limit_count", "stamina_min", "stamina_max"}
_SEARCH_FIELDS = {value.name for value in fields(ProduceCardSearchRule)}
_SCALAR_FIELDS = {value.name for value in fields(Plan1ScalarState)}


class Plan1RuntimeLoadoutBundleError(ValueError):
    """Typed strict-schema, identity, or fixture-binding failure."""

    def __init__(self, code: str, field: str, detail: str = "") -> None:
        self.code = str(code)
        self.field = str(field)
        self.detail = str(detail)
        super().__init__(
            f"{self.code}:{self.field}"
            + (f": {self.detail}" if self.detail else "")
        )


def _error(code: str, field: str, detail: str = "") -> Plan1RuntimeLoadoutBundleError:
    return Plan1RuntimeLoadoutBundleError(code, field, detail)


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise _error("plan1-loadout-object-required", field)
    if any(not isinstance(key, str) for key in value):
        raise _error("plan1-loadout-key-invalid", field)
    return value  # type: ignore[return-value]


def _sequence(value: object, field: str) -> Sequence[object]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise _error("plan1-loadout-array-required", field)
    return value


def _exact(
    payload: Mapping[str, object], expected: set[str], field: str
) -> None:
    actual = set(payload)
    forbidden = actual & _FORBIDDEN_RUNTIME_FIELDS
    if forbidden:
        raise _error(
            "plan1-loadout-runtime-field-unsupported",
            field,
            f"unsupported={sorted(forbidden)}",
        )
    if actual != expected:
        raise _error(
            "plan1-loadout-fields-invalid",
            field,
            f"missing={sorted(expected - actual)};unknown={sorted(actual - expected)}",
        )


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _error("plan1-loadout-identity-missing", field)
    return value


def _integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _error("plan1-loadout-integer-invalid", field)
    return value


def _boolean(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise _error("plan1-loadout-boolean-invalid", field)
    return value


def _text_tuple(value: object, field: str) -> tuple[str, ...]:
    sequence = _sequence(value, field)
    result: list[str] = []
    for index, item in enumerate(sequence):
        result.append(_text(item, f"{field}[{index}]"))
    return tuple(result)


def _integer_tuple(value: object, field: str) -> tuple[int, ...]:
    sequence = _sequence(value, field)
    return tuple(
        _integer(item, f"{field}[{index}]")
        for index, item in enumerate(sequence)
    )


def _validate_outer_payload(payload: Mapping[str, object]) -> None:
    _exact(payload, _OUTER_STATE_FIELDS, "bootstrap.outer_state_identity.outer_state")
    attributes = _mapping(
        payload["attributes"], "bootstrap.outer_state_identity.outer_state.attributes"
    )
    _exact(
        attributes,
        {"vocal", "dance", "visual"},
        "bootstrap.outer_state_identity.outer_state.attributes",
    )
    for index, raw in enumerate(
        _sequence(payload["deck"], "bootstrap.outer_state_identity.outer_state.deck")
    ):
        row = _mapping(
            raw, f"bootstrap.outer_state_identity.outer_state.deck[{index}]"
        )
        _exact(
            row,
            {"card_id", "upgrade", "count", "instance_ids"},
            f"bootstrap.outer_state_identity.outer_state.deck[{index}]",
        )
    pending = payload["pending"]
    if pending is not None:
        pending_payload = _mapping(
            pending, "bootstrap.outer_state_identity.outer_state.pending"
        )
        _exact(
            pending_payload,
            {
                "request_id",
                "kind",
                "week",
                "action_id",
                "stage_type",
                "adapter_ref",
                "required_fields",
            },
            "bootstrap.outer_state_identity.outer_state.pending",
        )
    for index, raw in enumerate(
        _sequence(
            payload["chance_history"],
            "bootstrap.outer_state_identity.outer_state.chance_history",
        )
    ):
        field = f"bootstrap.outer_state_identity.outer_state.chance_history[{index}]"
        history = _mapping(raw, field)
        _exact(history, {"request_id", "kind", "week", "branch"}, field)
        branch = _mapping(history["branch"], f"{field}.branch")
        _exact(
            branch,
            {"branch_id", "probability", "rng_token", "event_id", "was_sp"},
            f"{field}.branch",
        )
    reward_context = payload["reward_context"]
    if reward_context is not None:
        context = _mapping(
            reward_context,
            "bootstrap.outer_state_identity.outer_state.reward_context",
        )
        _exact(
            context,
            {"source_action_id", "terminal_after_reward", "cleared", "continuation"},
            "bootstrap.outer_state_identity.outer_state.reward_context",
        )
        continuation = context["continuation"]
        if continuation is not None:
            continuation_payload = _mapping(
                continuation,
                "bootstrap.outer_state_identity.outer_state.reward_context.continuation",
            )
            _exact(
                continuation_payload,
                {
                    "kind",
                    "action_id",
                    "stage_type",
                    "adapter_ref",
                    "required_fields",
                },
                "bootstrap.outer_state_identity.outer_state.reward_context.continuation",
            )
    for index, raw in enumerate(
        _sequence(
            payload["reward_offers"],
            "bootstrap.outer_state_identity.outer_state.reward_offers",
        )
    ):
        field = f"bootstrap.outer_state_identity.outer_state.reward_offers[{index}]"
        offer = _mapping(raw, field)
        expected = {"offer_id", "card_id", "upgrade"}
        if "received_card_instance_id" in offer:
            expected.add("received_card_instance_id")
        _exact(offer, expected, field)


def _bootstrap_to_dict(bootstrap: Plan1StageBootstrap) -> dict[str, object]:
    identity = bootstrap.outer_state_identity
    outer_state = identity.outer_state.to_dict()
    # Schema v1 does not expose even empty placeholders for unimplemented
    # drink/passive runtime.  The typed state is restored with empty tuples.
    outer_state.pop("drink_session_refs")
    outer_state.pop("passive_session_refs")
    return {
        "authority": bootstrap.authority.value,
        "authority_detail": bootstrap.authority_detail,
        "outer_state_identity": {
            "scope": identity.scope.value,
            "external_request_id": identity.external_request_id,
            "action_id": identity.action_id,
            "stage_kind": identity.stage_kind.value,
            "stage_type": identity.stage_type,
            "outer_state": outer_state,
        },
        "initial_state": {
            value.name: getattr(bootstrap.initial_state, value.name)
            for value in fields(Plan1ScalarState)
        },
        "attributes": bootstrap.attributes.to_dict(),
        "battle_bonus_permille": list(bootstrap.battle_bonus_permille),
        "lesson_buff_multiple_permille": bootstrap.lesson_buff_multiple_permille,
        "rng_before": bootstrap.rng_before,
    }


def _bootstrap_from_dict(value: object) -> Plan1StageBootstrap:
    payload = _mapping(value, "bootstrap")
    _exact(payload, _BOOTSTRAP_FIELDS, "bootstrap")
    identity_payload = _mapping(
        payload["outer_state_identity"], "bootstrap.outer_state_identity"
    )
    _exact(
        identity_payload, _OUTER_IDENTITY_FIELDS, "bootstrap.outer_state_identity"
    )
    outer_payload = _mapping(
        identity_payload["outer_state"],
        "bootstrap.outer_state_identity.outer_state",
    )
    _validate_outer_payload(outer_payload)
    scalar_payload = _mapping(payload["initial_state"], "bootstrap.initial_state")
    _exact(scalar_payload, _SCALAR_FIELDS, "bootstrap.initial_state")
    attributes_payload = _mapping(payload["attributes"], "bootstrap.attributes")
    _exact(attributes_payload, {"vocal", "dance", "visual"}, "bootstrap.attributes")
    stage_type = identity_payload["stage_type"]
    if stage_type is not None and not isinstance(stage_type, str):
        raise _error("plan1-loadout-text-invalid", "bootstrap.outer_state_identity.stage_type")
    try:
        authority = Plan1StageBootstrapAuthority(
            _text(payload["authority"], "bootstrap.authority")
        )
        scope = Plan1StageOuterIdentityScope(
            _text(identity_payload["scope"], "bootstrap.outer_state_identity.scope")
        )
        stage_kind = InitialRegularInnerStageKind(
            _text(
                identity_payload["stage_kind"],
                "bootstrap.outer_state_identity.stage_kind",
            )
        )
        typed_outer_payload = dict(outer_payload)
        typed_outer_payload["drink_session_refs"] = []
        typed_outer_payload["passive_session_refs"] = []
        outer_state = ProduceRolloutState.from_dict(typed_outer_payload)
        scalar = Plan1ScalarState(**dict(scalar_payload))
        attributes = AttributeValues.from_dict(attributes_payload)
        battle_bonus = _integer_tuple(
            payload["battle_bonus_permille"], "bootstrap.battle_bonus_permille"
        )
        if len(battle_bonus) != 3:
            raise _error(
                "plan1-loadout-battle-bonus-invalid",
                "bootstrap.battle_bonus_permille",
            )
        return Plan1StageBootstrap(
            authority=authority,
            authority_detail=_text(
                payload["authority_detail"], "bootstrap.authority_detail"
            ),
            outer_state_identity=Plan1StageOuterStateIdentity(
                scope=scope,
                external_request_id=_text(
                    identity_payload["external_request_id"],
                    "bootstrap.outer_state_identity.external_request_id",
                ),
                action_id=_text(
                    identity_payload["action_id"],
                    "bootstrap.outer_state_identity.action_id",
                ),
                stage_kind=stage_kind,
                stage_type=stage_type,
                outer_state=outer_state,
            ),
            initial_state=scalar,
            attributes=attributes,
            battle_bonus_permille=battle_bonus,  # type: ignore[arg-type]
            lesson_buff_multiple_permille=_integer(
                payload["lesson_buff_multiple_permille"],
                "bootstrap.lesson_buff_multiple_permille",
            ),
            rng_before=_integer(payload["rng_before"], "bootstrap.rng_before"),
        )
    except Plan1RuntimeLoadoutBundleError:
        raise
    except (TypeError, ValueError) as error:
        raise _error(
            "plan1-loadout-bootstrap-invalid", "bootstrap", str(error)
        ) from error


def _search_to_dict(rule: ProduceCardSearchRule) -> dict[str, object]:
    result: dict[str, object] = {}
    for value in fields(ProduceCardSearchRule):
        raw = getattr(rule, value.name)
        result[value.name] = list(raw) if isinstance(raw, tuple) else raw
    return result


def _search_from_dict(value: object, index: int) -> ProduceCardSearchRule:
    field = f"hand_add_support_loadout.support_card_searches[{index}]"
    payload = _mapping(value, field)
    _exact(payload, _SEARCH_FIELDS, field)
    kwargs: dict[str, object] = {}
    for name in _SEARCH_FIELDS:
        raw = payload[name]
        child = f"{field}.{name}"
        if name in _SEARCH_TUPLE_TEXT_FIELDS:
            kwargs[name] = _text_tuple_allow_empty(raw, child)
        elif name in _SEARCH_TUPLE_INT_FIELDS:
            kwargs[name] = _integer_tuple(raw, child)
        elif name in _SEARCH_BOOL_FIELDS:
            kwargs[name] = _boolean(raw, child)
        elif name in _SEARCH_INT_FIELDS:
            kwargs[name] = _integer(raw, child)
        else:
            if not isinstance(raw, str):
                raise _error("plan1-loadout-text-invalid", child)
            kwargs[name] = raw
    return ProduceCardSearchRule(**kwargs)  # type: ignore[arg-type]


def _text_tuple_allow_empty(value: object, field: str) -> tuple[str, ...]:
    sequence = _sequence(value, field)
    result: list[str] = []
    for index, item in enumerate(sequence):
        if not isinstance(item, str):
            raise _error("plan1-loadout-text-invalid", f"{field}[{index}]")
        result.append(item)
    return tuple(result)


def _hand_add_to_dict(loadout: Plan1HandAddSupportLoadout) -> dict[str, object]:
    return {
        "supports": [
            {"support_id": support.support_id, **support.to_dict()}
            for support in loadout.supports
        ],
        "support_card_searches": [
            _search_to_dict(loadout.support_card_searches[search_id])
            for search_id in sorted(loadout.support_card_searches)
        ],
        "lesson_type": loadout.lesson_type,
        "random_state": loadout.random_state,
        "rng_authority": loadout.rng_authority,
        "used_support_ids": list(loadout.used_support_ids),
    }


def _hand_add_from_dict(value: object) -> Plan1HandAddSupportLoadout:
    payload = _mapping(value, "hand_add_support_loadout")
    _exact(payload, _HAND_ADD_FIELDS, "hand_add_support_loadout")
    supports: list[SupportUpgradeRuntimeInput] = []
    for index, raw in enumerate(
        _sequence(payload["supports"], "hand_add_support_loadout.supports")
    ):
        field = f"hand_add_support_loadout.supports[{index}]"
        item = _mapping(raw, field)
        _exact(item, _SUPPORT_FIELDS, field)
        try:
            supports.append(
                SupportUpgradeRuntimeInput(
                    support_id=_text(item["support_id"], f"{field}.support_id"),
                    lesson_type=_text(item["lesson_type"], f"{field}.lesson_type"),
                    runtime_permil=_integer(
                        item["runtime_permil"], f"{field}.runtime_permil"
                    ),
                    loadout_order=_integer(
                        item["loadout_order"], f"{field}.loadout_order"
                    ),
                    card_search_id=_text(
                        item["card_search_id"], f"{field}.card_search_id"
                    ),
                )
            )
        except Plan1RuntimeLoadoutBundleError:
            raise
        except (TypeError, ValueError) as error:
            raise _error("plan1-loadout-support-invalid", field, str(error)) from error
    searches: dict[str, ProduceCardSearchRule] = {}
    for index, raw in enumerate(
        _sequence(
            payload["support_card_searches"],
            "hand_add_support_loadout.support_card_searches",
        )
    ):
        rule = _search_from_dict(raw, index)
        if rule.id in searches:
            raise _error(
                "plan1-loadout-search-identity-duplicate",
                "hand_add_support_loadout.support_card_searches",
                rule.id,
            )
        searches[rule.id] = rule
    try:
        return Plan1HandAddSupportLoadout(
            supports=tuple(supports),
            support_card_searches=searches,
            lesson_type=_text(
                payload["lesson_type"], "hand_add_support_loadout.lesson_type"
            ),
            random_state=_integer(
                payload["random_state"], "hand_add_support_loadout.random_state"
            ),
            rng_authority=_text(
                payload["rng_authority"], "hand_add_support_loadout.rng_authority"
            ),
            used_support_ids=_text_tuple(
                payload["used_support_ids"],
                "hand_add_support_loadout.used_support_ids",
            ),
        )
    except Plan1HandAddSupportProviderError as error:
        raise _error(
            error.code, "hand_add_support_loadout", error.detail
        ) from error


@dataclass(frozen=True, slots=True)
class InitialRegularPlan1RuntimeLoadoutBundle:
    """Strict version-1 caller runtime bundle; never authentic LocalSave."""

    authority: Plan1StageBootstrapAuthority
    runtime_ref: str
    produce_id: str
    character_id: str
    bootstrap: Plan1StageBootstrap
    hand_add_support_loadout: Plan1HandAddSupportLoadout
    mandatory_before_produce_item_id: str = FKTN_SSR_PLAN1_BEFORE_ITEM_ID
    authentic_localsave: bool = False
    schema_version: int = PLAN1_RUNTIME_LOADOUT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != PLAN1_RUNTIME_LOADOUT_SCHEMA_VERSION:
            raise _error(
                "plan1-loadout-schema-version-unsupported", "schema_version"
            )
        if not isinstance(self.authority, Plan1StageBootstrapAuthority):
            raise _error("plan1-loadout-authority-invalid", "authority")
        if not isinstance(self.bootstrap, Plan1StageBootstrap):
            raise _error("plan1-loadout-bootstrap-invalid", "bootstrap")
        if not isinstance(
            self.hand_add_support_loadout, Plan1HandAddSupportLoadout
        ):
            raise _error(
                "plan1-loadout-support-invalid", "hand_add_support_loadout"
            )
        _text(self.runtime_ref, "runtime_ref")
        _text(self.produce_id, "produce_id")
        _text(self.character_id, "character_id")
        if self.authority is not self.bootstrap.authority:
            raise _error(
                "plan1-loadout-authority-mismatch",
                "authority",
                f"bundle={self.authority.value}:bootstrap={self.bootstrap.authority.value}",
            )
        if self.mandatory_before_produce_item_id != FKTN_SSR_PLAN1_BEFORE_ITEM_ID:
            raise _error(
                "plan1-loadout-before-produce-item-override-forbidden",
                "mandatory_before_produce_item_id",
                str(self.mandatory_before_produce_item_id),
            )
        if type(self.authentic_localsave) is not bool or self.authentic_localsave:
            raise _error(
                "plan1-loadout-authentic-localsave-forbidden",
                "authentic_localsave",
                "this bundle is caller runtime, not an authentic LocalSave artifact",
            )
        outer = self.bootstrap.outer_state_identity.outer_state
        if outer.mode_id != self.produce_id:
            raise _error(
                "plan1-loadout-produce-mismatch",
                "produce_id",
                f"bundle={self.produce_id}:bootstrap={outer.mode_id}",
            )
        if outer.character_id != self.character_id:
            raise _error(
                "plan1-loadout-character-mismatch",
                "character_id",
                f"bundle={self.character_id}:bootstrap={outer.character_id}",
            )
        if outer.drink_session_refs or outer.passive_session_refs:
            raise _error(
                "plan1-loadout-runtime-field-unsupported",
                "bootstrap.outer_state_identity.outer_state",
                "drink/passive runtime refs are outside schema version 1",
            )
        if any(
            not support_lesson_type_matches(
                support.lesson_type, self.hand_add_support_loadout.lesson_type
            )
            for support in self.hand_add_support_loadout.supports
        ):
            raise _error(
                "plan1-loadout-support-lesson-mismatch",
                "hand_add_support_loadout.lesson_type",
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": PLAN1_RUNTIME_LOADOUT_SCHEMA,
            "schema_version": self.schema_version,
            "authority": self.authority.value,
            "runtime_ref": self.runtime_ref,
            "produce_id": self.produce_id,
            "character_id": self.character_id,
            "authentic_localsave": self.authentic_localsave,
            "mandatory_before_produce_item_id": (
                self.mandatory_before_produce_item_id
            ),
            "bootstrap": _bootstrap_to_dict(self.bootstrap),
            "hand_add_support_loadout": _hand_add_to_dict(
                self.hand_add_support_loadout
            ),
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
    ) -> "InitialRegularPlan1RuntimeLoadoutBundle":
        payload = _mapping(value, "bundle")
        _exact(payload, _ROOT_FIELDS, "bundle")
        if payload["schema"] != PLAN1_RUNTIME_LOADOUT_SCHEMA:
            raise _error("plan1-loadout-schema-unsupported", "schema")
        version = _integer(payload["schema_version"], "schema_version")
        if version != PLAN1_RUNTIME_LOADOUT_SCHEMA_VERSION:
            raise _error(
                "plan1-loadout-schema-version-unsupported",
                "schema_version",
                str(version),
            )
        try:
            authority = Plan1StageBootstrapAuthority(
                _text(payload["authority"], "authority")
            )
        except ValueError as error:
            raise _error(
                "plan1-loadout-authority-invalid", "authority", str(error)
            ) from error
        return cls(
            authority=authority,
            runtime_ref=_text(payload["runtime_ref"], "runtime_ref"),
            produce_id=_text(payload["produce_id"], "produce_id"),
            character_id=_text(payload["character_id"], "character_id"),
            bootstrap=_bootstrap_from_dict(payload["bootstrap"]),
            hand_add_support_loadout=_hand_add_from_dict(
                payload["hand_add_support_loadout"]
            ),
            mandatory_before_produce_item_id=_text(
                payload["mandatory_before_produce_item_id"],
                "mandatory_before_produce_item_id",
            ),
            authentic_localsave=_boolean(
                payload["authentic_localsave"], "authentic_localsave"
            ),
            schema_version=version,
        )

    @classmethod
    def from_json(cls, value: str) -> "InitialRegularPlan1RuntimeLoadoutBundle":
        if not isinstance(value, str):
            raise TypeError("value must be JSON text")
        try:
            payload = json.loads(value)
        except json.JSONDecodeError as error:
            raise _error(
                "plan1-loadout-json-invalid", "bundle", str(error)
            ) from error
        return cls.from_dict(_mapping(payload, "bundle"))


@dataclass(frozen=True, slots=True)
class InitialRegularPlan1RuntimeLoadoutProjection:
    """Existing runtime objects projected without mutating the fixture."""

    bootstrap: Plan1StageBootstrap
    support_loadout: Plan1HandAddSupportLoadout
    support_provider: Plan1HandAddSupportProvider
    authority: Plan1StageBootstrapAuthority
    runtime_ref: str
    mandatory_before_produce_item_id: str

    @property
    def hand_add_support_resolver(self) -> Plan1HandAddSupportProvider:
        return self.support_provider


def project_initial_regular_plan1_runtime_loadout(
    bundle: InitialRegularPlan1RuntimeLoadoutBundle,
    *,
    fixture: Plan1TerminalFixture,
) -> InitialRegularPlan1RuntimeLoadoutProjection:
    """Project bootstrap + HandAdd provider after fixture item validation."""

    if not isinstance(bundle, InitialRegularPlan1RuntimeLoadoutBundle):
        raise TypeError("bundle must be InitialRegularPlan1RuntimeLoadoutBundle")
    if not isinstance(fixture, Plan1TerminalFixture):
        raise TypeError("fixture must be Plan1TerminalFixture")
    manifest = fixture.compilation.manifest
    if manifest.produce_id != bundle.produce_id:
        raise _error(
            "plan1-loadout-fixture-produce-mismatch",
            "fixture.compilation.manifest.produce_id",
            f"fixture={manifest.produce_id}:bundle={bundle.produce_id}",
        )
    master_item_id = manifest.before_produce_item.item_id
    if (
        master_item_id != FKTN_SSR_PLAN1_BEFORE_ITEM_ID
        or master_item_id != bundle.mandatory_before_produce_item_id
    ):
        raise _error(
            "plan1-loadout-fixture-before-produce-item-mismatch",
            "fixture.compilation.manifest.before_produce_item.item_id",
            master_item_id,
        )
    runtime = fixture.stage.equipped_item_runtime
    if runtime is None or runtime.item_id != master_item_id:
        raise _error(
            "plan1-loadout-fixture-before-produce-runtime-mismatch",
            "fixture.stage.equipped_item_runtime",
            "missing" if runtime is None else runtime.item_id,
        )
    item_blockers = validate_fktn_ssr_plan1_equipped_item_runtime(runtime)
    if item_blockers:
        blocker = item_blockers[0]
        raise _error(
            "plan1-loadout-fixture-before-produce-runtime-invalid",
            "fixture.stage.equipped_item_runtime",
            f"{blocker.code}:{blocker.detail}",
        )
    provider = build_plan1_hand_add_support_provider(
        bundle.hand_add_support_loadout
    )
    return InitialRegularPlan1RuntimeLoadoutProjection(
        bootstrap=bundle.bootstrap,
        support_loadout=bundle.hand_add_support_loadout,
        support_provider=provider,
        authority=bundle.authority,
        runtime_ref=bundle.runtime_ref,
        mandatory_before_produce_item_id=master_item_id,
    )


def save_initial_regular_plan1_runtime_loadout(
    bundle: InitialRegularPlan1RuntimeLoadoutBundle,
    path: str | Path,
) -> Path:
    """Write one canonical, human-readable versioned bundle."""

    if not isinstance(bundle, InitialRegularPlan1RuntimeLoadoutBundle):
        raise TypeError("bundle must be InitialRegularPlan1RuntimeLoadoutBundle")
    target = Path(path)
    target.write_text(bundle.to_json(indent=2) + "\n", encoding="utf-8")
    return target


def load_initial_regular_plan1_runtime_loadout(
    path: str | Path,
) -> InitialRegularPlan1RuntimeLoadoutBundle:
    """Load one strict versioned bundle from disk."""

    return InitialRegularPlan1RuntimeLoadoutBundle.from_json(
        Path(path).read_text(encoding="utf-8")
    )


# Short public names for callers already scoped to Plan 1.
Plan1RuntimeLoadoutBundle = InitialRegularPlan1RuntimeLoadoutBundle
Plan1RuntimeLoadoutProjection = InitialRegularPlan1RuntimeLoadoutProjection
load_plan1_runtime_loadout_bundle = load_initial_regular_plan1_runtime_loadout
project_plan1_runtime_loadout_bundle = project_initial_regular_plan1_runtime_loadout
save_plan1_runtime_loadout_bundle = save_initial_regular_plan1_runtime_loadout


__all__ = [
    "PLAN1_RUNTIME_LOADOUT_SCHEMA",
    "PLAN1_RUNTIME_LOADOUT_SCHEMA_VERSION",
    "InitialRegularPlan1RuntimeLoadoutBundle",
    "InitialRegularPlan1RuntimeLoadoutProjection",
    "Plan1RuntimeLoadoutBundle",
    "Plan1RuntimeLoadoutBundleError",
    "Plan1RuntimeLoadoutProjection",
    "load_initial_regular_plan1_runtime_loadout",
    "load_plan1_runtime_loadout_bundle",
    "project_initial_regular_plan1_runtime_loadout",
    "project_plan1_runtime_loadout_bundle",
    "save_initial_regular_plan1_runtime_loadout",
    "save_plan1_runtime_loadout_bundle",
]
