"""Read-only decoder for Gakumas LocalSaveManager files.

The format and key derivation implemented here are the deterministic defaults from
Qua.LocalSaveManagement used by Campus.Common.LocalSaveManager. This module only
turns bytes into Python values; it has no file-writing API.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import struct
from typing import Any

from Crypto.Cipher import AES


_SEED = b"campus"
_AES_BLOCK_SIZE = 16
_BINARY_HEADER_SIZE = 8


class LocalSaveDecodeError(ValueError):
    """Raised when a file is not a valid, decodable Gakumas local save."""


@dataclass(frozen=True)
class LocalSaveEnvelope:
    save_data_version: int
    encrypted_body_size: int
    plaintext: bytes


def obfuscated_name(type_name: str, label: str | None = None) -> str:
    """Return the uppercase MD5 filename used by LocalSaveManagerBase."""
    if not type_name:
        raise ValueError("type_name must not be empty")
    source = type_name if not label else f"{type_name}_{label}"
    return hashlib.md5(source.encode("utf-8")).hexdigest().upper()


def derive_key_and_iv(type_name: str) -> tuple[bytes, bytes]:
    """Derive DefaultEncryptor's AES-128 key and per-type IV."""
    if not type_name:
        raise ValueError("type_name must not be empty")
    type_bytes = type_name.encode("utf-8")
    key = hashlib.md5(_SEED).digest()
    iv = hashlib.md5(_SEED + type_bytes).digest()
    return key, iv


def decode_local_save_bytes(data: bytes, type_name: str) -> LocalSaveEnvelope:
    """Validate and decrypt one complete LocalSaveManager file."""
    if len(data) < 12:
        raise LocalSaveDecodeError("file is shorter than the 12-byte envelope header")

    header_size = struct.unpack_from("<I", data, 0)[0]
    if header_size != _BINARY_HEADER_SIZE:
        raise LocalSaveDecodeError(
            f"unsupported binary header size {header_size}; expected {_BINARY_HEADER_SIZE}"
        )

    save_data_version, encrypted_body_size = struct.unpack_from("<iI", data, 4)
    body_offset = 4 + header_size
    actual_body_size = len(data) - body_offset
    if encrypted_body_size != actual_body_size:
        raise LocalSaveDecodeError(
            "encrypted body size mismatch: "
            f"header says {encrypted_body_size}, file contains {actual_body_size}"
        )
    if encrypted_body_size == 0 or encrypted_body_size % _AES_BLOCK_SIZE:
        raise LocalSaveDecodeError(
            "encrypted body size must be a non-zero multiple of the AES block size"
        )

    key, iv = derive_key_and_iv(type_name)
    padded = AES.new(key, AES.MODE_CBC, iv).decrypt(data[body_offset:])
    padding_size = padded[-1]
    if not 1 <= padding_size <= _AES_BLOCK_SIZE:
        raise LocalSaveDecodeError("invalid PKCS#7 padding length (wrong type or damaged file)")
    if padded[-padding_size:] != bytes([padding_size]) * padding_size:
        raise LocalSaveDecodeError("invalid PKCS#7 padding bytes (wrong type or damaged file)")

    return LocalSaveEnvelope(
        save_data_version=save_data_version,
        encrypted_body_size=encrypted_body_size,
        plaintext=padded[:-padding_size],
    )


def decode_local_save_file(path: str | Path, type_name: str) -> LocalSaveEnvelope:
    """Read and decode a file without opening it for write access."""
    return decode_local_save_bytes(Path(path).read_bytes(), type_name)


def decode_local_save_json(path: str | Path, type_name: str) -> Any:
    """Decode the JsonUtility payload from a local-save file."""
    envelope = decode_local_save_file(path, type_name)
    try:
        return json.loads(envelope.plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LocalSaveDecodeError("decrypted body is not valid UTF-8 JSON") from exc


def exam_support_card_permils(data: Any) -> list[dict[str, Any]]:
    """Extract support-card IDs and field-16-derived runtime permil values."""
    if not isinstance(data, dict) or not isinstance(data.get("supportCardList"), list):
        raise LocalSaveDecodeError("JSON does not contain ExamSaveData.supportCardList")

    result: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, card in enumerate(data["supportCardList"]):
        if not isinstance(card, dict):
            raise LocalSaveDecodeError(f"supportCardList[{index}] is not an object")
        card_id = card.get("_supportCardId")
        permil = card.get("_produceCardUpgradePermil")
        if (
            not isinstance(card_id, str)
            or not card_id.strip()
            or not isinstance(permil, int)
            or isinstance(permil, bool)
            or not 0 <= permil <= 1000
        ):
            raise LocalSaveDecodeError(
                f"supportCardList[{index}] lacks a valid card ID or upgrade permil"
            )
        if card_id in seen_ids:
            raise LocalSaveDecodeError(
                f"supportCardList contains duplicate support ID: {card_id}"
            )
        seen_ids.add(card_id)
        result.append(
            {
                "index": index,
                "supportCardId": card_id,
                "produceCardUpgradePermil": permil,
            }
        )
    return result


def exam_current_turn_support_state(data: Any) -> dict[str, Any]:
    """Extract exact current-turn support use and Hand lineage.

    These are direct ``ExamSaveData`` fields.  The decoder does not infer a
    support identity from an upgrade delta, nor does it reconcile any other
    zone; callers decide which independently bound session/checkpoint should
    be compared with this snapshot.
    """

    if not isinstance(data, dict):
        raise LocalSaveDecodeError("ExamSaveData JSON root must be an object")
    current_turn = data.get("currentTurn")
    remain_turn = data.get("remainTurn")
    if (
        not isinstance(current_turn, int)
        or isinstance(current_turn, bool)
        or current_turn < 1
    ):
        raise LocalSaveDecodeError("ExamSaveData.currentTurn must be an integer >= 1")
    if (
        not isinstance(remain_turn, int)
        or isinstance(remain_turn, bool)
        or remain_turn < 0
    ):
        raise LocalSaveDecodeError("ExamSaveData.remainTurn must be an integer >= 0")

    raw_used = data.get("turnUseSupportCardIdList")
    if not isinstance(raw_used, list) or any(
        not isinstance(value, str) or not value.strip() for value in raw_used
    ):
        raise LocalSaveDecodeError(
            "ExamSaveData.turnUseSupportCardIdList must be a list of support IDs"
        )
    if len(set(raw_used)) != len(raw_used):
        raise LocalSaveDecodeError(
            "ExamSaveData.turnUseSupportCardIdList contains duplicate support IDs"
        )

    raw_hand = data.get("handList")
    if not isinstance(raw_hand, list):
        raise LocalSaveDecodeError("ExamSaveData.handList must be a list")
    hand: list[dict[str, Any]] = []
    for index, raw_card in enumerate(raw_hand):
        if not isinstance(raw_card, dict):
            raise LocalSaveDecodeError(f"handList[{index}] is not an object")
        card_data = raw_card.get("_cardData")
        if not isinstance(card_data, dict):
            raise LocalSaveDecodeError(f"handList[{index}]._cardData is not an object")
        card_id = card_data.get("_id")
        effective_upgrade = card_data.get("_upgradeCount")
        base_upgrade = raw_card.get("_baseUpgradeCount")
        support_ids = raw_card.get("_supportUpgradeIdList")
        if not isinstance(card_id, str) or not card_id.strip():
            raise LocalSaveDecodeError(f"handList[{index}] card ID is invalid")
        for label, value in (
            ("effective upgrade", effective_upgrade),
            ("base upgrade", base_upgrade),
        ):
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not 0 <= value <= 3
            ):
                raise LocalSaveDecodeError(
                    f"handList[{index}] {label} must be an integer in 0..3"
                )
        if not isinstance(support_ids, list) or any(
            not isinstance(value, str) or not value.strip()
            for value in support_ids
        ):
            raise LocalSaveDecodeError(
                f"handList[{index}]._supportUpgradeIdList must be a list of IDs"
            )
        if len(set(support_ids)) != len(support_ids):
            raise LocalSaveDecodeError(
                f"handList[{index}] contains duplicate support upgrade IDs"
            )
        if effective_upgrade != base_upgrade + len(support_ids):
            raise LocalSaveDecodeError(
                f"handList[{index}] support lineage does not explain upgrade delta"
            )
        unknown_used = set(support_ids) - set(raw_used)
        if unknown_used:
            raise LocalSaveDecodeError(
                f"handList[{index}] support IDs are absent from current-turn use list"
            )
        hand.append(
            {
                "index": index,
                "cardId": card_id,
                "baseUpgradeCount": base_upgrade,
                "effectiveUpgradeCount": effective_upgrade,
                "supportUpgradeIdList": list(support_ids),
            }
        )
    return {
        "currentTurn": current_turn,
        "remainTurn": remain_turn,
        "turnUseSupportCardIdList": list(raw_used),
        "handList": hand,
    }
