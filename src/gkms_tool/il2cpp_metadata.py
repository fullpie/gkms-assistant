"""Small, read-only indexer for the game's IL2CPP metadata v31 file.

The full IL2CPP dump needs native code/metadata registration addresses.  This
module deliberately does less: it parses the unencrypted metadata tables that
remain useful even when those registrations are stripped.  The v31 layouts are
cross-checked against LibCpp2IL 2022.1.0-pre-release.21 (MIT, commit 12c8b44).
"""

from __future__ import annotations

import argparse
import json
import struct
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


METADATA_MAGIC = 0xFAB11BAF
SUPPORTED_VERSION = 31
HEADER_SIZE_V31 = 0x100

# Metadata v31 stores offset/byte-size pairs in this exact order.
SECTION_NAMES_V31 = (
    "string_literals",
    "string_literal_data",
    "strings",
    "events",
    "properties",
    "methods",
    "parameter_default_values",
    "field_default_values",
    "field_and_parameter_default_value_data",
    "field_marshaled_sizes",
    "parameters",
    "fields",
    "generic_parameters",
    "generic_parameter_constraints",
    "generic_containers",
    "nested_types",
    "interfaces",
    "vtable_methods",
    "interface_offsets",
    "type_definitions",
    "images",
    "assemblies",
    "field_refs",
    "referenced_assemblies",
    "attribute_data",
    "attribute_data_ranges",
    "unresolved_virtual_call_parameter_types",
    "unresolved_virtual_call_parameter_ranges",
    "windows_runtime_type_names",
    "windows_runtime_strings",
    "exported_type_definitions",
)

TYPE_DEFINITION = struct.Struct("<7iI8i8H2I")
FIELD_DEFINITION = struct.Struct("<iiI")
METHOD_DEFINITION = struct.Struct("<iiiIiiI4H")
PROPERTY_DEFINITION = struct.Struct("<iiiII")
IMAGE_DEFINITION = struct.Struct("<iiiIiIiIiI")


@dataclass(frozen=True, slots=True)
class MetadataSection:
    offset: int
    size: int


@dataclass(frozen=True, slots=True)
class FieldDefinition:
    index: int
    name: str
    type_index: int
    token: int


@dataclass(frozen=True, slots=True)
class MethodDefinition:
    index: int
    name: str
    declaring_type_index: int
    return_type_index: int
    parameter_start: int
    parameter_count: int
    flags: int
    token: int


@dataclass(frozen=True, slots=True)
class PropertyDefinition:
    index: int
    name: str
    getter_index: int
    setter_index: int
    token: int


@dataclass(frozen=True, slots=True)
class TypeDefinition:
    index: int
    namespace: str
    name: str
    first_field_index: int
    field_count: int
    first_method_index: int
    method_count: int
    first_property_index: int
    property_count: int
    flags: int
    bitfield: int
    token: int

    @property
    def full_name(self) -> str:
        return f"{self.namespace}.{self.name}" if self.namespace else self.name


@dataclass(frozen=True, slots=True)
class ImageDefinition:
    index: int
    name: str
    first_type_index: int
    type_count: int

    def contains_type(self, type_index: int) -> bool:
        return self.first_type_index <= type_index < self.first_type_index + self.type_count


@dataclass(frozen=True, slots=True)
class TypeDescription:
    image: str | None
    type: TypeDefinition
    fields: tuple[FieldDefinition, ...]
    methods: tuple[MethodDefinition, ...]
    properties: tuple[PropertyDefinition, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "image": self.image,
            "type": {**asdict(self.type), "full_name": self.type.full_name},
            "fields": [asdict(item) for item in self.fields],
            "methods": [asdict(item) for item in self.methods],
            "properties": [asdict(item) for item in self.properties],
        }


class Il2CppMetadataV31:
    """Validated, immutable view of the metadata tables needed by the probe."""

    def __init__(self, path: Path, data: bytes) -> None:
        self.path = path
        self._data = data
        self.magic, self.version = struct.unpack_from("<II", data, 0)
        if self.magic != METADATA_MAGIC:
            raise ValueError(f"invalid IL2CPP metadata magic: 0x{self.magic:08X}")
        if self.version != SUPPORTED_VERSION:
            raise ValueError(f"unsupported IL2CPP metadata version: {self.version}")
        if len(data) < HEADER_SIZE_V31:
            raise ValueError("metadata header is truncated")

        self.sections = self._read_sections()
        self._string_blob = self._section_bytes("strings")
        self.fields = self._read_fields()
        self.methods = self._read_methods()
        self.properties = self._read_properties()
        self.types = self._read_types()
        self.images = self._read_images()

    @classmethod
    def load(cls, path: str | Path) -> "Il2CppMetadataV31":
        source = Path(path)
        return cls(source, source.read_bytes())

    def _read_sections(self) -> dict[str, MetadataSection]:
        result: dict[str, MetadataSection] = {}
        for index, name in enumerate(SECTION_NAMES_V31):
            offset, size = struct.unpack_from("<ii", self._data, 8 + index * 8)
            if offset < 0 or size < 0 or offset + size > len(self._data):
                raise ValueError(f"metadata section {name} is outside the file")
            result[name] = MetadataSection(offset, size)
        return result

    def _section_bytes(self, name: str) -> bytes:
        section = self.sections[name]
        return self._data[section.offset : section.offset + section.size]

    def _records(self, name: str, layout: struct.Struct) -> Iterable[tuple[int, tuple[int, ...]]]:
        section = self.sections[name]
        if section.size % layout.size:
            raise ValueError(f"metadata section {name} has an invalid record size")
        for index, offset in enumerate(range(section.offset, section.offset + section.size, layout.size)):
            yield index, layout.unpack_from(self._data, offset)

    def string(self, index: int) -> str:
        if index < 0 or index >= len(self._string_blob):
            raise ValueError(f"string index is outside metadata: {index}")
        end = self._string_blob.find(b"\0", index)
        if end < 0:
            raise ValueError(f"unterminated metadata string at index {index}")
        return self._string_blob[index:end].decode("utf-8", "replace")

    def _read_fields(self) -> tuple[FieldDefinition, ...]:
        return tuple(
            FieldDefinition(index, self.string(values[0]), values[1], values[2])
            for index, values in self._records("fields", FIELD_DEFINITION)
        )

    def _read_methods(self) -> tuple[MethodDefinition, ...]:
        result = []
        for index, values in self._records("methods", METHOD_DEFINITION):
            result.append(
                MethodDefinition(
                    index=index,
                    name=self.string(values[0]),
                    declaring_type_index=values[1],
                    return_type_index=values[2],
                    parameter_start=values[4],
                    parameter_count=values[10],
                    flags=values[7],
                    token=values[6],
                )
            )
        return tuple(result)

    def _read_properties(self) -> tuple[PropertyDefinition, ...]:
        return tuple(
            PropertyDefinition(index, self.string(values[0]), values[1], values[2], values[4])
            for index, values in self._records("properties", PROPERTY_DEFINITION)
        )

    def _read_types(self) -> tuple[TypeDefinition, ...]:
        result = []
        for index, values in self._records("type_definitions", TYPE_DEFINITION):
            result.append(
                TypeDefinition(
                    index=index,
                    namespace=self.string(values[1]),
                    name=self.string(values[0]),
                    first_field_index=values[8],
                    field_count=values[18],
                    first_method_index=values[9],
                    method_count=values[16],
                    first_property_index=values[11],
                    property_count=values[17],
                    flags=values[7],
                    bitfield=values[24],
                    token=values[25],
                )
            )
        return tuple(result)

    def _read_images(self) -> tuple[ImageDefinition, ...]:
        return tuple(
            ImageDefinition(index, self.string(values[0]), values[2], values[3])
            for index, values in self._records("images", IMAGE_DEFINITION)
        )

    def image_for_type(self, type_index: int) -> str | None:
        return next((image.name for image in self.images if image.contains_type(type_index)), None)

    def describe(self, type_definition: TypeDefinition) -> TypeDescription:
        def take(items: tuple[object, ...], first: int, count: int) -> tuple[object, ...]:
            if first < 0 or count == 0:
                return ()
            if first + count > len(items):
                raise ValueError(f"type {type_definition.full_name} has an invalid metadata slice")
            return items[first : first + count]

        return TypeDescription(
            image=self.image_for_type(type_definition.index),
            type=type_definition,
            fields=take(self.fields, type_definition.first_field_index, type_definition.field_count),  # type: ignore[arg-type]
            methods=take(self.methods, type_definition.first_method_index, type_definition.method_count),  # type: ignore[arg-type]
            properties=take(self.properties, type_definition.first_property_index, type_definition.property_count),  # type: ignore[arg-type]
        )

    def find_types(self, terms: Iterable[str], image: str | None = None) -> tuple[TypeDescription, ...]:
        lowered = tuple(term.casefold() for term in terms if term)
        matches = []
        for item in self.types:
            item_image = self.image_for_type(item.index)
            if image is not None and item_image != image:
                continue
            if lowered and not any(term in item.full_name.casefold() for term in lowered):
                continue
            matches.append(self.describe(item))
        return tuple(matches)


def build_report(metadata: Il2CppMetadataV31, terms: Iterable[str], image: str | None) -> dict[str, object]:
    matches = metadata.find_types(terms, image)
    return {
        "source": str(metadata.path),
        "magic": f"0x{metadata.magic:08X}",
        "version": metadata.version,
        "type_count": len(metadata.types),
        "field_count": len(metadata.fields),
        "method_count": len(metadata.methods),
        "image_count": len(metadata.images),
        "matches": [item.to_dict() for item in matches],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Index unencrypted IL2CPP metadata v31 without loading the game")
    parser.add_argument("metadata", type=Path)
    parser.add_argument("--match", action="append", default=[])
    parser.add_argument("--image")
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    metadata = Il2CppMetadataV31.load(arguments.metadata)
    report = build_report(metadata, arguments.match, arguments.image)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if arguments.output is None:
        print(text)
    else:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(text + "\n", encoding="utf-8")
        print(arguments.output)


if __name__ == "__main__":
    main()
