"""Price the unpaid customization goals of the same adopted replay deck.

Upgrade levels guide free card-selection rewards but do not prove a paid
purchase. Only missing customization ID/count steps receive a Master PT quote.
Missing cards and ordinary-upgrade prerequisites remain explicitly conditional.
"""
from __future__ import annotations

import sqlite3

from .deck_plan import DeckPlan, DeckPlanProgress, match_plan_requirements
from .deck_value import available_card_customizations, with_card_customization
from .master_db import DEFAULT_DATABASE
from .passive_catalog import DEFAULT_MASTER_DIR

SCHEMA = "gkms.deck-plan-customization-cost.v1"


def quote_plan_customizations(deck, context, *, stage=None, database=DEFAULT_DATABASE,
                              master_dir=DEFAULT_MASTER_DIR):
    result = {"schema": SCHEMA, "status": "no-plan", "plan_id": None, "stage": None, "requested_stage": stage,
              "requirements": [], "unpriced": [], "known_pt_total": 0,
              "owned_card_pt_total": 0, "conditional_card_pt_total": 0, "total_pt": None,
              "ordinary_upgrades_priced": False, "historical_spend_reconstructed": False,
              "native_option_legality_verified": False}
    if context.get("deck_plan") is None:
        return result
    try:
        plan = DeckPlan.from_dict(context["deck_plan"])
        if (plan.scope != tuple(context.get(key) for key in ("produce_id", "plan_type", "exam_effect_type"))
                or plan.idol_card_id != context.get("idol_card_id")):
            raise ValueError("adopted-plan-context-mismatch")
        matching = match_plan_requirements(deck, plan, stage=stage)
        if matching.gap is None:
            raise ValueError(matching.reason)
        progress = DeckPlanProgress(plan.plan_id, matching.gap, matching.gap, matching.reason, matching.target_stage)
        result.update(plan_id=plan.plan_id, stage=progress.target_stage, progress=progress.to_dict(),
                      ordinary_upgrade_steps=progress.before.upgrade_steps)
        gap = progress.before.to_dict()
        for requirement in gap["details"]:
            if not requirement["missing_customizes"]:
                continue
            target, observed = requirement["target"], requirement["observed"]
            # This is a quote carrier, never an asserted owned/upgraded card.
            carrier = {"card_id": target["card_id"],
                       "upgrade": max(target["upgrade_count"], (observed or {}).get("upgrade_count", 0)),
                       "customizes": list((observed or {}).get("customizes", ())) }
            quoted = {**requirement, "card_id": target["card_id"], "operations": [], "known_pt": 0,
                      "requires_acquisition": observed is None,
                      "requires_normal_upgrade": requirement["upgrade_steps"] > 0,
                      "ordinary_upgrade_pt": None}
            for identity, count in sorted(requirement["missing_customizes"].items()):
                for _ in range(count):
                    try:
                        available = available_card_customizations(carrier, database=database, master_dir=master_dir)
                        matches = [row for row in available if row["customize_id"] == identity]
                        if len(matches) != 1:
                            raise ValueError("no unique next Master customization level")
                        option = matches[0]
                        price = option.get("produce_points")
                        if type(price) is not int or price < 0:
                            raise ValueError("customization price is not a nonnegative integer")
                        quoted["operations"].append({"card_id": target["card_id"], "customize_id": identity,
                            "customize_count": option["customize_count"], "cost": price,
                            "description": option.get("description", ""),
                            "source": "Master ProduceCardCustomize.producePoint"})
                        quoted["known_pt"] += price
                        carrier = with_card_customization(carrier, option)
                    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as error:
                        result["unpriced"].append({"card_id": target["card_id"], "customize_id": identity,
                                                   "target_slot": requirement["target_slot"],
                                                   "observed_ref": requirement["observed_ref"],
                                                   "reason": str(error)})
                        break
            conditional = quoted["requires_acquisition"] or quoted["requires_normal_upgrade"]
            result["conditional_card_pt_total" if conditional else "owned_card_pt_total"] += quoted["known_pt"]
            result["known_pt_total"] += quoted["known_pt"]
            result["requirements"].append(quoted)
        result["status"] = "partial" if result["unpriced"] else "quoted"
        result["total_pt"] = None if result["unpriced"] else result["known_pt_total"]
        return result
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as error:
        return {**result, "status": "unavailable", "reason": str(error)}
