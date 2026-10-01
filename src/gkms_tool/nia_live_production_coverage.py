"""Mechanical ownership audit for MaaGakumasu's N.I.A. production graph.

This is intentionally not another runner.  It pins every branch reachable
from the original ``ProduceEntryNIA`` node to the production component that
owns it.  If the bundled Maa resource adds or removes a branch, the audit
fails until the live implementation makes that ownership explicit.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final


DEFAULT_MAA_NIA_PIPELINE: Final = (
    Path(__file__).resolve().parents[2]
    / "_research"
    / "MaaGakumasu"
    / "assets"
    / "resource"
    / "base"
    / "pipeline"
    / "ProduceNIA.json"
)
DEFAULT_MAA_PIPELINE_ROOT: Final = DEFAULT_MAA_NIA_PIPELINE.parent


@dataclass(frozen=True, slots=True)
class NiaLiveNodeOwnership:
    maa_node: str
    owner: str
    authority: str


@dataclass(frozen=True, slots=True)
class NiaLiveProductionCoverage:
    original_nodes: tuple[str, ...]
    ownership: tuple[NiaLiveNodeOwnership, ...]
    missing_nodes: tuple[str, ...]
    stale_nodes: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.missing_nodes and not self.stale_nodes


@dataclass(frozen=True, slots=True)
class NiaLiveReachableGraph:
    """Every Maa node transitively reachable from ``ProduceEntryNIA``."""

    reachable_nodes: tuple[str, ...]
    defining_files: tuple[tuple[str, str], ...]
    unresolved_nodes: tuple[str, ...]
    duplicate_nodes: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.unresolved_nodes and not self.duplicate_nodes


_OWNERSHIP: Final = {
    "ProduceExit": (
        "InitialRegularLiveSurfaceReader._final_live_surface/advance_nia_post_live",
        "Produce LocalSave final lifecycle + Maa native orientation",
    ),
    "ProduceNIAFailedFlag": (
        "nia_live_outer.analyze_nia_outer_subpage",
        "Maa retry/challenge templates + Produce LocalSave CAS",
    ),
    "ProduceGeneration": (
        "MaaWin32Session.advance_nia_post_live",
        "original Maa ProduceEnd graph",
    ),
    "ProduceChooseNIAEventFlag": (
        "nia_live_outer._event_choice",
        "visible Maa action tiles + Produce LocalSave parameters",
    ),
    "ProduceCardsFlag": (
        "nia_live_exam.run_live_nia_plan3_exam",
        "ExamSave ordered Hand/GUID + MAA fixed slots",
    ),
    "ProduceRecognitionWorkOptions": (
        "nia_live_outer._work_choice",
        "Maa work rows + LocalSave parameter headroom",
    ),
    "ProduceWorkFlag": (
        "nia_live_outer._work_choice/_work_start",
        "Maa work page/start templates + Produce LocalSave CAS",
    ),
    "ProduceChooseGetFlag": (
        "nia_live_outer.analyze_nia_outer_subpage reward-choice",
        "Maa recommendation/choice templates + LocalSave CAS",
    ),
    "ProduceChooseStrengthenFlag": (
        "nia_live_outer.analyze_nia_outer_subpage card-operation",
        "Maa operation templates + fixed first-card policy",
    ),
    "ProduceChooseDeleteFlag": (
        "nia_live_outer.analyze_nia_outer_subpage card-operation",
        "Maa operation templates + fixed first-card policy",
    ),
    "ProduceKeepDrinkFlag": (
        "nia_live_outer.analyze_nia_outer_subpage drink-keep",
        "Maa checkbox/button templates + LocalSave CAS",
    ),
    "ProduceSkipChatFlag": (
        "nia_live_outer.analyze_nia_outer_subpage common-button",
        "original Maa templates + LocalSave CAS",
    ),
    "ProduceNIAButton": (
        "nia_live_outer.analyze_nia_outer_subpage common-button",
        "original Maa templates + LocalSave CAS",
    ),
    "ProduceMirrorFlag": (
        "nia_live_outer._mirror_choice",
        "LocalSave vote + Master thresholds + Maa row locations",
    ),
    "ProduceSkip": (
        "nia_live_outer.analyze_nia_outer_subpage common-button",
        "original Maa skip template + LocalSave CAS",
    ),
    "Click_1": (
        "nia_live_outer.read_live_nia_click1_fallback",
        "last-resort original Maa node + bounded unchanged LocalSave",
    ),
}


def _entry_node_name(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("ProduceEntryNIA.next must contain non-empty node names")
    name = value
    if name.startswith("[JumpBack]"):
        name = name.removeprefix("[JumpBack]")
    if not name:
        raise ValueError("ProduceEntryNIA.next contains an empty JumpBack target")
    return name


def audit_nia_live_production_coverage(
    pipeline_path: str | Path = DEFAULT_MAA_NIA_PIPELINE,
) -> NiaLiveProductionCoverage:
    path = Path(pipeline_path).resolve()
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Maa N.I.A. pipeline root must be an object")
    entry = raw.get("ProduceEntryNIA")
    if not isinstance(entry, dict):
        raise ValueError("Maa N.I.A. pipeline has no ProduceEntryNIA node")
    next_nodes = entry.get("next")
    if not isinstance(next_nodes, list):
        raise ValueError("ProduceEntryNIA.next must be a list")
    original = tuple(_entry_node_name(value) for value in next_nodes)
    if len(original) != len(set(original)):
        raise ValueError("ProduceEntryNIA.next contains duplicate node targets")
    original_set = set(original)
    missing = tuple(node for node in original if node not in _OWNERSHIP)
    stale = tuple(sorted(node for node in _OWNERSHIP if node not in original_set))
    owned = tuple(
        NiaLiveNodeOwnership(node, *_OWNERSHIP[node])
        for node in original
        if node in _OWNERSHIP
    )
    return NiaLiveProductionCoverage(original, owned, missing, stale)


def _node_target(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("Maa pipeline next must contain non-empty node names")
    target = re.sub(r"^\[[^]]+\]", "", value)
    if not target:
        raise ValueError("Maa pipeline contains an empty prefixed target")
    return target


def audit_nia_live_reachable_graph(
    pipeline_root: str | Path = DEFAULT_MAA_PIPELINE_ROOT,
) -> NiaLiveReachableGraph:
    """Traverse the bundled Maa graph instead of checking only entry children.

    The custom N.I.A. policy owns state decisions, while original Maa nodes own
    their fixed button transitions.  This audit proves that every transitive
    reference used by that graph is actually present in the shipped bundle;
    it does not pretend that an offline graph walk is authentic live evidence.
    """

    root = Path(pipeline_root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(root)
    nodes: dict[str, Mapping[str, object]] = {}
    origins: dict[str, Path] = {}
    duplicates: set[str] = set()
    for path in sorted(root.rglob("*.json"), key=lambda value: str(value).casefold()):
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            continue
        for name, value in raw.items():
            if not isinstance(name, str) or not name or not isinstance(value, dict):
                continue
            if name in nodes:
                duplicates.add(name)
                continue
            nodes[name] = value
            origins[name] = path

    pending = ["ProduceEntryNIA"]
    seen: set[str] = set()
    unresolved: set[str] = set()
    while pending:
        name = pending.pop(0)
        if name in seen:
            continue
        seen.add(name)
        node = nodes.get(name)
        if node is None:
            unresolved.add(name)
            continue
        raw_next = node.get("next", ())
        if raw_next is None:
            raw_next = ()
        if not isinstance(raw_next, list | tuple):
            raise ValueError(f"Maa pipeline node {name}.next must be a list")
        for value in raw_next:
            target = _node_target(value)
            if target not in seen:
                pending.append(target)

    reachable = tuple(sorted(seen))
    defining = tuple(
        (name, str(origins[name].relative_to(root)).replace("\\", "/"))
        for name in reachable
        if name in origins
    )
    return NiaLiveReachableGraph(
        reachable_nodes=reachable,
        defining_files=defining,
        unresolved_nodes=tuple(sorted(unresolved)),
        duplicate_nodes=tuple(sorted(duplicates)),
    )


__all__ = [
    "DEFAULT_MAA_NIA_PIPELINE",
    "DEFAULT_MAA_PIPELINE_ROOT",
    "NiaLiveReachableGraph",
    "NiaLiveNodeOwnership",
    "NiaLiveProductionCoverage",
    "audit_nia_live_production_coverage",
    "audit_nia_live_reachable_graph",
]
