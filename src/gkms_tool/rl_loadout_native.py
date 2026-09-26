"""Bounded, offline-only loadout trials through the existing PC policy probe.

The outer resource projection is an explicitly qualified approximation. The
inner scores below are actual terminal scores under the pinned actor in that
projected world, never an assertion about a completed live cultivation.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from .application_paths import app_root, state_root
from .qualification_verification import source_stamp
from .rl_loadout_advisor import LoadoutRecommendationUnavailable, SharedLoadoutEvaluator, semantic_selection_key
from .rl_loadout_reference import LoadoutReferenceMaster, project_loadout_context

POLICY_SOURCE_PATHS = (
    "scripts/policy_python_runtime.py", "scripts/pc_exam_state.py", "scripts/pc_exam_candidates.py",
    "scripts/pc_pre_action_cost.py",
    "src/gkms_tool/native_policy_features.py", "src/gkms_tool/native_secondary_input.py",
    "src/gkms_tool/runtime_optional_parent_features.py", "src/gkms_tool/training_artifact_io.py",
    "src/gkms_tool/rl/native_actor_policy.py", "src/gkms_tool/rl/expert_projection.py",
    "src/gkms_tool/rl/game_projection.py", "src/gkms_tool/rl/native_information_view.py",
    "src/gkms_tool/rl/native_projection.py", "src/gkms_tool/rl/native_observation.py",
    "src/gkms_tool/rl/decision_projection.py", "src/gkms_tool/rl/semantic_entity_encoding.py",
    "src/gkms_tool/rl/actor_projection_equivalence.py", "src/gkms_tool/rl/contracts.py",
    "src/gkms_tool/rl/features.py", "src/gkms_tool/rl/networks.py", "src/gkms_tool/rl/offline_policy.py",
    "devtools/rl/import_qualified_io_equivalence.py",
)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _reference(path):
    path = Path(path).resolve()
    before = source_stamp(path)
    with path.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    if source_stamp(path) != before:
        raise ValueError("Loadout trial source changed while reading")
    return {"path": str(path), "sha256": checksum, "bytes": before[2]}


def _read(reference):
    actual = _reference(reference["path"])
    if any(actual[key] != reference[key] for key in ("sha256", "bytes") if key in reference):
        raise ValueError("Loadout trial source identity changed: " + actual["path"])
    value = json.loads(Path(actual["path"]).read_bytes())
    if not isinstance(value, dict):
        raise ValueError("Loadout trial document must be an object")
    return value


def _create(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False) + "\n").encode()
    if path.exists():
        if path.read_bytes() != encoded:
            raise ValueError("Retained loadout trial input differs")
    else:
        with path.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    return _reference(path)


class NativeLoadoutProjector:
    """One existing simulator launch per missing candidate/stage, never game IO."""
    def __init__(self, document, descriptor, reference_identity, *, root=None, executor=None):
        self.root = Path(root or app_root()).resolve()
        self.document, self.descriptor = deepcopy(document), deepcopy(descriptor)
        self.identity = reference_identity
        self.spec = self.descriptor["specification"]
        if (document.get("schema") != "gkms.rl-loadout-reference.v1"
                or document.get("checkpoint_sha256") != self.spec["checkpoint"]["sha256"]
                or document.get("dataset_identity") != self.spec["dataset_identity"]
                or document.get("partition") != "train"
                or document.get("validation_or_test_consumed") is not False):
            raise LoadoutRecommendationUnavailable("RL_LOADOUT_REFERENCE_IDENTITY_INVALID")
        config_ref = document.get("native_configuration")
        if not isinstance(config_ref, dict):
            raise LoadoutRecommendationUnavailable("RL_LOADOUT_NATIVE_CONFIGURATION_MISSING")
        self.config = _read(config_ref)
        self.template = _read(self.config["policy_template"])
        if self.template.get("factory") != "gkms_tool.rl.native_actor_policy:SharedNativeActorPolicy":
            raise ValueError("Loadout trials require the existing shared actor factory")
        self.master = LoadoutReferenceMaster(Path(document["master_directory"]))
        # Native LoadLibrary still has a MAX_PATH boundary in some Windows
        # builds. Keep locators short; the complete identities remain checked.
        self.cache = state_root(self.root) / "rl_loadout/trials" / reference_identity[:16]
        self.cache.mkdir(parents=True, exist_ok=True)
        _create(self.cache / "reference_identity.json", {"reference_identity": reference_identity})
        self.executor = executor or self._execute
        self.worker_limit = min(4, max(1, int(document.get("search_workers", 2))))
        self._source_lock = threading.RLock()
        self._watches = {}
        references = [config_ref, self.spec["checkpoint"], self.config["policy_template"],
            self.config["engine_manifest"], self.config["provider_manifest"],
            *self.spec["semantic_sources"].values(), *self.spec["training_source_code"].values()]
        references += [self.spec[name] for name in ("runtime_io_equivalence", "runtime_loader_compatibility", "runtime_master_source", "projection_equivalence")
            if name in self.spec]
        if descriptor.get("loadout_reference"):
            references.append(descriptor["loadout_reference"])
        references += list(document.get("master_files", {}).values()) if isinstance(document.get("master_files"), dict) else document.get("master_files", [])
        for context in document.get("contexts", []):
            references += [context["source"], context["qualified_evidence"]["inputs_reference"]]
        for reference in references:
            if not isinstance(reference, dict) or "path" not in reference:
                raise ValueError("Explicit loadout input file reference required")
            actual = _reference(reference["path"])
            if any(actual[k] != reference[k] for k in ("sha256", "bytes") if k in reference):
                raise ValueError("Loadout source artifact differs")
            self._watches[actual["path"]] = source_stamp(actual["path"])
        implementation = [Path(__file__), Path(__file__).with_name("rl_loadout_reference.py"),
            Path(__file__).with_name("rl_loadout_advisor.py"),
            *(self.root / "scripts" / name for name in ("probe_rebuilt_pc_core.py",
                "pc_exam_initialization.py", "pc_loadout_counterfactual.py", "pc_policy_execution.py", "pc_policy_probe.py"))]
        implementation.extend(self.root / name for name in POLICY_SOURCE_PATHS)
        code = [_reference(path) for path in implementation]
        self.execution_identity = _digest({"code": [{k: ref[k] for k in ("sha256", "bytes")} for ref in code],
            "policy": self._policy("cache-policy-identity")})
        for ref in code:
            self._watches[ref["path"]] = source_stamp(ref["path"])
        self.validate_unchanged()

    def validate_unchanged(self):
        if any(source_stamp(path) != stamp for path, stamp in self._watches.items()):
            raise LoadoutRecommendationUnavailable("RL_LOADOUT_PINNED_SOURCE_CHANGED")

    def _contexts(self, selection):
        selected = [row for row in self.document["contexts"]
            if row["produce_id"] == selection.produce_id and row["idol_card_id"] == selection.idol_card_id]
        if len(selected) != 3 or {x["stage_id"] for x in selected} != {"Mid1", "Mid2", "Final"}:
            raise LoadoutRecommendationUnavailable("RL_LOADOUT_THREE_EXAM_REFERENCE_NOT_FOUND_FOR_IDOL_MODE")
        if any(len({x[key] for x in selected}) != 1 for key in ("whole_trajectory_id", "source_group", "player_sha256")):
            raise ValueError("Paired loadout contexts changed their complete original trajectory")
        for context in selected:
            source = _read(context["source"])
            if (context.get("partition") != "train"
                    or context.get("source_master_hash") != self.document["execution_master_hash"]
                    or str(context.get("seed")) != str(source["expected"]["seed"])
                    or source.get("produce_id") != selection.produce_id
                    or source.get("idol_card_id") != selection.idol_card_id
                    or source["source"].get("trajectory_id") != context["whole_trajectory_id"]):
                raise ValueError("Loadout context scope/seed differs from its original training source")
        return sorted(selected, key=lambda x: ("Mid1", "Mid2", "Final").index(x["stage_id"]))

    def _policy(self, run_id):
        policy = deepcopy(self.template)
        names = ("checkpoint", "dataset_identity", "semantic_sources", "training_source_code", "device",
            "runtime_io_equivalence", "runtime_master_source", "runtime_loader_compatibility", "projection_equivalence")
        policy["specification"] = {key: deepcopy(self.spec[key]) for key in names if key in self.spec}
        policy["run_id"] = run_id
        return policy

    def _execute(self, command, directory):
        """The existing probe owns native child/timeout/cleanup, not this caller."""
        environment = dict(os.environ)
        # A private GUI also has portable BC/loadout asset settings. They must
        # not override the explicit, source-bound offline actor configuration.
        # This child never addresses the live DLL mailbox or GUI state store.
        from .private_gui import PROFILE_ENV
        for name in PROFILE_ENV:
            environment.pop(name, None)
        environment.update(OMP_NUM_THREADS="1", MKL_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1")
        with (directory / "stdout.log").open("xb") as out, (directory / "stderr.log").open("xb") as err:
            completed = subprocess.run(command, cwd=self.root, env=environment, stdout=out, stderr=err,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
        return completed.returncode

    def _case_input(self, context, snapshot, selection, effects):
        projected = project_loadout_context(context, snapshot, selection, effects, self.master)
        key = _digest({"reference": self.identity, "execution": self.execution_identity, "context": context["context_id"],
            "checkpoint": self.spec["checkpoint"]["sha256"], "inventory": snapshot.content_digest,
            "selection": semantic_selection_key(selection), "effects": effects.identity, "player": projected["player_fields"]})
        return key, projected

    def _case(self, context, snapshot, selection, effects):
        key, projected = self._case_input(context, snapshot, selection, effects)
        directory = self.cache / key[:24]
        identity_file = directory / "case_identity.json"
        if identity_file.exists():
            _create(identity_file, {"key": key, "reference_identity": self.identity})
        result_file = directory / "result.json"
        if result_file.is_file():
            result = json.loads(result_file.read_bytes())
            if result.get("key") != key or result.get("complete") is not True:
                raise ValueError("Retained loadout result is incomplete or differs")
            for reference in result["evidence"].values():
                if _reference(reference["path"]) != reference:
                    raise ValueError("Retained loadout rollout evidence changed")
            execution = _read(result["evidence"]["native_execution"])
            process = _read(result["evidence"]["native_process"])
            if (result.get("score") != execution.get("actual_terminal_score")
                    or result.get("checkpoint_sha256") != self.spec["checkpoint"]["sha256"]
                    or result.get("context_id") != context["context_id"]
                    or execution.get("policy_complete") is not True
                    or execution.get("native_terminal_complete") is not True
                    or execution.get("teacher_actions_used") is not False
                    or execution.get("game_process_used") is not False
                    or process.get("requested_stage_succeeded") is not True):
                raise ValueError("Retained loadout score differs from completed native evidence")
            return {**result, "cache_hit": True}
        if directory.exists():
            # Retain failed attempts. Only a recorded, actually exited native
            # process permits another attempt; unknown/in-flight owners block.
            from .gui_setup.launcher_guard import _process_alive
            if not identity_file.is_file():
                raise LoadoutRecommendationUnavailable("RL_LOADOUT_PRIOR_TRIAL_IDENTITY_UNVERIFIED: " + str(directory))
            attempts = sorted(directory.glob("attempt-*"))
            prior = attempts[-1] if attempts else directory
            failure = prior / "failure.json"
            process = json.loads(failure.read_bytes()).get("process", {}) if failure.is_file() else {}
            if (type(process.get("exit_code")) is not int
                    or _process_alive(process.get("pid")) is not False):
                raise LoadoutRecommendationUnavailable("RL_LOADOUT_PRIOR_TRIAL_INCOMPLETE: " + str(prior))
            directory = directory / ("attempt-%04d" % (len(attempts) + 2))
        directory.mkdir()
        _create(identity_file, {"key": key, "reference_identity": self.identity})
        selection_ref = _create(directory / "selection.json", asdict(selection))
        effect_ref = _create(directory / "effects.json", {"effect_projection": asdict(effects),
            "execution_master_hash": self.document["execution_master_hash"],
            "outer_projection": projected["effect_evidence"], "limitations": projected["limitations"]})
        overlay = {"schema": "gkms.loadout-counterfactual-input.v1", "original_source_ref": context["source"],
            "checkpoint_sha256": self.spec["checkpoint"]["sha256"], "produce_id": selection.produce_id,
            "idol_card_id": selection.idol_card_id, "execution_master_hash": self.document["execution_master_hash"],
            "inventory_digest": snapshot.content_digest, "selection_ref": selection_ref,
            "effect_reference": effect_ref, "player_overrides": projected["player_fields"],
            "training_admitted": False, "game_io": False}
        overlay_ref = _create(directory / "counterfactual.json", overlay)
        policy_ref = _create(directory / "policy.json", self._policy("private-loadout:" + key))
        inputs = _read(context["qualified_evidence"]["inputs_reference"])
        native = directory / "native"
        command = [sys.executable, "-B", "-X", "utf8", str(self.root / "scripts/probe_rebuilt_pc_core.py"),
            "--manifest", self.config["engine_manifest"]["path"], "--output", str(native), "--phase", "policy",
            "--policy-config", policy_ref["path"], "--replay-source", context["source"]["path"],
            "--master-manifest", inputs["master_manifest"]["path"],
            "--export-manifest", inputs["replay_export_manifest"]["path"],
            "--export-manifest-sha256", inputs["replay_export_manifest"]["sha256"],
            "--table-provider-manifest", self.config["provider_manifest"]["path"],
            "--observer-mode", "off", "--timeout", "900",
            "--loadout-counterfactual", overlay_ref["path"]]
        for name, flag in (("runtime_game_data_root", "--runtime-game-data-root"), ("roots_manifest", "--roots-manifest")):
            if name in self.config:
                value = self.config[name]
                command += [flag, value["path"] if isinstance(value, dict) else str(value)]
        export = inputs["replay_export_manifest"]
        if export.get("parent_export"):
            command += ["--export-parent-sha256", export["parent_export"]["sha256"]]
        _create(directory / "launch.json", {"argv": command, "counterfactual": overlay_ref,
            "original_source": context["source"], "checkpoint": self.spec["checkpoint"], "game_io": False})
        started = time.monotonic()
        code = self.executor(command, directory)
        process = json.loads((native / "process_outcome.json").read_bytes()) if (native / "process_outcome.json").exists() else {}
        execution = json.loads((native / "pc_policy_execution.json").read_bytes()) if (native / "pc_policy_execution.json").exists() else {}
        initialization = json.loads((native / "pc_exam_initialization.json").read_bytes()) if (native / "pc_exam_initialization.json").exists() else {}
        decisions = execution.get("policy_decisions", [])
        if (code != 0 or execution.get("policy_complete") is not True
                or execution.get("native_terminal_complete") is not True or execution.get("teacher_actions_used") is not False
                or execution.get("game_process_used") is not False
                or process.get("requested_stage_succeeded") is not True
                or initialization.get("counterfactual_applied") is not True
                or initialization.get("loadout_counterfactual_reference", {}).get("sha256") != overlay_ref["sha256"]
                or not decisions or any(row.get("policy_source", {}).get("kind") != "shared-offline-IQL"
                    or row.get("policy_source", {}).get("checkpoint", {}).get("sha256") != self.spec["checkpoint"]["sha256"]
                    for row in decisions)):
            _create(directory / "failure.json", {"return_code": code, "process": process,
                "execution": execution, "game_io": False, "complete": False})
            raise LoadoutRecommendationUnavailable("RL_LOADOUT_NATIVE_TRIAL_FAILED: " + str(directory))
        score = execution.get("actual_terminal_score")
        if type(score) is not int or score < 0:
            raise LoadoutRecommendationUnavailable("RL_LOADOUT_NATIVE_TERMINAL_SCORE_MISSING")
        result = {"key": key, "context_id": context["context_id"], "complete": True, "score": score,
            "limitations": projected["limitations"], "effect_evidence": projected["effect_evidence"],
            "checkpoint_sha256": self.spec["checkpoint"]["sha256"], "policy_source": "shared-offline-IQL",
            "native_terminal_outcome": execution.get("native_terminal_outcome"),
            "elapsed_seconds": time.monotonic() - started, "game_io": False, "cache_hit": False,
            "evidence": {"native_execution": _reference(native / "pc_policy_execution.json"),
                "native_process": _reference(native / "process_outcome.json"),
                "native_initialization": _reference(native / "pc_exam_initialization.json"),
                "counterfactual": overlay_ref, "policy": policy_ref}}
        _create(result_file, result)
        return result

    def __call__(self, snapshot, selection, effects):
        self.validate_unchanged()
        contexts = self._contexts(selection)
        # A small cap complements the existing per-probe process ownership.
        # Account/game operations never enter this pool.
        from scripts.run_fixed_native_bc_benchmark import memory_resources
        missing = sum(not (self.cache / self._case_input(context, snapshot, selection, effects)[0][:24] / "result.json").is_file()
            for context in contexts)
        workers = 1
        if missing:
            available = memory_resources(self.cache)["memory_available_bytes"]
            workers = min(self.worker_limit, missing, max(0, (available - 1024**3) // (3 * 1024**3)))
            if workers < 1:
                raise LoadoutRecommendationUnavailable("RL_LOADOUT_WAITING_FOR_NATIVE_MEMORY_BUDGET")
        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = list(executor.map(lambda context: self._case(context, snapshot, selection, effects), contexts))
        self.validate_unchanged()
        return {"checkpoint_sha256": self.spec["checkpoint"]["sha256"], "produce_id": selection.produce_id,
            "idol_card_id": selection.idol_card_id, "legacy_rule_fallback": False,
            "context_ids": [row["context_id"] for row in results], "native_scores": [row["score"] for row in results],
            "complete": True, "policy_source": "shared-offline-IQL",
            "limitations": list(dict.fromkeys(["培育收益採來源綁定估算；演出分數屬該估算情境，尚非實機培育得分改善證據。",
                *(text for row in results for text in row["limitations"])])),
            "evidence": {"method": "source-anchored-outer-projection-and-native-shared-policy-rollout",
                "native_game_engine_reused": True, "game_input_used": False, "cases": results}}


def build_loadout_projector(document, descriptor, reference_identity):
    runner = NativeLoadoutProjector(document, descriptor, reference_identity)
    return SharedLoadoutEvaluator(checkpoint_sha256=descriptor["specification"]["checkpoint"]["sha256"],
        projector=runner, source_identity=reference_identity, cache_size=128,
        validate_unchanged=runner.validate_unchanged)
