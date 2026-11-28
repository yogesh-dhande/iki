"""A record as the files its type exports (ADR 0171), at any of its revisions; and the
writer of each record that many records' one file is made of (ADR 0229).

A type names the fields each file is written from (``exports`` in its manifest), as it
names those Iki's viewers show (ADR 0085). The file is written here, from the record's
values at the revision asked for, so it's the same file for the same revision whoever
asks and whenever: :mod:`iki_server.entities.sequence_files` writes it, and the download
is audited (``entity.exported``) with the file's SHA-256.

A sequence export whose ``sequence`` is a reference, as a plasmid's is, writes the
referenced record's sequence under this record's name, with this record's description
(or else the sequence's) and its features after the sequence's. The referenced record is
the one in this record's project, as a run reads a reference (ADR 0105), read as it was
while the asked-for revision was this record's latest, as a record reads its registry
entry (ADR 0115).

Each format's file is written by a :class:`Writer`: ``begin``, ``record`` for each record
in order, then ``end``. Iki's own write each record whole, ending itself, so a file of
many is one after another: FASTA's, GenBank's (each ending ``//``), SDF's (``$$$$``). A
format that holds one record, such as a Molfile or a signed report, says so (``many:
one``). :mod:`iki_server.entities.record_exports` writes many records' file with these.

A plugin's format (ADR 0230) is written by its writer, a WebAssembly module, through
:class:`~iki_server.entities.plugin_writers.PluginWriter`, the same calls in the export
sandbox: the type's ``exports`` maps the writer's roles to its fields, and each record is
gathered here, as it was at the revision asked for, with what it refers to as its reader
can read it. Its download is pinned, hashed and audited as Iki's own are, its audit
event naming the plugin, its commit and the module's digest. A file of a format that
holds one record each (``many: zip``) is a ZIP of them, one per record.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Literal, Protocol

from django.db import transaction

from iki_server.accounts.models import User
from iki_server.audit import log as audit
from iki_server.entities import (
    access,
    lists,
    plugin_writers,
    registry,
    search_exports,
    signed_reports,
)
from iki_server.entities.compound_files import CompoundFile, NotAMolfile, molfile, sdf
from iki_server.entities.models import Entity, Revision
from iki_server.entities.plugin_writers import PluginRecord, PluginWriter
from iki_server.entities.sequence_files import (
    Organism,
    ProteinFile,
    SequenceFile,
    Written,
    fasta,
    genbank,
    protein_fasta,
)
from iki_server.entities.services import values_at
from iki_server.entities.values import reference_ids, shape, to_json
from iki_spec import titles
from iki_spec.model import (
    AnyExport,
    CompoundExport,
    EntityType,
    Export,
    PluginExport,
    ProteinExport,
    ReportExport,
)
from iki_spec.model import Field as SpecField
from iki_spec.writers import abi as writer_abi

__all__ = [
    "FORMATS",
    "Format",
    "Gathered",
    "Unexportable",
    "Writer",
    "data_fields",
    "export",
    "exports_of",
    "find_format",
    "format_of",
    "formats_of",
    "gather",
    "writer",
]


class Writer(Protocol):
    """Writes one file of a format, from records one at a time: ``begin``, then
    ``record`` for each, in the file's order, then ``end``. Each returns the bytes it adds
    to the file and what of its record the file couldn't hold. A writer that can't write a
    record raises :class:`Unexportable`, and no file is made. Iki's own writers run here;
    a plugin's (ADR 0230) will run the same calls in a sandbox."""

    #: Which code wrote the file, as an export's audit event says it.
    @property
    def identity(self) -> Mapping[str, str]: ...

    def begin(self, records: int) -> bytes: ...

    def record(self, record: Any) -> Written: ...  # noqa: ANN401 - what its export gathers

    def end(self) -> Written: ...


def _iki_version() -> str:
    try:
        return version("iki-server")
    except PackageNotFoundError:  # pragma: no cover - run from a source tree without it
        return "unknown"


class Joined:
    """A writer whose records each end themselves, so a file of them is one after another:
    FASTA's, GenBank's (``//``) and SDF's (``$$$$``)."""

    def __init__(self, name: str, one: Callable[[Any], Written]) -> None:
        self.identity: Mapping[str, str] = {"builtin": name, "version": _iki_version()}
        self._one = one

    def begin(self, records: int) -> bytes:
        return b""

    def record(self, record: Any) -> Written:  # noqa: ANN401 - what its export gathers
        return self._one(record)

    def end(self) -> Written:
        return Written(b"", [])


@dataclass(frozen=True)
class Format:
    """A file an export writes."""

    format: str
    label: str
    extension: str
    media_type: str
    description: str
    #: ``file``: any number of records make one file of it; ``one``: it holds one record;
    #: ``zip``: one record each, so many make a ZIP of them.
    many: Literal["file", "one", "zip"] = "file"
    #: The plugin whose writer writes it (ADR 0230); None for Iki's own.
    plugin: str | None = None
    #: What its files start with, as (offset, bytes), one of which a plugin's writer's
    #: file must: the signatures its format declares (ADR 0213).
    signatures: tuple[tuple[int, bytes], ...] = ()


FORMATS: dict[str, tuple[Format, ...]] = {
    "sequence": (
        Format(
            "genbank",
            "GenBank",
            "gb",
            "chemical/seq-na-genbank",
            "The bases, topology and features, with their qualifiers.",
        ),
        Format(
            "fasta",
            "FASTA",
            "fasta",
            "chemical/seq-na-fasta",
            "The bases, with the record's ID, name and topology in its header. FASTA holds "
            "no features.",
        ),
    ),
    "protein": (
        Format(
            "protein-fasta",
            "FASTA",
            "fasta",
            "chemical/seq-aa-fasta",
            "The residues in one-letter codes, with the record's ID, name, description and "
            "organism in its header.",
        ),
    ),
    "compound": (
        Format(
            "sdf",
            "SDF",
            "sdf",
            "chemical/x-mdl-sdfile",
            "The structure with its 2D coordinates, and the fields you choose as its data.",
        ),
        Format(
            "molfile",
            "Molfile",
            "mol",
            "chemical/x-mdl-molfile",
            "The structure with its 2D coordinates, under the record's name.",
            "one",
        ),
    ),
    "signed_report": (
        Format(
            "signed-report-html",
            "Signed report (HTML)",
            "html",
            "text/html; charset=utf-8",
            "A printable report with electronic signature manifestations and verification data.",
            "one",
        ),
        Format(
            "signed-report-json",
            "Signed report (JSON)",
            "json",
            "application/json",
            "The exact report and native signature proofs for verification.",
            "one",
        ),
    ),
}
_ONE: dict[str, Callable[[Any], Written]] = {
    "genbank": genbank,
    "fasta": fasta,
    "protein-fasta": protein_fasta,
    "sdf": sdf,
    "molfile": molfile,
}


def writer(file_format: str, *, background: bool = False) -> Writer:
    """A fresh writer of ``file_format``, for one file: one of Iki's own, or a plugin's
    (ADR 0230), which runs in the export sandbox, given longer in the ``background``.
    Close a plugin's when its file's written (:func:`closed`)."""
    if file_format in _ONE:
        return Joined(file_format, _ONE[file_format])
    found = plugin_writers.find(file_format)
    if found is None:
        raise Unexportable(f"No installed plugin writes {file_format}.", "no_export")
    return PluginWriter(
        found,
        deadline=plugin_writers.background_deadline()
        if background
        else plugin_writers.inline_deadline(),
    )


def closed(file_writer: Writer) -> None:
    """Ends a writer's process, if it has one."""
    if isinstance(file_writer, PluginWriter):
        file_writer.close()


def plugin_format(found: Any) -> Format | None:  # noqa: ANN401 - a spec Writer
    """A plugin's writer's format, as an export offers it: its format's (ADR 0213) media
    type and extension, under the writer's label. None if the format's gone, or a
    browser would run it."""
    declared = registry.file_formats().formats.get(found.format)
    if declared is None or not declared.extensions:
        return None
    media_type = declared.canonical
    if plugin_writers.is_active(media_type):
        return None
    return Format(
        format=found.qualified_name,
        label=found.label,
        extension=declared.extensions[0].removeprefix("."),
        media_type=media_type,
        description=found.description or f"{declared.label}, written by {found.plugin}.",
        many=found.many,
        plugin=found.plugin,
        signatures=tuple((s.offset, bytes.fromhex(s.hex)) for s in declared.signatures),
    )


def formats_of(export_: AnyExport) -> tuple[Format, ...]:
    """The files an export writes."""
    if isinstance(export_, PluginExport):
        found = plugin_writers.find(export_.export)
        chosen = plugin_format(found) if found is not None else None
        return (chosen,) if chosen is not None else ()
    return FORMATS[export_.export]


def find_format(file_format: str) -> Format | None:
    """Any installed format by its ID: Iki's own, or a plugin's writer's."""
    for formats in FORMATS.values():
        for fmt in formats:
            if fmt.format == file_format:
                return fmt
    found = plugin_writers.find(file_format)
    return plugin_format(found) if found is not None else None


def check_signature(chosen: Format, content: bytes, of: str) -> None:
    """Refuses a plugin's file that doesn't start as its format says its files do."""
    if chosen.signatures and not any(
        content.startswith(raw, offset) for offset, raw in chosen.signatures
    ):
        plugin_writers.failures.add(1, {"writer": chosen.format, "code": "signature"})
        raise Unexportable(
            f"{chosen.label}'s writer wrote {of} that doesn't start as a {chosen.label} file "
            "does, so it wasn't kept.",
            "writer_failed",
        )


#: The kinds of field an SDF's data holds, as text: not files, people or JSON.
SD_KINDS = frozenset(
    {"string", "text", "int", "float", "bool", "quantity", "enum", "state", "date", "datetime"}
    | {"ref", "list"}
)
#: What a file's name may hold, beyond letters and digits: the rest become underscores.
_UNSAFE = re.compile(r"[^\w .()+-]", re.ASCII)


class Unexportable(Exception):  # noqa: N818 - raised for its message, as Refused is
    """A record that can't be written as the file asked for, and why."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


def exports_of(entity_type: EntityType) -> tuple[AnyExport, ...]:
    """What a type exports: its own, or its nearest ancestor's."""
    return registry.effective(entity_type.qualified_name).exports


def format_of(type_name: str, file_format: str) -> tuple[AnyExport, Format] | None:
    """The export of a type that writes ``file_format``, and the format; None if none does."""
    entity_type = registry.types().get(type_name)
    if entity_type is None:
        return None
    return next(
        (
            (export_, fmt)
            for export_ in exports_of(entity_type)
            if not isinstance(export_, PluginExport) or export_.export == file_format
            for fmt in formats_of(export_)
            if fmt.format == file_format
        ),
        None,
    )


@dataclass(frozen=True)
class Downloaded:
    name: str
    media_type: str
    content: bytes
    left_out: list[str]


@dataclass(frozen=True)
class Gathered:
    """What a record's file is written from at one revision, as its writer takes it; its
    file's name alone; and every record read to gather it (itself, the sequence a
    plasmid's is, its organism, what its SD data refers to), which whoever takes the file
    must be able to read."""

    record: SequenceFile | ProteinFile | CompoundFile | PluginRecord
    name: str
    read: frozenset[uuid.UUID] = field(default_factory=frozenset)


def gather(
    user: User,
    entity: Entity,
    number: int,
    export_: Export | ProteinExport | CompoundExport | PluginExport,
    chosen: Format,
    *,
    data: Sequence[str] | None = None,
) -> Gathered:
    """The record at revision ``number``, as ``chosen``'s writer takes it, read as ``user``
    can read what it refers to. ``data`` names the fields an SDF carries (each the type's,
    of a kind SD holds), or else every one it can. Raises :class:`Unexportable`."""
    read: set[uuid.UUID] = {entity.pk}
    record: SequenceFile | ProteinFile | CompoundFile | PluginRecord
    if isinstance(export_, PluginExport):
        record = _plugin_record(user, entity, number, export_, read)
    elif isinstance(export_, ProteinExport):
        record = _protein_file(user, entity, number, export_, read)
    elif isinstance(export_, CompoundExport):
        record = _compound_file(user, entity, number, export_, data, read)
    else:
        record = _sequence_file(user, entity, number, export_, read)
    return Gathered(record, _named(record, chosen), frozenset(read))


def written_by(file_writer: Writer, gathered: Gathered) -> Written:
    """One record, as a writer writes it, saying which record a refusal is of."""
    try:
        return file_writer.record(gathered.record)
    except NotAMolfile as why:
        raise Unexportable(
            f"{gathered.record.hrid} can't be written: {why}.", "no_structure"
        ) from None
    except plugin_writers.WriterRefused as why:
        plugin_writers.failures.add(
            1, {"writer": str(file_writer.identity.get("writer")), "code": "refused"}
        )
        raise Unexportable(
            f"{gathered.record.hrid} can't be written: {why}", "writer_refused"
        ) from None


def edges(file_writer: Writer, entry: str, *args: Any) -> Any:  # noqa: ANN401 - begin's or end's
    """A writer's ``begin`` or ``end``, a plugin's refusal of the file said as Iki's are."""
    try:
        return getattr(file_writer, entry)(*args)
    except plugin_writers.WriterRefused as why:
        plugin_writers.failures.add(
            1, {"writer": str(file_writer.identity.get("writer")), "code": "refused"}
        )
        raise Unexportable(f"The file can't be written: {why}", "writer_refused") from None


def broke(error: plugin_writers.WriterBroke) -> Unexportable:
    return Unexportable(str(error), "writer_failed")


def export(
    user: User,
    entity: Entity,
    file_format: str,
    revision: int | None,
    *,
    data: Sequence[str] | None = None,
) -> Downloaded:
    """The record as ``file_format`` at ``revision`` (its latest if None), audited."""
    match = format_of(entity.type, file_format)
    if match is None:
        raise Unexportable(f"{entity.hrid} isn't exported as {file_format}.", "no_export")
    export_, chosen = match
    number = entity.revision if revision is None else revision
    if not 1 <= number <= entity.revision:
        raise Unexportable(f"{entity.hrid} has no revision {number}.", "not_found")
    if data is not None:
        if not isinstance(export_, CompoundExport) or chosen.format != "sdf":
            raise Unexportable(f"{chosen.label} holds no fields as data.", "no_data")
        check_data_fields(entity.type, export_, data)
    identity: Mapping[str, str]
    if isinstance(export_, ReportExport):
        try:
            snapshot = signed_reports.snapshot(user, entity, number, export_)
        except signed_reports.Unissued as error:
            raise Unexportable(str(error), "report_not_issued") from error
        issued = snapshot["record"]["issued_revision"]
        report = (
            signed_reports.html_file(snapshot)
            if chosen.format == "signed-report-html"
            else signed_reports.json_file(snapshot)
        )
        written = Written(report, [])
        name = f"{entity.hrid} (revision {issued}).{chosen.extension}"
        identity = {"builtin": chosen.format, "version": _iki_version()}
    else:
        gathered = gather(user, entity, number, export_, chosen, data=data)
        file_writer = writer(chosen.format)
        try:
            head = edges(file_writer, "begin", 1)
            one = written_by(file_writer, gathered)
            tail = edges(file_writer, "end")
        except plugin_writers.WriterBroke as error:
            raise broke(error) from None
        finally:
            closed(file_writer)
        written = Written(head + one.content + tail.content, [*one.left_out, *tail.left_out])
        if chosen.plugin is not None:
            check_signature(chosen, written.content, "a file")
        name = gathered.name
        identity = file_writer.identity
    with transaction.atomic():
        audit.record(
            "entity.exported",
            actor=user,
            target=entity,
            project=entity.owner_project,
            data={
                "format": chosen.format,
                "revision": number,
                "latest": number == entity.revision,
                "file": name,
                "sha256": hashlib.sha256(written.content).hexdigest(),
                "size": len(written.content),
                # What the file couldn't hold, as it says it (GenBank's COMMENT).
                "left_out": written.left_out,
                "writer": dict(identity),
                **({"fields": list(data)} if data is not None else {}),
            },
        )
    return Downloaded(name, chosen.media_type, written.content, written.left_out)


def file_name(
    record: SequenceFile | ProteinFile | CompoundFile | PluginRecord, chosen: Format
) -> str:
    """Its name, or its HRID, and the revision when it isn't the latest: ``pUC19.gb``."""
    return _named(record, chosen)


def _named(record: SequenceFile | ProteinFile | CompoundFile | PluginRecord, chosen: Format) -> str:
    base = " ".join(_UNSAFE.sub("_", record.name).split())[:100] or record.hrid
    if not record.latest:
        base = f"{base} (revision {record.revision})"
    return f"{base}.{chosen.extension}"


# --- What a record's files are written from ------------------------------------------------------


def _at(
    entity: Entity, number: int, *, reader: User | None = None
) -> tuple[dict[str, Any], str, Any]:
    """Its values at revision ``number`` as JSON, its title then, and when it was made.
    With a ``reader``, each reference to a record they can't read is ``"unreadable"``, as
    the API gives it (ADR 0067): what's written never names what its reader can't read."""
    form = shape(entity.type)
    fields = form.fields()
    raw = {n: v for n, v in values_at(entity, number).items() if n in fields}
    readable = (
        access.readable_reference_ids(reader, reference_ids(fields, raw))
        if reader is not None
        else None
    )
    values = {n: to_json(fields[n], v, readable_refs=readable) for n, v in raw.items()}
    entity_type = registry.types()[entity.type]
    title = titles.render(
        registry.effective(entity_type.qualified_name).display_title, values, entity.hrid, fields
    )
    when = (
        Revision.objects.filter(entity=entity, number=number)
        .values_list("created_at", flat=True)
        .first()
    )
    assert when is not None  # noqa: S101 - every revision up to its latest exists
    return values, title, when


def _plugin_record(
    user: User, entity: Entity, number: int, export_: PluginExport, read: set[uuid.UUID]
) -> PluginRecord:
    """The record as its writer's roles read it (ADR 0230): each mapped field's value, a
    quantity in the role's unit, a reference as the record it refers to then, one step
    deep. A reference its reader can't read isn't given; its role is ``withheld``."""
    found = plugin_writers.find(export_.export)
    if found is None:
        raise Unexportable(f"No installed plugin writes {export_.export}.", "no_export")
    values, title, when = _at(entity, number)
    fields = shape(entity.type).fields()
    roles: dict[str, Any] = {}
    withheld: list[str] = []
    for role_name, role in found.roles.items():
        field_name = export_.fields.get(role_name)
        if field_name is None or field_name not in fields:
            continue
        value = values.get(field_name)
        if value is None or value == "" or value == []:
            continue
        spec = fields[field_name]
        if role.type == "ref":
            try:
                target = _referenced(user, entity, number, value)
            except Unexportable:
                withheld.append(role_name)
                continue
            read.add(target.entity.pk)
            # The record it refers to, one step deep: what that refers to in turn is given
            # only where its reader can read it.
            their, their_title, _ = _at(target.entity, target.revision, reader=user)
            roles[role_name] = {
                "fields": their,
                "hrid": target.entity.hrid,
                "id": str(target.entity.pk),
                "name": their_title,
                "revision": target.revision,
                "type": target.entity.type,
            }
            continue
        try:
            roles[role_name] = plugin_writers.role_value(role.type, role.unit, spec.unit, value)
        except ValueError as error:  # a unit Iki can't convert, as the manifest let it be
            raise Unexportable(
                f"{entity.hrid}'s {field_name} can't be given in {role.unit}: {error}",
                "writer_refused",
            ) from None
    return PluginRecord(
        writer_abi.Record(
            id=entity.pk,
            hrid=entity.hrid,
            name=title,
            type=entity.type,
            revision=number,
            latest=number == entity.revision,
            revised_at=when,
            roles=roles,
            withheld=tuple(withheld),
        )
    )


def _protein_file(
    user: User, entity: Entity, number: int, export_: ProteinExport, read: set[uuid.UUID]
) -> ProteinFile:
    values, title, _ = _at(entity, number)
    residues = str(values.get(export_.sequence) or "")
    if not residues:
        raise Unexportable(f"{entity.hrid} has no residues to write.", "no_sequence")
    organism = (
        _organism(user, entity, number, values.get(export_.organism), read)
        if export_.organism
        else None
    )
    return ProteinFile(
        hrid=entity.hrid,
        name=title,
        residues=residues,
        revision=number,
        description=str(values.get(export_.description) or "") if export_.description else "",
        organism=organism.name if organism is not None else "",
        latest=number == entity.revision,
    )


def data_fields(type_name: str, export_: CompoundExport) -> list[str]:
    """The fields an SDF of this type can carry as data, in the type's order: every one of
    a kind SD holds as text, but its structure."""
    return [
        name
        for name, f in shape(type_name).fields().items()
        if name != export_.structure and _sd_kind(f)
    ]


def _sd_kind(f: SpecField) -> bool:
    if f.type == "list":
        return f.items is not None and f.items.type in SD_KINDS - {"list"}
    return f.type in SD_KINDS


def check_data_fields(type_name: str, export_: CompoundExport, wanted: Sequence[str]) -> None:
    """Refuses a field an SDF of the type can't carry, saying which and why."""
    fields = shape(type_name).fields()
    for name in wanted:
        f = fields.get(name)
        if f is None:
            raise Unexportable(f"{type_name} has no field `{name}`.", "no_field")
        if name == export_.structure or not _sd_kind(f):
            raise Unexportable(
                f"`{name}` can't be an SDF's data: "
                + (
                    "it's the structure itself."
                    if name == export_.structure
                    else "SD holds text, numbers, dates and references, not files, people or JSON."
                ),
                "no_field",
            )


def _compound_file(
    user: User,
    entity: Entity,
    number: int,
    export_: CompoundExport,
    data: Sequence[str] | None,
    read: set[uuid.UUID],
) -> CompoundFile:
    values, title, _ = _at(entity, number)
    structure = str(values.get(export_.structure) or "")
    if not structure:
        raise Unexportable(
            f"{entity.hrid} has no structure to write yet: its Molfile is computed once it's "
            "registered.",
            "no_structure",
        )
    fields = shape(entity.type).fields()
    names = list(data) if data is not None else data_fields(entity.type, export_)
    return CompoundFile(
        hrid=entity.hrid,
        uuid=str(entity.pk),
        name=title,
        revision=number,
        structure=structure,
        data=tuple(_sd_data(user, fields, values, names, read)),
        latest=number == entity.revision,
    )


def _sd_data(
    user: User,
    fields: Mapping[str, SpecField],
    values: Mapping[str, Any],
    names: Sequence[str],
    read: set[uuid.UUID],
) -> list[tuple[str, str]]:
    """Each field named that has a value, as text, as a search's CSV gives it (ADR 0175):
    a reference by its HRID, a state by its label, numbers in full; a list's items apart
    by "; ", as the small_molecule plugin's SDF import reads them. A quantity is a number
    in its field's unit, as the type gives it, as PubChem's SD fields are numbers in the
    units its documentation states."""
    ids: set[uuid.UUID] = set()
    for name in names:
        f, value = fields[name], values.get(name)
        if f.type == "ref" or (f.items is not None and f.items.type == "ref"):
            for one in value if isinstance(value, list) else [value]:
                if one:
                    ids.add(uuid.UUID(str(one)))
    readable = access.readable_reference_ids(user, ids) if ids else set()
    read |= readable
    hrids = {
        str(pk): hrid
        for pk, hrid in Entity.objects.filter(pk__in=readable).values_list("pk", "hrid")
    }
    # A lab list's option by its code (ADR 0270), as the search's download gives it.
    codes = {str(pk): code for pk, code in lists.codes_of(readable).items()}
    row = {
        "refs": {k: {"hrid": v, "code": codes.get(k)} for k, v in hrids.items()},
        "files": {},
        "people": {},
    }
    out = []
    for name in names:
        value = values.get(name)
        if value in (None, "", []):
            continue
        f = fields[name]
        if isinstance(value, list) and f.items is not None:
            # Items apart by "; ", as the small_molecule plugin's SDF import reads a list.
            item = _field_out(name, f.items)
            text = "; ".join(search_exports.text(item, one, row) for one in value)
        else:
            text = search_exports.text(_field_out(name, f), value, row)
        out.append((name, text))
    return out


def _field_out(name: str, f: SpecField) -> dict[str, Any]:
    """What :func:`search_exports.text` reads of a field."""
    return {
        "name": name,
        "type": f.type,
        "items": f.items.type if f.items is not None else None,
        "states": [{"name": n, "label": s.label} for n, s in f.states.items()],
    }


def _sequence_file(
    user: User, entity: Entity, number: int, export_: Export, read: set[uuid.UUID]
) -> SequenceFile:
    values, title, when = _at(entity, number)
    fields = shape(entity.type).fields()
    own = _features(values, export_.features)
    description = str(values.get(export_.description) or "") if export_.description else ""
    spec = fields.get(export_.sequence)
    if spec is not None and spec.type == "ref":
        target = _referenced(user, entity, number, values.get(export_.sequence))
        read.add(target.entity.pk)
        inner_type = registry.types()[target.entity.type]
        inner_export = next(e for e in exports_of(inner_type) if isinstance(e, Export))
        inner = _sequence_file(user, target.entity, target.revision, inner_export, read)
        return replace(
            inner,
            hrid=entity.hrid,
            name=title,
            revision=number,
            date=when.date(),
            description=description or inner.description,
            features=(*inner.features, *_beyond(own, inner.features)),
            sequence_of=target.entity.hrid,
            latest=number == entity.revision,
        )
    bases = str(values.get(export_.sequence) or "")
    topology = str(values.get(spec.topology) or "linear") if spec and spec.topology else "linear"
    stranded = values.get(spec.strands) if spec and spec.strands else None
    return SequenceFile(
        hrid=entity.hrid,
        name=title,
        bases=bases,
        topology=topology,
        revision=number,
        date=when.date(),
        molecule="RNA" if spec is not None and spec.format == "rna_sequence" else "DNA",
        strandedness=str(stranded) if stranded else None,
        description=description,
        accession=str(values.get(export_.accession) or "") if export_.accession else "",
        organism=(
            _organism(user, entity, number, values.get(export_.organism), read)
            if export_.organism
            else None
        ),
        features=own,
        latest=number == entity.revision,
    )


def _place(feature: dict[str, Any]) -> tuple[Any, ...]:
    parts = feature.get("parts") or ()
    return (
        feature.get("type"),
        feature.get("name"),
        feature.get("start"),
        feature.get("end"),
        feature.get("strand"),
        tuple((p.get("start"), p.get("end")) for p in parts if isinstance(p, dict)),
    )


def _beyond(
    own: tuple[dict[str, Any], ...], theirs: tuple[dict[str, Any], ...]
) -> tuple[dict[str, Any], ...]:
    """The record's own features but those its sequence's file already has: an annotation
    that found a part where the file says it is, with the same name, is one feature."""
    known = {_place(f) for f in theirs}
    return tuple(f for f in own if _place(f) not in known)


def _features(values: dict[str, Any], name: str) -> tuple[dict[str, Any], ...]:
    found = values.get(name) if name else None
    return tuple(f for f in found if isinstance(f, dict)) if isinstance(found, list) else ()


@dataclass(frozen=True)
class _Pinned:
    entity: Entity
    revision: int


def _referenced(user: User, owner: Entity, number: int, ref: Any) -> _Pinned:  # noqa: ANN401 - a UUID
    """The record a reference names, as a run reads it (ADR 0105): the record of its entry
    in ``owner``'s project, or else one ``user`` can read; at the revision it had while
    ``owner``'s revision was its latest."""
    if ref is None:
        raise Unexportable(f"{owner.hrid} names no sequence to export.", "no_sequence")
    readable = access.visible(user).filter(purged_at__isnull=True)
    found = readable.filter(pk=ref).first()
    if found is None:
        records = list(readable.filter(entry_id=ref).order_by("created_at"))
        held = [r for r in records if r.owner_project_id == owner.owner_project_id]
        found = next(iter(held or records), None)
    if found is None:
        raise Unexportable(
            f"{owner.hrid}'s sequence isn't a record you can read, so it can't be written.",
            "sequence_unreadable",
        )
    return _Pinned(found, _revision_then(found, owner, number))


def _revision_then(target: Entity, owner: Entity, number: int) -> int:
    """``target``'s latest revision while ``owner``'s revision ``number`` was its latest, as
    :func:`~iki_server.entities.services.entry_at` reads a record's entry."""
    after = owner.revisions.filter(number=number + 1).values_list("created_at", flat=True).first()
    then = Revision.objects.filter(entity=target)
    if after is not None:
        then = then.filter(created_at__lt=after)
    return then.order_by("-number").values_list("number", flat=True).first() or target.revision


def _organism(
    user: User,
    owner: Entity,
    number: int,
    ref: Any,  # noqa: ANN401 - a UUID
    read: set[uuid.UUID],
) -> Organism | None:
    """The taxon a reference to iki/core.Organism names, as ``user`` can read it then: its
    scientific name and NCBI Taxonomy ID. One they can't read is left out, as the record
    page leaves out its title."""
    if ref is None:
        return None
    try:
        pinned = _referenced(user, owner, number, ref)
    except Unexportable:
        return None
    read.add(pinned.entity.pk)
    values = values_at(pinned.entity, pinned.revision)
    taxon = values.get("taxon_id")
    return Organism(str(values.get("scientific_name") or ""), int(taxon) if taxon else None)
