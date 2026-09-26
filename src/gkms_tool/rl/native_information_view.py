"""Small information boundary over existing, source-checked native semantics.

The caller validates raw observations and splits cards, drinks and statuses by
owner BEFORE calling these helpers. This module does not serialize native state,
resolve references, reinterpret effects, change BC tokens or admit training data.

Reviewed sources: scripts/pc_exam_state.py::_direct_state/PARAMETER_GETTERS;
active_state_features.py::_ROOT_SCALARS; native_structure_features.py::_emit,
_card/_grow/_native_links; integrated_exam_bc_features.py::_command_tokens.
Raw queues/history remain in the source journal. Omitting them from this first
learned view is a scoped limitation, not a claim of a complete Markov state.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
import math
import re
from types import MappingProxyType

from .contracts import ContractError

VERSION = "gkms.rl.native-information-view.v1"

_CURRENT_NUMBERS = (
    "examType", "lessonLimitUpScore", "examExtraTurn", "produceDrinkPossessLimit",
    "stamina", "maxStamina", "producePoint", "stepType", "mainEffectType",
    "displayMainEffectType", "planType", "clearBorder", "originClearBorder",
    "limitBorder", "phase", "currentTurn", "limitTurn", "remainTurn", "extraTurn",
    "parameter", "block", "turnCardPlayCount", "examCardPlayCount",
    "currentTurnTotalConsumeStamina", "currentTurnTotalAddParameter", "currentTurnTotalBlock",
    "totalConsumeStamina", "fullPowerPointGetSumCount", "blockConsumptionSumCount",
    "reviewConsumptionSumCount", "stanceChangeCount", "stanceFullPowerChangeCount",
    "stanceConcentrationChangeCount", "stancePreservationChangeCount", "totalDrawCardCount",
    "produceTargetParameter", "vocalBonusPermil", "danceBonusPermil", "visualBonusPermil",
    "vocalConfigParameter", "danceConfigParameter", "visualConfigParameter",
    "parameterVocal", "parameterDance", "parameterVisual", "npcScoreMultiplePermil",
    "competitionSectionIndex",
)
_CURRENT_BOOLEANS = (
    "isTurnCardPlayEnd", "isTurnCardGrave", "isTurnCardLost", "isExamEndComplete", "isStaticNpcScore",
)
_ZONES = ("handList", "deckList", "graveList", "lostList", "holdList", "drinkList")
_NPC_NAMES = ("npc_current_count", "npc_current_score_sum", "npc_current_score_max")
PUBLIC_ROOT_NAMES = (*_CURRENT_NUMBERS, *_CURRENT_BOOLEANS,
                     *(zone + "_count" for zone in _ZONES), *_NPC_NAMES)

# Explicit account of the reviewed PC snapshot roots. The caller owns semantic
# entity construction for the retained collections; this is not a blanket grant
# to feed their original indices, reference numbers or native copies to a model.
_ROOT_POLICY = {name: "current-public-scalar" for name in (*_CURRENT_NUMBERS, *_CURRENT_BOOLEANS)}
_ROOT_POLICY.update({name: "split-by-owner-semantic-entities; count-is-public; raw-order-is-not-a-feature"
                     for name in _ZONES})
_ROOT_POLICY.update({name: "public-definition-context; not-a-numeric-root-feature"
                     for name in ("settingId", "characterId", "produceId", "idolCardId")})
_ROOT_POLICY.update({name: "split-by-owner-public-definition-and-current-runtime"
                     for name in ("itemList", "supportCardList", "gimmickList")})
_ROOT_POLICY["playingCard"] = "current-secondary-parent-via-checked-command; redundant-root-copy-excluded"
_ROOT_POLICY.update({name: "exclude-initial-learned-view; retained-raw-internal-queue-or-history; not-full-Markov"
                     for name in ("commandList", "playLogList", "userPlayLogList", "turnLogList",
                                  "currentTurnHandCardList", "currentTurnHoldCardList",
                                  "currentTurnStartStatusList", "currentTurnStartStatusEffectDataList",
                                  "removedCardList")})
_ROOT_POLICY.update({
    "random": "exclude-hidden-generator-state",
    "futureDeckList": "exclude-hidden-deck-order",
    "pastDeckList": "exclude-internal-deck-history; retained-in-raw",
    "references": "local-reference-resolution-only; never-learn-rid",
    "status": "resolve-and-split-active-status-owners; preserve-typed-values-and-trigger-order",
    "npcDataList": "only-observed-currentScore; exclude-full-scoreList-and-future-category-totals",
    "turnStatusParameterTypeList": "public-turn-parameter-schedule; caller-encodes-semantic-sequence",
    "turnUseSupportCardIdList": "public-current-turn-definition-history; caller-scoped-encoding",
    "currentTurnTriggeredStatusEnchantIdList": "public-current-turn-trigger-history; caller-scoped-encoding",
    "planIgnoreProduceCardWhiteList": "definition-rule-context; caller-scoped-encoding",
    "competitionIdolDataList": "exclude-initial-NIA-view; requires-separate-competition-scope",
    "drawCardGuidList": "local-card-history-binding-only",
    "idolCardSkinId": "exclude-presentation-only",
    "isReplay": "exclude-execution-mode-provenance",
})
ROOT_FIELD_POLICY = MappingProxyType(_ROOT_POLICY)


def _number(value, name):
    if value is None:
        return None
    if type(value) not in (int, float):
        raise ContractError(name + ": observed finite number required")
    try:
        valid = math.isfinite(value)
    except OverflowError:
        valid = False
    if not valid:
        raise ContractError(name + ": observed finite number required")
    return value


def public_current_npc_scores(state: Mapping) -> tuple[int | float | None, ...] | None:
    """Read current scores only, never the precomputed per-turn score list."""
    if not isinstance(state, Mapping):
        raise ContractError("native state mapping required")
    rows = state.get("npcDataList")
    if rows is None:
        return None
    if not isinstance(rows, (list, tuple)):
        raise ContractError("npcDataList must be an observed collection or null")
    result = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ContractError("native NPC row mapping required")
        result.append(_number(row.get("_currentScore"), "NPC current score"))
    return tuple(result)


def public_root_values(state: Mapping) -> dict[str, int | float | bool | None]:
    """Fixed named globals; missing/null stays unknown and observed empty is zero."""
    if not isinstance(state, Mapping):
        raise ContractError("native state mapping required")
    result = {name: _number(state.get(name), name) for name in _CURRENT_NUMBERS}
    for name in _CURRENT_BOOLEANS:
        value = state.get(name)
        if value is not None and type(value) is not bool:
            raise ContractError(name + ": observed boolean required")
        result[name] = value
    for zone in _ZONES:
        rows = state.get(zone)
        if rows is not None and not isinstance(rows, (list, tuple)):
            raise ContractError(zone + ": observed collection or null required")
        result[zone + "_count"] = None if rows is None else len(rows)
    scores = public_current_npc_scores(state)
    complete = scores is not None and all(value is not None for value in scores)
    result["npc_current_count"] = None if scores is None else len(scores)
    result["npc_current_score_sum"] = _number(sum(scores), "NPC score sum") if complete else None
    result["npc_current_score_max"] = max(scores) if complete and scores else None
    return result


# Host _emit uses path:int/str/... and number_tokens additionally emits
# native-magnitude:n:path:sign/coarse/log2. active_state_features removes a
# leading underscore from nested field names. Match the parsed FIELD PATH,
# including these aliases, never arbitrary card/effect definition-ID contents.
def _field_name(value):
    return re.sub(r"\[\d+\]", "", value).replace("_", "").casefold()


_PRIVATE_FIELDS = frozenset(_field_name(name) for name in (
    "random", "random_raw", "futureDeckList", "pastDeckList", "_fixedDeckOrder", "fixed_deck_order",
    "_guid", "card_guid", "_uid", "rid", "_statusUid", "_sourceUid", "_ownerUid",
    "owned_object_identity", "position_object_identity", "instance_key", "pointer_binding",
    "simulator_identity", "sequence_identity", "parameter_identity", "play_log_identity",
    "command_identity", "candidate_list_identity", "effect_context_identity",
    "engine_identity", "worker_pid", "session_generation", "source_report_sha256", "source_journal_sha256",
    "observed_terminal_score", "policy_source", "teacher_action", "teacher_action_present",
    "_affectGrowEffectIdList", "affected_grow_effect_ids", "lineage", "_fromExamEffectIdList", "_fromExamEffectId",
    "_startEnchantOriginId", "_startEnchantOwnerId",
    # These locate the executor target/response, not an effect-sequence element.
    "_playIndex", "_selectIndex", "_cardGuids", "_useEffectUidList", "_enchantEffectUid",
    "offered_ordinal", "native_zone_index", "slot_index", "occurrence_index", "selected_ui_index",
    "_assetId", "_produceCardSkinId", "_produceCardSkinAssetId",
    "commandList", "playLogList", "userPlayLogList", "turnLogList", "npcDataList", "_scoreList",
    "currentTurnHandCardList", "currentTurnHoldCardList", "currentTurnStartStatusList",
    "currentTurnStartStatusEffectDataList", "removedCardList", "drawCardGuidList", "isReplay",
))
_MATERIALIZED_OWNERS = frozenset(("materialized", "groweffectexamstartafterlist", "playeffect", "effectlist"))
_WRAPPERS = ("native-magnitude:", "active-v2:", "semantic:")
_VALUE_KINDS = frozenset(("n", "s", "b", "missing", "object", "count", "empty", "class", "source"))
_BUNDLED_OWNER = re.compile(r"(?:^|[:.])(?:native\.zone\.[A-Za-z_][A-Za-z_0-9]*|joint\.secondary\.(?:offered|selected_prefix))\[\d+\]")
_OPAQUE_ID_SUFFIX = re.compile(r"(?:uid|guid)(?:list|s)?$")
_PARENT_CARD_WRAPPER = "joint.secondary.parent._playingCard:"
_HOST_TOKEN_STARTS = ("native.", "native-magnitude:", "semantic:", "active-v2:")


def _token_path(token):
    while True:
        wrapper = next((prefix for prefix in _WRAPPERS if token.startswith(prefix)), None)
        if wrapper is None:
            break
        token = token[len(wrapper):]
    head, separator, rest = token.partition(":")
    if separator and head in _VALUE_KINDS:
        token = rest
    return token.partition(":")[0]


def _token_paths(token):
    # _command_tokens prefixes each _card token with this exact owner marker.
    # It is distinct from _emit's owner:str:<arbitrary text> grammar: string
    # values and definition-ID contents must never be reparsed as field paths.
    if token.startswith(_PARENT_CARD_WRAPPER):
        inner = token[len(_PARENT_CARD_WRAPPER):]
        if inner.startswith(_HOST_TOKEN_STARTS):
            return (_PARENT_CARD_WRAPPER[:-1], _token_path(inner))
    return (_token_path(token),)


def public_semantic_tokens(tokens: Iterable[str]) -> tuple[str, ...]:
    """Filter one owner's checked host tokens, preserving order and multiplicity.

    PT option indices, materialized-value indices, trigger order, effect-chain
    indices, _originEffectIndex and _judgeTargetIndex retain semantic meaning.
    Whole zone/offered bundles are rejected rather than flattening their owners
    into a bag. Definitions keep their IDs; materialized _id and lineage are
    witnesses only. Mandatory host gaps remain the caller's admission gate.
    """
    if isinstance(tokens, (str, bytes)) or not isinstance(tokens, Iterable):
        raise ContractError("an iterable of individual semantic tokens is required")
    result = []
    for token in tokens:
        if type(token) is not str or not token:
            raise ContractError("semantic tokens must be nonempty strings")
        paths = _token_paths(token)
        if any(_BUNDLED_OWNER.search(path) for path in paths):
            raise ContractError("split native zone/offered tokens by owner before information filtering")
        parts_by_path = tuple(tuple(_field_name(part) for part in re.split(r"[./]", path))
                              for path in paths)
        if any(part in _PRIVATE_FIELDS or _OPAQUE_ID_SUFFIX.search(part)
               for parts in parts_by_path for part in parts):
            continue
        if any(len(parts) >= 2 and parts[-1] == "id" and parts[-2] in _MATERIALIZED_OWNERS
               and "definition" not in parts for parts in parts_by_path):
            continue
        result.append(token)
    return tuple(result)


__all__ = ["VERSION", "PUBLIC_ROOT_NAMES", "ROOT_FIELD_POLICY", "public_root_values",
           "public_current_npc_scores", "public_semantic_tokens"]
