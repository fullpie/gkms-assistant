"""Versioned GUI choices for the two completed exam policies.

Artifact availability is separate from live backend admission. This module
never changes a registry, loads a game, owns input or fabricates worker status.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import threading

from .application_paths import app_root

ROOT = app_root()
POLICY_VARIANT_IDS = ("baseline", "integrated")
POLICY_VARIANT_LABELS = {"baseline": "主 BC＋附屬策略", "integrated": "整合版 BC"}
CATALOG_RELATIVE = Path("var/research/pc_core_20260914/gui_exam_models_v1/manifest.json")
CATALOG_SHA256 = "fa1ffc67255e42b9a46b17a6e7b05e9a8e51020887be1a2dcadaa070c4dc9058"
MODEL_SHA256 = {
    "baseline": "9200a547d7c45058b115a6d0f6ea77b12d237bfc86f131a5ebcb1054c91e3834",
    "integrated": "83b902963b578a4fa9d270c508e4b10d21308d2351d59113043d0a776bdb2c9c",
}
_FACTORIES = {"baseline": "gkms_tool.native_policy_features:MixedNativeBcPolicy",
              "integrated": "gkms_tool.integrated_exam_bc_policy:IntegratedNativeBcPolicy"}
_CACHE = {}
_LOCK = threading.RLock()


def _stamp(path):
    path = Path(path).resolve()
    try:
        stat = path.stat()
        return str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns
    except OSError as error:
        return str(path), None, type(error).__name__, error.errno


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class _ArtifactChecks:
    def __init__(self):
        self.watched = {}
        self.verified = set()
        self.documents = {}

    def read(self, reference, *, document=True):
        if (not isinstance(reference, Mapping) or not isinstance(reference.get("path"), str)
                or not isinstance(reference.get("sha256"), str)):
            raise ValueError("完整模型檔案參照缺失")
        path = Path(reference["path"]).resolve()
        stamp = _stamp(path)
        self.watched[str(path)] = stamp
        if stamp[1] is None:
            raise FileNotFoundError(path.name)
        key = (str(path), reference["sha256"])
        if key not in self.verified:
            if _sha256_file(path) != reference["sha256"] or "bytes" in reference and stamp[1] != reference["bytes"]:
                raise ValueError("檔案版本不符：" + path.name)
            if _stamp(path) != stamp:
                raise ValueError("檔案在核對時改變：" + path.name)
            self.verified.add(key)
        if not document:
            return None
        if key not in self.documents:
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != reference["sha256"] or _stamp(path) != stamp:
                raise ValueError("文件在核對時改變：" + path.name)
            self.documents[key] = json.loads(data)
        return self.documents[key]


def _placeholder(variant, reason, detail=""):
    return {"id": variant, "label": POLICY_VARIANT_LABELS[variant], "model_label": POLICY_VARIANT_LABELS[variant],
        "model_sha256": MODEL_SHA256[variant], "available": False, "reason": reason, "diagnostic": detail,
        "artifact_available": False, "live_ready": False, "start_block_reason": reason,
        "research_fallback": False, "unsupported_route_behavior": "stop-with-reason"}


def _verified_variant(variant, reference, manifest, checks):
    descriptor = checks.read(reference)
    if (descriptor.get("schema") != "gkms.gui-exam-policy-descriptor.v1" or descriptor.get("id") != variant
            or descriptor.get("label") != POLICY_VARIANT_LABELS[variant]
            or descriptor.get("model_sha256") != MODEL_SHA256[variant]
            or descriptor.get("policy_factory") != _FACTORIES[variant]
            or descriptor.get("research_fallback") is not False
            or descriptor.get("live_ready") is not False
            or descriptor.get("active_registry_modified") is not False):
        raise ValueError("策略描述版本不符")
    spec = descriptor["specification"]
    if set(spec) != {"model", "contract_set", "original_shared_encoder"} or spec["model"] != descriptor["model"]:
        raise ValueError("策略模型與載入參照不同")
    if descriptor["model"]["sha256"] != MODEL_SHA256[variant] or descriptor["evidence"] != manifest["evidence"]:
        raise ValueError("模型與完成證據不同")
    for key in ("model", "contract_set", "original_shared_encoder"):
        checks.read(spec[key], document=False)
    evidence = checks.read(descriptor["evidence"])
    if (evidence.get("schema") != "gkms.gui-exam-model-completed-evidence.v1"
            or evidence.get("status") != "completed-artifacts-verified"
            or evidence["model_loading_verified"].get(variant) is not True
            or evidence["models"][variant]["sha256"] != MODEL_SHA256[variant]
            or evidence["native_comparison"]["completed"].get(variant) != 18
            or evidence.get("live_ui_or_server_clear_observed") is not False):
        raise ValueError("完成的模型／比較證據不足")
    for ref in evidence["inputs"].values():
        checks.read(ref, document=False)
    result = deepcopy(descriptor)
    result.update(available=True, reason=None, artifact_available=True, descriptor_reference=deepcopy(reference),
        can_request_preflight=True,
        preflight_note='開始時會先核對 DLL、完整模型及目前遊戲資料；通過前不消耗 AP。',
        start_block_reason=descriptor["live_start_block_reason"],
        loading_readiness={"model_bytes_verified": True, "offline_model_reader_verified": True,
            "owned_original_pc_policy_available": True, "live_adapter_ready": False,
            "current_master_live_binding_verified": False},
        evidence_summary={"native_completed_cases": 18, "derived_clear_cases": descriptor["offline_derived_clear_cases"],
            "live_ui_or_server_clear_observed": False, "full_cultivation_qualified": False})
    return result


def get_gui_exam_policy_variants(*, project_root=ROOT, refresh=False):
    """Return two selectable artifacts; normal snapshots only stat cached files.

    ``available`` refers to the model artifact, never to live/start admission.
    Use the explicit refresh entry before starting a worker. Returned copies
    cannot mutate the cache or the immutable versioned policy declarations.
    """
    if type(refresh) is not bool:
        raise ValueError("refresh must be boolean")
    # A configured public package is a distinct, pinned inference artifact.
    # Missing/corrupt public files never fall back to private research assets.
    from .portable_model_assets import configured_portable_assets
    try:
        portable = configured_portable_assets(project_root=project_root)
        if portable is not None:
            return tuple(portable.descriptor(variant) for variant in POLICY_VARIANT_IDS)
    except (OSError, ValueError, KeyError, TypeError) as error:
        return tuple(_placeholder(variant, "公開模型套件缺失或版本不符", str(error)) for variant in POLICY_VARIANT_IDS)
    root = str(Path(project_root).resolve())
    with _LOCK:
        cached = _CACHE.get(root)
        if cached and not refresh and all(_stamp(path) == stamp for path, stamp in cached[0].items()):
            return deepcopy(cached[1])
        checks = _ArtifactChecks()
        catalog_ref = {"path": str(Path(root) / CATALOG_RELATIVE), "sha256": CATALOG_SHA256}
        try:
            manifest = checks.read(catalog_ref)
            if (manifest.get("schema") != "gkms.gui-exam-model-catalog-manifest.v1"
                    or set(manifest.get("descriptors", {})) != set(POLICY_VARIANT_IDS)
                    or manifest.get("automatic_model_activation") is not False):
                raise ValueError("兩個固定版本的策略目錄缺失")
            result = []
            for variant in POLICY_VARIANT_IDS:
                try:
                    result.append(_verified_variant(variant, manifest["descriptors"][variant], manifest, checks))
                except (OSError, ValueError, KeyError, TypeError) as error:
                    result.append(_placeholder(variant, "模型檔案缺失或版本不符", str(error)))
        except (OSError, ValueError, KeyError, TypeError) as error:
            result = [_placeholder(variant, "模型目錄尚未就緒", str(error)) for variant in POLICY_VARIANT_IDS]
        # A later file read may overlap replacement of an earlier checked
        # dependency. Never publish a mixed-version successful refresh.
        if any(_stamp(path) != stamp for path, stamp in checks.watched.items()):
            _CACHE.pop(root, None)
            return tuple(_placeholder(variant, "模型檔案正在更新，請重新讀取") for variant in POLICY_VARIANT_IDS)
        result = tuple(result)
        _CACHE[root] = (dict(checks.watched), result)
        return deepcopy(result)


def get_gui_exam_policy(variant_id, *, project_root=ROOT, refresh=False):
    if variant_id not in POLICY_VARIANT_IDS:
        raise ValueError("策略必須為 baseline 或 integrated")
    return next(row for row in get_gui_exam_policy_variants(project_root=project_root, refresh=refresh) if row["id"] == variant_id)


def verify_gui_exam_model_artifacts(variant_id, *, project_root=ROOT):
    """Start preflight: rehash artifacts even when a GUI cache entry exists."""
    return get_gui_exam_policy(variant_id, project_root=project_root, refresh=True)


def policy_selection_state(variant_id, *, running=False, pending_transaction=False, existing_run_id=None):
    """Selection applies to the next run and never becomes actual-worker state."""
    if variant_id not in POLICY_VARIANT_IDS:
        raise ValueError("策略必須為 baseline 或 integrated")
    if type(running) is not bool or type(pending_transaction) is not bool:
        raise ValueError("策略鎖定狀態必須來自明確的執行／交易狀態")
    if existing_run_id is not None and (not isinstance(existing_run_id, str) or not existing_run_id):
        raise ValueError("目前場次 ID 無效")
    locked = running or pending_transaction or existing_run_id is not None
    reason = ("原生操作尚待確認，暫停切換策略" if pending_transaction else
              "目前場次保留原策略，結束後才可切換" if locked else None)
    return {"policy_variant_id": variant_id, "locked": locked, "reason": reason,
        "affects_next_run_only": True, "research_fallback": False}


def current_policy_selection_state(variant_id, *, running=False, pending_transaction=False):
    """Read the durable run identity; a stale GUI cache cannot unlock a model."""
    from .run_identity import load_active_run
    try:
        active = load_active_run()
    except (OSError, ValueError, TypeError, KeyError) as error:
        return {**policy_selection_state(variant_id, running=running, pending_transaction=pending_transaction),
            "locked": True, "reason": "目前場次資料無法確認，暫停切換模型。",
            "reason_kind": "active-run-unavailable", "active_run_id": None, "active_variant_id": None,
            "read_error": str(error), "authority": "durable-active-run"}
    result = policy_selection_state(variant_id, running=running, pending_transaction=pending_transaction,
        existing_run_id=None if active is None else active.run_id)
    actual = None if active is None else active.evidence.get('exam_policy_variant')
    result.update(active_run_id=None if active is None else active.run_id,
        active_variant_id=actual if actual in POLICY_VARIANT_IDS else None,
        reason_kind='active-run' if active is not None else 'pending' if pending_transaction else 'running' if running else None,
        authority='durable-active-run')
    return result


__all__ = ["POLICY_VARIANT_IDS", "POLICY_VARIANT_LABELS", "get_gui_exam_policy_variants",
    "get_gui_exam_policy", "verify_gui_exam_model_artifacts", "policy_selection_state", "current_policy_selection_state"]
