"""Small project-local checkpoint for a screenshot-verified live lesson."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from .logic_engine import LogicExamState


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SESSION_PATH = PROJECT_ROOT / "var" / "lesson_session.json"
SESSION_SCHEMA_VERSION = 3
LESSON_TYPE_UNKNOWN = "ProduceStepLessonType_Unknown"
LESSON_TYPES = frozenset({
    "ProduceStepLessonType_LessonVocal",
    "ProduceStepLessonType_LessonDance",
    "ProduceStepLessonType_LessonVisual",
})


@dataclass(frozen=True, slots=True)
class LessonSession:
    logic_state: LogicExamState
    clear_target: int
    idol_card_id: str
    gimmick_group_id: str
    lesson_type: str = LESSON_TYPE_UNKNOWN
    run_id: str | None = None
    character_id: str | None = None
    produce_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": 1 if self.run_id is None else SESSION_SCHEMA_VERSION,
            "logic_state": asdict(self.logic_state),
            "clear_target": self.clear_target,
            "idol_card_id": self.idol_card_id,
            "gimmick_group_id": self.gimmick_group_id,
            "lesson_type": self.lesson_type,
        }
        if self.run_id is not None:
            if not self.run_id or not self.character_id or not self.produce_id:
                raise ValueError("run-scoped lesson session identity is incomplete")
            payload.update(
                {
                    "run_id": self.run_id,
                    "character_id": self.character_id,
                    "produce_id": self.produce_id,
                }
            )
        return payload

    def matches_run(
        self,
        *,
        run_id: str,
        idol_card_id: str,
        character_id: str,
        produce_id: str,
    ) -> bool:
        return (
            self.run_id == run_id
            and self.idol_card_id == idol_card_id
            and self.character_id == character_id
            and self.produce_id == produce_id
        )


def load_lesson_session(
    path: Path = DEFAULT_SESSION_PATH,
) -> LessonSession | None:
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") not in {1, 2, 3}:
        raise ValueError("不支援的課程 session 格式")
    raw_state = payload.get("logic_state")
    if not isinstance(raw_state, dict):
        raise ValueError("課程 session 缺少 logic_state")
    state = LogicExamState(**raw_state)
    state.validate(allow_completed=True)
    clear_target = payload.get("clear_target")
    idol_card_id = payload.get("idol_card_id")
    gimmick_group_id = payload.get("gimmick_group_id")
    if not isinstance(clear_target, int) or clear_target < 1:
        raise ValueError("課程 session 的 clear_target 無效")
    if not isinstance(idol_card_id, str) or not idol_card_id:
        raise ValueError("課程 session 的 idol_card_id 無效")
    if not isinstance(gimmick_group_id, str) or not gimmick_group_id:
        raise ValueError("課程 session 的 gimmick_group_id 無效")
    lesson_type = payload.get("lesson_type", LESSON_TYPE_UNKNOWN)
    if lesson_type != LESSON_TYPE_UNKNOWN and lesson_type not in LESSON_TYPES:
        raise ValueError("lesson session lesson_type is invalid")
    run_id = None
    character_id = None
    produce_id = None
    if payload.get("schema_version") in {2, 3}:
        run_id = payload.get("run_id")
        character_id = payload.get("character_id")
        produce_id = payload.get("produce_id")
        if not all(
            isinstance(value, str) and value
            for value in (run_id, character_id, produce_id)
        ):
            raise ValueError("lesson session run identity is invalid")
    return LessonSession(
        state,
        clear_target,
        idol_card_id,
        gimmick_group_id,
        lesson_type,
        run_id,
        character_id,
        produce_id,
    )


def save_lesson_session(
    logic_state: Mapping[str, Any] | LogicExamState,
    clear_target: int,
    *,
    idol_card_id: str,
    gimmick_group_id: str,
    lesson_type: str = LESSON_TYPE_UNKNOWN,
    run_id: str | None = None,
    character_id: str | None = None,
    produce_id: str | None = None,
    path: Path = DEFAULT_SESSION_PATH,
) -> LessonSession:
    state = (
        logic_state
        if isinstance(logic_state, LogicExamState)
        else LogicExamState(**dict(logic_state))
    )
    state.validate(allow_completed=True)
    session = LessonSession(
        state,
        int(clear_target),
        idol_card_id,
        gimmick_group_id,
        lesson_type,
        run_id,
        character_id,
        produce_id,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(session.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    return session
