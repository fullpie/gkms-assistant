"""Fail-closed audit helpers for leaderboard replay UI controls.

The checked-in MaaGakumasu bundle currently contains the ordinary Produce
``ProduceRecognitionSkipRound`` node only.  That node is a live cultivation
gate and must never be treated as a leaderboard replay control.  This module
therefore discovers *only* nodes whose own bundled name proves replay scope;
it does not infer replay state from a generic ``skip`` image or manufacture a
coordinate when the bundle has no replay control.

The helpers are deliberately filesystem-only and side-effect free.  The
Maa/Win32 session uses the returned contracts for a second, live recognition
gate before it is allowed to send a background click.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping


CANONICAL_WIDTH = 720
CANONICAL_HEIGHT = 1280

_NODE_NAME_RE = re.compile(r"[^a-z0-9]+")
_REPLAY_SKIP_WORDS = ("replay", "skip")
_REPLAY_TURN_WORDS = ("replay", "turn")


@dataclass(frozen=True, slots=True)
class ReplayTemplateContract:
    """One explicit replay-scoped template/action contract from Maa."""

    node_name: str
    source: str
    role: str
    templates: tuple[str, ...]
    roi: tuple[int, int, int, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "node": self.node_name,
            "source": self.source,
            "role": self.role,
            "templates": list(self.templates),
            "roi": list(self.roi),
        }


@dataclass(frozen=True, slots=True)
class ReplayControlAudit:
    """Static, typed audit result used by both the client and the session."""

    resource_root: Path
    resource_present: bool
    produce_skip_source: dict[str, Any] | None
    replay_skip: tuple[ReplayTemplateContract, ...]
    replay_move_to_turn: tuple[ReplayTemplateContract, ...]
    blockers: tuple[str, ...]

    @property
    def skip_input_allowed(self) -> bool:
        return len(self.replay_skip) == 1

    @property
    def move_to_turn_allowed(self) -> bool:
        return len(self.replay_move_to_turn) == 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "resource_root": str(self.resource_root),
            "resource_present": self.resource_present,
            "produce_skip_source": self.produce_skip_source,
            "replay_skip": [value.to_dict() for value in self.replay_skip],
            "replay_move_to_turn": [
                value.to_dict() for value in self.replay_move_to_turn
            ],
            "skip_input_allowed": self.skip_input_allowed,
            "move_to_turn_allowed": self.move_to_turn_allowed,
            "input_allowed": self.skip_input_allowed,
            "blockers": list(self.blockers),
        }


def _normalize_name(value: object) -> str:
    return _NODE_NAME_RE.sub("", str(value).casefold())


def _pipeline_files(root: Path) -> Iterator[Path]:
    """Yield deterministic Maa pipeline files below one resource root."""

    if not root.is_dir():
        return
    yield from sorted(root.glob("*/pipeline/*.json"))


def _load_pipeline(path: Path) -> Mapping[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, Mapping) else None


def _as_int_tuple(value: object) -> tuple[int, int, int, int] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    if any(type(item) is not int for item in value):
        return None
    left, top, width, height = (int(item) for item in value)
    if width <= 0 or height <= 0:
        return None
    if not (
        0 <= left < CANONICAL_WIDTH
        and 0 <= top < CANONICAL_HEIGHT
        and left + width <= CANONICAL_WIDTH
        and top + height <= CANONICAL_HEIGHT
    ):
        return None
    return left, top, width, height


def _template_list(param: Mapping[str, Any]) -> tuple[str, ...] | None:
    value = param.get("template")
    if isinstance(value, str):
        value = (value,)
    if not isinstance(value, (list, tuple)) or not value:
        return None
    templates = tuple(item for item in value if isinstance(item, str) and item)
    return templates if len(templates) == len(value) else None


def _template_contract(
    *,
    path: Path,
    root: Path,
    node_name: str,
    node: Mapping[str, Any],
    role: str,
) -> ReplayTemplateContract | None:
    recognition = node.get("recognition")
    action = node.get("action")
    if not isinstance(recognition, Mapping) or not isinstance(action, Mapping):
        return None
    if recognition.get("type") != "TemplateMatch" or action.get("type") != "Click":
        return None
    param = recognition.get("param")
    action_param = action.get("param")
    if not isinstance(param, Mapping) or not isinstance(action_param, Mapping):
        return None
    templates = _template_list(param)
    roi = _as_int_tuple(param.get("roi"))
    if templates is None or roi is None:
        return None
    # A fixed target is not needed: Maa's Click action uses the recognized
    # box.  Reject it so a future replay node cannot smuggle a guessed point
    # into this narrow controller path.
    target = action_param.get("target", True)
    if target is not True:
        return None
    # Ensure every declared image exists in the same bundled resource.  A
    # path from an arbitrary location is not a valid replay contract.
    bundle_root = path.parent.parent
    for template in templates:
        image_path = (bundle_root / "image" / template).resolve()
        try:
            image_path.relative_to(bundle_root.resolve())
        except ValueError:
            return None
        if not image_path.is_file():
            return None
    return ReplayTemplateContract(
        node_name=node_name,
        source=str(path.relative_to(root)),
        role=role,
        templates=templates,
        roi=roi,
    )


def audit_bundled_replay_controls(
    root: Path,
) -> ReplayControlAudit:
    """Audit bundled replay controls without opening a game or sending input.

    Generic Produce nodes are intentionally reported as evidence only.  They
    cannot make ``skip_input_allowed`` true because their node name does not
    prove leaderboard replay scope.
    """

    root = Path(root).resolve()
    resource_present = root.is_dir()
    produce_skip_source: dict[str, Any] | None = None
    replay_skip: list[ReplayTemplateContract] = []
    replay_move_to_turn: list[ReplayTemplateContract] = []
    invalid_replay_nodes: list[str] = []
    pipeline_seen = False

    for path in _pipeline_files(root):
        pipeline = _load_pipeline(path)
        if pipeline is None:
            continue
        pipeline_seen = True
        for raw_name, raw_node in pipeline.items():
            if not isinstance(raw_name, str) or not isinstance(raw_node, Mapping):
                continue
            if raw_name == "ProduceRecognitionSkipRound":
                # Preserve only the authoritative upstream source metadata;
                # never copy its recognition box into an input contract.
                recognition = raw_node.get("recognition")
                action = raw_node.get("action")
                param = (
                    recognition.get("param")
                    if isinstance(recognition, Mapping)
                    else None
                )
                templates = (
                    _template_list(param) if isinstance(param, Mapping) else None
                )
                roi = _as_int_tuple(param.get("roi")) if isinstance(param, Mapping) else None
                if (
                    isinstance(recognition, Mapping)
                    and recognition.get("type") == "TemplateMatch"
                    and isinstance(action, Mapping)
                    and action.get("type") == "Click"
                    and templates is not None
                    and roi is not None
                    and produce_skip_source is None
                ):
                    produce_skip_source = {
                        "node": raw_name,
                        "source": str(path.relative_to(root)),
                        "templates": list(templates),
                        "roi": list(roi),
                        "scope": "formal-produce-only",
                    }

            normalized = _normalize_name(raw_name)
            if "replay" not in normalized:
                continue
            role: str | None = None
            if "skip" in normalized:
                role = "skip"
            elif "turn" in normalized and ("move" in normalized or "next" in normalized):
                role = "move-to-turn"
            if role is None:
                continue
            contract = _template_contract(
                path=path,
                root=root,
                node_name=raw_name,
                node=raw_node,
                role=role,
            )
            if contract is None:
                invalid_replay_nodes.append(raw_name)
            elif role == "skip":
                replay_skip.append(contract)
            else:
                replay_move_to_turn.append(contract)

    # Duplicate base/locale declarations are ambiguous even when their visual
    # images happen to be identical.  Keep every source in the audit and let
    # the caller refuse input unless there is exactly one contract.
    blockers: list[str] = []
    if not resource_present:
        blockers.append("maa-resource-root-missing")
    elif not pipeline_seen:
        blockers.append("maa-replay-pipeline-missing")
    if not replay_skip:
        blockers.append("replay-skip-template-missing")
        if produce_skip_source is not None:
            blockers.append("formal-produce-skip-is-not-replay-scoped")
    elif len(replay_skip) != 1:
        blockers.append("replay-skip-template-ambiguous")
    if invalid_replay_nodes:
        blockers.append("replay-template-contract-invalid")
    if not replay_move_to_turn:
        blockers.append("replay-move-to-turn-template-missing")
    elif len(replay_move_to_turn) != 1:
        blockers.append("replay-move-to-turn-template-ambiguous")

    return ReplayControlAudit(
        resource_root=root,
        resource_present=resource_present,
        produce_skip_source=produce_skip_source,
        replay_skip=tuple(replay_skip),
        replay_move_to_turn=tuple(replay_move_to_turn),
        blockers=tuple(dict.fromkeys(blockers)),
    )


__all__ = [
    "ReplayControlAudit",
    "ReplayTemplateContract",
    "audit_bundled_replay_controls",
]
