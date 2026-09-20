"""Pure decisions over actual native reward/card/customize selector models.

No screenshot, click coordinates, candidate invention, or persistent UI state
machine. The selected target is returned verbatim from the native legal set.
Every permanent card mutation uses the shared complete-deck delta evaluator.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping

from .master_db import DEFAULT_DATABASE
from .deck_value import (DeckValueError, available_card_customizations, card_customizations,
                         context_from_native, evaluate_deck_delta, evaluate_drink_inventory_delta,
                         normalize_deck, with_card_customization)


class RuntimeCardChoiceError(ValueError):
    pass


def _paid_goal_progress(delta):
    """A paid choice follows the adopted replay's customization counts.

    Ordinary upgrade gaps do not enter this preference or the PT ledger.
    Native affordability/eligibility still precede the preference.
    """
    progress = getattr(delta, "plan_progress", None) or {}
    if (getattr(delta, "valuation", None) or {}).get("mutation_unresolved_effects"):
        return 0
    if progress.get("status") != "compared":
        return 0
    before, after = progress.get("before") or {}, progress.get("after") or {}
    return max(0, before.get("customize_steps", 0) - after.get("customize_steps", 0))


def _customization_rank(delta):
    return (_paid_goal_progress(delta), *delta.ranking_key)


def _integer(value: object, label: str, *, minimum=0) -> int:
    if type(value) is not int or value < minimum:
        raise RuntimeCardChoiceError(f"{label} must be an integer >= {minimum}")
    return value


def _models(state: Mapping[str, Any], name="candidates") -> tuple[Mapping[str, Any], ...]:
    values = state.get(name)
    if not isinstance(values, list) or any(not isinstance(value, Mapping) for value in values):
        raise RuntimeCardChoiceError(f"native {name} must be a complete array")
    indexes = [_integer(row.get("index"), "candidate index") for row in values]
    if len(set(indexes)) != len(indexes):
        raise RuntimeCardChoiceError("native candidate indexes are ambiguous")
    return tuple(values)


def _customization_available(row: Mapping[str, Any]) -> bool:
    """Respect the native per-Number v2 capacity without reinterpreting v1.

    V1 RemainCount is a page/new-card quota. V2's explicit availability already
    accounts for whether this exact card consumed that slot; it may therefore
    remain available at global zero. Missing v2 data never invents that right.
    """
    if (row.get("deleted") is True or row.get("lack_cost") is True
            or row.get("customize_locked") is True or row.get("can_customize") is False):
        return False
    if "customization_available" in row:
        available = row["customization_available"]
        if type(available) is not bool:
            raise RuntimeCardChoiceError("native customization_available must be boolean")
        if not available:
            return False
        return _integer(row.get("card_customizations_remaining"), "native card customizations remaining") > 0
    return row.get("remaining_count", 1) > 0


def _deck_index(deck, row):
    fingerprint = (row.get("card_id"), row.get("upgrade", 0), tuple(sorted(card_customizations(row))))
    def same(value):
        return (value["card_id"], value["upgrade"], tuple(sorted(card_customizations(value)))) == fingerprint
    number = row.get("deck_number")
    if number is not None:
        matches = [i for i, value in enumerate(deck) if value.get("deck_number") == number]
        if matches and not same(deck[matches[0]]):
            raise RuntimeCardChoiceError("native deck Number disagrees with its card/upgrade/customization fingerprint")
    else:
        matches = [i for i, value in enumerate(deck) if same(value)]
    if not matches:
        raise RuntimeCardChoiceError("native selected instance does not match the complete run deck")
    # Equal fingerprints are semantically identical copies. Choosing a
    # representative for pure arithmetic does not assign its Number to the
    # returned UI target, which remains the actual native selector instance.
    return matches[0]


def _project(deck, row, operation):
    after = [dict(value) for value in deck]
    if operation == "Add":
        added = dict(row)
    elif operation in {"Duplicate", "DuplicateUpgrade"}:
        added = dict(deck[_deck_index(deck, row)])
    elif operation in {"Upgrade", "Delete", "Change", "ChangeUpgrade"}:
        index = _deck_index(deck, row)
        if operation == "Upgrade":
            after[index]["upgrade"] += 1
            after[index]["upgradeCount"] = after[index]["upgrade"]
        else:
            after.pop(index)
        return after
    else:
        raise RuntimeCardChoiceError(f"permanent deck mutation semantics unavailable: {operation}")
    for key in ("number", "deck_number", "card_guid", "guid", "instance_key", "instance_id"):
        added.pop(key, None)
    if operation == "DuplicateUpgrade":
        added["upgrade"] += 1
        added["upgradeCount"] = added["upgrade"]
    quantity = _integer(row.get("quantity", 1), "reward card quantity", minimum=1) if operation == "Add" else 1
    for _ in range(quantity):
        copy = dict(added)
        copy["instance_id"] = f"proposed:selector-{row.get('index', 'offer')}-{len(after)}"
        after.append(copy)
    return after


def choose_runtime_card_ui_action(native, *, database: Path = DEFAULT_DATABASE, card_prior=None,
                                 allow_exam_continuation=False, preferred_card_guid: str | None = None,
                                 deck_context: Mapping[str, Any] | Callable[[], Mapping[str, Any]] | None = None,
                                 model_value=None):
    """Return (exact native target, reason), or None for a different UI family.

    Exam selectors require an explicit caller retaining the original pending
    main action. This function cannot grant that authority or submit a command.
    """
    raw = native.raw if hasattr(native, "raw") else native
    if not isinstance(raw, Mapping):
        raise RuntimeCardChoiceError("native selector snapshot must be an object")
    state = raw.get("ui_state", {})
    if not isinstance(state, Mapping) or state.get("family") not in {"reward", "reward_group", "reward-confirmation", "card-selector", "customize", "lesson-choice"}:
        return None
    if raw.get("busy") is not False or raw.get("actions_complete") is not True:
        raise RuntimeCardChoiceError("native selector is not ready")
    if raw.get("exam_continuation") is True and not allow_exam_continuation:
        raise RuntimeCardChoiceError("Exam selector must continue under its original pending main action")
    actions = raw.get("legal_actions")
    if not isinstance(actions, list) or any(not isinstance(a, Mapping) or not isinstance(a.get("target"), Mapping) for a in actions):
        raise RuntimeCardChoiceError("native legal selector targets are incomplete")

    def matching(name, **fields):
        matches = [a for a in actions if a.get("action_id") == name and all(a["target"].get(k) == v for k, v in fields.items())]
        if len(matches) > 1:
            raise RuntimeCardChoiceError(f"ambiguous native action {name}")
        return matches[0] if matches else None

    def choose(candidate, reason, **detail):
        if candidate is None:
            raise RuntimeCardChoiceError("desired native target is not currently actionable")
        return dict(candidate["target"]), {"source": "native-selector-deck-delta", "reason": reason, **detail}

    def waiting(reason, **detail):
        return None, {"source": "native-selector-deck-delta", "status": "waiting", "reason": reason, **detail}

    if state["family"] == "reward_group":
        candidate = matching("reward.open_group")
        if candidate is None:
            return waiting("等待目前禮物包裹的原生開啟按鈕就緒", source="native-reward-group")
        return choose(candidate, "開啟目前的禮物包裹，再依實際獎勵選卡", source="native-reward-group")

    context = context_from_native(raw)
    context_ready = False
    deck = None

    def ensure_context():
        nonlocal context_ready
        if not context_ready:
            value = deck_context() if callable(deck_context) else deck_context
            if value is not None:
                if not isinstance(value, Mapping):
                    raise RuntimeCardChoiceError("deck context resolver must return a mapping")
                context.update(value)
            context_ready = True
        return context

    def actual_deck():
        nonlocal deck
        if deck is None:
            collections = raw.get("collections")
            if not isinstance(collections, Mapping) or "cards" not in collections:
                raise RuntimeCardChoiceError("complete native collections.cards required for deck-aware choice")
            try:
                deck = normalize_deck(collections["cards"])
            except DeckValueError as error:
                raise RuntimeCardChoiceError(str(error)) from error
        return deck

    def score(row, operation, before=None):
        before = actual_deck() if before is None else before
        after = _project(before, row, operation)
        delta = evaluate_deck_delta(before, after, ensure_context(), database=database, model_value=model_value)
        return delta, after

    def detail(delta):
        return {"ranking_score": delta.value, "before_deck_value": delta.before_value,
                "after_deck_value": delta.after_value, "value_method": delta.method,
                "context_digest": delta.context_digest, "unscored_effects": list(delta.unscored_effects),
                "value_reasons": list(delta.reasons), "deck_plan_progress": delta.plan_progress,
                "ranking_order": list(_customization_rank(delta) if state["family"] == "customize" else delta.ranking_key),
                "effect_valuation": delta.valuation,
                **({"paid_customization_goal_steps": _paid_goal_progress(delta)} if state["family"] == "customize" else {})}

    def rank_mutations(entries):
        """Entries are (delta, native row, projected deck); no new target exists here."""
        complete = [entry for entry in entries if (entry[0].valuation or {}).get("comparison_ready", True)]
        if complete:
            return max(complete, key=lambda entry: (*entry[0].ranking_key, -entry[1]["index"])), None
        referenced = [entry for entry in entries if (entry[0].plan_progress or {}).get("status") == "compared"]
        if referenced:
            chosen = max(referenced, key=lambda entry: (float(entry[0].plan_progress["value"]), -entry[1]["index"]))
            return chosen, "required-unresolved-choice: adopted historical composition only; not complete utility"
        return min(entries, key=lambda entry: entry[1]["index"]), "required-unresolved-choice: native legal order; benefits unavailable"

    def customization_delta(row, option):
        before = actual_deck()
        after = list(before)
        index = _deck_index(before, row)
        after[index] = with_card_customization(before[index], option)
        return evaluate_deck_delta(before, after, {**ensure_context(), "produce_point_cost": option.get("produce_points", 0)},
                                   database=database, model_value=model_value)

    rows = _models(state)
    if state["family"] == "reward":
        from .runtime_reward_quantities import resolve_present_quantities
        try:
            rows = resolve_present_quantities(raw, rows)
        except ValueError as error:
            raise RuntimeCardChoiceError(str(error)) from error
    if state["family"] == "reward-confirmation":
        return choose(matching("reward.acknowledge_guide"), "acknowledge the existing native deck-guide setting without changing its checkbox")
    row_indexes = {row["index"]: row for row in rows}
    option_indexes = {row["index"]: row for row in _models(state, "customizes")} if state["family"] == "customize" else {}
    for candidate in actions:
        target = candidate["target"]
        if "index" not in target:
            continue
        pool = option_indexes if candidate["action_id"] == "customize.select_option" else row_indexes
        row = pool.get(target["index"])
        if row is None:
            raise RuntimeCardChoiceError("legal target index is outside its native candidate models")
        for key in ("card_id", "upgrade", "card_guid", "resource_id", "resource_type", "instance_key", "deck_number", "customize_id", "customize_count"):
            if key in target and key in row and target[key] != row[key]:
                raise RuntimeCardChoiceError(f"native legal target no longer binds candidate {key}")
    if state["family"] == "reward":
        if state.get("select_status") == 2 and matching("reward.confirm_skip") is not None:
            return choose(matching("reward.confirm_skip"), "confirm the native skip selection without changing the owned inventory")
        if len(rows) == 1:
            row = rows[0]
            if row.get("selected") is True and state.get("drink_capacity") is not None:
                from .runtime_reward_drink_capacity import choose_runtime_reward_drink_capacity_action
                capacity = choose_runtime_reward_drink_capacity_action(raw, deck_context=ensure_context(), database=database)
                if capacity is not None:
                    return capacity
            name = "reward.receive" if row.get("selected") is True else "reward.select"
            return choose(matching(name, index=row["index"]), "only native reward candidate; no comparative card valuation required")
        if all(row.get("resource_type") in (2, "ProduceResourceType_ProduceItem") for row in rows):
            from .runtime_pitem_reward import rank_native_pitem_reward
            progress = raw.get("progress")
            if not isinstance(progress, Mapping):
                raise RuntimeCardChoiceError("complete native progress required for owned P-items")
            owned = progress.get("produceItems", [])
            if not isinstance(owned, list) or any(not isinstance(value, Mapping) for value in owned):
                raise RuntimeCardChoiceError("native owned P-items must be an array")
            scored_items = []
            for row in rows:
                if row.get("selected") is not True and matching("reward.select", index=row["index"]) is None:
                    continue
                if _integer(row.get("quantity", 1), "P-item reward quantity", minimum=1) != 1:
                    raise RuntimeCardChoiceError("multiple same-ID passive item reward semantics are unavailable")
                score_value, metadata = rank_native_pitem_reward(row.get("resource_id"), actual_deck(), owned,
                                                                ensure_context(), database=database)
                scored_items.append((score_value, row, metadata))
            if not scored_items:
                raise RuntimeCardChoiceError("no native P-item reward is actionable")
            _, row, metadata = min(scored_items, key=lambda item: (-item[0], item[1]["index"]))
            name = "reward.receive" if row.get("selected") is True else "reward.select"
            return choose(matching(name, index=row["index"]), "Master P-item effects and remaining opportunities in the current deck/route context",
                          **metadata, inventory_kind="passive-item")
        if all(row.get("resource_type") in (3, "ProduceResourceType_ProduceDrink") for row in rows):
            progress = raw.get("progress")
            if not isinstance(progress, Mapping):
                raise RuntimeCardChoiceError("complete native progress required for current drink inventory")
            # This field is a repeated protobuf string: absent means empty in
            # the complete native progress object, not an unknown screen list.
            drinks = progress.get("produceDrinkIds", [])
            scored_drinks = []
            for row in rows:
                if row.get("selected") is not True and matching("reward.select", index=row["index"]) is None:
                    continue
                identity = row.get("resource_id")
                quantity = _integer(row.get("quantity", 1), "drink reward quantity", minimum=1)
                delta = evaluate_drink_inventory_delta(actual_deck(), drinks, [identity] * quantity,
                                                       ensure_context(), database=database)
                scored_drinks.append((delta, row))
            if not scored_drinks:
                raise RuntimeCardChoiceError("no native drink reward is actionable")
            complete_drinks = [item for item in scored_drinks if (item[0].valuation or {}).get("comparison_ready", True)]
            drink_fallback = None
            if complete_drinks:
                delta, row = min(complete_drinks, key=lambda item: (-item[0].value, item[1]["index"]))
            else:
                delta, row = min(scored_drinks, key=lambda item: item[1]["index"])
                drink_fallback = "required-unresolved-drink-choice: native legal order; benefits unavailable"
            if row.get("selected") is True and state.get("drink_capacity") is not None:
                from .runtime_reward_drink_capacity import choose_runtime_reward_drink_capacity_action
                capacity = choose_runtime_reward_drink_capacity_action(raw, deck_context=ensure_context(), database=database)
                if capacity is not None:
                    return capacity
            name = "reward.receive" if row.get("selected") is True else "reward.select"
            return choose(matching(name, index=row["index"]), "best marginal consumable portfolio for the actual deck and next exam",
                          **detail(delta), resource_id=row["resource_id"], inventory_kind="drink",
                          quantity=row.get("quantity", 1), quantity_source=row.get("quantity_source"),
                          valuation_fallback=drink_fallback)
        scored = []
        for row in rows:
            available = row.get("selected") is True or matching("reward.select", index=row["index"]) is not None
            if available and row.get("card_id"):
                delta, _ = score(row, "Add")
                scored.append((delta.value, row, delta))
        if not scored:
            if len(rows) == 1:
                row = rows[0]
                name = "reward.receive" if row.get("selected") is True else "reward.select"
                return choose(matching(name, index=row["index"]), "only native reward candidate")
            raise RuntimeCardChoiceError("non-card reward family needs its resource-specific policy")
        (delta, row, _), fallback = rank_mutations([(x[2], x[1], None) for x in scored])
        name = "reward.receive" if row.get("selected") is True else "reward.select"
        return choose(matching(name, index=row["index"]), "best complete-deck acquisition delta for the upcoming exam context",
                      **detail(delta), card_id=row["card_id"], valuation_fallback=fallback)

    if state["family"] == "card-selector":
        minimum = _integer(state.get("minimum"), "native minimum")
        maximum = _integer(state.get("maximum"), "native maximum")
        selected = {row["index"] for row in rows if row.get("selected") is True}
        if len(selected) != _integer(state.get("selected_count"), "native selected_count"):
            raise RuntimeCardChoiceError("native selected count is inconsistent")
        if maximum == 0:
            if state.get("valid_count") is True:
                return choose(matching("card_choice.confirm"), "native selector confirms its already valid selection")
            raise RuntimeCardChoiceError("native zero-maximum selector cannot be newly selected")
        if minimum > maximum:
            raise RuntimeCardChoiceError("native selection cardinality is inconsistent")
        if state.get("valid_count") is True and len(actions) == 1 and matching("card_choice.confirm"):
            return choose(matching("card_choice.confirm"), "only native confirmation remains and selection count is valid")
        eligible = [row for row in rows if row.get("selected") is True or (
            row.get("restricted") is not True and (matching("card_choice.select", index=row["index"]) is not None or
                                                       matching("card_choice.reveal", index=row["index"]) is not None))]
        if preferred_card_guid is not None:
            eligible = [row for row in eligible if row.get("card_guid") == preferred_card_guid]
            if len(eligible) != 1:
                raise RuntimeCardChoiceError("preferred Exam card GUID is not uniquely exposed by the actual selector")
        if raw.get("exam_continuation") is True:
            if preferred_card_guid is None or maximum != 1 or minimum > 1:
                raise RuntimeCardChoiceError("Exam continuation needs one caller-bound native policy GUID; outer deck value is not an exam action policy")
            row = eligible[0]
            if row["index"] in selected:
                return choose(matching("card_choice.confirm"), "confirm the parent exam policy's exact native card GUID")
            return choose(matching("card_choice.select", index=row["index"]) or matching("card_choice.reveal", index=row["index"]),
                          "parent exam policy's exact native card GUID", card_guid=preferred_card_guid)
        operation = str(state["selection_type"])
        scratch = actual_deck()
        scored = []
        # Marginal greedy joint selection, recomputed after each hypothetical
        # mutation. Equal IDs remain individual candidates and consume count.
        remaining = list(eligible)
        selection_fallback = None
        for _ in range(min(maximum, len(remaining))):
            ranked = [(result[0], row, result[1]) for row in remaining for result in (score(row, operation, scratch),)]
            (delta, row, after), fallback = rank_mutations(ranked)
            if len(scored) >= minimum and (delta.value <= 0 or fallback is not None):
                break
            selection_fallback = fallback or selection_fallback
            scored.append((delta, row))
            scratch = normalize_deck(after)
            remaining = [candidate for candidate in remaining if candidate["index"] != row["index"]]
        required = len(scored)
        if required < minimum:
            raise RuntimeCardChoiceError("not enough native eligible card instances")
        desired = {entry[1]["index"] for entry in scored}
        excess = sorted(selected - desired)
        if excess and (maximum > 1 or not desired):
            return choose(matching("card_choice.deselect", index=excess[0]), "retain only the ranked native instance set")
        for delta, row in scored:
            if row["index"] not in selected:
                candidate = matching("card_choice.select", index=row["index"]) or matching("card_choice.reveal", index=row["index"])
                reason = "joint complete-deck mutation delta; preserve each native instance and count"
                if operation in {"Change", "ChangeUpgrade"}:
                    reason += "; only known removal valued, replacement pool is not exposed"
                return choose(candidate, reason, **detail(delta), instance_key=row.get("instance_key"), required_count=required,
                              valuation_fallback=selection_fallback)
        return choose(matching("card_choice.confirm"), "native selection count and ranked instance set are satisfied")

    if state["family"] == "customize":
        if state.get("selecting_card") is True:
            eligible = [row for row in rows if _customization_available(row)
                        and (row.get("selected") is True or matching("customize.select_card", index=row["index"])
                             or matching("customize.reveal_card", index=row["index"]))]
            if not eligible:
                return choose(matching("customize.finish"), "no native customizable card remains")
            ensure_context()
            scored = []
            for row in eligible:
                instance = actual_deck()[_deck_index(actual_deck(), row)]
                options = available_card_customizations(instance, database=database,
                                                       **({"master_dir": context["master_dir"]} if "master_dir" in context else {}))
                for option in options:
                    wallet = context.get("produce_points", context.get("producePoint"))
                    if wallet is not None and option.get("produce_points", 0) > wallet:
                        continue
                    delta = customization_delta(row, option)
                    scored.append((delta.value, delta, row))
            if not scored:
                if matching("customize.finish"):
                    return choose(matching("customize.finish"), "no supported beneficial customization is available in the complete-deck context")
                raise RuntimeCardChoiceError("customization options are absent from Master; cannot rank unseen choices")
            value, delta, row = max(scored, key=lambda item: (*_customization_rank(item[1]), -item[2]["index"]))
            if value <= 0 and not _paid_goal_progress(delta):
                leave = matching("customize.finish") or matching("customize.back_to_cards")
                if leave is None:
                    return waiting("retain PT: waiting for the normal customization exit", **detail(delta))
                return choose(leave, "retain PT: no positive complete-deck customization delta", **detail(delta))
            if row.get("selected") is True:
                candidate = matching("customize.open_options", deck_number=row["deck_number"])
                if candidate is None:
                    return waiting("best deck instance is selected; waiting for its native options button",
                                   waiting_for="customize.open_options", deck_number=row["deck_number"], **detail(delta))
                return choose(candidate, "open actual options for selected deck instance")
            return choose(matching("customize.select_card", index=row["index"]) or matching("customize.reveal_card", index=row["index"]),
                          "best complete-deck customization delta among next concrete Master slots", **detail(delta), deck_number=row["deck_number"])
        if state.get("selecting_customize") is True:
            options = [row for row in _models(state, "customizes")
                       if row.get("enabled") is True or row.get("selected") is True or row.get("selection_noop") is True]
            if not options:
                return choose(matching("customize.back_to_cards") or matching("customize.finish"), "no enabled customization remains for this card")
            selected_card = state.get("selected_card")
            if not isinstance(selected_card, Mapping) or "deck_number" not in selected_card:
                selected_rows = [row for row in rows if row.get("selected") is True]
                if len(selected_rows) != 1:
                    raise RuntimeCardChoiceError("native customize selected card is not uniquely observed")
                selected_card = selected_rows[0]
            availability = next((card for card in rows
                                 if card.get("deck_number") == selected_card["deck_number"]), selected_card)
            if not _customization_available(availability):
                return choose(matching("customize.back_to_cards") or matching("customize.finish"),
                              "selected card has no remaining customization; return to the actual card pool",
                              deck_number=selected_card["deck_number"])
            wallet = ensure_context().get("produce_points", context.get("producePoint"))
            if wallet is not None:
                options = [row for row in options if row.get("produce_points", 0) <= wallet]
            if not options:
                leave = matching("customize.back_to_cards") or matching("customize.finish")
                return (choose(leave, "no offered customization fits the actual PT wallet") if leave else
                        waiting("waiting for normal exit from unaffordable customization options"))
            instance = actual_deck()[_deck_index(actual_deck(), selected_card)]
            current_counts = dict(card_customizations(instance))
            # The view may retain a consumed option and its cached/no-op ID
            # after the actual deck has advanced. It is not a next-level
            # proposal and must not be scored or sent through cache recovery.
            options = [row for row in options
                       if _integer(row.get("customize_count"), "offered customization count", minimum=1)
                       > current_counts.get(row.get("customize_id"), 0)]
            if not options:
                return choose(matching("customize.finish") or matching("customize.back_to_cards"),
                              "native deck already contains all offered customization levels")
            scored = [(customization_delta(selected_card, row), row) for row in options]
            delta, row = max(scored, key=lambda item: (*_customization_rank(item[0]), -item[1]["index"]))
            if delta.value <= 0 and not _paid_goal_progress(delta):
                leave = matching("customize.finish") or matching("customize.back_to_cards")
                if leave is None:
                    return waiting("retain PT: waiting for the normal customization exit", **detail(delta))
                return choose(leave, "retain PT: observed options do not improve complete-deck utility", **detail(delta))
            if row.get("selection_noop") is True and row.get("selected") is False:
                # CurrentCustomizeId can survive while OpenOptions clears the
                # actual cursor. Selecting that ID then returns immediately;
                # the game's normal Back callback is the supported cache reset.
                back = matching("customize.back_to_cards")
                if back is None:
                    return waiting("best option is only a stale native cached ID; waiting for the normal Back callback",
                                   waiting_for="customize.back_to_cards", deck_number=selected_card["deck_number"],
                                   customize_id=row["customize_id"], **detail(delta))
                return choose(back, "clear stale cached customization through the game's normal Back callback, then reopen the same best option",
                              recovery="native-customize-cached-id-without-cursor", deck_number=selected_card["deck_number"],
                              customize_id=row["customize_id"], **detail(delta))
            selected = (row.get("selected") is True if "selected" in row else
                        state.get("selected_customize_id") == row["customize_id"] and
                        state.get("selected_customize_count") == row["customize_count"])
            # V2 availability belongs to this Number, not a same-ID copy or an
            # unrelated selected-card summary. Its option target must bind it.
            selection_fields = {"index": row["index"]}
            if type(availability.get("customization_available")) is bool:
                selection_fields["deck_number"] = selected_card["deck_number"]
            candidate = (matching("customize.execute", deck_number=selected_card["deck_number"], customize_id=row["customize_id"])
                         if selected else matching("customize.select_option", **selection_fields))
            if candidate is None or (selected and state.get("execute_enabled") is False):
                return waiting("best option has no ready native execute/selection action; historical selected ID is not execution proof",
                               waiting_for="customize.execute" if selected else "customize.select_option",
                               deck_number=selected_card["deck_number"], customize_id=row["customize_id"],
                               customize_count=row["customize_count"], option_index=row["index"], **detail(delta))
            return choose(candidate, "actual selected-instance customization delta and PT opportunity cost",
                          **detail(delta), customize_id=row["customize_id"])
        raise RuntimeCardChoiceError("native customization is between interactive view states")

    # Some lesson/ADV choices expose no structured mechanical outcomes.
    # They can still be handled when the game itself offers only one choice.
    only = [action for action in actions if action.get("action_id") == "lesson_choice.choose"]
    if len(only) == 1:
        return choose(only[0], "only native lesson choice")
    raise RuntimeCardChoiceError("lesson choices need structured effect evidence; text is not a fabricated utility")
