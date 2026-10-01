"""A small, diagnostic adapter for replaying leaderboard ExamAction prefixes.

The leaderboard wire format and the native Plan2 horizon use different
identities.  A leaderboard ``use-hand`` action carries a hand *index*, while
the native reducer accepts the settled runtime GUID.  Likewise, a drink index
is meaningful only relative to the ordered, still-unconsumed drink runtime.
This module owns that translation and nothing else.

The adapter intentionally starts from one v2 episode.  It applies the fixed
contest inputs proven by the native builder (realized deck, direct exam items,
stage start statuses, gimmicks, bonus schedule) while retaining support,
memory activation, customize, and other upstream context explicitly.  It is
therefore still diagnostic, not a claim of exact recreation.  Card execution
continues through :class:`Plan2NativeReducer`; fixed-command acceptance is
reported separately when the ordinary/live preflight rejects an action that
the fixed sequence would nevertheless enqueue.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from functools import lru_cache
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .exam_reducer import (
    CanonicalAction,
    EffectSelect,
    EndTurn,
    PlayCard,
    Plan2NativeReducer,
    Transition,
    UseDrink,
)
from .leaderboard_replay import (
    LeaderboardReplayAction,
    LeaderboardReplayEpisode,
    SCHEMA as LEADERBOARD_EPISODE_SCHEMA,
    migrate_episode_payload,
)
from .master_db import DEFAULT_DATABASE
from .item_rules import load_item_rule
from .nia_static_adapter import (
    NiaAuditionDefinition,
    NiaTurnParameterSchedule,
    load_nia_static_bundle,
    replay_nia_turn_parameter_schedule,
)
from .native_exam_formula import ProduceParameterType
from .plan2_exam_save_battle_scoring import (
    PLAN2_EXAM_SAVE_BATTLE_SCORING_SCHEMA_VERSION,
    Plan2ExamSaveBattleScoringContext,
)
from .plan2_exam_mode import (
    Plan2ScheduledGimmick,
    plan2_scheduled_gimmick_hook_key,
)
from .plan2_native_horizon import (
    Plan2MasterInitialDeck,
    Plan2NativeBlocker,
    Plan2NativeDrinkInstance,
    Plan2NativeDrinkRuntime,
    Plan2NativeHorizonState,
    Plan2NativeProgramCatalog,
    bootstrap_plan2_native_master_deck,
    dispatch_plan2_native_turn_timer,
    execute_plan2_native_current_turn_gimmicks,
)
from .plan2_native_program_catalog import (
    Plan2NativeProgramCatalogCompilation,
    compile_plan2_native_program_catalog,
)
from .plan2_native_item_runtime import compile_plan2_native_item_rules
from .initial_regular_plan2_drink_runtime import (
    Plan2NativeDrinkCompilation,
    compile_plan2_native_drink_instance,
)
from .drink_catalog import load_drink_catalog
from .plan2_native_catalog_status_enchant import (
    install_plan2_native_status_enchant,
    load_plan2_native_bare_start_status_enchant,
)
from .initial_regular_plan2_gimmick_runtime import (
    build_plan2_master_review_gimmick_hooks,
    load_plan2_master_review_gimmick_group,
)


ADAPTER_SCHEMA = "gkms.leaderboard-plan2-prefix-replay.v2"
DIAGNOSTIC_DEFAULT_DECK_ID = "leaderboard-diagnostic-default"
DIAGNOSTIC_CHARACTER_DECK_ID = "leaderboard-diagnostic-character"
DIAGNOSTIC_DRAW_COUNT = 3
DIAGNOSTIC_HAND_LIMIT = 5

_PARAMETER_TYPE = {
    "ProduceParameterType_Vocal": ProduceParameterType.VOCAL,
    "ProduceParameterType_Dance": ProduceParameterType.DANCE,
    "ProduceParameterType_Visual": ProduceParameterType.VISUAL,
}

CatalogLoader = Callable[[], object]
DrinkRuntimeLoader = Callable[[Sequence[str]], object]


@lru_cache(maxsize=32)
def _nia_audition_definition(
    produce_id: str,
    idol_card_id: str,
    step_type: str,
    step_select_number: int,
) -> NiaAuditionDefinition:
    """Resolve the one Master audition row for a leaderboard episode."""

    bundle = load_nia_static_bundle(
        idol_card_id,
        produce_id=produce_id,
    )
    matches = tuple(
        audition
        for audition in bundle.auditions
        if audition.rules.step_type == step_type
        and audition.rules.number == step_select_number
    )
    if len(matches) != 1:
        raise ValueError(
            "leaderboard NIA audition schedule must resolve once: "
            f"{produce_id}/{idol_card_id}/{step_type}/{step_select_number}:"
            f"{len(matches)}"
        )
    return matches[0]


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


def _blocker(code: str, detail: object = "") -> Plan2NativeBlocker:
    return Plan2NativeBlocker(code, str(detail) if detail else "")


def _install_contest_stage_statuses(
    state: Plan2NativeHorizonState,
    episode: LeaderboardReplayEpisode,
    catalog: Plan2NativeProgramCatalog,
) -> tuple[Plan2NativeHorizonState, dict[str, Any]]:
    installed_rows: list[dict[str, Any]] = []
    after = state
    # Native SetUpExam installs permanent start statuses before regular start
    # statuses.  The v2 projection keeps these two raw stage fields distinct.
    for context_key, permanence in (
        ("exam_permanent_status_enchants", "permanent"),
        ("exam_status_enchants", "regular"),
    ):
        raw_rows = episode.stage_context.get(context_key, ())
        if raw_rows is None:
            raw_rows = ()
        if not isinstance(raw_rows, Sequence) or isinstance(
            raw_rows, (str, bytes)
        ):
            raise ValueError(f"stage_context.{context_key} must be an array")
        for index, raw in enumerate(raw_rows):
            if not isinstance(raw, Mapping):
                raise ValueError(
                    f"stage_context.{context_key}[{index}] must be an object"
                )
            status_id = _text(
                raw.get("id"),
                f"stage_context.{context_key}[{index}].id",
            )
            origin_owner = raw.get("originOwnerId", raw.get("origin_owner_id"))
            origin_id = raw.get("originId", raw.get("origin_id"))
            origin_level = raw.get("originLevel", raw.get("origin_level", 0))
            origin_level = _integer(
                origin_level,
                f"stage_context.{context_key}[{index}].originLevel",
            )
            source_id = next(
                (
                    value
                    for value in (origin_owner, origin_id, status_id)
                    if isinstance(value, str) and value
                ),
                status_id,
            )
            program = load_plan2_native_bare_start_status_enchant(
                status_id,
                source_id=source_id,
                source_upgrade=origin_level,
                database=DEFAULT_DATABASE,
            )
            next_uid = max(
                after.scalar.next_status_uid,
                after.status_enchant.next_status_uid,
            )
            runtime = replace(
                after.status_enchant,
                next_status_uid=next_uid,
            )
            installed = install_plan2_native_status_enchant(
                runtime,
                program,
                source_guid=(
                    f"contest-stage-{permanence}-{index}:{source_id}"
                ),
                completed_effect_ids=(),
            )
            if not installed.executable or not installed.installed:
                detail = ",".join(installed.unresolved) or "not-installed"
                raise ValueError(f"{status_id}:{detail}")
            assert installed.listener is not None
            after = replace(
                after,
                scalar=replace(
                    after.scalar,
                    next_status_uid=installed.after.next_status_uid,
                ),
                status_enchant=installed.after,
            )
            installed_rows.append(
                {
                    "id": status_id,
                    "source_id": source_id,
                    "source_level": origin_level,
                    "permanence": permanence,
                    "status_uid": installed.listener.status_uid,
                    "trigger_phase": program.trigger.phase,
                }
            )
    after, timer_trace = dispatch_plan2_native_turn_timer(after, catalog)
    return after, {
        "applied": bool(installed_rows),
        "installed": installed_rows,
        "initial_turn_timer_trace": list(timer_trace),
    }


def _install_contest_gimmicks(
    state: Plan2NativeHorizonState,
    episode: LeaderboardReplayEpisode,
    catalog: Plan2NativeProgramCatalog,
) -> tuple[Plan2NativeHorizonState, dict[str, Any]]:
    group_id = _text(
        episode.stage_context.get("produce_exam_gimmick_effect_group_id"),
        "stage_context.produce_exam_gimmick_effect_group_id",
    )
    group = load_plan2_master_review_gimmick_group(
        group_id,
        database=DEFAULT_DATABASE,
    )
    hooks = build_plan2_master_review_gimmick_hooks(group)
    scheduled = tuple(
        Plan2ScheduledGimmick(
            step.start_turn,
            group.group_id,
            step.effect.effect_id,
            hooks[
                plan2_scheduled_gimmick_hook_key(
                    step.start_turn,
                    group.group_id,
                    step.effect.effect_id,
                )
            ],
        )
        for step in group.steps
    )
    after = replace(state, scheduled_gimmicks=scheduled)
    after, initial_trace = execute_plan2_native_current_turn_gimmicks(
        after,
        catalog,
    )
    return after, {
        "applied": True,
        "group_id": group.group_id,
        "steps": [
            {
                "priority": step.priority,
                "turn": step.start_turn,
                "review_threshold": step.review_threshold,
                "effect_id": step.effect.effect_id,
                "effect_type": step.effect.effect_type,
            }
            for step in group.steps
        ],
        "initial_turn_trace": list(initial_trace),
    }


def _blocker_dict(value: Plan2NativeBlocker) -> dict[str, str]:
    return value.to_dict()


def _json_value(value: object) -> Any:
    """Make report values JSON-safe without serializing the full horizon."""

    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(child) for key, child in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(child) for child in value]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _json_value(to_dict())
    if hasattr(value, "value") and type(getattr(value, "value")) in {
        str,
        int,
    }:
        return getattr(value, "value")
    if hasattr(value, "__dataclass_fields__"):
        return {
            field.name: _json_value(getattr(value, field.name))
            for field in fields(value)
        }
    return str(value)


def _scalar_snapshot(state: Plan2NativeHorizonState | None) -> dict[str, Any] | None:
    """Project only the scalar fields useful for transition diagnostics."""

    if state is None:
        return None
    scalar = state.scalar
    return {
        "current_turn": scalar.current_turn,
        "review": scalar.review,
        "score": scalar.score,
        "review_count_add": scalar.review_count_add,
        "block": scalar.block,
        "card_play_aggressive": scalar.card_play_aggressive,
        "stamina": scalar.stamina,
        "max_stamina": scalar.max_stamina,
        "exam_card_play_count": scalar.exam_card_play_count,
        "turn_card_play_count": scalar.turn_card_play_count,
        "plays_remaining": state.plays_remaining,
        "limit_turn": state.limit_turn,
        "terminal": state.terminal,
    }


def _card_identity(card: object, *, hand_index: int | None = None) -> dict[str, Any]:
    """Return a stable identity projection for a native ordered card."""

    result: dict[str, Any] = {
        "guid": getattr(card, "guid", ""),
        "card_id": getattr(card, "card_id", ""),
        "upgrade": getattr(card, "effective_upgrade", None),
    }
    if hand_index is not None:
        result["hand_index"] = hand_index
    return result


def _action_dict(action: CanonicalAction | None) -> dict[str, Any] | None:
    if action is None:
        return None
    result: dict[str, Any] = {"kind": action.kind, "action_id": action.action_id}
    if isinstance(action, PlayCard):
        result["guid"] = action.guid
    elif isinstance(action, UseDrink):
        result.update(
            {
                "slot": action.slot,
                "instance": action.instance,
                "selection": action.selection,
            }
        )
    elif isinstance(action, EffectSelect):
        result["indexes"] = list(action.indexes)
    return result


def _episode_from_mapping(payload: Mapping[str, Any]) -> LeaderboardReplayEpisode:
    """Rehydrate a canonical v2 JSON object into the typed episode class."""

    value = migrate_episode_payload(payload)
    raw_actions = value.get("actions")
    if not isinstance(raw_actions, Sequence) or isinstance(raw_actions, (str, bytes)):
        raise ValueError("leaderboard episode actions must be an array")
    actions: list[LeaderboardReplayAction] = []
    for index, raw in enumerate(raw_actions):
        if not isinstance(raw, Mapping):
            raise ValueError(f"leaderboard episode action {index} must be an object")
        indexes = raw.get("indexes", ())
        if not isinstance(indexes, Sequence) or isinstance(indexes, (str, bytes)):
            raise ValueError(f"leaderboard episode action {index}.indexes must be an array")
        actions.append(
            LeaderboardReplayAction(
                order=_integer(raw.get("order", index), f"actions[{index}].order"),
                action_type=_text(raw.get("action_type"), f"actions[{index}].action_type"),
                indexes=tuple(
                    _integer(item, f"actions[{index}].indexes[]") for item in indexes
                ),
            )
        )

    known = {field.name for field in fields(LeaderboardReplayEpisode)}
    episode_values = {name: value[name] for name in known if name in value}
    episode_values["actions"] = tuple(actions)
    episode_values["produce_cards"] = tuple(
        row for row in value.get("produce_cards", ()) if isinstance(row, Mapping)
    )
    episode_values["produce_items"] = tuple(
        row for row in value.get("produce_items", ()) if isinstance(row, Mapping)
    )
    episode_values["support_cards"] = tuple(
        row for row in value.get("support_cards", ()) if isinstance(row, Mapping)
    )
    raw_memory_abilities = value.get("memory_abilities")
    if not isinstance(raw_memory_abilities, Sequence) or isinstance(
        raw_memory_abilities, (str, bytes)
    ):
        raise ValueError("leaderboard replay v2 memory_abilities must be an array")
    if any(not isinstance(row, Mapping) for row in raw_memory_abilities):
        raise ValueError("leaderboard replay v2 memory_abilities must contain objects")
    episode_values["memory_abilities"] = tuple(raw_memory_abilities)
    raw_memory_loadout = value.get("memory_loadout", ())
    if not isinstance(raw_memory_loadout, Sequence) or isinstance(
        raw_memory_loadout, (str, bytes)
    ):
        raise ValueError("leaderboard replay v2 memory_loadout must be an array")
    if any(not isinstance(row, Mapping) for row in raw_memory_loadout):
        raise ValueError("leaderboard replay v2 memory_loadout must contain objects")
    episode_values["memory_loadout"] = tuple(raw_memory_loadout)
    for name in (
        "vocal_bonus_permil",
        "dance_bonus_permil",
        "visual_bonus_permil",
    ):
        if name not in value:
            raise ValueError(f"leaderboard replay v2 requires {name}")
    episode_values["produce_customize_item_ids"] = tuple(
        _text(item, "produce_customize_item_id")
        for item in value.get("produce_customize_item_ids", ())
    )
    episode_values["produce_drink_ids"] = tuple(
        _text(item, "produce_drink_id") for item in value.get("produce_drink_ids", ())
    )
    episode_values["stage_context"] = value.get("stage_context", {})
    episode_values["section_context"] = value.get("section_context", {})
    episode_values["schema"] = value.get("schema", LEADERBOARD_EPISODE_SCHEMA)
    return LeaderboardReplayEpisode(**episode_values)


def read_leaderboard_episode(
    source: str | Path | Mapping[str, Any] | LeaderboardReplayEpisode,
    *,
    episode_index: int = 0,
) -> LeaderboardReplayEpisode:
    """Read one v2 row from a JSON object, JSON array, or JSONL file."""

    if isinstance(source, LeaderboardReplayEpisode):
        return source
    if isinstance(source, Mapping):
        return _episode_from_mapping(source)
    path = Path(source)
    if not path.is_file():
        raise FileNotFoundError(path)
    raw_text = path.read_text(encoding="utf-8-sig")
    if not raw_text.strip():
        raise ValueError(f"leaderboard episode source is empty: {path}")
    try:
        decoded = json.loads(raw_text)
    except json.JSONDecodeError:
        rows = [json.loads(line) for line in raw_text.splitlines() if line.strip()]
        decoded = rows
    if isinstance(decoded, Mapping):
        return _episode_from_mapping(decoded)
    if not isinstance(decoded, Sequence) or isinstance(decoded, (str, bytes)):
        raise ValueError("leaderboard episode source must be a v2 object/array/JSONL")
    index = _integer(episode_index, "episode_index")
    if index >= len(decoded):
        raise IndexError(f"episode_index-out-of-range:{index}/{len(decoded)}")
    row = decoded[index]
    if not isinstance(row, Mapping):
        raise ValueError(f"leaderboard episode row {index} must be an object")
    return _episode_from_mapping(row)


def build_plan2_master_initial_deck(
    episode: LeaderboardReplayEpisode | Mapping[str, Any],
) -> Plan2MasterInitialDeck:
    """Build the card-only diagnostic Plan2 identity manifest for an episode."""

    typed = (
        episode
        if isinstance(episode, LeaderboardReplayEpisode)
        else _episode_from_mapping(episode)
    )
    if typed.plan_type != "ProducePlanType_Plan2":
        raise ValueError(f"leaderboard episode is not Plan2: {typed.plan_type!r}")
    refs: list[tuple[str, int]] = []
    for index, row in enumerate(typed.produce_cards):
        if not isinstance(row, Mapping):
            raise ValueError(f"produce_cards[{index}] must be an object")
        card_id = row.get("id", row.get("cardId"))
        upgrade = row.get("upgradeCount", row.get("upgrade_count", 0))
        refs.append(
            (
                _text(card_id, f"produce_cards[{index}].id"),
                _integer(upgrade, f"produce_cards[{index}].upgradeCount"),
            )
        )
    if not refs:
        raise ValueError("leaderboard episode produce_cards must be non-empty")
    # These two IDs are labels only: the episode gives us the realized card
    # list, not the Master deck-row identities that produced it.
    item_context = _contest_item_context(
        tuple(
            _text(
                row.get("produceItemId", row.get("id")),
                f"produce_items[{index}].produceItemId",
            )
            for index, row in enumerate(typed.produce_items)
        )
    )
    return Plan2MasterInitialDeck(
        idol_card_id=typed.idol_card_id,
        produce_id=typed.produce_id,
        plan_type="ProducePlanType_Plan2",
        exam_effect_type=_text(typed.exam_effect_type, "episode.exam_effect_type"),
        produce_default_deck_id=DIAGNOSTIC_DEFAULT_DECK_ID,
        character_deck_id=DIAGNOSTIC_CHARACTER_DECK_ID,
        card_refs=tuple(refs),
        item_ids=tuple(item_context["direct_exam_item_ids"]),
    )


@lru_cache(maxsize=128)
def _contest_item_context(item_ids: tuple[str, ...]) -> dict[str, Any]:
    """Classify contest ItemList rows by their Master execution boundary.

    Direct exam enchantments belong to ``ExamContestPlayerData.ItemList`` and
    can enter the native item runtime.  Items with only Produce/outer effects
    have already shaped the realized deck/loadout and are not replayed inside
    the fixed contest battle.  An item that does contain a direct enchantment
    but cannot compile remains explicit diagnostic debt; it is never silently
    treated as outer-only.
    """

    direct: list[str] = []
    outer_only: list[str] = []
    unsupported: list[dict[str, Any]] = []
    for item_id in item_ids:
        try:
            rule = load_item_rule(item_id)
        except (KeyError, OSError, TypeError, ValueError) as error:
            unsupported.append(
                {
                    "item_id": item_id,
                    "reason": f"{type(error).__name__}:{error}",
                }
            )
            continue
        if not rule.enchantments:
            outer_only.append(item_id)
            continue
        compilation = compile_plan2_native_item_rules((rule,))
        if compilation.supported:
            direct.append(item_id)
        else:
            unsupported.append(
                {
                    "item_id": item_id,
                    "reason": ";".join(compilation.blockers),
                }
            )
    return {
        "direct_exam_item_ids": tuple(direct),
        "outer_only_item_ids": tuple(outer_only),
        "unsupported_direct_items": tuple(unsupported),
    }


@lru_cache(maxsize=1)
def _default_catalog_loader() -> Plan2NativeProgramCatalog:
    compilation = compile_plan2_native_program_catalog(database=DEFAULT_DATABASE)
    if not isinstance(compilation, Plan2NativeProgramCatalogCompilation):
        raise TypeError("catalog compiler returned an invalid compilation")
    return compilation.catalog


def _default_drink_runtime_loader(
    drink_ids: Sequence[str],
) -> Plan2NativeDrinkRuntime:
    if not drink_ids:
        return Plan2NativeDrinkRuntime()
    catalog = load_drink_catalog(DEFAULT_DATABASE)
    instances: list[Plan2NativeDrinkInstance] = []
    for slot, drink_id in enumerate(drink_ids):
        drink = catalog.get_drink(drink_id)
        compilation = compile_plan2_native_drink_instance(
            drink,
            instance_id=f"leaderboard-drink-{slot:03d}-{drink_id}",
            session_ref=drink_id,
            database=DEFAULT_DATABASE,
        )
        if not isinstance(compilation, Plan2NativeDrinkCompilation):
            raise TypeError("drink compiler returned an invalid compilation")
        if not compilation.supported or compilation.instance is None:
            detail = ";".join(
                value.code + (":" + value.detail if value.detail else "")
                for value in compilation.blockers
            )
            raise ValueError(f"{drink_id}:{detail or 'unsupported'}")
        instances.append(compilation.instance)
    return Plan2NativeDrinkRuntime(tuple(instances))


@dataclass(frozen=True, slots=True)
class LeaderboardReplayStep:
    """One translated source action and its native reducer result."""

    order: int
    source_action: LeaderboardReplayAction
    action: CanonicalAction | None
    action_id: str | None
    card_identity: Mapping[str, Any] | None
    before_scalar: Mapping[str, Any] | None
    after_scalar: Mapping[str, Any] | None
    trace: tuple[str, ...] = ()
    blockers: tuple[Plan2NativeBlocker, ...] = ()
    ordinary_blockers: tuple[Plan2NativeBlocker, ...] = ()
    fixed_command_accepted: bool = False
    supported: bool = False
    terminal: bool = False

    @property
    def blocker(self) -> Plan2NativeBlocker | None:
        return self.blockers[0] if self.blockers else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "order": self.order,
            "source_action": {
                "order": self.source_action.order,
                "action_type": self.source_action.action_type,
                "indexes": list(self.source_action.indexes),
            },
            "action": _action_dict(self.action),
            "action_id": self.action_id,
            "card_identity": _json_value(self.card_identity),
            "before_scalar": _json_value(self.before_scalar),
            "after_scalar": _json_value(self.after_scalar),
            # Short aliases make the JSON useful to simple training/logging
            # consumers without requiring them to understand the dataclass
            # names.  The canonical fields above remain the typed contract.
            "before": _json_value(self.before_scalar),
            "after": _json_value(self.after_scalar),
            "trace": list(self.trace),
            "blockers": [_blocker_dict(value) for value in self.blockers],
            "ordinary_blockers": [
                _blocker_dict(value) for value in self.ordinary_blockers
            ],
            "blocker": (
                None if self.blocker is None else _blocker_dict(self.blocker)
            ),
            "supported": self.supported,
            "fixed_command_accepted": self.fixed_command_accepted,
            "terminal": self.terminal,
        }


@dataclass(frozen=True, slots=True)
class LeaderboardReplayReport:
    """Prefix replay output; ``exact`` is deliberately always false here."""

    episode: LeaderboardReplayEpisode
    manifest: Plan2MasterInitialDeck | None
    initial_state: Plan2NativeHorizonState | None
    final_state: Plan2NativeHorizonState | None
    steps: tuple[LeaderboardReplayStep, ...]
    bootstrap_blockers: tuple[Plan2NativeBlocker, ...] = ()
    first_blocker: Plan2NativeBlocker | None = None
    ignored_context: Mapping[str, Any] = field(default_factory=dict)
    unapplied_context: tuple[str, ...] = ()
    exact: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.episode, LeaderboardReplayEpisode):
            raise TypeError("episode must be LeaderboardReplayEpisode")
        if self.manifest is not None and not isinstance(
            self.manifest, Plan2MasterInitialDeck
        ):
            raise TypeError("manifest must be Plan2MasterInitialDeck or None")
        if self.initial_state is not None and not isinstance(
            self.initial_state, Plan2NativeHorizonState
        ):
            raise TypeError("initial_state must be Plan2NativeHorizonState or None")
        if self.final_state is not None and not isinstance(
            self.final_state, Plan2NativeHorizonState
        ):
            raise TypeError("final_state must be Plan2NativeHorizonState or None")
        if any(not isinstance(value, LeaderboardReplayStep) for value in self.steps):
            raise TypeError("steps must contain LeaderboardReplayStep values")
        if any(not isinstance(value, Plan2NativeBlocker) for value in self.bootstrap_blockers):
            raise TypeError("bootstrap_blockers must contain Plan2NativeBlocker values")
        if self.first_blocker is not None and not isinstance(
            self.first_blocker, Plan2NativeBlocker
        ):
            raise TypeError("first_blocker must be Plan2NativeBlocker or None")
        if not isinstance(self.ignored_context, Mapping):
            raise TypeError("ignored_context must be a mapping")
        if self.exact:
            raise ValueError("leaderboard prefix report cannot claim exact replay")

    @property
    def supported_steps(self) -> int:
        return sum(1 for step in self.steps if step.supported)

    @property
    def fixed_command_steps(self) -> int:
        return sum(1 for step in self.steps if step.fixed_command_accepted)

    @property
    def transitions(self) -> tuple[LeaderboardReplayStep, ...]:
        """Alias used by replay/training callers that call rows transitions."""

        return self.steps

    @property
    def bootstrap_context(self) -> Mapping[str, Any]:
        """Combined diagnostic context; no field is implied to be applied."""

        return {
            "ignored": self.ignored_context,
            "unapplied": list(self.unapplied_context),
            "exact": False,
        }

    @property
    def stopped(self) -> bool:
        return self.first_blocker is not None

    def to_dict(self) -> dict[str, Any]:
        manifest = None
        if self.manifest is not None:
            manifest = {
                "idol_card_id": self.manifest.idol_card_id,
                "produce_id": self.manifest.produce_id,
                "plan_type": self.manifest.plan_type,
                "exam_effect_type": self.manifest.exam_effect_type,
                "produce_default_deck_id": self.manifest.produce_default_deck_id,
                "character_deck_id": self.manifest.character_deck_id,
                "card_refs": [list(ref) for ref in self.manifest.card_refs],
                "item_ids": list(self.manifest.item_ids),
            }
        return {
            "schema": ADAPTER_SCHEMA,
            "exact": False,
            "episode": self.episode.to_dict(),
            "manifest": manifest,
            "initial_scalar": _scalar_snapshot(self.initial_state),
            "final_scalar": _scalar_snapshot(self.final_state),
            "steps": [step.to_dict() for step in self.steps],
            "bootstrap_blockers": [
                _blocker_dict(value) for value in self.bootstrap_blockers
            ],
            "first_blocker": (
                None if self.first_blocker is None else _blocker_dict(self.first_blocker)
            ),
            "ignored_context": _json_value(self.ignored_context),
            "unapplied_context": list(self.unapplied_context),
            "bootstrap_context": _json_value(self.bootstrap_context),
            "supported_steps": self.supported_steps,
            "fixed_command_steps": self.fixed_command_steps,
            "stopped": self.stopped,
        }


def _context_for_episode(
    episode: LeaderboardReplayEpisode,
    *,
    drink_runtime_applied: bool,
    turn_schedule: Mapping[str, Any] | None,
    stage_status_context: Mapping[str, Any] | None = None,
    stage_status_error: str | None = None,
    gimmick_context: Mapping[str, Any] | None = None,
    gimmick_error: str | None = None,
    drink_runtime_error: str | None = None,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Retain every source field not represented by diagnostic bootstrap."""

    item_context = _contest_item_context(
        tuple(
            str(row.get("produceItemId", row.get("id", "")))
            for row in episode.produce_items
        )
    )
    ignored: dict[str, Any] = {
        "produce_items": [dict(row) for row in episode.produce_items],
        "produce_customize_item_ids": list(episode.produce_customize_item_ids),
        "support_cards": {
            "rows": [dict(row) for row in episode.support_cards],
            "applied": False,
            "reason": "ExamContestPlayerData has no support-card runtime field",
        },
        "memory_abilities": [dict(row) for row in episode.memory_abilities],
        "memory_loadout": [dict(row) for row in episode.memory_loadout],
        "battle_bonus_permil": {
            "applied": True,
            "vocal": episode.vocal_bonus_permil,
            "dance": episode.dance_bonus_permil,
            "visual": episode.visual_bonus_permil,
        },
        "contest_start_stamina": {
            "applied": True,
            "value": episode.max_stamina,
            "episode_stamina": episode.stamina,
            "reason": (
                "ExamContestPlayerData initializes current stamina from MaxStamina"
            ),
        },
        "card_customizes": [
            {
                "card_index": index,
                "card_id": str(row.get("id", "")),
                "customizes": _json_value(row.get("customizes", ())),
            }
            for index, row in enumerate(episode.produce_cards)
            if row.get("customizes")
        ],
        "turn_parameter_schedule": (
            dict(turn_schedule)
            if turn_schedule is not None
            else {"rng_before_shuffle_applied": False}
        ),
        "stage_context": dict(episode.stage_context),
        "stage_status_runtime": {
            "applied": stage_status_context is not None,
            "runtime": (
                None
                if stage_status_context is None
                else dict(stage_status_context)
            ),
            "reason": stage_status_error,
        },
        "gimmick_runtime": {
            "applied": gimmick_context is not None,
            "runtime": (
                None if gimmick_context is None else dict(gimmick_context)
            ),
            "reason": gimmick_error,
        },
        "section_context": dict(episode.section_context),
        "items": {
            "applied": bool(item_context["direct_exam_item_ids"]),
            "direct_exam_item_ids": list(item_context["direct_exam_item_ids"]),
            "outer_only_item_ids": list(item_context["outer_only_item_ids"]),
            "unsupported_direct_items": [
                dict(row) for row in item_context["unsupported_direct_items"]
            ],
            "reason": (
                "direct exam enchantments loaded; Produce-only effects are upstream"
                if not item_context["unsupported_direct_items"]
                else "one or more direct exam item programs remain unsupported"
            ),
            "ids": [
                str(row.get("produceItemId", row.get("id", "")))
                for row in episode.produce_items
            ],
        },
        "drinks": {
            "ids": list(episode.produce_drink_ids),
            "applied": drink_runtime_applied,
            "reason": drink_runtime_error
            if drink_runtime_error
            else "ordered native drink runtime",
        },
    }
    unapplied: list[str] = [
        "produce_items",
        "produce_customize_item_ids",
        "memory_abilities",
        "memory_loadout",
        "card_customizes",
        "stage_context",
        "section_context",
    ]
    if episode.support_cards:
        unapplied.append("support_cards")
    if episode.produce_drink_ids and not drink_runtime_applied:
        unapplied.append("produce_drink_ids")
    if (
        episode.stage_context.get("exam_status_enchants")
        or episode.stage_context.get("exam_permanent_status_enchants")
    ) and stage_status_context is None:
        unapplied.append("stage_status_runtime")
    if (
        episode.stage_context.get("produce_exam_gimmick_effect_group_id")
        and gimmick_context is None
    ):
        unapplied.append("gimmick_runtime")
    return ignored, tuple(unapplied)


def _map_action(
    state: Plan2NativeHorizonState,
    source: LeaderboardReplayAction,
) -> tuple[CanonicalAction | None, Mapping[str, Any] | None, Plan2NativeBlocker | None]:
    """Translate one wire action, retaining the source card identity."""

    indexes = source.indexes
    if source.action_type == "use-hand":
        if len(indexes) != 1:
            return None, None, _blocker("hand-index-arity", len(indexes))
        index = indexes[0]
        if index >= len(state.zones.hand):
            return (
                None,
                None,
                _blocker("hand-index-out-of-range", f"index={index};size={len(state.zones.hand)}"),
            )
        card = state.zones.hand[index]
        return PlayCard(card.guid), _card_identity(card, hand_index=index), None

    if source.action_type == "use-drink":
        if len(indexes) != 1:
            return None, None, _blocker("drink-index-arity", len(indexes))
        index = indexes[0]
        inventory = state.drink_runtime.inventory
        if index >= len(inventory):
            return (
                None,
                None,
                _blocker("drink-index-out-of-range", f"index={index};size={len(inventory)}"),
            )
        drink = inventory[index]
        identity = {
            "slot": index,
            "instance": drink.instance_id,
            "session_ref": drink.session_ref,
            "drink_id": drink.drink_id,
        }
        return UseDrink(index, drink.instance_id), identity, None

    if source.action_type == "turn-end":
        if indexes:
            return None, None, _blocker("turn-end-indexes-unexpected", indexes)
        return EndTurn(), None, None

    if source.action_type == "effect-card-select":
        return EffectSelect(tuple(indexes)), None, None

    return None, None, _blocker("action-type-unsupported", source.action_type)


def replay_leaderboard_episode(
    source: LeaderboardReplayEpisode | Mapping[str, Any] | str | Path,
    *,
    catalog: Plan2NativeProgramCatalog | None = None,
    catalog_loader: CatalogLoader | None = None,
    drink_runtime: Plan2NativeDrinkRuntime | None = None,
    drink_runtime_loader: DrinkRuntimeLoader | None = None,
    episode_index: int = 0,
) -> LeaderboardReplayReport:
    """Replay a v2 episode until the first mapping/reducer blocker.

    ``catalog`` and ``drink_runtime`` are injectable so tests can use a small
    reviewed fixture.  With neither supplied, the current local Master
    catalog/drink compiler is used.  Compilation failures become typed report
    blockers; they are not converted into guessed card or drink effects.
    """

    episode = read_leaderboard_episode(source, episode_index=episode_index)
    manifest: Plan2MasterInitialDeck | None = None
    bootstrap_blockers: list[Plan2NativeBlocker] = []
    initial_state: Plan2NativeHorizonState | None = None
    schedule_context: dict[str, Any] | None = None
    try:
        manifest = build_plan2_master_initial_deck(episode)
        if episode.limit_turn is None:
            raise ValueError("episode.limit_turn is missing")
        audition = _nia_audition_definition(
            episode.produce_id,
            episode.idol_card_id,
            str(episode.step_type),
            episode.step_select_number,
        )
        schedule = audition.turn_parameter_schedule
        replayed_schedule = replay_nia_turn_parameter_schedule(
            schedule,
            episode.seed,
        )
        schedule_context = {
            "parameter_types": list(replayed_schedule.parameter_types),
            "random_state_before": replayed_schedule.random_state_before,
            "random_state_after": replayed_schedule.random_state_after,
            "rng_draws": replayed_schedule.rng_draws,
            "rng_before_shuffle_applied": True,
            "scoring_schedule_applied": True,
        }
        boot = bootstrap_plan2_native_master_deck(
            manifest,
            # CalcTurnParameterType consumes this same native RNG before the
            # initial deck shuffle.  Passing Player.Seed directly here gives
            # the wrong opening hand even when every card effect is correct.
            random_state=replayed_schedule.random_state_after,
            limit_turn=episode.limit_turn,
            # ExamContestPlayerData carries only MaxStamina.  The native
            # contest builder assigns it to both current and maximum stamina;
            # the episode's separate scalar stays report-only.
            stamina=episode.max_stamina,
            max_stamina=episode.max_stamina,
            draw_count=DIAGNOSTIC_DRAW_COUNT,
            hand_limit=DIAGNOSTIC_HAND_LIMIT,
        )
        bootstrap_blockers.extend(boot.blockers)
        initial_state = boot.state
        if initial_state is not None:
            try:
                parameter_types = tuple(
                    _PARAMETER_TYPE[value]
                    for value in replayed_schedule.parameter_types
                )
            except KeyError as error:
                raise ValueError(
                    f"unsupported NIA turn parameter type: {error.args[0]}"
                ) from error
            scoring = Plan2ExamSaveBattleScoringContext(
                schema_version=PLAN2_EXAM_SAVE_BATTLE_SCORING_SCHEMA_VERSION,
                current_turn=1,
                limit_turn=episode.limit_turn,
                turn_parameter_types=parameter_types,
                vocal_bonus_permille=episode.vocal_bonus_permil,
                dance_bonus_permille=episode.dance_bonus_permil,
                visual_bonus_permille=episode.visual_bonus_permil,
                judge_parameter=0,
                clear_border=audition.clear_rank,
            )
            initial_state = replace(
                initial_state,
                scalar=replace(
                    initial_state.scalar,
                    battle_scoring=scoring,
                ),
                judge_parameter=0,
            )
    except (KeyError, OSError, TypeError, ValueError, OverflowError) as error:
        bootstrap_blockers.append(
            _blocker("leaderboard-diagnostic-bootstrap-failed", f"{type(error).__name__}:{error}")
        )

    drink_error: str | None = None
    drinks_applied = False
    if initial_state is not None:
        try:
            resolved_drinks = drink_runtime
            if resolved_drinks is None:
                loader = drink_runtime_loader or _default_drink_runtime_loader
                candidate = loader(episode.produce_drink_ids)
                if not isinstance(candidate, Plan2NativeDrinkRuntime):
                    raise TypeError("drink runtime loader returned an invalid runtime")
                resolved_drinks = candidate
            if tuple(value.drink_id for value in resolved_drinks.inventory) != tuple(
                episode.produce_drink_ids
            ):
                raise ValueError(
                    "drink-runtime-episode-order-mismatch:"
                    f"expected={tuple(episode.produce_drink_ids)!r};"
                    f"actual={tuple(value.drink_id for value in resolved_drinks.inventory)!r}"
                )
            initial_state = replace(initial_state, drink_runtime=resolved_drinks)
            drinks_applied = True
        except (
            KeyError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            OverflowError,
        ) as error:
            drink_error = f"{type(error).__name__}:{error}"
            bootstrap_blockers.append(_blocker("leaderboard-drink-runtime-unavailable", drink_error))

    if catalog is None:
        try:
            loader = catalog_loader or _default_catalog_loader
            candidate = loader()
            if isinstance(candidate, Plan2NativeProgramCatalogCompilation):
                candidate = candidate.catalog
            if not isinstance(candidate, Plan2NativeProgramCatalog):
                raise TypeError("catalog loader returned an invalid catalog")
            catalog = candidate
        except (FileNotFoundError, KeyError, OSError, TypeError, ValueError, OverflowError) as error:
            bootstrap_blockers.append(
                _blocker("leaderboard-catalog-unavailable", f"{type(error).__name__}:{error}")
            )

    stage_status_context: dict[str, Any] | None = None
    stage_status_error: str | None = None
    if initial_state is not None and catalog is not None:
        try:
            initial_state, stage_status_context = _install_contest_stage_statuses(
                initial_state,
                episode,
                catalog,
            )
        except (KeyError, OSError, TypeError, ValueError, OverflowError) as error:
            stage_status_error = f"{type(error).__name__}:{error}"
            bootstrap_blockers.append(
                _blocker(
                    "leaderboard-stage-status-runtime-unavailable",
                    stage_status_error,
                )
            )

    gimmick_context: dict[str, Any] | None = None
    gimmick_error: str | None = None
    if initial_state is not None and catalog is not None:
        try:
            initial_state, gimmick_context = _install_contest_gimmicks(
                initial_state,
                episode,
                catalog,
            )
        except (
            KeyError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            OverflowError,
        ) as error:
            gimmick_error = f"{type(error).__name__}:{error}"
            bootstrap_blockers.append(
                _blocker(
                    "leaderboard-gimmick-runtime-unavailable",
                    gimmick_error,
                )
            )

    ignored_context, unapplied_context = _context_for_episode(
        episode,
        drink_runtime_applied=drinks_applied,
        turn_schedule=schedule_context,
        stage_status_context=stage_status_context,
        stage_status_error=stage_status_error,
        gimmick_context=gimmick_context,
        gimmick_error=gimmick_error,
        drink_runtime_error=drink_error,
    )

    steps: list[LeaderboardReplayStep] = []
    # A missing drink compiler must not hide otherwise useful card-prefix
    # evidence.  The corresponding use-drink row will receive a precise
    # ordered-runtime/index blocker when it is reached.  Catalog/bootstrap
    # failures remain fatal because no reducer execution would be meaningful.
    fatal_bootstrap_blockers = tuple(
        value
        for value in bootstrap_blockers
        if value.code != "leaderboard-drink-runtime-unavailable"
    )
    first_blocker: Plan2NativeBlocker | None = (
        fatal_bootstrap_blockers[0] if fatal_bootstrap_blockers else None
    )
    if initial_state is not None and catalog is not None and not fatal_bootstrap_blockers:
        reducer = Plan2NativeReducer(catalog)
        state = initial_state
        for source_action in episode.actions:
            action, identity, mapping_blocker = _map_action(state, source_action)
            before_scalar = _scalar_snapshot(state)
            if mapping_blocker is not None:
                step = LeaderboardReplayStep(
                    order=source_action.order,
                    source_action=source_action,
                    action=None,
                    action_id=None,
                    card_identity=identity,
                    before_scalar=before_scalar,
                    after_scalar=None,
                    blockers=(mapping_blocker,),
                )
                steps.append(step)
                first_blocker = first_blocker or mapping_blocker
                break
            assert action is not None
            transition: Transition = reducer.reduce(state, action)
            after = transition.after
            ordinary_blockers = transition.unsupported
            fixed_command_accepted = True
            blockers = ordinary_blockers
            trace = transition.trace
            if not transition.supported:
                # ExamFixedActionSequenceSimulator submits the indexed command
                # without the ordinary/live ValidateUseHandCard preflight.
                # Payment/direct-effect behavior is a later native command
                # boundary, so retain the ordinary blocker diagnostically but
                # do not mislabel the fixed command itself as rejected.
                blockers = (
                    _blocker(
                        "fixed-executor-post-command-unresolved",
                        ";".join(
                            value.code
                            + (":" + value.detail if value.detail else "")
                            for value in ordinary_blockers
                        )
                        or action.action_id,
                    ),
                )
                trace = (
                    *transition.trace,
                    f"fixed-command-accepted:{action.action_id}",
                    "fixed-post-command-outcome-unresolved",
                )
            step = LeaderboardReplayStep(
                order=source_action.order,
                source_action=source_action,
                action=action,
                action_id=action.action_id,
                card_identity=identity,
                before_scalar=before_scalar,
                after_scalar=_scalar_snapshot(after),
                trace=trace,
                blockers=blockers,
                ordinary_blockers=ordinary_blockers,
                fixed_command_accepted=fixed_command_accepted,
                supported=transition.supported,
                terminal=transition.terminal,
            )
            steps.append(step)
            if not transition.supported or after is None:
                first_blocker = first_blocker or (
                    blockers[0]
                    if blockers
                    else _blocker("leaderboard-reducer-no-after-state", action.action_id)
                )
                break
            state = after

        final_state = state
    else:
        final_state = None

    return LeaderboardReplayReport(
        episode=episode,
        manifest=manifest,
        initial_state=initial_state,
        final_state=final_state,
        steps=tuple(steps),
        bootstrap_blockers=tuple(bootstrap_blockers),
        first_blocker=first_blocker,
        ignored_context=ignored_context,
        unapplied_context=unapplied_context,
    )


def replay_leaderboard_episode_file(
    source: str | Path,
    *,
    episode_index: int = 0,
    **kwargs: Any,
) -> LeaderboardReplayReport:
    """File-oriented alias for callers processing the captured JSONL."""

    return replay_leaderboard_episode(
        source,
        episode_index=episode_index,
        **kwargs,
    )


@dataclass(frozen=True, slots=True)
class LeaderboardReplayAdapter:
    """Reusable façade around :func:`replay_leaderboard_episode`.

    The façade is intentionally thin: it stores injectable catalog/runtime
    providers, while the reducer and report contract remain exactly the same
    as the function form.  It is convenient for a training job that replays
    many JSONL rows with one compiled catalog.
    """

    catalog: Plan2NativeProgramCatalog | None = None
    catalog_loader: CatalogLoader | None = None
    drink_runtime_loader: DrinkRuntimeLoader | None = None

    def replay(
        self,
        source: LeaderboardReplayEpisode | Mapping[str, Any] | str | Path,
        *,
        episode_index: int = 0,
        drink_runtime: Plan2NativeDrinkRuntime | None = None,
    ) -> LeaderboardReplayReport:
        return replay_leaderboard_episode(
            source,
            catalog=self.catalog,
            catalog_loader=self.catalog_loader,
            drink_runtime=drink_runtime,
            drink_runtime_loader=self.drink_runtime_loader,
            episode_index=episode_index,
        )

    run = replay

    def __call__(
        self,
        source: LeaderboardReplayEpisode | Mapping[str, Any] | str | Path,
        *,
        episode_index: int = 0,
        drink_runtime: Plan2NativeDrinkRuntime | None = None,
    ) -> LeaderboardReplayReport:
        return self.replay(
            source,
            episode_index=episode_index,
            drink_runtime=drink_runtime,
        )


# Short aliases keep the adapter discoverable for scripts without creating a
# second implementation or changing the existing leaderboard parser API.
build_diagnostic_plan2_initial_deck = build_plan2_master_initial_deck
load_leaderboard_episode = read_leaderboard_episode
replay_leaderboard_prefix = replay_leaderboard_episode
replay_leaderboard_episode_prefix = replay_leaderboard_episode
run_leaderboard_prefix_replay = replay_leaderboard_episode
LeaderboardReplayPrefixReport = LeaderboardReplayReport
LeaderboardReplayPrefixStep = LeaderboardReplayStep
Plan2LeaderboardReplayAdapter = LeaderboardReplayAdapter


__all__ = [
    "ADAPTER_SCHEMA",
    "DIAGNOSTIC_CHARACTER_DECK_ID",
    "DIAGNOSTIC_DEFAULT_DECK_ID",
    "LeaderboardReplayAdapter",
    "LeaderboardReplayReport",
    "LeaderboardReplayPrefixReport",
    "LeaderboardReplayStep",
    "LeaderboardReplayPrefixStep",
    "Plan2LeaderboardReplayAdapter",
    "build_diagnostic_plan2_initial_deck",
    "build_plan2_master_initial_deck",
    "load_leaderboard_episode",
    "read_leaderboard_episode",
    "replay_leaderboard_episode",
    "replay_leaderboard_episode_prefix",
    "replay_leaderboard_episode_file",
    "replay_leaderboard_prefix",
    "run_leaderboard_prefix_replay",
]
