"""Pure card-selector choices for one already-verified pending Exam owner.

The gateway verifies parent request/sequence/source before calling this module.
This module neither verifies a parent transaction nor submits any game input.
Prebound-GUID decisions keep their existing route; this policy owns only the
explicit no-prebound-GUID fallback requested by that same pending owner.
"""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import closing
import json
import math
from pathlib import Path
import sqlite3

from .master_db import DEFAULT_DATABASE


SECONDARY_POLICY_ID = "native-card-choice-value-v1"
SCHEMA = "gkms.native-exam-continuation-choice.v1"
_CARD_ACTIONS = {"card_choice.select", "card_choice.deselect", "card_choice.reveal", "card_choice.confirm"}
_DIRECT_DIRECTIONS = {"Upgrade": "upgrade", "Duplicate": "retain", "DuplicateUpgrade": "retain",
    "Add": "retain", "Delete": "discard", "Change": "discard", "ChangeUpgrade": "discard",
    "Grave": "discard", "Lost": "discard"}
_BENEFICIAL = {"upgrade", "retain", "retrieve", "hold"}


def _integer(value, label):
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")
    return value


def _object(value, label):
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def _raw_master(connection, identity, *, upgrade=None):
    if upgrade is None:
        row = connection.execute("SELECT raw_json FROM effect WHERE id=?", (identity,)).fetchone()
    else:
        row = connection.execute("SELECT raw_json FROM card WHERE id=? AND upgrade_count=?", (identity, upgrade)).fetchone()
    if row is None:
        raise ValueError(f"Master reference unavailable:{identity}")
    return _object(json.loads(row[0]), "Master row")


def _parent_direction(native, *, parent_card_id, parent_card_upgrade, parent_drink_id, parent_effect_id, database):
    """Resolve only a unique current Select effect, using the native search ID."""
    context = native.get("parent_context", native.get("ui_state", {}).get("parent_context", {}))
    context = context if isinstance(context, Mapping) else {}
    second = context.get("is_card_select2") is True
    search = context.get("card_search_id2" if second else "card_search_id") or None
    if not any((parent_card_id, parent_drink_id, parent_effect_id)):
        return None, {"parent_effect_resolution": "not-supplied"}
    try:
        with closing(sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            effect_ids = []
            if parent_card_id:
                if parent_card_upgrade is None:
                    raise ValueError("parent card effective upgrade is unavailable")
                card = _raw_master(connection, parent_card_id, upgrade=_integer(parent_card_upgrade, "parent upgrade"))
                effect_ids.extend(row["produceExamEffectId"] for row in card.get("playEffects", ()))
            elif parent_drink_id:
                # Reuse the existing complete drink link/catalog contract.
                from .drink_catalog import load_drink_catalog
                drink = load_drink_catalog(Path(database)).get_drink(parent_drink_id)
                effect_ids.extend(ref.link.produce_exam_effect_id for ref in drink.effect_refs
                                  if ref.link.produce_exam_effect_id)
            if parent_effect_id:
                if effect_ids and parent_effect_id not in effect_ids:
                    raise ValueError("parent effect is not in the supplied parent's direct Master effects")
                effect_ids = [parent_effect_id]
            matches = []
            for identity in effect_ids:
                effect = _raw_master(connection, identity)
                suffix = "2" if second else ""
                if effect.get("pickRangeType" + suffix) != "ProducePickRangeType_Select":
                    continue
                if search is not None and effect.get("produceCardSearchId" + suffix) != search:
                    continue
                kind = effect.get("effectType")
                if kind == "ProduceExamEffectType_ExamCardMove":
                    direction = {"ProduceCardMovePositionType_Hold": "hold", "ProduceCardMovePositionType_Hand": "retrieve",
                                 "ProduceCardMovePositionType_Grave": "discard", "ProduceCardMovePositionType_Lost": "discard"}.get(effect.get("movePositionType"))
                elif kind == "ProduceExamEffectType_ExamCardUpgrade":
                    direction = "upgrade"
                else:
                    direction = None
                matches.append((identity, direction))
            if len(matches) != 1 or matches[0][1] is None:
                raise ValueError("no unique supported parent Select-effect direction")
            return matches[0][1], {"parent_effect_resolution": "unique-Master-Select-effect",
                "parent_effect_id": matches[0][0], "native_card_search_id": search, "second_selection": second}
    except (KeyError, OSError, ValueError, TypeError, sqlite3.Error) as error:
        return None, {"parent_effect_resolution": "unresolved", "parent_effect_gap": str(error)}


def _rank(row, direction, database):
    """Weak native/official-card evaluation, never an Exam-score prediction."""
    native_value = row.get("evaluation")
    native_value = float(native_value) if type(native_value) in (int, float) and math.isfinite(native_value) else None
    delta = None
    if direction == "upgrade":
        try:
            from .logic_engine import load_master_card
            before = load_master_card(row["card_id"], row["upgrade"], Path(database))
            after = load_master_card(row["card_id"], row["upgrade"] + 1, Path(database))
            delta = after.evaluation - before.evaluation
        except (KeyError, OSError, ValueError, TypeError, sqlite3.Error):
            pass
    if direction == "unknown":
        key = (0, 0, 0, -row["index"])
        source = "native-order; direction-unresolved"
    elif direction == "upgrade" and delta is not None:
        key = (1, delta, native_value or 0, -row["index"])
        source = "Master-card-evaluation-upgrade-delta"
    else:
        sign = -1 if direction == "discard" else 1
        key = (0, sign * (native_value or 0), 0, -row["index"])
        source = "native-card-evaluation" if native_value is not None else "native-order; evaluation-unavailable"
    return key, {"index": row["index"], "card_guid": row.get("card_guid"), "card_id": row["card_id"],
                 "rank_source": source, "native_evaluation": native_value, "Master_evaluation_upgrade_delta": delta}


def choose_native_exam_continuation(native, *, parent_card_id=None, parent_card_upgrade=None,
                                   parent_drink_id=None, parent_effect_id=None, database=DEFAULT_DATABASE):
    """Return one original target or an explicit waiting/unavailable result.

    Input is the DLL card-selector frame after the gateway has verified the
    pending parent. Full native model rows define count/identity, and only its
    present legal targets can be returned. This function never polls or clicks.
    """
    raw = native.raw if hasattr(native, "raw") else native
    detail = {"schema": SCHEMA, "secondary_policy": SECONDARY_POLICY_ID,
              "source": "native-pending-exam-selector-rule", "owns_input": False,
              "optimality_claimed": False, "predicts_exam_score": False,
              "parent_verification_owner": "calling-gateway", "selection_rule": "native-count-and-direction"}

    def result(target, reason, *, status="ready", **extra):
        return (dict(target) if target is not None else None,
                {**detail, "status": status, "reason": reason, **extra})

    try:
        raw = _object(raw, "native selector")
        state = _object(raw.get("ui_state"), "native UI state")
        if raw.get("exam_continuation") is not True or state.get("family") != "card-selector":
            raise ValueError("secondary policy requires an Exam card-selector continuation")
        if raw.get("busy") is not False or raw.get("actions_complete") is not True:
            return result(None, "waiting for the current native selector boundary", status="waiting")
        minimum, maximum, count = (_integer(state.get(key), key) for key in ("minimum", "maximum", "selected_count"))
        if minimum > maximum or type(state.get("valid_count")) is not bool:
            raise ValueError("native selection count bounds/validity are inconsistent")
        rows = state.get("candidates")
        if not isinstance(rows, list):
            raise ValueError("native selector models must be complete")
        by_index, guids = {}, set()
        for row in rows:
            row = _object(row, "native candidate")
            index = _integer(row.get("index"), "candidate index")
            if index in by_index or not isinstance(row.get("card_id"), str) or not row["card_id"]:
                raise ValueError("native candidate index/card identity is unavailable or duplicated")
            _integer(row.get("upgrade"), "native candidate upgrade")
            if type(row.get("selected")) is not bool or type(row.get("restricted")) is not bool:
                raise ValueError("native selected/restricted flags are unavailable")
            guid = row.get("card_guid")
            if guid is not None and (not isinstance(guid, str) or not guid or guid in guids):
                raise ValueError("native candidate GUID is invalid or duplicated")
            if guid is not None:
                guids.add(guid)
            by_index[index] = row
        selected = {index for index, row in by_index.items() if row["selected"]}
        if len(selected) != count or count > maximum or (state["valid_count"] and not minimum <= count <= maximum):
            raise ValueError("native selected models disagree with their declared count")
        actions = raw.get("legal_actions")
        if not isinstance(actions, list):
            raise ValueError("native legal selector targets are unavailable")
        bound = {}
        parent = raw.get("parent_context", state.get("parent_context"))
        for action in actions:
            action = _object(action, "native action")
            target = _object(action.get("target"), "native target")
            name = action.get("action_id")
            if name not in _CARD_ACTIONS or target.get("action_id") != name:
                raise ValueError("unexpected or inconsistent selector action family")
            if "parent_context" in target and parent is not None and target["parent_context"] != parent:
                raise ValueError("native target belongs to another parent context")
            index = None if name == "card_choice.confirm" else _integer(target.get("index"), "target index")
            if (name, index) in bound:
                raise ValueError("native selector target is ambiguous")
            if name == "card_choice.confirm":
                if not state["valid_count"] or not minimum <= count <= maximum or target.get("selected_count", count) != count:
                    raise ValueError("native confirmation does not bind a valid current count")
                if "confirm_enabled" in state and state["confirm_enabled"] is not True:
                    raise ValueError("native confirm target contradicts the observed enabled state")
            else:
                row = by_index.get(index)
                if row is None:
                    raise ValueError("native action index is outside current candidate models")
                for key in ("card_id", "upgrade", "card_guid", "instance_key"):
                    if key in target and target[key] != row.get(key):
                        raise ValueError("native target identity differs from its current row:" + key)
                if "selected_before" in target and target["selected_before"] is not row["selected"]:
                    raise ValueError("native target selected state is stale")
                if name == "card_choice.select" and (row["selected"] or row["restricted"]):
                    raise ValueError("native select target contradicts selected/restricted state")
                if name == "card_choice.deselect" and (not row["selected"] or row["restricted"] or maximum <= 1):
                    raise ValueError("native deselect target contradicts its current model")
            bound[(name, index)] = target
        if count == maximum and state["valid_count"]:
            confirm = bound.get(("card_choice.confirm", None))
            return result(confirm, "confirm the observed valid native selection" if confirm else "waiting for native confirmation",
                          status="ready" if confirm else "waiting", minimum=minimum, maximum=maximum,
                          selected_count=count, desired_count=count, direction="not-needed")
        parent_direction, source = _parent_direction(raw, parent_card_id=parent_card_id,
            parent_card_upgrade=parent_card_upgrade, parent_drink_id=parent_drink_id,
            parent_effect_id=parent_effect_id, database=database)
        kind = state.get("selection_type")
        # Hand/Move are generic in some Exam presenters. A unique current
        # parent Select effect gives their destination without guessing GUIDs.
        direction = parent_direction if parent_direction is not None and kind in {"Hand", "Move", "None"} else _DIRECT_DIRECTIONS.get(kind)
        direction = direction or parent_direction or "unknown"
        detail.update(selection_type=kind, direction=direction, minimum=minimum, maximum=maximum,
                      selected_count=count, direction_evidence=source,
                      direction_source=("parent-Master-Select-effect" if parent_direction is not None and direction == parent_direction
                                        else "native-selection-type" if direction != "unknown" else "unresolved"))
        candidates = [row for row in rows if row["selected"] or not row["restricted"]]
        if len(candidates) < minimum:
            raise ValueError("native minimum cannot be satisfied by the current eligible models")
        desired_count = min(maximum, len(candidates)) if direction in _BENEFICIAL else max(minimum, count)
        detail["desired_count"] = desired_count
        confirm = bound.get(("card_choice.confirm", None))
        if count >= desired_count and minimum <= count <= maximum:
            return result(confirm, "confirm the observed valid native selection" if confirm else "waiting for native confirmation",
                          status="ready" if confirm else "waiting")
        ranked = sorted((_rank(row, direction, database) for row in candidates if not row["selected"]),
                        key=lambda item: item[0], reverse=True)
        detail["rankings"] = [value for _, value in ranked]
        # Preserve already chosen cards. Fresh calls add at most one more
        # actual index until the desired native cardinality is satisfied.
        for _, value in ranked:
            index = value["index"]
            target = bound.get(("card_choice.select", index)) or bound.get(("card_choice.reveal", index))
            if target is not None:
                reason = "choose an actual candidate by the explicit direction rule"
                if direction == "unknown":
                    reason = "direction unresolved; choose the next native candidate in order without an optimality claim"
                return result(target, reason, selected_index=index, selected_card_guid=by_index[index].get("card_guid"))
        if minimum <= count <= maximum and confirm is not None:
            return result(confirm, "confirm current valid count; no additional candidate is currently selectable")
        return result(None, "waiting for a selectable native candidate within the declared count", status="waiting")
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as error:
        return result(None, str(error), status="unavailable")
