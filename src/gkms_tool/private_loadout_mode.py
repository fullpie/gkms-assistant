"""Private next-run loadout preference; no game ownership or silent fallback."""
from __future__ import annotations

import json
import hashlib
from functools import lru_cache
from pathlib import Path

from .application_paths import app_root, state_root, public_installation
from .loadout_snapshot import _atomic_write_json

LEGACY = "game_and_rules"
RL = "shared_offline_rl"
MODES = (LEGACY, RL)
_UNSET = object()


def preference_path(root=None):
    return state_root(root) / "account_inventory" / "recommendation_mode.json"


def load_recommendation_mode(*, root=None):
    path = preference_path(root)
    if not path.is_file():
        if public_installation(root):
            try:
                private_rl_descriptor(root=root)
            except (OSError, ValueError, KeyError, TypeError):
                return LEGACY
            return RL
        return LEGACY
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != "gkms.private-loadout-mode.v1" or data.get("mode") not in MODES:
        raise ValueError("編成推薦設定無法確認，請重新選擇推薦方式。")
    return data["mode"]


def save_recommendation_mode(mode, *, root=None):
    if mode not in MODES:
        raise ValueError("未知的編成推薦方式。")
    if public_installation(root) and mode == RL:
        private_rl_descriptor(root=root)
    _atomic_write_json(preference_path(root), {"schema": "gkms.private-loadout-mode.v1", "mode": mode})


def private_rl_descriptor(*, root=None, refresh=False):
    if public_installation(root):
        from .portable_actor_assets import load_configured_actor_descriptor
        descriptor = load_configured_actor_descriptor(project_root=root or app_root(), refresh=refresh)
        if (not descriptor or descriptor.get('portable_actor') is not True
                or not descriptor.get('available') or not descriptor.get('initial_loadout')):
            raise ValueError("RL_LOADOUT_MODEL_UNAVAILABLE: 公開 RL 編成模型或驗收資料不可用。")
        return descriptor
    from .rl.private_actor_assets import load_private_actor_descriptor
    descriptor = load_private_actor_descriptor(project_root=root or app_root(), refresh=refresh)
    if descriptor is None or not descriptor.get("available"):
        raise ValueError("RL_LOADOUT_MODEL_UNAVAILABLE: 私人 RL 模型尚未完成基本驗收。")
    if not descriptor.get("initial_loadout") and not descriptor.get("loadout_reference"):
        raise ValueError("RL_LOADOUT_REFERENCE_UNAVAILABLE: 尚未建立演出與培育資源評估參照。")
    return descriptor


@lru_cache(maxsize=2)
def _scope_reference_document(path, expected_sha256, stamp):
    """Read just the sealed scope metadata once per actual source version."""
    from .qualification_verification import source_stamp
    from .rl_loadout_advisor import LoadoutRecommendationUnavailable
    source = Path(path)
    if not 0 < stamp[2] <= 64 * 1024 * 1024:
        raise LoadoutRecommendationUnavailable("RL_LOADOUT_REFERENCE_CHANGED: 編成評估參照大小無法確認。")
    raw = source.read_bytes()
    if (len(raw) != stamp[2] or hashlib.sha256(raw).hexdigest() != expected_sha256
            or source_stamp(source) != stamp):
        raise LoadoutRecommendationUnavailable("RL_LOADOUT_REFERENCE_CHANGED: 編成評估參照已變更，請重新驗收。")
    return json.loads(raw)


def require_loadout_reference_scope(descriptor, *, idol_card_id, produce_id):
    """Fail before navigation/rental reads; this never builds an evaluator.

    The normal projector still validates the selected source/seed and every
    immutable native input at evaluation time. This gate only checks whether
    the exact requested scope exists in its already hash-bound reference.
    """
    if isinstance(descriptor, dict) and descriptor.get('initial_loadout') is not None:
        from .private_initial_loadout import require_scope
        return require_scope(descriptor, idol_card_id=idol_card_id, produce_id=produce_id)
    from .qualification_verification import source_stamp
    from .rl_loadout_advisor import LoadoutRecommendationUnavailable, REFERENCE_SCHEMA
    ref = descriptor.get("loadout_reference") if isinstance(descriptor, dict) else None
    if not isinstance(ref, dict) or not {"path", "sha256"} <= ref.keys():
        raise LoadoutRecommendationUnavailable("RL_LOADOUT_REFERENCE_UNAVAILABLE: 尚未建立 RL 編成評估參照。")
    path = Path(ref["path"]).resolve(); stamp = source_stamp(path)
    if "bytes" in ref and ref["bytes"] != stamp[2]:
        raise LoadoutRecommendationUnavailable("RL_LOADOUT_REFERENCE_CHANGED: 編成評估參照大小已變更。")
    document = _scope_reference_document(str(path), ref["sha256"], stamp)
    spec = descriptor.get("specification", {})
    if (not isinstance(document, dict) or document.get("schema") != REFERENCE_SCHEMA
            or not isinstance(spec.get("checkpoint", {}).get("sha256"), str)
            or not isinstance(document.get("execution_master_hash"), str)
            or document.get("checkpoint_sha256") != spec.get("checkpoint", {}).get("sha256")
            or document.get("dataset_identity") != spec.get("dataset_identity")
            or document.get("partition") != "train" or document.get("validation_or_test_consumed") is not False
            or not isinstance(document.get("contexts"), list)):
        raise LoadoutRecommendationUnavailable("RL_LOADOUT_REFERENCE_IDENTITY_INVALID: 編成參照與目前固定模型或訓練來源不符。")
    selected = [row for row in document["contexts"] if isinstance(row, dict)
        and row.get("produce_id") == produce_id and row.get("idol_card_id") == idol_card_id]
    if len(selected) != 3 or {row.get("stage_id") for row in selected} != {"Mid1", "Mid2", "Final"}:
        mode = {"produce-004": "NIA Pro", "produce-005": "NIA Master"}.get(produce_id, str(produce_id))
        raise LoadoutRecommendationUnavailable(
            "RL_LOADOUT_THREE_EXAM_REFERENCE_NOT_FOUND_FOR_IDOL_MODE: "
            f"此偶像卡（{idol_card_id}）在 {mode} 尚缺同一培育的三場完整訓練參照，"
            "目前無法計算模型編成推薦。可明確選擇「支援能力／SP 排序＋遊戲回憶推薦」，"
            "演出與培育策略仍使用固定 RL 模型。")
    if (any(not row.get(key) for row in selected
            for key in ("whole_trajectory_id", "source_group", "player_sha256", "flow_id", "context_id"))
            or any(len({row[key] for row in selected}) != 1
                for key in ("whole_trajectory_id", "source_group", "player_sha256", "flow_id"))
            or len({row["context_id"] for row in selected}) != 3
            or any(row.get("partition") != "train"
                or row.get("source_master_hash") != document.get("execution_master_hash") for row in selected)):
        raise LoadoutRecommendationUnavailable("RL_LOADOUT_REFERENCE_SCOPE_INVALID: 三場編成參照的原角色、分組或版本不一致。")
    if source_stamp(path) != stamp:
        raise LoadoutRecommendationUnavailable("RL_LOADOUT_REFERENCE_CHANGED: 編成評估參照在檢查時已變更。")
    return {"produce_id": produce_id, "idol_card_id": idol_card_id,
        "context_ids": [row["context_id"] for row in sorted(selected,
            key=lambda row: ("Mid1", "Mid2", "Final").index(row["stage_id"]))],
        "reference_sha256": ref["sha256"], "native_or_game_execution": False}


def recommend_with_mode(snapshot, loadout, *, mode, constraints, idol_card_id, produce_id,
                        catalog=None, descriptor=None, limit=3, report_callback=None, progress_callback=None, cancelled=None):
    if mode == LEGACY:
        from .account_loadout import recommend_initial_supports
        return recommend_initial_supports(snapshot, loadout, constraints=constraints,
            idol_card_id=idol_card_id, produce_id=produce_id, catalog=catalog, limit=limit)
    if mode != RL:
        raise ValueError("Unknown loadout recommendation mode")
    from .rl_loadout_advisor import recommend_rl_loadouts, build_private_loadout_evaluator
    fixed_descriptor = descriptor or private_rl_descriptor()
    require_loadout_reference_scope(fixed_descriptor, idol_card_id=idol_card_id, produce_id=produce_id)
    if fixed_descriptor.get('initial_loadout') is not None:
        from .rl_loadout_advisor import build_private_initial_loadout_selector
        selector = build_private_initial_loadout_selector(fixed_descriptor)
        try:
            return selector.recommend(snapshot, loadout, constraints=constraints, idol_card_id=idol_card_id,
                produce_id=produce_id, limit=limit, progress_callback=progress_callback, cancelled=cancelled)
        finally:
            if report_callback is not None: report_callback(selector.last_report)
    if progress_callback is not None:
        progress_callback({"phase":"loading-evaluator","processed":0,"total":None,"succeeded":0,"failed":0,"cached_candidates":0})
    evaluator = build_private_loadout_evaluator(fixed_descriptor)
    try:
        return recommend_rl_loadouts(snapshot, loadout, constraints=constraints,
            idol_card_id=idol_card_id, produce_id=produce_id, catalog=catalog,
            descriptor=fixed_descriptor, evaluator=evaluator, limit=limit,
            progress_callback=progress_callback,cancelled=cancelled)
    finally:
        if report_callback is not None:
            report_callback(evaluator.last_report)


def _selection_path(root=None):
    return state_root(root) / "account_inventory" / "rl_preparation_selection.json"


def clear_prepared_recommendation(*, root=None, reason="preferences-changed"):
    path = _selection_path(root)
    if path.exists():
        _atomic_write_json(path, {"schema": "gkms.private-rl-preparation-selection.v1",
            "status": "invalidated", "reason": reason})


def save_prepared_recommendation(proposal, *, model_sha256, constraints, root=None, rental_parameters=None):
    from dataclasses import asdict
    import re
    if not isinstance(model_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", model_sha256):
        raise ValueError("A fixed model identity is required for the selected loadout")
    if public_installation(root) and private_rl_descriptor(root=root)['model_sha256'] != model_sha256:
        raise ValueError("Selected loadout belongs to another public model")
    _atomic_write_json(_selection_path(root), {"schema": "gkms.private-rl-preparation-selection.v1",
        "status": "selected-next-prepare", "proposal": asdict(proposal),
        "model_sha256": model_sha256, "constraints": asdict(constraints),
        "rental_parameters": rental_parameters, "support_apply_outcome": {"status": "not-submitted"}})


def load_prepared_recommendation(snapshot, loadout, *, constraints, idol_card_id,
                                produce_id, model_sha256, root=None):
    """Reuse an explicitly selected preview, never silently substitute another deck."""
    from dataclasses import asdict, replace
    from .account_loadout import BorrowedSupportCard, InitialLoadoutRecommendation, LoadoutSelection, validate_selection
    path = _selection_path(root)
    if not path.is_file():
        return None
    record = json.loads(path.read_text(encoding="utf-8"))
    if record.get("schema") != "gkms.private-rl-preparation-selection.v1":
        raise ValueError("Saved RL loadout selection has an invalid schema")
    if record.get("status") == "invalidated":
        return None
    if record.get("status") != "selected-next-prepare":
        raise ValueError("Saved RL loadout selection has an invalid status")
    raw = record["proposal"]
    data = dict(raw["selection"])
    if (data["account_scope"], data["idol_card_id"], data["produce_id"]) != (snapshot.account_scope, idol_card_id, produce_id):
        return None
    if record["model_sha256"] != model_sha256 or record["constraints"] != json.loads(json.dumps(asdict(constraints))):
        raise ValueError("Selected RL loadout model or locks changed; recompute the recommendation")
    data["borrowed_support"] = BorrowedSupportCard(**data["borrowed_support"])
    selection = LoadoutSelection(**data)
    selection = renew_exact_rental(snapshot, selection, loadout,
        expected_parameters=record.get("rental_parameters"), constraints=constraints)
    return InitialLoadoutRecommendation(selection, raw["ranking_score"], tuple(raw["reasons"]), raw["method"])


def validate_selection_for_review(snapshot, selection, *, constraints):
    """Check immutable identities now; the worker refreshes the rental before IO."""
    from datetime import datetime, timezone
    from .account_loadout import validate_selection
    validate_selection(snapshot, selection, constraints=constraints,
        now=datetime.min.replace(tzinfo=timezone.utc))


def rental_parameters(loadout, selection):
    rows = [row for row in loadout.get("rental_support_cards", ())
            if row.get("rental_key") == selection.borrowed_support.rental_key]
    if len(rows) != 1 or any(rows[0].get(key) != getattr(selection.borrowed_support, key)
                            for key in ("card_id", "level", "plan_type")):
        raise ValueError("Selected RL borrowed support changed; recompute the recommendation")
    return rows[0].get("produce_parameters")


def renew_exact_rental(snapshot, selection, loadout, *, expected_parameters, constraints):
    """Renew freshness only, after a fresh read of the same card and observed stats."""
    from dataclasses import replace
    from .account_loadout import validate_selection
    if any(loadout.get(key) != getattr(selection, key) for key in ("account_scope", "produce_id", "idol_card_id")):
        raise ValueError("Current loadout scope differs from the selected RL proposal")
    actual_parameters = rental_parameters(loadout, selection)
    if actual_parameters != expected_parameters:
        raise ValueError("Selected RL borrowed support parameters changed; recompute the recommendation")
    row = next(row for row in loadout["rental_support_cards"]
               if row["rental_key"] == selection.borrowed_support.rental_key)
    updated = replace(selection, borrowed_support=replace(selection.borrowed_support, expires_at=row["expires_at"]))
    validate_selection(snapshot, updated, constraints=constraints)
    return updated


def _record_support_outcome(outcome, *, root=None):
    path = _selection_path(root)
    record = json.loads(path.read_text(encoding="utf-8"))
    if record.get("status") != "selected-next-prepare":
        raise ValueError("Selected RL intent changed during its support operation")
    record["support_apply_outcome"] = outcome
    _atomic_write_json(path, record)


def apply_rl_support_selection(snapshot, proposal, *, constraints, model_sha256,
                               expected_loadout, client=None, root=None, expected_parameters=_UNSET):
    """Read, renew, save intent, then use the existing owner and support setter."""
    from dataclasses import replace
    from .controller_client import _serialized_controller_request
    from .runtime_command_client import RuntimeCommandClient, RuntimeCommandPending, RuntimeCommandRequest
    from .account_loadout import read_available_loadout, apply_account_loadout, poll_account_loadout_pending
    client = RuntimeCommandClient() if client is None else client
    with _serialized_controller_request(30):
        pending_path = Path(client.root) / "pending_loadout.json"
        if pending_path.is_file():
            poll_account_loadout_pending(client)
            if pending_path.is_file():
                data = json.loads(pending_path.read_text(encoding="utf-8"))
                request = RuntimeCommandRequest(**{key: data[key] for key in (
                    "request_id", "session_generation", "command", "expected_revision", "target")})
                raise RuntimeCommandPending(request, "prior loadout outcome requires reconciliation")
        previous = rental_parameters(expected_loadout, proposal.selection) if expected_parameters is _UNSET else expected_parameters
        live = read_available_loadout(client)
        selection = renew_exact_rental(snapshot, proposal.selection, live,
            expected_parameters=previous, constraints=constraints)
        updated = replace(proposal, selection=selection)
        # This is the user's selected plan, never evidence that native input
        # succeeded. Persist BEFORE the first write so Pending/restart retains it.
        save_prepared_recommendation(updated, model_sha256=model_sha256, constraints=constraints,
            root=root, rental_parameters=previous)
        try:
            result = apply_account_loadout(snapshot, replace(selection, memory_ids=()), client, section="support")
        except RuntimeCommandPending as error:
            _record_support_outcome({"status":"pending", "request_id":error.request.request_id}, root=root)
            raise
        except Exception as error:
            _record_support_outcome({"status":"failed", "reason":str(error)}, root=root)
            raise
        _record_support_outcome({"status":result.status, "applied":result.raw.get("applied") is True,
            "request_id":getattr(getattr(result, "request", None), "request_id", None)}, root=root)
        return result, updated


def verify_native_support_selection(selection, loadout):
    """Readback proves current composition, not an earlier setter's receipt."""
    if any(loadout.get(key) != getattr(selection, key) for key in ("account_scope", "produce_id", "idol_card_id")):
        raise ValueError("Native support readback scope changed")
    validation = loadout.get("game_validation", {})
    if any(validation.get(key) is False for key in ("support_resource_count_valid", "support_ids_valid",
            "support_plan_type_valid", "support_deck_valid")):
        raise ValueError("The native game rejects the current support composition")
    rows = loadout.get("support_cards", ())
    if not isinstance(rows, (list, tuple)):
        raise ValueError("Native support readback is incomplete")
    owned = [row.get("card_id") for row in rows if isinstance(row, dict) and row.get("is_rental") is False]
    borrowed = [row for row in rows if isinstance(row, dict) and row.get("is_rental") is True]
    if (len(rows) != 6 or len(owned) != 5 or len(set(owned)) != 5 or set(owned) != set(selection.support_card_ids)
            or len(borrowed) != 1 or borrowed[0].get("rental_key") != selection.borrowed_support.rental_key
            or borrowed[0].get("card_id") != selection.borrowed_support.card_id):
        raise ValueError("Native support readback differs from the fixed RL preparation plan")
    return {"source": "current-supports-native-readback", "revision": loadout.get("revision"),
        "support_card_ids": list(selection.support_card_ids), "rental_key": selection.borrowed_support.rental_key,
        "original_setter_receipt_recovered": False}
