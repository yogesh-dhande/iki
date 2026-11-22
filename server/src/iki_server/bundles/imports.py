"""Imports: a bundle from another deployment, checked and planned before anything changes.

An Org Admin who is also the project's Admin uploads a bundle into the project (its upload
is of kind ``bundle``, kept under its own key). Once its bytes are in, a task reads it,
trusting nothing until it checks out (:mod:`.reading`), and works out the plan (ADR 0096):

- every type it holds is here, active, at its schema version or later, with the same units;
- each registry entry its records are of (ADR 0115) is registered in the project's registry
  already, which its record then refers to, or is registered by the import: its facts
  must agree with the registry's, and it mustn't be superseded here;
- each entity is new, here already (skipped), here at an earlier revision from an earlier
  import (its newer revisions are added), or a conflict: in another project here, changed
  here and there since, or sharing a unique value or an identity with an entity here;
- each HRID another entity has here is renumbered from this deployment's own sequence;
- each person is matched by email, or becomes a placeholder who can't sign in.

Nothing changes until the plan is applied, whole.
"""

from __future__ import annotations

import datetime as dt
import tempfile
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from itertools import islice
from pathlib import Path
from typing import Any

import structlog
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from iki_server.accounts.models import Kind, User
from iki_server.audit import log as audit
from iki_server.bundles import reading
from iki_server.bundles.models import (
    BundleImport,
    EntityPlan,
    ImportEntity,
    ImportPerson,
    ImportState,
)
from iki_server.entities import access, identity, registry, requests, unique
from iki_server.entities import services as entity_services
from iki_server.entities.models import Entity, Revision, UniqueValue
from iki_server.entities.services import frozen
from iki_server.entities.values import Shape, shape
from iki_server.files import services as files
from iki_server.files import storage
from iki_server.files.models import Blob, File, Upload, UploadKind
from iki_server.org.models import Project, registry_of
from iki_server.org.permissions import Action, can
from iki_server.org.services import Refused
from iki_server.runs.models import ActionRun
from iki_server.tasks import task
from iki_spec import titles
from iki_spec.model import EntityType, Field

logger = structlog.get_logger("iki.bundles")

MEDIA_TYPE = "application/gzip"
#: Entities planned at a time.
BATCH = 500
#: How many references outside the bundle the plan lists; it counts them all.
OUTSIDE_SHOWN = 200


def start(by: User, project: Project, *, name: str, size: int, sha256: str) -> BundleImport:
    """Starts uploading a bundle to import into ``project``. Its parts go as any upload's."""
    if not can(by, Action.PROJECT_IMPORT, project):
        raise Refused(
            f"Importing into {project.name} needs an Org Admin who is also its Admin: an import "
            "can add people."
        )
    if size <= 0:
        raise Refused("An empty file isn't a bundle.", field="size")
    with transaction.atomic():
        upload = files.start(
            by,
            project,
            name=name,
            media_type=MEDIA_TYPE,
            size=size,
            sha256=sha256,
            kind=UploadKind.BUNDLE,
        )
        return BundleImport.objects.create(
            project=project,
            requested_by=by,
            agent=audit.current_agent(),
            via=audit.current_via(),
            upload=upload,
            sha256=upload.sha256,
            size=size,
        )


def uploaded(upload: Upload) -> None:
    """Its bytes are in and match: plan it. Called in the transaction that marks it ready."""
    made = BundleImport.objects.select_for_update().get(upload=upload)
    if made.state != ImportState.UPLOADING:
        return
    made.state = ImportState.PLANNING
    made.save(update_fields=["state"])
    plan_import.enqueue(str(made.pk))


def state_of(made: BundleImport) -> tuple[str, str]:
    """Its state and error, with an upload that failed, was cancelled or expired as failed."""
    if made.state == ImportState.UPLOADING and made.upload.state in ("failed", "cancelled"):
        return ImportState.FAILED, made.upload.error or "Its upload was cancelled."
    if made.state == ImportState.UPLOADING and made.upload.state == "expired":
        return ImportState.FAILED, "Its upload wasn't finished in time. Upload it again."
    return made.state, made.error


def discard(by: User, made: BundleImport) -> BundleImport:
    """Drops an import that hasn't been applied, and deletes its archive."""
    with transaction.atomic():
        # Its upload too: whether the archive is deleted now depends on its state.
        locked = (
            BundleImport.objects.select_for_update(of=("self", "upload"))
            .select_related("upload")
            .get(pk=made.pk)
        )
        if locked.state in (ImportState.APPLYING, ImportState.APPLIED):
            raise Refused("It was applied: what it imported stays, as the record of it.")
        if locked.state == ImportState.DISCARDED:
            return locked
        if locked.upload.state in ("pending", "checking"):
            files.cancel(locked.upload)
        locked.state = ImportState.DISCARDED
        locked.save(update_fields=["state"])
        ImportEntity.objects.filter(bundle_import=locked).delete()
        ImportPerson.objects.filter(bundle_import=locked).delete()
        audit.record(
            "bundle.import_discarded",
            actor=by,
            target=locked,
            project=locked.project,
            data={"import": str(locked.pk), "sha256": locked.sha256},
        )
        transaction.on_commit(
            lambda: storage.delete_for_good(storage.imported_key(locked.upload_id))
        )
    return locked


# --- Planning -------------------------------------------------------------------------------


@task()
def plan_import(import_id: str) -> str:
    """Reads the uploaded bundle and works out what importing it does."""
    made = BundleImport.objects.select_related("project", "requested_by").get(pk=import_id)
    if made.state != ImportState.PLANNING:
        return made.state
    try:
        return _plan(made)
    except Exception:
        # Not one it can explain: it says so, rather than seem to go on planning. The
        # task's record and the logs keep what went wrong.
        logger.exception("bundle_plan_crashed", import_id=import_id)
        BundleImport.objects.filter(pk=made.pk).update(
            state=ImportState.FAILED,
            error="Something went wrong on Iki's side while checking it. Discard it and upload "
            "it again; if it happens again, your administrator can find what went wrong in "
            "Iki's logs.",
        )
        raise


def _plan(made: BundleImport) -> str:
    with tempfile.TemporaryDirectory(prefix="iki-import-") as tmp:
        path = Path(tmp) / "bundle.tar.gz"
        storage.download_file(storage.imported_key(made.upload_id), path)
        try:
            bundle = reading.read(
                path, settings.IKI_BUNDLE_TRUSTED_KEYS, limit=settings.IKI_BUNDLE_MAX_BYTES
            )
        except reading.BundleError as error:
            return _refuse(made, str(error))
        made.bundle_id = uuid.UUID(bundle.manifest["id"])
        made.source = _source(bundle.manifest)
        if bundle.trusted_as is None:
            return _refuse(
                made,
                f"It's signed with {bundle.source}'s key {bundle.manifest['source']['key']}, "
                f"which this deployment doesn't trust. To import from {bundle.source}, an "
                "administrator adds that key to the deployment's trusted bundle keys "
                "(bundles.trusted).",
            )
        made.trusted_as = bundle.trusted_as
        try:
            with transaction.atomic():
                ImportEntity.objects.filter(bundle_import=made).delete()
                ImportPerson.objects.filter(bundle_import=made).delete()
                made.plan = _Planner(made, bundle).plan()
                made.state = ImportState.PLANNED
                made.planned_at = timezone.now()
                made.save()
                audit.record(
                    "bundle.import_planned",
                    actor=made.requested_by,
                    agent=made.agent,
                    target=made,
                    project=made.project,
                    data={
                        "import": str(made.pk),
                        "bundle": str(made.bundle_id),
                        "sha256": made.sha256,
                        "source": made.trusted_as,
                        **made.plan["counts"],
                    },
                )
        except reading.BundleError as error:
            return _refuse(made, str(error))
    return made.state


def _refuse(made: BundleImport, message: str) -> str:
    made.state = ImportState.REFUSED
    made.error = message
    made.save()
    audit.record(
        "bundle.import_refused",
        actor=made.requested_by,
        agent=made.agent,
        target=made,
        project=made.project,
        data={"import": str(made.pk), "sha256": made.sha256, "why": message},
    )
    return made.state


def _source(manifest: dict[str, Any]) -> dict[str, Any]:
    source = manifest["source"]
    return {
        "deployment": source["deployment"],
        "url": source.get("url", ""),
        "iki": source["iki"],
        "exported_at": manifest["created_at"],
        "exported_by": {
            "name": manifest["exported_by"]["name"],
            "email": manifest["exported_by"]["email"],
        },
        "reason": manifest["reason"],
        "project": manifest["project"],
        "selection": manifest["selection"],
    }


@dataclass
class _Counts:
    new: int = 0
    update: int = 0
    skip: int = 0
    conflict: int = 0
    renumbered: int = 0
    revisions: int = 0
    runs: int = 0
    runs_here: int = 0
    runs_renumbered: int = 0
    registered: int = 0
    entries_here: int = 0


@dataclass
class _Entry:
    """A bundle's registry entry, here: the entry it is, or the ID and HRID it's registered
    with (blank: a new one, its own being another's here), and why it can't be."""

    here: Entity | None
    pk: uuid.UUID
    hrid: str
    why: str = ""


@dataclass
class _Planner:
    made: BundleImport
    bundle: reading.Bundle
    problems: list[str] = field(default_factory=list)
    counts: _Counts = field(default_factory=_Counts)
    #: Each file's SHA-256, by its ID, for identities that name a file.
    file_sha: dict[str, str] = field(default_factory=dict)
    ids: set[uuid.UUID] = field(default_factory=set)
    #: The bundle's registry entries, by their ID there, and what each is here.
    entries: dict[str, dict[str, Any]] = field(default_factory=dict)
    resolved: dict[str, _Entry] = field(default_factory=dict)

    def plan(self) -> dict[str, Any]:
        types = self._types()
        people = self._people()
        stored = self._files()
        self._entries()
        for batch in _batched(self.bundle.lines("entities.ndjson"), BATCH):
            self.ids |= {uuid.UUID(e["id"]) for e in batch}
            self._entities(batch)
        outside = self._outside()
        self._runs()
        c = self.counts
        if c.conflict:
            self.problems.append(
                f"{c.conflict:,} entit{'y is' if c.conflict == 1 else 'ies are'} in conflict: "
                "see each one's reason. Resolve them where the bundle is from, or here, then "
                "export and import again."
            )
        return {
            "counts": {
                "entities": c.new + c.update + c.skip + c.conflict,
                "new": c.new,
                "update": c.update,
                "skip": c.skip,
                "conflict": c.conflict,
                "renumbered": c.renumbered,
                "registered": c.registered,
                "entries_here": c.entries_here,
                "revisions": c.revisions,
                "people": people["people"],
                "placeholders": people["placeholders"],
                "files": stored["files"],
                "runs": c.runs,
                "runs_here": c.runs_here,
                "runs_renumbered": c.runs_renumbered,
            },
            "types": types,
            "files": stored,
            "outside": outside,
            "problems": self.problems,
            "applicable": not self.problems,
        }

    # Runs ---------------------------------------------------------------------------------

    def _runs(self) -> None:
        """Runs are records: one here already stays as it is, and a new one gets a new HRID
        when its own is another run's here. The plugin that ran it needn't be here."""
        for batch in _batched(self.bundle.lines("runs.ndjson"), BATCH):
            ids = [r["id"] for r in batch]
            here = set(ActionRun.objects.filter(pk__in=ids).values_list("pk", flat=True))
            taken = set(
                ActionRun.objects.filter(hrid__in=[r["hrid"] for r in batch])
                .exclude(pk__in=ids)
                .values_list("hrid", flat=True)
            )
            for run in batch:
                if uuid.UUID(run["id"]) in here:
                    self.counts.runs_here += 1
                    continue
                self.counts.runs += 1
                if run["hrid"] in taken:
                    self.counts.runs_renumbered += 1

    # Types --------------------------------------------------------------------------------

    def _types(self) -> list[dict[str, Any]]:
        out = []
        here = registry.types()
        for plugin in self.bundle.manifest["plugins"]:
            name, version = plugin["name"], plugin["version"]
            deploy = f"deploy {name} {version}, or a later release, first"
            for type_name, wanted in plugin["types"].items():
                found = here.get(type_name)
                row: dict[str, Any] = {
                    "type": type_name,
                    "plugin": name,
                    "schema_version": wanted["schema_version"],
                    "here": found.schema_version if found else None,
                }
                out.append(row)
                if found is None:
                    self.problems.append(f"This deployment has no {type_name}: {deploy}.")
                    continue
                if found.schema_version < wanted["schema_version"]:
                    self.problems.append(
                        f"{type_name} is at schema version {found.schema_version} here, and "
                        f"{wanted['schema_version']} in the bundle: {deploy}."
                    )
                elif found.schema_version > wanted["schema_version"]:
                    # Its values would need this deployment's plugin migrations, which
                    # importing doesn't run.
                    self.problems.append(
                        f"{type_name} is at schema version {found.schema_version} here, and "
                        f"{wanted['schema_version']} in the bundle: deploy the same {name} "
                        "release where the bundle is from, then export it again."
                    )
                if why := frozen(shape(type_name)):
                    self.problems.append(why)
                self._units(type_name, wanted["units"], found.fields)
            for type_name, wanted in plugin["extensions"].items():
                extension = next(
                    (
                        e
                        for e in registry.extensions()
                        if e.plugin == name and e.extends_type == type_name
                    ),
                    None,
                )
                if extension is None:
                    self.problems.append(
                        f"The bundle has the fields {name} adds to {type_name}, and this "
                        f"deployment doesn't: {deploy}."
                    )
                    continue
                self._units(type_name, wanted["units"], extension.fields)
        return out

    def _units(self, type_name: str, wanted: dict[str, str], fields: dict[str, Field]) -> None:
        for name, unit in wanted.items():
            f = fields.get(name)
            if f is not None and f.unit and f.unit != unit:
                self.problems.append(
                    f"{type_name}'s {name} is in {unit} in the bundle and {f.unit} here: "
                    "the values would be read wrong."
                )

    # People -------------------------------------------------------------------------------

    def _people(self) -> dict[str, int]:
        lines = list(self.bundle.lines("people.ndjson"))
        found = {
            (u.email.lower(), u.kind): u
            for u in User.objects.filter(email__in=[p["email"] for p in lines])
        }
        rows = []
        for p in lines:
            kind = Kind.SERVICE if p["kind"] == "service" else Kind.PERSON
            rows.append(
                ImportPerson(
                    bundle_import=self.made,
                    source_id=p["id"],
                    kind=p["kind"],
                    name=p["name"][:200],
                    email=p["email"],
                    user=found.get((p["email"].lower(), kind)),
                )
            )
        ImportPerson.objects.bulk_create(rows, batch_size=BATCH)
        return {"people": len(rows), "placeholders": sum(1 for r in rows if r.user is None)}

    # Files --------------------------------------------------------------------------------

    def _files(self) -> dict[str, int]:
        lines = list(self.bundle.lines("files.ndjson"))
        carried = {
            m["path"].rsplit("/", 1)[1]
            for m in self.bundle.manifest["members"]
            if m["path"].startswith("blobs/")
        }
        shas = {f["sha256"] for f in lines}
        here = set(Blob.objects.filter(sha256__in=shas).values_list("sha256", flat=True))
        existing = dict(
            File.objects.filter(pk__in=[f["id"] for f in lines]).values_list("pk", "blob__sha256")
        )
        existing_uris = set(
            Blob.objects.filter(sha256__in=shas).values_list("storage_uri", flat=True)
        )
        registered = 0
        allowed = files.locations()
        for f in lines:
            self.file_sha[f["id"]] = f["sha256"]
            if f["registered"]:
                registered += 1
                uri = f["registered"]["uri"]
                if uri not in existing_uris and not any(uri.startswith(a) for a in allowed):
                    self.problems.append(
                        f"{f['name']} is registered at {uri}, where this deployment doesn't "
                        "register files from: an administrator adds the location first."
                    )
            elif f["sha256"] not in carried and f["sha256"] not in here:
                raise reading.BundleError(f"It names {f['name']}, but doesn't carry its bytes.")
            sha = existing.get(uuid.UUID(f["id"]))
            if sha is not None and sha != f["sha256"]:
                self.problems.append(
                    f"{f['name']} is here already with other bytes: a file never changes, so "
                    "one of the two deployments has it wrong."
                )
        return {
            "files": len(lines),
            "here": sum(1 for f in lines if uuid.UUID(f["id"]) in existing),
            "carried": len(carried),
            "registered": registered,
        }

    # Registry entries ---------------------------------------------------------------------

    def _entries(self) -> None:
        """Finds each entry's identity in the project's registry, as registering it would."""
        registry_id = registry_of(self.made.project)
        lines = list(self.bundle.lines("entries.ndjson"))
        self.entries = {e["id"]: e for e in lines}
        digests: dict[str, str] = {}
        for line in lines:
            entity_type = registry.types().get(line["type"])
            if entity_type is None or not shape(line["type"]).has_entries:
                continue  # Its type's problem is the plan's already, or it isn't one.
            form = shape(line["type"])
            try:
                digest = identity.hash_of(
                    _typed(form, line["values"]),
                    entity_type,
                    lambda f: self.file_sha.get(str(f), ""),
                )
            except (ValueError, TypeError):
                digest = None
            if digest is not None:
                digests[line["id"]] = digest
        here = {
            e.identity_hash: e
            for e in Entity.objects.filter(
                registry_id=registry_id, identity_hash__in=list(digests.values())
            ).select_related("superseded_by")
        }
        taken_ids = {
            str(pk)
            for pk in Entity.objects.filter(pk__in=list(self.entries)).values_list("pk", flat=True)
        }
        taken_hrids = set(
            Entity.objects.filter(
                hrid__in=[e["hrid"] for e in lines], entry__isnull=True
            ).values_list("hrid", flat=True)
        )
        for line in lines:
            digest = digests.get(line["id"])
            if digest is None:
                self.resolved[line["id"]] = _Entry(
                    None,
                    uuid.uuid4(),
                    "",
                    f"{line['hrid']}'s identity can't be read here: is its type's plugin here?",
                )
                continue
            found = here.get(digest)
            if found is not None:
                self.counts.entries_here += 1
                self.resolved[line["id"]] = _Entry(
                    found, found.pk, found.hrid, self._against_entry(line, found)
                )
                continue
            self.counts.registered += 1
            pk = uuid.UUID(line["id"]) if line["id"] not in taken_ids else uuid.uuid4()
            hrid = line["hrid"] if line["hrid"] not in taken_hrids else ""
            if not hrid:
                self.counts.renumbered += 1
            form = shape(line["type"])
            why = next(iter(_entry_clashes(registry_id, form, _typed(form, line["values"]))), "")
            self.resolved[line["id"]] = _Entry(None, pk, hrid, why)

    @staticmethod
    def _against_entry(line: dict[str, Any], here: Entity) -> str:
        """Why the bundle's entry can't be the one here: another type, superseded, or a
        fact with another value, which only its registrars change."""
        if here.type != line["type"]:
            return f"It's registered here already as a {here.type}, {here.hrid}."
        if here.superseded_by is not None:
            return (
                f"It's registered here as {here.hrid}, which was superseded by "
                f"{here.superseded_by.hrid}."
            )
        form = shape(here.type)
        fields = form.entry_fields()
        current = entity_services.current_values(entity_services.specific(here))
        theirs = _typed(form, line["values"])
        for name, value in theirs.items():
            f = fields.get(name)
            if f is None or f.registry != "fact" or value is None or value == []:
                continue
            mine = current.get(name)
            if mine is not None and mine != [] and mine != value:
                return f"Its {f.label or name} isn't {here.hrid}'s here: a registry fact " + (
                    "that's fixed: no one changes it."
                    if f.immutable
                    else "only its registrars change."
                )
        return ""

    # Entities -----------------------------------------------------------------------------

    def _entities(self, batch: list[dict[str, Any]]) -> None:
        ids = [uuid.UUID(e["id"]) for e in batch]
        existing = {e.pk: e for e in Entity.objects.filter(pk__in=ids)}
        history: dict[uuid.UUID, list[uuid.UUID]] = {}
        for entity_id, revision_id in (
            Revision.objects.filter(entity_id__in=existing)
            .order_by("entity_id", "number")
            .values_list("entity_id", "id")
        ):
            history.setdefault(entity_id, []).append(revision_id)
        taken = set(
            Entity.objects.filter(hrid__in=[e["hrid"] for e in batch])
            .exclude(pk__in=ids)
            .values_list("hrid", flat=True)
        )
        held = dict(
            Entity.objects.filter(
                owner_project=self.made.project,
                entry_id__in=[r.pk for r in self.resolved.values() if r.here is not None],
            ).values_list("entry_id", "hrid")
        )
        rows = [self._entity(line, existing, history, taken, held) for line in batch]
        ImportEntity.objects.bulk_create(rows, batch_size=BATCH)

    def _entity(
        self,
        line: dict[str, Any],
        existing: dict[uuid.UUID, Entity],
        history: dict[uuid.UUID, list[uuid.UUID]],
        taken: set[str],
        held: dict[uuid.UUID, str],
    ) -> ImportEntity:
        entity_id = uuid.UUID(line["id"])
        revisions = [uuid.UUID(r["id"]) for r in sorted(line["revisions"], key=_number)]
        values = _current(line)
        entry = self.resolved.get(line.get("entry") or "")
        if line.get("entry") and line["entry"] in self.entries:
            values = {**self.entries[line["entry"]]["values"], **values}
        row = ImportEntity(
            bundle_import=self.made,
            entity_id=entity_id,
            type=line["type"],
            hrid=line["hrid"],
            hrid_here=line["hrid"],
            title=_title(line, values),
            revisions=len(revisions),
        )
        here = existing.get(entity_id)
        if line.get("entry") and entry is None:
            raise reading.BundleError(
                f"{line['hrid']} is a record of a registry entry the bundle doesn't have."
            )
        if entry is not None:
            row.entry_id, row.registers = entry.pk, entry.here is None
            row.hrid_here = entry.hrid
        if here is not None:
            row.hrid_here = here.hrid
            action, adds, why = self._against(here, history.get(entity_id, []), revisions)
            if entry is not None and here.entry_id != entry.pk and action != EntityPlan.CONFLICT:
                action, adds, why = (
                    EntityPlan.CONFLICT,
                    0,
                    f"It's here already as {here.hrid}, a record of another registry entry.",
                )
            row.action, row.adds, row.why = action, adds, why
        elif entry is not None:
            row.action, row.adds = EntityPlan.NEW, len(revisions)
            if entry.why:
                row.action, row.adds, row.why = EntityPlan.CONFLICT, 0, entry.why
            elif entry.here is not None and entry.pk in held:
                row.action, row.adds, row.why = (
                    EntityPlan.CONFLICT,
                    0,
                    f"{self.made.project.name} has {held[entry.pk]} already, a record of the "
                    "same registry entry: a project has one record of each.",
                )
            elif entry.here is not None:
                row.why = f"Its registry entry here is {entry.hrid}."
            elif not entry.hrid:
                row.why = (
                    f"{line['hrid']} is another entity's HRID here: it's registered with a new one."
                )
        else:
            row.action, row.adds = EntityPlan.NEW, len(revisions)
            if line["hrid"] in taken:
                row.hrid_here = ""
                row.why = f"{line['hrid']} is another entity's HRID here."
                self.counts.renumbered += 1
        entity_type = registry.types().get(line["type"])
        if row.action in (EntityPlan.NEW, EntityPlan.UPDATE) and entity_type is not None:
            clash = self._clash(entity_id, entity_type, values)
            if clash:
                row.action, row.adds, row.why = EntityPlan.CONFLICT, 0, clash
        setattr(self.counts, str(row.action), getattr(self.counts, str(row.action)) + 1)
        self.counts.revisions += row.adds
        return row

    def _against(
        self, here: Entity, mine: list[uuid.UUID], theirs: list[uuid.UUID]
    ) -> tuple[str, int, str]:
        if here.purged_at is not None:
            return EntityPlan.CONFLICT, 0, f"{here.hrid}'s plugin was purged here."
        if here.owner_project_id != self.made.project_id:
            return (
                EntityPlan.CONFLICT,
                0,
                f"It's here already as {here.hrid}, in another project: an entity belongs to "
                "one project.",
            )
        if mine == theirs:
            return EntityPlan.SKIP, 0, "Here already, as it is."
        if theirs[: len(mine)] == mine:
            if here.released_review_revision is not None:
                return (
                    EntityPlan.CONFLICT,
                    0,
                    f"{here.hrid} has been released and cannot be changed.",
                )
            sent = sent_request(here)
            if sent:
                return EntityPlan.CONFLICT, 0, sent
            added = len(theirs) - len(mine)
            return EntityPlan.UPDATE, added, ""
        if mine[: len(theirs)] == theirs:
            later = len(mine) - len(theirs)
            return (
                EntityPlan.SKIP,
                0,
                f"Here already, with {later:,} later revision{'' if later == 1 else 's'} of "
                "this deployment's own.",
            )
        return (
            EntityPlan.CONFLICT,
            0,
            f"{here.hrid} changed both here and where the bundle is from since it was last "
            "imported: Iki doesn't merge two histories.",
        )

    def _clash(self, entity_id: uuid.UUID, entity_type: EntityType, values: dict[str, Any]) -> str:
        """A unique value of the record's own another record of the project has. (Its
        entry's are the registry's, checked with the entry.)"""
        form = shape(entity_type.qualified_name)
        for message in _unique_clashes(self.made.project, form, values, entity_id, self.ids):
            return message
        return ""

    # Outside ------------------------------------------------------------------------------

    def _outside(self) -> dict[str, Any]:
        lines = list(self.bundle.lines("outside.ndjson"))
        ids = [o["id"] for o in lines]
        # A reference to a registry entry holds it only for a project with a record of it.
        here = set(
            Entity.objects.filter(pk__in=ids, registry__isnull=True).values_list("pk", flat=True)
        ) | set(
            access.held_entries(self.made.project).filter(pk__in=ids).values_list("pk", flat=True)
        )
        items = [{**o, "here": uuid.UUID(o["id"]) in here} for o in lines]
        # Values refer to them now (history's references needn't resolve): they must be here.
        missing = [i for i in items if not i["here"] and i["why"] not in ("history", "run")]
        if missing:
            named = ", ".join(i["hrid"] or i["id"] for i in missing[:5])
            more = f" and {len(missing) - 5:,} more" if len(missing) > 5 else ""
            self.problems.append(
                f"Its entities refer to {named}{more}, which aren't in it or here: import "
                "them first, or export what refers to them with them."
            )
        return {
            "count": len(items),
            "here": sum(1 for i in items if i["here"]),
            "items": items[:OUTSIDE_SHOWN],
        }


def sent_request(here: Entity) -> str:
    """Why an import can't add revisions to a record that's a request sent from here to a
    service (ADR 0132), or "" if it isn't one. A bundle's revisions are the whole record's,
    the service's fields and what it sends among them, and the service works it here: each
    side changes only its own fields, as itself. The database's guard refuses the service's
    columns too, but this says why before anything's applied."""
    if requests.spec_of(here.type) is None:
        return ""
    request = requests.of(here)
    if request is None:
        return ""
    return (
        f"{here.hrid} is a request sent to {request.service.name}, which works it here: an "
        "import doesn't change a request once it's sent, as each side changes only its own "
        "fields. Change it here instead."
    )


def _number(revision: dict[str, Any]) -> int:
    return int(revision["number"])


def _batched(lines: Iterable[dict[str, Any]], size: int) -> Iterator[list[dict[str, Any]]]:
    it = iter(lines)
    while batch := list(islice(it, size)):
        yield batch


def _current(line: dict[str, Any]) -> dict[str, Any]:
    """The entity's values at its latest revision: each table's latest row, merged."""
    latest: dict[str, dict[str, Any]] = {}
    for row in sorted(line["history"], key=lambda r: int(r["revision"])):
        latest[row["table"]] = row["values"]
    out: dict[str, Any] = {}
    for values in latest.values():
        out.update(values)
    return out


def _title(line: dict[str, Any], values: dict[str, Any]) -> str:
    entity_type = registry.types().get(line["type"])
    if entity_type is None:
        return str(line["hrid"])
    try:
        return titles.render(
            registry.display_title(entity_type),
            values,
            line["hrid"],
            shape(line["type"]).fields(),
        )
    except (KeyError, TypeError, ValueError):
        return str(line["hrid"])


def _typed(form: Shape, values: dict[str, Any]) -> dict[str, Any]:
    """Values from a bundle's JSON as the database has them, for identity hashes."""
    out: dict[str, Any] = {}
    fields = form.fields()
    for name, value in values.items():
        f = fields.get(name)
        if f is None or value is None:
            out[name] = value
        elif f.is_rows and f.items is not None and isinstance(value, list):
            cells = f.items.fields
            out[name] = [
                {n: _one(cell, row.get(n)) for n, cell in cells.items()}
                for row in value
                if isinstance(row, dict)
            ]
        elif f.type == "list" and f.items is not None and isinstance(value, list):
            out[name] = [_one(f.items, v) for v in value]
        else:
            out[name] = _one(f, value)
    return out


def _one(f: Field, value: Any) -> Any:  # noqa: ANN401 - JSON in, a database value out
    if value is None:
        return None
    if f.type == "ref":
        return uuid.UUID(str(value))
    if f.type == "datetime":
        return dt.datetime.fromisoformat(str(value))
    if f.type == "date":
        return dt.date.fromisoformat(str(value))
    return value


def _unique_clashes(
    project: Project,
    form: Shape,
    values: dict[str, Any],
    entity_id: uuid.UUID,
    bundled: set[uuid.UUID],
) -> Iterator[str]:
    for part in form.parts:
        if part.entry:
            continue
        for name, f in part.fields.items():
            value = values.get(name)
            if not f.unique or f.removed or value is None:
                continue
            holder = (
                UniqueValue.objects.filter(
                    project=project, key=unique.key(part.model, name), value=str(value)
                )
                .exclude(entity_id=entity_id)
                .exclude(entity_id__in=bundled)
                .values_list("entity__hrid", flat=True)
                .first()
            )
            if holder is not None:
                yield (
                    f"Its {f.label or name} {value} is {holder}'s here already: no two entities "
                    "in a project share it."
                )


def _entry_clashes(registry_id: uuid.UUID, form: Shape, values: dict[str, Any]) -> Iterator[str]:
    """A unique registry value of a new entry's another entry has, in the project's registry:
    which, it doesn't say, as registering doesn't."""
    for part in form.parts:
        if not part.entry:
            continue
        for name, f in part.fields.items():
            value = values.get(name)
            if not f.unique or f.removed or value is None:
                continue
            if UniqueValue.objects.filter(
                registry_id=registry_id, key=unique.key(part.model, name), value=str(value)
            ).exists():
                yield (
                    f"Its {f.label or name} {value} is another registry entry's here already: "
                    "no two in a registry share it."
                )
