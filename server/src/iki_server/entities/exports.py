"""A record as the files its type exports (ADR 0171), at any of its revisions.

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
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from typing import Any

from django.db import transaction

from iki_server.accounts.models import User
from iki_server.audit import log as audit
from iki_server.entities import access, registry, signed_reports
from iki_server.entities.models import Entity, Revision
from iki_server.entities.sequence_files import Organism, SequenceFile, Written, fasta, genbank
from iki_server.entities.services import values_at
from iki_server.entities.values import shape, to_json
from iki_spec import titles
from iki_spec.model import EntityType, Export, ReportExport

__all__ = ["FORMATS", "Format", "Unexportable", "export", "exports_of"]


@dataclass(frozen=True)
class Format:
    """A file a sequence export writes."""

    format: str
    label: str
    extension: str
    media_type: str
    description: str


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
    "signed_report": (
        Format(
            "signed-report-html",
            "Signed report (HTML)",
            "html",
            "text/html; charset=utf-8",
            "A printable report with electronic signature manifestations and verification data.",
        ),
        Format(
            "signed-report-json",
            "Signed report (JSON)",
            "json",
            "application/json",
            "The exact report and native signature proofs for verification.",
        ),
    ),
}
_WRITERS = {"genbank": genbank, "fasta": fasta}
#: What a file's name may hold, beyond letters and digits: the rest become underscores.
_UNSAFE = re.compile(r"[^\w .()+-]", re.ASCII)


class Unexportable(Exception):  # noqa: N818 - raised for its message, as Refused is
    """A record that can't be written as the file asked for, and why."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


def exports_of(entity_type: EntityType) -> tuple[Export | ReportExport, ...]:
    """What a type exports: its own, or its nearest ancestor's."""
    return registry.exports(entity_type)


@dataclass(frozen=True)
class Downloaded:
    name: str
    media_type: str
    content: bytes
    left_out: list[str]


def export(user: User, entity: Entity, file_format: str, revision: int | None) -> Downloaded:
    """The record as ``file_format`` at ``revision`` (its latest if None), audited."""
    entity_type = registry.types().get(entity.type)
    match = next(
        (
            (export_, fmt)
            for export_ in (exports_of(entity_type) if entity_type is not None else ())
            for fmt in FORMATS[export_.export]
            if fmt.format == file_format
        ),
        None,
    )
    if entity_type is None or match is None:
        raise Unexportable(f"{entity.hrid} isn't exported as {file_format}.", "no_export")
    export_, chosen = match
    number = entity.revision if revision is None else revision
    if not 1 <= number <= entity.revision:
        raise Unexportable(f"{entity.hrid} has no revision {number}.", "not_found")
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
    else:
        record = _sequence_file(user, entity, number, export_)
        written = _WRITERS[chosen.format](record)
        name = file_name(record, chosen)
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
            },
        )
    return Downloaded(name, chosen.media_type, written.content, written.left_out)


def file_name(record: SequenceFile, chosen: Format) -> str:
    """Its name, or its HRID, and the revision when it isn't the latest: ``pUC19.gb``."""
    base = " ".join(_UNSAFE.sub("_", record.name).split())[:100] or record.hrid
    if not record.latest:
        base = f"{base} (revision {record.revision})"
    return f"{base}.{chosen.extension}"


def _sequence_file(user: User, entity: Entity, number: int, export_: Export) -> SequenceFile:
    form = shape(entity.type)
    fields = form.fields()
    values = {n: to_json(fields[n], v) for n, v in values_at(entity, number).items() if n in fields}
    entity_type = registry.types()[entity.type]
    title = titles.render(registry.display_title(entity_type), values, entity.hrid, fields)
    when = (
        Revision.objects.filter(entity=entity, number=number)
        .values_list("created_at", flat=True)
        .first()
    )
    assert when is not None  # noqa: S101 - every revision up to its latest exists
    own = _features(values, export_.features)
    description = str(values.get(export_.description) or "") if export_.description else ""
    spec = fields.get(export_.sequence)
    if spec is not None and spec.type == "ref":
        target = _referenced(user, entity, number, values.get(export_.sequence))
        inner_type = registry.types()[target.entity.type]
        inner_export = next(e for e in exports_of(inner_type) if isinstance(e, Export))
        inner = _sequence_file(user, target.entity, target.revision, inner_export)
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
            _organism(user, entity, number, values.get(export_.organism))
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


def _organism(user: User, owner: Entity, number: int, ref: Any) -> Organism | None:  # noqa: ANN401 - a UUID
    """The taxon a reference to iki/core.Organism names, as ``user`` can read it then: its
    scientific name and NCBI Taxonomy ID. One they can't read is left out, as the record
    page leaves out its title."""
    if ref is None:
        return None
    try:
        pinned = _referenced(user, owner, number, ref)
    except Unexportable:
        return None
    values = values_at(pinned.entity, pinned.revision)
    taxon = values.get("taxon_id")
    return Organism(str(values.get("scientific_name") or ""), int(taxon) if taxon else None)
