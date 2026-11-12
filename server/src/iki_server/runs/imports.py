"""Bulk imports: a run's creates from a file, too many for output.json (ADR 0056).

A command that makes more entities than ``output.json`` holds (over 10,000, or large
ones) writes them to a file in ``IKI_OUT_DIR``, one create per line (JSON Lines), and
names it ``creates_file``. Its worker uploads the file with the run's other results.
When the command finishes, the run's job stays ``committing`` while the task runner
imports the file in two phases:

1. **Checking.** A chunk of lines at a time: each line is checked as an item of
   ``creates`` would be (against the action, then against its entity type's fields and
   rules, as the launcher), then staged with COPY in a table of the import's own. Each
   chunk commits with how far it got, so an import that's interrupted (its runner
   stopped, the database restarted) goes on from there, and a big file is never read
   twice. Problems point at ``#/creates_file/<line>``; after 100 the import stops.
2. **Writing.** Once every line is checked, and none has problems, everything is
   written in one transaction, with INSERT … SELECT from the staged table: entities,
   their tables and history, revisions, project links, the run's effects, external
   origins and lineage. So an import is all or nothing, as ``creates`` is: nobody sees
   half of it, and a failed or cancelled one leaves nothing behind.

Duplicates are found as :func:`entities.create` finds them for runs: for types that
reuse duplicates, an entity the launcher can read with the same identity is reused (and
linked into the run's project), and so is an earlier line's. While it writes, an import
holds its types' identity locks, so a create elsewhere can't slip a duplicate in.

Two things differ from ``creates``, at this scale. Webhooks get one ``entity.imported``
event for the run, not ``entity.created`` for each entity. And lines can't attach files.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import time
import uuid
from collections import deque
from collections.abc import Generator
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import structlog
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.db.models import Q
from django.utils import timezone

from iki_server.audit import log as audit
from iki_server.entities import access, registry
from iki_server.entities import services as entities
from iki_server.entities.access import readable_projects
from iki_server.entities.hrids import sequence_name
from iki_server.entities.identity import hash_of
from iki_server.entities.values import Invalid, Part, Refs, is_list_table, shape
from iki_server.files import storage
from iki_server.files.models import File
from iki_server.runs import live, results
from iki_server.runs.models import (
    ActionRun,
    ImportPhase,
    Job,
    JobState,
    RunEffect,
    RunImport,
    RunOutput,
    RunStatus,
)
from iki_spec import schemas

if TYPE_CHECKING:
    from collections.abc import Mapping

    from iki_server.entities.values import Shape

__all__ = ["MAX_LINES", "recover", "start", "step"]

logger = structlog.get_logger("iki.runs.imports")

#: Lines checked and staged per transaction.
CHUNK_LINES = 10_000
#: Or fewer, when they're big.
CHUNK_BYTES = 32 * 1024 * 1024
#: The longest line: one create, such as a chromosome's sequence.
MAX_LINE = 256 * 1024 * 1024
#: The most lines one file may hold.
MAX_LINES = 100_000_000
#: How long one task works before it hands on to the next, so others get a turn.
STEP_FOR = 30.0
#: Steps that fail unexpectedly, in a row, before the import gives up.
MAX_FAILURES = 5
#: An import that hasn't moved for this long gets another step (its runner died).
STALLED_AFTER = timedelta(minutes=10)
#: Staged lines deleted per transaction, once they're done with.
CLEAR_BATCH = 50_000
#: How much of the progress bar checking takes; writing takes the rest.
CHECKING_SHARE = 0.9
READ_BLOCK = 1024 * 1024


# --- Starting ----------------------------------------------------------------------------------


def start(job: Job, run: ActionRun, file_id: uuid.UUID) -> RunImport:
    """The command finished, naming a creates file: hands the job to the task runner.
    Call in the transaction that completes the job, once the output is checked."""
    from iki_server.runs import tasks

    file = File.objects.select_related("blob").get(pk=file_id)
    imported = RunImport.objects.create(
        run=run, job=job, file=file, size=file.blob.size, schema=_schema()
    )
    job.state = JobState.COMMITTING
    job.lease_token = job.lease_expires_at = None
    job.progress, job.progress_message = 0.0, "Checking its creates."
    job.save(
        update_fields=["state", "lease_token", "lease_expires_at", "progress", "progress_message"]
    )
    live.changed(run.pk, run.batch_id)
    tasks.import_creates.enqueue(str(imported.pk))
    return imported


def _lines(imported: RunImport) -> str:
    """Its staged lines, this round, for FROM: our own UUID and number, so no quoting."""
    return (
        "(SELECT * FROM iki_import_line"  # noqa: S608 - ours; values are parameters
        f" WHERE bulk_import_id = '{uuid.UUID(str(imported.pk))}'::uuid"
        f" AND round = {int(imported.round)})"
    )


def clear(import_id: str) -> str:
    """Deletes an import's staged lines from rounds before its current one (all of them,
    once it's ended), a batch at a time. Enqueues itself again until they're gone."""
    from iki_server.runs import tasks

    deadline = time.monotonic() + STEP_FOR
    while time.monotonic() < deadline:
        with transaction.atomic():
            imported = (
                RunImport.objects.select_for_update(skip_locked=True).filter(pk=import_id).first()
            )
            if imported is None:
                return "busy"  # Its step has it; it clears again when it ends.
            keep = 0 if imported.phase == ImportPhase.ENDED else imported.round
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM iki_import_line WHERE (bulk_import_id, round, line) IN ("
                    " SELECT bulk_import_id, round, line FROM iki_import_line"
                    " WHERE bulk_import_id = %s AND round <> %s LIMIT %s)",
                    [imported.pk, keep, CLEAR_BATCH],
                )
                deleted = cursor.rowcount
            if deleted < CLEAR_BATCH:
                if imported.phase == ImportPhase.ENDED:
                    imported.cleared = True
                    imported.save(update_fields=["cleared"])
                return "cleared"
    with transaction.atomic():
        tasks.clear_import.enqueue(import_id)
    return "continues"


def _schema() -> str:
    """What the installed plugins are: lines checked against one may not fit another."""
    record = registry.installed() or {}
    plugins = sorted(
        (str(p.get("name")), str(p.get("version")), str(p.get("sha")))
        for p in record.get("plugins", ())
    )
    return hashlib.sha256(json.dumps(plugins).encode()).hexdigest()


# --- Stepping ----------------------------------------------------------------------------------


def step(import_id: str) -> str:
    """Works on the import for a while, then hands on to another task if it isn't done.
    Safe to run more than once at a time: only one works on an import at once."""
    from iki_server.runs import tasks
    from iki_server.tasks.backend import Retry

    try:
        done = _advance(uuid.UUID(import_id))
    except Exception as error:
        if _failed(uuid.UUID(import_id), error):
            return "gave up"
        raise Retry(f"The import failed for now: {error}") from error
    if done:
        return "ended"
    with transaction.atomic():
        tasks.import_creates.enqueue(import_id)
    return "continues"


def recover() -> int:
    """Gives imports whose runner went away another step, or ends them past a few tries.
    The task runner's housekeeping calls it. Returns how many it moved on."""
    from iki_server.runs import tasks

    moved = 0
    with transaction.atomic():
        stalled = RunImport.objects.select_for_update(skip_locked=True).filter(
            ~Q(phase=ImportPhase.ENDED), updated_at__lt=timezone.now() - STALLED_AFTER
        )[:100]
        for imported in stalled:
            imported.failures += 1
            imported.updated_at = timezone.now()
            imported.save(update_fields=["failures", "updated_at"])
            if imported.failures >= MAX_FAILURES:
                _give_up(imported, "Its runner stopped, again and again.")
            else:
                tasks.import_creates.enqueue(str(imported.pk))
            moved += 1
        # Lines whose clearing stopped (its runner died) are cleared again.
        left = RunImport.objects.filter(
            phase=ImportPhase.ENDED,
            cleared=False,
            finished_at__lt=timezone.now() - STALLED_AFTER,
        ).values_list("pk", flat=True)[:100]
        for found in left:
            tasks.clear_import.enqueue(str(found))
            moved += 1
    return moved


def _failed(import_id: uuid.UUID, error: Exception) -> bool:
    """Counts a step that failed unexpectedly. True if the import gave up."""
    logger.exception("import_step_failed", import_id=str(import_id))
    with transaction.atomic():
        imported = RunImport.objects.select_for_update().filter(pk=import_id).first()
        if imported is None or imported.phase == ImportPhase.ENDED:
            return True
        imported.failures += 1
        imported.updated_at = timezone.now()
        imported.save(update_fields=["failures", "updated_at"])
        if imported.failures < MAX_FAILURES:
            return False
        _give_up(imported, str(error)[:500])
        return True


def _give_up(imported: RunImport, why: str) -> None:
    job = Job.objects.select_for_update().get(pk=imported.job_id)
    run = ActionRun.objects.select_for_update().get(pk=imported.run_id)
    _end(
        imported,
        job,
        run,
        JobState.FAILED,
        RunStatus.FAILED,
        {"code": "import_failed", "message": f"Iki couldn't import its creates: {why}"},
    )


def _advance(import_id: uuid.UUID) -> bool:
    """True once the import has ended; False if there's more to do."""
    deadline = time.monotonic() + STEP_FOR
    reader: _Reader | None = None
    try:
        while True:
            with transaction.atomic():
                imported = (
                    # Its own row: files are append-only, and nothing here changes them.
                    RunImport.objects.select_for_update(skip_locked=True, of=("self",))
                    .select_related("file__blob")
                    .filter(pk=import_id)
                    .first()
                )
                if imported is None:
                    # Another task has it, and goes on with it; or it's gone.
                    return True
                if imported.phase == ImportPhase.ENDED:
                    return True
                run = ActionRun.objects.select_related("project", "requested_by").get(
                    pk=imported.run_id
                )
                if run.cancel_requested_at is not None or imported.phase == ImportPhase.WRITING:
                    # Writing (or cancelling) locks the job and its run, as cancelling does.
                    return _finish(imported)
                if imported.offset >= imported.size or len(imported.errors) >= MAX_ERRORS:
                    _checked(imported, run)
                else:
                    if reader is None or reader.offset != imported.offset:
                        if reader is not None:
                            reader.close()
                        reader = _Reader(imported.file, imported.offset)
                    _check_chunk(imported, run, reader)
            if time.monotonic() > deadline:
                return False
    finally:
        if reader is not None:
            reader.close()


MAX_ERRORS = results.MAX_ERRORS


def _checked(imported: RunImport, run: ActionRun) -> None:
    """Every line is read: on to writing, or the run fails with what's wrong."""
    if imported.errors:
        job = Job.objects.select_for_update().get(pk=imported.job_id)
        run = ActionRun.objects.select_for_update().get(pk=run.pk)
        more = " (and maybe more: it stopped looking)" if len(imported.errors) >= MAX_ERRORS else ""
        _end(
            imported,
            job,
            run,
            JobState.FAILED,
            RunStatus.FAILED,
            {
                "code": "invalid_output",
                "message": f"Some of its creates don't fit what the action declares{more}.",
                "errors": imported.errors,
            },
        )
        return
    imported.phase = ImportPhase.WRITING
    imported.updated_at = timezone.now()
    imported.save(update_fields=["phase", "updated_at"])
    _progress(imported, CHECKING_SHARE, f"Writing {imported.staged:,} entities.")


def _progress(imported: RunImport, fraction: float, message: str) -> None:
    Job.objects.filter(pk=imported.job_id).update(progress=fraction, progress_message=message[:500])
    run = ActionRun.objects.only("batch_id").get(pk=imported.run_id)
    live.changed(imported.run_id, run.batch_id)


# --- Checking ----------------------------------------------------------------------------------


class _Reader:
    """The file's lines from ``offset``, each with the bytes it took (its newline too)."""

    def __init__(self, file: File, offset: int) -> None:
        bucket, key = storage.split_uri(file.blob.storage_uri)
        params: dict[str, Any] = {"Bucket": bucket, "Key": key, "Range": f"bytes={offset}-"}
        if file.blob.object_version:
            params["VersionId"] = file.blob.object_version
        self.offset = offset
        #: Whole lines read but not yet given, then the start of the next one.
        self._ready: deque[bytes] = deque()
        self._pending: list[bytes] = []
        self._held = 0
        self._size = file.blob.size
        self._body = (
            None
            if offset >= self._size
            else storage.client_for(bucket).get_object(**params)["Body"]
        )

    def lines(self) -> Generator[bytes | None]:
        """Each line (without its newline); None for one that's over :data:`MAX_LINE`,
        which ends reading. :attr:`offset` is where the next line starts. Stopping early
        and asking again goes on from there."""
        while True:
            while self._ready:
                line = self._ready.popleft()
                self.offset += len(line) + 1
                yield line
            block = self._body.read(READ_BLOCK) if self._body is not None else b""
            if not block:
                if self._held:
                    line = b"".join(self._pending)
                    self._pending, self._held = [], 0
                    self.offset += len(line)
                    yield line
                return
            if b"\n" not in block:
                self._pending.append(block)
                self._held += len(block)
                if self._held > MAX_LINE:
                    yield None
                    return
                continue
            pieces = b"".join((*self._pending, block)).split(b"\n")
            rest = pieces.pop()
            self._pending, self._held = ([rest], len(rest)) if rest else ([], 0)
            self._ready.extend(pieces)

    def close(self) -> None:
        if self._body is not None:
            self._body.close()


@dataclass
class _Line:
    number: int
    item: dict[str, Any]


def _check_chunk(imported: RunImport, run: ActionRun, reader: _Reader) -> None:
    """Reads, checks and stages up to a chunk of lines, then saves how far it got."""
    document = RunOutput.objects.filter(job_id=imported.job_id).values_list("document", flat=True)[
        0
    ]
    check = results.checked(run, document).check
    errors: list[dict[str, str]] = list(imported.errors)
    number = imported.lines
    parsed: list[_Line] = []
    size = 0
    lines = reader.lines()
    for raw in lines:
        number += 1
        if raw is None:
            errors.append(
                {
                    "pointer": results.pointer("creates_file", number),
                    "detail": f"The line is over {MAX_LINE // (1024 * 1024)} MiB.",
                }
            )
            reader.offset = imported.size  # Nothing after it can be read as lines.
            break
        size += len(raw)
        if raw.strip():
            item = _parse(raw, number, errors)
            if item is not None:
                parsed.append(_Line(number, item))
        if number > MAX_LINES:
            errors.append(
                {
                    "pointer": results.pointer("creates_file", number),
                    "detail": f"A creates file holds at most {MAX_LINES:,} lines: split the run.",
                }
            )
            reader.offset = imported.size
            break
        if len(parsed) >= CHUNK_LINES or size >= CHUNK_BYTES or len(errors) >= MAX_ERRORS:
            break
    lines.close()
    rows = _stage_rows(check, run, parsed, errors)
    if rows and not errors:
        _copy(imported, rows)
    imported.offset = reader.offset
    imported.lines = number
    imported.staged += len(rows) if not errors else 0
    imported.errors = errors[:MAX_ERRORS]
    imported.failures = 0
    imported.updated_at = timezone.now()
    imported.save(update_fields=["offset", "lines", "staged", "errors", "failures", "updated_at"])
    fraction = CHECKING_SHARE * (imported.offset / imported.size if imported.size else 1)
    _progress(imported, fraction, f"Checked {imported.lines:,} lines of its creates.")


def _parse(raw: bytes, number: int, errors: list[dict[str, str]]) -> dict[str, Any] | None:
    at = results.pointer("creates_file", number)
    try:
        item: dict[str, Any] | None = json.loads(raw)
    except ValueError as error:
        errors.append({"pointer": at, "detail": f"The line isn't JSON: {error}."})
        return None
    if not _plainly_a_create(item):
        issues = schemas.check("run-output", item, part="create")
        if issues:
            errors.extend(
                {"pointer": results.pointer("creates_file", number, *i.path), "detail": i.message}
                for i in issues
            )
            return None
    return item


def _stage_rows(
    check: results.Check, run: ActionRun, parsed: list[_Line], errors: list[dict[str, str]]
) -> list[tuple[Any, ...]]:
    user = run.requested_by
    # Like a create in the run's project: references to what it may use (ADR 0067).
    refs = Refs(access.shareable_into(user, run.project), None, held=access.visible(user))
    _look_up_refs(refs, parsed)
    rows: list[tuple[Any, ...]] = []
    for line in parsed:
        if len(errors) >= MAX_ERRORS:
            break
        check.errors = []
        create = results.check_line(check, line.item, line.number)
        if create is None or check.errors:
            errors.extend(check.errors)
            continue
        try:
            form, given = entities.prepare(user, create.type, create.values, refs, by_run=True)
        except Invalid as error:
            check.errors = []
            results.invalid(check, create.pointer, error)
            errors.extend(check.errors)
            continue
        rows.append(_row(line.number, form, given, create))
    return rows


def _look_up_refs(refs: Refs, parsed: list[_Line]) -> None:
    """Finds every entity the chunk's references name in one query, not one per line."""
    wanted: set[uuid.UUID] = set()
    for line in parsed:
        type_name = line.item.get("type")
        if type_name not in registry.types():
            continue
        fields = shape(type_name).fields()
        for name, value in line.item.get("values", {}).items():
            f = fields.get(name)
            if f is None:
                continue
            if f.type == "ref":
                values = [value]
            elif f.type == "list" and f.items is not None and f.items.type == "ref":
                values = value if isinstance(value, list) else []
            else:
                continue
            for given in values:
                try:
                    wanted.add(uuid.UUID(str(given)))
                except ValueError:
                    continue
    if wanted:
        refs.types(wanted)


def _row(number: int, form: Shape, given: dict[str, Any], create: Any) -> tuple[Any, ...]:  # noqa: ANN401 - results' create
    parts: dict[str, dict[str, Any]] = {}
    for part in form.parts:
        if part.extension and not any(given.get(n) is not None for n in part.fields):
            continue
        parts[part.model._meta.db_table] = {
            name: _json(given[name])
            for name in part.fields
            if name in given and given[name] is not None
        }
    try:
        digest = hash_of(given, form.entity_type)
    except ValueError:
        digest = None
    origin = None
    if create.origin is not None:
        origin = {
            **create.origin,
            "retrieved_at": (
                create.origin["retrieved_at"].isoformat() if create.origin["retrieved_at"] else None
            ),
            "file_id": str(create.origin["file_id"]) if create.origin["file_id"] else None,
        }
    parents = [[str(p), name, revision] for p, (name, revision) in create.parents.items()]
    return (
        number,
        _uuid7(),
        create.type,
        digest,
        _ENCODE(parts),
        sorted(n for n, v in given.items() if v is not None),
        _ENCODE(origin) if origin is not None else None,
        _ENCODE(parents) if parents else None,
    )


def _uuid7() -> uuid.UUID:
    from iki_server.core.ids import uuid7

    return uuid7()


def _json(value: Any) -> Any:  # noqa: ANN401 - a database value
    if isinstance(value, uuid.UUID | Decimal):
        return str(value)
    if isinstance(value, dt.datetime | dt.date):
        return value.isoformat()
    if isinstance(value, list):
        return [_json(v) for v in value]
    return value


def _copy(imported: RunImport, rows: list[tuple[Any, ...]]) -> None:
    with connection.cursor() as cursor:
        statement = (
            "COPY iki_import_line (bulk_import_id, round, line, entity_id, type,"
            " identity_hash, parts, changed, origin, parents) FROM STDIN"
        )
        with cursor.copy(statement) as copy:
            for row in rows:
                copy.write_row((imported.pk, imported.round, *row))


_ENCODE = json.JSONEncoder(separators=(",", ":")).encode
_CREATE_KEYS = frozenset(("type", "values", "derived_from", "origin"))
_ORIGIN_TEXT = {"source": (1, 100), "id": (1, 255), "version": (0, 100), "url": (0, 2000)}
_ORIGIN_KEYS = frozenset((*_ORIGIN_TEXT, "retrieved_at", "sha256", "file"))
_HEX = frozenset("0123456789abcdef")


def _plainly_a_create(item: Any) -> bool:  # noqa: ANN401 - JSON
    """True only if ``item`` certainly fits ``run-output``'s ``create``: checking a line
    this way is many times faster than the schema, which still explains any that don't."""
    if type(item) is not dict or not item.keys() <= _CREATE_KEYS:
        return False
    type_name, values = item.get("type"), item.get("values")
    if type(type_name) is not str or type(values) is not dict:
        return False
    if type_name not in registry.types():
        return False  # The schema checks the name's shape; it isn't installed either way.
    parents = item.get("derived_from")
    if parents is not None and (
        type(parents) is not list
        or any(type(p) is not str for p in parents)
        or len(set(parents)) != len(parents)
    ):
        return False
    origin = item.get("origin")
    if origin is None:
        return "origin" not in item
    if type(origin) is not dict or not origin.keys() <= _ORIGIN_KEYS:
        return False
    for key, (shortest, longest) in _ORIGIN_TEXT.items():
        text = origin.get(key)
        if text is None:
            if key in ("source", "id"):
                return False
            continue
        if type(text) is not str or not shortest <= len(text) <= longest:
            return False
    if "url" in origin and not origin["url"].startswith("https://"):
        return False
    sha = origin.get("sha256")
    if sha is not None and (type(sha) is not str or len(sha) != 64 or not set(sha) <= _HEX):
        return False
    return all(type(origin.get(k, "")) is str for k in ("retrieved_at", "file"))


# --- Writing -----------------------------------------------------------------------------------


def _finish(imported: RunImport) -> bool:
    """Writes the import, or ends it cancelled. Holds the import; locks its job and run.
    Returns True: it has ended (or gone back to checking, after a plugin changed)."""
    from iki_server.runs import queue

    job = Job.objects.select_for_update().get(pk=imported.job_id)
    run = (
        ActionRun.objects.select_for_update()
        .select_related("project__team", "requested_by")
        .get(pk=imported.run_id)
    )
    if run.cancel_requested_at is not None:
        _end(imported, job, run, JobState.CANCELLED, RunStatus.CANCELLED, None)
        return True
    if imported.schema != _schema():
        # A plugin changed while it was checked: check it all again, against what's
        # installed now.
        from iki_server.runs import tasks

        imported.round += 1
        tasks.clear_import.enqueue(str(imported.pk))
        imported.phase = ImportPhase.CHECKING
        imported.offset = imported.lines = imported.staged = 0
        imported.errors = []
        imported.schema = _schema()
        imported.updated_at = timezone.now()
        imported.save()
        _progress(imported, 0.0, "A plugin changed: checking its creates again.")
        return False
    document = RunOutput.objects.filter(job=job).values_list("document", flat=True)[0]
    outcome = queue.outcome(run, lambda: _write(imported, run, document))
    state, status, error = outcome
    _end(imported, job, run, state, status, error)
    return True


def _write(imported: RunImport, run: ActionRun, document: Mapping[str, Any]) -> None:
    """Everything, in one transaction (a savepoint of the caller's): raises, writing
    nothing, if any of it can't be written."""
    from iki_server.webhooks import events
    from iki_server.webhooks.services import emit

    done = results.checked(run, document)
    try:
        with transaction.atomic():
            results.write(done)
            imported.updated = RunEffect.objects.filter(run=run).count()
            counts = _insert(imported, run)
    except (IntegrityError, DatabaseError) as error:
        if not isinstance(error, IntegrityError) and "violates" not in str(error):
            raise
        raise results.InvalidOutput(
            [{"pointer": results.pointer("creates_file"), "detail": _first_line(error)}]
        ) from error
    imported.created, imported.reused, imported.linked = counts
    data = {
        "run": {"id": str(run.pk), "hrid": run.hrid, "action": run.action},
        "project": events.project_data(run.project),
        "created": imported.created,
        "reused": imported.reused,
        "linked": imported.linked,
        "actor": events.actor_data(run.requested_by),
        "agent": audit.agent_data(run.agent),
    }
    emit("entity.imported", data, project=run.project)


def _first_line(error: Exception) -> str:
    return f"The database refused it: {str(error).splitlines()[0][:500]}"


def _insert(imported: RunImport, run: ActionRun) -> tuple[int, int, int]:
    """Writes the staged creates. Returns how many it created, reused and linked."""
    table = _lines(imported)
    user = run.requested_by
    params: dict[str, Any] = {"run": run.pk, "user": user.pk, "project": run.project_id}
    with connection.cursor() as cursor:
        # Foreign keys are checked after each statement, not all at commit: millions of
        # rows' checks would queue in memory until then.
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        # The HRID trigram index takes millions of new entries in larger batches.
        cursor.execute("SET LOCAL gin_pending_list_limit = '64MB'")
        cursor.execute(f"SELECT DISTINCT type FROM {table} s")  # noqa: S608 - our own values
        types = sorted(t for (t,) in cursor.fetchall())
        reuse = [
            t
            for t in types
            if (registry.duplicate_policy(registry.types()[t]) or "allow") == "reuse"
        ]
        locked = sorted(
            {
                o.qualified_name
                for t in reuse
                if (o := registry.identity_owner(registry.types()[t])) is not None
            }
        )
        for owner in locked:
            entities.identity_lock(registry.types()[owner], shared=False)
        params["reuse"] = reuse
        params["projects"] = list(readable_projects(user).values_list("pk", flat=True))
        # Each line's entity: a new one, an earlier line's, or one that exists already.
        cursor.execute("DROP TABLE IF EXISTS iki_import_resolved")
        cursor.execute(
            "CREATE TEMP TABLE iki_import_resolved ON COMMIT DROP AS "  # noqa: S608 - ours; values are parameters
            "WITH firsts AS ("
            f" SELECT DISTINCT ON (identity_hash) identity_hash, entity_id FROM {table}"
            " WHERE identity_hash IS NOT NULL AND type = ANY(%(reuse)s)"
            " ORDER BY identity_hash, line"
            "), existing AS ("
            " SELECT DISTINCT ON (e.identity_hash) e.identity_hash, e.id FROM iki_entity e"
            " WHERE e.identity_hash IN (SELECT identity_hash FROM firsts)"
            " AND e.archived_at IS NULL"
            " AND EXISTS (SELECT 1 FROM iki_entity_project l"
            "  WHERE l.entity_id = e.id AND l.project_id = ANY(%(projects)s))"
            # One the run's project may use: its project allows sharing, or it's already
            # here (ADR 0067). Otherwise the line makes its own, as if it weren't there.
            " AND (EXISTS (SELECT 1 FROM iki_project p WHERE p.id = e.owner_project_id"
            "  AND (p.allow_sharing OR p.id = %(project)s))"
            "  OR EXISTS (SELECT 1 FROM iki_entity_project h"
            "  WHERE h.entity_id = e.id AND h.project_id = %(project)s))"
            " ORDER BY e.identity_hash, e.created_at, e.id"
            "), lines AS ("
            " SELECT s.line, s.entity_id,"
            "  COALESCE(x.id, f.entity_id, s.entity_id) AS target,"
            "  x.id IS NOT NULL AS reused"
            f" FROM {table} s"
            " LEFT JOIN firsts f ON f.identity_hash = s.identity_hash AND s.type = ANY(%(reuse)s)"
            " LEFT JOIN existing x ON x.identity_hash = s.identity_hash"
            "  AND s.type = ANY(%(reuse)s)"
            ") SELECT line, entity_id, target, reused,"
            " target = entity_id AND NOT reused AS new,"
            " row_number() OVER (PARTITION BY target ORDER BY line) = 1 AS first"
            " FROM lines",
            params,
        )
        cursor.execute("CREATE UNIQUE INDEX ON iki_import_resolved (line)")
        # An entity the run already changed (one of its inputs, updated) keeps that effect.
        cursor.execute(
            "UPDATE iki_import_resolved r SET first = false WHERE first AND EXISTS ("
            " SELECT 1 FROM iki_run_effect e WHERE e.run_id = %(run)s AND e.entity_id = r.target)",
            params,
        )
        cursor.execute("ANALYZE iki_import_resolved")
        new = f"FROM {table} s JOIN iki_import_resolved r USING (line) WHERE r.new"
        for type_name in types:
            entity_type = registry.types()[type_name]
            prefix, digits = entity_type.hrid_prefix, entity_type.hrid_digits
            cursor.execute(
                "INSERT INTO iki_entity (id, type, hrid, owner_project_id, created_by_id,"  # noqa: S608 - ours; values are parameters
                " created_at, identity_hash, revision, updated_at)"
                " SELECT entity_id, type, %(prefix)s || '-' ||"
                "  CASE WHEN length(n) >= %(digits)s THEN n ELSE lpad(n, %(digits)s, '0') END,"
                "  %(project)s, %(user)s, now(), identity_hash, 1, now()"
                " FROM (SELECT o.*, nextval(%(sequence)s::regclass)::text AS n FROM ("
                f"  SELECT s.entity_id, s.type, s.identity_hash {new} AND s.type = %(type)s"
                "  ORDER BY s.line) o) q",
                {
                    **params,
                    "prefix": prefix,
                    "digits": digits,
                    "sequence": sequence_name(prefix or ""),
                    "type": type_name,
                },
            )
            for part in shape(type_name).parts:
                _insert_part(cursor, table, part, type_name)
        cursor.execute(
            "INSERT INTO iki_revision (id, entity_id, number, kind, changed_fields, user_id,"
            " via, client, reason, created_at)"
            " SELECT uuidv7(), s.entity_id, 1, 'created', s.changed, %(user)s, 'run', '', '',"
            f" now() {new} ORDER BY s.line",
            params,
        )
        cursor.execute(
            "INSERT INTO iki_entity_project (id, entity_id, project_id, linked_by_id, linked_at)"
            f" SELECT uuidv7(), s.entity_id, %(project)s, %(user)s, now() {new}",
            params,
        )
        created = cursor.rowcount
        cursor.execute(
            "INSERT INTO iki_entity_project (id, entity_id, project_id, linked_by_id, linked_at)"
            " SELECT uuidv7(), target, %(project)s, %(user)s, now()"
            " FROM iki_import_resolved WHERE reused AND first"
            " ON CONFLICT (entity_id, project_id) DO NOTHING",
            params,
        )
        linked = cursor.rowcount
        cursor.execute(
            "INSERT INTO iki_run_effect (run_id, entity_id, kind, revision)"
            " SELECT %(run)s, target, CASE WHEN reused THEN 'reused' ELSE 'created' END,"
            "  CASE WHEN reused THEN NULL ELSE 1 END"
            " FROM iki_import_resolved WHERE first ORDER BY line",
            params,
        )
        reused = cursor.rowcount - created
        cursor.execute(
            "INSERT INTO iki_external_origin (run_id, entity_id, revision, source, identifier,"  # noqa: S608 - ours; values are parameters
            " version, url, retrieved_at, sha256, file_id, created_at)"
            " SELECT %(run)s, r.target, CASE WHEN r.reused THEN NULL ELSE 1 END,"
            "  s.origin->>'source', s.origin->>'identifier', s.origin->>'version',"
            "  s.origin->>'url', (s.origin->>'retrieved_at')::timestamptz,"
            "  s.origin->>'sha256', (s.origin->>'file_id')::uuid, now()"
            f" FROM {table} s JOIN iki_import_resolved r USING (line)"
            " WHERE r.first AND s.origin IS NOT NULL ORDER BY s.line",
            params,
        )
        cursor.execute(
            "INSERT INTO iki_lineage (entity_id, parent_id, parent_revision, run_id, input)"  # noqa: S608 - ours; values are parameters
            " SELECT r.target, (p->>0)::uuid, (p->>2)::integer, %(run)s, p->>1"
            f" FROM {table} s JOIN iki_import_resolved r USING (line)"
            " CROSS JOIN LATERAL jsonb_array_elements(s.parents) p"
            " WHERE r.first AND s.parents IS NOT NULL",
            params,
        )
    return created, reused, linked


def _insert_part(cursor: Any, table: str, part: Part, type_name: str) -> None:  # noqa: ANN401 - a cursor
    """One table's rows and their history, and its list fields' items, for the new
    entities of one type."""
    new = f"FROM {table} s JOIN iki_import_resolved r USING (line) WHERE r.new"
    key = part.model._meta.db_table
    meta = part.model._meta
    columns = [(f.name, _column(f)) for f in meta.local_concrete_fields if not f.primary_key]
    renames = "".join(
        f" || jsonb_build_object({_literal(column)}, s.parts->{_literal(key)}->{_literal(name)})"
        for name, column in columns
        if column != name
    )
    selected = ", ".join(f"(p).{_q(column)}" for _, column in columns)
    names = ", ".join(_q(column) for _, column in columns)
    where = f"{new} AND s.type = %s AND s.parts ? {_literal(key)}"
    cursor.execute(
        f"INSERT INTO {_q(key)} ({_q(_column(meta.pk))}{', ' if names else ''}{names})"  # noqa: S608
        f" SELECT entity_id{', ' if selected else ''}{selected} FROM ("
        f" SELECT s.entity_id, jsonb_populate_record(NULL::{_q(key)},"
        f"  s.parts->{_literal(key)}{renames}) AS p {where}) q",
        [type_name],
    )
    history = part.history._meta
    kept = [
        c for c in map(_column, history.local_concrete_fields) if c not in ("entity_id", "revision")
    ]
    selected = ", ".join(f"(p).{_q(c)}" for c in kept)
    names = ", ".join(_q(c) for c in kept)
    cursor.execute(
        f"INSERT INTO {_q(history.db_table)} (entity_id, revision{', ' if names else ''}{names})"  # noqa: S608
        f" SELECT entity_id, 1{', ' if selected else ''}{selected} FROM ("
        f" SELECT s.entity_id, jsonb_populate_record(NULL::{_q(history.db_table)},"
        f"  s.parts->{_literal(key)}) AS p {where}) q",
        [type_name],
    )
    for name, f in part.fields.items():
        if not is_list_table(f) or f.removed:
            continue
        items = part.list_model(name)._meta
        item = _column(items.get_field("item"))
        cursor.execute(
            f"INSERT INTO {_q(items.db_table)} (entity_id, position, {_q(item)})"  # noqa: S608
            " SELECT s.entity_id, e.position - 1, e.item::uuid"
            f" FROM {table} s JOIN iki_import_resolved r USING (line)"
            f" CROSS JOIN LATERAL jsonb_array_elements_text(s.parts->{_literal(key)}->"
            f"{_literal(name)}) WITH ORDINALITY AS e(item, position)"
            " WHERE r.new AND s.type = %s",
            [type_name],
        )


def _column(field: object) -> str:
    """A concrete field's column name."""
    column = getattr(field, "column", None)
    assert isinstance(column, str), field  # noqa: S101 - concrete fields always have one
    return column


def _q(name: str) -> str:
    return connection.ops.quote_name(name)


def _literal(text: str) -> str:
    """A name we made (a table's, a field's) as an SQL string literal."""
    if not text.replace("_", "").isalnum():
        raise ValueError(f"Not a name: {text!r}")
    return f"'{text}'"


# --- Ending ------------------------------------------------------------------------------------


def _end(
    imported: RunImport,
    job: Job,
    run: ActionRun,
    state: str,
    status: str,
    error: dict[str, Any] | None,
) -> None:
    from iki_server.runs import queue, tasks

    imported.phase = ImportPhase.ENDED
    imported.finished_at = imported.updated_at = timezone.now()
    imported.save()
    tasks.clear_import.enqueue(str(imported.pk))
    if state == JobState.CANCELLED:
        queue.end_cancelled(job)
    else:
        job.progress = 1.0 if state == JobState.SUCCEEDED else job.progress
        job.save(update_fields=["progress"])
        queue.finish(job, run, state, status, error)
