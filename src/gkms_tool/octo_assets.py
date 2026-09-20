"""Read card artwork from Gakumas' on-disk Octo cache.

This module only reads static files below the game installation.  It never
opens the game process, reads process memory, injects code, or installs a
system-wide component.
"""

from __future__ import annotations

import hashlib
import re
import warnings
from dataclasses import dataclass
from pathlib import Path
from .application_paths import game_file
from typing import Collection, Iterator

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
from PIL import Image


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OCTO_ROOT = game_file('octo')
DEFAULT_CARD_ART_CACHE = PROJECT_ROOT / "var" / "card_art"
DEFAULT_UNITY_VERSION = "6000.0.77f1"

# Octo manifest settings used by the installed PC client.  The manifest is
# read locally and is never modified.
_MANIFEST_KEY = b"1nuv9td1bw1udefk"
_MANIFEST_IV = b"LvAUtf+tnz"
_CARD_ASSET_PATTERN = re.compile(
    r"^img_general_skillcard_"
    r"(?P<suffix>(?P<category>act|men|ido|sup|acc)-\d+_\d+)"
    r"(?:-(?P<character_id>[a-z0-9]+))?$"
)
_CARD_ID_PATTERN = re.compile(r"^p_card-\d+-(?P<suffix>.+)$")


@dataclass(frozen=True, slots=True)
class OctoAsset:
    id: int
    name: str
    size: int
    md5: str
    object_name: str = ""


def _read_varint(payload: memoryview, position: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while position < len(payload) and shift < 70:
        byte = payload[position]
        position += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, position
        shift += 7
    raise ValueError("invalid protobuf varint")


def _iter_protobuf_fields(
    payload: bytes | memoryview,
) -> Iterator[tuple[int, int, int | memoryview]]:
    view = payload if isinstance(payload, memoryview) else memoryview(payload)
    position = 0
    while position < len(view):
        tag, position = _read_varint(view, position)
        field_number = tag >> 3
        wire_type = tag & 7
        if field_number < 1:
            raise ValueError("invalid protobuf field number")
        if wire_type == 0:
            value, position = _read_varint(view, position)
        elif wire_type == 1:
            end = position + 8
            if end > len(view):
                raise ValueError("truncated fixed64 field")
            value = view[position:end]
            position = end
        elif wire_type == 2:
            length, position = _read_varint(view, position)
            end = position + length
            if end > len(view):
                raise ValueError("truncated length-delimited field")
            value = view[position:end]
            position = end
        elif wire_type == 5:
            end = position + 4
            if end > len(view):
                raise ValueError("truncated fixed32 field")
            value = view[position:end]
            position = end
        else:
            raise ValueError(f"unsupported protobuf wire type: {wire_type}")
        yield field_number, wire_type, value


def _parse_asset_message(payload: memoryview) -> OctoAsset:
    values: dict[int, int | str] = {}
    for number, wire_type, raw in _iter_protobuf_fields(payload):
        if number in (1, 4) and wire_type == 0:
            values[number] = int(raw)
        elif number in (3, 10, 11) and wire_type == 2:
            values[number] = bytes(raw).decode("utf-8")
    try:
        return OctoAsset(
            id=int(values[1]),
            name=str(values[3]),
            size=int(values.get(4, 0)),
            md5=str(values[10]),
            object_name=str(values.get(11, "")),
        )
    except KeyError as error:
        raise ValueError(f"Octo asset is missing field {error.args[0]}") from error


def parse_octo_asset_list(payload: bytes) -> tuple[OctoAsset, ...]:
    """Parse only ``assetBundleList`` (field 2) from the Octo protobuf."""

    assets: list[OctoAsset] = []
    for number, wire_type, raw in _iter_protobuf_fields(payload):
        if number == 2 and wire_type == 2:
            assets.append(_parse_asset_message(raw))  # type: ignore[arg-type]
    return tuple(assets)


def decrypt_octo_manifest(path: Path) -> bytes:
    encrypted = path.resolve().read_bytes()
    if len(encrypted) < 33 or encrypted[0] != 1:
        raise ValueError(f"unexpected Octo manifest envelope: {path}")
    key = hashlib.md5(_MANIFEST_KEY).digest()
    iv = hashlib.md5(_MANIFEST_IV).digest()
    decrypted = unpad(
        AES.new(key, AES.MODE_CBC, iv).decrypt(encrypted[1:]),
        AES.block_size,
        style="pkcs7",
    )
    if len(decrypted) < 16:
        raise ValueError(f"Octo manifest payload is truncated: {path}")
    # The first 16 decrypted bytes are an integrity digest.
    return decrypted[16:]


def latest_manifest_path(octo_root: Path = DEFAULT_OCTO_ROOT) -> Path:
    manifest_root = octo_root.resolve() / "pdb" / "400"
    candidates = list(manifest_root.glob("*/octocacheevai"))
    if not candidates:
        raise FileNotFoundError(f"Octo manifest not found below {manifest_root}")

    def revision(path: Path) -> tuple[int, float]:
        try:
            numeric = int(path.parent.name)
        except ValueError:
            numeric = -1
        return numeric, path.stat().st_mtime

    return max(candidates, key=revision)


def build_mask_bytes(asset_name: str) -> bytes:
    if not asset_name:
        raise ValueError("asset name must not be empty")
    encoded = asset_name.encode("ascii")
    mask = bytearray(len(encoded) * 2)
    left = 0
    right = len(mask) - 1
    for byte in encoded:
        mask[left] = byte
        mask[right] = (~byte) & 0xFF
        left += 2
        right -= 2
    rolling = 0x9B
    for byte in mask:
        rolling = (((rolling & 1) << 7) | (rolling >> 1)) ^ byte
    return bytes(byte ^ rolling for byte in mask)


def decrypt_asset_bundle_header(
    payload: bytes, asset_name: str, *, header_length: int = 256
) -> bytes:
    """Undo Octo's repeating XOR mask over an AssetBundle header."""

    if payload.startswith(b"Unity"):
        return payload
    mask = build_mask_bytes(asset_name)
    buffer = bytearray(payload)
    for index in range(min(header_length, len(buffer))):
        buffer[index] ^= mask[index % len(mask)]
    return bytes(buffer)


def asset_cache_path(octo_root: Path, asset: OctoAsset) -> Path:
    encoded_directory = ("A" + str(asset.id)).encode("ascii").hex()
    return (
        octo_root.resolve()
        / "v1"
        / "400"
        / str(asset.id % 10)
        / encoded_directory
        / asset.md5
    )


class OctoAssetIndex:
    def __init__(
        self,
        assets: tuple[OctoAsset, ...],
        *,
        octo_root: Path = DEFAULT_OCTO_ROOT,
    ) -> None:
        self.assets = assets
        self.octo_root = octo_root.resolve()
        self.by_name = {asset.name: asset for asset in assets}

    @classmethod
    def load(
        cls,
        octo_root: Path = DEFAULT_OCTO_ROOT,
        manifest_path: Path | None = None,
    ) -> "OctoAssetIndex":
        manifest = manifest_path or latest_manifest_path(octo_root)
        return cls(
            parse_octo_asset_list(decrypt_octo_manifest(manifest)),
            octo_root=octo_root,
        )

    def read_decrypted_bundle(self, asset_name: str) -> bytes:
        try:
            asset = self.by_name[asset_name]
        except KeyError as error:
            raise KeyError(f"Octo asset not found: {asset_name}") from error
        path = asset_cache_path(self.octo_root, asset)
        if not path.is_file():
            raise FileNotFoundError(f"cached Octo asset is missing: {path}")
        decrypted = decrypt_asset_bundle_header(path.read_bytes(), asset.name)
        if not decrypted.startswith(b"Unity"):
            raise ValueError(f"AssetBundle decryption failed: {asset.name}")
        return decrypted

    def extract_texture(
        self,
        asset_name: str,
        *,
        unity_version: str = DEFAULT_UNITY_VERSION,
    ) -> Image.Image:
        # Imported lazily so ordinary OCR/master workflows do not pay the
        # Unity parser startup cost.
        import UnityPy
        import UnityPy.config

        UnityPy.config.FALLBACK_UNITY_VERSION = unity_version
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            environment = UnityPy.load(self.read_decrypted_bundle(asset_name))
        textures = [obj for obj in environment.objects if obj.type.name == "Texture2D"]
        if not textures:
            raise ValueError(f"AssetBundle has no Texture2D: {asset_name}")
        preferred = next(
            (obj for obj in textures if obj.peek_name() == asset_name),
            textures[0],
        )
        return preferred.read().image.copy()

    def ensure_generic_card_art(
        self,
        cache_dir: Path = DEFAULT_CARD_ART_CACHE,
    ) -> dict[str, Path]:
        """Extract the generic Active/Mental card art needed for First mode."""

        return self.ensure_card_art(
            cache_dir,
            include_generic=True,
            categories=("act", "men"),
        )

    def ensure_card_art(
        self,
        cache_dir: Path = DEFAULT_CARD_ART_CACHE,
        *,
        character_id: str | None = None,
        include_generic: bool = True,
        categories: tuple[str, ...] = ("act", "men", "ido", "sup", "acc"),
        suffixes: Collection[str] | None = None,
    ) -> dict[str, Path]:
        """Extract generic art plus one character's localized art variants.

        Character variants such as ``img_general_skillcard_men-2_054-kllj``
        render differently but still map to the base Master suffix
        ``men-2_054``.  Restricting extraction to one character avoids loading
        every idol's duplicate artwork into the matcher.
        """

        if character_id is not None and re.fullmatch(r"[a-z0-9]+", character_id) is None:
            raise ValueError(f"invalid character id: {character_id!r}")
        selected_categories = frozenset(categories)
        selected_suffixes = frozenset(suffixes) if suffixes is not None else None
        unknown = selected_categories.difference({"act", "men", "ido", "sup", "acc"})
        if unknown:
            raise ValueError(f"unsupported card-art categories: {sorted(unknown)}")

        destination = cache_dir.resolve()
        destination.mkdir(parents=True, exist_ok=True)
        result: dict[str, Path] = {}
        for asset in self.assets:
            match = _CARD_ASSET_PATTERN.fullmatch(asset.name)
            if match is None or match.group("category") not in selected_categories:
                continue
            if selected_suffixes is not None and match.group("suffix") not in selected_suffixes:
                continue
            variant = match.group("character_id")
            if variant is None:
                if not include_generic:
                    continue
            elif variant != character_id:
                continue
            output = destination / f"{asset.name}.png"
            if not output.is_file():
                self.extract_texture(asset.name).save(output)
            result[asset.name] = output
        if not result:
            scope = f"character {character_id}" if character_id else "generic"
            raise ValueError(f"Octo manifest contains no {scope} skill-card artwork")
        return result


def card_asset_name(card_id: str) -> str:
    match = _CARD_ID_PATTERN.fullmatch(card_id)
    if match is None:
        raise ValueError(f"not a Produce card id: {card_id}")
    return "img_general_skillcard_" + match.group("suffix")


def card_suffix_from_asset(asset_name: str) -> str:
    match = _CARD_ASSET_PATTERN.fullmatch(asset_name)
    if match is None:
        raise ValueError(f"not a skill-card asset: {asset_name}")
    return match.group("suffix")


def card_character_from_asset(asset_name: str) -> str | None:
    match = _CARD_ASSET_PATTERN.fullmatch(asset_name)
    if match is None:
        raise ValueError(f"not a skill-card asset: {asset_name}")
    return match.group("character_id")
