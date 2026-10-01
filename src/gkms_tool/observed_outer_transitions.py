"""Source-bound outer transitions from existing settled host receipts.

This is a data adapter, not a controller or a future-outcome simulator. UI
selection/confirmation chains retain their observed prefixes; an unobserved
combinatorial action pool is never invented. Export qualification is separate
from player split, source compatibility, and shared-model representation gates.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib
import json
from typing import Mapping

from .runtime_economy_context import advance_economy_context

SCHEMA = "gkms.observed-outer-transition.v1"
OBJECTIVE_ID = "recorded-produce-rating-v1"
_MECHANICAL = {
    "effect.advance", "effect.confirm_card_change", "story.advance", "story.skip", "story.confirm_skip",
    "ui.navigation", "event.confirm_choice", "reward.open_group",
    "customize.open_options", "customize.back_to_cards", "customize.confirm_execute",
    "audition.continue_after_win", "audition.open_retry_result", "audition.retry_open",
    "audition.retry_confirm_free", "audition.retry_confirm_ticket",
}
_DIRECT = {
    "schedule.choose": "weekly-action", "event.choose": "event-option",
    "business.choose": "business-offer", "customize.select_card": "pt-card",
    "customize.select_option": "pt-option", "customize.finish": "pt-finish",
    "shop.select": "shop-product", "shop.open_product": "shop-product",
    "shop.finish": "shop-finish", "interval.select": "interval-product",
    "interval.open_product": "interval-product", "interval.finish": "interval-finish",
    "audition.choose": "audition-tier", "audition.retry_select": "audition-retry-tier",
    "reward.skip": "reward-skip", "reward.drink_capacity_open": "drink-discard-slot",
    # The recorded screen type and actual confirm/cancel candidates remain inputs.
    # A two-choice refresh confirmation is not silently swallowed as navigation.
    "ui.sheet_confirm": "confirmation-choice", "ui.sheet_cancel": "confirmation-choice",
    "shop.cancel_operation": "shop-cancel", "interval.cancel_operation": "interval-cancel",
    "reward.drink_capacity_cancel": "drink-retain-or-cancel",
    "reward.drink_capacity_confirm_skip": "drink-skip-or-retain",
    "reward.drink_capacity_cancel_skip": "drink-skip-or-retain",
    "card_choice.reveal": "card-realization-control",
}
_COMMIT_KIND = {
    "business.start": "business-offer", "reward.receive": "reward-",
    "card_choice.confirm": "card-", "drink_choice.confirm": "drink-retained-set",
    "shop.buy": "shop-product", "interval.execute": "interval-product",
    "audition.enter": "audition-tier",
}


def canonical_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def _mapping(value):
    return value if isinstance(value, Mapping) else {}


def _name(record):
    return record["receipt"]["request"]["target"]["action_id"]


def _scope(raw):
    state, progress = _mapping(raw.get("state")), _mapping(raw.get("progress"))
    return (state.get("produce_id"), progress.get("idolCardId"),
            state.get("week"), state.get("step_type"))


def _control_errors(receipt, generation):
    before = _mapping(receipt.get("before"))
    request, outcome = _mapping(receipt.get("request")), _mapping(receipt.get("outcome"))
    errors = []
    if request.get("command") != "outer.action":
        errors.append("not-an-outer-action")
    if request.get("expected_revision") != before.get("revision"):
        errors.append("request-before-revision-mismatch")
    if request.get("session_generation") != generation:
        errors.append("session-generation-mismatch")
    if outcome.get("status") != "settled" or not isinstance(outcome.get("after"), Mapping):
        errors.append("missing-settled-after-observation")
    if before.get("actions_complete") is not True or before.get("busy") is not False:
        errors.append("before-candidates-not-ready-and-complete")
    legal = before.get("legal_actions") or []
    if sum(_mapping(a).get("target") == request.get("target") for a in legal) != 1:
        errors.append("chosen-target-not-unique-in-recorded-legal-actions")
    return errors


def _boundary_kind(record, previous, records):
    """Classify observed actions; no ranking and no new legal action creation."""
    raw = record["receipt"]["before"]
    ui = _mapping(raw.get("ui_state"))
    name = _name(record)
    if name == "ui.navigation" and record["receipt"]["request"]["target"].get("button_id") == "schedule.refresh":
        return "weekly-rest"
    if name in _DIRECT:
        return _DIRECT[name]
    if name == "customize.confirm_finish":
        if previous and previous["kind"] == "pt-finish" and _scope(raw) == _scope(records[previous["position"]]["receipt"]["before"]):
            return None
        return "pt-finish-confirmation"
    if name in ("shop.confirm_operation", "shop.confirm_finish", "interval.confirm_operation", "interval.confirm_finish"):
        context = record.get("economy_before") or {}
        target = record["receipt"]["request"]["target"]
        scope = context.get("progress_scope") or {}
        bound = (context.get("submission_status") == "settled" and context.get("origin_receipt") and
                 context.get("session_generation") == record["receipt"]["request"].get("session_generation") and
                 target.get("parent_instance_id") == context.get("parent_instance_id") and
                 (scope.get("produce_id"), scope.get("idol_card_id"), scope.get("week"), scope.get("step_type")) == _scope(raw))
        if bound:
            return None
        return name.split(".")[0] + "-unbound-confirmation"
    if name == "reward.select":
        target = record["receipt"]["request"]["target"]
        rows = [x for x in ui.get("candidates", ()) if x.get("index") == target.get("index")]
        return "reward-" + str(rows[0].get("resource_type")) if len(rows) == 1 else "reward-unknown"
    if name == "card_choice.select":
        # Ordered/multiple selection prefixes belong to the same observed plan.
        if previous and previous["kind"].startswith("card-"):
            chain = records[previous["position"]:record["position"]]
            if not any(_name(x) == "card_choice.confirm" for x in chain) and _scope(raw) == _scope(chain[0]["receipt"]["before"]):
                return None
        return "card-" + str(ui.get("selection_type", "unknown"))
    if name == "drink_choice.toggle":
        if previous and previous["kind"] == "drink-retained-set":
            chain = records[previous["position"]:record["position"]]
            if not any(_name(x) in ("drink_choice.confirm", "drink_choice.confirm_skip_new") for x in chain):
                return None
        return "drink-retained-set"
    if name == "customize.execute":
        if previous and previous["kind"] == "pt-option":
            chain = records[previous["position"]:record["position"]]
            if not any(_name(x) == "customize.execute" for x in chain):
                return None
        # Another upgrade, even with the same id, has a new level/price/state.
        return "pt-option-already-selected"
    if name in _COMMIT_KIND:
        prefix = _COMMIT_KIND[name]
        if previous and previous["kind"].startswith(prefix):
            chain = records[previous["position"]:record["position"]]
            if not any(_name(x) == name for x in chain):
                return None
        return prefix.rstrip("-") + "-already-selected"
    if name in ("drink_choice.confirm_skip_new", "drink_choice.cancel_skip", "reward.confirm_skip"):
        return "drink-skip-or-retain"
    if name in ("reward.drink_capacity_trash", "reward.drink_capacity_confirm"):
        if previous and previous["kind"] == "drink-discard-slot":
            origin = records[previous["position"]]
            selected = origin["receipt"]["request"]["target"]
            target = record["receipt"]["request"]["target"]
            keys = ("parent_instance_id", "inventory_fingerprint", "owned_index", "drink_id",
                    "selected_reward_id", "selected_reward_index", "reward_pool_fingerprint", "limit_count")
            if (_name(origin) == "reward.drink_capacity_open" and
                    _scope(raw) == _scope(origin["receipt"]["before"]) and
                    all(key in selected and target.get(key) == selected[key] for key in keys)):
                return None
        return "drink-unbound-discard"
    if name in _MECHANICAL or name.startswith(("result.", "loadout.", "produce.")):
        return None
    return "unsupported-observed-strategy:" + name


def _payments(chain):
    payments = []
    for index, record in enumerate(chain):
        receipt = record["receipt"]
        before, after = receipt["before"], receipt["outcome"]["after"]
        target = receipt["request"]["target"]
        name = target["action_id"]
        if name in ("shop.confirm_operation", "interval.confirm_operation"):
            context = record.get("economy_before")
            quote = _mapping(context).get("quote")
            before_pt, after_pt = _mapping(before.get("state")).get("produce_points"), _mapping(after.get("state")).get("produce_points")
            parent_matches = bool(context and target.get("parent_instance_id") == context["parent_instance_id"])
            price = _mapping(quote).get("price")
            delta = after_pt - before_pt if type(before_pt) is int and type(after_pt) is int else None
            payments.append({"kind": "economy", "receipt": record["source"],
                             "origin_receipt": _mapping(context).get("origin_receipt"), "quote": deepcopy(quote),
                             "actual_wallet_delta": delta, "exact_price": price,
                             "verified": parent_matches and type(price) is int and delta == -price})
        elif name == "customize.confirm_execute":
            execute = next((x for x in reversed(chain[:index]) if _name(x) == "customize.execute"), None)
            options = []
            if execute:
                er = execute["receipt"]
                tid = er["request"]["target"].get("customize_id")
                options = [x for x in er["before"].get("ui_state", {}).get("customizes", ())
                           if x.get("customize_id") == tid and x.get("selected") is True]
            price = options[0].get("produce_points") if len(options) == 1 else None
            old, new = _mapping(before.get("state")).get("produce_points"), _mapping(after.get("state")).get("produce_points")
            delta = new - old if type(old) is int and type(new) is int else None
            payments.append({"kind": "pt-customization", "receipt": record["source"],
                             "execute_receipt": execute["source"] if execute else None,
                             "selected_option": deepcopy(options[0]) if len(options) == 1 else None,
                             "exact_price": price, "actual_wallet_delta": delta,
                             "ignored_legacy_modal_wallet": _mapping(before.get("ui_state")).get("produce_points"),
                             "verified": type(price) is int and delta == -price})
    return payments


def _attach_causal_history(rows):
    history = []
    for row in rows:
        row["observed_history"] = deepcopy(history)
        history.append({"run_step_ordinal": row["provenance"]["original_run_step_ordinal"],
                        "decision_kind": row["decision_kind"],
                        "state_before": deepcopy(row["observation"]["state"]),
                        "chosen_action": deepcopy(row["chosen_action"]),
                        "state_after": deepcopy(row["next_observation"]["state"]),
                        "completed_auditions": deepcopy(row["provenance"]["completed_auditions"])})


def completed_auditions_from_chain(chain):
    """Reuse exact pre-entry receipts; never substitute a result/current deck."""
    starts = {"audition.enter", "audition.retry_confirm_free", "audition.retry_confirm_ticket"}
    before_exam = None; completed = []
    for record in chain:
        if _name(record) in starts:
            before_exam = record
        if record["step"].get("page") != "audition_result" or _name(record) == "effect.advance":
            continue
        ui = _mapping(record["receipt"]["before"].get("ui_state"))
        if type(ui.get("score")) is not int: continue
        milestone = {"source_ref": record["source"], "json_pointer": "/before/ui_state",
                     **{key: ui.get(key) for key in ("step_type", "score", "is_win", "selected_number")},
                     "week": _mapping(record["receipt"]["before"].get("state")).get("week"),
                     "start_state": None, "start_state_source": None}
        if before_exam is not None:
            raw = before_exam["receipt"]["before"]
            state, cards = _mapping(raw.get("state")), _mapping(raw.get("collections")).get("cards")
            target = before_exam["receipt"]["request"]["target"]
            selected = [_mapping(x.get("identity")) for x in _mapping(raw.get("ui_state")).get("candidates", [])
                        if isinstance(x, Mapping) and x.get("selected") is True]
            result_raw = record["receipt"]["before"]
            result_state = _mapping(result_raw.get("state"))
            initial_entry = _name(before_exam) == "audition.enter"
            # The first entry screen still reports the preceding completed
            # week/step. Its actual selected native identity binds the coming
            # audition; retries remain on the already-entered audition week.
            expected_week = state.get("week")
            if type(expected_week) is int and initial_entry: expected_week += 1
            same = (_scope(raw)[:2] == _scope(result_raw)[:2] and
                    before_exam["receipt"]["request"].get("session_generation") == record["receipt"]["request"].get("session_generation") and
                    type(expected_week) is int and expected_week == result_state.get("week") and
                    len(selected) == 1 and selected[0].get("produce_id") == state.get("produce_id") and
                    selected[0].get("step_type") == ui.get("step_type") == result_state.get("step_type") and
                    selected[0].get("number") == ui.get("selected_number") == target.get("number" if initial_entry else "selected_number") and
                    (not initial_entry or target.get("step_type") == ui.get("step_type")))
            if same and isinstance(cards, list) and cards and all(type(state.get(key)) is int for key in ("vocal", "dance", "visual")):
                milestone["start_state"] = {"timing": "audition-start", "step_type": ui["step_type"],
                    **{key: state[key] for key in ("vocal", "dance", "visual")}, "cards": deepcopy(cards)}
                milestone["start_state_source"] = {"source_ref": before_exam["source"], "json_pointer": "/before",
                    "run_step_ordinal": before_exam["ordinal"], "authority": "settled-before-exam-entry-callback"}
        completed.append(milestone)
        before_exam = None
    return completed


def expand_observed_selector_transitions(rows):
    """Use actual settled selection prefixes as genuine one-control decisions.

    Intermediate observations and candidate sets are original receipts, not
    generated combinations. The observed terminal reward occurs exactly once
    in the original whole trajectory, even if its final macro is a selector.
    """
    expanded = []
    for row in rows:
        if row["action_form"] != "observed-selector-sequence":
            row = deepcopy(row)
            row["duration"] = len(row["provenance"]["mechanical_chain"])
            row["duration_units"] = "settled-outer-controls"
            expanded.append(row)
            continue
        prefixes = row["selection_prefixes"]
        for index, prefix in enumerate(prefixes):
            following = prefixes[index + 1] if index + 1 < len(prefixes) else None
            begin = prefix["run_step_ordinal"]
            end = following["run_step_ordinal"] if following else row["provenance"]["next_original_run_step_ordinal"]
            if end <= begin: raise ValueError("observed selector prefix order is not strictly causal")
            segment = [entry for entry in row["provenance"]["mechanical_chain"] if begin <= entry["run_step_ordinal"] < end]
            if not segment or segment[0]["source_ref"] != prefix["source_ref"]:
                raise ValueError("observed selector prefix has no matching actual control chain")
            value = deepcopy(row)
            value.update(transition_id=canonical_digest([row["provenance"]["trajectory_id"], prefix["request_id"]]),
                         observation=deepcopy(prefix["observation"]), candidates=deepcopy(prefix["legal_actions"]),
                         chosen_action=deepcopy(prefix["chosen_target"]), action_form="recorded-native-target",
                         selection_prefixes=[], duration=len(segment), duration_units="settled-outer-controls",
                         observed_sequence={"macro_transition_id": row["transition_id"], "prefix_index": index,
                                            "prefix_count": len(prefixes), "source_semantics": "original-settled-selector-prefix"})
            value["provenance"].update(before_ref=deepcopy(prefix["source_ref"]), original_run_step_ordinal=begin,
                                       next_original_run_step_ordinal=end, mechanical_chain=deepcopy(segment))
            value["provenance"]["history"]["steps_prefix_exclusive"] = begin
            value["qualification"]["pending"] = [x for x in value["qualification"]["pending"] if x != "selector-sequence-model-contract"]
            if following:
                value.update(next_observation=deepcopy(following["observation"]), next_decision_kind=row["decision_kind"], done=False, reward=0)
                value["provenance"].update(next_ref=deepcopy(following["source_ref"]), payments=[], completed_auditions=[], intervening_exam_segments=[])
            expanded.append(value)
    _attach_causal_history(expanded)
    return expanded


def observed_chain_acceptance(rows):
    """Check the one observed trajectory after actual selector-prefix expansion."""
    if not rows or len({r["transition_id"] for r in rows}) != len(rows):
        raise ValueError("observed transition identities are empty or repeated")
    if len({(r["provenance"]["account_scope"], r["provenance"]["trajectory_id"]) for r in rows}) != 1:
        raise ValueError("one original account and trajectory are required")
    if any(type(r.get("duration")) is not int or r["duration"] < 1 or
           r.get("duration_units") != "settled-outer-controls" or r["objective"]["gamma"] != 1 for r in rows):
        raise ValueError("positive actual control duration and undiscounted objective required")
    for first, second in zip(rows, rows[1:]):
        if (first["done"] or first["reward"] != 0 or first["next_observation"] != second["observation"] or
                first["provenance"]["next_ref"] != second["provenance"]["before_ref"] or
                first["provenance"]["next_original_run_step_ordinal"] != second["provenance"]["original_run_step_ordinal"]):
            raise ValueError("observed transition chain is discontinuous or rewards a nonterminal boundary")
    last = rows[-1]
    if last["done"] is not True or any(r["return_to_go"] != last["reward"] for r in rows):
        raise ValueError("exactly one recorded terminal rating must close the return chain")
    controls = [entry["run_step_ordinal"] for row in rows for entry in row["provenance"]["mechanical_chain"]]
    if controls != sorted(set(controls)) or sum(row["duration"] for row in rows) != len(controls):
        raise ValueError("actual control chain is repeated, reordered or has the wrong duration")
    return {"actual_prefix_chain_contiguous": True, "terminal_boundary_count": 1,
            "terminal_reward_sum": sum(row["reward"] for row in rows),
            "actual_settled_control_count": len(controls), "duration_units": "settled-outer-controls",
            "gamma": 1, "trajectory_count": 1, "account_scope_count": 1,
            "selector_macro_count": len({row["observed_sequence"]["macro_transition_id"]
                                         for row in rows if "observed_sequence" in row})}


def assemble_observed_outer_transitions(run, receipts, *, run_reference, receipt_references,
                                        objective_id=OBJECTIVE_ID, legacy_authorities=None):
    """Return observed rows and a coverage report, without assigning TRAIN.

    ``receipts`` is keyed by the explicit original run request ids. Callers must
    load/hash the original files, not search a latest/live receipt directory.
    The sparse objective gives zero to nonterminal macro boundaries by definition,
    and the *observed formal produceScore* to the terminal boundary, gamma=1.
    It does not claim those zeros were rewards emitted by the game server.
    """
    if objective_id != OBJECTIVE_ID:
        raise ValueError("unsupported observed outer objective")
    result, run_info = run["result"], run["run"]
    if result.get("status") != "completed":
        raise ValueError("an original completed run is required")
    generation = run_info["evidence"]["session_generation"]
    observed_preflights = [{"value": deepcopy(step.get("outcome", {}).get("decision", {}).get("outer_asset_preflight")),
                           "run_step_ordinal": ordinal}
                          for ordinal, step in enumerate(result["steps"])
                          if isinstance(step.get("outcome", {}).get("decision", {}).get("outer_asset_preflight"), Mapping)]
    records, gaps, seen_requests = [], [], set()
    context, account_evidence = None, None
    for ordinal, step in enumerate(result["steps"]):
        if step.get("page") == "exam":
            continue
        rid = _mapping(step.get("outcome")).get("request_id")
        if not rid:
            # Run setup/loadout summaries do not masquerade as native controls.
            if step.get("action") not in ("dll-loadout", "native-memory-auto-ready"):
                gaps.append({"ordinal": ordinal, "reason": "missing-request-id", "action": step.get("action")})
            continue
        if rid not in receipts or rid not in receipt_references:
            gaps.append({"ordinal": ordinal, "request_id": rid, "reason": "missing-original-receipt"})
            continue
        if rid in seen_requests:
            raise ValueError("duplicate original request id in run order")
        seen_requests.add(rid)
        receipt = receipts[rid]
        if _mapping(receipt.get("request")).get("request_id") != rid:
            raise ValueError("receipt request id differs from original run order")
        before = _mapping(receipt.get("before"))
        after = _mapping(_mapping(receipt.get("outcome")).get("after"))
        source = receipt_references[rid]
        prior = deepcopy(context)
        if isinstance(before.get("state"), Mapping) and isinstance(before.get("progress"), Mapping) and isinstance(after.get("state"), Mapping):
            advanced = advance_economy_context(context, receipt)
            if advanced is not None and advanced is not context and advanced.get("request_id") == rid:
                advanced = {**advanced, "origin_receipt": deepcopy(source)}
            context = advanced
        target = _mapping(receipt.get("request", {}).get("target"))
        auto = _mapping(_mapping(after.get("ui_state")).get("memory_auto"))
        if target.get("action_id") == "loadout.memory_auto_confirm" and all(auto.get(k) is True for k in ("owner_bound", "confirmed")) and auto.get("phase") == "succeeded":
            account_evidence = {"account_scope": auto.get("account_scope"), "source_ref": source,
                                "json_pointer": "/outcome/after/ui_state/memory_auto",
                                "produce_id": auto.get("produce_id"), "idol_card_id": auto.get("idol_card_id"),
                                "session_generation": receipt["request"].get("session_generation")}
        records.append({"position": len(records), "ordinal": ordinal, "step": step,
                        "receipt": receipt, "source": source, "economy_before": prior})
    active = [r for r in records if _mapping(r["receipt"]["before"].get("state")).get("in_progress") is True]
    if not active:
        raise ValueError("this trajectory has no observed active cultivation")
    active_start = active[0]["position"]
    # Home/preparation can retain a previous result, even for the same idol/mode.
    terminals = [r for r in records[active_start:] if _mapping(r["receipt"]["before"].get("progress")).get("status") == "ProduceProgressStatus_Finished"
                 and type(_mapping(r["receipt"]["before"].get("progress")).get("produceScore")) is int]
    if not terminals:
        raise ValueError("recorded formal produceScore is missing; cannot assign terminal reward")
    terminal = terminals[0]
    score = terminal["receipt"]["before"]["progress"]["produceScore"]
    if score < 0 or any(r["receipt"]["before"]["progress"]["produceScore"] != score for r in terminals):
        raise ValueError("terminal produceScore is invalid or inconsistent")
    recorded_material = None
    if legacy_authorities is not None:
        from .observed_outer_legacy_binding import bind_legacy_observed_run
        if account_evidence is not None:
            raise ValueError("legacy authority cannot replace an existing native account confirmation")
        account_evidence, recorded_material = bind_legacy_observed_run(run, records, active[0], terminal, legacy_authorities)
    if not account_evidence or not account_evidence["account_scope"] or account_evidence["session_generation"] != generation:
        raise ValueError("observed original account/trajectory binding is missing")
    boundaries = []
    for record in records[active_start:terminal["position"]]:
        kind = _boundary_kind(record, boundaries[-1] if boundaries else None, records)
        if kind is not None:
            boundaries.append({"position": record["position"], "kind": kind})
    rows = []
    for index, boundary in enumerate(boundaries):
        next_boundary = boundaries[index + 1] if index + 1 < len(boundaries) else None
        start = boundary["position"]
        end = next_boundary["position"] if next_boundary else terminal["position"]
        chain, next_record = records[start:end], records[end]
        first = chain[0]
        before = first["receipt"]["before"]
        errors = sorted({e for r in chain for e in _control_errors(r["receipt"], generation)})
        if any(r["step"].get("action") != _name(r) for r in chain):
            errors.append("run-action-and-original-receipt-target-differ")
        if any(first["ordinal"] <= g["ordinal"] < next_record["ordinal"] for g in gaps):
            errors.append("original-command-sequence-has-missing-receipt")
        exam_segments = []
        for ordinal in range(first["ordinal"], next_record["ordinal"]):
            step = result["steps"][ordinal]
            if step.get("page") == "exam":
                value = _mapping(step.get("outcome"))
                exam_segments.append({"source_ref": run_reference, "json_pointer": f"/result/steps/{ordinal}/outcome",
                                      "accepted": value.get("accepted"), "terminal": value.get("terminal")})
                if value.get("accepted") is not True or value.get("terminal") is not True:
                    errors.append("intervening-exam-not-complete")
        payments = _payments(chain)
        completed_auditions = completed_auditions_from_chain(chain)
        if any(p["verified"] is not True for p in payments):
            errors.append("payment-source-or-wallet-delta-not-verified")
        if boundary["kind"].startswith("unsupported-"):
            errors.append("unclassified-strategic-action")
        # A selected set is a recorded sequence, not an invented candidate set.
        choice_names = {"drink_choice.toggle", "card_choice.select", "reward.drink_capacity_trash"}
        prefixes = [{"source_ref": r["source"], "observation_pointer": "/before",
                     "run_step_ordinal": r["ordinal"], "request_id": r["receipt"]["request"]["request_id"],
                     "observation": deepcopy(r["receipt"]["before"]),
                     "legal_actions": deepcopy(r["receipt"]["before"]["legal_actions"]),
                     "chosen_target": deepcopy(r["receipt"]["request"]["target"])}
                    for r in chain if _name(r) in choice_names]
        commits = [r for r in chain if _name(r) in ("drink_choice.confirm", "card_choice.confirm", "reward.drink_capacity_confirm")]
        final_selection = deepcopy(commits[-1]["receipt"]["request"]["target"]) if commits else None
        done = next_boundary is None
        source_id = first["receipt"]["request"]["request_id"]
        rows.append({"schema": SCHEMA, "transition_id": canonical_digest([run_info["run_id"], source_id]),
                     "decision_type": "OUTER", "decision_kind": boundary["kind"],
                     "observation": deepcopy(before), "candidates": deepcopy(before.get("legal_actions", [])),
                     "chosen_action": deepcopy(first["receipt"]["request"]["target"]),
                     "action_form": "observed-selector-sequence" if len(prefixes) > 1 else "recorded-native-target",
                     "selection_prefixes": prefixes, "selection_commit": final_selection,
                     "next_observation": deepcopy(next_record["receipt"]["before"]),
                     "next_decision_kind": next_boundary["kind"] if next_boundary else "terminal-produce-rating",
                     "done": done, "reward": score if done else 0, "return_to_go": score,
                     "objective": {"id": OBJECTIVE_ID, "units": "raw-produce-rating-points", "normalization": None,
                                   "gamma": 1, "nonterminal_reward": "zero-by-explicit-sparse-objective-not-server-reward"},
                     "provenance": {"run_ref": deepcopy(run_reference), "before_ref": deepcopy(first["source"]),
                                    "next_ref": deepcopy(next_record["source"]), "original_run_step_ordinal": first["ordinal"],
                                    "next_original_run_step_ordinal": next_record["ordinal"],
                                    "mechanical_chain": [{"source_ref": r["source"], "action_id": _name(r),
                                                          "run_step_ordinal": r["ordinal"]} for r in chain],
                                    "intervening_exam_segments": exam_segments, "payments": payments,
                                    "completed_auditions": completed_auditions,
                                    "terminal_ref": terminal["source"], "terminal_pointer": "/before/progress/produceScore",
                                    "behavior_role": "observed-policy-behavior-not-expert", "teacher": False,
                                    "behavior_policies": sorted({str(_mapping(r["step"].get("outcome", {}).get("decision")).get("source", "unrecorded")) for r in chain}),
                                    "account_scope": account_evidence["account_scope"], "trajectory_id": run_info["run_id"],
                                    "session_generation": generation,
                                    "observed_asset_preflights": deepcopy(observed_preflights),
                                    "history": {"source_ref": run_reference, "steps_start_inclusive": records[active_start]["ordinal"],
                                                "steps_prefix_exclusive": first["ordinal"],
                                                "future_observations_and_labels_excluded": True}},
                     "qualification": {"observed_source_chain_complete": not errors, "errors": errors,
                                       "split": None, "training_qualified": False,
                                       "pending": ["whole-account-and-trajectory-split-comparison", "recorded-source-and-shared-encoder-compatibility",
                                                   "selector-sequence-model-contract"] if len(prefixes) > 1 else
                                                  ["whole-account-and-trajectory-split-comparison", "recorded-source-and-shared-encoder-compatibility"]}})
    return_chain_complete = bool(rows) and not gaps and all(r["qualification"]["observed_source_chain_complete"] for r in rows)
    macro_count = len(rows)
    for row in rows:
        row["qualification"]["observed_return_chain_complete"] = return_chain_complete
        if recorded_material is not None:
            row["provenance"]["recorded_material_binding"] = deepcopy(recorded_material)
    rows = expand_observed_selector_transitions(rows)
    chain_acceptance = observed_chain_acceptance(rows)
    settlements = []
    for record in records:
        ui = _mapping(record["receipt"]["before"].get("ui_state"))
        if record["step"].get("page") == "audition_result" and record["step"].get("action") != "effect.advance":
            settlements.append({"source_ref": record["source"], "json_pointer": "/before/ui_state",
                                **{key: ui.get(key) for key in ("step_type", "score", "is_win", "selected_number")}})
    return {"schema": "gkms.observed-outer-export.v1", "rows": rows,
            "report": {"status": "observed-transitions-exported-pending-training-qualification",
                       "source_run": run_reference, "run_id": run_info["run_id"],
                       "account_group_evidence": account_evidence, "objective_id": OBJECTIVE_ID,
                       "terminal_produce_score": score, "terminal_grade": None,
                       "terminal_grade_reason": "not-recorded-in-older-result-dto-no-score-to-grade-inference",
                       "receipt_count": len(records), "transition_count": len(rows),
                       "original_macro_count": macro_count,
                       "observed_selector_prefix_count": sum("observed_sequence" in row for row in rows),
                       "chain_acceptance": chain_acceptance,
                       "complete_source_chain_count": sum(r["qualification"]["observed_source_chain_complete"] for r in rows),
                       "observed_return_chain_complete": return_chain_complete,
                       "by_decision_kind": dict(Counter(r["decision_kind"] for r in rows)),
                       "errors": dict(Counter(e for r in rows for e in r["qualification"]["errors"])),
                       "missing_receipts": gaps, "audition_settlements": settlements,
                       "split_assigned": False, "training_qualified": False,
                       "new_game_read": False, "native_execution": False, "training": False}}
