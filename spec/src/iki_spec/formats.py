"""The installed plugins' file formats, their recognizers, and the actions that accept them."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Iterator
from functools import cached_property
from pathlib import PurePosixPath

from iki_spec import naming
from iki_spec.model import Action, Field, FileFormat, Plugin, included_reference

#: Uploads need at most this many bytes to distinguish registered signatures (ADR 0214).
MAX_SIGNATURE_BYTES = 4096


class AmbiguousSignatureError(ValueError):
    """Two equally specific signatures identified different formats in these bytes."""


class FormatRegistry:
    def __init__(self, plugins: Iterable[Plugin]) -> None:
        self._plugins = tuple(plugins)
        self.formats: dict[str, FileFormat] = {}
        self._media: dict[str, FileFormat] = {}
        self._extensions: dict[str, list[FileFormat]] = defaultdict(list)
        self._signatures: dict[tuple[int, bytes], FileFormat] = {}
        self.signature_bytes = 0
        for plugin in self._plugins:
            for key, fmt in plugin.formats.items():
                if key != fmt.id or naming.plugin_of(key) != plugin.name:
                    raise ValueError(f"Format {key} must be declared under {plugin.name}'s name.")
                self._register(fmt)

    def _register(self, fmt: FileFormat) -> None:
        if fmt.id in self.formats:
            raise ValueError(f"Format {fmt.id} is declared twice.")
        if other := self._media.get(fmt.id):
            raise ValueError(f"Format ID {fmt.id} is a media type of {other.id}.")
        self.formats[fmt.id] = fmt
        for media in fmt.media_types:
            if media == "application/octet-stream":
                raise ValueError(
                    f"{fmt.id} claims application/octet-stream, reserved for unknown bytes."
                )
            if (other := self.formats.get(media)) and other.id != fmt.id:
                raise ValueError(f"Media type {media} of {fmt.id} is another format's ID.")
            if other := self._media.get(media):
                raise ValueError(f"{fmt.id} and {other.id} both claim media type {media}.")
            self._media[media] = fmt
        for ext in fmt.extensions:
            owners = self._extensions[ext]
            if owners and naming.plugin_of(owners[0].id) != naming.plugin_of(fmt.id):
                raise ValueError(f"{fmt.id} and {owners[0].id} both claim extension {ext}.")
            owners.append(fmt)
        for signature in fmt.signatures:
            raw = bytes.fromhex(signature.hex)
            end = signature.offset + len(raw)
            if not raw or signature.offset < 0 or end > MAX_SIGNATURE_BYTES:
                raise ValueError(f"{fmt.id}'s signature must fit in the first 4096 bytes.")
            key = (signature.offset, raw)
            if other := self._signatures.get(key):
                raise ValueError(
                    f"{fmt.id} and {other.id} both claim signature {raw.hex()} "
                    f"at offset {signature.offset}."
                )
            self._signatures[key] = fmt
            self.signature_bytes = max(end, self.signature_bytes)

    def by_media_type(self, media_type: str) -> FileFormat | None:
        return self._media.get(media_type.partition(";")[0].strip().lower())

    def by_extension(self, name: str) -> tuple[FileFormat, ...]:
        """Formats for the longest matching suffix, in their owner's declaration order."""
        filename = PurePosixPath(name).name.lower()
        for i, character in enumerate(filename):
            if character == "." and i and (formats := self._extensions.get(filename[i:])):
                return tuple(formats)
        return ()

    def by_signature(self, prefix: bytes) -> FileFormat | None:
        """The longest matching signature. Equally specific conflicting evidence is an error."""
        longest = 0
        matches: dict[str, FileFormat] = {}
        for (offset, signature), fmt in self._signatures.items():
            size = len(signature)
            if size < longest or not prefix.startswith(signature, offset):
                continue
            if size > longest:
                matches.clear()
                longest = size
            matches[fmt.id] = fmt
        if len(matches) > 1:
            raise AmbiguousSignatureError(
                f"The bytes match multiple file formats: {', '.join(matches)}."
            )
        return next(iter(matches.values()), None)

    def recognize(
        self, name: str, prefix: bytes, media_type: str = "application/octet-stream"
    ) -> FileFormat | None:
        """Recognize checked bytes, then the longest filename extension, then the supplied type.

        A declaration with signatures lists the alternatives that can start every valid
        file. A specific supplied type contradicting those bytes is an error.
        """
        supplied = media_type.partition(";")[0].strip().lower()
        specific = supplied not in ("", "application/octet-stream")
        declared = self.by_media_type(supplied) if specific else None
        match = self.by_signature(prefix)
        if match is not None:
            if specific and declared != match:
                raise ValueError(
                    f"The bytes identify {match.label} ({match.canonical}), "
                    f"but the declared media type is {supplied}. Check the file and its type."
                )
            return match

        candidates = self.by_extension(name)
        # Shared suffixes (e.g. .fa) honor an explicit variant supported by the name.
        match = next((fmt for fmt in candidates if fmt == declared), None)
        if match is None:
            match = candidates[0] if candidates else declared

        for expected in (declared, match):
            if expected is not None and expected.signatures:
                raise ValueError(
                    f"The bytes don't have the signature for {expected.label}. "
                    "Check the file and its type."
                )
        return match

    def expand(self, choices: Iterable[str]) -> tuple[str, ...]:
        """Concrete media types and their aliases, preserving wildcards and unknown MIME types."""
        result: dict[str, None] = {}
        for choice in choices:
            fmt = self.formats.get(choice) or self.by_media_type(choice)
            result.update(dict.fromkeys(fmt.media_types if fmt else (choice,)))
            if choice.endswith("/*"):
                result.update(dict.fromkeys(self._family_aliases.get(choice, ())))
        return tuple(result)

    @cached_property
    def _family_aliases(self) -> dict[str, tuple[str, ...]]:
        """Types outside a wildcard's own family that store the same format as a member."""
        by_family: dict[str, set[str]] = defaultdict(set)
        for fmt in self.formats.values():
            for family in {t.partition("/")[0] for t in fmt.media_types}:
                by_family[f"{family}/*"].update(
                    t for t in fmt.media_types if not t.startswith(f"{family}/")
                )
        return {family: tuple(sorted(types)) for family, types in by_family.items()}

    def parsers(self, id: str) -> tuple[str, ...]:
        """Actions declaring this format on a file parameter or a field of an input type."""
        return self._parsers.get(id, ())

    @cached_property
    def _parsers(self) -> dict[str, tuple[str, ...]]:
        types = {k: t for plugin in self._plugins for k, t in plugin.types.items()}
        extensions = defaultdict(list)
        for plugin in self._plugins:
            for extension in plugin.extensions:
                extensions[extension.adds_to].append(extension)

        def record_fields(name: str) -> dict[str, Field]:
            fields: dict[str, Field] = {}
            seen: set[str] = set()
            while name not in seen and (owner := types.get(name)):
                seen.add(name)
                fields.update(owner.fields)
                for extension in extensions[name]:
                    fields.update(extension.fields)
                if not owner.extends:
                    break
                name = owner.extends
            return fields

        def inputs(action: Action) -> Iterator[Field]:
            yield from action.params.values()
            for put in action.inputs.values():
                fields = record_fields(put.type)
                yield from fields.values()
                for include in put.include:
                    ref = included_reference(fields, include)
                    if ref is not None:
                        for target in (ref.items or ref).to:
                            yield from record_fields(target).values()

        actions: dict[str, set[str]] = defaultdict(set)
        for plugin in self._plugins:
            for action in plugin.actions.values():
                choices = {choice for field in inputs(action) for choice in _file_choices(field)}
                allowed = set(self.expand(choices))
                for fmt in self.formats.values():
                    if any(
                        t in allowed or f"{t.split('/')[0]}/*" in allowed for t in fmt.media_types
                    ):
                        actions[fmt.id].add(action.qualified_name)
        return {id: tuple(sorted(names)) for id, names in actions.items()}


def _file_choices(field: Field) -> Iterator[str]:
    """Only explicitly constrained files identify an action as consuming a particular format."""
    if field.removed:
        return
    if field.type == "file":
        yield from field.media_types
    elif field.items:
        yield from _file_choices(field.items)
    else:
        for cell in field.fields.values():
            yield from _file_choices(cell)
