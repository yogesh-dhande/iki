"""A project's Files (ADR 0177): the files in a project, or in every project their reader
can read, found by name or what they state (ADR 0224), format, when they were acquired, the
sample they belong to and the run that read or made them; and, for each, what uses it.

**Which files.** A project's files are those added to it, and those its records hold in
a file field, at any revision (:func:`iki_server.entities.access.project_files`). Across
projects, they're every file the reader can read
(:func:`iki_server.entities.access.readable_files`). Either way they're two disjoint
sets, each found through its own index: files added to the projects (by project and the
sort's columns), and files from elsewhere that their records hold (through the records'
uses). A page takes the first rows of each in the page's order and merges them, so
neither set's size slows the other down, and no `OR` hides an index from Postgres.

**Pages** are keyset pages: a cursor holds the last row's sort values and ID, so a page
starts where the one before ended, however many files there are.

**Searching** first asks the trigram indexes which files' names, or what they state
(their instrument, operator, read groups and so on), hold the text, up to :data:`NAMED_MAX`.
A rare text, as most searched for are, is then a page of those few; a common one is found
by reading the page's order until a page matches, which is quick because so many do.
Postgres can't tell which it is from the text: it would read a million files in order for
a name that only one has.

**Samples.** A file belongs to a sample when a record of a sample type (one whose type
says ``sample: true``, or extends one) holds it in a file field, or a run that read the
sample read or made it. Plugins say which of their types are samples, so this holds for
any lab's.

**Used by** counts and lists only what the reader can read: records they can read, and
runs in projects whose data they can read. Nothing else is named or counted.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal

from django.core import signing
from django.db import connection, transaction
from django.db.models import Count, Exists, F, OuterRef, Q, QuerySet
from django.db.models.functions import Collate, Lower

from iki_server.accounts.models import User
from iki_server.entities import access, registry
from iki_server.entities.history import FieldNames
from iki_server.entities.models import Entity, ProjectLink, Revision
from iki_server.entities.values import shape
from iki_server.files.models import File, FileUse
from iki_server.org.models import Project
from iki_spec.model import Field

Sort = Literal["newest", "oldest", "name", "name_desc", "acquired", "acquired_oldest"]
SORTS: tuple[Sort, ...] = ("newest", "oldest", "name", "name_desc", "acquired", "acquired_oldest")
#: Counts stop here, as search everything's do (ADR 0176): past it, "1,000+".
COUNT_CAP = 1000
#: A search that the trigram indexes find no more than this many files for lists them
#: from those; one that finds more reads the list's order.
NAMED_MAX = 5000
#: The shortest text the trigram index finds: shorter is read in the list's order.
TRIGRAM = 3
_SALT = "iki.files.cursor"


class CursorError(ValueError):
    """A cursor from another list, another order, or forged."""


@dataclass(frozen=True)
class Filters:
    q: str = ""
    #: Format IDs, media types or families such as ``image/*``.
    formats: tuple[str, ...] = ()
    acquired_from: dt.date | None = None
    acquired_to: dt.date | None = None
    #: The sample entity whose files these are.
    sample: Entity | None = None
    #: The run that read or made them.
    run: Any = None
    #: Only files of these media types or families a field or param takes, and this small.
    accepts: tuple[str, ...] = ()
    max_size: int | None = None
    #: The files whose names or stated text hold ``q``, where the trigram indexes found few
    #: enough (:func:`named`); None to read the list's order instead.
    named: tuple[uuid.UUID, ...] | None = None


_PLANNED = ("enable_seqscan", "enable_indexscan", "enable_indexonlyscan")


def _text_match(q: str) -> Q:
    """Its name, or what it states, holds ``q``."""
    return Q(name__icontains=q) | Q(stated_text__icontains=q)


def named(filters: Filters) -> Filters:
    """``filters``, with the files whose names or stated text hold its text if the trigram
    indexes find no more than :data:`NAMED_MAX` of them in all. The indexes are read on
    their own (bitmaps, or'd), never in another index's order, so a rare text costs what its
    few files do."""
    q = filters.q.strip()
    if len(q) < TRIGRAM or filters.named is not None:
        return filters
    statement, params = (
        File.objects.filter(_text_match(q)).values_list("pk", flat=True)[: NAMED_MAX + 1]
    ).query.sql_with_params()
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SELECT " + ", ".join(f"current_setting('{n}')" for n in _PLANNED))
        before = cursor.fetchone()
        cursor.execute("SELECT " + ", ".join(f"set_config('{n}', 'off', true)" for n in _PLANNED))
        cursor.execute(statement, params)
        found = tuple(row[0] for row in cursor.fetchall())
        # Inside a transaction already, what follows is planned as it would have been.
        cursor.execute(
            "SELECT " + ", ".join(f"set_config('{n}', %s, true)" for n in _PLANNED), list(before)
        )
    return replace(filters, named=found if len(found) <= NAMED_MAX else None)


# --- Which files -------------------------------------------------------------------------------


def branches(user: Any, project: Project | None) -> tuple[QuerySet[File], QuerySet[File]]:  # noqa: ANN401 - User
    """The two disjoint sets of files listed: those added to the project (or to any the
    user can read), and those from elsewhere that its records (or any they can read)
    hold."""
    if project is not None:
        held_here = access.visible(user).filter(
            Exists(ProjectLink.objects.filter(entity=OuterRef("pk"), project=project))
        )
        # Found from the records' uses, by the files' keys: never by reading every file
        # in order to ask each whether a record holds it.
        uses = FileUse.objects.filter(entity__in=held_here.values("pk")).values("file_id")
        return (
            File.objects.filter(project=project),
            File.objects.filter(pk__in=uses).exclude(project=project),
        )
    projects = access.readable_projects(user).values("pk")
    uses = FileUse.objects.filter(entity__in=access.visible(user).values("pk")).values("file_id")
    return (
        File.objects.filter(project__in=projects),
        File.objects.filter(pk__in=uses).exclude(project__in=projects),
    )


def filtered(user: User, files: QuerySet[File], filters: Filters) -> QuerySet[File]:
    from iki_server.runs.models import RunFile, RunInput
    from iki_server.runs.services import visible_runs

    if filters.q.strip():
        files = files.filter(_text_match(filters.q.strip()))
        if filters.named is not None:
            files = files.filter(pk__in=filters.named)
    if filters.formats:
        files = files.filter(_formats(filters.formats))
    if filters.accepts:
        files = files.filter(_formats(filters.accepts))
    if filters.max_size is not None:
        files = files.filter(blob__size__lte=filters.max_size)
    if filters.acquired_from is not None:
        files = files.filter(acquired_on__gte=filters.acquired_from)
    if filters.acquired_to is not None:
        files = files.filter(acquired_on__lte=filters.acquired_to)
    if filters.run is not None:
        files = files.filter(Exists(RunFile.objects.filter(file=OuterRef("pk"), run=filters.run)))
    if filters.sample is not None:
        held = FileUse.objects.filter(file=OuterRef("pk"), entity=filters.sample)
        read_with = RunFile.objects.filter(
            file=OuterRef("pk"),
            run__in=RunInput.objects.filter(
                entity=filters.sample, run__in=visible_runs(user)
            ).values("run_id"),
        )
        files = files.filter(Q(Exists(held)) | Q(Exists(read_with)))
    return files


def _formats(formats: Iterable[str]) -> Q:
    """Filter using stored media types, including historical aliases of each format."""
    expanded = registry.file_formats().expand(formats)
    exact = [f for f in expanded if not f.endswith("/*")]
    q = Q(media_type__in=exact) if exact else Q(pk__in=[])
    for family in (f for f in expanded if f.endswith("/*")):
        q |= Q(media_type__startswith=family[:-1])
    return q


# --- Pages -------------------------------------------------------------------------------------


#: Names sort by their lowercase bytes ("C"), as Python compares them: a page merges two
#: sets' rows, which must agree on their order with Postgres, or a row could be skipped.
_NAME = Collate(Lower("name"), "C")
_DESCENDING = frozenset({"newest", "name_desc", "acquired"})


def _order(sort: Sort) -> list[Any]:
    match sort:
        case "newest":
            return [F("id").desc()]
        case "oldest":
            return [F("id").asc()]
        case "name":
            return [F("lname").asc(), F("id").asc()]
        case "name_desc":
            return [F("lname").desc(), F("id").desc()]
        case "acquired":
            return [F("acquired_on").desc(nulls_last=True), F("id").desc()]
        case "acquired_oldest":
            return [F("acquired_on").asc(nulls_last=True), F("id").asc()]


def _key(file: File, sort: Sort) -> tuple[Any, ...]:
    """A file's place in the order: what sorts it, then its ID. A name's is Postgres's
    lowercase, not Python's, which differ for a few letters."""
    if sort in ("name", "name_desc"):
        return (file.lname, file.pk)  # type: ignore[attr-defined]
    if sort in ("acquired", "acquired_oldest"):
        return (file.acquired_on, file.pk)
    return (file.pk,)


def _merge_key(sort: Sort, key: tuple[Any, ...]) -> tuple[Any, ...]:
    """`key` as Python sorts it, ascending or (for :data:`_DESCENDING`) reversed, in the
    page's order: unknown acquisitions last either way."""
    if sort == "acquired":
        return (key[0] is not None, key[0] or dt.date.min, key[1])
    if sort == "acquired_oldest":
        return (key[0] is None, key[0] or dt.date.min, key[1])
    return key


def _after(sort: Sort, key: Sequence[Any]) -> Q:
    """Files after `key` in the order."""
    op = "lt" if sort in _DESCENDING else "gt"
    if sort in ("newest", "oldest"):
        return Q(**{f"pk__{op}": key[0]})
    if sort in ("name", "name_desc"):
        return Q(**{f"lname__{op}": key[0]}) | Q(lname=key[0], **{f"pk__{op}": key[1]})
    # By when acquired, unknown last either way.
    on, pk = key
    if on is None:
        return Q(acquired_on__isnull=True, **{f"pk__{op}": pk})
    return (
        Q(**{f"acquired_on__{op}": on})
        | Q(acquired_on=on, **{f"pk__{op}": pk})
        | Q(acquired_on__isnull=True)
    )


def encode(sort: Sort, key: tuple[Any, ...]) -> str:
    plain = [
        v.isoformat() if isinstance(v, dt.date) else str(v) if v is not None else None for v in key
    ]
    return signing.Signer(salt=_SALT).sign_object([sort, *plain], compress=True)


def decode(sort: Sort, cursor: str) -> tuple[Any, ...]:
    try:
        found = signing.Signer(salt=_SALT).unsign_object(cursor)
    except (signing.BadSignature, ValueError):
        raise CursorError from None
    if not isinstance(found, list) or not found or found[0] != sort:
        raise CursorError
    values = found[1:]
    try:
        if sort in ("newest", "oldest") and len(values) == 1:
            return (uuid.UUID(values[0]),)
        if sort in ("name", "name_desc") and len(values) == 2:
            return (str(values[0]), uuid.UUID(values[1]))
        if sort in ("acquired", "acquired_oldest") and len(values) == 2:
            on = dt.date.fromisoformat(values[0]) if values[0] is not None else None
            return (on, uuid.UUID(values[1]))
    except (TypeError, ValueError):
        pass
    raise CursorError


def page(
    user: Any,  # noqa: ANN401 - User
    project: Project | None,
    filters: Filters,
    *,
    sort: Sort = "newest",
    cursor: str | None = None,
    limit: int = 50,
) -> tuple[list[File], str | None]:
    """A page of files, in `sort`'s order, and the cursor for the next one, if any."""
    after = decode(sort, cursor) if cursor else None
    filters = named(filters)
    found: list[File] = []
    for files in branches(user, project):
        files = filtered(user, files, filters).annotate(lname=_NAME)
        if after is not None:
            files = files.filter(_after(sort, after))
        found += (
            files.defer("read_groups", "stated_text")
            .select_related("blob", "created_by", "agent")
            .order_by(*_order(sort))[: limit + 1]
        )
    found.sort(key=lambda f: _merge_key(sort, _key(f, sort)), reverse=sort in _DESCENDING)
    more = len(found) > limit
    found = found[:limit]
    return found, encode(sort, _key(found[-1], sort)) if more and found else None


def count(user: Any, project: Project | None, filters: Filters) -> tuple[int, bool]:  # noqa: ANN401 - User
    """How many files match, up to :data:`COUNT_CAP`, and whether there are more."""
    filters = named(filters)
    total = 0
    for files in branches(user, project):
        total += filtered(user, files, filters).values("pk")[: COUNT_CAP + 1 - total].count()
        if total > COUNT_CAP:
            return COUNT_CAP, True
    return total, False


def formats(user: Any, project: Project | None, filters: Filters) -> list[tuple[str, int]]:  # noqa: ANN401 - User
    """The media types of the files that match (other than by format), with how many of
    each, the most common first."""
    counts: dict[str, int] = {}
    catalog = registry.file_formats()
    unformatted = named(replace(filters, formats=()))
    for files in branches(user, project):
        rows = filtered(user, files, unformatted).values("media_type").annotate(n=Count("pk"))
        for row in rows:
            media_type = row["media_type"]
            declaration = catalog.by_media_type(media_type)
            canonical = declaration.canonical if declaration else media_type
            counts[canonical] = counts.get(canonical, 0) + row["n"]
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


# --- What uses each ----------------------------------------------------------------------------


def used_by_counts(user: Any, files: Sequence[File]) -> dict[Any, tuple[int, int]]:  # noqa: ANN401 - User
    """For each file, how many records the user can read hold it, and how many runs they
    can read read or made it."""
    from iki_server.runs.models import RunFile
    from iki_server.runs.services import visible_runs

    ids = [f.pk for f in files]
    records = dict(
        FileUse.objects.filter(file_id__in=ids, entity__in=access.visible(user).values("pk"))
        .values("file_id")
        .annotate(n=Count("entity_id", distinct=True))
        .values_list("file_id", "n")
    )
    runs = dict(
        RunFile.objects.filter(file_id__in=ids, run__in=visible_runs(user).values("pk"))
        .values("file_id")
        .annotate(n=Count("run_id", distinct=True))
        .values_list("file_id", "n")
    )
    return {pk: (records.get(pk, 0), runs.get(pk, 0)) for pk in ids}


def holding_records(user: Any, file: File) -> QuerySet[Entity]:  # noqa: ANN401 - User
    """The records the user can read that hold the file, at any revision."""
    return access.visible(user).filter(
        Exists(FileUse.objects.filter(file=file, entity=OuterRef("pk")))
    )


def using_runs(user: Any, file: File) -> QuerySet[Any]:  # noqa: ANN401 - User
    """The runs the user can read that read or made the file."""
    from iki_server.runs.models import RunFile
    from iki_server.runs.services import visible_runs

    return visible_runs(user).filter(Exists(RunFile.objects.filter(file=file, run=OuterRef("pk"))))


def file_fields(entity_type: str) -> dict[str, Field]:
    """A type's fields that can hold files: file fields, lists of files, and lists of rows
    with a file in them."""
    out = {}
    for name, f in shape(entity_type).fields().items():
        holds = f.type == "file" or (
            f.type == "list"
            and f.items is not None
            and (
                f.items.type == "file"
                or (f.is_rows and any(c.type == "file" for c in f.items.fields.values()))
            )
        )
        if holds:
            out[name] = f
    return out


@dataclass(frozen=True)
class Span:
    """Revisions of a record that held a file: from one to another, or to now (None)."""

    first: int
    last: int | None
    fields: tuple[str, ...]


def held_spans(entity: Entity, file_id: uuid.UUID) -> list[Span]:
    """The revisions of `entity` that held the file, as spans, with the fields that held
    it: from the revision that set it to the one before the revision that took it out."""
    from iki_server.entities.services import values_at
    from iki_server.runs.input_files import file_ids

    fields = file_fields(entity.type)
    if not fields:
        return []
    first = (
        FileUse.objects.filter(file_id=file_id, entity=entity)
        .values_list("revision", flat=True)
        .first()
    ) or 1
    names = FieldNames.for_type(entity.type, entry=entity.registry_id is not None)
    changes = [
        r.number
        for r in Revision.objects.filter(entity=entity, number__gte=first).order_by("number")
        if r.number == first or fields.keys() & names.changed(r)
    ]
    spans: list[Span] = []
    open_from: int | None = None
    open_fields: tuple[str, ...] = ()
    for number in changes:
        values = values_at(entity, number, removed=True)
        holding = tuple(
            sorted(n for n, f in fields.items() if file_id in file_ids(f, values.get(n)))
        )
        if holding and open_from is None:
            open_from, open_fields = number, holding
        elif holding and holding != open_fields and open_from is not None:
            spans.append(Span(open_from, number - 1, open_fields))
            open_from, open_fields = number, holding
        elif not holding and open_from is not None:
            spans.append(Span(open_from, number - 1, open_fields))
            open_from, open_fields = None, ()
    if open_from is not None:
        spans.append(Span(open_from, None, open_fields))
    return spans


def samples_of(user: Any, file: File) -> list[tuple[Entity, Any]]:  # noqa: ANN401 - User
    """The samples the file belongs to that the user can read, each with the run it came
    through (None when the sample holds it itself), the sample's own first."""
    from iki_server.runs.models import ActionRun, RunInput

    samples = registry.sample_types()
    if not samples:
        return []
    readable = access.visible(user).filter(type__in=samples)
    own = list(holding_records(user, file).filter(type__in=samples).order_by("hrid"))
    out: list[tuple[Entity, Any]] = [(e, None) for e in own]
    seen = {e.pk for e in own}
    runs = using_runs(user, file).order_by("-pk")[:50]
    rows = (
        RunInput.objects.filter(run__in=runs, entity__in=readable.values("pk"))
        .select_related("entity", "run")
        .order_by("entity__hrid", "-run_id")
    )
    for row in rows:
        if row.entity_id not in seen:
            seen.add(row.entity_id)
            run: ActionRun = row.run
            out.append((row.entity, run))
    return out
