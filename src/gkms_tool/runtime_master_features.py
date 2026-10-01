"""Explicit inference-only Master routing over the frozen numerical encoder.

Training contracts, the four historical packages and model arrays remain
unchanged. Only source routing is overridden; parent token/hash/decoder code
is inherited without alteration.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path

from . import shared_bc_features as shared
from .integrated_exam_bc_features import IntegratedExamFeatureEncoder
from .native_structure_contract_set import _common
from .native_structure_features import NativeStructureCardSemanticFeatures, native_structure_contract
from .native_structure_features import _reference as verified_structure_reference
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
                 source_resolver=None, loader_compatibility=None, historical_encoder=None):
        from .runtime_master_source import RuntimeMasterSource
        if type(runtime_source) is not RuntimeMasterSource:
            raise ValueError("An independently verified runtime Master authority is required")
        runtime_source.validate_unchanged()
        self._historical_source_watches=()
        self._historical_source_resolver=source_resolver
        self._historical_material_reuse=None
        if historical_encoder is None:
            super().__init__(contract_set_reference, original_shared_reference,
                             source_resolver=source_resolver, loader_compatibility=loader_compatibility)
        else:
            self._reuse_historical_material(historical_encoder,contract_set_reference,original_shared_reference,
                source_resolver=source_resolver,loader_compatibility=loader_compatibility)
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

    def _reuse_historical_material(self,encoder,contract_set_reference,original_shared_reference,*,
                                   source_resolver,loader_compatibility):
        """Reuse sealed data only; each route/feature wrapper keeps its own state."""
        if type(encoder) is not IntegratedExamFeatureEncoder or loader_compatibility is None:
            raise ValueError('Historical reuse requires the verified original encoder and loader authority')
        expected={'contract_set':deepcopy(dict(contract_set_reference)),
                  'original_shared_encoder':deepcopy(dict(original_shared_reference))}
        if encoder._input_references!=expected:
            raise ValueError('Historical encoder input identities differ')
        loader_compatibility.validate_unchanged()
        loader_compatibility.validate_runtime_contract(encoder.contract)
        if set(encoder._features)!=set(encoder._contract_set['contracts']):
            raise ValueError('Historical encoder material coverage differs')
        references=[*expected.values(),*encoder._provenance['encoder_sources'].values()]
        features={}
        deferred=not isinstance(encoder._features,dict)
        if deferred:
            from devtools.rl.lazy_feature_materials import _LazyFeatures
            if type(encoder._features) is not _LazyFeatures:
                raise ValueError('Unrecognized deferred historical allocation authority')
            mapping=encoder._features
            for key,recipe in mapping._recipes.items():
                route=encoder._contract_set['contracts'][key]
                native=route['native_structure_contract']
                expected_arguments={name:native[name] for name in ('source_master_hash',
                    'source_inventory_reference','native_schema_reference','catalog_reference','snapshot_reference')}
                if (recipe.package.master!=native['source_master_hash'] or
                        recipe.package.arguments!=expected_arguments or
                        recipe.package.source_resolver is not source_resolver or
                        recipe.active_status_contract!=route.get('observed_active_status_contract')):
                    raise ValueError('Deferred historical recipe differs from its checked route')
            recipes={key:replace(value,package=replace(value.package,arguments=deepcopy(value.package.arguments)),
                active_status_contract=deepcopy(value.active_status_contract)) for key,value in mapping._recipes.items()}
            features=_LazyFeatures(recipes,mapping.cache,mapping._package_loader,mapping._feature_constructor)
        for key,feature in (() if deferred else encoder._features.items()):
            if type(feature) is not NativeStructureCardSemanticFeatures:
                raise ValueError('Historical reuse accepts only immutable source package features')
            package=feature.package;contract=native_structure_contract(package)
            if contract!=encoder._contract_set['contracts'][key]['native_structure_contract']:
                raise ValueError('Historical immutable package identity differs from its route')
            references.extend(contract[name] for name in ('source_inventory_reference','native_schema_reference',
                'catalog_reference','snapshot_reference','source_archive_manifest_reference'))
            references.extend(contract['supplemental_table_references'].values())
            references.extend(package.native_schema[name] for name in ('proof','grow_enum','source_image','source_metadata'))
            features[key]=NativeStructureCardSemanticFeatures(package,active_status_contract=feature.active_status_contract)
        watches={}
        for reference in references:
            identity=(reference['path'],reference['sha256'])
            if identity in watches:continue
            physical=(source_resolver.resolve(reference).physical_path if source_resolver is not None
                      else Path(reference['path']).resolve())
            before=physical.stat();before_stamp=(before.st_size,before.st_mtime_ns,before.st_ctime_ns)
            path,_=verified_structure_reference(reference,source_resolver=source_resolver)
            stat=path.stat();stamp=(stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns)
            if path!=physical or stamp!=before_stamp:
                raise ValueError('Historical source changed during material verification')
            watches[identity]=(path,stamp)
        for field in ('_input_references','_contract_set','_bridge','_layout','_provenance','_contract'):
            setattr(self,field,deepcopy(getattr(encoder,field)))
        self._features=features
        self._historical_source_watches=tuple(watches.values())
        self._historical_material_reuse={'schema':'gkms.immutable-historical-encoder-reuse.v1',
            'package_count':len(features),'verified_source_count':len(watches),
            'historical_materialization':'checked-on-first-route' if deferred else 'already-verified-immutable-packages',
            'immutable_packages_shared':True,'feature_adapters_shared':deferred,'owner_state_shared':False,
            'sharing_scope':'single-actor-projector-runtime',
            'numerical_contract_sha256':self._contract['contract_sha256'],
            'numerical_sources':deepcopy(self._provenance['encoder_sources']),
            'allocation_implementation':{'path':str(Path(__file__).resolve()),'sha256':sha256_file(Path(__file__))}}

    @property
    def historical_material_reuse(self):
        self.validate_runtime_source()
        return deepcopy(self._historical_material_reuse)

    @property
    def runtime_master_version(self):
        return self._runtime_source.execution_master_version

    @property
    def runtime_master_hash(self):
        return self._runtime_source.master_hash

    def validate_runtime_source(self):
        for path,stamp in self._historical_source_watches:
            stat=path.stat()
            if (stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns)!=stamp:
                raise ValueError('Reused historical source changed during execution')
        if self._historical_source_resolver is not None:
            self._historical_source_resolver.validate_unchanged()
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
