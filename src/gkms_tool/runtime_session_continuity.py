"""Append-only same-run evidence across a verified native maintenance job.

No helper invocation, game read/input, manifest change or pending-file repair.
A new Title proves only that maintenance launched a bridge, not the saved run.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re

from .runtime_command_client import RuntimeCommandError
from .runtime_login_touch import LOGIN_SCREENS
from .training_artifact_io import canonical_json_bytes

SCHEMA = "gkms.run-session-continuity.v1"
CHECKPOINT_SCHEMA = "gkms.persisted-produce-checkpoint.v2"
_LEGACY_CHECKPOINT_SCHEMA = "gkms.persisted-produce-checkpoint.v1"
_AUDITIONS = {"ProduceStepType_AuditionMid1", "ProduceStepType_AuditionMid2", "ProduceStepType_AuditionFinal"}
_OPTIONAL_PROGRESS = ("stepSelectNumber", "inProgressStep", "examSeedValue", "produceExamGimmickEffectGroupId",
    "stamina", "maxStamina", "vocal", "dance", "visual", "producePoint", "voteCount",
    "produceDrinkIds", "produceItems", "auditions", "convertProduceCardIds")
_HASH = re.compile(r"[a-fA-F0-9]{64}\Z")
_JOB_ID = re.compile(r"[a-f0-9]{32}\Z")


def _fail(detail):
    raise RuntimeCommandError("run session continuity: " + detail)


def _digest(value):
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _manifest_digest(run):
    return _digest(run.to_dict())


def proof_directory(bridge_root, run_id):
    if not isinstance(run_id, str) or not run_id.startswith("run-") or Path(run_id).name != run_id or any(c in run_id for c in "/\\"):
        _fail("invalid run identity")
    return Path(bridge_root) / "run_session_continuity" / run_id


def _read(path, *, within=None, expected=None):
    path = Path(path).resolve()
    if within is not None and not path.is_relative_to(Path(within).resolve()):
        _fail("evidence path escapes its source directory")
    try:
        data = path.read_bytes()
        value = json.loads(data)
    except (OSError, ValueError) as error:
        _fail("evidence source unavailable: " + path.name + ": " + str(error))
    digest = hashlib.sha256(data).hexdigest()
    if expected is not None and digest != expected:
        _fail("evidence source changed: " + path.name)
    if not isinstance(value, dict):
        _fail("evidence source is not an object")
    return value, {"path": str(path), "sha256": digest}


def _native(path, generation, bridge_root, *, expected=None):
    value, ref = _read(path, within=Path(bridge_root) / "results", expected=expected)
    raw = value.get("snapshot")
    if (value.get("schema") != "gkms.runtime-command-result.v1" or value.get("status") != "ok"
            or value.get("session_generation") != generation or not isinstance(raw, dict)
            or raw.get("schema") != "gkms.outer-runtime-snapshot.v1"):
        _fail("native source does not bind the requested generation")
    return raw, ref


def _time(raw):
    value = raw.get("captured_at")
    if not isinstance(value, str):
        _fail("native source timestamp missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            _fail("native source timestamp has no timezone")
        return parsed.timestamp()
    except ValueError as error:
        _fail("native source timestamp invalid: " + str(error))


def checkpoint_signature(raw, run, *, schema=CHECKPOINT_SCHEMA):
    """Only persisted produce fields; UI identity and scene state are excluded."""
    from .runtime_final_presentation_recovery import SCHEMA as FINAL_SCHEMA, signature as final_signature
    if schema == FINAL_SCHEMA:
        return final_signature(raw, run)
    if (raw.get("state") or {}).get("in_progress") is not True:
        _fail("saved active produce is not observed")
    progress = raw.get("progress")
    if not isinstance(progress, Mapping):
        _fail("persisted produce progress missing")
    identity = {"produceId": run.produce_id, "idolCardId": run.idol_card_id, "characterId": run.character_id}
    if any(progress.get(key) != value for key, value in identity.items()):
        _fail("saved produce mode/idol/character differs from the run")
    for key in ("stepNumber", "stepType", "status"):
        if key not in progress:
            _fail("checkpoint field missing: " + key)
    if type(progress["stepNumber"]) is not int or progress["stepNumber"] < 1:
        _fail("checkpoint week is invalid")
    if any(not isinstance(progress[key], str) or not progress[key] for key in ("stepType", "status")):
        _fail("checkpoint step/status is invalid")
    if progress["stepType"] in _AUDITIONS:
        if type(progress.get("stepSelectNumber")) is not int or progress["stepSelectNumber"] < 1:
            _fail("audition tier missing from persisted checkpoint")
        if type(progress.get("examSeedValue")) is not int:
            _fail("audition seed missing from persisted checkpoint")
    values = {**identity, **{key: progress[key] for key in ("stepNumber", "stepType", "status")}}
    # Sparse protobuf fields remain absent, not fabricated as zero/false/[];
    # the observed field set must match on both sides of maintenance.
    values.update({key: progress[key] for key in _OPTIONAL_PROGRESS if key in progress})
    cards = (raw.get("collections") or {}).get("cards")
    if not isinstance(cards, list) or not cards:
        _fail("persisted produce card collection missing")
    numbers = set()
    for card in cards:
        if (not isinstance(card, Mapping) or type(card.get("number")) is not int or card["number"] < 1
                or card["number"] in numbers or not isinstance(card.get("produceCardId"), str) or not card["produceCardId"]):
            _fail("persisted produce card identity invalid")
        numbers.add(card["number"])
    # This is UserProduceProgress's persistent card collection, not Exam
    # Hand/Deck order. Native presentation can reorder it before a restart;
    # the unique Number binds each complete card record across reload.
    if schema not in {CHECKPOINT_SCHEMA, _LEGACY_CHECKPOINT_SCHEMA}:
        _fail("unsupported persisted checkpoint schema")
    ordered = cards if schema == _LEGACY_CHECKPOINT_SCHEMA else sorted(cards, key=lambda card: card["number"])
    return {"schema": schema, "values": values, "produce_cards": ordered,
            "unobserved_optional_fields": [key for key in _OPTIONAL_PROGRESS if key not in progress]}


def _cultivation_surface(raw):
    screen = raw.get("screen_type", "")
    if screen in {"TitlePresenter", "HomeTopScreenPresenter", "HomeProduceProgressSheetPresenter"} | LOGIN_SCREENS:
        return False
    def actual(name):
        return isinstance(name, str) and (name.startswith("Schedule") or name.startswith("Audition")
            or name in {"ExamScreenPresenter", "ProduceEvaluateScreenPresenter", "ProduceResultScreenPresenter",
                        "ProduceBeforeLiveEvaluateScreenPresenter", "ProduceBeforeLiveEvaluateNiaScreenPresenter", "LiveScenePresenter"})
    if actual(screen):
        return True
    parent = raw.get("underlying_screen_type")
    ui = raw.get("ui_state") or {}
    if not actual(parent):
        return False
    # Card selectors publish both fields from the same current parent. Do not
    # invent a UI-only fallback when the common parent observation is absent.
    if "parent_screen_type" in ui and ui["parent_screen_type"] != parent:
        return False
    if ("parent_instance_id" in ui and "underlying_screen_instance_id" in raw
            and ui["parent_instance_id"] != raw["underlying_screen_instance_id"]):
        return False
    return True


def _launch_process_matches(launch, result, previous_pid):
    actual, reported = result.get("game_pid"), launch.get("game_pid")
    if any(type(pid) is not int or pid <= 0 for pid in (actual, reported)) or actual == previous_pid:
        return False
    if reported == actual:
        return True
    # A launch-window candidate can exit between its selection and Refresh,
    # leaving only the bootstrap PID and no window/path. The maintenance response
    # instead uses the new DLL's verified status PID and same-generation read.
    # Accept only that explicit legacy shape, not an arbitrary PID mismatch.
    return (type(launch.get("bootstrap_pid")) is int and launch["bootstrap_pid"] == reported
            and launch.get("game_hwnd") == 0 and type(launch.get("game_hwnd")) is int
            and "game_path" in launch and launch["game_path"] is None
            and launch.get("already_running") is False)


def _authorization(response_path, run, before_generation, after_generation, bridge_root, expected_refs=None,
                   checkpoint_schema=CHECKPOINT_SCHEMA, *, _late_candidate=None):
    maintenance = Path(bridge_root).parent / "native_maintenance" / "sessions"
    response_path = Path(response_path).resolve()
    if response_path.parent.name != "responses" or not response_path.is_relative_to(maintenance.resolve()):
        _fail("maintenance response is outside its session")
    session = response_path.parent.parent
    if not _JOB_ID.fullmatch(session.name) or not _JOB_ID.fullmatch(response_path.stem):
        _fail("maintenance session/job identity invalid")
    job = session / "jobs" / response_path.stem
    # A completed physical reload can carry a separate, explicit current-native
    # qualification. Its bootstrap remains a bootstrap; no launch.json or
    # missing ancestry is fabricated to fit the historical success schema.
    from .runtime_maintenance_independent_verification import SCHEMA as INDEPENDENT_SCHEMA, authorization as independent_authorization
    if _late_candidate is not None and _late_candidate.get("schema") == INDEPENDENT_SCHEMA:
        return independent_authorization(_late_candidate, response_path, run, before_generation, after_generation,
            bridge_root, expected_refs=expected_refs, checkpoint_schema=checkpoint_schema)
    candidates = list((job / "late_native_verifications").glob("*.json"))
    if len(candidates) == 1:
        body, body_ref = _read(candidates[0])
        if body.get("schema") == INDEPENDENT_SCHEMA:
            if candidates[0].stem != _digest(body):
                _fail("independent maintenance qualification content address changed")
            return independent_authorization(body, response_path, run, before_generation, after_generation,
                bridge_root, body_ref=body_ref, expected_refs=expected_refs, checkpoint_schema=checkpoint_schema)
    elif candidates:
        _fail("maintenance job has multiple post-operation qualifications")
    refs = {}
    def read(name, path):
        expected = None if expected_refs is None else expected_refs[name]["sha256"]
        value, ref = _read(path, within=maintenance, expected=expected)
        if expected_refs is not None and Path(expected_refs[name]["path"]).resolve() != Path(ref["path"]):
            _fail("maintenance source reference changed")
        refs[name] = ref
        return value
    response = read("response", response_path)
    request = read("request", job / "request.json")
    preflight = read("preflight", job / "preflight.json")
    operation = read("operation", job / "operation.json")
    executor = read("executor", job / "executor_result.json")
    launch = read("launch", job / "launch.json")
    payload, result = request.get("payload") or {}, response.get("result") or {}
    verified = response.get("status") == "completed"
    if response.get("status") == "failed":
        from .runtime_maintenance_late_verification import load_late_verification, _validate_body
        if _late_candidate is not None:
            # Qualification validates the original job before publishing its
            # separate receipt. This argument never changes the failed response.
            result = _validate_body(_late_candidate, response_path, run, bridge_root)
        else:
            expected_late = None if expected_refs is None else expected_refs.get("late_verification")
            if expected_refs is not None and expected_late is None:
                _fail("session proof has no bound late verification receipt")
            result, late_ref = load_late_verification(response_path, run, bridge_root, expected_ref=expected_late)
            if late_ref is not None:
                refs["late_verification"] = late_ref
        verified = result is not None
        result = result or {}
    command = request.get("command")
    # serve authenticates the original envelope, then retains only this body;
    # schema/session/request ID survive in the response and directory identity.
    if (set(request) != {"command", "payload"} or command not in {"reload-dll", "restart-game"}
            or response.get("schema") != "gkms.native-maintenance.v1"
            or not verified or response.get("session_id") != session.name
            or response.get("request_id") != response_path.stem
            or payload.get("expected_run_id") != run.run_id or result.get("expected_run_id") != run.run_id
            or preflight.get("run_id") != run.run_id
            or payload.get("expected_generation") != before_generation or preflight.get("generation") != before_generation
            or result.get("native_generation") != after_generation or before_generation == after_generation):
        _fail("maintenance job does not authorize this same-run generation change")
    installed = payload.get("candidate_sha256") if command == "reload-dll" else payload.get("expected_installed_sha256")
    if (not isinstance(installed, str) or not _HASH.fullmatch(installed)
            or not all(isinstance(value, str) and _HASH.fullmatch(value) for value in
                       (before_generation, after_generation, payload.get("expected_installed_sha256")))
            or type(payload.get("expected_game_pid")) is not int or payload["expected_game_pid"] <= 0
            or Path(operation.get("workspace", "")).resolve() != Path(bridge_root).resolve().parent.parent
            or Path(operation.get("job_directory", "")).resolve() != job
            or operation.get("candidate_sha256") != payload.get("candidate_sha256")
            or (command == "reload-dll" and Path(operation.get("candidate_path", "")).resolve() != job / "candidate.dll")
            or operation.get("operation") != command or operation.get("expected_pid") != payload.get("expected_game_pid")
            or preflight.get("pid") != payload.get("expected_game_pid")
            or operation.get("installed_sha256") != payload.get("expected_installed_sha256")
            or executor.get("schema") != "gkms.native-maintenance-executor.v1" or executor.get("operation") != command
            or executor.get("installed_sha256") != installed or result.get("installed_sha256") != installed
            or launch.get("schema") != "gkms.gakumas-direct-cached-launch.v1" or launch.get("status") != "started"
            or not _launch_process_matches(launch, result, payload.get("expected_game_pid"))):
        _fail("maintenance process/DLL evidence is inconsistent")
    for name, path, generation in (("before", preflight.get("snapshot_path"), before_generation),
                                   ("launched", result.get("snapshot_path"), after_generation)):
        raw, ref = _native(path, generation, bridge_root,
            expected=None if expected_refs is None else expected_refs[name]["sha256"])
        if expected_refs is not None and Path(expected_refs[name]["path"]).resolve() != Path(ref["path"]):
            _fail("native maintenance source reference changed")
        refs[name] = ref
        if name == "before":
            before = raw
        else:
            launched = raw
    if (before.get("busy") is not False or preflight.get("screen_type") != before.get("screen_type")
            or preflight.get("state") != before.get("state") or result.get("screen_type") != launched.get("screen_type")
            or _time(launched) < _time(before)):
        _fail("maintenance preflight/launch snapshot is stale or inconsistent")
    return {"sources": refs, "before_signature": checkpoint_signature(before, run, schema=checkpoint_schema),
        "launched_at": _time(launched), "from_generation": before_generation, "to_generation": after_generation,
        "job_id": response_path.stem}


def _verified_entries(bridge_root, run):
    directory = proof_directory(bridge_root, run.run_id)
    entries = []
    for path in sorted(directory.glob("*.json")) if directory.exists() else ():
        value, _ = _read(path)
        if (value.get("schema") != SCHEMA or value.get("run_id") != run.run_id
                or value.get("run_manifest_digest") != _manifest_digest(run) or path.stem != _digest(value)):
            _fail("session proof changed or belongs to another manifest")
        auth = _authorization(value["maintenance_sources"]["response"]["path"], run,
            value["from_generation"], value["to_generation"], bridge_root, value["maintenance_sources"],
            checkpoint_schema=(value.get("checkpoint") or {}).get("schema"))
        if auth.get('recovery_scope') != value.get('recovery_scope'):
            _fail('session proof dropped or changed its operational recovery scope')
        if auth.get('recovery_scope') and any(value.get(key) is not False for key in
                ('cards_continuity_verified','launch_lineage_verified','training_admitted')):
            _fail('operational recovery proof cannot promote data or ancestry qualification')
        resumed, _ = _native(value["resumed_source"]["path"], value["to_generation"], bridge_root,
                            expected=value["resumed_source"]["sha256"])
        signature = checkpoint_signature(resumed, run, schema=(value.get("checkpoint") or {}).get("schema"))
        if (not _cultivation_surface(resumed) or resumed.get("busy") is not False or _time(resumed) < auth["launched_at"]
                or signature != auth["before_signature"] or value.get("checkpoint") != signature):
            _fail("session proof has no matching persisted checkpoint")
        entries.append((path.stem, value))
    return entries


def verified_run_generation(bridge_root, run):
    """Original generation or a verified append-only chain, never an echo."""
    generation = run.evidence.get("session_generation")
    if not isinstance(generation, str) or not generation:
        _fail("original run generation missing")
    remaining = _verified_entries(bridge_root, run)
    previous = None
    seen = {generation}
    while remaining:
        candidates = [entry for entry in remaining if entry[1]["from_generation"] == generation]
        if len(candidates) != 1:
            _fail("session proof chain is missing or forked")
        digest, entry = candidates[0]
        if entry.get("previous_proof") != previous or entry["to_generation"] in seen:
            _fail("session proof chain is stale or cyclic")
        generation, previous = entry["to_generation"], digest
        seen.add(generation)
        remaining.remove(candidates[0])
    return generation


def _no_pending(bridge_root):
    root = Path(bridge_root)
    if list(root.glob("*pending*.json")):
        _fail("pending input must remain unresolved; no session proof published")
    for folder in ("inbox", "running"):
        for path in (root / folder).glob("*.json"):
            value, _ = _read(path)
            if value.get("command") not in {"status", "read_snapshot", "read_outer_snapshot", "read_inventory", "read_loadout"}:
                _fail("queued game input prevents session proof publication")


def observe_run_session(bridge_root, run, snapshot, previous=None):
    """Authorize login from an existing job; prove the run only from progress."""
    generation = verified_run_generation(bridge_root, run)
    if generation == snapshot.session_generation:
        from .runtime_final_presentation_recovery import SCOPE as FINAL_SCOPE, enforce
        scoped = [entry for _,entry in _verified_entries(bridge_root,run)
                  if entry.get('to_generation')==generation and entry.get('recovery_scope')==FINAL_SCOPE]
        if scoped:
            checkpoint=scoped[0]['checkpoint'];phase=enforce(snapshot.raw,run,checkpoint)
            info={'status':'verified','generation':generation,'initial_generation':run.evidence['session_generation'],
                  'recovery_scope':FINAL_SCOPE,'cards_continuity_verified':False,'launch_lineage_verified':False,
                  'training_admitted':False,'checkpoint':checkpoint,'gameplay_continuity_verified':False,
                  'operational_continuity_verified':True}
            if phase=='login-resume':
                info.update(status='awaiting-progress',maintenance_job_id=Path(scoped[0]['maintenance_sources']['response']['path']).stem,
                    maintenance_sources=scoped[0]['maintenance_sources'])
            return info
        return {"status": "verified", "generation": generation, "initial_generation": run.evidence["session_generation"]}
    # A loss journal is evidence of a particular session; never relabel it.
    if (Path(bridge_root) / "failed_settlements" / (run.run_id + ".json")).exists():
        _fail("existing failure journal cannot change generation")
    responses = Path(bridge_root).parent / "native_maintenance" / "sessions"
    matches = []
    for path in responses.glob("*/responses/*.json"):
        value, _ = _read(path)
        result = value.get("result") or {}
        if value.get("status") == "failed":
            from .runtime_maintenance_late_verification import load_late_verification
            job = path.parent.parent / "jobs" / path.stem
            # Unrelated failed jobs remain historical failures. Only an explicit
            # append-only receipt can supply late observation metadata.
            if not (job / "late_native_verifications").exists():
                continue
            request, _ = _read(job / "request.json")
            if (request.get("payload") or {}).get("expected_run_id") != run.run_id:
                continue
            result, _ = load_late_verification(path, run, bridge_root)
            result = result or {}
        if result.get("native_generation") != snapshot.session_generation or result.get("expected_run_id") != run.run_id:
            continue
        expected = None
        if previous is not None and previous.get("status") == "awaiting-progress":
            expected = previous.get("maintenance_sources")
            if previous.get("maintenance_job_id") != path.stem:
                _fail("maintenance authorization changed while awaiting saved progress")
        matches.append(_authorization(path, run, generation, snapshot.session_generation, bridge_root, expected))
    if len(matches) != 1:
        _fail("no unique verified maintenance job authorizes this run")
    auth = matches[0]
    info = {"status": "awaiting-progress", "from_generation": generation,
        "generation": snapshot.session_generation, "maintenance_job_id": auth["job_id"],
        "gameplay_continuity_verified": False, "checkpoint": auth["before_signature"],
        "maintenance_sources": auth["sources"]}
    if auth.get('recovery_scope'):
        info.update(recovery_scope=auth['recovery_scope'],cards_continuity_verified=False,
                    launch_lineage_verified=False,training_admitted=False)
    if (not _cultivation_surface(snapshot.raw) or snapshot.raw.get("busy") is not False
            or (snapshot.raw.get("state") or {}).get("in_progress") is not True):
        return info
    signature = checkpoint_signature(snapshot.raw, run, schema=auth['before_signature']['schema'])
    if signature != auth["before_signature"]:
        _fail("restored persisted checkpoint differs from pre-maintenance progress")
    restored, ref = _native(snapshot.source_path, snapshot.session_generation, bridge_root)
    if restored != snapshot.raw or _time(restored) < auth["launched_at"]:
        _fail("restored native snapshot is stale or differs from its source")
    _no_pending(bridge_root)
    previous = next((digest for digest, entry in _verified_entries(bridge_root, run)
                     if entry["to_generation"] == generation), None)
    body = {"schema": SCHEMA, "run_id": run.run_id, "run_manifest_digest": _manifest_digest(run),
        "from_generation": generation, "to_generation": snapshot.session_generation, "previous_proof": previous,
        "maintenance_sources": auth["sources"], "resumed_source": ref, "checkpoint": signature,
        "source_kind": "controlled-maintenance-and-restored-native-progress", "exact_transition_claimed": False}
    if auth.get('recovery_scope'):
        from .runtime_final_presentation_recovery import enforce
        enforce(snapshot.raw,run,signature)
        body.update(recovery_scope=auth['recovery_scope'],cards_continuity_verified=False,
                    launch_lineage_verified=False,training_admitted=False)
    directory = proof_directory(bridge_root, run.run_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (_digest(body) + ".json")
    data = canonical_json_bytes(body)
    try:
        with path.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        if path.read_bytes() != data:
            _fail("existing session proof differs; it was not overwritten")
    return {**info, "status": "verified", "gameplay_continuity_verified": not bool(auth.get('recovery_scope')),
            **({'operational_continuity_verified':True} if auth.get('recovery_scope') else {}),
            "proof_path": str(path.resolve()), "proof_sha256": _digest(body)}


def choose_session_recovery_action(snapshot, run, continuity):
    """Only observed login/resume controls while the saved run is unconfirmed."""
    if snapshot.raw.get("busy") is True or snapshot.raw.get("actions_complete") is not True:
        return None
    choices = []
    for action in snapshot.actions:
        target = action["target"]
        if (action["action_id"] == "ui.navigation" and
                ((snapshot.raw["screen_type"] == "TitlePresenter" and target.get("button_id") == "title.start")
                 or (snapshot.raw["screen_type"] in LOGIN_SCREENS and target.get("button_id") == "login.continue"))):
            choices.append(target)
        elif (action["action_id"] == "ui.navigation" and snapshot.raw["screen_type"] == "HomeTopScreenPresenter"
              and target.get("button_id") == "home.produce"):
            if checkpoint_signature(snapshot.raw, run, schema=continuity['checkpoint']['schema']) != continuity["checkpoint"]:
                _fail("Home saved checkpoint differs; new cultivation must not start")
            choices.append(target)
        elif (action["action_id"] == "produce.resume" and snapshot.raw["screen_type"] == "HomeProduceProgressSheetPresenter"
              and target.get("produce_id") == run.produce_id and target.get("idol_card_id") == run.idol_card_id):
            choices.append(target)
    if len(choices) > 1:
        _fail("login/resume control is ambiguous")
    if not choices and snapshot.actions:
        _fail("only normal login/resume is allowed before saved-run verification")
    return choices[0] if choices else None
