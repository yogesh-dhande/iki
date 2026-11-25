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

Identities are registered as :func:`entities.create` registers them (ADR 0115): a line
whose identity is new in the project's registry registers it (an earlier line's, if
two have it), and one already registered gets a record of the entry, or is the
project's record it has already, reused. While it writes, an import holds its types'
identity locks in the registry, so a create elsewhere can't register one at the same
time.

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
from dataclasses import asdict, dataclass
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import structlog
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.db.models import Q
from django.utils import timezone

from iki_server.audit import log as audit
from iki_server.entities import (
    access,
    checking,
    constraints,
    holds,
    identity,
    registry,
    requests,
    unique,
)
from iki_server.entities import services as entities
from iki_server.entities.hrids import sequence_name
from iki_server.entities.identity import hash_of, identity_of
from iki_server.entities.models import Entity
from iki_server.entities.people import Namer
from iki_server.entities.values import (
    Invalid,
    Part,
    Refs,
    is_list_table,
    problems_of,
    row_columns,
    shape,
    to_db,
)
from iki_server.files import storage
from iki_server.files.models import File
from iki_server.org.models import registry_of
from iki_server.runs import live, makings, results
from iki_server.runs.models import (
    ActionRun,
    EffectKind,
    ExternalOrigin,
    ImportPhase,
    Job,
    JobState,
    Lineage,
    RunEffect,
    RunImport,
    RunOutput,
    RunStatus,
)
from iki_server.runs.services import is_a
from iki_server.setup import starter_flows
from iki_spec import schemas
from iki_spec.model import Action as SpecAction
from iki_spec.model import Field, Software

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from iki_server.entities.values import Shape
    from iki_server.org.models import Project

__all__ = [
    "MAX_LINES",
    "breaking",
    "launch",
    "look_up_refs",
    "recover",
    "refs_for",
    "start",
    "step",
]

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


def launch(
    user: Any,  # noqa: ANN401 - a User
    project: Any,  # noqa: ANN401 - a Project
    action: SpecAction,
    params: dict[str, Any],
    creates: File,
    *,
    reason: str = "",
    via: str | None = None,
    client: str | None = None,
    agent: Any = None,  # noqa: ANN401 - an Agent
) -> ActionRun:
    """A run of one of Iki's core actions, on its own tier (``server``), whose results are
    ``creates``' lines (ADR 0178): such as a spreadsheet's rows, as Iki read them. No worker
    runs it: its one attempt has finished its command, and the task runner checks and
    writes its creates as it does a worker's (ADR 0056), with everything a run's give its
    entities. Call in a transaction, with what it may do checked."""
    from iki_server import __version__
    from iki_server.runs import services

    now = timezone.now()
    run = ActionRun.objects.create(
        hrid=services._next_hrid(),
        action=action.qualified_name,
        plugin_version=__version__,
        spec=asdict(action),
        project=project,
        requested_by=user,
        via=via or audit.current_via(),
        client=audit.current_oauth_client() if client is None else client,
        agent=agent if agent is not None else audit.current_agent(),
        reason=reason,
        tier=action.tier,
        environment=action.environment,
        params=params,
        status=RunStatus.RUNNING,
        started_at=now,
        timeout=action.timeout,
        max_attempts=1,
    )
    job = Job.objects.create(
        run=run,
        project=project,
        tier=action.tier,
        environment=f"{action.plugin}.{action.environment}",
        state=JobState.COMMITTING,
        started_at=now,
    )
    RunOutput.objects.create(
        run=run, job=job, document={"apiVersion": "iki/v1", "kind": "RunOutput"}
    )
    audit.record(
        "run.launched",
        actor=user,
        agent=run.agent,
        target=run,
        project=project,
        data={"action": action.qualified_name, "tier": action.tier, "inputs": 0},
    )
    start(job, run, creates.pk)
    return run


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
    reader: Reader | None = None
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
                        reader = Reader.of(imported.file, imported.offset)
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


class Reader:
    """A stored file's lines from ``offset``, each with the bytes it took (its newline too):
    a creates file's, or a spreadsheet's rows to preview (ADR 0178)."""

    def __init__(self, storage_uri: str, version: str, size: int, offset: int) -> None:
        self.offset = offset
        #: Whole lines read but not yet given, then the start of the next one.
        self._ready: deque[bytes] = deque()
        self._pending: list[bytes] = []
        self._held = 0
        self._size = size
        self._body = (
            None if offset >= self._size else storage.open_object(storage_uri, version, offset)
        )

    @classmethod
    def of(cls, file: File, offset: int) -> Reader:
        return cls(file.blob.storage_uri, file.blob.object_version, file.blob.size, offset)

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


def _check_chunk(imported: RunImport, run: ActionRun, reader: Reader) -> None:
    """Reads, checks and stages up to a chunk of lines, then saves how far it got."""
    document = RunOutput.objects.filter(job_id=imported.job_id).values_list("document", flat=True)[
        0
    ]
    done = results.checked(run, document)
    check = done.check
    keyed = {c.key: c.type for c in done.creates if c.key is not None}
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
    rows = _stage_rows(check, run, parsed, errors, keyed, imported)
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
    check: results.Check,
    run: ActionRun,
    parsed: list[_Line],
    errors: list[dict[str, str]],
    keyed: dict[str, str],
    imported: RunImport,
) -> list[tuple[Any, ...]]:
    user = run.requested_by
    refs = refs_for(user, run.project)
    look_up_refs(refs, [line.item for line in parsed])
    # A line's `$create` names one of the output's creates (ADR 0140), written first: it's
    # staged as a stand-in, which writing replaces with what that create made.
    for key, type_name in keyed.items():
        refs.expect(stand_in(imported, key), type_name)
    rows: list[tuple[Any, ...]] = []
    #: Lines checked against their types' fields, for the plugins' rules all at once.
    prepared: list[tuple[_Line, Any, Shape, dict[str, Any]]] = []
    for line in parsed:
        if len(errors) >= MAX_ERRORS:
            break
        check.errors = []
        create = results.check_line(check, line.item, line.number, frozenset(keyed))
        if create is None or check.errors:
            errors.extend(check.errors)
            continue
        values = _standing_in(check, imported, create)
        if values is None:
            errors.extend(check.errors)
            continue
        try:
            form, given, _ = entities.prepare(
                user,
                create.type,
                run.project,
                values,
                refs,
                by_run=True,
                check_rules=False,
                defer_result_check=requests.result_spec(create.type) is not None,
            )
        except Invalid as error:
            check.errors = []
            results.invalid(check, create.pointer, error)
            errors.extend(check.errors)
            continue
        prepared.append((line, create, form, given))
    refused = {
        index: [b for b in broken if not b.held]
        for index, broken in breaking([(c.type, g) for _, c, _, g in prepared]).items()
        if any(not b.held for b in broken)
    }
    for index, (line, create, form, given) in enumerate(prepared):
        if index in refused:
            if len(errors) >= MAX_ERRORS:
                break
            check.errors = []
            try:
                checking.raise_broken(refused[index], hold_reason="")
            except Invalid as error:
                results.invalid(check, create.pointer, error)
            errors.extend(check.errors)
            continue
        rows.append(_row(line.number, form, given, create))
    return rows


def stand_in(imported: RunImport, key: str) -> uuid.UUID:
    """What a line's ``{"$create": key}`` is staged as: the same in every chunk."""
    return uuid.uuid5(uuid.UUID(str(imported.pk)), key)


def _standing_in(
    check: results.Check,
    imported: RunImport,
    create: Any,  # noqa: ANN401 - results' create
) -> dict[str, Any] | None:
    """The line's values with each ``$create`` staged as its stand-in; None (noting why)
    if one is in its type's identity, whose hash is staged with the line."""
    values = dict(create.values)
    identity = set(identity_of(shape(create.type).entity_type))
    for name, key, where in results.created_refs({"type": create.type, "values": values}):
        if name in identity:
            check.error(
                "/".join((create.pointer, "values", name, *map(str, where))),
                "A line's identity can't refer to a create: give this one in `creates` too.",
            )
            return None
        if where:
            values[name] = list(values[name])
            values[name][where[0]] = str(stand_in(imported, key))
        else:
            values[name] = str(stand_in(imported, key))
    return values


def refs_for(user: Any, project: Any) -> Refs:  # noqa: ANN401 - a User and a Project
    """What a create in ``project`` may refer to, as ``user``: what they may use there
    (ADR 0067). A bulk import's lines are checked with it, and so is a spreadsheet's
    preview of them (ADR 0178)."""
    return Refs(
        access.shareable_into(user, project),
        None,
        held=access.held_entity(user, project),
        entries=access.held_entries(project),
        readable_entries=access.visible_entries(user),
        project=project.name,
        namer=Namer(project),
    )


def breaking(lines: list[tuple[str, dict[str, Any]]]) -> dict[int, list[checking.Broken]]:
    """The plugins' rules each of a chunk's prepared lines breaks, by its index: each type's
    lines at once, as one evaluation (ADR 0127). Those breaking only rules that hold are
    written, and held once they are (:func:`holds.hold_imported`)."""
    by_type: dict[str, list[int]] = {}
    for index, (type_name, _) in enumerate(lines):
        by_type.setdefault(type_name, []).append(index)
    out: dict[int, list[checking.Broken]] = {}
    for type_name, indexes in by_type.items():
        found = checking.broken(type_name, [checking.Saving(lines[i][1].get) for i in indexes])
        out.update((i, b) for i, b in zip(indexes, found, strict=True) if b)
    return out


def look_up_refs(refs: Refs, items: list[dict[str, Any]]) -> None:
    """Finds every entity the chunk's references name in one query, not one per line."""
    wanted: set[uuid.UUID] = set()
    for item in items:
        type_name = item.get("type")
        if type_name not in registry.types():
            continue
        fields = shape(type_name).fields()
        for name, value in item.get("values", {}).items():
            f = fields.get(name)
            if f is None:
                continue
            if f.type == "ref":
                values = [value]
            elif f.type == "list" and f.items is not None and f.items.type == "ref":
                values = value if isinstance(value, list) else []
            elif f.is_rows and f.items is not None and isinstance(value, list):
                cells = [n for n, c in f.items.fields.items() if c.type == "ref"]
                values = [r.get(n) for r in value if isinstance(r, dict) for n in cells]
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
    made_with = [[tool.name, tool.version] for tool in create.made_with]
    return (
        number,
        _uuid7(),
        create.type,
        digest,
        _ENCODE(parts),
        sorted(n for n, v in given.items() if v is not None),
        _ENCODE(origin) if origin is not None else None,
        _ENCODE(parents) if parents else None,
        _ENCODE(made_with) if made_with else None,
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
    if isinstance(value, dict):
        # A list's row (ADR 0137).
        return {k: _json(v) for k, v in value.items()}
    return value


def _copy(imported: RunImport, rows: list[tuple[Any, ...]]) -> None:
    with connection.cursor() as cursor:
        statement = (
            "COPY iki_import_line (bulk_import_id, round, line, entity_id, type,"
            " identity_hash, parts, changed, origin, parents, made_with) FROM STDIN"
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
        ActionRun.objects.select_for_update(of=("self",))
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
            made = results.write(done)
            _replace_stand_ins(imported, made)
            unchanged = _update_matches(imported, done.check)
            imported.updated = RunEffect.objects.filter(run=run).count() - unchanged
            created, reused, registered = _insert(imported, run)
            # Lines were checked as they were staged; those that break rules that hold
            # are held now they're records (ADR 0127).
            holds.hold_imported(run)
            counts = (created, reused + unchanged, registered)
    except (IntegrityError, DatabaseError) as error:
        if not isinstance(error, IntegrityError) and "violates" not in str(error):
            raise
        rule, is_rule = (
            constraints.clash(error) if isinstance(error, IntegrityError) else (None, False)
        )
        if is_rule:
            # A plugin's `unique` or `no_overlap` rule (ADR 0127), between two lines or a
            # line and a record saved meanwhile.
            label = rule.rule.label if rule is not None else "a plugin's rule"
            detail = (
                f"Its records break “{label}”: two of its lines, or a line and a record the "
                "project has."
            )
        elif isinstance(error, IntegrityError) and unique.is_clash(error):
            detail = unique.clash_message(error)
        else:
            detail = _first_line(error)
        raise results.InvalidOutput(
            [{"pointer": results.pointer("creates_file"), "detail": detail}]
        ) from error
    imported.created, imported.reused, imported.registered = counts
    data = {
        "run": {"id": str(run.pk), "hrid": run.hrid, "action": run.action},
        "project": events.project_data(run.project),
        "created": imported.created,
        "updated": imported.updated,
        "reused": imported.reused,
        "registered": imported.registered,
        "actor": events.actor_data(run.requested_by),
        "agent": audit.agent_data(run.agent),
    }
    emit("entity.imported", data, project=run.project)


def _replace_stand_ins(imported: RunImport, made: dict[str, uuid.UUID]) -> None:
    """Puts what each keyed create made (its registry entry, for a type with an identity)
    where the staged lines refer to it, a few statements for the whole file."""
    if not made:
        return
    targets = dict(Entity.objects.filter(pk__in=made.values()).values_list("pk", "entry_id"))
    pairs = [(str(stand_in(imported, key)), str(targets.get(pk) or pk)) for key, pk in made.items()]
    table = "iki_import_line"
    with connection.cursor() as cursor:
        # A line may refer to several creates: each pass replaces one of them in each line.
        while True:
            cursor.execute(
                f"UPDATE {table} l SET parts = replace(l.parts::text, m.stand_in, m.target)::jsonb"  # noqa: S608 - ours; values are parameters
                " FROM unnest(%(stand_ins)s::text[], %(targets)s::text[]) AS m(stand_in, target)"
                " WHERE l.bulk_import_id = %(import)s AND l.round = %(round)s"
                " AND strpos(l.parts::text, m.stand_in) > 0",
                {
                    "stand_ins": [a for a, _ in pairs],
                    "targets": [b for _, b in pairs],
                    "import": imported.pk,
                    "round": imported.round,
                },
            )
            if cursor.rowcount == 0:
                break


def _update_matches(imported: RunImport, check: results.Check) -> int:
    """Writes each staged line whose ``match_on`` value an entity of the run's project has
    to that entity, as a revision (ADR 0082), and unstages it so the rest are inserted.
    Raises :class:`results.InvalidOutput` for lines that can't be. Returns how many changed
    nothing."""
    table = _lines(imported)
    unchanged = 0
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT DISTINCT type FROM {table} s")  # noqa: S608 - our own values
        staged = sorted(t for (t,) in cursor.fetchall())
    for effect in check.action.creates:
        if effect.match_on is None:
            continue
        for type_name in (t for t in staged if is_a(t, effect.type)):
            unchanged += _update_matches_of(imported, check, type_name, effect.match_on)
    if check.errors:
        raise results.InvalidOutput(check.errors)
    return unchanged


def _update_matches_of(
    imported: RunImport, check: results.Check, type_name: str, field: str
) -> int:
    run = check.run
    table = _lines(imported)
    part = shape(type_name).part_of(field)
    params: dict[str, Any] = {
        "type": type_name,
        "part": part.model._meta.db_table,
        "field": field,
        "project": run.project_id,
        "registry": registry_of(run.project),
        "key": unique.key(part.model, field),
    }
    value = "s.parts->%(part)s->>%(field)s"
    # The record a line's value matches: the project's own, or, for a registry fact, the
    # project's record of the entry that has it (ADR 0115).
    if part.entry:
        match = (
            "iki_unique_value u JOIN iki_entity m ON m.entry_id = u.entity_id"
            " AND m.owner_project_id = %(project)s WHERE u.registry_id = %(registry)s"
            f" AND u.key = %(key)s AND u.value = {value}",
            "m.id",
            True,
        )
    else:
        match = (
            "iki_unique_value u WHERE u.project_id = %(project)s AND u.key = %(key)s"
            f" AND u.value = {value}",
            "u.entity_id",
            False,
        )
    with connection.cursor() as cursor:
        # A record twice in one file: which would win is anyone's guess.
        cursor.execute(
            f"SELECT array_agg(s.line ORDER BY s.line) FROM {table} s"  # noqa: S608 - ours; values are parameters
            f" WHERE s.type = %(type)s AND {value} IS NOT NULL"
            f" GROUP BY {value} HAVING count(*) > 1 ORDER BY min(s.line) LIMIT 100",
            params,
        )
        for (lines,) in cursor.fetchall():
            for line in lines[1:]:
                check.error(
                    results.pointer("creates_file", line, "values", field),
                    f"Line {lines[0]} has the same {field}: each record comes once in a file.",
                )
        if check.errors:
            return 0
        cursor.execute(
            f"SELECT s.line, s.parts, s.origin, s.parents, s.made_with, mm.id FROM {table} s"  # noqa: S608 - ours; values are parameters
            f" JOIN LATERAL (SELECT {match[1]} AS id FROM {match[0]}) mm ON true"
            " WHERE s.type = %(type)s ORDER BY s.line",
            params,
        )
        matched = cursor.fetchall()
        matched += _claims(cursor, check, table, type_name, field, params, match)
        matched.sort(key=lambda row: row[0])
        if check.errors:
            return 0
    unchanged = 0
    done: list[int] = []
    with entities.quietly():
        for line, parts_text, origin_text, parents_text, made_text, entity_id in matched:
            # Django reads jsonb as text.
            parts, origin, parents, made_with = (
                json.loads(t) if isinstance(t, str) else t
                for t in (parts_text, origin_text, parents_text, made_text)
            )
            entity = Entity.objects.select_for_update().get(pk=entity_id)
            values = {n: v for fields in parts.values() for n, v in fields.items()}
            at = results.pointer("creates_file", line)
            saved = results.update_match(check, at, type_name, entity, values)
            if saved is None:
                continue
            revision = saved.revision.number if saved.revision is not None else None
            unchanged += revision is None
            RunEffect.objects.create(
                run=run,
                entity_id=entity.pk,
                kind=EffectKind.UPDATED if revision is not None else EffectKind.REUSED,
                revision=revision,
                **results.entry_made(saved),
            )
            kept = None
            if origin is not None:
                # As staged: ExternalOrigin's fields.
                kept = ExternalOrigin.objects.create(
                    run=run, entity_id=entity.pk, revision=revision, **origin
                )
            tools = [Software(name, version) for name, version in made_with or ()]
            makings.record(run.pk, entity.pk, tools, kept)
            Lineage.objects.bulk_create(
                Lineage(
                    entity_id=entity.pk,
                    parent_id=parent,
                    parent_revision=parent_revision,
                    run=run,
                    input=name,
                )
                for parent, name, parent_revision in parents or []
            )
            done.append(line)
    if done:
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM iki_import_line WHERE bulk_import_id = %s AND round = %s"
                " AND line = ANY(%s)",
                [imported.pk, imported.round, done],
            )
    return unchanged


def _claims(
    cursor: Any,  # noqa: ANN401 - a cursor
    check: results.Check,
    table: str,
    type_name: str,
    field: str,
    params: dict[str, Any],
    match: tuple[str, str, bool],
) -> list[tuple[Any, ...]]:
    """The staged lines with no match whose type has an identity, each with the run's
    project's own record of its registry entry, which it claims as the matched rows are
    (ADR 0082, 0115). Notes those whose record has another value already, or that
    another line in the file has the same identity."""
    entity_type = registry.types()[type_name]
    owner = registry.identity_owner(entity_type)
    if owner is None:
        return []
    matched_from, _, on_entry = match
    same = " and ".join(owner.identity)
    value = "s.parts->%(part)s->>%(field)s"
    unmatched = (
        f" s.type = %(type)s AND {value} IS NOT NULL AND s.identity_hash IS NOT NULL"  # noqa: S608 - ours; values are parameters
        f" AND NOT EXISTS (SELECT 1 FROM {matched_from})"
    )
    # Two records of one entity in a file: it can only be one of them.
    cursor.execute(
        f"SELECT array_agg(s.line ORDER BY s.line) FROM {table} s WHERE {unmatched}"  # noqa: S608 - ours; values are parameters
        f" GROUP BY s.identity_hash HAVING count(DISTINCT {value}) > 1"
        " ORDER BY min(s.line) LIMIT 100",
        params,
    )
    for (lines,) in cursor.fetchall():
        for line in lines[1:]:
            check.error(
                results.pointer("creates_file", line, "values", field),
                f"It has the same {same} as line {lines[0]}: they'd be one entity, which "
                "can't be two records.",
            )
    holder = "x.id" if on_entry else "e.id"
    cursor.execute(
        f"SELECT s.line, s.parts, s.origin, s.parents, s.made_with,"  # noqa: S608 - ours; values are parameters
        f" e.id, e.hrid, u.value FROM {table} s"
        " JOIN iki_entity x ON x.registry_id = %(registry)s"
        "  AND x.identity_hash = s.identity_hash"
        " JOIN iki_entity e ON e.entry_id = x.id AND e.owner_project_id = %(project)s"
        "  AND e.archived_at IS NULL"
        f" LEFT JOIN iki_unique_value u ON u.entity_id = {holder} AND u.key = %(key)s"
        f" WHERE {unmatched} ORDER BY s.line",
        params,
    )
    claims = []
    for line, parts, origin, parents, made_with, entity_id, hrid, other in cursor.fetchall():
        if other is None:
            claims.append((line, parts, origin, parents, made_with, entity_id))
            continue
        check.error(
            results.pointer("creates_file", line, "values", field),
            f"It has the same {same} as {hrid}, whose {field} is {other} already: they'd be "
            "one entity, which can't be two records.",
        )
    return claims


def _first_line(error: Exception) -> str:
    return f"The database refused it: {str(error).splitlines()[0][:500]}"


def _check_request_results(imported: RunImport, project: Project, types: Sequence[str]) -> None:
    """Check actual IDs after keyed creates resolve, holding requests until the import commits."""
    for type_name in types:
        spec = requests.result_spec(type_name)
        if not spec:
            continue
        form = shape(type_name)
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT parts #>> %s AS request, parts #>> %s AS material, min(line) "  # noqa: S608 - _lines is our table
                f"FROM {_lines(imported)} s WHERE type = %s GROUP BY 1, 2 ORDER BY 1, 2",
                [
                    [form.part_of(name).model._meta.db_table, name] if name else None
                    for name in (spec.of, spec.material)
                ]
                + [type_name],
            )
            for request, material, line in cursor:
                given: dict[str, Any] = {spec.of: uuid.UUID(request)}
                if spec.material:
                    given[spec.material] = uuid.UUID(material) if material else None
                for name, errors in requests.check_result(
                    project, type_name, given, lock=True
                ).items():
                    raise results.InvalidOutput(
                        [
                            {
                                "pointer": results.pointer("creates_file", line, "values", name),
                                "detail": msg,
                            }
                            for msg in errors
                        ]
                    )


def _insert(imported: RunImport, run: ActionRun) -> tuple[int, int, int]:
    """Writes the staged creates. Returns how many records it created and reused, and how
    many registry entries it registered (ADR 0115)."""
    table = _lines(imported)
    user = run.requested_by
    project = run.project
    params: dict[str, Any] = {
        "run": run.pk,
        "user": user.pk,
        "reason": run.change_reason,
        "project": project.pk,
        "registry": registry_of(project),
    }
    with connection.cursor() as cursor:
        # Foreign keys are checked after each statement, not all at commit: millions of
        # rows' checks would queue in memory until then.
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        # The HRID trigram index takes millions of new entries in larger batches.
        cursor.execute("SET LOCAL gin_pending_list_limit = '64MB'")
        cursor.execute(f"SELECT DISTINCT type FROM {table} s")  # noqa: S608 - our own values
        types = sorted(t for (t,) in cursor.fetchall())
        if refused := starter_flows.refused_create(project, types):
            raise results.InvalidOutput(
                [{"pointer": results.pointer("creates_file"), "detail": refused}]
            )
        _check_request_results(imported, project, types)
        identified = [t for t in types if shape(t).has_entries]
        owners = sorted(
            {
                o.qualified_name
                for t in identified
                if (o := registry.identity_owner(registry.types()[t])) is not None
            }
        )
        for owner in owners:
            entities.identity_lock(registry.types()[owner], registry_of(project), shared=False)
        params["identified"] = identified
        # PostgreSQL can underestimate a freshly staged import and rescan an
        # unindexed CTE for every line. Resolve each identity once and give
        # the next join an indexed lookup with current statistics.
        cursor.execute(
            "CREATE TEMP TABLE iki_import_entries ON COMMIT DROP AS "  # noqa: S608 - ours; values are parameters
            "SELECT f.identity_hash, f.line AS first_line, f.type AS first_type,"
            " f.entity_id AS first_entity_id, COALESCE(x.id, uuidv7()) AS entry_id,"
            " x.id IS NULL AS new_entry, x.type AS entry_type, x.hrid AS entry_hrid,"
            " x.superseded_by_id, h.id AS held_id"
            " FROM (SELECT DISTINCT ON (identity_hash) identity_hash, line, entity_id, type"
            f" FROM {table} WHERE identity_hash IS NOT NULL AND type = ANY(%(identified)s)"
            " ORDER BY identity_hash, line) f"
            " LEFT JOIN iki_entity x ON x.registry_id = %(registry)s"
            "  AND x.identity_hash = f.identity_hash"
            " LEFT JOIN iki_entity h ON h.owner_project_id = %(project)s AND h.entry_id = x.id",
            params,
        )
        cursor.execute("CREATE UNIQUE INDEX ON iki_import_entries (identity_hash)")
        cursor.execute("ANALYZE iki_import_entries")
        # Each line's record: a new one, an earlier line's of the same identity, or the
        # project's own of the entry there is; and for an identity, its entry: a new one
        # (the first line with it makes it), or the registry's.
        cursor.execute("DROP TABLE IF EXISTS iki_import_resolved")
        cursor.execute(
            "CREATE TEMP TABLE iki_import_resolved ON COMMIT DROP AS "  # noqa: S608 - ours; values are parameters
            "WITH lines AS ("
            " SELECT s.line, s.entity_id, s.type, n.entry_id, n.entry_type, n.entry_hrid,"
            "  n.superseded_by_id, n.new_entry AND n.first_line = s.line AS makes_entry,"
            "  CASE WHEN n.new_entry AND n.first_line <> s.line"
            "   THEN n.first_line END AS registered_by,"
            # A new identity is the first line's type: a later line of it as another type
            # would be that entry under another type (ADR 0165).
            "  CASE WHEN n.new_entry THEN n.first_type END AS first_type,"
            "  COALESCE(n.held_id, n.first_entity_id, s.entity_id) AS target,"
            "  n.held_id IS NOT NULL AS reused"
            f" FROM {table} s"
            " LEFT JOIN iki_import_entries n ON n.identity_hash = s.identity_hash"
            "  AND s.type = ANY(%(identified)s)"
            ") SELECT line, entity_id, type, entry_id, entry_type, entry_hrid, superseded_by_id,"
            " makes_entry, registered_by, first_type, target, reused,"
            " target = entity_id AND NOT reused AS new,"
            " row_number() OVER (PARTITION BY target ORDER BY line) = 1 AS first"
            " FROM lines",
            params,
        )
        cursor.execute("DROP TABLE iki_import_entries")
        cursor.execute("CREATE UNIQUE INDEX ON iki_import_resolved (line)")
        _refuse_unregistrable(cursor)
        # An entity the run already changed (one of its inputs, updated) keeps that effect.
        cursor.execute(
            "UPDATE iki_import_resolved r SET first = false WHERE first AND EXISTS ("
            " SELECT 1 FROM iki_run_effect e WHERE e.run_id = %(run)s AND e.entity_id = r.target)",
            params,
        )
        cursor.execute("ANALYZE iki_import_resolved")
        _place_along_entries(cursor, imported, table, identified)
        registered = 0
        for type_name in identified:
            registered += _insert_entries(cursor, table, type_name, params)
        new = f"FROM {table} s JOIN iki_import_resolved r USING (line) WHERE r.new"
        for type_name in types:
            entity_type = registry.types()[type_name]
            prefix, digits = entity_type.hrid_prefix, entity_type.hrid_digits
            if type_name in identified:
                # A record of an entry has the entry's HRID.
                cursor.execute(
                    "INSERT INTO iki_entity (id, type, hrid, owner_project_id, entry_id,"  # noqa: S608 - ours; values are parameters
                    " created_by_id, created_at, revision, updated_at)"
                    " SELECT s.entity_id, s.type, e.hrid, %(project)s, r.entry_id, %(user)s,"
                    f"  now(), 1, now() FROM {table} s JOIN iki_import_resolved r USING (line)"
                    " JOIN iki_entity e ON e.id = r.entry_id"
                    " WHERE r.new AND s.type = %(type)s ORDER BY s.line",
                    {**params, "type": type_name},
                )
            else:
                cursor.execute(
                    "INSERT INTO iki_entity (id, type, hrid, owner_project_id, created_by_id,"  # noqa: S608 - ours; values are parameters
                    " created_at, revision, updated_at)"
                    " SELECT entity_id, type, %(prefix)s || '-' ||"
                    "  CASE WHEN length(n) >= %(digits)s THEN n ELSE lpad(n, %(digits)s, '0') END,"
                    "  %(project)s, %(user)s, now(), 1, now()"
                    " FROM (SELECT o.*, nextval(%(sequence)s::regclass)::text AS n FROM ("
                    f"  SELECT s.entity_id, s.type {new} AND s.type = %(type)s"
                    "  ORDER BY s.line) o) q",
                    {
                        **params,
                        "prefix": prefix,
                        "digits": digits,
                        "sequence": sequence_name(prefix or ""),
                        "type": type_name,
                    },
                )
            form = shape(type_name)
            for part in form.parts:
                if not part.entry:
                    _insert_part(cursor, table, part, type_name)
            own = sorted(n for p in form.parts if not p.entry for n in p.fields)
            # A record of an entry whose identity it spells otherwise keeps what it gave
            # (ADR 0138), as a create's does.
            cursor.execute(
                "INSERT INTO iki_revision (id, entity_id, number, kind, changed_fields, user_id,"  # noqa: S608 - ours; values are parameters
                " via, client, reason, submitted, created_at, history_seen)"
                " SELECT uuidv7(), s.entity_id, 1, 'created',"
                "  ARRAY(SELECT c FROM unnest(s.changed) c WHERE c = ANY(%(own)s) ORDER BY c),"
                "  %(user)s, 'run', '', %(reason)s, p.submitted, now(), (SELECT iki_history_seen())"
                f" FROM {table} s JOIN iki_import_resolved r USING (line)"
                " LEFT JOIN iki_import_spelled p USING (line)"
                " WHERE r.new AND s.type = %(type)s ORDER BY s.line",
                {**params, "own": own, "type": type_name},
            )
        cursor.execute(
            "INSERT INTO iki_entity_project (id, entity_id, project_id, linked_by_id, linked_at)"
            f" SELECT uuidv7(), s.entity_id, %(project)s, %(user)s, now() {new}",
            params,
        )
        created = cursor.rowcount
        # A record whose line registered its entry names the entry's first revision too,
        # as a create's does (ADR 0115).
        cursor.execute(
            "INSERT INTO iki_run_effect (run_id, entity_id, kind, revision, entry_id,"
            " entry_revisions)"
            " SELECT %(run)s, r.target, CASE WHEN r.reused THEN 'reused' ELSE 'created' END,"
            "  CASE WHEN r.reused THEN NULL ELSE 1 END, m.entry_id,"
            "  CASE WHEN m.entry_id IS NOT NULL THEN ARRAY[1] ELSE '{}' END"
            " FROM iki_import_resolved r"
            " LEFT JOIN (SELECT target, entry_id FROM iki_import_resolved WHERE makes_entry) m"
            "  ON m.target = r.target"
            " WHERE r.first ORDER BY r.line",
            params,
        )
        reused = cursor.rowcount - created
        # Every line's origin, the first of a record and each that found it again: one
        # molecule is in several databases' records (ADR 0154).
        cursor.execute(
            "INSERT INTO iki_external_origin (run_id, entity_id, revision, source, identifier,"  # noqa: S608 - ours; values are parameters
            " version, url, retrieved_at, sha256, file_id, made, created_at)"
            " SELECT %(run)s, r.target, CASE WHEN r.first AND NOT r.reused THEN 1 END,"
            "  s.origin->>'source', s.origin->>'identifier', s.origin->>'version',"
            "  s.origin->>'url', (s.origin->>'retrieved_at')::timestamptz,"
            "  s.origin->>'sha256', (s.origin->>'file_id')::uuid,"
            "  NULLIF(s.origin->'made', 'null'::jsonb), now()"
            f" FROM {table} s JOIN iki_import_resolved r USING (line)"
            " WHERE s.origin IS NOT NULL ORDER BY s.line",
            params,
        )
        # How each was made (ADR 0106): Iki's record of the action's software, then what
        # each origin's source says, skipping those the matched lines kept one by one.
        cursor.execute(
            "INSERT INTO iki_entity_making (entity_id, run_id, origin_id, by, kind, method,"  # noqa: S608 - ours; values are parameters
            " software, version, created_at)"
            " SELECT r.target, %(run)s, NULL, 'iki', 'computational', '', t->>0, t->>1, now()"
            f" FROM {table} s JOIN iki_import_resolved r USING (line)"
            " CROSS JOIN LATERAL jsonb_array_elements(s.made_with) t"
            " WHERE r.first AND s.made_with IS NOT NULL ORDER BY s.line",
            params,
        )
        cursor.execute(
            "INSERT INTO iki_entity_making (entity_id, run_id, origin_id, by, kind, method,"
            " software, version, created_at)"
            " SELECT o.entity_id, o.run_id, o.id, 'source', o.made->>'kind',"
            "  COALESCE(o.made->>'method', ''), COALESCE(t->>'name', ''),"
            "  COALESCE(t->>'version', ''), now()"
            " FROM iki_external_origin o CROSS JOIN LATERAL jsonb_array_elements("
            "  CASE WHEN jsonb_array_length(COALESCE(o.made->'software', '[]'::jsonb)) > 0"
            "  THEN o.made->'software' ELSE '[{}]'::jsonb END) t"
            " WHERE o.run_id = %(run)s AND o.made IS NOT NULL"
            " AND NOT EXISTS (SELECT 1 FROM iki_entity_making m WHERE m.origin_id = o.id)"
            " ORDER BY o.id",
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
    _fill_entries(run, table, identified)
    return created, reused, registered


def _refuse_unregistrable(cursor: Any) -> None:  # noqa: ANN401 - a cursor
    """Lines whose identity is registered as another type, or was superseded (ADR 0115), or
    that an earlier line of the file registers as another type: as a create of them would
    be, one after the other, they're refused, naming the entry or the line."""
    cursor.execute(
        "SELECT r.line, r.entry_hrid, r.entry_type, s.hrid, r.registered_by, r.first_type"
        " FROM iki_import_resolved r"
        " LEFT JOIN iki_entity s ON s.id = r.superseded_by_id"
        " WHERE (r.entry_type IS NOT NULL"
        " AND (r.entry_type <> r.type OR r.superseded_by_id IS NOT NULL))"
        " OR (r.registered_by IS NOT NULL AND r.first_type <> r.type)"
        " ORDER BY r.line LIMIT 100"
    )
    errors = []
    for line, hrid, entry_type, replacement, first, first_type in cursor.fetchall():
        if replacement is not None:
            detail = f"{hrid} was superseded by {replacement}: register that instead."
        elif entry_type is not None:
            detail = f"This is registered already, as a {entry_type} ({hrid}): register it as one."
        else:
            detail = (
                f"Line {first} registers this as a {first_type}, the same molecule: register "
                "it as one."
            )
        errors.append({"pointer": results.pointer("creates_file", line, "type"), "detail": detail})
    if errors:
        raise results.InvalidOutput(errors)


#: How many lines that spell their entries' identities otherwise are placed at a time.
PLACE_BATCH = 1000


def _place_along_entries(
    cursor: Any,  # noqa: ANN401 - a cursor
    imported: RunImport,
    table: str,
    identified: list[str],
) -> None:
    """Each new record of an entry registered before the import, whose line spells the
    entry's identity otherwise (a plasmid from another origin, or from its other strand),
    made as :func:`entities.create` makes one (ADR 0138): what the line gives along its
    sequence is placed along the entry's, and what it gave of the identity is kept, for
    its first revision, in ``iki_import_spelled``.

    Lines that spell their identities as their entries do are found in SQL and skipped;
    the rest are placed a batch at a time."""
    cursor.execute("DROP TABLE IF EXISTS iki_import_spelled")
    cursor.execute(
        "CREATE TEMP TABLE iki_import_spelled (line bigint PRIMARY KEY, entry_id uuid NOT NULL,"
        " submitted jsonb) ON COMMIT DROP"
    )
    for type_name in identified:
        _find_spelled(cursor, table, type_name)
    after = 0
    while True:
        cursor.execute(
            "SELECT p.line, s.type, s.parts, p.entry_id FROM iki_import_spelled p"  # noqa: S608 - ours; values are parameters
            f" JOIN {table} s USING (line) WHERE p.line > %s ORDER BY p.line LIMIT %s",
            [after, PLACE_BATCH],
        )
        batch = cursor.fetchall()
        if not batch:
            break
        after = batch[-1][0]
        found = Entity.objects.filter(pk__in={entry_id for *_, entry_id in batch})
        stored = entities.current_values_of(list(found))
        placed: dict[str, list[Any]] = {"lines": [], "parts": [], "submitted": []}
        for line, type_name, parts_text, entry_id in batch:
            # Django reads jsonb as text.
            parts = json.loads(parts_text) if isinstance(parts_text, str) else parts_text
            given = {n: v for values in parts.values() for n, v in values.items()}
            entry = {n: _json(v) for n, v in stored[entry_id].items()}
            entity_type = shape(type_name).entity_type
            spelled = identity.spelled(entity_type, given, entry)
            if not spelled:
                continue
            moved = identity.placed(entity_type, given, spelled, entry.get)
            for values in parts.values():
                for name in values:
                    values[name] = moved.get(name)
            placed["lines"].append(line)
            placed["parts"].append(_ENCODE(parts))
            placed["submitted"].append(_ENCODE(spelled))
        if not placed["lines"]:
            continue
        cursor.execute(
            "UPDATE iki_import_line l SET parts = m.parts::jsonb"
            " FROM unnest(%(lines)s::bigint[], %(parts)s::text[]) AS m(line, parts)"
            " WHERE l.bulk_import_id = %(import)s AND l.round = %(round)s AND l.line = m.line",
            {**placed, "import": imported.pk, "round": imported.round},
        )
        cursor.execute(
            "UPDATE iki_import_spelled p SET submitted = m.submitted::jsonb"
            " FROM unnest(%(lines)s::bigint[], %(submitted)s::text[]) AS m(line, submitted)"
            " WHERE p.line = m.line",
            placed,
        )
    cursor.execute("ANALYZE iki_import_spelled")


def _find_spelled(cursor: Any, table: str, type_name: str) -> None:  # noqa: ANN401 - a cursor
    """The new records of one type, of entries registered before the import, whose lines
    may spell their identities otherwise: each identity value compared with the entry's
    tables, as Postgres stores both, and a list's always (in Python, item by item)."""
    form = shape(type_name)
    identity_fields = _identity(type_name)
    joins: list[str] = []
    differs: list[str] = []
    for n, part in enumerate(p for p in form.parts if p.entry):
        names = [name for name in part.fields if name in identity_fields]
        if not names:
            continue
        meta = part.model._meta
        key = meta.db_table
        columns = {f.name: _column(f) for f in meta.local_concrete_fields if not f.primary_key}
        renames = "".join(
            f" || jsonb_build_object({_literal(column)}, s.parts->{_literal(key)}->"
            f"{_literal(name)})"
            for name, column in columns.items()
            if column != name
        )
        joins.append(
            f" LEFT JOIN {_q(key)} t{n} ON t{n}.{_q(_column(meta.pk))} = q.entry_id"
            f" CROSS JOIN LATERAL (SELECT jsonb_populate_record(NULL::{_q(key)},"
            f" COALESCE(s.parts->{_literal(key)}, '{{}}'::jsonb){renames}) AS p) g{n}"
        )
        for name in names:
            column = columns.get(name)
            if is_list_table(part.fields[name]) or column is None:
                differs.append("true")
            else:
                differs.append(f"(g{n}.p).{_q(column)} IS DISTINCT FROM t{n}.{_q(column)}")
    if not differs:
        return
    cursor.execute(
        "INSERT INTO iki_import_spelled (line, entry_id)"  # noqa: S608 - ours; values are parameters
        " SELECT q.line, q.entry_id FROM iki_import_resolved q"
        f" JOIN {table} s USING (line){''.join(joins)}"
        " WHERE q.type = %s AND q.new AND q.entry_type IS NOT NULL"
        f" AND ({' OR '.join(differs)})",
        [type_name],
    )


def _insert_entries(cursor: Any, table: str, type_name: str, params: dict[str, Any]) -> int:  # noqa: ANN401 - a cursor
    """The new registry entries of one type: each identity's first line makes it, with its
    identity, derived and fact values. Returns how many."""
    entity_type = registry.types()[type_name]
    prefix, digits = entity_type.hrid_prefix, entity_type.hrid_digits
    making = f"FROM {table} s JOIN iki_import_resolved r USING (line) WHERE r.makes_entry"
    cursor.execute(
        "INSERT INTO iki_entity (id, type, hrid, registry_id, created_by_id,"  # noqa: S608 - ours; values are parameters
        " created_at, identity_hash, revision, updated_at)"
        " SELECT entry_id, type, %(prefix)s || '-' ||"
        "  CASE WHEN length(n) >= %(digits)s THEN n ELSE lpad(n, %(digits)s, '0') END,"
        "  %(registry)s, %(user)s, now(), identity_hash, 1, now()"
        " FROM (SELECT o.*, nextval(%(sequence)s::regclass)::text AS n FROM ("
        f"  SELECT r.entry_id, s.type, s.identity_hash {making} AND s.type = %(type)s"
        "  ORDER BY s.line) o) q",
        {
            **params,
            "prefix": prefix,
            "digits": digits,
            "sequence": sequence_name(prefix or ""),
            "type": type_name,
        },
    )
    count = cursor.rowcount
    if not count:
        return 0
    form = shape(type_name)
    for part in form.parts:
        if part.entry:
            _insert_part(cursor, table, part, type_name, entry=True)
    names = sorted(n for p in form.parts if p.entry for n in p.fields)
    cursor.execute(
        "INSERT INTO iki_revision (id, entity_id, number, kind, changed_fields, user_id,"  # noqa: S608 - ours; values are parameters
        " via, client, reason, project_id, created_at, history_seen)"
        " SELECT uuidv7(), r.entry_id, 1, 'created',"
        "  ARRAY(SELECT c FROM unnest(s.changed) c WHERE c = ANY(%(names)s) ORDER BY c),"
        "  %(user)s, 'run', '', %(reason)s, %(project)s, now(), (SELECT iki_history_seen())"
        f" {making} AND s.type = %(type)s"
        " ORDER BY s.line",
        {**params, "names": names, "type": type_name},
    )
    return int(count)


#: How many lines that may fill their registry entries are compared with them at a time.
FILL_BATCH = 1000


def _fill_entries(run: ActionRun, table: str, identified: list[str]) -> None:
    """Each line of an identity, but the one that registers it, fills what its entry hasn't
    got yet, as a holder's create would (ADR 0115): a fact the entry has another value
    for, or that an earlier line gives another value, is refused, naming that line.

    Lines that give only what their entries have already are found in SQL and skipped,
    so an import of identities registered already costs a few queries per type, not per
    line. The rest are compared with their entries a batch at a time, in line order, and
    the entries they fill are written."""
    if not identified:
        return
    with connection.cursor() as cursor:
        cursor.execute("DROP TABLE IF EXISTS iki_import_fills")
        cursor.execute(
            "CREATE TEMP TABLE iki_import_fills (line bigint PRIMARY KEY, type text NOT NULL,"
            " entry_id uuid NOT NULL, registered_by bigint) ON COMMIT DROP"
        )
        for type_name in identified:
            _find_fills(cursor, table, type_name)
        cursor.execute("ANALYZE iki_import_fills")
    filling = _Filling(run)
    after = 0
    while len(filling.errors) < MAX_ERRORS:
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT f.line, f.type, s.parts, f.entry_id, f.registered_by"  # noqa: S608 - ours; values are parameters
                f" FROM iki_import_fills f JOIN {table} s USING (line)"
                " WHERE f.line > %s ORDER BY f.line LIMIT %s",
                [after, FILL_BATCH],
            )
            batch = cursor.fetchall()
        if not batch:
            break
        after = batch[-1][0]
        filling.batch(batch)
    if filling.errors:
        raise results.InvalidOutput(filling.errors[:MAX_ERRORS])


def _find_fills(cursor: Any, table: str, type_name: str) -> None:  # noqa: ANN401 - a cursor
    """The lines of one type that give their entry a value it hasn't got, the same: each
    compared with the entry's tables, as Postgres stores both."""
    form = shape(type_name)
    identity = _identity(type_name)
    joins: list[str] = []
    differs: list[str] = []
    for n, part in enumerate(p for p in form.parts if p.entry):
        meta = part.model._meta
        key = meta.db_table
        columns = {f.name: _column(f) for f in meta.local_concrete_fields if not f.primary_key}
        renames = "".join(
            f" || jsonb_build_object({_literal(column)}, s.parts->{_literal(key)}->"
            f"{_literal(name)})"
            for name, column in columns.items()
            if column != name
        )
        joins.append(
            f" LEFT JOIN {_q(key)} t{n} ON t{n}.{_q(_column(meta.pk))} = q.entry_id"
            f" CROSS JOIN LATERAL (SELECT jsonb_populate_record(NULL::{_q(key)},"
            f" COALESCE(s.parts->{_literal(key)}, '{{}}'::jsonb){renames}) AS p) g{n}"
        )
        for name, f in part.fields.items():
            if name in identity or f.removed:
                continue
            given = f"s.parts->{_literal(key)}->{_literal(name)}"
            some = f"{given} IS NOT NULL AND {given} NOT IN ('null'::jsonb, '[]'::jsonb)"
            column = columns.get(name)
            if is_list_table(f) or column is None:
                # Compared item by item, with the entry's, in Python.
                differs.append(f"({some})")
            else:
                differs.append(
                    f"({some} AND (g{n}.p).{_q(column)} IS DISTINCT FROM t{n}.{_q(column)})"
                )
    if not differs:
        return
    cursor.execute(
        "INSERT INTO iki_import_fills (line, type, entry_id, registered_by)"  # noqa: S608 - ours; values are parameters
        " SELECT q.line, q.type, q.entry_id, q.registered_by"
        " FROM iki_import_resolved q"
        f" JOIN {table} s USING (line){''.join(joins)}"
        " WHERE q.type = %s AND q.entry_id IS NOT NULL AND NOT q.makes_entry"
        f" AND ({' OR '.join(differs)})",
        [type_name],
    )


class _Filling:
    """What the lines compared so far fill, and the errors they make."""

    def __init__(self, run: ActionRun) -> None:
        self.run = run
        self.refs = access.refs_for(run.requested_by, run.project)
        self.errors: list[dict[str, str]] = []
        #: Which line set each entry value this import filled: a later line that gives
        #: another is refused, naming it.
        self.set_by: dict[tuple[uuid.UUID, str], int] = {}

    def error(self, line: int, name: str, detail: str) -> None:
        pointer = results.pointer("creates_file", line, "values", *name.split("/"))
        self.errors.append({"pointer": pointer, "detail": detail})

    def batch(self, batch: list[tuple[Any, ...]]) -> None:
        """Compares a batch of lines with their entries, locked, in line order, and writes
        what they fill."""
        ids = sorted({entry_id for _, _, _, entry_id, _ in batch})
        locked = {
            e.pk: e for e in Entity.objects.select_for_update().filter(pk__in=ids).order_by("pk")
        }
        current = entities.current_values_of(list(locked.values()))
        new: dict[uuid.UUID, dict[str, Any]] = {}
        for line, type_name, parts_text, entry_id, registered_by in batch:
            parts = json.loads(parts_text) if isinstance(parts_text, str) else parts_text
            entry = locked[entry_id]
            form = shape(type_name)
            given = self._given(line, form, parts, _identity(type_name))
            fills, refused = entities.entry_changes(form, current[entry_id], given)
            for name, why in refused.items():
                earlier = self.set_by.get((entry_id, name), registered_by)
                if why == "settled" and earlier is not None:
                    self.error(
                        line,
                        name,
                        f"It gives {entry.hrid}'s {name} another value than line {earlier} "
                        "does: a registry fact has one value.",
                    )
                else:
                    self.error(
                        line,
                        name,
                        f"It {entities.refusal(entry, why, form.part_of(name).fields[name])}",
                    )
            for name, value in fills.items():
                current[entry_id][name] = value
                new.setdefault(entry_id, {})[name] = value
                self.set_by[(entry_id, name)] = line
        # Written even after an error, which fails the import anyway: later lines are
        # compared with what earlier ones filled.
        with entities.quietly():
            for entry_id, values in new.items():
                try:
                    filled = entities.fill_entry(
                        self.run.requested_by,
                        shape(locked[entry_id].type),
                        locked[entry_id],
                        values,
                        project=self.run.project,
                        reason=self.run.change_reason,
                        by_run=True,
                    )
                except Invalid as error:
                    # Another entry's unique value, say: on the line that filled it.
                    for name, messages in error.errors.items():
                        line = self.set_by.get(
                            (entry_id, name.split("/")[0]),
                            min(self.set_by[(entry_id, n)] for n in values),
                        )
                        self.error(line, name, f"It {' '.join(messages)}")
                    continue
                if filled is not None:
                    # The project's record of the entry has this run's effect already.
                    record = Entity.objects.filter(
                        owner_project=self.run.project, entry_id=entry_id
                    ).values_list("pk", flat=True)
                    results.add_entry_revision(self.run, record.first(), filled)

    def _given(
        self, line: int, form: Shape, parts: dict[str, Any], identity: frozenset[str]
    ) -> dict[str, Any]:
        """The line's entry values but its identity, as the database stores them."""
        tables = {p.model._meta.db_table for p in form.parts if p.entry}
        fields = form.fields()
        given: dict[str, Any] = {}
        for key, values in parts.items():
            if key not in tables:
                continue
            for name, value in values.items():
                if name in identity:
                    continue
                try:
                    given[name] = to_db(fields[name], value, self.refs)
                except ValueError as error:
                    for where, messages in problems_of(name, error).items():
                        self.error(line, where, f"It {' '.join(messages)}")
        return given


def _identity(type_name: str) -> frozenset[str]:
    owner = registry.identity_owner(registry.types()[type_name])
    return frozenset(owner.identity) if owner is not None else frozenset()


def _insert_part(
    cursor: Any,  # noqa: ANN401 - a cursor
    table: str,
    part: Part,
    type_name: str,
    *,
    entry: bool = False,
) -> None:
    """One table's rows and their history, and its list fields' items, for the new
    entities of one type: its new records, or with ``entry``, its new registry entries."""
    key_column, condition = ("r.entry_id", "r.makes_entry") if entry else ("s.entity_id", "r.new")
    new = f"FROM {table} s JOIN iki_import_resolved r USING (line) WHERE {condition}"
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
        f" SELECT owner{', ' if selected else ''}{selected} FROM ("
        f" SELECT {key_column} AS owner, jsonb_populate_record(NULL::{_q(key)},"
        f"  s.parts->{_literal(key)}{renames}) AS p {where}) q",
        [type_name],
    )
    history = part.history._meta
    kept = [
        c for c in map(_column, history.local_concrete_fields) if c not in ("entity_id", "revision")
    ]
    selected = ", ".join(f"(p).{_q(c)}" for c in kept)
    names = ", ".join(_q(c) for c in kept)
    # A reference's or person's history column is `<name>_id`, as its table's is.
    history_renames = "".join(
        f" || jsonb_build_object({_literal(_column(f))},"
        f" s.parts->{_literal(key)}->{_literal(f.name)})"
        for f in history.local_concrete_fields
        if _column(f) != f.name and f.name not in ("entity", "revision")
    )
    cursor.execute(
        f"INSERT INTO {_q(history.db_table)} (entity_id, revision{', ' if names else ''}{names})"  # noqa: S608
        f" SELECT owner, 1{', ' if selected else ''}{selected} FROM ("
        f" SELECT {key_column} AS owner, jsonb_populate_record(NULL::{_q(history.db_table)},"
        f"  s.parts->{_literal(key)}{history_renames}) AS p {where}) q",
        [type_name],
    )
    for name, f in part.fields.items():
        if not is_list_table(f) or f.removed:
            continue
        items = part.list_model(name)._meta
        if f.is_rows:
            _insert_rows(cursor, f, items, key, key_column, table, condition, type_name)
            continue
        item = _column(items.get_field("item"))
        cursor.execute(
            f"INSERT INTO {_q(items.db_table)} (entity_id, position, {_q(item)})"  # noqa: S608
            f" SELECT {key_column}, e.position - 1, e.item::uuid"
            f" FROM {table} s JOIN iki_import_resolved r USING (line)"
            f" CROSS JOIN LATERAL jsonb_array_elements_text(s.parts->{_literal(key)}->"
            f"{_literal(name)}) WITH ORDINALITY AS e(item, position)"
            f" WHERE {condition} AND s.type = %s",
            [type_name],
        )


def _insert_rows(
    cursor: Any,  # noqa: ANN401 - Django's cursor
    f: Field,
    items: Any,  # noqa: ANN401 - a model's _meta
    key: str,
    key_column: str,
    table: str,
    condition: str,
    type_name: str,
) -> None:
    """A list of rows' table (ADR 0137), each row's fields from its staged object, a
    reference's or person's into its `<name>_id` column."""
    columns = row_columns(f)
    renames = "".join(
        f" || jsonb_build_object({_literal(column)}, e.item->{_literal(name)})"
        for name, column in columns.items()
        if column != name
    )
    names = ", ".join(_q(c) for c in columns.values())
    selected = ", ".join(f"(p).{_q(c)}" for c in columns.values())
    cursor.execute(
        f"INSERT INTO {_q(items.db_table)} (entity_id, position, {names})"  # noqa: S608
        f" SELECT owner, position, {selected} FROM ("
        f" SELECT {key_column} AS owner, e.position - 1 AS position,"
        f" jsonb_populate_record(NULL::{_q(items.db_table)}, e.item{renames}) AS p"
        f" FROM {table} s JOIN iki_import_resolved r USING (line)"
        f" CROSS JOIN LATERAL jsonb_array_elements(s.parts->{_literal(key)}->"
        f"{_literal(f.name)}) WITH ORDINALITY AS e(item, position)"
        f" WHERE {condition} AND s.type = %s) q",
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
