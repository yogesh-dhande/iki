"""Writes a bundle: entities from one project, with their history, files and authors (ADR 0096).

The archive is ``.tar.gz``. Its NDJSON members and blobs come first, then ``bundle.json``,
which lists every other member's SHA-256, and ``bundle.json.sig``, this deployment's
Ed25519 signature of ``bundle.json``'s bytes.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import tarfile
import tempfile
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from django.conf import settings

from iki_server.accounts.models import Kind, User
from iki_server.audit import log as audit
from iki_server.audit.models import Agent
from iki_server.bundles import keys
from iki_server.entities import access, registry
from iki_server.entities import services as entity_services
from iki_server.entities.models import Entity, Revision
from iki_server.entities.values import Part, Shape, shape
from iki_server.files import storage
from iki_server.files.models import File
from iki_server.org.models import Project
from iki_server.org.permissions import Action, can
from iki_server.runs.models import (
    FINISHED,
    ActionRun,
    EntityMaking,
    ExternalOrigin,
    Job,
    JobLog,
    JobState,
    Lineage,
    MadeBy,
    RunEffect,
    RunInclusion,
    RunInput,
    RunOutput,
)
from iki_spec import releases
from iki_spec.model import Field

#: How much of a blob is read at a time.
CHUNK = 1024 * 1024
#: A finished run's attempts have all ended; a run still with one of these doesn't go.
_UNFINISHED_JOBS = frozenset(
    {
        JobState.AWAITING_APPROVAL,
        JobState.QUEUED,
        JobState.LEASED,
        JobState.STAGING,
        JobState.RUNNING,
        JobState.UPLOADING,
        JobState.COMMITTING,
    }
)


class ExportError(Exception):
    """Why a bundle can't be made, said to the person exporting it."""


@dataclass
class Exported:
    """A bundle that was written: its ID, its archive's SHA-256 and size, and what it holds."""

    id: uuid.UUID
    sha256: str
    size: int
    counts: dict[str, int]


@dataclass
class _Found:
    """What the picked entities, and what they refer to, come to."""

    entities: dict[uuid.UUID, Entity] = field(default_factory=dict)
    #: The registry entries of the records going (ADR 0115): their values go, not their
    #: history, which is the registry's, not the project's.
    entries: dict[uuid.UUID, Entity] = field(default_factory=dict)
    #: Each entity's history rows, by table, oldest first.
    history: dict[uuid.UUID, list[tuple[Part, dict[str, Any]]]] = field(default_factory=dict)
    files: set[uuid.UUID] = field(default_factory=set)
    #: Entities referred to that aren't going, and why.
    outside: dict[uuid.UUID, str] = field(default_factory=dict)
    #: The project's finished runs that made or changed them.
    runs: dict[uuid.UUID, ActionRun] = field(default_factory=dict)
    #: What only those runs refer to: their inputs, and their lineage's parents.
    run_refs: set[uuid.UUID] = field(default_factory=set)
    #: What the entities' current values refer to.
    value_refs: set[uuid.UUID] = field(default_factory=set)


def write(
    project: Project,
    picked: Iterable[Entity],
    out: Path,
    *,
    by: User,
    reason: str,
    selection: str,
    agent: Agent | None = None,
    export_id: uuid.UUID | None = None,
) -> Exported:
    """Writes the bundle of ``picked`` (all owned by ``project``) and what they refer to in
    it to ``out``, and records it in the audit log."""
    reason = reason.strip()
    if not reason:
        raise ExportError("Say why you're exporting these: the bundle and the audit log keep it.")
    if not settings.IKI_BUNDLE_SIGNING_KEY:
        raise ExportError(
            "This deployment has no bundle signing key, so it can't export: set "
            "IKI_BUNDLE_SIGNING_KEY (make one with `iki-server bundle_key`)."
        )
    if not can(by, Action.PROJECT_MANAGE, project):
        raise ExportError(f"Exporting from {project.name} needs its Admin role.")
    found = _find(project, list(picked), by)
    if not found.entities:
        raise ExportError("There's nothing to export: pick at least one entity.")
    signing = keys.private_key(settings.IKI_BUNDLE_SIGNING_KEY)
    bundle_id = uuid.uuid4()
    with tempfile.TemporaryDirectory(prefix="iki-bundle-") as tmp:
        work = Path(tmp)
        members: list[dict[str, Any]] = []
        revisions = list(
            Revision.objects.filter(entity_id__in=found.entities).order_by("entity_id", "number")
        )
        files = list(File.objects.filter(id__in=found.files).select_related("blob"))
        runs = _run_lines(found)
        people = (
            {by.pk}
            | _run_people(runs)
            | {
                u
                for u in [
                    *(e.created_by_id for e in found.entities.values()),
                    *(r.user_id for r in revisions),
                    *(f.created_by_id for f in files),
                ]
                if u is not None
            }
        )
        agents = {
            a for a in [*(r.agent_id for r in revisions), *(f.agent_id for f in files)] if a
        } | _run_agents(runs)
        by_entity: dict[uuid.UUID, list[Revision]] = {}
        for r in revisions:
            by_entity.setdefault(r.entity_id, []).append(r)
        lines = {
            "people.ndjson": (
                _person(u) for u in User.objects.filter(pk__in=people).order_by("pk")
            ),
            "agents.ndjson": (
                _agent(a) for a in Agent.objects.filter(pk__in=agents).order_by("pk")
            ),
            "files.ndjson": (_file(f) for f in sorted(files, key=lambda f: f.pk)),
            "entries.ndjson": (
                _entry(e) for e in sorted(found.entries.values(), key=lambda e: e.pk)
            ),
            "entities.ndjson": (
                _entity(e, by_entity.get(e.pk, []), found.history[e.pk])
                for e in sorted(found.entities.values(), key=lambda e: e.pk)
            ),
            "outside.ndjson": (
                _outside(i, why, by) for i, why in sorted(found.outside.items(), key=str)
            ),
            "runs.ndjson": iter(runs),
        }
        paths = {}
        for name, rows in lines.items():
            paths[name] = work / name
            members.append(_write_lines(paths[name], name, rows))
        blobs = sorted(
            {f.blob for f in files if not f.blob.registered_external}, key=lambda b: b.sha256
        )
        manifest: dict[str, Any] = {
            "apiVersion": "iki/v1",
            "kind": "Bundle",
            "id": str(bundle_id),
            "created_at": dt.datetime.now(dt.UTC).isoformat(),
            "source": {
                "deployment": settings.IKI_ENVIRONMENT,
                "url": settings.IKI_PUBLIC_URL,
                "iki": releases.iki(),
                "key": keys.public_text(signing),
            },
            "exported_by": _person(by),
            "reason": reason,
            "project": {
                "id": str(project.pk),
                "team": project.team.name,
                "name": project.name,
                "slug": project.slug,
            },
            "selection": selection[:2000],
            "plugins": _plugins(found),
            "counts": {
                "entities": len(found.entities),
                "entries": len(found.entries),
                "revisions": len(revisions),
                "files": len(files),
                "outside": len(found.outside),
                "runs": len(runs),
            },
            "members": [
                *members,
                *(
                    {"path": _blob_path(b.sha256), "sha256": b.sha256, "size": b.size}
                    for b in blobs
                ),
            ],
        }
        document = json.dumps(manifest, indent=2, ensure_ascii=False).encode() + b"\n"
        try:
            with tarfile.open(out, "w:gz") as tar:
                for name, path in paths.items():
                    with path.open("rb") as stream:
                        tar.addfile(_info(name, path.stat().st_size), stream)
                for blob in blobs:
                    _add_blob(tar, blob)
                tar.addfile(_info("bundle.json", len(document)), io.BytesIO(document))
                signature = keys.sign(signing, document)
                tar.addfile(_info("bundle.json.sig", len(signature)), io.BytesIO(signature))
        except BaseException:
            out.unlink(missing_ok=True)
            raise
    sha, size = _digest(out)
    exported = Exported(bundle_id, sha, size, manifest["counts"])
    audit.record(
        "bundle.exported",
        actor=by,
        agent=agent,
        project=project,
        data={
            **({"export": str(export_id)} if export_id else {}),
            "bundle": str(bundle_id),
            "sha256": sha,
            "size": size,
            "selection": manifest["selection"],
            "reason": reason,
            **manifest["counts"],
        },
    )
    return exported


# --- What goes ----------------------------------------------------------------------------------


def _find(project: Project, picked: list[Entity], by: User) -> _Found:
    found = _Found()
    for entity in picked:
        if entity.owner_project_id != project.pk:
            raise ExportError(
                f"{entity.hrid} belongs to another project, so it can't go from this one."
            )
        if entity.purged_at is not None:
            raise ExportError(f"{entity.hrid}'s plugin was purged: only its tombstone is left.")
    queue = [e.pk for e in picked]
    found.value_refs |= set(queue)
    referred: set[uuid.UUID] = set()
    while queue or (queue := _runs(project, found)):
        batch = [
            i
            for i in dict.fromkeys(queue)
            if i not in found.entities and i not in found.outside and i not in found.entries
        ]
        queue = []
        for entity in Entity.objects.filter(pk__in=batch).select_related("entry"):
            if entity.purged_at is not None:
                found.outside[entity.pk] = "purged"
                continue
            if entity.registry_id is not None:
                # A reference to a registry entry: the project's own record of it goes,
                # and the entry's values with it.
                record = Entity.objects.filter(entry=entity, owner_project=project).first()
                if record is None:
                    found.outside[entity.pk] = "another-project"
                    continue
                found.entries[entity.pk] = entity
                queue.extend(_entry_refs(entity, found))
                queue.append(record.pk)
                continue
            if entity.owner_project_id != project.pk:
                found.outside[entity.pk] = "another-project"
                continue
            found.entities[entity.pk] = entity
            if entity.entry is not None and entity.entry.pk not in found.entries:
                found.entries[entity.entry.pk] = entity.entry
                queue.extend(_entry_refs(entity.entry, found))
            rows = _history(shape(entity.type), entity.pk)
            found.history[entity.pk] = rows
            latest: dict[str, dict[str, Any]] = {}
            for part, values in rows:
                latest[part.name] = values
                for f, value in _of(part, values):
                    targets = _uuids(value)
                    if f.type == "file" or (f.items is not None and f.items.type == "file"):
                        found.files |= targets
                    else:
                        referred |= targets
            current = {
                target
                for part in shape(entity.type).parts
                if part.name in latest
                for f, value in _of(part, latest[part.name])
                if f.type == "ref" or (f.items is not None and f.items.type == "ref")
                if not f.removed
                for target in _uuids(value)
            }
            queue.extend(current)
            found.value_refs |= current
        missing = set(batch) - set(found.entities) - set(found.outside) - set(found.entries)
        for gone in missing:
            found.outside[gone] = "unreadable"
    for target in referred - set(found.entities) - set(found.outside) - set(found.entries):
        found.outside[target] = "history"
    for target, why in list(found.outside.items()):
        if target in found.run_refs and target not in found.value_refs and why != "history":
            # Only a run read it: the run's record keeps it, and importing doesn't need it.
            found.outside[target] = "run"
    return found


def _entry_values(entry: Entity) -> dict[str, Any]:
    form = shape(entry.type)
    fields = form.entry_fields()
    values = entity_services.current_values(entity_services.specific(entry))
    return {n: v for n, v in values.items() if n in fields and not fields[n].removed}


def _entry_refs(entry: Entity, found: _Found) -> list[uuid.UUID]:
    """What an entry's values refer to (its files go; its references are entries too,
    whose records go if the project has its own), noted as current values."""
    fields = shape(entry.type).entry_fields()
    refs: list[uuid.UUID] = []
    for name, value in _entry_values(entry).items():
        f = fields[name]
        kind = f.items.type if f.type == "list" and f.items is not None else f.type
        if kind == "file":
            found.files |= _uuids(value)
        elif kind == "ref":
            refs.extend(_uuids(value))
    found.value_refs |= set(refs)
    return refs


def _runs(project: Project, found: _Found) -> list[uuid.UUID]:
    """Takes in the project's finished runs that made or changed what's going, and gives
    the entities they read that haven't been looked at yet."""
    ids = set(
        RunEffect.objects.filter(entity_id__in=list(found.entities))
        .exclude(run_id__in=list(found.runs))
        .values_list("run_id", flat=True)
    )
    if not ids:
        return []
    runs = list(
        ActionRun.objects.filter(pk__in=ids, project=project, status__in=list(FINISHED))
        .exclude(jobs__state__in=list(_UNFINISHED_JOBS))
        .distinct()
    )
    if not runs:
        return []
    for run in runs:
        found.runs[run.pk] = run
    read = (
        set(
            RunInput.objects.filter(run_id__in=[r.pk for r in runs]).values_list(
                "entity_id", flat=True
            )
        )
        | set(
            RunInclusion.objects.filter(run_id__in=[r.pk for r in runs]).values_list(
                "entity_id", flat=True
            )
        )
        | set(
            Lineage.objects.filter(
                run_id__in=[r.pk for r in runs], entity_id__in=list(found.entities)
            ).values_list("parent_id", flat=True)
        )
    )
    found.run_refs |= read
    origin_files = ExternalOrigin.objects.filter(
        run_id__in=[r.pk for r in runs], entity_id__in=list(found.entities), file__isnull=False
    ).values_list("file_id", flat=True)
    found.files |= set(origin_files)
    return [i for i in read if i not in found.entities and i not in found.outside]


def _history(form: Shape, entity: uuid.UUID) -> list[tuple[Part, dict[str, Any]]]:
    rows = [
        (part, row)
        for part in form.parts
        for row in part.history._default_manager.filter(entity_id=entity).values()
    ]
    return sorted(rows, key=lambda r: r[1]["revision"])


def _of(part: Part, row: dict[str, Any]) -> Iterator[tuple[Field, Any]]:
    """Each ref or file field of a history row, with its value."""
    for name, f in part.fields.items():
        if f.type in ("ref", "file") or (f.items is not None and f.items.type in ("ref", "file")):
            yield f, row.get(name)


def _uuids(value: Any) -> set[uuid.UUID]:  # noqa: ANN401 - a column's value
    if value is None:
        return set()
    if isinstance(value, list):
        return {v if isinstance(v, uuid.UUID) else uuid.UUID(str(v)) for v in value if v}
    return {value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))}


def _plugins(found: _Found) -> list[dict[str, Any]]:
    """Each plugin with types or extensions in the bundle, as installed."""
    types: dict[str, dict[str, Any]] = {}
    for entity in found.entities.values():
        entity_type = registry.types()[entity.type]
        for t in [entity_type, *registry.ancestors(entity_type)]:
            types[t.qualified_name] = {
                "schema_version": t.schema_version,
                "units": _units(t.fields),
            }
    extensions: dict[str, dict[str, Any]] = {}
    for e in registry.extensions():
        if e.extends_type in types:
            extensions.setdefault(e.plugin, {})[e.extends_type] = {"units": _units(e.fields)}
    entries = {e["name"]: e for e in (registry.installed() or {}).get("plugins", ())}
    out = []
    for name, plugin in registry.plugins().items():
        mine = {k: v for k, v in types.items() if k.split(".", 1)[0] == name}
        if mine or name in extensions:
            sha = entries.get(name, {}).get("sha") or ""
            out.append(
                {
                    "name": name,
                    "version": plugin.version,
                    "sha": "" if sha == registry.LOCAL else sha,
                    "types": dict(sorted(mine.items())),
                    "extensions": dict(sorted(extensions.get(name, {}).items())),
                }
            )
    return out


def _units(fields: dict[str, Field]) -> dict[str, str]:
    return {n: f.unit for n, f in fields.items() if f.type == "quantity" and f.unit}


# --- Lines --------------------------------------------------------------------------------------


def _json(value: Any) -> Any:  # noqa: ANN401 - a column's value
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, list):
        return [_json(v) for v in value]
    return value


def _person(user: User) -> dict[str, Any]:
    return {
        "id": str(user.pk),
        "kind": "service" if user.kind == Kind.SERVICE else "person",
        "email": user.email,
        "name": user.name,
    }


def _agent(agent: Agent) -> dict[str, Any]:
    return {
        "id": str(agent.pk),
        "key": agent.key,
        "client_id": agent.client_id,
        "client_name": agent.client_name,
        "grant": _json(agent.grant),
        "session": agent.session,
        "software": agent.software,
        "model": agent.model,
        "first_seen_at": agent.first_seen_at.isoformat(),
    }


def _file(f: File) -> dict[str, Any]:
    blob = f.blob
    return {
        "id": str(f.pk),
        "name": f.name,
        "media_type": f.media_type,
        "created_at": f.created_at.isoformat(),
        "created_by": _json(f.created_by_id),
        "agent": _json(f.agent_id),
        "sha256": blob.sha256,
        "size": blob.size,
        "registered": (
            {"uri": blob.storage_uri, "version": blob.object_version}
            if blob.registered_external
            else None
        ),
    }


def _entity(
    entity: Entity, revisions: list[Revision], rows: list[tuple[Part, dict[str, Any]]]
) -> dict[str, Any]:
    return {
        "id": str(entity.pk),
        "type": entity.type,
        "hrid": entity.hrid,
        "created_at": entity.created_at.isoformat(),
        "created_by": _json(entity.created_by_id),
        "archived_at": _json(entity.archived_at),
        **({"entry": str(entity.entry_id)} if entity.entry_id is not None else {}),
        "revisions": [
            {
                "id": str(r.pk),
                "number": r.number,
                "kind": r.kind,
                "changed_fields": list(r.changed_fields),
                "user": _json(r.user_id),
                "via": r.via,
                "client": r.client,
                "agent": _json(r.agent_id),
                "reason": r.reason,
                "created_at": r.created_at.isoformat(),
            }
            for r in revisions
        ],
        "history": [
            {
                "table": part.name,
                "revision": row["revision"],
                "values": {name: _json(row.get(name)) for name in part.fields},
            }
            for part, row in rows
        ],
    }


def _entry(entry: Entity) -> dict[str, Any]:
    """A registry entry the bundle's records are of: its identity, derived values and facts,
    which an import registers, or finds registered, in its project's registry."""
    return {
        "id": str(entry.pk),
        "type": entry.type,
        "hrid": entry.hrid,
        "values": {n: _json(v) for n, v in sorted(_entry_values(entry).items())},
    }


def _run_lines(found: _Found) -> list[dict[str, Any]]:
    """Each run, with what it read, its attempts and their logs, and what it did to what's
    going, oldest first."""
    ids = sorted(found.runs)
    if not ids:
        return []
    going = set(found.entities)
    runs = (
        ActionRun.objects.filter(pk__in=ids)
        .select_related("batch", "workflow", "reused_from", "reproduces", "approval")
        .order_by("created_at", "id")
    )
    inputs: dict[uuid.UUID, list[dict[str, Any]]] = {}
    for i in RunInput.objects.filter(run_id__in=ids).order_by("run_id", "name", "position"):
        inputs.setdefault(i.run_id, []).append(
            {
                "name": i.name,
                "position": i.position,
                "entity": str(i.entity_id),
                "revision": i.revision,
            }
        )
    # What their inputs' references named (ADR 0105): only runs that read any have them.
    included: dict[uuid.UUID, list[dict[str, Any]]] = {}
    for c in RunInclusion.objects.filter(run_id__in=ids).order_by(
        "run_id", "input", "position", "field", "index"
    ):
        included.setdefault(c.run_id, []).append(
            {
                "input": c.input,
                "position": c.position,
                "field": c.field,
                "index": c.index,
                "entity": str(c.entity_id),
                "revision": c.revision,
            }
        )
    effects: dict[uuid.UUID, list[dict[str, Any]]] = {}
    for e in RunEffect.objects.filter(run_id__in=ids, entity_id__in=going).order_by("id"):
        effects.setdefault(e.run_id, []).append(
            {"entity": str(e.entity_id), "kind": e.kind, "revision": e.revision}
        )
    lineage: dict[uuid.UUID, list[dict[str, Any]]] = {}
    for row in Lineage.objects.filter(run_id__in=ids, entity_id__in=going).order_by("id"):
        lineage.setdefault(row.run_id, []).append(
            {
                "entity": str(row.entity_id),
                "parent": str(row.parent_id),
                "parent_revision": row.parent_revision,
                "input": row.input,
            }
        )
    origins: dict[uuid.UUID, list[dict[str, Any]]] = {}
    for o in ExternalOrigin.objects.filter(run_id__in=ids, entity_id__in=going).order_by("id"):
        origins.setdefault(o.run_id, []).append(
            {
                "entity": str(o.entity_id),
                "revision": o.revision,
                "source": o.source,
                "identifier": o.identifier,
                "version": o.version,
                "url": o.url,
                "retrieved_at": _json(o.retrieved_at),
                "sha256": o.sha256,
                "file": _json(o.file_id),
                "made": o.made,
                "created_at": o.created_at.isoformat(),
            }
        )
    made_with: dict[uuid.UUID, list[dict[str, Any]]] = {}
    for m in EntityMaking.objects.filter(
        run_id__in=ids, entity_id__in=going, by=MadeBy.IKI
    ).order_by("id"):
        made_with.setdefault(m.run_id, []).append(
            {
                "entity": str(m.entity_id),
                "software": m.software,
                "version": m.version,
                "created_at": m.created_at.isoformat(),
            }
        )
    attempts: dict[uuid.UUID, list[dict[str, Any]]] = {}
    for job in (
        Job.objects.filter(run_id__in=ids).select_related("worker").order_by("attempt", "id")
    ):
        log = "".join(
            JobLog.objects.filter(job=job).order_by("offset").values_list("text", flat=True)
        )
        attempts.setdefault(job.run_id, []).append(
            {
                "id": str(job.pk),
                "attempt": job.attempt,
                "state": job.state,
                "tier": job.tier,
                "environment": job.environment,
                "environment_build": job.environment_build,
                "resources": job.resources,
                "worker": job.worker.name if job.worker is not None else "",
                # Whose own computer ran it, before self-hosted workers (ADR 0112).
                "owner": None,
                "volumes": job.volumes,
                "queued_at": job.queued_at.isoformat(),
                "started_at": _json(job.started_at),
                "finished_at": _json(job.finished_at),
                "error": job.error,
                "log": log,
            }
        )
    outputs = {o.run_id: o for o in RunOutput.objects.filter(run_id__in=ids)}
    return [
        _run(
            run,
            inputs=inputs.get(run.pk, []),
            **({"included": included[run.pk]} if run.pk in included else {}),
            effects=effects.get(run.pk, []),
            lineage=lineage.get(run.pk, []),
            origins=origins.get(run.pk, []),
            **({"made_with": made_with[run.pk]} if run.pk in made_with else {}),
            attempts=attempts.get(run.pk, []),
            output=outputs.get(run.pk),
        )
        for run in runs
    ]


def _run(
    run: ActionRun, *, output: RunOutput | None, **parts: list[dict[str, Any]]
) -> dict[str, Any]:
    approval = run.approval
    workflow = run.workflow
    return {
        "id": str(run.pk),
        "hrid": run.hrid,
        "action": run.action,
        "plugin_version": run.plugin_version,
        "plugin_sha": run.plugin_sha,
        "spec": run.spec,
        "requested_by": _json(run.requested_by_id),
        "via": run.via,
        "client": run.client,
        "agent": _json(run.agent_id),
        "reason": run.reason,
        "tier": run.tier,
        "environment": run.environment,
        "params": run.params,
        "plugin_settings": run.plugin_settings,
        "seed": run.seed,
        "status": run.status,
        "created_at": run.created_at.isoformat(),
        "started_at": _json(run.started_at),
        "finished_at": _json(run.finished_at),
        "error": run.error,
        "timeout": run.timeout,
        "max_attempts": run.max_attempts,
        "retry_on": run.retry_on,
        "reproduction": run.reproduction,
        "linked": {
            "batch": _ref(run.batch),
            "workflow": (
                {"id": str(workflow.pk), "hrid": workflow.hrid, "step": run.workflow_step}
                if workflow is not None
                else None
            ),
            "reused_from": _ref(run.reused_from),
            "reproduces": _ref(run.reproduces),
        },
        "approval": (
            {
                "state": approval.state,
                "requested_by": _json(approval.requested_by_id),
                "agent": _json(approval.agent_id),
                "client": approval.client,
                "created_at": approval.created_at.isoformat(),
                "decided_by": _json(approval.decided_by_id),
                "decided_at": _json(approval.decided_at),
                "note": approval.note,
            }
            if approval is not None
            else None
        ),
        **parts,
        "output": (
            {
                "job": str(output.job_id),
                "document": output.document,
                "received_at": output.received_at.isoformat(),
            }
            if output is not None
            else None
        ),
    }


def _ref(row: Any) -> dict[str, str] | None:  # noqa: ANN401 - a batch or a run
    return {"id": str(row.pk), "hrid": row.hrid} if row is not None else None


def _run_people(runs: list[dict[str, Any]]) -> set[uuid.UUID]:
    found = {r["requested_by"] for r in runs} | {
        p
        for r in runs
        if r["approval"]
        for p in (r["approval"]["requested_by"], r["approval"]["decided_by"])
    }
    return {uuid.UUID(p) for p in found if p}


def _run_agents(runs: list[dict[str, Any]]) -> set[uuid.UUID]:
    found = {r["agent"] for r in runs} | {r["approval"]["agent"] for r in runs if r["approval"]}
    return {uuid.UUID(a) for a in found if a}


def _outside(entity_id: uuid.UUID, why: str, by: User) -> dict[str, Any]:
    entity = access.visible(by).filter(pk=entity_id).only("hrid", "type").first()
    if entity is None and why == "another-project":
        why = "unreadable"
    return {
        "id": str(entity_id),
        "hrid": entity.hrid if entity else None,
        "type": entity.type if entity else None,
        "why": why,
    }


# --- The archive --------------------------------------------------------------------------------


def _write_lines(path: Path, name: str, rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    sha = hashlib.sha256()
    size = 0
    with path.open("wb") as stream:
        for row in rows:
            line = json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
            sha.update(line)
            size += len(line)
            stream.write(line)
    return {"path": name, "sha256": sha.hexdigest(), "size": size}


def _blob_path(sha256: str) -> str:
    return f"blobs/sha256/{sha256}"


def _info(name: str, size: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = size
    info.mode = 0o644
    info.mtime = 0
    return info


class _Checked(io.RawIOBase):
    """A blob's bytes as storage gives them, hashed as they're read."""

    def __init__(self, body: IO[bytes]) -> None:
        self.body = body
        self.sha = hashlib.sha256()

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        chunk = self.body.read(size if size >= 0 else CHUNK)
        self.sha.update(chunk)
        return chunk


def _add_blob(tar: tarfile.TarFile, blob: Any) -> None:  # noqa: ANN401 - a Blob
    body = storage.open_object(blob.storage_uri, blob.object_version)
    checked = _Checked(body)
    tar.addfile(_info(_blob_path(blob.sha256), blob.size), checked)
    if checked.sha.hexdigest() != blob.sha256:
        raise ExportError(
            f"The bytes stored for {blob.sha256[:12]} don't match its SHA-256, so it can't go: "
            "tell your administrator."
        )


def _digest(path: Path) -> tuple[str, int]:
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK), b""):
            sha.update(chunk)
    return sha.hexdigest(), path.stat().st_size
