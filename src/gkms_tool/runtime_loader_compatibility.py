"""Verify an explicit frozen-model to runtime-loader compatibility receipt.

Only source-location plumbing may differ in the three named files. Numerical
encoder code, constants and all other source files must retain their original
AST/bytes. Runtime source identities stay new; trained contracts remain old.
This bridge neither admits a new game consumer nor changes a model artifact.
"""
from __future__ import annotations

import ast
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from .artifact_source_resolver import ArtifactSourceResolver
from .training_artifact_io import canonical_json_bytes, sha256_file

SCHEMA = 'gkms.model-runtime-loader-compatibility.v1'
_SEAL = object()
_CHANGED = {'native_structure_features.py', 'native_policy_features.py', 'integrated_exam_bc_features.py'}
_INFERENCE_FACTORY_AST_SHA256 = '74102e6a055905b14865fdfa6b623803d4726205d51153315d3acdc4d0df1e2e'


def _ast(value):
    return ast.dump(value, include_attributes=False)


def _statement(text):
    return ast.parse(text).body[0]


def _equal(left, right):
    return _ast(left) == _ast(right)


def _read(reference):
    path = Path(reference['path']).resolve()
    before = path.stat()
    if sha256_file(path) != reference['sha256']:
        raise ValueError('Loader compatibility source SHA differs: ' + str(path))
    raw = path.read_bytes(); after = path.stat()
    stamp = (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
    if stamp != (before.st_size, before.st_mtime_ns, before.st_ctime_ns):
        raise ValueError('Loader compatibility source changed while reading')
    return raw, (path, stamp)


class _RemoveResolverPlumbing(ast.NodeTransformer):
    """Remove only named optional dependency arguments/call forwarding."""
    def visit_FunctionDef(self, node):
        node = self.generic_visit(node)
        kept = [(arg, default) for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults)
            if arg.arg not in ('source_resolver', 'loader_compatibility')]
        node.args.kwonlyargs = [arg for arg, _ in kept]
        node.args.kw_defaults = [default for _, default in kept]
        return node

    def visit_Call(self, node):
        node = self.generic_visit(node)
        if isinstance(node.func, ast.Name) and node.func.id in (
                '_reference', '_native_schema', 'load_native_structure_package', 'verify_primary_kernel_bridge'):
            node.keywords = [kw for kw in node.keywords if kw.arg not in ('source_resolver', 'loader_compatibility')]
        return node


def _normalize_loader_source(name, raw):
    tree = ast.parse(raw.decode('utf-8'))
    if name == 'native_structure_features.py':
        additions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
            and node.name == 'load_native_structure_inference_package']
        if additions:
            if len(additions) != 1 or hashlib.sha256(_ast(additions[0]).encode()).hexdigest() != _INFERENCE_FACTORY_AST_SHA256:
                raise ValueError('The added public inference factory differs from its exact reviewed definition')
            tree.body.remove(additions[0])
            exports = [node for node in tree.body if isinstance(node, ast.Assign)
                and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and node.targets[0].id == '__all__']
            if len(exports) != 1 or not isinstance(exports[0].value, ast.List):
                raise ValueError('The inference factory must have one explicit export')
            values = exports[0].value.elts
            named = [node for node in values if isinstance(node, ast.Constant) and node.value == 'load_native_structure_inference_package']
            if len(named) != 1:
                raise ValueError('The inference factory export is missing or ambiguous')
            values.remove(named[0])
    def function(name):
        values = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name]
        if len(values) != 1:
            raise ValueError('Expected single loader function: ' + name)
        return values[0]
    if name == 'native_structure_features.py':
        ref = function('_reference')
        alias = _statement('logical_path = path')
        branch = _statement('''if source_resolver is not None:
    resolved = source_resolver.resolve(reference, expected_sha256=expected)
    if resolved.logical_reference != dict(reference):
        raise ValueError("Source resolver changed the original logical reference")
    path = resolved.physical_path''')
        if sum(_equal(node, alias) for node in ref.body) != 1 or sum(_equal(node, branch) for node in ref.body) != 1:
            raise ValueError('Only the exact logical/physical reference bridge is accepted')
        ref.body = [node for node in ref.body if not _equal(node, alias) and not _equal(node, branch)]
        expected_return = _statement('return path, {"path": str(logical_path), "sha256": sha}')
        if not _equal(ref.body[-1], expected_return):
            raise ValueError('Historical source reference must remain logical')
        ref.body[-1] = _statement('return path, {"path": str(path), "sha256": sha}')
        load = function('load_native_structure_package')
        physical = _statement('metadata_path, _ = _reference(proof["source_metadata"], source_resolver=source_resolver)')
        stat = _statement('stat = metadata_path.stat()')
        if sum(_equal(node, physical) for node in load.body) != 1 or sum(_equal(node, stat) for node in load.body) != 1:
            raise ValueError('Native schema cache must use the verified physical metadata stamp')
        load.body = [_statement('stat = Path(proof["source_metadata"]["path"]).stat()') if _equal(node, stat) else node
            for node in load.body if not _equal(node, physical)]
    elif name == 'native_policy_features.py':
        verify = function('verify_primary_kernel_bridge')
        guard = _statement('''if loader_compatibility is not None:
    from .runtime_loader_compatibility import RuntimeLoaderCompatibility
    if type(loader_compatibility) is not RuntimeLoaderCompatibility:
        raise ValueError('An independently verified loader compatibility receipt is required')
    loader_compatibility.validate_unchanged()''')
        if sum(_equal(node, guard) for node in verify.body) != 1:
            raise ValueError('The loader proof must be sealed and independently source-verified')
        verify.body = [node for node in verify.body if not _equal(node, guard)]
        mismatch = _statement('''if loader_compatibility is None:
    raise ValueError('Baseline encoder dependency changed: ' + name)''')
        checked = _statement('loader_compatibility.verify_dependency(name, expected_sha, path)')
        replaced = 0
        for node in ast.walk(verify):
            if isinstance(node, ast.If) and len(node.body) == 2 and _equal(node.body[0], mismatch) and _equal(node.body[1], checked):
                node.body = [_statement("raise ValueError('Baseline encoder dependency changed: ' + name)")]
                replaced += 1
        if replaced != 1:
            raise ValueError('No general source-hash bypass is permitted')
    elif name != 'integrated_exam_bc_features.py':
        raise ValueError('A different frozen encoder dependency cannot use this loader-only bridge')
    return _RemoveResolverPlumbing().visit(tree)


def verify_loader_only_source_changes(frozen_sources, runtime_sources):
    """Entire modules must match after removing the exact I/O-only edits."""
    if set(frozen_sources) != set(runtime_sources):
        raise ValueError('Frozen and runtime dependency sets differ')
    checks = {}
    for name, original in frozen_sources.items():
        old, _ = _read(original); current, _ = _read(runtime_sources[name])
        if name in _CHANGED:
            equal = _equal(ast.parse(old.decode('utf-8')), _normalize_loader_source(name, current))
        else:
            equal = old == current
        if not equal:
            raise ValueError('Numerical code/constants or an unapproved dependency changed: ' + name)
        checks[name] = {'original_sha256': original['sha256'], 'runtime_sha256': runtime_sources[name]['sha256'],
            'loader_plumbing_only': name in _CHANGED, 'numeric_and_other_definitions_equal': True}
    return checks


class RuntimeLoaderCompatibility:
    """Sealed source proof; does not qualify current DLL/game semantics."""
    def __init__(self, reference, body, contract, source_checks, watched, *, _authority=None):
        if _authority is not _SEAL:
            raise ValueError('Use load_runtime_loader_compatibility')
        self._reference = deepcopy(reference)
        self._body, self._trained_contract = deepcopy(body), deepcopy(contract)
        self._checks, self._watched = deepcopy(source_checks), tuple(watched)

    @property
    def trained_feature_contract(self):
        return deepcopy(self._trained_contract)

    @property
    def source_relocations_reference(self):
        return deepcopy(self._body['source_relocations'])

    @property
    def provenance(self):
        self.validate_unchanged()
        return {'schema': SCHEMA, 'receipt': deepcopy(self._reference), 'source_checks': deepcopy(self._checks),
            'trained_feature_contract_sha256': self._trained_contract['contract_sha256'],
            'logical_training_identity_preserved': True, 'numeric_code_and_constants_unchanged': True,
            'current_PC_or_Master_admitted': False, 'model_artifact_rewritten': False}

    def make_source_resolver(self):
        self.validate_unchanged()
        return ArtifactSourceResolver(self._body['source_relocations'])

    def validate_unchanged(self):
        for path, expected in self._watched:
            stat = path.stat()
            if (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns) != expected:
                raise ValueError('Loader compatibility source changed during execution: ' + str(path))

    def verify_dependency(self, name, expected_original_sha, actual_path):
        self.validate_unchanged()
        if (name not in _CHANGED or name not in self._checks
                or self._checks[name]['original_sha256'] != expected_original_sha
                or Path(actual_path).resolve() != Path(self._body['runtime_sources'][name]['path']).resolve()
                or sha256_file(Path(actual_path)) != self._checks[name]['runtime_sha256']):
            raise ValueError('Changed encoder dependency is outside the exact loader-only proof')

    def validate_runtime_contract(self, runtime_contract):
        self.validate_unchanged()
        expected = deepcopy(self._trained_contract)
        expected.pop('contract_sha256')
        for name in expected['source_hashes']:
            expected['source_hashes'][name] = self._checks[name]['runtime_sha256']
        for name in expected['primary_kernel_bridge']['runtime_source_hashes']:
            expected['primary_kernel_bridge']['runtime_source_hashes'][name] = self._checks[name]['runtime_sha256']
        expected['contract_sha256'] = hashlib.sha256(canonical_json_bytes(expected)).hexdigest()
        if canonical_json_bytes(expected) != canonical_json_bytes(runtime_contract):
            raise ValueError('Runtime representation differs beyond verified loader provenance')
        return {'trained_feature_contract_sha256': self._trained_contract['contract_sha256'],
            'runtime_feature_contract_sha256': runtime_contract['contract_sha256'],
            'identical_numeric_representation': True, 'contracts_are_identical': False,
            'runtime_loader_compatibility': deepcopy(self._reference), 'current_PC_admitted': False}


def load_runtime_loader_compatibility(reference):
    raw, stamp = _read(reference); body = json.loads(raw); watched = [stamp]
    if (body.get('schema') != SCHEMA or set(body) != {'schema', 'frozen_sources', 'runtime_sources',
            'trained_feature_contract', 'source_relocations', 'helpers', 'current_PC_admitted'}
            or body['current_PC_admitted'] is not False):
        raise ValueError('Exact loader-only compatibility receipt required')
    raw, stamp = _read(body['trained_feature_contract']); watched.append(stamp)
    trained = json.loads(raw)
    core_hashes = {**trained['primary_kernel_bridge']['runtime_source_hashes'], **trained['source_hashes']}
    if (set(core_hashes) != set(body['frozen_sources']) or set(core_hashes) != set(body['runtime_sources'])
            or any(body['frozen_sources'][name]['sha256'] != sha for name, sha in core_hashes.items())):
        raise ValueError('Frozen source identities differ from the actual trained feature contract')
    source_dir = Path(__file__).resolve().parent
    if any(Path(ref['path']).resolve() != source_dir / name for name, ref in body['runtime_sources'].items()):
        raise ValueError('Runtime proof must inspect the actual imported source directory')
    allowed_helpers = {'artifact_source_resolver.py', 'runtime_loader_compatibility.py', 'runtime_master_source.py'}
    if (not {'artifact_source_resolver.py', 'runtime_loader_compatibility.py'} <= set(body['helpers'])
            or not set(body['helpers']) <= allowed_helpers):
        raise ValueError('Actual resolver and compatibility helper sources must be bound')
    for name, ref in body['helpers'].items():
        if Path(ref['path']).resolve() != source_dir / name:
            raise ValueError('Runtime loader helper identity differs')
    for ref in [*body['frozen_sources'].values(), *body['runtime_sources'].values(), *body['helpers'].values(), body['source_relocations']]:
        _, stamp = _read(ref); watched.append(stamp)
    checks = verify_loader_only_source_changes(body['frozen_sources'], body['runtime_sources'])
    ArtifactSourceResolver(body['source_relocations'])  # Independently verify every declared same-byte destination.
    proof = RuntimeLoaderCompatibility(reference, body, trained, checks, watched, _authority=_SEAL)
    proof.validate_unchanged()
    return proof


__all__ = ['SCHEMA', 'RuntimeLoaderCompatibility', 'load_runtime_loader_compatibility', 'verify_loader_only_source_changes']
