"""Applying an import's plan: everything at once, or nothing (ADR 0096).

An Org Admin who is the project's Admin confirms the plan with a reason. A task then reads
the bundle again, and in one transaction, holding its types' identity locks, plans it
again against what's here now. If anything changed so that it can't be applied, it stops
and shows the new plan. Otherwise it writes:

- a placeholder for each person nobody here is, who can't sign in;
- each agent its revisions name, matched by its key;
- each file it names, with its bytes stored once by SHA-256 (or registered where it is);
- each registry entry its records are of that the project's registry doesn't have
  (ADR 0115), registered by the import's applier, with its ID and HRID when they're free
  here; an entry the registry has gets the values it lacks, as registering it again would;
- each new entity, with its ID, its HRID (or a new one, when its own is taken here), every
  revision with its author, time, means and reason, and each table's history; and, for an
  entity here from an earlier import, the revisions it doesn't have yet.

Its records are then checked against this deployment's plugins' rules (ADR 0127). They
stay as they were made: one that breaks a rule is reported on it, or for a rule that holds,
held for review by the applier. The database's own rules (``unique``, ``no_overlap``)
refuse the whole import instead, as unique fields do.

Revisions keep who made them and when: the import itself is the record of who brought them
here, why and when, and each imported entity's page links to it.
"""

from __future__ import annotations

import datetime as dt
import tempfile
import uuid
from collections import Counter
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import structlog
from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.db.models import F
from django.utils import timezone
from jsonschema import Draft202012Validator

from iki_server.accounts.models import SERVICE_ACCOUNT_DOMAIN, Kind, User
from iki_server.api.problems import Problem
from iki_server.audit import log as audit
from iki_server.audit.models import Agent
from iki_server.bundles import reading
from iki_server.bundles.imports import _current, _Planner, _typed, archive_hold, check_archive
from iki_server.bundles.models import (
    BundleImport,
    EntityPlan,
    ImportEntity,
    ImportPerson,
    ImportState,
)
from iki_server.bundles.review import ReviewedPlan
from iki_server.entities import breaches, hrids, lists, registry, requests, states
from iki_server.entities import services as entity_services
from iki_server.entities.dashboards import APP
from iki_server.entities.models import Entity, ProjectLink, Revision
from iki_server.entities.services import (
    _set,
    _use_files,
    _write_list,
    identity_lock,
    quietly,
)
from iki_server.entities.values import Shape, shape
from iki_server.entities.values import is_list_table as _is_list
from iki_server.files import metadata as stated
from iki_server.files import services as file_services
from iki_server.files import storage
from iki_server.files.models import Blob, File
from iki_server.files.scan_state import require_usable
from iki_server.files.scanner import ScannerError
from iki_server.org.models import registry_of
from iki_server.org.permissions import Action, can
from iki_server.org.services import Refused
from iki_server.runs import makings
from iki_server.runs import services as run_services
from iki_server.runs.models import (
    HRID_PREFIX,
    ActionRun,
    ApiCall,
    EntityMaking,
    ExternalOrigin,
    Job,
    JobLog,
    Lineage,
    MadeBy,
    Making,
    RunEffect,
    RunInclusion,
    RunInput,
    RunOutput,
)
from iki_server.tasks import task
from iki_server.tasks.backend import Retry
from iki_spec import schemas
from iki_spec.model import LIST_CODE, Field

logger = structlog.get_logger("iki.bundles")

UNEXPECTED = (
    "Something went wrong on Iki's side, so nothing was imported. Try again; if it happens "
    "again, your administrator can find what went wrong in Iki's logs."
)
CHANGED = (
    "Something here changed since it was planned, so nothing was imported: read the new "
    "plan, then apply it again."
)


class StalePlan(Refused):
    """The request names an earlier version of this import's plan."""


def apply(by: User, made: BundleImport, reason: str, *, planned_at: dt.datetime) -> BundleImport:
    """Confirms the plan: a task applies it, whole."""
    if not can(by, Action.PROJECT_IMPORT, made.project):
        raise Refused(
            f"Importing into {made.project.name} needs an Org Admin who is also its Admin."
        )
    reason = reason.strip()
    if not reason:
        raise Refused("Say why you're importing it: the import keeps it.", field="reason")
    with transaction.atomic():
        locked = BundleImport.objects.select_for_update().get(pk=made.pk)
        if locked.state not in (ImportState.PLANNED, ImportState.FAILED):
            raise Refused(f"It's {locked.get_state_display().lower()}: there's no plan to apply.")
        if locked.planned_at != planned_at:
            raise StalePlan("The plan changed. Review the current plan before applying it.")
        if not locked.plan.get("applicable"):
            raise Refused("Resolve what keeps it from being applied first, then import it again.")
        require_usable(locked.upload)
        locked.state = ImportState.APPLYING
        locked.applied_by = by
        locked.reason = reason
        locked.error = ""
        locked.save(update_fields=["state", "applied_by", "reason", "error"])
        apply_import.enqueue(str(locked.pk), planned_at.isoformat())
    return locked


@task()
def apply_import(import_id: str, planned_at: str | None = None) -> str:
    made = BundleImport.objects.select_related(
        "project__team", "applied_by", "upload__scan_result__release"
    ).get(pk=import_id)
    if made.state != ImportState.APPLYING or (
        planned_at is not None and dt.datetime.fromisoformat(planned_at) != made.planned_at
    ):
        return made.state
    if hold := archive_hold(made):
        return hold
    with tempfile.TemporaryDirectory(prefix="iki-import-") as tmp:
        path = Path(tmp) / "bundle.tar.gz"
        try:
            storage.download_file(storage.imported_key(made.upload_id), path)
            bundle = reading.read(
                path, settings.IKI_BUNDLE_TRUSTED_KEYS, limit=settings.IKI_BUNDLE_MAX_BYTES
            )
            check_archive(made, bundle)
            if bundle.trusted_as is None:
                raise reading.BundleError(
                    f"This deployment no longer trusts {bundle.source}'s key, so nothing was "
                    "imported."
                )
            _verify_registered_files(made, bundle)
            return _apply(made, bundle, Path(tmp))
        except (ScannerError, Retry):
            return _failed(made, "A file is awaiting its scan. Try applying the import again.")
        except Problem as error:
            return _failed(made, error.detail)
        except (reading.BundleError, IntegrityError, DatabaseError) as error:
            logger.warning("bundle_import_failed", import_id=import_id, error=str(error))
            return _failed(made, _said(error))
        except Exception:
            # Not one it can explain: nothing changed, and it says so, rather than seem to
            # go on applying. The task's record and the logs keep what went wrong.
            logger.exception("bundle_import_crashed", import_id=import_id)
            _failed(made, UNEXPECTED)
            raise


def _verify_registered_files(made: BundleImport, bundle: reading.Bundle) -> None:
    """Read each external version once, before taking the project's write locks.

    Check both the source the bundle declares and any external blob the reviewed plan
    will actually use. The locked replan requires confirmation if that choice changes.
    """
    decisions = made.plan.get("members", {}).get("files", {})
    reused = {
        str(blob.pk): blob
        for blob in Blob.objects.filter(
            pk__in={d["blob"] for d in decisions.values() if "blob" in d},
            registered_external=True,
        )
    }
    checked: dict[tuple[str, str], tuple[str, int]] = {}

    def verify(uri: str, version: str, sha: str, size: int, label: str) -> None:
        if version.strip() in ("", "null"):
            raise reading.BundleError(f"{label} requires an immutable object version.")
        source = (uri, version)
        if source not in checked:
            try:
                found = file_services.registration_object(uri, version)
                if found["size"] != size:
                    raise reading.BundleError(
                        f"{label}: the registered version has a different size."
                    )
                bucket, key = storage.split_uri(uri)
                checked[source] = storage.digest_object(bucket, key, version)
            except (Refused, BotoCoreError, ClientError, storage.StorageError, OSError) as error:
                # A local copy may belong to a different project. Keep its location and
                # the storage provider's response out of the caller's import record.
                raise reading.BundleError(
                    f"{label}: Iki could not read the pinned version at an approved location. "
                    "Check the object's version, availability and registration settings."
                ) from error
        if checked[source] != (sha, size):
            raise reading.BundleError(
                f"{label}: the registered bytes differ from the recorded SHA-256 or size."
            )

    for item in bundle.lines("files.ndjson"):
        if registered := item["registered"]:
            verify(
                registered["uri"], registered["version"], item["sha256"], item["size"], item["name"]
            )
        target = reused.get(decisions.get(item["id"], {}).get("blob", ""))
        if target is not None:
            if (target.sha256, target.size) != (item["sha256"], item["size"]):
                raise reading.BundleError(
                    f"{item['name']}: the local file has a different SHA-256 or size."
                )
            verify(
                target.storage_uri, target.object_version, target.sha256, target.size, item["name"]
            )


def _said(error: Exception) -> str:
    if isinstance(error, reading.BundleError):
        return str(error)
    first = str(error).strip().splitlines()[0] if str(error).strip() else type(error).__name__
    return f"The database refused it, so nothing was imported: {first}"


def _failed(made: BundleImport, message: str) -> str:
    with transaction.atomic():
        changed = BundleImport.objects.filter(
            pk=made.pk, state=ImportState.APPLYING, planned_at=made.planned_at
        ).update(state=ImportState.FAILED, error=message)
        if not changed:
            return BundleImport.objects.get(pk=made.pk).state
        audit.record(
            "bundle.import_failed",
            actor=made.applied_by,
            target=made,
            project=made.project,
            data={"import": str(made.pk), "sha256": made.sha256, "why": message},
        )
    return ImportState.FAILED


def _apply(made: BundleImport, bundle: reading.Bundle, tmp: Path) -> str:
    writer = _Writer(made, bundle, tmp)
    prepared = writer.prepare_files()
    with transaction.atomic(), quietly():
        # Project registry changes take the project row lock. Use its current registry
        # through the entire replan and write, even if it moved during the download.
        locked = (
            BundleImport.objects.select_related("project__team", "applied_by")
            .select_for_update(of=("self", "project"))
            .get(pk=made.pk)
        )
        if locked.state != ImportState.APPLYING or locked.planned_at != made.planned_at:
            return locked.state
        for type_name in sorted({t for p in bundle.manifest["plugins"] for t in p["types"]}):
            entity_type = registry.types().get(type_name)
            if entity_type is not None and not entity_type.abstract:
                identity_lock(entity_type, registry_of(locked.project), shared=False)
        confirmed = ReviewedPlan.read(locked)
        ImportEntity.objects.filter(bundle_import=locked).delete()
        ImportPerson.objects.filter(bundle_import=locked).delete()
        locked.plan = _Planner(locked, bundle, prepared_blobs=prepared).plan()
        difference = confirmed.difference(ReviewedPlan.read(locked))
        if difference or not locked.plan["applicable"]:
            locked.state = ImportState.PLANNED
            now = timezone.now()
            if locked.planned_at is not None:
                now = max(now, locked.planned_at + dt.timedelta(microseconds=1))
            locked.planned_at = now
            locked.error = f"{CHANGED} {difference}".strip()
            locked.save()
            audit.record(
                "bundle.import_planned",
                actor=locked.applied_by,
                target=locked,
                project=locked.project,
                data={
                    "import": str(locked.pk),
                    "bundle": str(locked.bundle_id),
                    "sha256": locked.sha256,
                    "why": locked.error,
                    "reviewed_at": made.planned_at.isoformat() if made.planned_at else None,
                    "planned_at": now.isoformat(),
                    **locked.plan["counts"],
                },
            )
            return locked.state
        writer.made = locked
        written = writer.write()
        locked.state = ImportState.APPLIED
        locked.applied_at = timezone.now()
        locked.save()
        data = {
            "import": str(locked.pk),
            "bundle": str(locked.bundle_id),
            "sha256": locked.sha256,
            "source": locked.trusted_as,
            "reason": locked.reason,
            **written,
        }
        audit.record(
            "bundle.imported",
            actor=locked.applied_by,
            target=locked,
            project=locked.project,
            data={
                **data,
                "planned_at": locked.planned_at.isoformat() if locked.planned_at else None,
            },
        )
        from iki_server.webhooks import events
        from iki_server.webhooks.services import emit

        emit(
            "bundle.imported",
            {
                **data,
                "project": events.project_data(locked.project),
                "actor": events.actor_data(locked.applied_by),
            },
            project=locked.project,
        )
    return ImportState.APPLIED


@dataclass
class _Writer:
    made: BundleImport
    bundle: reading.Bundle
    tmp: Path
    file_blobs: dict[str, Blob] = field(default_factory=dict)
    people: dict[str, User] = field(default_factory=dict)
    agents: dict[str, Agent] = field(default_factory=dict)
    counts: Counter[str] = field(default_factory=Counter)
    #: The highest number kept from the source, by HRID sequence.
    kept: dict[str, int] = field(default_factory=dict)
    #: Each registry entry's ID here, by its ID in the bundle, where they differ.
    renamed: dict[uuid.UUID, uuid.UUID] = field(default_factory=dict)

    def write(self) -> dict[str, int]:
        self._people()
        self._agents()
        self._files()
        planned = {
            str(r.entity_id): r
            for r in ImportEntity.objects.filter(
                bundle_import=self.made, action__in=[EntityPlan.NEW, EntityPlan.UPDATE]
            )
        }
        self._reserve_hrids(planned)
        self._entries(planned)
        for line in self.bundle.lines("entities.ndjson"):
            row = planned.get(line["id"])
            if row is None:
                continue
            if row.action == EntityPlan.NEW:
                self._create(line, row)
            else:
                self._append(line, row)
        self._signatures(planned)
        for run in self.bundle.lines("runs.ndjson"):
            self._run(run)
        self._rename_in_apps()
        checked = breaches.check_brought(
            [r.entity_id for r in planned.values()],
            user=self.made.applied_by,
            reason=f"Imported from {self.made.trusted_as}, as it was there: {self.made.reason}",
        )
        self.counts["breaking"] = sum(checked.broken.values())
        self.counts["held"] = sum(checked.held.values())
        return {
            "created": self.counts["created"],
            "registered": self.counts["registered"],
            "updated": self.counts["updated"],
            "revisions": self.counts["revisions"],
            "files": self.counts["files"],
            "placeholders": self.counts["placeholders"],
            "runs": self.counts["runs"],
            "apps_renamed": self.counts["apps_renamed"],
            "signatures": self.counts["signatures"],
            "breaking": self.counts["breaking"],
            "held": self.counts["held"],
        }

    def _reserve_hrids(self, planned: dict[str, ImportEntity]) -> None:
        """Move each sequence past all the numbers the plan keeps before allocating any."""
        highest: dict[str, int] = {}

        def note(prefix: str | None, hrid: str) -> None:
            head, _, digits = hrid.rpartition("-")
            if head == prefix and digits.isdigit():
                highest[head] = max(highest.get(head, 0), int(digits))

        types = registry.types()
        for row in planned.values():
            # A record of an existing entry already has its entry's local number.
            if row.action == EntityPlan.NEW and (row.registers or row.entry_id is None):
                note(types[row.type].hrid_prefix, row.hrid_here)
        for run in self.made.plan["members"]["runs"].values():
            if run["action"] == "keep_hrid":
                note(HRID_PREFIX, run["label"])
        for prefix, number in sorted(highest.items()):
            self._keep(prefix, f"{prefix}-{number}")

    # Who --------------------------------------------------------------------------------

    def _people(self) -> None:
        source = self.made.trusted_as
        for person in ImportPerson.objects.filter(bundle_import=self.made).select_related("user"):
            user = person.user
            if user is None:
                user = _placeholder(person, source)
                person.user = user
                person.save(update_fields=["user"])
                self.counts["placeholders"] += 1
            self.people[str(person.source_id)] = user

    def _agents(self) -> None:
        for line in self.bundle.lines("agents.ndjson"):
            info = audit.AgentInfo(
                client_id=line["client_id"],
                client_name=line["client_name"],
                grant=uuid.UUID(line["grant"]) if line["grant"] else None,
                session=line["session"],
                software=line["software"],
                model=line["model"],
            )
            key = info.key()
            if key != line["key"]:
                raise reading.BundleError("An agent's key does not match its identity.")
            identity = asdict(info)
            agent, _ = Agent.objects.get_or_create(
                key=key, defaults={**identity, "first_seen_at": line["first_seen_at"]}
            )
            if any(getattr(agent, field) != value for field, value in identity.items()):
                raise reading.BundleError("An agent's identity conflicts with the local record.")
            if self.agents.setdefault(line["id"], agent) != agent:
                raise reading.BundleError("The bundle gives one agent ID two identities.")

    def _user(self, source_id: str | None) -> User | None:
        return self.people.get(source_id) if source_id else None

    def _agent(self, source_id: str | None) -> Agent | None:
        return self.agents.get(source_id) if source_id else None

    # Files ------------------------------------------------------------------------------

    def prepare_files(self) -> set[uuid.UUID]:
        """Verify and scan reviewed sources before locking the project and writing its records.

        Keep quarantine evidence even if the import cannot proceed. During the final
        replan, blobs created here are part of the import's own work.
        """
        decisions = self.made.plan.get("members", {}).get("files", {})
        if not decisions:
            return set()  # empty or legacy plan; the replan checks it before applying
        lines = list(self.bundle.lines("files.ndjson"))
        self._store([f for f in lines if decisions[f["id"]]["storage"] == "store"])
        existing = Blob.objects.in_bulk(
            {uuid.UUID(d["blob"]) for d in decisions.values() if "blob" in d}
        )
        created: set[uuid.UUID] = set()
        checked: set[uuid.UUID] = set()
        for f in lines:
            decision = decisions[f["id"]]
            if "blob" in decision:
                blob = existing[uuid.UUID(decision["blob"])]
            elif decision["storage"] == "share_blob":
                blob = self.file_blobs[decision["source_file"]]
            else:
                blob, added = self._prepare_blob(f)
                if added:
                    created.add(blob.pk)
            if (blob.sha256, blob.size) != (f["sha256"], f["size"]):
                raise reading.BundleError("A file's recorded SHA-256 or size differs here.")
            if blob.pk not in checked:
                file_services.scan_source(blob).require_usable()
                checked.add(blob.pk)
            self.file_blobs[f["id"]] = blob
        return created

    def _files(self) -> None:
        for f in self.bundle.lines("files.ndjson"):
            if self.made.plan["members"]["files"][f["id"]]["action"] != "new":
                continue
            blob_id = self.file_blobs[f["id"]].pk
            # When its data was acquired, and how that was known, as the deployment it came
            # from had it (ADR 0177).
            when = f.get("acquired") or {}
            # What it states (ADR 0224), checked as a reader's would be: a bundle is
            # another deployment's word, and its file's bytes aren't read again here.
            try:
                facts = stated.clean_stored(f.get("facts"))
                groups = stated.clean_read_groups(f.get("read_groups"))
            except ValueError as error:
                raise reading.BundleError(f"A file's stated facts aren't valid: {error}") from None
            File.objects.create(
                id=f["id"],
                blob_id=blob_id,
                name=f["name"],
                media_type=f["media_type"],
                project=self.made.project,
                created_by=self._user(f["created_by"]),
                agent=self._agent(f["agent"]),
                created_at=f["created_at"],
                acquired_on=when.get("on"),
                acquired_at=when.get("at", ""),
                acquired_from=when.get("source", ""),
                facts=facts,
                read_groups=groups,
                stated_text=stated.search_text(facts, groups),
            )
            self.counts["files"] += 1

    def _store(self, files: list[dict[str, Any]]) -> None:
        """Store the reviewed new blobs in one archive pass, checking each SHA-256."""
        sizes = {f["sha256"]: f["size"] for f in files}
        for member in self.bundle.manifest["members"]:
            path = member["path"]
            if (
                path.startswith("blobs/")
                and sizes.get(path.rsplit("/", 1)[1], member["size"]) != member["size"]
            ):
                raise reading.BundleError("A carried file differs from its declared size.")
        wanted = {sha for sha in sizes if not storage.exists(storage.blob_key(sha))}
        for sha256, chunks in self.bundle.blobs(wanted):
            local = self.tmp / sha256
            try:
                with local.open("wb") as out:
                    for chunk in chunks:
                        out.write(chunk)
                storage.upload_file(local, storage.blob_key(sha256), "application/octet-stream")
            finally:
                local.unlink(missing_ok=True)

    @staticmethod
    def _prepare_blob(f: dict[str, Any]) -> tuple[Blob, bool]:
        registered = f["registered"]
        if registered:
            uri, version = registered["uri"], registered["version"]
            return Blob.objects.get_or_create(
                storage_uri=uri,
                object_version=version,
                registered_external=True,
                defaults={"sha256": f["sha256"], "size": f["size"]},
            )
        key = storage.blob_key(f["sha256"])
        return Blob.objects.get_or_create(
            sha256=f["sha256"],
            registered_external=False,
            defaults={"size": f["size"], "storage_uri": storage.uri(key)},
        )

    # Registry entries -------------------------------------------------------------------

    def _entries(self, planned: dict[str, ImportEntity]) -> None:
        """Registers the entries the plan registers, before the records of them; gives those
        registered already the values they lack, as registering them again would."""
        lines = {e["id"]: e for e in self.bundle.lines("entries.ndjson")}
        rows = [
            (r, lines[line_entry])
            for line in self.bundle.lines("entities.ndjson")
            if (r := planned.get(line["id"])) is not None
            and r.entry_id is not None
            and (line_entry := line.get("entry")) in lines
        ]
        for row, line in rows:
            if uuid.UUID(line["id"]) != row.entry_id:
                self.renamed[uuid.UUID(line["id"])] = row.entry_id  # type: ignore[assignment]
        self._options(lines.values())
        by = self.made.applied_by
        assert by is not None  # noqa: S101 - it's being applied
        project = self.made.project
        reason = f"Imported from {self.made.trusted_as}: {self.made.reason}"
        for row, line in rows:
            form = shape(line["type"])
            values = self._remap(form, _typed(form, line["values"]))
            if row.registers:
                entry, _ = entity_services._new_entry(
                    by, form, project, values, reason, False, pk=row.entry_id, hrid=row.hrid_here
                )
                if row.hrid_here:
                    self._keep(form.entity_type.hrid_prefix, entry.hrid)
                else:
                    row.hrid_here = entry.hrid
                    row.save(update_fields=["hrid_here"])
                self.counts["registered"] += 1
            elif row.action == EntityPlan.NEW:
                entry = Entity.objects.get(pk=row.entry_id or uuid.UUID(int=0))
                fields = form.entry_fields()
                entity_services.fill_entry(
                    by,
                    form,
                    entry,
                    {n: v for n, v in values.items() if n in fields and not fields[n].removed},
                    project=project,
                    reason=reason,
                )

    def _options(self, lines: Iterable[dict[str, Any]]) -> None:
        """The lab lists' options (ADR 0270) the bundle's records name, by the option of the
        same code here: the plan made a record naming one this registry hasn't got a
        conflict, so the rest are here."""
        registry_id = self.made.project.registry_id
        for line in lines:
            if not lists.is_list(line["type"]):
                continue
            found = lists.find(registry_id, line["type"], str(line["values"].get(LIST_CODE)))
            if found is not None and found.entry.pk != uuid.UUID(line["id"]):
                self.renamed[uuid.UUID(line["id"])] = found.entry.pk

    def _remap(
        self, form: Shape, values: dict[str, Any], ids: _Ids | None = None
    ) -> dict[str, Any]:
        """Values whose references name a bundle's registry entry by its ID there, naming
        it by its ID here; and the people they name, as they are here (ADR 0122): the same
        person by their email address, or their placeholder. ``ids``, if given, is told
        each ID named anew, by field and row cell."""
        fields = {n: f for p in form.parts for n, f in p.fields.items()}
        out = dict(values)
        for name, value in values.items():
            f = fields.get(name)
            if f is None or value is None:
                continue
            if f.is_rows and f.items is not None:
                out[name] = [self._row(f.items, r, name, ids) for r in value]
            elif f.type == "person":
                out[name] = _noted(ids, name, None, value, self._person(value))
            elif f.type == "list" and f.items is not None and f.items.type == "person":
                found = [_noted(ids, name, None, v, self._person(v)) for v in value]
                out[name] = [p for p in found if p is not None]
            elif not self.renamed:
                continue
            elif f.type == "ref":
                out[name] = _noted(ids, name, None, value, self._renamed(value))
            elif f.type == "list" and f.items is not None and f.items.type == "ref":
                out[name] = [_noted(ids, name, None, v, self._renamed(v)) for v in value]
        return out

    def _row(
        self, row: Field, values: dict[str, Any], field: str = "", ids: _Ids | None = None
    ) -> dict[str, Any]:
        """A list's row (ADR 0137), its people and references as :meth:`_remap` has them."""
        out = dict(values)
        for name, cell in row.fields.items():
            if values.get(name) is None:
                continue
            if cell.type == "person":
                out[name] = _noted(ids, field, name, values[name], self._person(values[name]))
            elif cell.type == "ref" and self.renamed:
                out[name] = _noted(ids, field, name, values[name], self._renamed(values[name]))
        return out

    def _person(self, value: Any) -> Any:  # noqa: ANN401 - a UUID, or its text
        """Someone a value names, as they are here: by their ID, as the value had it."""
        found = self._user(str(value))
        if found is None:
            return None
        return found.pk if isinstance(value, uuid.UUID) else str(found.pk)

    def _renamed(self, value: Any) -> Any:  # noqa: ANN401 - a UUID, or its text
        if value is None:
            return None
        found = self.renamed.get(value if isinstance(value, uuid.UUID) else uuid.UUID(str(value)))
        if found is None:
            return value
        return found if isinstance(value, uuid.UUID) else str(found)

    # Entities ---------------------------------------------------------------------------

    def _create(self, line: dict[str, Any], row: ImportEntity) -> None:
        form = shape(line["type"])
        values = self._remap(form, _typed(form, _current(line)))
        revisions = sorted(line["revisions"], key=lambda r: int(r["number"]))
        instance = form.model(
            owner_project=self.made.project, created_by=self._user(line["created_by"])
        )
        # Its root table's ID: each table below takes it from there.
        instance.id = uuid.UUID(line["id"])
        if row.entry_id is not None:
            # A record has its entry's HRID: the entry's here.
            entry = Entity.objects.only("hrid").get(pk=row.entry_id)
            instance.entry_id = entry.pk
            row.hrid_here = entry.hrid
            row.save(update_fields=["hrid_here"])
        instance.hrid = row.hrid_here
        instance.created_at = line["created_at"]
        instance.archived_at = line["archived_at"]
        instance.revision = len(revisions)
        instance.updated_at = revisions[-1]["created_at"]
        self._set_values(form, instance, values)
        instance.save()
        if row.entry_id is not None:
            pass  # Its HRID is its entry's, kept as that was.
        elif row.hrid_here:
            self._keep(form.entity_type.hrid_prefix, instance.hrid)
        else:
            row.hrid_here = instance.hrid
            row.save(update_fields=["hrid_here"])
        self._extensions_and_lists(form, instance.pk, values, fresh=True)
        self._history(form, instance.pk, line, revisions, after=0)
        self._lock(instance.pk, line, values)
        ProjectLink.objects.create(
            entity_id=instance.pk, project=self.made.project, linked_by=self.made.applied_by
        )
        self.counts["created"] += 1

    def _append(self, line: dict[str, Any], row: ImportEntity) -> None:
        form = shape(line["type"])
        values = self._remap(form, _typed(form, _current(line)))
        revisions = sorted(line["revisions"], key=lambda r: int(r["number"]))
        instance = form.model._default_manager.select_for_update().get(pk=line["id"])
        had = instance.revision
        if instance.released_review_revision is not None:
            raise reading.BundleError(f"{instance.hrid} has been released and cannot be changed.")
        # The source's revisions come with its state changes (ADR 0125): its state fields
        # are values like any, and whether they lock it is set again once they're written.
        Entity.objects.filter(pk=instance.pk).update(locked_by=[])
        self._set_values(form, instance, values)
        instance.save()
        self._extensions_and_lists(form, instance.pk, values, fresh=False)
        self._history(form, instance.pk, line, revisions, after=had)
        Entity.objects.filter(pk=instance.pk).update(
            revision=len(revisions),
            updated_at=revisions[-1]["created_at"],
            archived_at=line["archived_at"],
        )
        self._lock(instance.pk, line, values)
        self.counts["updated"] += 1

    @staticmethod
    def _lock(entity_id: uuid.UUID, line: dict[str, Any], values: dict[str, Any]) -> None:
        """Whether the record's state fields, as the bundle has them, lock it (ADR 0125)."""
        fields = states.state_fields(line["type"])
        found = states.locking(fields, values)
        if found or line.get("has_been_locked"):
            Entity.objects.filter(pk=entity_id).update(locked_by=found, has_been_locked=True)

    @staticmethod
    def _set_values(form: Shape, instance: Any, values: dict[str, Any]) -> None:  # noqa: ANN401
        for part in form.parts:
            if part.extension or part.entry:
                continue
            for name, f in part.fields.items():
                if name in values and not _is_list(f) and not f.removed:
                    _set(instance, f, values[name])

    @staticmethod
    def _extensions_and_lists(
        form: Shape, entity_id: uuid.UUID, values: dict[str, Any], *, fresh: bool
    ) -> None:
        for part in form.parts:
            if part.entry:
                continue  # The entry's: registered, or filled, with it.
            if part.extension and any(values.get(n) is not None for n in part.fields):
                ext = part.model._default_manager.filter(entity_id=entity_id).first()
                ext = ext or part.model(entity_id=entity_id)
                for name, f in part.fields.items():
                    if name in values and not _is_list(f) and not f.removed:
                        _set(ext, f, values[name])
                ext.save()
            for name, f in part.fields.items():
                if not _is_list(f) or f.removed:
                    continue
                if not fresh:
                    part.list_model(name)._default_manager.filter(entity_id=entity_id).delete()
                if values.get(name):
                    _write_list(part, f, entity_id, values[name])

    def _history(
        self,
        form: Shape,
        entity_id: uuid.UUID,
        line: dict[str, Any],
        revisions: list[dict[str, Any]],
        *,
        after: int,
    ) -> None:
        Revision.objects.bulk_create(
            Revision(
                id=r["id"],
                entity_id=entity_id,
                number=r["number"],
                kind=r["kind"],
                changed_fields=r["changed_fields"],
                user=self._user(r["user"]),
                via=r["via"],
                client=r["client"],
                agent=self._agent(r["agent"]),
                reason=r["reason"],
                created_at=r["created_at"],
                signatures=r.get("signatures") or [],
                **(
                    {
                        "state_field": r["state"]["field"],
                        "state_from": r["state"]["from"],
                        "state_to": r["state"]["to"],
                    }
                    if r.get("state")
                    else {}
                ),
            )
            for r in revisions
            if int(r["number"]) > after
        )
        self.counts["revisions"] += sum(1 for r in revisions if int(r["number"]) > after)
        parts = {p.name: p for p in form.parts}
        fields = form.fields()
        for h in line["history"]:
            number = int(h["revision"])
            if number <= after:
                continue
            part = parts[h["table"]]
            kept = self._remap(form, h["values"])
            part.history._default_manager.create(entity_id=entity_id, revision=number, **kept)
            _use_files(
                fields,
                entity_id,
                {k: v for k, v in kept.items() if k in fields},
                number,
            )

    # Signatures -------------------------------------------------------------------------

    def _signatures(self, planned: dict[str, Any]) -> None:
        """The bundle's electronic signatures on the records it brings (ADR 0126), as they
        were signed, by the keys its source trusts: each checked before it's kept, so an
        import never brings one that doesn't verify."""
        from iki_server.signatures import proof, signing_keys
        from iki_server.signatures import services as signature_services
        from iki_server.signatures.models import ForeignSigningKey, Signature, Withdrawal

        # What migrations here do to its record's history from now on, it's read through,
        # signed by this deployment's own key (ADR 0148): made now, if it has none yet.
        seen = signature_services.history_seen()
        if any(line["record"] in planned for line in self.bundle.lines("signatures.ndjson")):
            signing_keys.current()
        listed = signing_keys.listed(
            self.bundle.manifest.get("signing_keys", []), source=self.bundle.source
        )
        if listed.problems:
            raise reading.BundleError(listed.problems[0])
        trusted = listed.trusted
        entities = {line["id"]: line["type"] for line in self.bundle.lines("entities.ndjson")}
        reviews: dict[str, tuple[str, int, str, str]] = {}
        for line in self.bundle.lines("signatures.ndjson"):
            if line["record"] not in planned:
                continue
            document = proof_document(line["document"])
            key, deployment = line["key"], line["deployment"]
            if (document.get("key"), document.get("deployment")) != (
                key,
                deployment,
            ) or trusted.get(key) != deployment:
                raise reading.BundleError(
                    "A signature's signing key or deployment conflicts with its signed identity."
                )
            if "reviewed" in document:
                reviews[line["id"]] = (
                    line["record"],
                    line["revision"] + 1,
                    line["field"],
                    line["state"],
                )
            if Signature.objects.filter(pk=line["id"]).exists():
                continue
            raw = proof.unb64(line["signature"])
            checks = [
                *proof.check_document(line["document"], raw, trusted),
                proof.check_content(document, line["content"]),
            ]
            if failed := [c for c in checks if not c.ok]:
                raise reading.BundleError(
                    f"Signature {line['id']} in it doesn't verify: {failed[0].detail}"
                )
            try:
                before = proof.vouched(line.get("since"), line, trusted)
                if since := line.get("since"):
                    authority = proof_document(since["document"])
                    since_label = trusted[since["key"]]
                    if since_label != authority.get("deployment") or not signing_keys.trust(
                        since["key"], since_label, self.made
                    ):
                        raise reading.BundleError(
                            "The history attestation's deployment conflicts with its signing key."
                        )
                content = proof.migrated(proof_document(line["content"]), before)
            except proof.Unexplained as unexplained:
                raise reading.BundleError(
                    f"Signature {line['id']} in it doesn't verify: {unexplained}"
                ) from None
            signer = self._user(line["signer"])
            if signer is None:
                raise reading.BundleError(f"Signature {line['id']} names nobody in it.")
            if not signing_keys.trust(key, deployment, self.made):
                raise reading.BundleError(
                    "The signing key already belongs to a different deployment."
                )
            # How this import names, here, the people and registry entries it names: what
            # its record's history here is read through, after what was done where it came
            # from (ADR 0096, ADR 0148).
            ids = _Ids(here=signature_services.deployment())
            self._remap(shape(entities[line["record"]]), content.get("values") or {}, ids)
            made = Signature.objects.create(
                id=line["id"],
                entity_id=line["record"],
                revision=line["revision"],
                entry_revision=line["entry_revision"],
                field=line["field"],
                state=line["state"],
                index=line["index"],
                meaning=line["meaning"],
                signer=signer,
                signer_ref=str((document.get("signer") or {}).get("id", line["signer"])),
                signed_at=line["signed_at"],
                document=line["document"],
                signature=raw,
                key=line["key"],
                content=line["content"],
                deployment=line["deployment"],
                bundle_import=self.made,
                history_seen=seen,
                read_through=signature_services.read_through(line, [*before, *ids.changes()]),
            )
            if line["withdrawn"] is not None:
                w = line["withdrawn"]
                Withdrawal.objects.create(
                    signature=made,
                    by=self._user(w["by"]) or signer,
                    reason=w["reason"],
                    withdrawn_at=w["at"],
                )
            self.counts["signatures"] += 1
        # Each key this import trusts now, for its own deployment or one it vouches for.
        for bound in ForeignSigningKey.objects.filter(bundle_import=self.made).order_by("public"):
            audit.record(
                "bundle.signing_key_trusted",
                actor=self.made.applied_by,
                target=self.made,
                project=self.made.project,
                data={
                    "import": str(self.made.pk),
                    "key": bound.public,
                    "deployment": bound.deployment,
                    "source": self.made.trusted_as,
                    "for_source": bound.deployment == self.bundle.source,
                },
            )
        self._released_reviews(planned, reviews, trusted)

    def _released_reviews(
        self,
        planned: dict[str, Any],
        reviews: dict[str, tuple[str, int, str, str]],
        trusted: dict[str, str],
    ) -> None:
        """A carried QA release retains its reviewed revision here without sharing the
        imported record. Verify the evidence against the bundle's history before commit."""
        from iki_server.signatures import offline
        from iki_server.signatures import services as signature_services
        from iki_server.signatures.models import Signature

        if reviews:
            for verified in offline.verify(self.bundle, trusted):
                if verified.id in reviews and not verified.valid:
                    why = next(c.detail for c in verified.checks if not c.ok)
                    raise reading.BundleError(f"Signature {verified.id} doesn't verify: {why}")
            # A prefix of the record's history may already have been here. Verify the
            # complete chain as imported, with this deployment's field and ID mappings.
            for signed in Signature.objects.filter(pk__in=reviews):
                for check in signature_services.verify(signed):
                    if not check.ok:
                        raise reading.BundleError(
                            f"Signature {signed.pk} doesn't verify: {check.detail}"
                        )
        for row in planned.values():
            spec = requests.result_spec(row.type)
            if spec is None:
                continue
            field = states.state_fields(row.type)[spec.released_field]
            targets = [s for s in spec.released_states if spec.requires_review(field, s)]
            if not targets:
                continue
            delivered = (
                Revision.objects.filter(
                    entity_id=row.entity_id, state_field=field.name, state_to__in=targets
                )
                .order_by("number")
                .first()
            )
            if delivered is None:
                continue
            expected = (str(row.entity_id), delivered.number, field.name, delivered.state_to)
            if not delivered.signatures or any(
                reviews.get(str(s)) != expected for s in delivered.signatures
            ):
                raise reading.BundleError(f"{row.hrid}'s release is missing its signed review.")
            if not Entity.objects.filter(pk=row.entity_id, revision=delivered.number).update(
                released_review_revision=delivered.number - 2
            ):
                raise reading.BundleError(f"{row.hrid} has further revisions after release.")

    # HRIDs named in values --------------------------------------------------------------

    def _rename_in_apps(self) -> None:
        """An app's viewer that names an entity by its HRID there names it by its HRID here,
        in a revision of the import's own: the app's own revisions stay as they were."""
        renamed = dict(
            ImportEntity.objects.filter(bundle_import=self.made, action=EntityPlan.NEW)
            .exclude(hrid=F("hrid_here"))
            .values_list("hrid", "hrid_here")
        )
        by = self.made.applied_by
        if not renamed or by is None:
            return
        apps = list(
            Entity.objects.filter(
                pk__in=ImportEntity.objects.filter(
                    bundle_import=self.made,
                    type=APP,
                    action__in=[EntityPlan.NEW, EntityPlan.UPDATE],
                ).values("entity_id")
            )
        )
        values = entity_services.current_values_of(apps)
        for app in apps:
            spec = values.get(app.pk, {}).get("spec")
            if not isinstance(spec, dict):
                continue
            changed: list[str] = []
            panels = []
            for panel in spec.get("panels", []):
                props = panel.get("props") or {}
                named = props.get("entity")
                if panel.get("component") == "iki.viewer" and named in renamed:
                    changed.append(f"{named} as {renamed[named]}")
                    panel = {**panel, "props": {**props, "entity": renamed[named]}}
                panels.append(panel)
            if not changed:
                continue
            entity_services.update(
                by,
                app,
                {"spec": {**spec, "panels": panels}},
                expected_revision=app.revision,
                reason=f"Imported from {self.made.trusted_as}: its viewers name "
                f"{', '.join(changed)}, its HRID{'s' if len(changed) > 1 else ''} here.",
            )
            self.counts["apps_renamed"] += 1

    # Runs -------------------------------------------------------------------------------

    def _run(self, line: dict[str, Any]) -> None:
        """A run, as the record of what made or changed the entities: its attempts, logs and
        output, what it read and made, and what it was tied to there that didn't come."""
        action = self.made.plan["members"]["runs"][line["id"]]["action"]
        if action == "skip":
            return
        hrid = line["hrid"]
        if action == "renumber":
            hrid = run_services._next_hrid()
        else:
            self._keep(HRID_PREFIX, hrid)
        inclusions = line.get("included") or []
        ids = (
            [i["entity"] for i in line["inputs"]]
            + [c["entity"] for c in inclusions]
            + [r["parent"] for r in line["lineage"]]
        )
        here = {str(i) for i in Entity.objects.filter(pk__in=ids).values_list("pk", flat=True)}
        approval = line["approval"]
        if approval is not None:
            approval = {
                **approval,
                "requested_by": self._name(approval["requested_by"]),
                "decided_by": self._name(approval["decided_by"]),
            }
        run = ActionRun.objects.create(
            id=line["id"],
            hrid=hrid,
            action=line["action"],
            plugin_version=line["plugin_version"],
            plugin_sha=line["plugin_sha"],
            spec=line["spec"],
            reference_mask=line.get("reference_mask", []),
            project=self.made.project,
            requested_by=self._requester(line),
            via=line["via"],
            client=line["client"],
            agent=self._agent(line["agent"]),
            reason=line["reason"],
            tier=line["tier"],
            environment=line["environment"],
            volumes=_attempt_volumes(line["attempts"]),
            params=line["params"],
            plugin_settings=line["plugin_settings"],
            seed=line["seed"],
            status=line["status"],
            created_at=line["created_at"],
            started_at=line["started_at"],
            finished_at=line["finished_at"],
            error=line["error"],
            timeout=line["timeout"],
            max_attempts=line["max_attempts"],
            retry_on=line["retry_on"],
            reproduction=line["reproduction"],
            imported={
                "from": self.made.trusted_as,
                "hrid": line["hrid"],
                "import": str(self.made.pk),
                "linked": line["linked"],
                "approval": approval,
                # Who agreed to send its data to outside APIs there (ADR 0130), by name.
                **(
                    {"consent": {**c, "given_by": self._name(c["given_by"])}}
                    if (c := line.get("consent"))
                    else {}
                ),
                # What it read that isn't here: kept as it was there.
                "inputs": [i for i in line["inputs"] if i["entity"] not in here],
                **(
                    {"included": gone}
                    if (gone := [c for c in inclusions if c["entity"] not in here])
                    else {}
                ),
                "lineage": [r for r in line["lineage"] if r["parent"] not in here],
                "workers": {str(a["attempt"]): a["worker"] for a in line["attempts"]},
            },
        )
        for a in line["attempts"]:
            job = Job.objects.create(
                id=a["id"],
                run=run,
                attempt=a["attempt"],
                state=a["state"],
                project=self.made.project,
                tier=a["tier"],
                environment=a["environment"],
                environment_build=a["environment_build"],
                resources=a["resources"],
                queued_at=a["queued_at"],
                started_at=a["started_at"],
                finished_at=a["finished_at"],
                error=a["error"],
                volumes=a["volumes"],
                log_size=len(a["log"].encode()),
            )
            if a["log"]:
                JobLog.objects.create(
                    job=job, offset=0, text=a["log"], created_at=a["finished_at"] or a["queued_at"]
                )
            if (output := a["output"]) is not None:
                RunOutput.objects.create(
                    job=job,
                    run=run,
                    document=output["document"],
                    received_at=output["received_at"],
                )
        # The source's recorded call price travels as provenance with the call.
        # Only the source keeps its accounting ledger entry.
        ApiCall.objects.bulk_create(
            ApiCall(
                run=run,
                job_id=c["job"],
                api=c["api"],
                host=c["host"],
                kind=c["kind"],
                method=c["method"],
                path=c["path"],
                status=c["status"],
                error=c["error"],
                started_at=c["started_at"],
                duration_ms=c["duration_ms"],
                request_bytes=c["request_bytes"],
                response_bytes=c["response_bytes"],
                request_sha256=c["request_sha256"],
                response_sha256=c["response_sha256"],
                model=c["model"],
                version=c["version"],
                parameters=c["parameters"],
                usage=c["usage"],
                cost=c.get("cost", {}).get("amount"),
                currency=c.get("cost", {}).get("currency", ""),
            )
            for c in line.get("api_calls", ())
        )
        RunInput.objects.bulk_create(
            RunInput(
                run=run,
                name=i["name"],
                position=i["position"],
                entity_id=i["entity"],
                revision=i["revision"],
            )
            for i in line["inputs"]
            if i["entity"] in here
        )
        RunInclusion.objects.bulk_create(
            RunInclusion(
                run=run,
                input=c["input"],
                covered_materials=c.get("covered_materials"),
                position=c["position"],
                field=c["field"],
                index=c["index"],
                entity_id=c["entity"],
                revision=c["revision"],
            )
            for c in inclusions
            if c["entity"] in here
        )
        RunEffect.objects.bulk_create(
            RunEffect(run=run, entity_id=e["entity"], kind=e["kind"], revision=e["revision"])
            for e in line["effects"]
        )
        Lineage.objects.bulk_create(
            Lineage(
                run=run,
                entity_id=r["entity"],
                parent_id=r["parent"],
                parent_revision=r["parent_revision"],
                input=r["input"],
            )
            for r in line["lineage"]
            if r["parent"] in here
        )
        kept = ExternalOrigin.objects.bulk_create(
            ExternalOrigin(
                run=run,
                entity_id=o["entity"],
                revision=o["revision"],
                source=o["source"],
                identifier=o["identifier"],
                version=o["version"],
                url=o["url"],
                retrieved_at=o["retrieved_at"],
                sha256=o["sha256"],
                file_id=o["file"],
                made=o.get("made"),
                created_at=o["created_at"],
            )
            for o in line["origins"]
        )
        # How each was made (ADR 0106): Iki's records as the source deployment kept them,
        # and each origin's statement, from the origin.
        EntityMaking.objects.bulk_create(
            [
                *(
                    EntityMaking(
                        run=run,
                        entity_id=m["entity"],
                        by=MadeBy.IKI,
                        kind=Making.COMPUTATIONAL,
                        software=m["software"],
                        version=m["version"],
                        created_at=m["created_at"],
                    )
                    for m in line.get("made_with", ())
                ),
                *(row for origin in kept for row in makings.stated(origin)),
            ]
        )
        self.counts["runs"] += 1

    def _keep(self, prefix: str | None, hrid: str) -> None:
        """Notes that an HRID kept from the source took its number here."""
        head, _, number = hrid.rpartition("-")
        if prefix is None or head != prefix or not number.isdigit():
            return
        name = hrids.sequence_name(prefix)
        if int(number) > self.kept.get(name, 0):
            # At once, not when the import ends: a record renumbered later in this import
            # takes the sequence's next number, which mustn't be one kept before it.
            self.kept[name] = int(number)
            _advance(name, int(number))

    def _requester(self, line: dict[str, Any]) -> User:
        user = self._user(line["requested_by"])
        if user is None:
            raise reading.BundleError(
                f"{line['hrid']} names nobody in the bundle who asked for it."
            )
        return user

    def _name(self, source_id: str | None) -> str | None:
        user = self._user(source_id)
        return user.get_full_name() if user is not None else None


def _attempt_volumes(attempts: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The volume pin for reproducing the final attempt, whose output is compared.
    Bundles keep the attempt's original JSON even if this deployment can't use it."""
    if not attempts:
        return None
    volumes: dict[str, Any] = max(attempts, key=lambda a: a["attempt"])["volumes"]
    if volumes:
        schema = schemas.load("run-input")["properties"]["volumes"]
        validator = Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)
        if not validator.is_valid(volumes) or len({v["build"] for v in volumes.values()}) != 1:
            return None
    return volumes


@dataclass
class _Ids:
    """The IDs an import names anew, here, in a signed record's values (ADR 0096): a
    person's, by their email or as their placeholder, and a registry entry's that's
    registered here already. ``here``: this deployment, as its signatures name it."""

    here: str
    fields: dict[str, dict[str, str]] = field(default_factory=dict)
    cells: dict[str, dict[str, dict[str, str]]] = field(default_factory=dict)

    def changes(self) -> list[dict[str, Any]]:
        """As ``proof.migrated`` reads them: one change, if any ID is named anew."""
        if not self.fields and not self.cells:
            return []
        return [
            {
                "kind": "ids",
                "here": self.here,
                "at": timezone.now().date().isoformat(),
                "fields": self.fields,
                "cells": self.cells,
            }
        ]


def _noted(ids: _Ids | None, name: str, cell: str | None, old: Any, new: Any) -> Any:  # noqa: ANN401 - an ID
    """``new``, the ID ``old`` is named by here, told to ``ids`` if it differs."""
    if ids is not None and new is not None and str(new) != str(old):
        into = (
            ids.fields.setdefault(name, {})
            if cell is None
            else (ids.cells.setdefault(name, {}).setdefault(cell, {}))
        )
        into[str(old)] = str(new)
    return new


def proof_document(text: str) -> dict[str, Any]:
    """A signature's document, as JSON; empty if it isn't."""
    import json

    try:
        found = json.loads(text)
    except ValueError:
        return {}
    return found if isinstance(found, dict) else {}


def _placeholder(person: ImportPerson, source: str) -> User:
    """Someone the bundle names whom nobody here is: their name and email, signing in as
    nobody until someone with their email does (ADR 0096)."""
    service = person.kind == "service"
    name = person.name
    if service:
        # Service accounts' names are unique: say where this one is from.
        name = f"{person.name} ({source})"[:200]
        while User.objects.filter(kind=Kind.SERVICE, name__iexact=name).exists():
            name = f"{name}*"[:200]
    user = User(
        email=person.email
        if not service or person.email.endswith(SERVICE_ACCOUNT_DOMAIN)
        else f"{uuid.uuid4().hex}@{SERVICE_ACCOUNT_DOMAIN}",
        name=name,
        kind=Kind.SERVICE if service else Kind.PERSON,
        is_active=False,
        imported_from=source,
    )
    user.set_unusable_password()
    user.save()
    return user


def _advance(sequence: str, number: int) -> None:
    """Moves the HRID sequence past ``number``, which an HRID kept from the source took, so
    it never hands that number out. Only forward: see migration 0004."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT iki_hrid_advance(%s, %s)", [sequence, number])
