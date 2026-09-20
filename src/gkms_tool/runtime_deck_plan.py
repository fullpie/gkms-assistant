"""Run-owned adoption of one optional deck reference; never owns game input."""
from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path

from .deck_plan import DeckPlan, load_deck_plan_library, select_deck_plan
from .training_artifact_io import atomic_write, canonical_json_bytes


_SCOPE = ("produce_id", "idol_card_id", "plan_type", "exam_effect_type")


class RuntimeDeckPlan:
    """Adopt once at the first complete-deck decision boundary of a run.

    Persist no-reference outcomes too: newly downloaded references are for a
    subsequent run. Bad/unwritable optional reference data disables this hint,
    without blocking native decisions or adopting an unrecorded replacement.
    """

    def __init__(self, path: Path, *, run_id: str, library_loader=load_deck_plan_library):
        self.path, self.run_id = Path(path), run_id
        self.library_loader = library_loader
        self._record = None

    def bind(self, context: Mapping, deck, *, boundary: Mapping | None = None):
        result = dict(context)
        scope = {key: context.get(key) for key in _SCOPE}
        if any(not isinstance(value, str) or not value for value in scope.values()):
            return {**result, "deck_plan": None, "deck_plan_status": {
                "status": "unavailable", "reason": "complete-plan-scope-unavailable"}}
        if self._record is None:
            try:
                if self.path.exists():
                    self._record = json.loads(self.path.read_text(encoding="utf-8"))
                elif not isinstance(deck, (tuple, list)) or not deck:
                    return {**result, "deck_plan": None, "deck_plan_status": {
                        "status": "waiting", "reason": "complete-current-deck-unavailable"}}
                else:
                    selection = select_deck_plan(context, deck, library=self.library_loader(context=context))
                    # An incomplete native deck is not a durable no-reference
                    # finding. Wait for an actual complete inventory boundary.
                    if selection.reason.startswith(("plan-unavailable:", "native-current-deck-unavailable")):
                        return {**result, "deck_plan": None, "deck_plan_status": {
                            "status": "unavailable", "reason": selection.reason}}
                    record = {"schema": "gkms.run-deck-plan.v1", "run_id": self.run_id,
                              "scope": scope, "adopted_at": dict(boundary or {}),
                              "selection": selection.to_dict()}
                    atomic_write(self.path, canonical_json_bytes(record))
                    self._record = record
            except (OSError, ValueError, TypeError) as error:
                self._record = {"disabled_reason": f"plan-storage-unavailable:{type(error).__name__}"}
        try:
            record = self._record
            if not isinstance(record, Mapping):
                raise ValueError("invalid-plan-record")
            if "disabled_reason" in record:
                raise ValueError(record["disabled_reason"])
            if (record.get("schema") != "gkms.run-deck-plan.v1" or record.get("run_id") != self.run_id
                    or record.get("scope") != scope):
                raise ValueError("persisted-plan-run-or-scope-mismatch")
            selection = record["selection"]
            plan = DeckPlan.from_dict(selection["plan"]) if selection.get("plan") is not None else None
            if plan is not None and (plan.scope != tuple(scope[key] for key in
                    ("produce_id", "plan_type", "exam_effect_type")) or plan.idol_card_id != scope["idol_card_id"]):
                raise ValueError("persisted-reference-scope-mismatch")
            return {**result, "deck_plan": plan.to_dict() if plan else None, "deck_plan_status": {
                "status": "adopted" if plan else "no-plan", "plan_id": plan.plan_id if plan else None,
                "reason": selection["reason"], "run_id": self.run_id,
                "adopted_at": record["adopted_at"], "path": str(self.path)}}
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            return {**result, "deck_plan": None, "deck_plan_status": {
                "status": "unavailable", "reason": str(error), "run_id": self.run_id}}
