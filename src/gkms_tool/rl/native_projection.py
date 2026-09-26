"""Checked original-PC snapshot -> existing EntityBatch information schema.

This is a declarative conversion engine, NOT a shipped/qualified game feature
map. A source-reviewed specification must account for every surviving leaf;
new card/status fields are errors, never silently dropped. Native reference
numbers are resolved locally and are never embedded. Unsupported reference
cycles/identity-dependent aliasing stay blocked. No game rules are simulated.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

from .contracts import Binding, ContractError, DecisionKind, FrozenJSON, Scope, State, digest, finite, sha256, text
from .features import FeatureSchema

VERSION = "gkms.rl.native-projection.v1"
SNAPSHOT = "gkms.original-pc-machine-state-snapshot.v1"
# These require a separately reviewed information/identity representation. The
# generic numeric mapper must not turn opaque IDs or a future RNG into inputs.
FORBIDDEN = frozenset({"random", "random_raw", "futureDeckList", "references", "rid",
    "_guid", "guid", "_uid", "_sourceUid", "_ownerUid", "_statusUid", "_fixedDeckOrder",
    "owned_object_identity", "position_object_identity", "instance_key", "card_guid",
    "pointer_binding", "engine_identity", "observed_terminal_score", "policy_source"})
_MISSING = object()


def _path(raw, *, empty=False):
    if type(raw) is not list or (not raw and not empty) or any(type(k) is not str or not k for k in raw):
        raise ContractError("projection path must be a literal string-key array")
    return tuple(raw)


def _input_path(raw, *, empty=False):
    value = _path(raw, empty=empty)
    if any(k in FORBIDDEN or k == "*" for k in value):
        raise ContractError("hidden/identity/label field is not a numeric model input")
    return value


def _matches(pattern, path):
    return len(pattern) <= len(path) and all(a == "*" or a == b for a, b in zip(pattern, path))


def _leaves(value, prefix=()):
    if type(value) is dict and value:
        return {p for k, v in value.items() for p in _leaves(v, prefix + (k,))}
    if type(value) is list and value:
        return {p for i, v in enumerate(value) for p in _leaves(v, prefix + (str(i),))}
    return {prefix}


def _lookup(value, path):
    for key in path:
        if type(value) is not dict or key not in value:
            return _MISSING
        value = value[key]
    return value


@dataclass(frozen=True, slots=True)
class ProjectionSpec:
    features: FeatureSchema
    rules: FrozenJSON

    def __post_init__(self):
        if not isinstance(self.features, FeatureSchema) or not isinstance(self.rules, FrozenJSON):
            raise ContractError("typed feature schema and immutable rules required")
        r = self.rules.unpack()
        if type(r) is not dict or set(r) != {"version", "global", "entities", "exclude", "routes", "shared_reference_proof"}:
            raise ContractError("projection specification fields differ")
        if r["version"] != VERSION:
            raise ContractError("unsupported native projection version")
        if type(r["global"]) is not dict or set(r["global"]) != set(self.features.global_names):
            raise ContractError("every global feature needs an explicit mapping")
        if type(r["entities"]) is not list or len(r["entities"]) > 128:
            raise ContractError("bounded entity mapping list required")
        for fields in [r["global"], *[e.get("fields") for e in r["entities"] if type(e) is dict]]:
            self._fields(fields)
        roots = set()
        for entity in r["entities"]:
            if type(entity) is not dict or set(entity) != {"path", "type", "zone", "definition", "fields"}:
                raise ContractError("entity mapping fields differ")
            path = _input_path(entity["path"])
            if path in roots:
                raise ContractError("one entity mapping per source collection")
            roots.add(path)
            _input_path(entity["definition"])
            if entity["type"] not in self.features.entity_types or entity["zone"] not in self.features.zones:
                raise ContractError("entity type/zone not in feature schema")
            if set(entity["fields"]) - set(self.features.entity_names):
                raise ContractError("entity feature not in schema")
        if type(r["exclude"]) is not list or len(r["exclude"]) > 1024:
            raise ContractError("bounded exclusion rules required")
        for exclusion in r["exclude"]:
            if type(exclusion) is not dict or set(exclusion) != {"path", "reason", "evidence_sha256"}:
                raise ContractError("every exclusion requires path, reason and source evidence")
            _path(exclusion["path"])
            text(exclusion["reason"], "exclusion reason")
            sha256(exclusion["evidence_sha256"], "exclusion evidence")
        if r["shared_reference_proof"] is not None:
            sha256(r["shared_reference_proof"], "shared reference proof")
        routes = r["routes"]
        if type(routes) is not dict or set(routes) != {"flows", "modes", "stages"}:
            raise ContractError("explicit flow/mode/stage routes required")
        for name, width, allowed in (("flows", 3, self.features.flows), ("modes", 2, self.features.modes),
                                    ("stages", 2, self.features.stages)):
            rows = routes[name]
            if type(rows) is not list or not rows or any(type(row) is not list or len(row) != width for row in rows):
                raise ContractError("invalid scope route table")
            if len({tuple(row[:-1]) for row in rows}) != len(rows) or any(row[-1] not in allowed for row in rows):
                raise ContractError("duplicate or unrepresented scope route")
            for row in rows:
                if name == "modes":
                    text(row[0], "native mode")
                elif any(type(x) is not int for x in row[:-1]):
                    raise ContractError("native flow/stage enums must be integers")

    @staticmethod
    def _fields(fields):
        if type(fields) is not dict:
            raise ContractError("explicit numeric field mappings required")
        for name, field in fields.items():
            text(name, "feature name")
            if type(field) is not dict or set(field) != {"path", "kind", "optional"}:
                raise ContractError("numeric mapping fields differ")
            _input_path(field["path"], empty=True)
            if field["kind"] not in {"number", "boolean"} or type(field["optional"]) is not bool:
                raise ContractError("explicit scalar type and optional flag required")

    @property
    def identity(self):
        return digest({"features": asdict(self.features), "rules": self.rules.unpack()})

    @property
    def information_policy_id(self):
        return VERSION + ":" + self.identity

    @classmethod
    def from_dict(cls, value):
        if type(value) is not dict or set(value) != {"features", "rules"}:
            raise ContractError("invalid projection document")
        return cls(FeatureSchema.from_dict(value["features"]), FrozenJSON.of(value["rules"]))


class _Resolver:
    def __init__(self, raw, exclusions, *, max_nodes, max_depth):
        refs = raw.get("references", {})
        if type(refs) is not dict or set(refs) - {"version", "RefIds"} or refs.get("version", 2) != 2:
            raise ContractError("unsupported native reference table")
        rows = refs.get("RefIds", [])
        if type(rows) is not list or len(rows) > max_nodes:
            raise ContractError("reference table exceeds budget")
        self.table = {}
        for row in rows:
            if (type(row) is not dict or set(row) != {"rid", "type", "data"} or type(row["rid"]) is not int
                    or row["rid"] < 0 or row["rid"] in self.table or type(row["data"]) is not dict):
                raise ContractError("invalid/duplicate native reference row")
            typ = row["type"]
            if type(typ) is not dict or set(typ) != {"class", "ns", "asm"} or any(type(v) is not str for v in typ.values()):
                raise ContractError("native reference type missing")
            self.table[row["rid"]] = row
        self.exclusions, self.nodes, self.limit, self.depth = exclusions, 0, max_nodes, max_depth
        self.used, self.shared, self.excluded = set(), set(), set()

    def resolve(self, value, path=(), active=frozenset()):
        self.nodes += 1
        if self.nodes > self.limit or len(path) > self.depth or len(active) > self.depth:
            raise ContractError("native graph traversal exceeds budget")
        if any(_matches(tuple(e["path"]), path) for e in self.exclusions):
            self.excluded.add(path)
            return _MISSING
        if type(value) is dict:
            if "$unqualified" in value or "$native_type" in value:
                raise ContractError("unqualified or reserved native graph field")
            if "rid" in value:
                if set(value) != {"rid"} or type(value["rid"]) is not int or value["rid"] not in self.table:
                    raise ContractError("invalid/dangling native reference")
                rid = value["rid"]
                if rid in active:
                    raise ContractError("cyclic reference needs an explicit graph feature representation")
                if rid in self.used:
                    self.shared.add(rid)
                self.used.add(rid)
                row = self.table[rid]
                resolved = self.resolve(row["data"], path, active | {rid})
                if resolved is _MISSING:
                    return resolved
                typ = row["type"]
                resolved["$native_type"] = typ["asm"] + ":" + typ["ns"] + "." + typ["class"]
                return resolved
            result = {}
            for key, child in value.items():
                if key == "references" and not path:
                    continue
                item = self.resolve(child, path + (key,), active)
                if item is not _MISSING:
                    result[key] = item
            return result
        if type(value) is list:
            result = []
            for i, child in enumerate(value):
                item = self.resolve(child, path + (str(i),), active)
                if item is _MISSING:
                    raise ContractError("cannot exclude a whole list item and silently change cardinality")
                result.append(item)
            return result
        return value


def _map_fields(value, fields, prefix, consumed):
    result = {}
    for name, field in fields.items():
        path = tuple(field["path"])
        raw = _lookup(value, path)
        if raw is _MISSING or raw is None:
            if not field["optional"]:
                raise ContractError("required native feature missing: " + name)
            result[name] = None
        elif field["kind"] == "boolean":
            if type(raw) is not bool:
                raise ContractError("native feature must be a boolean: " + name)
            result[name] = float(raw)
        else:
            result[name] = finite(raw, name)
        if raw is not _MISSING:
            consumed.add(prefix + path)
    return result


@dataclass(frozen=True, slots=True)
class ProjectionResult:
    state: State
    audit: FrozenJSON


def project_snapshot(snapshot, spec: ProjectionSpec, *, expected_state_sha256, scope: Scope,
                     session_generation: str, revision: str, kind: DecisionKind, binding: Binding,
                     max_nodes=100_000, max_depth=64) -> ProjectionResult:
    """Convert a source-bound snapshot, rejecting unaccounted semantics.

    Caller must obtain expected_state_sha256 from the already validated original
    report/journal and separately qualify the source assets/specification. A hash
    match is integrity evidence, NOT game feature or model-quality acceptance.
    Secondary source/context composition is intentionally not accepted here yet.
    """
    sha256(expected_state_sha256, "trusted native state")
    if type(max_nodes) is not int or not 0 < max_nodes <= 1_000_000 or type(max_depth) is not int or not 0 < max_depth <= 128:
        raise ContractError("bounded traversal limits required")
    if not isinstance(spec, ProjectionSpec) or not isinstance(scope, Scope) or not isinstance(binding, Binding):
        raise ContractError("typed projection specification/scope/binding required")
    frozen = FrozenJSON.of(snapshot).unpack()
    if (type(frozen) is not dict or frozen.get("schema") != SNAPSHOT
            or frozen.get("mode") != "direct-native-projection" or frozen.get("state_complete") is not True
            or frozen.get("pure") is not True or frozen.get("read_errors") != [] or frozen.get("unqualified") != []):
        raise ContractError("complete original-PC snapshot required")
    raw = frozen.get("state")
    if type(raw) is not dict or frozen.get("state_sha256") != expected_state_sha256 or digest(raw) != expected_state_sha256:
        raise ContractError("native state bytes/hash mismatch")
    if kind not in {DecisionKind.MAIN, DecisionKind.TERMINAL}:
        raise ContractError("secondary/outer projection needs its own complete context mapping")
    if type(raw.get("isExamEndComplete")) is not bool or (kind is DecisionKind.TERMINAL) != raw["isExamEndComplete"]:
        raise ContractError("native terminal marker differs from boundary")
    if kind is DecisionKind.MAIN and (type(raw.get("phase")) is not int or raw["phase"] != 6):
        raise ContractError("primary projection requires the native Main phase")
    if binding.feature_schema_sha256 != spec.features.identity or binding.information_policy_id != spec.information_policy_id:
        raise ContractError("feature/information-policy binding differs")
    r = spec.rules.unpack()
    for keys, family, expected in ((('planType', 'mainEffectType'), "flows", scope.flow_id),
                                  (('produceId',), "modes", scope.mode_id), (('stepType',), "stages", scope.stage_id)):
        actual = [raw.get(k) for k in keys]
        matches = [row[-1] for row in r["routes"][family] if actual == row[:-1]
                   and all(type(v) is type(w) for v, w in zip(actual, row[:-1]))]
        if matches != [expected]:
            raise ContractError("native scope differs from declared " + family)
    resolver = _Resolver(raw, r["exclude"], max_nodes=max_nodes, max_depth=max_depth)
    view = resolver.resolve(raw)
    if view is _MISSING:
        raise ContractError("cannot exclude the whole native state")
    if resolver.shared and r["shared_reference_proof"] is None:
        raise ContractError("shared reference identity needs a reviewed semantics contract")
    consumed = {(k,) for k in ('planType', 'mainEffectType', 'produceId', 'stepType')}
    globals_ = _map_fields(view, r["global"], (), consumed)
    entities = []
    for rule in r["entities"]:
        path = tuple(rule["path"])
        rows = _lookup(view, path)
        if type(rows) is not list:
            raise ContractError("entity collection missing/null; do not invent an empty list")
        if not rows:
            consumed.add(path)
        for i, row in enumerate(rows):
            prefix = path + (str(i),)
            definition = _lookup(row, tuple(rule["definition"]))
            if definition not in spec.features.vocabulary:
                raise ContractError("native definition absent from the reviewed vocabulary")
            consumed.add(prefix + tuple(rule["definition"]))
            fields = _map_fields(row, rule["fields"], prefix, consumed)
            entities.append({"type": rule["type"], "zone": rule["zone"], "definition_id": definition,
                             "count": 1, "values": fields})
            if len(entities) > spec.features.max_entities:
                raise ContractError("native entity count exceeds budget; never truncate")
    unmapped = _leaves(view) - consumed
    if unmapped:
        # Paths, never raw private values, are returned to aid a source review.
        examples = ["/".join(p) for p in sorted(unmapped)[:12]]
        raise ContractError("unmapped native fields: " + "; ".join(examples))
    # No ordinal from a shuffled deck enters the network. Counts and zones stay.
    from .contracts import canonical
    entities.sort(key=canonical)
    info = FrozenJSON.of({"schema": spec.features.identity, "global": globals_, "entities": entities})
    state = State(scope, session_generation, revision, kind, info, binding.identity)
    return ProjectionResult(state, FrozenJSON.of({"schema": VERSION, "spec_sha256": spec.identity,
        "source_state_sha256": expected_state_sha256, "feature_sha256": info.sha256,
        "excluded_paths": [list(p) for p in sorted(resolver.excluded)],
        "reference_nodes_used": len(resolver.used), "shared_reference_count": len(resolver.shared),
        "entity_count": len(entities), "unmapped_fields": [], "source_bytes_unchanged": True,
        "native_semantics_qualified": False, "training_admitted": False, "live_enabled": False}))
