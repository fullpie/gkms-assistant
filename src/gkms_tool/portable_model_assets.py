"""Hash-bound inference assets exported from fully verified development sources.

Only constructors and source routing differ. Numerical feature/model methods
are inherited from the frozen implementations. Public artifacts contain no
replay rows, executable game images, metadata images or development addresses.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re

import numpy as np

from .training_artifact_io import canonical_json_bytes, sha256_file

SCHEMA = "gkms.portable-model-assets.v1"
DEFAULT_RELATIVE = Path("assets/model_runtime")
ENVIRONMENT_VARIABLE = "GKMS_PORTABLE_MODEL_ASSETS"
RELEASE_MANIFEST_SHA256 = "e85b8eae35b917f04776ef7ea8c208d47e7a5558c38e71c5a0f9a256d1761923"
_SEAL = object()
_CACHE = {}
_MODEL_SHA = {"baseline": "9200a547d7c45058b115a6d0f6ea77b12d237bfc86f131a5ebcb1054c91e3834",
    "integrated": "83b902963b578a4fa9d270c508e4b10d21308d2351d59113043d0a776bdb2c9c"}


def _require(value, reason):
    if not value:
        raise ValueError(reason)


def _digest(value):
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _plain(value):
    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    return value


def public_provenance(value):
    """Replace development addresses by content IDs, never executable paths."""
    if isinstance(value, Mapping):
        result = {k: public_provenance(v) for k, v in value.items()}
        if isinstance(value.get("sha256"), str) and isinstance(value.get("path"), str):
            result["path"] = "sha256:" + value["sha256"]
        return result
    if isinstance(value, (list, tuple)):
        return [public_provenance(v) for v in value]
    if isinstance(value, str) and (re.match(r"^[A-Za-z]:[\\/]", value)
            or value.startswith(("\\\\", "/home/", "/Users/", "/mnt/", "/tmp/"))):
        return "source-address-sha256:" + hashlib.sha256(value.encode()).hexdigest()
    return value


def _stamp(path):
    value = path.stat()
    return value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _tensor_identity(array):
    return {"dtype": str(array.dtype), "shape": list(array.shape),
        "sha256": hashlib.sha256(array.tobytes(order="C")).hexdigest()}


class PortableModelAssets:
    def __init__(self, root, expected_sha256):
        self.root = Path(root).resolve()
        self._watched = {}
        manifest = self.root / "manifest.json"
        _require(isinstance(expected_sha256, str) and re.fullmatch("[a-f0-9]{64}", expected_sha256),
            "A release-pinned portable manifest SHA is required")
        raw = self._read_path(manifest, expected_sha256)
        self.reference = {"path": str(manifest), "sha256": expected_sha256}
        self.manifest = json.loads(raw)
        body = self.manifest
        _require(body.get("schema") == SCHEMA and set(body.get("models", {})) == set(_MODEL_SHA)
            and body.get("training_admitted") is False and body.get("raw_game_or_replay_assets_included") is False,
            "Portable inference manifest scope differs")
        for variant, model in body["models"].items():
            _require(model["original_model_sha256"] == _MODEL_SHA[variant], "Portable model source identity differs")
        source_dir = Path(__file__).resolve().parent
        for name, sha in body["numeric_sources"].items():
            _require(Path(name).name == name and name.endswith(".py"), "Invalid frozen numerical source name")
            self._read_path(source_dir / name, sha)
        # All declared assets are checked, including those not needed by the selected head.
        for ref in body["files"].values():
            self.read(ref, document=False)
        self.materials = self.read(body["files"]["structure"])
        self.contracts = self.read(body["files"]["contracts"])
        self._consumer = self.read(body["files"]["consumer_evidence"])
        self.validate_unchanged()

    def _read_path(self, path, sha):
        before = _stamp(path); raw = path.read_bytes(); after = _stamp(path)
        _require(before == after and hashlib.sha256(raw).hexdigest() == sha,
            "Portable model asset SHA or stable-read check failed: " + path.name)
        self._watched[path] = after
        return raw

    def physical_reference(self, ref):
        relative = PurePosixPath(ref["path"])
        _require(not relative.is_absolute() and relative.parts and ".." not in relative.parts
            and ":" not in ref["path"] and "\\" not in ref["path"], "Portable asset path escapes its package")
        path = (self.root / Path(*relative.parts)).resolve()
        _require(path.is_relative_to(self.root), "Portable asset symlink escapes its package")
        return {"path": str(path), "sha256": ref["sha256"], "bytes": ref["bytes"]}

    def read(self, ref, *, document=True):
        actual = self.physical_reference(ref)
        raw = self._read_path(Path(actual["path"]), actual["sha256"])
        _require(len(raw) == actual["bytes"], "Portable model asset length differs")
        if not document:
            return raw
        return json.loads(gzip.decompress(raw) if ref["path"].endswith(".gz") else raw)

    def validate_unchanged(self):
        _require(all(_stamp(path) == stamp for path, stamp in self._watched.items()),
            "Portable model assets or numerical code changed during this run")

    def consumer_evidence(self):
        self.validate_unchanged()
        return {**deepcopy(self._consumer), "verified_reference": self.physical_reference(
            self.manifest["files"]["consumer_evidence"])}

    @property
    def trained_feature_contract(self):
        return {"contract_sha256": self.manifest["trained_integrated_contract_sha256"]}

    def descriptor(self, variant):
        _require(variant in _MODEL_SHA, "Unknown portable model variant")
        self.validate_unchanged()
        from .gui_exam_models import POLICY_VARIANT_LABELS
        model = self.manifest["models"][variant]
        return {"id": variant, "label": POLICY_VARIANT_LABELS[variant], "model_label": POLICY_VARIANT_LABELS[variant],
            "model_sha256": model["original_model_sha256"], "original_model_sha256": model["original_model_sha256"],
            "artifact_sha256": model["artifact"]["sha256"], "model": self.physical_reference(model["artifact"]),
            "available": True, "artifact_available": True, "can_request_preflight": True,
            "live_ready": False, "reason": None, "research_fallback": False,
            "portable_assets_reference": deepcopy(self.reference), "unsupported_route_behavior": "stop-with-reason",
            "training_coverage": deepcopy(model["training_coverage"]),
            "start_block_reason": "Actual DLL and Master preflight required before AP use"}

    def load_parameters(self, variant):
        model = self.manifest["models"][variant]
        ref = self.physical_reference(model["artifact"])
        with np.load(ref["path"], allow_pickle=False) as arrays:
            _require(set(arrays.files) == set(model["tensors"]), "Portable model tensor set differs")
            parameters = {name: arrays[name].copy() for name in arrays.files}
        _require(all(_tensor_identity(value) == model["tensors"][name] for name, value in parameters.items()),
            "Portable model weights differ from verified original tensors")
        if variant == "integrated":
            from .integrated_exam_bc_model import validate_parameters
            validate_parameters(parameters, finite=True)
        else:
            _require(all(value.dtype == np.float32 and np.all(np.isfinite(value)) for value in parameters.values()),
                "Portable primary model tensors must be finite float32")
        self.validate_unchanged()
        return parameters, {"schema": "gkms.portable-inference-model.v1", "original_model_sha256": _MODEL_SHA[variant],
            "artifact_sha256": ref["sha256"], "tensors_equal_original": True, "training_admitted": False}


def load_portable_model_assets(root, *, expected_sha256):
    key = str(Path(root).resolve()), expected_sha256
    if key not in _CACHE:
        _CACHE[key] = PortableModelAssets(root, expected_sha256)
    _CACHE[key].validate_unchanged()
    return _CACHE[key]


def configured_portable_assets(*, project_root=None):
    from .application_paths import model_assets_root
    package = model_assets_root(root=project_root)
    if package is None:
        return None
    _require(package.is_absolute(), "Configured portable assets require an absolute local directory")
    return load_portable_model_assets(package, expected_sha256=RELEASE_MANIFEST_SHA256)


# This constructor has a separate, public-export authority. It never opens or
# bypasses the historical NativeStructurePackage factory's private seal.
from .native_structure_features import NativeStructurePackage, _freeze


class PortableStructurePackage(NativeStructurePackage):
    def __init__(self, assets, *, _authority=None):
        _require(_authority is _SEAL and type(assets) is PortableModelAssets, "Verified portable package factory required")
        assets.validate_unchanged()
        material = assets.materials
        for name in ("payload", "snapshot", "native_schema", "supplemental_tables"):
            value = deepcopy(material[name])
            if name == "native_schema":
                value["numeric"] = {owner: {int(key): member for key, member in members.items()}
                    for owner, members in value["numeric"].items()}
            object.__setattr__(self, name, _freeze(value))
        object.__setattr__(self, "contract", _freeze(assets.contracts["native_contract"]))


from .runtime_optional_parent_features import RuntimeOptionalParentCardFeatures
from .native_structure_features import NativeStructureCardSemanticFeatures


class PortableCardFeatures(RuntimeOptionalParentCardFeatures):
    def __init__(self, assets):
        NativeStructureCardSemanticFeatures.__init__(self, PortableStructurePackage(assets, _authority=_SEAL),
            active_status_contract=assets.contracts["primary_contract"].get("observed_active_status_contract"))
        object.__setattr__(self, "_portable_assets", assets)
        object.__setattr__(self, "_extension_json", canonical_json_bytes(assets.contracts["optional_parent_extension"]))

    def validate_definition_extension(self):
        self._portable_assets.validate_unchanged()


from .integrated_exam_bc_features import IntegratedExamFeatureEncoder


class PortableFeatureEncoder(IntegratedExamFeatureEncoder):
    """Constructor/route only; the frozen numerical methods remain inherited."""
    def __init__(self, assets):
        self.assets = assets
        self._layout = deepcopy(assets.contracts["integrated_contract"]["pointer_layout"])
        self._contract = deepcopy(assets.contracts["integrated_contract"])
        self._contract_set = deepcopy(assets.contracts["portable_contract_set"])
        self._provenance = {"authority": SCHEMA, "manifest_sha256": assets.reference["sha256"],
            "trained_contract_set_sha256": assets.manifest["trained_contract_set_sha256"], "training_admitted": False}
        self._input_references = {"portable_manifest": deepcopy(assets.reference)}
        self.features = PortableCardFeatures(assets)
        from .native_structure_contract_set import _common
        original = assets.contracts["primary_contract"]
        for contract in (original, assets.contracts["native_contract"], self._contract):
            _require(contract["contract_sha256"] == _digest({key: value for key, value in contract.items()
                if key != "contract_sha256"}), "Portable numerical contract digest differs")
        _require(_common(original) == assets.contracts["common_representation"]
            and original["native_structure_contract"] == assets.contracts["native_contract"]
            and all(assets.manifest["numeric_sources"].get(name) == sha
                for name, sha in original["encoder_source_hashes"].items()),
            "Portable package differs from its source-verified numerical representation")

    def _route(self, master, produce):
        self.assets.validate_unchanged()
        _require(master == self.assets.manifest["master_hash"] and produce in self.assets.manifest["produce_ids"],
            "Master or mode is not present in this portable inference package")
        return self.features, deepcopy(self.assets.contracts["primary_contract"])

    def runtime_master_binding(self, version, produce):
        if version != self.assets.manifest["execution_master_version"]:
            return None
        _, contract = self._route(self.assets.manifest["master_hash"], produce)
        database = self.assets.physical_reference(self.assets.manifest["files"]["database"])
        binding = {"schema": "gkms.portable-runtime-master-binding.v1", "execution_master_version": version,
            "source_master_hash": self.assets.manifest["master_hash"], "produce_id": produce,
            "manifest_sha256": self.assets.reference["sha256"], "source_database_sha256": database["sha256"],
            "primary_contract_sha256": contract["contract_sha256"],
            "numeric_representation_contract_sha256": self._contract["contract_sha256"]}
        return {"source_master_hash": self.assets.manifest["master_hash"], "contract": contract,
            "source_database": database, "runtime_package_binding": binding,
            "provenance": {"authority": "verified-portable-current-Master", "runtime_package_binding": deepcopy(binding),
                "historical_Master_aliased": False, "training_admitted": False}}

    def validate_runtime_source(self):
        self.assets.validate_unchanged()


class PortableMasterCatalog:
    def __init__(self, encoder):
        self.encoder = encoder

    def validate_unchanged(self):
        self.encoder.assets.validate_unchanged()

    def resolve(self, execution_master, produce_id):
        from .runtime_live_exam_input import _pointer
        self.validate_unchanged()
        _require(isinstance(execution_master, Mapping)
            and execution_master.get("schema") == "gkms.native-execution-master-observation.v1"
            and execution_master.get("authority") == "native-existing-MasterManager"
            and execution_master.get("ready") is True and execution_master.get("manager_count") == 1
            and execution_master.get("master_update_succeeded") is True
            and execution_master.get("master_tables_initialized") is True, "Current native MasterManager is not ready")
        _pointer(execution_master.get("manager_instance_id"))
        bound = self.encoder.runtime_master_binding(execution_master.get("execution_master_version"), produce_id)
        _require(bound is not None, "Current Master is not included in this portable package")
        _require(execution_master.get("execution_master_hash") in (None, bound["source_master_hash"]),
            "Observed current Master hash contradicts the portable source")
        bound["provenance"]["execution_master"] = deepcopy(execution_master)
        return bound


@dataclass
class PortableRuntimeBundle:
    assets: PortableModelAssets
    descriptor: dict
    encoder: PortableFeatureEncoder
    catalog: PortableMasterCatalog
    parameters: dict
    metadata: dict
    model_reference: dict
    original_model_sha256: str
    manifest_reference: dict
    runtime_model_compatibility: dict

    @property
    def loader_compatibility(self): return self.assets
    @property
    def source_resolver(self): return self.assets
    @property
    def runtime_source(self): return self
    @property
    def execution_master_version(self): return self.assets.manifest["execution_master_version"]


def load_portable_model_runtime(variant, *, assets=None):
    assets = configured_portable_assets() if assets is None else assets
    if assets is None:
        return None
    descriptor = assets.descriptor(variant)
    encoder = PortableFeatureEncoder(assets)
    parameters, metadata = assets.load_parameters(variant)
    return PortableRuntimeBundle(assets, descriptor, encoder, PortableMasterCatalog(encoder), parameters, metadata,
        descriptor["model"], descriptor["original_model_sha256"], deepcopy(assets.reference),
        {"schema": "gkms.portable-numerical-representation-bridge.v1", "identical_numeric_representation": True,
            "source_loader_receipt_sha256": assets.manifest["source_loader_receipt_sha256"],
            "source_runtime_contract_sha256": assets.manifest["source_runtime_contract_sha256"],
            "portable_manifest_sha256": assets.reference["sha256"], "training_admitted": False})
