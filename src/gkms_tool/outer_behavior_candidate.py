"""Reproducible, candidate-conditioned weekly schedule BC research candidate.

Uses observed stepTypes/selectedStepType, never final stats as decision state.
Normal/SP mapping comes from the existing trajectory parser; coarse labels use
the live prior bridge. This module neither loads nor activates production bundles.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

from .leaderboard_outer_schedule import _STEP_ACTION, extract_leaderboard_outer_trajectories
from .nia_route_profile import nia_final_week, nia_phase_for_week
from .nia_static_adapter import _STEP_TYPES
from .runtime_schedule_prior import coarse_schedule_label

SCHEMA = "gkms.outer-behavior-candidate.v1"
_STEP_NUMBERS = {value: key for key, value in _STEP_TYPES.items()}
_CONTEXT = ("produce_id", "plan_type", "exam_effect_type", "idol_card_id")


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def digest(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def candidate_semantics(step_type: str | int) -> dict[str, Any]:
    """One shared mapping, including an exact native step and SP distinction."""
    name = _STEP_TYPES.get(step_type) if type(step_type) is int else step_type
    if name not in _STEP_ACTION or name not in _STEP_NUMBERS:
        raise ValueError(f"schedule candidate has no existing verified mapping: {step_type}")
    number = _STEP_NUMBERS[name]
    coarse = coarse_schedule_label({"action_id": "schedule.choose", "step_type": number})
    parsed_coarse, is_sp = _STEP_ACTION[name]
    if coarse != parsed_coarse:
        raise ValueError("live prior and trajectory coarse mappings disagree")
    axis = coarse.removesuffix("_lesson") if coarse in {"vocal_lesson", "dance_lesson", "visual_lesson"} else None
    return {"step_type": name, "native_step_type": number, "coarse_action": coarse,
            "is_sp": is_sp, "axis": axis}


def make_examples(trajectory: Mapping[str, Any]) -> list[dict[str, Any]]:
    context = {key: trajectory[key] for key in _CONTEXT}
    for key, value in context.items():
        if not isinstance(value, str) or not value:
            raise ValueError(f"missing trajectory context {key}")
    total = nia_final_week(context["produce_id"])
    examples, seen = [], set()
    for choice in trajectory["choices"]:
        week = choice.get("number")
        if type(week) is not int or not 1 <= week <= total or week in seen:
            raise ValueError("trajectory has an invalid/repeated weekly decision")
        seen.add(week)
        if choice.get("candidate_set_complete") is not True:
            raise ValueError("weekly candidate set is not complete")
        candidates = []
        for row in choice["candidates"]:
            candidate = candidate_semantics(row["step_type"])
            if row.get("is_sp") is not candidate["is_sp"] or row.get("action") != candidate["coarse_action"]:
                raise ValueError("source Normal/SP/coarse annotation differs from its exact step")
            candidates.append(candidate)
        names = [row["step_type"] for row in candidates]
        if not names or len(names) != len(set(names)) or names.count(choice.get("chosen_step_type")) != 1:
            raise ValueError("chosen step is not one unique current candidate")
        selected = candidates[names.index(choice["chosen_step_type"])]
        if choice.get("chosen_is_sp") is not selected["is_sp"] or choice.get("chosen_action") != selected["coarse_action"]:
            raise ValueError("chosen Normal/SP annotation differs from actual selected step")
        # Only an explicitly supplied before-state is usable. The historical
        # score and trajectory/final state fields are intentionally not read.
        before = choice.get("state_before", {})
        if not isinstance(before, Mapping):
            raise ValueError("explicit state_before must be an object")
        masked = {}
        for field, aliases in (("hp", ("stamina", "hp")), ("pt", ("produce_points", "producePoint", "pt"))):
            values = [before[key] for key in aliases if key in before]
            if values and (any(type(value) is not int or value < 0 for value in values) or len(set(values)) != 1):
                raise ValueError(f"invalid/ambiguous explicit before {field}")
            masked[field] = values[0] if values else 0
            masked[field + "_missing"] = not values
        examples.append({"trajectory_id": trajectory["trajectory_id"], "context": context,
            "week": week, "phase": nia_phase_for_week(context["produce_id"], week),
            "total_weeks": total, "before_state": masked, "candidates": candidates,
            "chosen_step_type": selected["step_type"], "candidate_set_complete": True})
    if not examples:
        raise ValueError("trajectory has no weekly schedule decisions")
    return examples


class _Groups:
    def __init__(self):
        self.parent = {}

    def find(self, key):
        self.parent.setdefault(key, key)
        if self.parent[key] != key:
            self.parent[key] = self.find(self.parent[key])
        return self.parent[key]

    def union(self, a, b):
        a, b = self.find(a), self.find(b)
        if a != b:
            self.parent[max(a, b)] = min(a, b)


def load_dataset(paths: Sequence[Path], *, raw_sources: Sequence[Path] = ()):
    records, inputs = [], []
    for path in sorted(map(Path, paths)):
        data = path.read_bytes()
        rows = [json.loads(line) for line in data.splitlines() if line.strip()]
        records.extend(rows)
        inputs.append({"path": str(path), "sha256": hashlib.sha256(data).hexdigest(), "rows": len(rows), "kind": "outer_schedules"})
    if raw_sources:
        extracted = extract_leaderboard_outer_trajectories(tuple(raw_sources), allow_schedule_only_fallback=False)
        records.extend(value.to_dict() for value in extracted)
        for path in sorted(map(Path, raw_sources)):
            data = path.read_bytes()
            inputs.append({"path": str(path), "sha256": hashlib.sha256(data).hexdigest(), "kind": "raw_history"})
    unique, memberships, groups, behavior_owner = {}, defaultdict(set), _Groups(), {}
    duplicated, behavior_duplicates = 0, 0
    versions = Counter()
    for row in records:
        identity, source = row.get("trajectory_id"), row.get("source_sha256")
        if not isinstance(identity, str) or not identity or not isinstance(source, str) or len(source) != 64:
            raise ValueError("trajectory/source identity is absent")
        examples = make_examples(row)
        # Exclude final score and path identities from the semantic signature.
        semantic = [{key: value for key, value in ex.items() if key != "trajectory_id"} for ex in examples]
        for ex in semantic:
            ex["candidates"] = sorted(ex["candidates"], key=lambda c: c["native_step_type"])
        signature = digest(semantic)
        if identity in unique:
            duplicated += 1
            if unique[identity]["signature"] != signature:
                raise ValueError("same original trajectory has conflicting choices/context")
        else:
            unique[identity] = {"examples": examples, "signature": signature}
            versions[(row.get("app_version"), row.get("master_version"), row.get("master_hash"))] += 1
        memberships[identity].add(source)
        groups.union("trajectory:" + identity, "source:" + source)
        if signature in behavior_owner and behavior_owner[signature] != identity:
            behavior_duplicates += 1
            groups.union("trajectory:" + identity, "trajectory:" + behavior_owner[signature])
        else:
            behavior_owner[signature] = identity
    by_root = defaultdict(list)
    for identity in unique:
        by_root[groups.find("trajectory:" + identity)].append(identity)
    result = []
    for members in by_root.values():
        group_id = "outer-source-group:" + digest(sorted(members))
        for identity in sorted(members):
            for example in unique[identity]["examples"]:
                result.append({**example, "group_id": group_id, "source_sha256s": sorted(memberships[identity])})
    audit = {"inputs": inputs, "input_trajectory_rows": len(records), "unique_trajectories": len(unique),
        "duplicate_export_rows_removed": duplicated, "source_hash_count": len({s for values in memberships.values() for s in values}),
        "source_connected_groups": len(by_root), "identical_behavior_links": behavior_duplicates,
        "decisions": len(result), "versions": [{"app_version": k[0], "master_version": k[1], "master_hash": k[2], "trajectories": v}
                                                   for k, v in sorted(versions.items())],
        "group_rule": "union original trajectory IDs, all observed source hashes and identical full semantic trajectories; final score is not a feature"}
    return sorted(result, key=lambda ex: (ex["trajectory_id"], ex["week"])), audit


def split_dataset(examples, *, seed=20260906):
    groups = defaultdict(list)
    for example in examples:
        groups[example["group_id"]].append(example)
    strata = defaultdict(list)
    for group_id, rows in groups.items():
        scope = sorted({tuple(row["context"][key] for key in ("produce_id", "plan_type", "exam_effect_type")) for row in rows})
        strata[json.dumps(scope)].append(group_id)
    assignment = {}
    for group_ids in strata.values():
        ordered = sorted(group_ids, key=lambda key: digest([seed, key]))
        n = len(ordered)
        test_n = max(1, int(.15 * n)) if n >= 3 else 0
        val_n = max(1, int(.15 * n)) if n >= 2 else 0
        for index, key in enumerate(ordered):
            assignment[key] = "test" if index < test_n else "validation" if index < test_n + val_n else "train"
    result = {name: [row for row in examples if assignment[row["group_id"]] == name] for name in ("train", "validation", "test")}
    if any(not result[name] for name in result):
        raise ValueError("source groups cannot provide nonempty train/validation/test splits")
    for field in ("group_id", "trajectory_id", "source_sha256s"):
        values = {name: {value for row in rows for value in (row[field] if isinstance(row[field], list) else [row[field]])} for name, rows in result.items()}
        if any(values[a] & values[b] for a, b in (("train", "validation"), ("train", "test"), ("validation", "test"))):
            raise ValueError(f"split leaks {field}")
    return result


def feature_tokens(example, candidate):
    context, step = example["context"], candidate["step_type"]
    semantics = ["step=" + step, "coarse=" + candidate["coarse_action"],
                 "axis=" + str(candidate["axis"]), "sp=" + str(int(candidate["is_sp"]))]
    scopes = ["mode=" + context["produce_id"], "plan=" + context["plan_type"],
        "effect=" + context["exam_effect_type"], "idol=" + context["idol_card_id"],
        "week=" + str(example["week"]), "phase=" + str(example["phase"]),
        "flow=" + context["produce_id"] + "/" + str(example["phase"]),
        "style=" + context["plan_type"] + "/" + context["exam_effect_type"],
        "set=" + ",".join(str(c["native_step_type"]) for c in sorted(example["candidates"], key=lambda c: c["native_step_type"]))]
    tokens = {"bias", *semantics}
    tokens.update(scope + "|" + semantic for scope in scopes for semantic in semantics)
    for field in ("hp", "pt"):
        before = example["before_state"]
        tokens.add(f"state:{field}_missing={int(before[field + '_missing'])}")
        if not before[field + "_missing"]:
            bucket = min(20, before[field] // (5 if field == "hp" else 25))
            tokens.update(f"state:{field}_bucket={bucket}|{semantic}" for semantic in semantics)
    return sorted(tokens)


def _matrix(examples, vocabulary):
    import numpy as np
    encoded, unknown, total = [], 0, 0
    for example in examples:
        row = []
        for candidate in example["candidates"]:
            tokens = feature_tokens(example, candidate)
            indexes = [vocabulary.get(token, 0) for token in tokens]
            unknown += indexes.count(0); total += len(indexes)
            row.append(indexes)
        encoded.append(row)
    width = max(len(row) for row in encoded)
    length = max(len(tokens) for row in encoded for tokens in row)
    matrix = np.zeros((len(examples), width, length), dtype=np.int32)
    mask = np.zeros((len(examples), width), dtype=bool)
    labels = np.zeros(len(examples), dtype=np.int32)
    for i, (example, row) in enumerate(zip(examples, encoded)):
        for j, tokens in enumerate(row):
            matrix[i, j, :len(tokens)] = tokens; mask[i, j] = True
            if example["candidates"][j]["step_type"] == example.get("chosen_step_type"):
                labels[i] = j
    return matrix, mask, labels, unknown / max(1, total)


def _probabilities(weights, matrix, mask):
    import numpy as np
    logits = weights[matrix].sum(axis=2)
    logits = np.where(mask, logits, -1e20)
    logits -= logits.max(axis=1, keepdims=True)
    exp = np.exp(logits) * mask
    return exp / exp.sum(axis=1, keepdims=True)


def _metrics(examples, probabilities):
    total = len(examples); correct = lessons = lesson_correct = sp_choices = sp_correct = 0
    loss = 0.0
    for example, row in zip(examples, probabilities):
        candidates = example["candidates"]
        chosen = next(i for i, c in enumerate(candidates) if c["step_type"] == example["chosen_step_type"])
        predicted = max(range(len(candidates)), key=lambda i: (float(row[i]), -candidates[i]["native_step_type"]))
        match = predicted == chosen
        correct += match; loss -= math.log(max(1e-12, float(row[chosen])))
        if any(c["axis"] is not None for c in candidates):
            lessons += 1; lesson_correct += match
        if any(c["is_sp"] for c in candidates) and any(c["axis"] is not None and not c["is_sp"] for c in candidates):
            sp_choices += 1; sp_correct += match
    return {"decisions": total, "top1_agreement": correct/max(1,total), "nll": loss/max(1,total),
        "lesson_decisions": lessons, "lesson_top1_agreement": lesson_correct/max(1,lessons),
        "mixed_normal_sp_decisions": sp_choices, "mixed_normal_sp_top1_agreement": sp_correct/max(1,sp_choices)}


def baseline_predictions(train, examples, kind):
    import numpy as np
    global_counts, counts = Counter(), defaultdict(Counter)
    def key(example):
        return tuple(example["context"][name] for name in ("produce_id", "plan_type", "exam_effect_type")) + (example["week"],)
    for example in train:
        chosen = next(c for c in example["candidates"] if c["step_type"] == example["chosen_step_type"])
        label = chosen["coarse_action"] if kind == "coarse_frequency" else chosen["step_type"]
        global_counts[label] += 1; counts[key(example)][label] += 1
    result = np.zeros((len(examples), max(len(row["candidates"]) for row in examples)))
    for index, example in enumerate(examples):
        tally = counts[key(example)] if kind == "scoped_exact_frequency" and counts[key(example)] else global_counts
        values = [tally[c["coarse_action"] if kind == "coarse_frequency" else c["step_type"]] + 1
                  for c in example["candidates"]]
        result[index, :len(values)] = np.asarray(values) / sum(values)
    return result


def coverage(rows):
    return {"decisions": len(rows), "trajectories": len({r["trajectory_id"] for r in rows}),
        "source_groups": len({r["group_id"] for r in rows}),
        **{key: dict(sorted(Counter(r["context"][key] for r in rows).items())) for key in _CONTEXT},
        "before_hp_missing": sum(r["before_state"]["hp_missing"] for r in rows),
        "before_pt_missing": sum(r["before_state"]["pt_missing"] for r in rows),
        "chosen_steps": dict(sorted(Counter(r["chosen_step_type"] for r in rows).items()))}


def train_candidate(examples, *, seed=20260906, epochs=100, learning_rate=.04, l2=.002):
    import numpy as np
    splits = split_dataset(examples, seed=seed)
    tokens = sorted({token for row in splits["train"] for candidate in row["candidates"] for token in feature_tokens(row, candidate)})
    vocabulary = {token: index + 1 for index, token in enumerate(tokens)}
    matrices = {name: _matrix(rows, vocabulary) for name, rows in splits.items()}
    weights = np.zeros(len(tokens)+1, dtype=np.float64)
    first = np.zeros_like(weights); second = np.zeros_like(weights)
    best_loss, best, best_epoch, history = math.inf, weights.copy(), 0, []
    x, mask, labels, _ = matrices["train"]
    for epoch in range(1, epochs+1):
        probabilities = _probabilities(weights, x, mask)
        error = probabilities.copy(); error[np.arange(len(labels)), labels] -= 1
        gradient = np.bincount(x.ravel(), weights=np.broadcast_to(error[:,:,None],x.shape).ravel(), minlength=len(weights))/len(labels)
        gradient += l2*weights; gradient[0] = 0
        first = .9*first+.1*gradient; second = .999*second+.001*gradient*gradient
        weights -= learning_rate*(first/(1-.9**epoch))/(np.sqrt(second/(1-.999**epoch))+1e-8)
        weights[0] = 0
        validation = _metrics(splits["validation"], _probabilities(weights,*matrices["validation"][:2]))
        if validation["nll"] < best_loss:
            best_loss, best, best_epoch = validation["nll"], weights.copy(), epoch
        if epoch == 1 or epoch % 10 == 0 or epoch == epochs:
            history.append({"epoch": epoch, "validation": validation})
    model = {"schema": SCHEMA, "model_type": "candidate-conditioned-linear-softmax", "vocabulary": tokens,
        "weights": best.tolist(), "seed": seed, "best_epoch": best_epoch,
        "input_scope": "weekly native schedule candidates; no reward-card/PT/customize labels",
        "mappings": ["nia_static_adapter._STEP_TYPES", "leaderboard_outer_schedule._STEP_ACTION", "runtime_schedule_prior.coarse_schedule_label"]}
    report = {"schema": SCHEMA, "training": {"seed": seed, "epochs": epochs, "learning_rate": learning_rate, "l2": l2,
        "optimizer": "deterministic full-batch NumPy Adam; validation-NLL checkpoint", "features": len(tokens), "best_epoch": best_epoch},
        "coverage": {name: coverage(rows) for name, rows in splits.items()}, "metrics": {}, "baselines": {}, "history": history,
        "split_policy": "source/trajectory connected components, stratified mode-plan-mainEffect; no source/trajectory overlap",
        "limitations": ["imitation agreement is not game score, qualification probability or win-rate improvement",
            "leaderboard teachers are selected high-score histories, not unbiased outcomes",
            "unprovided before HP/PT are masked; final stats/history_score are excluded",
            "event options, card rewards, PT purchases and customizations have no labels in this candidate",
            "only explicitly covered modes/plans/steps may use this as a weak prior alongside the stateful deck planner",
            "additional raw histories outside the supplied inputs were not silently added"], "activated": False}
    for name, rows in splits.items():
        report["metrics"][name] = _metrics(rows, _probabilities(best,*matrices[name][:2]))
        report["metrics"][name]["unknown_feature_fraction"] = matrices[name][3]
    for kind in ("coarse_frequency", "exact_frequency", "scoped_exact_frequency"):
        report["baselines"][kind] = {name: _metrics(rows, baseline_predictions(splits["train"], rows, kind)) for name, rows in splits.items()}
    return model, report, splits


def predict_example(model, example):
    import numpy as np
    vocabulary = {token: index+1 for index, token in enumerate(model["vocabulary"])}
    matrix, mask, _, _ = _matrix([example], vocabulary)
    values = _probabilities(np.asarray(model["weights"]), matrix, mask)[0]
    return {candidate["step_type"]: float(values[index]) for index, candidate in enumerate(example["candidates"])}


def write_candidate(output: Path, examples, audit, *, seed=20260906, epochs=100, learning_rate=.04, l2=.002):
    output = Path(output)
    if output.exists():
        raise FileExistsError("candidate output already exists; existing artifacts are immutable")
    model, report, splits = train_candidate(examples, seed=seed, epochs=epochs, learning_rate=learning_rate, l2=l2)
    output.mkdir(parents=True)
    report["data_audit"] = audit
    artifacts = {"model.json": model, "report.json": report,
        "split_membership.json": {name: {"groups": sorted({r['group_id'] for r in rows}),
            "trajectories": sorted({r['trajectory_id'] for r in rows}),
            "sources": sorted({s for r in rows for s in r['source_sha256s']})} for name,rows in splits.items()}}
    files = {}
    for name, value in artifacts.items():
        data = canonical(value)
        with (output/name).open("xb") as stream:
            stream.write(data)
        files[name] = hashlib.sha256(data).hexdigest()
    manifest = {"schema": SCHEMA, "component_role": "outer_schedule_bc", "status": "candidate-not-activated",
        "files": files, "input_dataset_digest": digest(examples), "covered_modes": sorted({r['context']['produce_id'] for r in examples}),
        "covered_plans": sorted({r['context']['plan_type'] for r in examples}), "supported_decisions": ["weekly-schedule"],
        "unsupported_decisions": ["event-options", "reward-card", "pt-purchase", "card-customize", "exam-play"],
        "predicts_game_score": False, "changes_active_bundle": False}
    with (output/"manifest.json").open("xb") as stream:
        stream.write(canonical(manifest))
    return report
