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
from iki_server.entities.models import Entity, Revision
from iki_server.entities.values import Part, Shape, shape
from iki_server.files import storage
from iki_server.files.models import File
from iki_server.org.models import Project
from iki_server.org.permissions import Action, can
from iki_spec import releases
from iki_spec.model import Field

#: How much of a blob is read at a time.
CHUNK = 1024 * 1024


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
    #: Each entity's history rows, by table, oldest first.
    history: dict[uuid.UUID, list[tuple[Part, dict[str, Any]]]] = field(default_factory=dict)
    files: set[uuid.UUID] = field(default_factory=set)
    #: Entities referred to that aren't going, and why.
    outside: dict[uuid.UUID, str] = field(default_factory=dict)


def write(
    project: Project,
    picked: Iterable[Entity],
    out: Path,
    *,
    by: User,
    reason: str,
    selection: str,
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
        people = {by.pk} | {
            u
            for u in [
                *(e.created_by_id for e in found.entities.values()),
                *(r.user_id for r in revisions),
                *(f.created_by_id for f in files),
            ]
            if u is not None
        }
        agents = {a for a in [*(r.agent_id for r in revisions), *(f.agent_id for f in files)] if a}
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
            "entities.ndjson": (
                _entity(e, by_entity.get(e.pk, []), found.history[e.pk])
                for e in sorted(found.entities.values(), key=lambda e: e.pk)
            ),
            "outside.ndjson": (
                _outside(i, why, by) for i, why in sorted(found.outside.items(), key=str)
            ),
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
                "revisions": len(revisions),
                "files": len(files),
                "outside": len(found.outside),
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
        project=project,
        data={
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
    referred: set[uuid.UUID] = set()
    while queue:
        batch = [
            i for i in dict.fromkeys(queue) if i not in found.entities and i not in found.outside
        ]
        queue = []
        for entity in Entity.objects.filter(pk__in=batch):
            if entity.purged_at is not None:
                found.outside[entity.pk] = "purged"
                continue
            if entity.owner_project_id != project.pk:
                found.outside[entity.pk] = "another-project"
                continue
            found.entities[entity.pk] = entity
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
        missing = set(batch) - set(found.entities) - set(found.outside)
        for gone in missing:
            found.outside[gone] = "unreadable"
    for target in referred - set(found.entities) - set(found.outside):
        found.outside[target] = "history"
    return found


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
