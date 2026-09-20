"""Shared six-flow inference scope with optional exact local trial binding.

This gate owns no game input, model selection, training, or source admission.
Training coverage is metadata, not an inference admission whitelist. The
optional local setting narrows a developer run; public inference needs no file.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from .canonical_training_labels import PLAN_BY_NATIVE_VALUE, EFFECT_BY_NATIVE_VALUE
from .shared_bc_features import PLAN_EFFECTS

SCHEMA = "gkms.local-flow-trial.v1"
BINDING_SCHEMA = "gkms.live-flow-scope-binding.v1"
ENVIRONMENT_VARIABLE = "GKMS_LOCAL_FLOW_TRIAL"
MAX_CONFIG_BYTES = 64 * 1024
_SEAL = object()
_PAIRS = frozenset((PLAN_BY_NATIVE_VALUE[plan], EFFECT_BY_NATIVE_VALUE[effect]) for plan, effect in PLAN_EFFECTS)
_FULLPOWER = (PLAN_BY_NATIVE_VALUE[4], EFFECT_BY_NATIVE_VALUE[47])
SUPPORTED_NATIVE_FLOW_PAIRS = frozenset(PLAN_EFFECTS)
_REQUIRED = {"schema", "trial_id", "produce_id", "plan_type", "exam_effect_type", "variant_id", "target_cycles"}


def flow_coverage(plan_type, exam_effect_type, produce_id):
    """Presentation claims are independent of trial authorization and game state."""
    pair = (plan_type, exam_effect_type)
    supported = pair in _PAIRS and produce_id in ("produce-004", "produce-005")
    trained = pair == _FULLPOWER and supported
    return {"supported": supported, "trained_flow_covered": trained,
            "experimental": supported and not trained,
            "plan_type": plan_type, "exam_effect_type": exam_effect_type, "produce_id": produce_id,
            "label": ("全力訓練範圍" if trained else "跨流派推論；本流派尚未納入這版權重訓練" if supported else "尚未支援的模式或流派"),
            "quality_accepted": False}


def _require(value, message):
    if not value:
        raise ValueError(message)


def _text(value):
    return type(value) is str and bool(value) and value == value.strip() and not any(ord(char) < 32 for char in value)


def _stamp(value):
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _local_path(value):
    _require(_text(value), "Local flow trial requires a nonempty absolute local JSON path")
    _require(not value.replace("\\", "/").startswith("//") and not (
        re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value) and not re.match(r"^[A-Za-z]:[\\/]", value)),
        "Local flow trial does not accept URI, UNC, or device paths")
    path = Path(value)
    _require(path.is_absolute(), "Local flow trial config path must be absolute")
    resolved = path.resolve(strict=True)
    _require(not str(resolved).startswith(("\\\\", "//")), "Local flow trial resolved outside a local path")
    return path, resolved


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "Local flow trial JSON has a duplicate field: " + key)
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("Local flow trial JSON contains a nonfinite value: " + value)


def _read_config(environment_path):
    try:
        logical, path = _local_path(environment_path)
        before = path.stat()
        _require(stat.S_ISREG(before.st_mode) and 0 < before.st_size <= MAX_CONFIG_BYTES,
                 "Local flow trial config must be a regular JSON file no larger than 64 KiB")
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            raw = stream.read(MAX_CONFIG_BYTES + 1)
            closed = os.fstat(stream.fileno())
        after = path.stat()
        _require(_stamp(before) == _stamp(opened) == _stamp(closed) == _stamp(after)
                 and len(raw) == after.st_size and len(raw) <= MAX_CONFIG_BYTES
                 and logical.resolve(strict=True) == path, "Local flow trial config changed while reading")
        config = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (OSError, UnicodeError) as error:
        raise ValueError("Local flow trial config is unavailable: " + str(error)) from error
    _require(type(config) is dict and _REQUIRED <= set(config) <= _REQUIRED | {"idol_card_id"},
             "Local flow trial config has missing or unsupported fields")
    _require(config["schema"] == SCHEMA and _text(config["trial_id"]) and len(config["trial_id"]) <= 128,
             "Local flow trial schema or trial_id is invalid")
    _require(config["produce_id"] in ("produce-004", "produce-005")
             and config["variant_id"] in ("baseline", "integrated")
             and type(config["plan_type"]) is str and type(config["exam_effect_type"]) is str
             and (config["plan_type"], config["exam_effect_type"]) in _PAIRS,
             "Local flow trial config mode, model variant, or symbolic flow is unsupported")
    _require(type(config["target_cycles"]) is int and config["target_cycles"] == 1,
             "Local flow trial must declare target_cycles=1")
    _require("idol_card_id" not in config or _text(config["idol_card_id"]),
             "Local flow trial idol_card_id must be a nonempty string when supplied")
    reference = {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
    return config, reference, _stamp(after)


@dataclass(frozen=True, slots=True, init=False)
class LiveFlowScope:
    """A frozen scope with fresh plain binding copies and a per-run file guard."""

    _binding_json: str
    _environment_path: str | None
    _file_stamp: tuple | None

    def __init__(self, binding, environment_path, file_stamp, *, _authority=None):
        _require(_authority is _SEAL, "Use resolve_live_flow_scope to verify the local trial")
        object.__setattr__(self, "_binding_json", json.dumps(binding, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        object.__setattr__(self, "_environment_path", environment_path)
        object.__setattr__(self, "_file_stamp", file_stamp)

    @property
    def binding(self):
        return json.loads(self._binding_json)

    def validate_unchanged(self):
        _require(os.environ.get(ENVIRONMENT_VARIABLE) == self._environment_path,
                 "Local flow trial environment changed during the active run")
        if self._environment_path is not None:
            _, reference, stamp = _read_config(self._environment_path)
            _require(reference == self.binding["config_reference"] and stamp == self._file_stamp,
                     "Local flow trial config identity or SHA changed during the active run")


def resolve_live_flow_scope(*, variant_id, produce_id, idol_card_id, plan_type, exam_effect_type, target_cycles=None):
    """Bind any represented flow; keep training coverage and trial claims separate."""
    _require(variant_id in ("baseline", "integrated"), "Explicit baseline or integrated model required")
    _require(produce_id in ("produce-004", "produce-005"), "These two completed models cover NIA Pro/Master only")
    _require(_text(idol_card_id), "A concrete idol_card_id is required for the live model scope")
    _require(type(plan_type) is str and type(exam_effect_type) is str
             and (plan_type, exam_effect_type) in _PAIRS, "Unsupported model flow pair")
    _require(target_cycles is None or type(target_cycles) is int and target_cycles > 0,
             "Target cycles must be a positive integer when supplied")
    environment_path = os.environ.get(ENVIRONMENT_VARIABLE)
    reference = file_stamp = trial_id = None
    requested = {"variant_id": variant_id, "produce_id": produce_id, "plan_type": plan_type,
                 "exam_effect_type": exam_effect_type, "idol_card_id": idol_card_id}
    trained = (plan_type, exam_effect_type) == _FULLPOWER
    if environment_path is not None:
        config, reference, file_stamp = _read_config(environment_path)
        for key in ("variant_id", "produce_id", "plan_type", "exam_effect_type"):
            _require(config[key] == requested[key], "Local flow trial does not match requested " + key)
        if "idol_card_id" in config:
            _require(config["idol_card_id"] == idol_card_id, "Local flow trial does not match requested idol_card_id")
        _require(target_cycles is None or target_cycles == 1, "Local flow trial permits exactly one target cycle")
        target_cycles, trial_id = 1, config["trial_id"]
    binding = {"schema": BINDING_SCHEMA, **requested, "flow": "|".join((produce_id, plan_type, exam_effect_type)),
        "experimental": not trained or environment_path is not None, "trained_flow_covered": trained,
        "trial_id": trial_id, "config_reference": reference, "target_cycles": target_cycles,
        "training_admitted": False, "policy_quality_accepted": False, "workflow_acceptance_claimed": False}
    scope = LiveFlowScope(binding, environment_path, file_stamp, _authority=_SEAL)
    scope.validate_unchanged()
    return scope


__all__ = ["SCHEMA", "BINDING_SCHEMA", "ENVIRONMENT_VARIABLE", "LiveFlowScope", "resolve_live_flow_scope",
           "SUPPORTED_NATIVE_FLOW_PAIRS"]
