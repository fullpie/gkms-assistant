"""Pure compression of one already-filtered entity's host semantic tokens.

This is not a raw-observation/privacy filter or a game simulator. Callers own
entity boundaries, visibility review, source binding and exact token coverage.
Feature hashing is a lossy representation, not a native-state serialization.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
import hashlib
import math
import re

from .contracts import ContractError

VERSION = "gkms.rl.semantic-entity-hash.v1"
_WIDTHS = {"cat": 256, "num": 128, "presence": 128}
ENTITY_FEATURE_NAMES = tuple(f"{channel}_{index:03d}" for channel, width in _WIDTHS.items()
                             for index in range(width))

# These are categorical native codes, not magnitudes. Do not infer that every
# integer, index, key or field containing "type" is an enum.
_ENUM_FIELDS = frozenset({
    "effectType", "targetExamEffectType", "costType", "triggerType",
    "movePositionType", "playMovePositionType", "playCardPositionType", "cardPositionType", "positionType",
    "phase", "phaseType", "playType", "planType", "mainEffectType", "displayMainEffectType", "stepType", "examType",
    "pickRangeType", "pickRangeType2", "pickCountType", "pickCountType2",
    "startEnchantOriginType", "originType", "targetType", "overrideType", "currentType",
    "native_cost_type", "filterParameterType", "itemType",
})
_ENUM_LISTS = frozenset({"turnStatusParameterTypeList"})
_NUMBER = re.compile(r"^(?P<prefix>(?:.*:)?)n:(?P<path>.+):exact:(?P<value>[^:]+)$")
_DERIVED = re.compile(r"(?:^|:)n:.+:(?:coarse|log2|sign):[^:]+$")
_NATIVE = re.compile(r"^(?P<path>.+):(?P<kind>int|float|list):(?P<value>[^:]+)$")
_COUNT = re.compile(r"^(?P<prefix>(?:.*:)?)(?P<kind>count|class-count):(?P<path>.+):(?P<value>[^:]+)$")
_NULL = re.compile(r"^(?P<path>.+):observed-null$")
_MISSING = re.compile(r"^(?P<prefix>(?:.*:)?)missing:(?P<path>.+)$")
_CATEGORY_MARKER = re.compile(r"(?:^|:)(?:s|b|str|bool|enum):")


def _enum_path(path: str) -> bool:
    leaf = re.split(r"[.:]", path)[-1].lstrip("_")
    if leaf in _ENUM_FIELDS:
        return True
    indexed = re.fullmatch(r"([^\[]+)\[\d+\]", leaf)
    if indexed and indexed[1].lstrip("_") in _ENUM_LISTS:
        return True
    return leaf == "key" and any(part.lstrip("_") == "phaseCountDictionary"
                                  for part in re.split(r"[.:]", path))


def _number(value: str, *, kind: str, path: str) -> float:
    try:
        if kind in ("int", "list", "count", "class-count"):
            if re.fullmatch(r"[+-]?\d+", value) is None:
                raise ValueError("integer token required")
            result = float(int(value))
        elif kind == "float":
            # _emit uses float.hex(); accepting decimal here would reinterpret
            # e.g. "1.5" as hexadecimal and silently change the observed value.
            if not re.fullmatch(r"[+-]?0x[0-9a-fA-F]+(?:\.[0-9a-fA-F]*)?p[+-]?\d+", value):
                raise ValueError("hexadecimal float token required")
            result = float.fromhex(value)
        else:
            result = float(value)
    except (ValueError, OverflowError) as error:
        raise ContractError("invalid semantic number at " + path) from error
    if not math.isfinite(result):
        raise ContractError("nonfinite semantic number at " + path)
    if kind in ("list", "count", "class-count") and result < 0:
        raise ContractError("negative semantic cardinality at " + path)
    return result


def _bucket(channel: str, key: str) -> tuple[str, float]:
    raw = hashlib.sha256((VERSION + "\0" + channel + "\0" + key).encode("utf-8")).digest()
    index = int.from_bytes(raw[:8], "big") % _WIDTHS[channel]
    return f"{channel}_{index:03d}", -1.0 if raw[8] & 1 else 1.0


def encode_semantic_tokens(tokens: Iterable[str]) -> dict[str, float]:
    """Encode ONE entity, preserving numeric magnitude independently of keys.

    Numeric coordinates retain their source namespace/path and unit (number or
    cardinality); the value never chooses a bucket. Booleans/enums/strings stay
    categorical. Derived host magnitude bins are redundant and omitted.
    Missing/null and observed numeric zero have distinct presence features.
    Unknown token grammars remain categorical; they are not visibility approval.
    """
    if isinstance(tokens, (str, bytes, Mapping)) or not isinstance(tokens, Iterable):
        raise ContractError("one entity requires an iterable of semantic tokens")
    categories, presences, numbers = set(), set(), {}
    for token in tokens:
        if type(token) is not str or not token:
            raise ContractError("nonempty semantic token text required")
        # String payloads may themselves contain ':int:' or ':n:'. They are
        # never reinterpreted as a numeric instruction or a derived token.
        if _CATEGORY_MARKER.search(token):
            categories.add(token)
            continue
        if "native-magnitude:" in token or _DERIVED.search(token):
            continue
        null = _NULL.fullmatch(token)
        missing = _MISSING.fullmatch(token)
        if null or missing:
            path = null["path"] if null else missing["prefix"] + missing["path"]
            presences.add(("null" if null else "missing") + "\0" + path)
            continue
        native, numeric, count = _NATIVE.fullmatch(token), _NUMBER.fullmatch(token), _COUNT.fullmatch(token)
        if native:
            path, kind, raw = native["path"], native["kind"], native["value"]
        elif numeric:
            path, kind, raw = numeric["prefix"] + numeric["path"], "exact", numeric["value"]
        elif count:
            path, kind, raw = count["prefix"] + count["path"], count["kind"], count["value"]
            if kind == "class-count":
                path = count["prefix"] + "class-count." + count["path"]
        else:
            categories.add(token)
            continue
        value = _number(raw, kind=kind, path=path)
        if kind not in ("list", "count", "class-count") and _enum_path(path):
            categories.add("enum\0" + path + "\0" + format(value, ".17g"))
            continue
        unit = "cardinality" if kind in ("list", "count", "class-count") else "number"
        coordinate = unit + "\0" + path
        if coordinate in numbers and numbers[coordinate] != value:
            raise ContractError("conflicting semantic scalar tokens at " + path)
        numbers[coordinate] = value
        presences.add("observed\0" + coordinate)
        if kind == "list":
            categories.add("shape:list\0" + path)
    buckets = defaultdict(list)
    for key in sorted(categories):
        name, sign = _bucket("cat", key)
        buckets[name].append(sign)
    for key in sorted(presences):
        name, sign = _bucket("presence", key)
        buckets[name].append(sign)
    for key, value in sorted(numbers.items()):
        name, sign = _bucket("num", key)
        magnitude = math.copysign(math.log1p(abs(value)), value)
        buckets[name].append(sign * magnitude)
    result = {key: math.fsum(values) for key, values in sorted(buckets.items())}
    if any(not math.isfinite(value) for value in result.values()):
        raise ContractError("semantic feature accumulation overflow")
    return {key: value for key, value in result.items() if value != 0.0}


__all__ = ["VERSION", "ENTITY_FEATURE_NAMES", "encode_semantic_tokens"]
