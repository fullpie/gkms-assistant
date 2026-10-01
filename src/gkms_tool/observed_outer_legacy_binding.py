"""Original native identity witnesses for older completed Produce receipts.

These records predate the outer material preflight and memory-auto callback.
They are kept as a distinct proof kind, never rewritten into those callbacks.
The caller verifies the referenced bytes before this pure structural check.
"""
from collections.abc import Mapping
from datetime import datetime

SCHEMA = "gkms.observed-outer-legacy-authorities.v1"


def _require(value, message):
    if not value: raise ValueError(message)


def bind_legacy_observed_run(run, records, first_active, terminal, authorities):
    """Bind the exact selected equipment and every recorded native Master query."""
    _require(authorities.get("schema") == SCHEMA, "explicit older native identity authority required")
    generation = run["run"]["evidence"]["session_generation"]
    raw = first_active["receipt"]["before"]
    produce, idol = raw["state"]["produce_id"], raw["progress"]["idolCardId"]
    inventory_result = authorities["inventory_result"]["document"]
    applied = authorities["loadout_application_result"]["document"]
    inventory, loadout = inventory_result.get("inventory", {}), applied.get("loadout", {})
    _require(inventory_result.get("session_generation") == applied.get("session_generation") == generation,
             "original inventory/loadout generation differs from the completed run")
    _require(inventory_result.get("status") == "ok" and inventory.get("schema") == "gkms.account-inventory.v1"
             and inventory.get("source") == "dll-user-data-manager" and inventory.get("complete") is True,
             "complete original native inventory witness required")
    _require(applied.get("applied") is True and applied.get("full_loadout_applied") is True
             and set(applied.get("applied_sections", [])) == {"support", "memory"}
             and loadout.get("schema") == "gkms.account-loadout.v1",
             "original full loadout application witness required")
    _require(loadout.get("account_scope") == inventory.get("account_scope")
             and isinstance(loadout.get("account_scope"), str) and loadout["account_scope"].startswith("sha256:")
             and (loadout.get("produce_id"), loadout.get("idol_card_id")) == (produce, idol),
             "older native account/loadout scope differs from the active Produce")
    def timestamp(value): return datetime.fromisoformat(value.replace("Z", "+00:00"))
    _require(timestamp(inventory["captured_at"]) <= timestamp(loadout["captured_at"]) <= timestamp(raw["captured_at"]),
             "account/loadout witnesses must precede the actual active state")
    supports = sorted(raw["collections"]["support_cards"], key=lambda row: row["number"])
    memories = sorted(raw["collections"]["memories"], key=lambda row: row["number"])
    offered_supports = sorted(loadout["support_cards"], key=lambda row: row["position"])
    offered_memories = sorted(loadout["memories"], key=lambda row: row["position"])
    _require([row["number"] for row in supports] == list(range(1, 7))
             and [row["position"] for row in offered_supports] == list(range(6))
             and [(row["supportCardId"], row["level"]) for row in supports] ==
                 [(row["card_id"], row["level"]) for row in offered_supports],
             "all six source-selected support identities/levels must match first active state")
    _require([row["number"] for row in memories] == list(range(1, 5))
             and [row["position"] for row in offered_memories] == list(range(4))
             and [row["userMemoryId"] for row in memories] == [row["memory_id"] for row in offered_memories],
             "all four source-selected memory identities must match first active state")
    inventory_memories = {row.get("memory_id") for row in inventory.get("memories", [])}
    _require(all(row.get("is_rental") is False and row["memory_id"] in inventory_memories for row in offered_memories),
             "selected original memories lack the same account's owned inventory witness")
    source = authorities["runtime_master_source"]["document"]
    historical = authorities["historical_master_manifest"]["document"]
    master = source.get("master_hash")
    _require(isinstance(master, str) and historical.get("archived_master_hash") == master
             and historical.get("archived_master", {}).get("sha256") == source.get("archive", {}).get("sha256"),
             "older observed Master and source archive disagree")
    # A bridge generation alone is insufficient. The same active Produce must
    # continue without a mode/idol/week reset, and all actual native Master
    # observations through every completed exam must bind this exact source.
    weeks, groups, scope_records = [], set(), 0
    for record in records[first_active["position"]:terminal["position"] + 1]:
        _require(record["receipt"]["request"]["session_generation"] == generation,
                 "older Produce authority crosses a native generation")
        before = record["receipt"]["before"]
        state, progress = before.get("state") or {}, before.get("progress") or {}
        _require((state.get("produce_id"), progress.get("idolCardId")) == (produce, idol),
                 "older Produce authority crosses a mode or idol")
        _require(type(state.get("week")) is int, "older Produce week authority missing")
        weeks.append(state["week"])
        if progress.get("produceGroupId"): groups.add(progress["produceGroupId"])
        scope_records += 1
    _require(weeks == sorted(weeks) and len(groups) == 1, "older Produce progress identity reset or group changed")
    start = records[first_active["position"] - 1] if first_active["position"] else None
    preflight = ((start or {}).get("step", {}).get("outcome", {}).get("decision", {}).get("model_mode_route") or {})
    _require(start is not None and preflight.get("ready") is True
             and preflight.get("scope") == "before-AP-artifact-and-current-Master-check"
             and preflight.get("input_submitted") is False and preflight.get("cultivation_started") is False
             and start["receipt"]["before"].get("state", {}).get("in_progress") is not True
             and start["receipt"]["outcome"]["after"].get("state", {}).get("in_progress") is True,
             "actual Produce-start control must retain its before-AP native Master preflight")
    native_witnesses, exam_count, managers, packages = [], 0, set(), set()
    def verify_master(binding):
        current = binding.get("execution_master") or {}
        provenance = binding.get("source") or {}
        package = binding.get("runtime_package_binding") or {}
        _require(current.get("authority") == "native-existing-MasterManager" and current.get("ready") is True
                 and current.get("manager_count") == 1 and isinstance(current.get("manager_instance_id"), str)
                 and current.get("execution_master_version") == master
                 and current.get("master_tables_initialized") is True and current.get("master_update_succeeded") is True
                 and provenance.get("source_master_hash") == master
                 and provenance.get("receipt", {}).get("sha256") == authorities["runtime_master_source"]["reference"]["sha256"]
                 and package.get("source_master_hash") == master and package.get("produce_id") == produce
                 and package.get("source_receipt", {}).get("sha256") == authorities["runtime_master_source"]["reference"]["sha256"]
                 and isinstance(package.get("package_contract_sha256"), str) and isinstance(package.get("source_database", {}).get("sha256"), str),
                 "original native Master query differs from the older material authority")
        managers.add(current["manager_instance_id"])
        packages.add((package["package_contract_sha256"], package["source_database"]["sha256"]))
    verify_master(preflight.get("master_binding") or {})
    for ordinal, step in enumerate(run["result"]["steps"]):
        if step.get("page") != "exam": continue
        outcome = step.get("outcome") or {}
        _require(outcome.get("accepted") is True and outcome.get("terminal") is True,
                 "older recorded exam did not close its observed return interval")
        exam_count += 1
        observed = 0
        for inner, decision in enumerate(outcome.get("steps", [])):
            policy = decision.get("policy_source") or {}
            binding = policy.get("master_binding")
            if not isinstance(binding, Mapping): continue
            verify_master(binding)
            native_witnesses.append({"run_step_ordinal": ordinal, "decision_ordinal": inner,
                "json_pointer": f"/result/steps/{ordinal}/outcome/steps/{inner}/policy_source/master_binding"})
            observed += 1
        _require(observed > 0, "older completed exam has no actual native Master authority")
    _require(exam_count >= 3 and native_witnesses, "older whole-Produce Master observations are incomplete")
    _require(len(managers) == 1 and len(packages) == 1, "native Master manager/package changed within the observed Produce")
    account = {"kind": "native-full-loadout-confirmed-by-first-active-state",
        "account_scope": inventory["account_scope"], "session_generation": generation,
        "produce_id": produce, "idol_card_id": idol,
        "inventory_result": authorities["inventory_result"]["reference"],
        "loadout_application_result": authorities["loadout_application_result"]["reference"],
        "first_active_receipt": first_active["source"], "support_slots_matched": 6, "memory_slots_matched": 4,
        "submitted_result_not_mislabeled_settled": True,
        "settled_effect_authority": "exact complete equipment in subsequent first active native observation"}
    material = {"kind": "recorded-native-Master-in-continuous-Produce", "source_master_hash": master,
        "runtime_master_source": authorities["runtime_master_source"]["reference"], "archive": source["archive"],
        "historical_master_manifest": authorities["historical_master_manifest"]["reference"],
        "produce_id": produce, "idol_card_id": idol, "produce_group_id": next(iter(groups)),
        "session_generation": generation, "continuous_produce_observations": scope_records,
        "before_ap_run_step_ordinal": start["ordinal"], "before_ap_receipt": start["source"],
        "before_ap_json_pointer": f"/result/steps/{start['ordinal']}/outcome/decision/model_mode_route/master_binding",
        "native_manager_instance_id": next(iter(managers)),
        "runtime_package_contract_sha256": next(iter(packages))[0], "source_database_sha256": next(iter(packages))[1],
        "master_continuity_basis": "original before-AP source binding, uninterrupted Produce identity, identical native manager/version at every recorded model query",
        "completed_exam_count": exam_count, "native_master_witnesses": native_witnesses,
        "unrecorded_outer_preflight_claimed": False}
    return account, material
