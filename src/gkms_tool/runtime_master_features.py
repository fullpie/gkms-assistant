"""Explicit inference-only Master routing over the frozen numerical encoder.

Training contracts, the four historical packages and model arrays remain
unchanged. Only source routing is overridden; parent token/hash/decoder code
is inherited without alteration.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from . import shared_bc_features as shared
from .integrated_exam_bc_features import IntegratedExamFeatureEncoder
from .native_structure_contract_set import _common
from .native_structure_features import NativeStructureCardSemanticFeatures, native_structure_contract
from .runtime_optional_parent_features import RuntimeOptionalParentCardFeatures
from .observed_empty_collections import empty_collection_contract
from .training_artifact_io import canonical_json_bytes, sha256_file


def _equal(left, right):
    return canonical_json_bytes(left) == canonical_json_bytes(right)


def _compatible_representation(contract, trained_common, kernel_bridge):
    """Compare every shared numeric rule; source changes need the existing proof."""
    if (kernel_bridge.get("exact_kernel_AST_equal") is not True or
            kernel_bridge.get("other_shared_definitions_equal") is not True):
        raise ValueError("The runtime route requires the verified frozen numeric kernel")
    expected = deepcopy(trained_common)
    source_hashes = {name: reference["sha256"] for name, reference in kernel_bridge["runtime_sources"].items()}
    if set(source_hashes) != set(expected["shared"]["encoder_source_hashes"]):
        raise ValueError("Runtime route encoder source coverage differs")
    expected["shared"]["encoder_source_hashes"] = source_hashes
    if not _equal(_common(contract), expected):
        raise ValueError("Runtime Master changes the frozen numerical representation")


class RuntimeMasterFeatureEncoder(IntegratedExamFeatureEncoder):
    def __init__(self, contract_set_reference, original_shared_reference, *, runtime_source,
                 source_resolver=None, loader_compatibility=None):
        from .runtime_master_source import RuntimeMasterSource
        if type(runtime_source) is not RuntimeMasterSource:
            raise ValueError("An independently verified runtime Master authority is required")
        runtime_source.validate_unchanged()
        super().__init__(contract_set_reference, original_shared_reference,
                         source_resolver=source_resolver, loader_compatibility=loader_compatibility)
        self._runtime_source = runtime_source
        if runtime_source.master_hash in {row["source_master_hash"] for row in self._contract_set["routes"]}:
            raise ValueError("A new runtime Master must not replace a historical training route")
        modes = tuple(runtime_source.produce_ids)
        if not modes or modes != tuple(sorted(set(modes))) or any(mode not in ("produce-004", "produce-005") for mode in modes):
            raise ValueError("Explicit bounded runtime Master mode scope required")
        package = runtime_source.package(source_resolver=source_resolver)
        native = native_structure_contract(package)
        if (native["source_master_hash"] != runtime_source.master_hash or
                not _equal(native.get("inference_source_receipt"), runtime_source.reference)):
            raise ValueError("Runtime package identity differs from its sealed source authority")
        template = next(iter(self._contract_set["contracts"].values()))
        active = deepcopy(template.get("observed_active_status_contract"))
        features = RuntimeOptionalParentCardFeatures(package, active_status_contract=active)
        original_empty = template["observed_empty_collection_contract"]
        empty = empty_collection_contract(card_payload_sha256=native["card_payload_sha256"],
            snapshot_fingerprint=native["snapshot_fingerprint"],
            inactive_status_contract=original_empty.get("inactive_card_status_contract"),
            optional_identifiers_contract=original_empty.get("optional_effect_identifiers_contract"))
        contract = shared.feature_contract(features, payload_sha256=native["card_payload_sha256"],
            identity_mode="observed-slot-native-structure", produce_ids=list(modes), diagnostic_only=True,
            observed_empty_collection_contract=empty, observed_active_status_contract=active)
        _compatible_representation(contract, self._contract_set["common_representation"], self._bridge)
        self._runtime_features, self._runtime_contract = features, contract
        self._runtime_package_contract = native
        self._route_source = Path(__file__).resolve()
        self._route_reference = {"path": str(self._route_source), "sha256": sha256_file(self._route_source)}
        stat = self._route_source.stat()
        self._route_stamp = stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns
        self.validate_runtime_source()

    @property
    def runtime_master_version(self):
        return self._runtime_source.execution_master_version

    @property
    def runtime_master_hash(self):
        return self._runtime_source.master_hash

    def validate_runtime_source(self):
        self._runtime_source.validate_unchanged()
        self._runtime_features.validate_definition_extension()
        stat = self._route_source.stat()
        if (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns) != self._route_stamp:
            raise ValueError("Runtime Master route implementation changed during execution")

    def _route(self, master, produce):
        self.validate_runtime_source()
        if master == self._runtime_source.master_hash:
            if produce not in self._runtime_source.produce_ids:
                raise ValueError("Runtime Master mode is outside its explicit source scope")
            return self._runtime_features, deepcopy(self._runtime_contract)
        return super()._route(master, produce)

    def runtime_master_binding(self, version, produce):
        """Return a distinct current route; an unknown version is never aliased."""
        self.validate_runtime_source()
        if version != self._runtime_source.execution_master_version:
            return None
        _, contract = self._route(self._runtime_source.master_hash, produce)
        package_binding = {"schema": "gkms.runtime-master-package-binding.v1",
            "execution_master_version": version, "source_master_hash": self._runtime_source.master_hash,
            "produce_id": produce, "source_receipt": deepcopy(self._runtime_source.reference),
            "source_database": deepcopy(self._runtime_source.source_database),
            "package_contract_sha256": self._runtime_package_contract["contract_sha256"],
            "primary_contract_sha256": contract["contract_sha256"],
            "numeric_representation_contract_sha256": self._contract["contract_sha256"],
            "optional_parent_definition_extension": self._runtime_features.optional_parent_definition_extension,
            "route_implementation": deepcopy(self._route_reference)}
        return {"source_master_hash": self._runtime_source.master_hash, "contract": contract,
            "source_database": deepcopy(self._runtime_source.source_database),
            "runtime_package_binding": package_binding,
            "provenance": {"authority": "explicit-runtime-Master-source-package",
                "source": deepcopy(self._runtime_source.provenance), "runtime_package_binding": deepcopy(package_binding),
                "historical_training_routes_modified": False, "model_weights_modified": False,
                "training_admitted": False, "live_policy_win_proven": False}}


__all__ = ["RuntimeMasterFeatureEncoder"]
