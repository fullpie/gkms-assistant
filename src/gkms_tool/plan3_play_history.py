"""The exact successful-use history read by native PlayCardLesson (field 20).

Android IsFieldStatusTriggerStatusEffect filters Command.UseHand/UsePool and
!IsCostFailed, conditionally SkipLast(1) while a PlayingCard exists, then reads
LastOrDefault().Command.PlayingCard.Category. IsSelectLog is not this filter.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from functools import lru_cache
from pathlib import Path

from .master_db import DEFAULT_DATABASE


TRIGGER_ID = "e_trigger-start_play-play_card_lesson"
FIELD = "ProduceExamFieldStatusType_PlayCardLesson"
PHASE = "ProduceExamPhaseType_StartPlay"
ACTIVE_CATEGORY = "ProduceCardCategory_ActiveSkill"


@dataclass(frozen=True)
class SuccessfulCardUse:
    card_id: str
    upgrade: int
    category: str
    guid: str
    command_type: int
    source_index: int | None = None
    source: str = "native-playLogList"

    def __post_init__(self):
        if not self.card_id or not self.category or not isinstance(self.guid, str):
            raise ValueError("successful card history identity is incomplete")
        if type(self.upgrade) is not int or self.upgrade < 0 or self.command_type not in (2, 4):
            raise ValueError("successful card history upgrade/command is invalid")
        if self.source_index is not None and (type(self.source_index) is not int or self.source_index < 0):
            raise ValueError("native history source index is invalid")


@dataclass(frozen=True)
class Plan3PlayHistory:
    # Retain the whole ordered native lineage for audit, but only the last two
    # cards can affect the proved Last/SkipLast predicate and search dedup.
    records: tuple[SuccessfulCardUse, ...] = field(compare=False, hash=False)
    playing_card_present: bool = False
    semantic_tail: tuple[tuple[str, int, str], ...] = field(init=False)

    def __post_init__(self):
        records = tuple(record if isinstance(record, SuccessfulCardUse) else SuccessfulCardUse(**record)
                        for record in self.records)
        if type(self.playing_card_present) is not bool:
            raise ValueError("native PlayingCard presence is unknown")
        object.__setattr__(self, "records", records)
        object.__setattr__(self, "semantic_tail", tuple((r.card_id, r.upgrade, r.category) for r in records[-2:]))

    @classmethod
    def from_dict(cls, payload):
        return cls(tuple(payload["records"]), payload["playing_card_present"])

    def to_dict(self):
        return asdict(self)

    def previous_card(self):
        eligible = self.records[:-1] if self.playing_card_present else self.records
        return eligible[-1] if eligible else None

    def play_card_lesson(self):
        previous = self.previous_card()
        return previous is not None and previous.category == ACTIVE_CATEGORY

    def append_completed(self, *, card_id, upgrade, category, guid, command_type):
        record = SuccessfulCardUse(card_id, upgrade, category, guid, command_type,
                                   source="simulated-successful-use")
        return replace(self, records=(*self.records, record), playing_card_present=False)


@lru_cache(maxsize=4096)
def _category(identity, upgrade, database, modified_ns):
    from .logic_engine import load_master_card
    return load_master_card(identity, upgrade, database).category


def _card_identity(raw, *, allow_empty=False):
    if raw is None and allow_empty:
        return None
    if not isinstance(raw, Mapping) or not isinstance(raw.get("_cardData"), Mapping):
        raise ValueError("native history PlayingCard serializer is unavailable")
    data = raw["_cardData"]
    identity = data.get("_id")
    if identity == "" and allow_empty:
        return None
    if not isinstance(identity, str) or not identity:
        raise ValueError("native successful-use card ID is missing")
    upgrade = data.get("_upgradeCount")
    if type(upgrade) is not int or upgrade < 0 or not isinstance(raw.get("_guid"), str):
        raise ValueError("native successful-use upgrade/GUID is unavailable")
    return identity, upgrade, raw["_guid"]


def project_play_history(raw, *, database=DEFAULT_DATABASE, playing_card_present=None):
    """Read the actual list order; never infer history from current deck zones."""
    logs = raw.get("playLogList")
    if not isinstance(logs, list):
        raise ValueError("native playLogList is unavailable")
    if playing_card_present is None:
        if "playingCard" not in raw:
            raise ValueError("native PlayingCard presence is unavailable")
        playing = _card_identity(raw["playingCard"], allow_empty=True) is not None
    else:
        if type(playing_card_present) is not bool:
            raise ValueError("typed native PlayingCard presence must be boolean")
        playing = playing_card_present
    database = Path(database)
    records = []
    for index, row in enumerate(logs):
        if not isinstance(row, Mapping) or "_command" not in row:
            raise ValueError(f"playLogList[{index}] command is unavailable")
        command = row["_command"]
        if command is None:
            continue  # Native nullable Command does not match Where.
        if not isinstance(command, Mapping) or type(command.get("_playType")) is not int:
            raise ValueError(f"playLogList[{index}] command type is unavailable")
        if command["_playType"] not in (2, 4):
            continue
        failed = row.get("_isCostFailed")
        if type(failed) is not bool:
            raise ValueError(f"playLogList[{index}] cost result is unavailable")
        if failed:
            continue
        identity, upgrade, guid = _card_identity(command.get("_playingCard"))
        category = _category(identity, upgrade, database, database.stat().st_mtime_ns)
        records.append(SuccessfulCardUse(identity, upgrade, category, guid, command["_playType"], index))
    return Plan3PlayHistory(tuple(records), playing)


def matches_trigger_contract(trigger):
    expected = {"id": TRIGGER_ID, "phase_types": (PHASE,), "phase_values": (), "field_check_types": (),
        "field_types": (FIELD,), "field_values": (), "field_card_search_ids": (),
        "produce_card_search_id": "", "upper_search_count": 0, "lower_search_count": 0,
        "card_move_position_type": "ProduceCardMovePositionType_Unknown", "effect_types": (),
        "lesson_type": "ProduceStepLessonType_Unknown"}
    return all(getattr(trigger, key, None) == value for key, value in expected.items())
