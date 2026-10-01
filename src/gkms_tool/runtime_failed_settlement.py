"""Same-run exhausted-audition settlement using the existing native dispatcher.

The durable loss proof survives interruptions. A finished Home proves settlement,
never victory or training eligibility. This module submits no game input.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .runtime_command_client import RuntimeCommandError
from .training_artifact_io import atomic_write, canonical_json_bytes
from .runtime_session_continuity import verified_run_generation


SCHEMA = "gkms.native-failure-settlement.v2"
_LEGACY_SCHEMA = "gkms.native-failure-settlement.v1"
_AUDITION_STAGES = {
    16: "ProduceStepType_AuditionMid1",
    17: "ProduceStepType_AuditionMid2",
    18: "ProduceStepType_AuditionFinal",
}


def _failed_step_type(payload):
    # V1 could only latch Final. Missing stage is meaningful only under that
    # original schema; new journals must bind the actually observed stage.
    step = payload.get("failed_step_type", 18 if payload.get("schema") == _LEGACY_SCHEMA else None)
    if (payload.get("schema") not in {SCHEMA, _LEGACY_SCHEMA}
            or type(step) is not int or step not in _AUDITION_STAGES
            or (payload.get("schema") == _LEGACY_SCHEMA and step != 18)):
        raise RuntimeCommandError("failure settlement journal has no valid failed audition stage")
    return step


def _same_outcome(ui, payload):
    return (type(payload.get("failed_score")) is int and type(payload.get("failed_tier")) is int
        and type(ui.get("score")) is int and type(ui.get("selected_number")) is int
        and type(ui.get("step_type")) is int
        and ui.get("score") == payload["failed_score"]
        and ui.get("selected_number") == payload["failed_tier"]
        and ui.get("step_type") == _failed_step_type(payload))


def _failure(raw, produce_id, idol_card_id):
    ui = raw.get("ui_state") or {}
    context = ui.get("produce_context") or {}
    progress = raw.get("progress") or {}
    state = raw.get("state") or {}
    step = ui.get("step_type")
    return (raw.get("surface") == "audition_result"
        and raw.get("screen_type") in {"AuditionBattleResultScreenPresenter", "AuditionBattleResultHifScreenPresenter"}
        and ui.get("data_ready") is True and ui.get("is_win") is False and ui.get("can_retry") is False
        and state.get("in_progress") is True
        and type(ui.get("remaining_count")) is int and ui["remaining_count"] == 0
        and type(ui.get("selected_number")) is int and ui["selected_number"] >= 1
        and type(ui.get("score")) is int and ui["score"] >= 0
        and type(step) is int and step in _AUDITION_STAGES
        and (step == 18 or produce_id == "produce-004")
        and type(context.get("step_type")) is int and context["step_type"] == step
        and context.get("produce_id") == produce_id and context.get("idol_card_id") == idol_card_id
        and progress.get("produceId") == produce_id and progress.get("idolCardId") == idol_card_id)


def _source(snapshot):
    path = Path(snapshot.source_path).resolve()
    data = path.read_bytes()
    saved = json.loads(data)
    if (saved.get("status") != "ok" or saved.get("session_generation") != snapshot.session_generation
            or saved.get("snapshot") != snapshot.raw):
        raise RuntimeCommandError("failure settlement source does not match the observed native snapshot")
    return {"path": str(path), "sha256": hashlib.sha256(data).hexdigest(), "revision": snapshot.revision}


def _allowed(raw, target):
    action = target.get("action_id", "")
    return (action.startswith(("effect.", "result.", "notice.", "story."))
        or (action == "ui.navigation" and target.get("button_id") == "reward.continue"
            and raw.get("screen_type") == "RewardGetOverlayPresenter"))


class FailedSettlement:
    def __init__(self, path, payload):
        self.path, self.payload = Path(path), payload

    @staticmethod
    def journal_path(bridge_root, run_id):
        if not run_id or Path(run_id).name != run_id or "/" in run_id or "\\" in run_id:
            raise RuntimeCommandError("invalid failure settlement run ID")
        return Path(bridge_root) / "failed_settlements" / (run_id + ".json")

    @classmethod
    def observe(cls, bridge_root, run, snapshot):
        path = cls.journal_path(bridge_root, run.run_id)
        if not path.exists() and not _failure(snapshot.raw, run.produce_id, run.idol_card_id):
            return None
        if verified_run_generation(bridge_root, run) != snapshot.session_generation:
            raise RuntimeCommandError("failure settlement session differs from the immutable run")
        identity = {"run_id": run.run_id, "produce_id": run.produce_id,
                    "idol_card_id": run.idol_card_id, "session_generation": snapshot.session_generation}
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("schema") not in {SCHEMA, _LEGACY_SCHEMA} or any(payload.get(k) != v for k, v in identity.items()):
                raise RuntimeCommandError("failure settlement journal identity changed")
            _failed_step_type(payload)
            source = payload.get("failure_source") or {}
            data = Path(source["path"]).read_bytes()
            original = json.loads(data)
            if (hashlib.sha256(data).hexdigest() != source.get("sha256") or original.get("status") != "ok"
                    or original.get("session_generation") != snapshot.session_generation
                    or not _failure(original.get("snapshot") or {}, run.produce_id, run.idol_card_id)):
                raise RuntimeCommandError("persisted failure settlement proof changed")
            ui = original["snapshot"]["ui_state"]
            if not _same_outcome(ui, payload):
                raise RuntimeCommandError("persisted failure outcome differs from its native source")
        else:
            ui = snapshot.raw["ui_state"]
            payload = {"schema": SCHEMA, **identity, "failure_source": _source(snapshot),
                "failed_score": ui["score"], "failed_tier": ui["selected_number"],
                "failed_step_type": ui["step_type"], "steps": [],
                "settlement_complete": False, "natural_home": False}
        payload.update(game_outcome="failed", successful_cultivation=False,
                       training_eligible=False, dataset_ready=False)
        result = cls(path, payload)
        result.check(snapshot, run)
        result.save()  # Commit the observed loss before any normal finish input.
        return result

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self.path, canonical_json_bytes(self.payload))

    def check(self, snapshot, run):
        if (run is None or run.run_id != self.payload["run_id"]
                or run.produce_id != self.payload["produce_id"] or run.idol_card_id != self.payload["idol_card_id"]
                or snapshot.session_generation != self.payload["session_generation"]
                or verified_run_generation(self.path.parent.parent, run) != snapshot.session_generation):
            raise RuntimeCommandError("active run/session changed during failure settlement")
        progress = snapshot.raw.get("progress") or {}
        if (progress.get("produceId") != self.payload["produce_id"]
                or progress.get("idolCardId") != self.payload["idol_card_id"]):
            raise RuntimeCommandError("native progress changed during failure settlement")

    def choose(self, snapshot, normal_chooser):
        raw, ui = snapshot.raw, snapshot.raw.get("ui_state") or {}
        if raw.get("surface") == "error":
            raise RuntimeCommandError("native game error during failure settlement")
        if raw.get("screen_type") == "HomeTopScreenPresenter":
            raise RuntimeCommandError("failure settlement Home must be verified before any further input")
        if raw.get("busy") is True or raw.get("actions_complete") is not True or not snapshot.actions:
            return None, {"source": "native-failure-settlement", "status": "waiting",
                          "reason": "waiting for the normal failure result control"}
        if raw.get("surface") == "audition_result":
            if (not _failure(raw, self.payload["produce_id"], self.payload["idol_card_id"])
                    or not _same_outcome(ui, self.payload)):
                raise RuntimeCommandError("current result differs from the exhausted audition loss")
            name = "effect.advance" if ui.get("phase") == "await_effect_acknowledgement" else "audition.finish_failed"
            choices = [a["target"] for a in snapshot.actions if a["action_id"] == name]
            if name == "effect.advance":
                choices = [a for a in choices if a.get("button_source") == "produce-screen-touch"]
                if (ui.get("effect_confirmation") or {}).get("ready") is not True:
                    choices = []
            if any(type(target.get("step_type")) is not int or target["step_type"] != _failed_step_type(self.payload)
                   or (name == "audition.finish_failed" and not _same_outcome(target, self.payload))
                   for target in choices):
                raise RuntimeCommandError("failure result callback differs from the observed audition loss")
            if len(choices) > 1:
                raise RuntimeCommandError("ambiguous failure result callback")
            return (choices[0] if choices else None), {"source": "native-failure-settlement",
                "game_outcome": "failed", "reason": "complete the observed exhausted audition result",
                "status": "ready" if choices else "waiting"}
        target, detail = normal_chooser()
        if target is not None and not _allowed(raw, target):
            raise RuntimeCommandError("action outside normal failure settlement: " + str(target.get("action_id")))
        return target, {**detail, "game_outcome": "failed", "failure_settlement": True}

    def record(self, snapshot, target, outcome):
        if outcome.request_id is None or outcome.status == "replan":
            return
        if any(row["request_id"] == outcome.request_id for row in self.payload["steps"]):
            return
        self.payload["steps"].append({"request_id": outcome.request_id, "status": outcome.status,
            "screen": snapshot.raw["screen_type"], "action": target["action_id"]})
        self.save()

    def reconcile(self, bridge_root, outcome):
        if outcome.status == "rejected":
            raise RuntimeCommandError("failure settlement pending action was rejected: " + outcome.detail)
        if outcome.request_id is None:
            return
        path = Path(bridge_root) / "completed_outer" / (outcome.request_id + ".json")
        if not path.exists():
            return
        receipt = json.loads(path.read_text(encoding="utf-8"))
        request = receipt.get("request") or {}
        if (request.get("session_generation") != self.payload["session_generation"]
                or (receipt.get("outcome") or {}).get("status") != "settled"):
            raise RuntimeCommandError("reconciled failure receipt is not a same-session settled action")
        for row in self.payload["steps"]:
            if row["request_id"] == outcome.request_id:
                if (request.get("target") or {}).get("action_id") != row["action"]:
                    raise RuntimeCommandError("reconciled failure action differs from its journal")
                row["status"] = "settled"
                self.save()
                break

    def complete(self, snapshot):
        raw = snapshot.raw
        state, progress = raw.get("state") or {}, raw.get("progress") or {}
        memory = progress.get("resultMemory") or {}
        step = _failed_step_type(self.payload)
        auditions = progress.get("auditions")
        failed_auditions = ([row for row in auditions if isinstance(row, dict)
                             and row.get("auditionStepType") == _AUDITION_STAGES[step]]
                            if isinstance(auditions, list) else [])
        if (snapshot.session_generation != self.payload["session_generation"]
                or progress.get("produceId") != self.payload["produce_id"]
                or progress.get("idolCardId") != self.payload["idol_card_id"]
                or raw.get("screen_type") != "HomeTopScreenPresenter" or state.get("in_progress") is not False
                or state.get("progress_status") != 99
                or type(state.get("step_type")) is not int or state["step_type"] != step
                or state.get("produce_id") != self.payload["produce_id"]
                or progress.get("isFailedProduce") is not True
                or memory.get("idolCardId") != self.payload["idol_card_id"]
                or not isinstance(memory.get("userMemoryId"), str) or not memory["userMemoryId"]
                or len(failed_auditions) != 1
                or type(failed_auditions[0].get("score")) is not int
                or failed_auditions[0]["score"] != self.payload["failed_score"]
                or type(failed_auditions[0].get("selectNumber")) is not int
                or failed_auditions[0]["selectNumber"] != self.payload["failed_tier"]):
            raise RuntimeCommandError("Home does not prove this failed cultivation settled")
        self.payload.update(settlement_complete=True, natural_home=True, home_source=_source(snapshot),
                            created_memory_id=memory["userMemoryId"], failed_audition=dict(failed_auditions[0]))
        if step == 18:
            self.payload["final_audition"] = dict(failed_auditions[0])
        self.save()
        return dict(self.payload)
