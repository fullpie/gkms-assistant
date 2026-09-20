"""Export ten verified current-Master tables, independently of frozen model assets."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from gkms_tool.passive_catalog import _load_rows
from gkms_tool.portable_loadout_assets import SCHEMA, MASTER_HASH, SOURCE_ARCHIVE_SHA256, MODEL_MANIFEST_SHA256, TABLES

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "var/research/public_delivery_20260920/master_12b_current_audit_v1/source_12b"
MODEL = ROOT / "var/research/public_delivery_20260920/portable_models_v5_12b/manifest.json"
OUTPUT = ROOT / "var/research/public_delivery_20260920/loadout_assets_v2_12b/assets/loadout"


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def encode(value):
    # Preserve source row/key order and exact JSON scalar types.
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode("utf8")


def export(source=SOURCE, model_manifest=MODEL, output=OUTPUT):
    source, model_manifest, output = Path(source), Path(model_manifest), Path(output)
    archive_raw, model_raw = (source / "manifest.json").read_bytes(), model_manifest.read_bytes()
    if sha(archive_raw) != SOURCE_ARCHIVE_SHA256 or sha(model_raw) != MODEL_MANIFEST_SHA256:
        raise ValueError("Retained Master or model manifest differs from its verified identity")
    archive, model = json.loads(archive_raw), json.loads(model_raw)
    if (archive["master_hash"] != MASTER_HASH or archive["upstream_commit_subject"] != MASTER_HASH
            or model["master_hash"] != MASTER_HASH):
        raise ValueError("Loadout Master differs from the frozen model runtime Master")
    if output.exists():
        raise FileExistsError("Preserve the existing loadout export; use a new output")
    files, tables = {}, {}
    for name in TABLES:
        path = source / "yaml" / (name + ".yaml")
        before = path.read_bytes()
        if sha(before) != archive["yaml_sha256"][path.name]:
            raise ValueError("Retained Master table changed: " + name)
        rows = _load_rows(path)
        if path.read_bytes() != before:
            raise ValueError("Retained Master table changed during parsing: " + name)
        raw = encode(rows)
        if json.loads(raw) != rows:
            raise ValueError("Master row scalar types are not JSON-lossless: " + name)
        files[name + ".json"] = raw
        tables[name] = {"file": name + ".json", "sha256": sha(raw), "bytes": len(raw), "rows": len(rows),
                        "source_yaml_sha256": sha(before)}
    manifest = {"schema": SCHEMA, "master_hash": MASTER_HASH, "source_archive_sha256": SOURCE_ARCHIVE_SHA256,
        "model_manifest_sha256": MODEL_MANIFEST_SHA256,
        "upstream": {"url": archive["upstream_url"], "commit": archive["upstream_commit"], "commit_subject": MASTER_HASH},
        "tables": tables, "source_rows_unchanged": True, "scoring_rules_changed": False,
        "model_assets_modified": False, "contains_account_or_replay_data": False}
    files["manifest.json"] = encode(manifest)
    files["NOTICE.txt"] = ("GKMS loadout static Master inputs\n"
        "Source: " + archive["upstream_url"] + "\nCommit: " + archive["upstream_commit"] + "\nMaster: " + MASTER_HASH + "\n"
        "Only the ten tables consumed by the existing passive/loadout catalog are included.\n"
        "Rows, scalar values and ordering are preserved; no account data or replay examples are included.\n"
        "This provenance notice does not supply a separate license grant for the upstream Master data.\n").encode("utf8")
    output.mkdir(parents=True)
    for name, raw in files.items():
        (output / name).write_bytes(raw)
    report = {"schema": "gkms.portable-loadout-export-audit.v1", "output": str(output.resolve()),
        "manifest_sha256": sha(files["manifest.json"]), "master_hash": MASTER_HASH,
        "model_manifest_sha256": sha(model_manifest.read_bytes()), "source_archive_sha256": sha((source / "manifest.json").read_bytes()),
        "tables": len(tables), "rows": sum(row["rows"] for row in tables.values()), "bytes": sum(len(raw) for raw in files.values()),
        "files": {name: sha(raw) for name, raw in files.items()}, "game_io": False, "training_admission_changed": False}
    output.parent.parent.joinpath("export_audit.json").write_bytes(encode(report))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--model-manifest", type=Path, default=MODEL)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    print(json.dumps(export(args.source, args.model_manifest, args.output), indent=2))
