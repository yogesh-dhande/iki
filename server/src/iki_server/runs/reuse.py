"""Reusing an identical run's results, and reproducing a run exactly (ADR 0057).

A deterministic action's run gets a :func:`key`: a hash of everything its results
depend on. That's its plugin's commit, its params (a file param by its content), its
plugin's settings, and each input's entity at the revision it reads, with that type's
schema version. A run launched with the key of an earlier run that succeeded, which the
launcher can read, reuses its results (:func:`find`, :func:`reuse`). It ends at once,
links the entities that run made into its own project, and runs nothing.

A reproduction runs a run again with all of that the same, and its seed too. It writes
nothing: when it ends, :func:`compare` says whether its results are the same.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from django.utils import timezone

from iki_server.audit import log as audit
from iki_server.entities import access, registry
from iki_server.entities.models import Entity, ProjectLink
from iki_server.files.models import File
from iki_server.runs import live
from iki_server.runs.models import (
    ActionRun,
    EffectKind,
    Job,
    JobState,
    RunEffect,
    RunInput,
    RunOutput,
    RunStatus,
    RunUpload,
)

if TYPE_CHECKING:
    from iki_server.accounts.models import User
    from iki_server.org.models import Project
    from iki_server.runs.models import RunBatch, WorkflowRun
    from iki_spec.model import Action

__all__ = ["MAX_EFFECTS", "blocked", "compare", "find", "key", "reproducible", "reuse"]

#: The most entities a run may have made for another to reuse its results: linking more
#: belongs in a run of its own (a bulk import is run again; its duplicates are reused).
MAX_EFFECTS = 10_000
#: The differences a reproduction lists; it counts the rest.
MAX_DIFFERENCES = 20
#: How many identical runs :func:`find` looks at, newest first.
_CANDIDATES = 5
#: What a reproduction compares: its results, not what it said about them.
_COMPARED = ("creates", "creates_file", "updates", "metrics")


def key(
    action: Action,
    sha: str,
    entities: Mapping[str, Sequence[Entity]],
    params: Mapping[str, Any],
    settings: Mapping[str, Any],
) -> str:
    """The run's reuse key, or ``""`` when its results can't be reused: its action isn't
    deterministic, or its plugin is local code, which may have changed since."""
    if not action.deterministic or not sha:
        return ""
    # A file param counts by its content: the same file uploaded twice is the same.
    files = {
        name
        for name, value in params.items()
        if value is not None and name in action.params and action.params[name].type == "file"
    }
    contents = {
        str(pk): sha
        for pk, sha in File.objects.filter(pk__in=[params[n] for n in files]).values_list(
            "pk", "blob__sha256"
        )
    }
    types = registry.types()
    document = {
        "action": action.qualified_name,
        "plugin_sha": sha,
        "params": {
            name: (
                f"sha256:{contents[str(value)]}"
                if name in files and str(value) in contents
                else value
            )
            for name, value in sorted(params.items())
        },
        "settings": dict(settings),
        "inputs": {
            name: [[str(e.pk), e.revision, e.type, types[e.type].schema_version] for e in chosen]
            for name, chosen in sorted(entities.items())
        },
    }
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def find(user: User, reuse_key: str, project: Project) -> ActionRun | None:
    """The newest run with this key that succeeded, ran (didn't reuse another), that `user`
    can read, and whose entities are all still there to reuse in `project`: none archived,
    none whose project keeps it to itself (ADR 0067), and no more than
    :data:`MAX_EFFECTS` of them."""
    if not reuse_key:
        return None
    candidates = (
        ActionRun.objects.filter(
            reuse_key=reuse_key,
            status=RunStatus.SUCCEEDED,
            reused_from__isnull=True,
            reproduces__isnull=True,
            project__in=access.readable_projects(user).values("pk"),
        )
        .exclude(reuse_key="")
        .order_by("-finished_at")[:_CANDIDATES]
    )
    for candidate in candidates:
        made = RunEffect.objects.filter(
            run=candidate, kind__in=[EffectKind.CREATED, EffectKind.REUSED]
        )
        if RunEffect.objects.filter(run=candidate)[: MAX_EFFECTS + 1].count() > MAX_EFFECTS:
            continue
        if made.filter(entity__archived_at__isnull=False).exists():
            continue
        if made.exclude(entity__in=access.shareable_into(user, project).values("pk")).exists():
            continue
        return candidate
    return None


def reuse(
    user: User,
    action: Action,
    project: Project,
    entities: Mapping[str, Sequence[Entity]],
    params: dict[str, Any],
    settings: dict[str, Any],
    reason: str,
    original: ActionRun,
    *,
    hrid: str,
    batch: RunBatch | None = None,
    step: tuple[WorkflowRun, str] | None = None,
) -> ActionRun:
    """A run that ends at once with `original`'s results: the entities it made (or reused)
    are linked into `project` and listed as this run's, reused. Call inside a transaction,
    with its inputs and params checked."""
    now = timezone.now()
    run = ActionRun.objects.create(
        status=RunStatus.SUCCEEDED,
        client=audit.current_oauth_client(),
        agent=audit.current_agent(),
        hrid=hrid,
        batch=batch,
        workflow=step[0] if step else None,
        workflow_step=step[1] if step else "",
        action=action.qualified_name,
        plugin_version=original.plugin_version,
        plugin_sha=original.plugin_sha,
        spec=original.spec,
        project=project,
        requested_by=user,
        via=audit.current_via(),
        reason=reason,
        tier=original.tier,
        environment=original.environment,
        params=params,
        plugin_settings=settings,
        seed=original.seed,
        reuse_key=original.reuse_key,
        reused_from=original,
        timeout=original.timeout,
        max_attempts=original.max_attempts,
        retry_on=original.retry_on,
        started_at=now,
        finished_at=now,
    )
    RunInput.objects.bulk_create(
        RunInput(run=run, name=name, position=index, entity=entity, revision=entity.revision)
        for name, chosen in entities.items()
        for index, entity in enumerate(chosen)
    )
    made = list(
        RunEffect.objects.filter(
            run=original, kind__in=[EffectKind.CREATED, EffectKind.REUSED]
        ).order_by("id")
    )
    RunEffect.objects.bulk_create(
        RunEffect(run=run, entity_id=e.entity_id, kind=EffectKind.REUSED) for e in made
    )
    linked = set(
        ProjectLink.objects.filter(
            project=project, entity_id__in=[e.entity_id for e in made]
        ).values_list("entity_id", flat=True)
    )
    new = [e.entity_id for e in made if e.entity_id not in linked]
    ProjectLink.objects.bulk_create(
        ProjectLink(entity_id=entity_id, project=project, linked_by=user) for entity_id in new
    )
    for entity in Entity.objects.filter(pk__in=new).order_by("id"):
        audit.record("entity.linked", actor=user, target=entity, project=project)
    audit.record(
        "run.launched",
        actor=user,
        target=run,
        project=project,
        data={
            "action": action.qualified_name,
            "tier": run.tier,
            "inputs": sum(len(e) for e in entities.values()),
            "reused_from": original.hrid,
            **({"batch": batch.hrid} if batch else {}),
            **({"workflow": step[0].hrid, "step": step[1]} if step else {}),
        },
    )
    live.changed(run.pk, run.batch_id)
    return run


def compare(job: Job, run: ActionRun, document: dict[str, Any]) -> dict[str, Any]:
    """How a reproduction's output compares with the run it reproduces: ``matches``, and
    where they differ (a JSON pointer, and each value), files by their contents. Its
    environment's build counts too: the same commit should build the same one."""
    original = run.reproduces
    assert original is not None  # noqa: S101 - only called for reproductions
    before = RunOutput.objects.filter(run=original).values_list("document", flat=True).first()
    contents = _contents([original.pk, run.pk])
    differences: list[dict[str, Any]] = []
    counted = [0]
    _diff(
        _normal({k: (before or {}).get(k) for k in _COMPARED}, contents),
        _normal({k: document.get(k) for k in _COMPARED}, contents),
        "",
        differences,
        counted,
    )
    built = (
        Job.objects.filter(run=original, state=JobState.SUCCEEDED)
        .order_by("-attempt")
        .values_list("environment_build", flat=True)
        .first()
    ) or ""
    builds = {"original": built, "reproduced": job.environment_build}
    same_build = not built or not job.environment_build or built == job.environment_build
    return {
        "matches": counted[0] == 0 and same_build,
        "differences": differences,
        "more_differences": max(0, counted[0] - len(differences)),
        "environment_build": builds,
    }


def _contents(runs: list[Any]) -> dict[str, str]:
    """The files these runs' commands uploaded, by ID: their SHA-256."""
    return {
        str(file_id): sha
        for file_id, sha in RunUpload.objects.filter(
            run__in=runs, upload__file__isnull=False
        ).values_list("upload__file_id", "upload__file__blob__sha256")
    }


def _normal(value: Any, contents: dict[str, str]) -> Any:  # noqa: ANN401 - JSON
    """`value` with each file a command uploaded named by its content, not its ID."""
    if isinstance(value, dict):
        return {k: _normal(v, contents) for k, v in value.items()}
    if isinstance(value, list):
        return [_normal(v, contents) for v in value]
    if isinstance(value, str) and value in contents:
        return f"sha256:{contents[value]}"
    return value


def _diff(
    before: Any,  # noqa: ANN401 - JSON
    after: Any,  # noqa: ANN401 - JSON
    at: str,
    out: list[dict[str, Any]],
    counted: list[int],
) -> None:
    if isinstance(before, dict) and isinstance(after, dict):
        for name in sorted(before.keys() | after.keys(), key=str):
            _diff(before.get(name), after.get(name), f"{at}/{_escape(name)}", out, counted)
        return
    if isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        for index, (b, a) in enumerate(zip(before, after, strict=True)):
            _diff(b, a, f"{at}/{index}", out, counted)
        return
    if before == after and type(before) is type(after):
        return
    counted[0] += 1
    if len(out) < MAX_DIFFERENCES:
        out.append({"pointer": f"#{at}", "original": _short(before), "reproduced": _short(after)})


def _escape(name: Any) -> str:  # noqa: ANN401 - a JSON key
    return str(name).replace("~", "~0").replace("/", "~1")


def _short(value: Any) -> Any:  # noqa: ANN401 - JSON
    """A value as a difference shows it: whole, unless it's long."""
    if isinstance(value, (dict, list)):
        text = json.dumps(value, sort_keys=True)
        return value if len(text) <= 200 else f"{text[:199]}…"
    if isinstance(value, str) and len(value) > 200:
        return f"{value[:199]}…"
    return value


def reproducible(run: ActionRun) -> ActionRun:
    """The run a reproduction of `run` reproduces: the one that ran. A run that reused
    another's results, or reproduced it, stands for that one."""
    return run.reused_from or run.reproduces or run


def blocked(original: ActionRun, sha: str, version: str) -> str | None:
    """Why `original` can't be reproduced exactly here, if it can't: the deployment runs
    other code for its plugin, or it ended without results to compare with."""
    plugin = original.action.split(".")[0]
    if original.plugin_sha != sha or original.plugin_version != version:
        ran = original.plugin_sha[:12] or "local code"
        here = sha[:12] or "local code"
        return (
            f"{original.hrid} ran the {plugin} plugin {original.plugin_version} ({ran}); this "
            f"deployment has {version} ({here}), so it can't be reproduced exactly here."
        )
    if not RunOutput.objects.filter(run=original).exists():
        return f"{original.hrid} ended without results to compare with."
    return None
