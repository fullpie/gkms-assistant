"""Manual, offline Master change inspection for GUI background workers.

This service inventories the existing imported Master, invokes the existing
Plan2 compiler, and inspects a verified PolicyBundle.  It never downloads or
installs Master data, trains a model, or promotes an artifact.  New effect
shapes remain review items even if their enum name already exists.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import uuid

import yaml

from .master_db import DEFAULT_DATABASE
from .training_artifact_io import atomic_write, canonical_json_bytes


SNAPSHOT_SCHEMA = "gkms.card-data-snapshot.v1"
REPORT_SCHEMA = "gkms.card-data-update.v1"
DEFAULT_OUTPUT_ROOT = DEFAULT_DATABASE.parent / "card_data_updates"
_UPDATE_LOCK = threading.Lock()
_TABLES = (
    "card", "effect", "produce_exam_trigger", "produce_exam_status_enchant",
    "produce_card_search", "produce_item", "produce_item_effect",
    "produce_effect", "produce_drink", "produce_drink_effect", "idol_card",
)
_REQUIRED_TABLES = frozenset(_TABLES[:5])
DEFAULT_SEMANTIC_MASTER_DIR = DEFAULT_DATABASE.parents[1] / "_research" / "gakumasu-diff"
_SEMANTIC_TABLES = {
    "produce_card_customize": ("ProduceCardCustomize.yaml", "customizeCount"),
    "produce_card_grow_effect": ("ProduceCardGrowEffect.yaml", None),
    "produce_exam_gimmick": ("ProduceExamGimmickEffectGroup.yaml", "priority"),
    "produce": ("Produce.yaml", None),
    "produce_audition": ("ProduceStepAuditionDifficulty.yaml", ("produceId", "stepType", "number")),
}
_PRESENTATION = frozenset({
    "name", "asset_id", "assetId", "descriptions_json", "descriptions",
    "description", "produceDescriptions", "produce_descriptions_json",
    "displayOrder", "display_order", "viewStartTime", "view_start_time",
})
_NUMERIC = frozenset({
    "stamina", "cost_value", "costValue", "value1", "value2", "effectValue1",
    "effectValue2", "effect_count", "effect_turn", "effectCount", "effectTurn",
    "effect_value_min", "effect_value_max", "effectValueMin", "effectValueMax",
    "fire_limit", "fire_interval", "fireLimit", "fireInterval",
    "vocal", "dance", "visual", "vocal_growth", "dance_growth", "visual_growth",
    "vocalGrowth", "danceGrowth", "visualGrowth", "upper_search_count",
    "lower_search_count", "upperSearchCount", "lowerSearchCount", "limit_count",
    "limitCount", "stamina_min", "stamina_max", "staminaMin", "staminaMax",
    "field_status_values", "fieldStatusValues", "phase_values", "phaseValues",
})


class CardDataUpdateCancelled(RuntimeError):
    """No baseline or active model was changed by this cancelled job."""


@dataclass(frozen=True, slots=True)
class CardDataUpdateResult:
    status: str
    report_path: Path
    snapshot_path: Path
    summary: Mapping[str, object]


def _hash(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _definition(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _definition(child)
            for key, child in value.items() if key not in _PRESENTATION
        }
    if isinstance(value, list):
        return [_definition(child) for child in value]
    return value


def _shape(value: object, key: str = "") -> object:
    if isinstance(value, Mapping):
        return {
            str(name): _shape(child, str(name))
            for name, child in value.items()
        }
    if isinstance(value, list):
        return [_shape(child, key) for child in value]
    if key in _NUMERIC and isinstance(value, (int, float)) and not isinstance(value, bool):
        return "<numeric>"
    return value


def _snapshot_payload(snapshot: Mapping[str, object]) -> dict[str, object]:
    keys = ("schema", "table_columns", "tables")
    return {key: snapshot[key] for key in (*keys, "semantic_tables") if key in snapshot}


def read_card_data_snapshot(database: str | Path = DEFAULT_DATABASE, *, semantic_master_dir: Path = DEFAULT_SEMANTIC_MASTER_DIR) -> dict[str, object]:
    """Read one SQLite transaction; fingerprints ignore row order and art/text."""
    source = Path(database).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    tables: dict[str, dict[str, object]] = {}
    columns: dict[str, list[str]] = {}
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        available = {
            str(row[0]) for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        missing = _REQUIRED_TABLES - available
        if missing:
            raise ValueError("Master tables missing: " + ",".join(sorted(missing)))
        for table in _TABLES:
            if table not in available:
                continue
            rows: dict[str, object] = {}
            # Names come only from the fixed allowlist above.
            columns[table] = sorted(
                str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')
            )
            for raw in connection.execute(f'SELECT * FROM "{table}"'):
                row = dict(raw)
                identity = row.get("id")
                if not isinstance(identity, str) or not identity:
                    raise ValueError(f"{table} has a missing identity")
                if table == "card":
                    upgrade = row.get("upgrade_count")
                    if type(upgrade) is not int or upgrade < 0:
                        raise ValueError(f"card upgrade is invalid: {identity}")
                    identity = f"{identity}@{upgrade}"
                if identity in rows:
                    raise ValueError(f"duplicate {table} identity: {identity}")
                for name, value in tuple(row.items()):
                    if name.endswith("_json"):
                        try:
                            row[name] = json.loads(value)
                        except (TypeError, json.JSONDecodeError) as error:
                            raise ValueError(f"invalid {table}.{name}: {identity}") from error
                rows[identity] = _definition(row)
            if table in _REQUIRED_TABLES and not rows:
                raise ValueError(f"Master table is empty: {table}")
            tables[table] = dict(sorted(rows.items()))
    snapshot: dict[str, object] = {
        "schema": SNAPSHOT_SCHEMA,
        "table_columns": columns,
        "tables": tables,
    }
    # These official rows are not imported into SQLite. Freeze their actual
    # definitions alongside it; inference must never reopen current YAML.
    semantic_tables = {}
    for table, (filename, suffix) in _SEMANTIC_TABLES.items():
        path = Path(semantic_master_dir) / filename
        rows = yaml.load(path.read_text(encoding="utf-8"), Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))
        if not isinstance(rows, list):
            raise ValueError(f"invalid semantic Master table: {filename}")
        indexed = {}
        for raw in rows:
            if not isinstance(raw, Mapping) or not isinstance(raw.get("id"), str):
                raise ValueError(f"invalid semantic Master row: {filename}")
            identity = (raw["id"] if suffix is None else
                        "@".join((raw["id"], *(str(raw[key]) for key in suffix))) if isinstance(suffix, tuple) else
                        f"{raw['id']}@{raw[suffix]}")
            if identity in indexed:
                raise ValueError(f"duplicate semantic Master identity: {filename}:{identity}")
            indexed[identity] = _definition(raw)
        semantic_tables[table] = indexed
    snapshot["semantic_tables"] = semantic_tables
    snapshot["fingerprint"] = _hash(_snapshot_payload(snapshot))
    snapshot["source_database"] = str(source)
    return snapshot


def load_card_data_snapshot(path: str | Path) -> dict[str, object]:
    snapshot = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(snapshot, dict) or snapshot.get("schema") != SNAPSHOT_SCHEMA:
        raise ValueError("unsupported card data snapshot schema")
    if snapshot.get("fingerprint") != _hash(_snapshot_payload(snapshot)):
        raise ValueError("card data snapshot fingerprint mismatch")
    return snapshot


def _effect_types(tables: Mapping[str, object]) -> set[str]:
    return {
        str(row["effect_type"])
        for table in ("effect", "produce_item_effect", "produce_effect")
        for row in tables.get(table, {}).values()
        if row.get("effect_type")
    }


def compare_card_data(
    previous: Mapping[str, object] | None,
    current: Mapping[str, object],
) -> dict[str, object]:
    """Classify numeric-only deltas conservatively; shape changes need review.

    Link, ordering, target, trigger, enum and unrecognized-field changes are
    semantic changes.  Numeric does not mean harmless or model-compatible.
    """
    new_tables = {**current["tables"], **current.get("semantic_tables", {})}
    if previous is None:
        return {
            "status": "initial-inventory", "previous_fingerprint": None,
            "current_fingerprint": current["fingerprint"], "changes": [],
            "counts": {}, "new_effect_types": [], "schema_changed": False,
            "requires_model_validation": True,
        }
    old_tables = {**previous["tables"], **previous.get("semantic_tables", {})}
    changes: list[dict[str, object]] = []
    for table in sorted(set(old_tables) | set(new_tables)):
        old_rows = old_tables.get(table, {})
        new_rows = new_tables.get(table, {})
        for identity in sorted(set(old_rows) | set(new_rows)):
            before, after = old_rows.get(identity), new_rows.get(identity)
            if before == after:
                continue
            if before is None:
                category = "added"
            elif after is None:
                category = "removed"
            elif _shape(before) == _shape(after):
                category = "numeric-change"
            else:
                category = "semantic-change"
            changes.append({"table": table, "id": identity, "category": category})
    schema_changed = previous["table_columns"] != current["table_columns"]
    return {
        "status": "changed" if changes or schema_changed else "unchanged",
        "previous_fingerprint": previous["fingerprint"],
        "current_fingerprint": current["fingerprint"],
        "changes": changes,
        "counts": dict(sorted(Counter(str(row["category"]) for row in changes).items())),
        "new_effect_types": sorted(_effect_types(new_tables) - _effect_types(old_tables)),
        "schema_changed": schema_changed,
        "semantic_coverage_added": sorted(set(current.get("semantic_tables", {})) - set(previous.get("semantic_tables", {}))),
        "requires_model_validation": bool(changes or schema_changed),
    }


def inspect_bundle_card_compatibility(
    fingerprint: str,
    *,
    bundle_manifest: str | Path | None = None,
) -> dict[str, object]:
    """Use PolicyBundle's model/report validation; never invent training scope."""
    from .policy_bundle import PolicyBundle

    try:
        bundle = PolicyBundle.load(None if bundle_manifest is None else Path(bundle_manifest))
    except (OSError, ValueError, KeyError) as error:
        return {"status": "unavailable", "reason": f"{type(error).__name__}: {error}"}
    contract = bundle.payload.get("card_data_contract")
    if not isinstance(contract, Mapping):
        status = "unbound"
        expected = None
    else:
        expected = contract.get("snapshot_fingerprint")
        status = "matched" if expected == fingerprint else "mismatch"
    return {
        "status": status, "bundle_id": bundle.bundle_id,
        "manifest_path": str(bundle.manifest_path),
        "expected_fingerprint": expected, "actual_fingerprint": fingerprint,
        "activation_changed": False,
        "generalization_verified": False,
    }


def run_card_data_update(
    *,
    database: str | Path = DEFAULT_DATABASE,
    output_root: str | Path = DEFAULT_OUTPUT_ROOT,
    baseline_path: str | Path | None = None,
    bundle_manifest: str | Path | None = None,
    validate_plan2: bool = True,
    runtime_acceptance: bool = False,
    progress: Callable[[str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> CardDataUpdateResult:
    """Run in a GUI worker; only update reports and a first-use baseline.

    Later differences never silently replace the comparison baseline.  The
    caller can explicitly choose a reviewed snapshot as ``baseline_path``.
    Compiler coverage is Plan2 only; no new model is produced by this job.
    """
    if runtime_acceptance and not validate_plan2:
        raise ValueError("runtime acceptance requires Plan2 compilation")
    if not _UPDATE_LOCK.acquire(blocking=False):
        raise RuntimeError("a card data update is already running")
    try:
        def checkpoint(stage: str) -> None:
            if cancelled is not None and cancelled():
                raise CardDataUpdateCancelled(stage)
            if progress is not None:
                progress(stage)

        root = Path(output_root).resolve()
        baseline = Path(baseline_path) if baseline_path is not None else root / "baseline.json"
        checkpoint("讀取目前卡片資料")
        current = read_card_data_snapshot(database)
        if baseline_path is not None and not baseline.is_file():
            raise FileNotFoundError(baseline)
        previous = load_card_data_snapshot(baseline) if baseline.is_file() else None
        difference = compare_card_data(previous, current)
        checkpoint("檢查目前模型與資料版本")
        bundle = inspect_bundle_card_compatibility(
            str(current["fingerprint"]), bundle_manifest=bundle_manifest,
        )
        validation: dict[str, object] = {"status": "not-run", "scope": "Common+Plan2"}
        blockers: list[str] = []
        if validate_plan2:
            checkpoint("編譯卡片效果並檢查涵蓋範圍")
            from .plan2_native_program_catalog import compile_plan2_native_program_catalog

            try:
                compilation = compile_plan2_native_program_catalog(database=database)
                validation = {
                    "status": "compiled" if compilation.fully_compiled else "blocked",
                    "scope": "Common+Plan2", "compilation": compilation.to_dict(),
                }
                if not compilation.fully_compiled:
                    blockers.append("plan2-compilation-incomplete")
                elif runtime_acceptance:
                    checkpoint("驗證卡片合法動作與執行結果")
                    from .plan2_native_full_catalog_acceptance import run_plan2_full_catalog_acceptance

                    matrix = run_plan2_full_catalog_acceptance(
                        compilation=compilation, strict_runtime_failures=False,
                    )
                    validation["runtime_acceptance"] = matrix.to_dict()
                    validation["status"] = "accepted" if matrix.accepted else "blocked"
                    if not matrix.accepted:
                        blockers.append("plan2-runtime-acceptance-failed")
            except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as error:
                validation = {
                    "status": "blocked", "scope": "Common+Plan2",
                    "error": f"{type(error).__name__}: {error}",
                }
                blockers.append("plan2-validation-error")
        checkpoint("確認資料版本並儲存更新報告")
        if read_card_data_snapshot(database)["fingerprint"] != current["fingerprint"]:
            blockers.append("master-changed-during-validation")
        if difference["new_effect_types"] or difference["schema_changed"]:
            blockers.append("new-master-semantics-require-review")
        counts = difference["counts"]
        needs_review = difference["status"] == "changed"
        status = "blocked" if blockers else (
            "review-required" if needs_review else (
                "baseline-created" if previous is None else "unchanged"
            )
        )
        summary: dict[str, object] = {
            "status": status, "card_versions": len(current["tables"]["card"]),
            "changes": counts, "blockers": blockers,
            "bundle_compatibility": bundle["status"],
            "model_created": False, "active_model_changed": False,
        }
        report = {
            "schema": REPORT_SCHEMA, "summary": summary, "difference": difference,
            "validation": validation, "bundle": bundle,
            "scope": "imported Master card/effect definitions; account inventory is separate",
            "limitations": [
                "Numeric changes still require strategy and model validation.",
                "Known effect enum names do not prove new effect shapes are supported.",
                "Plan1/Plan3 and account support/memory coverage are not validated by this job.",
                "This job does not download data, train, select, or activate a model.",
            ],
        }
        snapshot_path = root / "snapshots" / f"{current['fingerprint']}.json"
        report_path = root / "jobs" / uuid.uuid4().hex / "report.json"
        atomic_write(snapshot_path, canonical_json_bytes(current) + b"\n")
        atomic_write(report_path, canonical_json_bytes(report) + b"\n")
        if previous is None and baseline_path is None and not blockers:
            atomic_write(baseline, canonical_json_bytes(current) + b"\n")
        atomic_write(root / "latest_report.json", canonical_json_bytes({
            "schema": REPORT_SCHEMA, "report_path": str(report_path),
            "snapshot_path": str(snapshot_path), "summary": summary,
        }) + b"\n")
        return CardDataUpdateResult(status, report_path, snapshot_path, summary)
    finally:
        _UPDATE_LOCK.release()


__all__ = [
    "CardDataUpdateCancelled", "CardDataUpdateResult", "compare_card_data",
    "inspect_bundle_card_compatibility", "load_card_data_snapshot",
    "read_card_data_snapshot", "run_card_data_update",
]
