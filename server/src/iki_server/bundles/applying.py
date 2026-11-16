"""Applying an import's plan: everything at once, or nothing (ADR 0096).

An Org Admin who is the project's Admin confirms the plan with a reason. A task then reads
the bundle again, and in one transaction, holding its types' identity locks, plans it
again against what's here now. If anything changed so that it can't be applied, it stops
and shows the new plan. Otherwise it writes:

- a placeholder for each person nobody here is, who can't sign in;
- each agent its revisions name, matched by its key;
- each file it names, with its bytes stored once by SHA-256 (or registered where it is);
- each new entity, with its ID, its HRID (or a new one, when its own is taken here), every
  revision with its author, time, means and reason, and each table's history; and, for an
  entity here from an earlier import, the revisions it doesn't have yet.

Revisions keep who made them and when: the import itself is the record of who brought them
here, why and when, and each imported entity's page links to it.
"""

from __future__ import annotations

import tempfile
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
from django.conf import settings
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.utils import timezone

from iki_server.accounts.models import SERVICE_ACCOUNT_DOMAIN, Kind, User
from iki_server.audit import log as audit
from iki_server.audit.models import Agent
from iki_server.bundles import reading
from iki_server.bundles.imports import _current, _Planner, _typed
from iki_server.bundles.models import (
    BundleImport,
    EntityPlan,
    ImportEntity,
    ImportPerson,
    ImportState,
)
from iki_server.entities import hrids, identity, registry
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
from iki_server.files import storage
from iki_server.files.models import Blob, File
from iki_server.org.permissions import Action, can
from iki_server.org.services import Refused
from iki_server.runs import services as run_services
from iki_server.runs.models import (
    HRID_PREFIX,
    ActionRun,
    ExternalOrigin,
    Job,
    JobLog,
    Lineage,
    RunEffect,
    RunInput,
    RunOutput,
)
from iki_server.tasks import task

logger = structlog.get_logger("iki.bundles")

UNEXPECTED = (
    "Something went wrong on Iki's side, so nothing was imported. Try again; if it happens "
    "again, your administrator can find what went wrong in Iki's logs."
)
CHANGED = (
    "Something here changed since it was planned, so nothing was imported: read the new "
    "plan, then apply it again."
)


def apply(by: User, made: BundleImport, reason: str) -> BundleImport:
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
        if not locked.plan.get("applicable"):
            raise Refused("Resolve what keeps it from being applied first, then import it again.")
        locked.state = ImportState.APPLYING
        locked.applied_by = by
        locked.reason = reason
        locked.error = ""
        locked.save(update_fields=["state", "applied_by", "reason", "error"])
        apply_import.enqueue(str(locked.pk))
    return locked


@task()
def apply_import(import_id: str) -> str:
    made = BundleImport.objects.select_related("project__team", "applied_by").get(pk=import_id)
    if made.state != ImportState.APPLYING:
        return made.state
    with tempfile.TemporaryDirectory(prefix="iki-import-") as tmp:
        path = Path(tmp) / "bundle.tar.gz"
        try:
            storage.download_file(storage.imported_key(made.upload_id), path)
            bundle = reading.read(
                path, settings.IKI_BUNDLE_TRUSTED_KEYS, limit=settings.IKI_BUNDLE_MAX_BYTES
            )
            if bundle.trusted_as is None:
                raise reading.BundleError(
                    f"This deployment no longer trusts {bundle.source}'s key, so nothing was "
                    "imported."
                )
            return _apply(made, bundle, Path(tmp))
        except (reading.BundleError, IntegrityError, DatabaseError) as error:
            logger.warning("bundle_import_failed", import_id=import_id, error=str(error))
            return _failed(made, _said(error))
        except Exception:
            # Not one it can explain: nothing changed, and it says so, rather than seem to
            # go on applying. The task's record and the logs keep what went wrong.
            logger.exception("bundle_import_crashed", import_id=import_id)
            _failed(made, UNEXPECTED)
            raise


def _said(error: Exception) -> str:
    if isinstance(error, reading.BundleError):
        return str(error)
    first = str(error).strip().splitlines()[0] if str(error).strip() else type(error).__name__
    return f"The database refused it, so nothing was imported: {first}"


def _failed(made: BundleImport, message: str) -> str:
    BundleImport.objects.filter(pk=made.pk).update(state=ImportState.FAILED, error=message)
    audit.record(
        "bundle.import_failed",
        actor=made.applied_by,
        target=made,
        project=made.project,
        data={"import": str(made.pk), "sha256": made.sha256, "why": message},
    )
    return ImportState.FAILED


def _apply(made: BundleImport, bundle: reading.Bundle, tmp: Path) -> str:
    with transaction.atomic(), quietly():
        locked = BundleImport.objects.select_for_update().get(pk=made.pk)
        locked.project = made.project
        locked.applied_by = made.applied_by
        for type_name in sorted({t for p in bundle.manifest["plugins"] for t in p["types"]}):
            entity_type = registry.types().get(type_name)
            if entity_type is not None and not entity_type.abstract:
                identity_lock(entity_type, shared=False)
        ImportEntity.objects.filter(bundle_import=locked).delete()
        ImportPerson.objects.filter(bundle_import=locked).delete()
        confirmed = locked.plan
        plan = _Planner(locked, bundle).plan()
        locked.plan = plan
        if not plan["applicable"] or plan["counts"] != confirmed.get("counts"):
            # What was confirmed isn't what it would do now.
            locked.state = ImportState.PLANNED
            locked.planned_at = timezone.now()
            locked.error = CHANGED
            locked.save()
            return locked.state
        written = _Writer(locked, bundle, tmp).write()
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
            data=data,
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
    people: dict[str, User] = field(default_factory=dict)
    agents: dict[str, Agent] = field(default_factory=dict)
    counts: Counter[str] = field(default_factory=Counter)

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
        for line in self.bundle.lines("entities.ndjson"):
            row = planned.get(line["id"])
            if row is None:
                continue
            if row.action == EntityPlan.NEW:
                self._create(line, row)
            else:
                self._append(line, row)
        for run in self.bundle.lines("runs.ndjson"):
            self._run(run)
        return {
            "created": self.counts["created"],
            "updated": self.counts["updated"],
            "revisions": self.counts["revisions"],
            "files": self.counts["files"],
            "placeholders": self.counts["placeholders"],
            "runs": self.counts["runs"],
        }

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
            agent = Agent.objects.filter(key=line["key"]).first()
            if agent is None:
                agent = Agent.objects.create(
                    key=line["key"],
                    client_id=line["client_id"],
                    client_name=line["client_name"],
                    grant=line["grant"],
                    session=line["session"],
                    software=line["software"],
                    model=line["model"],
                    first_seen_at=line["first_seen_at"],
                )
            self.agents[line["id"]] = agent

    def _user(self, source_id: str | None) -> User | None:
        return self.people.get(source_id) if source_id else None

    def _agent(self, source_id: str | None) -> Agent | None:
        return self.agents.get(source_id) if source_id else None

    # Files ------------------------------------------------------------------------------

    def _files(self) -> None:
        lines = list(self.bundle.lines("files.ndjson"))
        here = set(
            File.objects.filter(pk__in=[f["id"] for f in lines]).values_list("pk", flat=True)
        )
        for f in lines:
            if uuid.UUID(f["id"]) in here:
                continue
            File.objects.create(
                id=f["id"],
                blob=self._blob(f),
                name=f["name"],
                media_type=f["media_type"],
                project=self.made.project,
                created_by=self._user(f["created_by"]),
                agent=self._agent(f["agent"]),
                created_at=f["created_at"],
            )
            self.counts["files"] += 1

    def _blob(self, f: dict[str, Any]) -> Blob:
        found = Blob.objects.filter(sha256=f["sha256"]).first()
        if found is not None:
            return found
        registered = f["registered"]
        if registered:
            return Blob.objects.create(
                sha256=f["sha256"],
                size=f["size"],
                storage_uri=registered["uri"],
                object_version=registered["version"],
                registered_external=True,
            )
        key = storage.blob_key(f["sha256"])
        if not storage.exists(key):
            # Its bytes, checked against their SHA-256 when the bundle was read.
            local = self.tmp / f["sha256"]
            with local.open("wb") as out:
                for chunk in self.bundle.blob(f["sha256"]):
                    out.write(chunk)
            storage.upload_file(local, key, "application/octet-stream")
            local.unlink()
        return Blob.objects.create(sha256=f["sha256"], size=f["size"], storage_uri=storage.uri(key))

    # Entities ---------------------------------------------------------------------------

    def _create(self, line: dict[str, Any], row: ImportEntity) -> None:
        form = shape(line["type"])
        values = _typed(form, _current(line))
        revisions = sorted(line["revisions"], key=lambda r: int(r["number"]))
        instance = form.model(
            owner_project=self.made.project, created_by=self._user(line["created_by"])
        )
        # Its root table's ID: each table below takes it from there.
        instance.id = uuid.UUID(line["id"])
        instance.hrid = row.hrid_here
        instance.created_at = line["created_at"]
        instance.archived_at = line["archived_at"]
        instance.revision = len(revisions)
        instance.updated_at = revisions[-1]["created_at"]
        self._set_values(form, instance, values)
        instance.save()
        if row.hrid_here:
            _advance(form.entity_type.hrid_prefix, instance.hrid)
        else:
            row.hrid_here = instance.hrid
            row.save(update_fields=["hrid_here"])
        self._extensions_and_lists(form, instance.pk, values, fresh=True)
        self._history(form, instance.pk, line, revisions, after=0)
        ProjectLink.objects.create(
            entity_id=instance.pk, project=self.made.project, linked_by=self.made.applied_by
        )
        self.counts["created"] += 1

    def _append(self, line: dict[str, Any], row: ImportEntity) -> None:
        form = shape(line["type"])
        values = _typed(form, _current(line))
        revisions = sorted(line["revisions"], key=lambda r: int(r["number"]))
        instance = form.model._default_manager.select_for_update().get(pk=line["id"])
        had = instance.revision
        self._set_values(form, instance, values)
        instance.save()
        self._extensions_and_lists(form, instance.pk, values, fresh=False)
        self._history(form, instance.pk, line, revisions, after=had)
        Entity.objects.filter(pk=instance.pk).update(
            revision=len(revisions),
            updated_at=revisions[-1]["created_at"],
            archived_at=line["archived_at"],
            identity_hash=identity.hash_for(instance, form.entity_type),
        )
        self.counts["updated"] += 1

    @staticmethod
    def _set_values(form: Shape, instance: Any, values: dict[str, Any]) -> None:  # noqa: ANN401
        for part in form.parts:
            if part.extension:
                continue
            for name, f in part.fields.items():
                if name in values and not _is_list(f) and not f.removed:
                    _set(instance, f, values[name])

    @staticmethod
    def _extensions_and_lists(
        form: Shape, entity_id: uuid.UUID, values: dict[str, Any], *, fresh: bool
    ) -> None:
        for part in form.parts:
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
            part.history._default_manager.create(
                entity_id=entity_id, revision=number, **h["values"]
            )
            _use_files(
                fields,
                entity_id,
                {k: v for k, v in h["values"].items() if k in fields},
                number,
            )

    # Runs -------------------------------------------------------------------------------

    def _run(self, line: dict[str, Any]) -> None:
        """A run, as the record of what made or changed the entities: its attempts, logs and
        output, what it read and made, and what it was tied to there that didn't come."""
        if ActionRun.objects.filter(pk=line["id"]).exists():
            return
        hrid = line["hrid"]
        if ActionRun.objects.filter(hrid=hrid).exists():
            hrid = run_services._next_hrid()
        else:
            _advance(HRID_PREFIX, hrid)
        ids = [i["entity"] for i in line["inputs"]] + [r["parent"] for r in line["lineage"]]
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
            project=self.made.project,
            requested_by=self._requester(line),
            via=line["via"],
            client=line["client"],
            agent=self._agent(line["agent"]),
            reason=line["reason"],
            tier=line["tier"],
            environment=line["environment"],
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
                # What it read that isn't here: kept as it was there.
                "inputs": [i for i in line["inputs"] if i["entity"] not in here],
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
                owner=self._user(a["owner"]),
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
        output = line["output"]
        if output is not None:
            RunOutput.objects.create(
                run=run,
                job_id=output["job"],
                document=output["document"],
                received_at=output["received_at"],
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
        ExternalOrigin.objects.bulk_create(
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
                created_at=o["created_at"],
            )
            for o in line["origins"]
        )
        self.counts["runs"] += 1

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


def _advance(prefix: str | None, hrid: str) -> None:
    """Makes sure the type's HRID sequence never hands out this number: an entity
    imported with its own HRID took it."""
    head, _, number = hrid.rpartition("-")
    if prefix is None or head != prefix or not number.isdigit():
        return
    name = hrids.sequence_name(prefix)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT setval(%s::regclass, %s) WHERE %s > COALESCE((SELECT last_value FROM "
            "pg_sequences WHERE schemaname = current_schema() AND sequencename = %s), 0)",
            [name, int(number), int(number), name],
        )
