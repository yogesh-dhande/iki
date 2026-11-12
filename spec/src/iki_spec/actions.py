"""Actions and the environments they run in: reading their manifests, and the rules beyond them.

A plugin's actions live under `actions/` and its environments under
`environments/`. The schema checks each document's shape; the rules here
check what needs the rest of the plugin (and the plugins it depends on): that
inputs and effects name real types and fields, that each action's environment
supports its tier, and that what an environment installs is pinned.
"""

from __future__ import annotations

import re
from pathlib import Path as FilePath
from typing import TYPE_CHECKING, Any

from iki_spec import schemas
from iki_spec import validate as _validate
from iki_spec.model import (
    Action,
    Creates,
    Environment,
    Field,
    Input,
    Resources,
    Secret,
    Updates,
    Volume,
    parse_duration,
    parse_quantity,
    qualify,
)
from iki_spec.problems import Path, Severity

if TYPE_CHECKING:
    from iki_spec.yamlsource import Document

__all__ = ["MAX_TIMEOUT", "check", "read_action", "read_environment"]

#: The longest any run may take.
MAX_TIMEOUT = 7 * 86400

#: Machines Iki provisions; the client and backend tiers run on what's there.
PROVISIONED = frozenset({"container", "vm"})

#: Environment variables the worker sets, which a secret can't replace.
RESERVED_VARIABLES = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "SHELL",
        "PWD",
        "TMPDIR",
        "LANG",
        "TZ",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "PYTHONPATH",
        "PYTHONUNBUFFERED",
        "VIRTUAL_ENV",
        "CONDA_PREFIX",
        "MAMBA_ROOT_PREFIX",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        # The egress proxy, with the attempt's credentials (ADR 0053).
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
        "ALL_PROXY",
    }
)


# --- Reading -----------------------------------------------------------------------------------


def _secret(given: str | dict[str, Any]) -> Secret:
    if isinstance(given, str):
        return Secret(given)
    return Secret(
        given["name"],
        required=bool(given.get("required", True)),
        description=str(given.get("description", "")).strip(),
    )


def read_action(plugin: str, body: dict[str, Any], document: Document) -> Action:
    runs_on = body["runs_on"]
    effects = body.get("effects") or {}
    artifacts = body.get("artifacts") or {}
    retries = body.get("retries") or {}
    name = body["name"]
    return Action(
        plugin=plugin,
        name=name,
        label=body.get("label") or name.replace("_", " ").capitalize(),
        description=body.get("description"),
        inputs={
            n: Input(
                name=n,
                type=qualify(spec["type"], plugin),
                schema=spec.get("schema"),
                cardinality=spec.get("cardinality", "one"),
                max=spec.get("max"),
                optional=bool(spec.get("optional", False)),
                label=spec.get("label") or n.replace("_", " ").capitalize(),
                description=spec.get("description"),
            )
            for n, spec in (body.get("inputs") or {}).items()
        },
        params={
            n: Field.from_manifest(n, spec, plugin=plugin)
            for n, spec in (body.get("params") or {}).items()
        },
        creates=tuple(
            Creates(
                type=qualify(c["type"], plugin),
                cardinality=c.get("cardinality", "one"),
                derived_from=tuple(c.get("derived_from", ())),
            )
            for c in effects.get("creates", ())
        ),
        updates=tuple(
            Updates(input=u["input"], fields=tuple(u["fields"])) for u in effects.get("updates", ())
        ),
        tier=runs_on["tier"],
        environment=runs_on["environment"],
        lifecycle=runs_on.get("lifecycle")
        or ("ephemeral" if runs_on["tier"] in PROVISIONED else None),
        resources=Resources.from_manifest(runs_on.get("resources")),
        command=tuple(body["command"]),
        deterministic=bool(body.get("deterministic", False)),
        checkpointable=bool(body.get("checkpointable", False)),
        batchable=body.get("batchable", 1),
        artifacts=tuple(artifacts.get("keep", ())),
        artifact_retention=parse_duration(artifacts["retention"])
        if "retention" in artifacts
        else None,
        network=tuple(body.get("network", ())),
        secrets=tuple(_secret(given) for given in body.get("secrets", ())),
        timeout=parse_duration(body["timeout"]) if "timeout" in body else 3600,
        # Runs whose machine went away are retried, unless they reach the network: a retry
        # could repeat what they did elsewhere, such as placing an order.
        retry_max=retries.get("max", 0 if body.get("network") else 2),
        retry_on=tuple(retries.get("on", ("worker_lost", "interrupted"))),
        file=document.file,
    )


def read_environment(plugin: str, body: dict[str, Any], document: Document) -> Environment:
    python = body.get("python") or {}
    setup = body.get("setup") or {}
    name = body["name"]
    return Environment(
        plugin=plugin,
        name=name,
        label=body.get("label") or name.replace("-", " ").capitalize(),
        description=body.get("description"),
        tiers=tuple(body["tiers"]),
        resources=Resources.from_manifest(body.get("resources")),
        image=body.get("image"),
        python_version=python.get("version"),
        requirements=python.get("requirements"),
        conda_lockfile=(body.get("conda") or {}).get("lockfile"),
        volumes={
            n: Volume(
                name=n,
                size=parse_quantity(v["size"]),
                mount=v["mount"],
                access=v.get("access", "read_only_many"),
                performance=v.get("performance", "standard"),
                populated_by=v.get("populated_by"),
            )
            for n, v in (body.get("volumes") or {}).items()
        },
        setup_script=setup.get("script"),
        setup_timeout=parse_duration(setup["timeout"]) if "timeout" in setup else None,
        setup_requires_approval=bool(setup.get("requires_approval", False)),
        setup_network=tuple(setup.get("network", ())),
        file=document.file,
    )


# --- Rules -------------------------------------------------------------------------------------


def check(semantics: _validate._Semantics) -> None:
    """Check the plugin's environments, then its actions."""
    _Rules(semantics).run()


class _Rules:
    def __init__(self, semantics: _validate._Semantics) -> None:
        self.s = semantics
        self.add = semantics.add
        self.plugin = semantics.plugin
        self.loaded = semantics.loaded
        self.root = semantics.loaded.directory

    def run(self) -> None:
        for name, environment in self.plugin.environments.items():
            self.environment(environment, self.loaded.environment_sources[name])
        used: set[str] = set()
        for name, action in self.plugin.actions.items():
            self.action(action, self.loaded.action_sources[name])
            used.add(action.environment)
        unreadable = any(
            p.is_error and (p.file or "").startswith("actions/") for p in self.s.checker.problems
        )
        for name in set() if unreadable else self.plugin.environments.keys() - used:
            self.add(
                "unused-environment",
                f"No action of this plugin runs in `{name}`.",
                self.loaded.environment_sources[name],
                ("name",),
                severity=Severity.WARNING,
                hint="Delete it, or point an action's `runs_on.environment` at it.",
            )

    # Environments -------------------------------------------------------------------------------

    def environment(self, env: Environment, source: Document) -> None:
        tiers = set(env.tiers)
        native = env.requirements is not None or env.conda_lockfile is not None
        if tiers & PROVISIONED and env.image is None:
            self.add(
                "environment-needs-image",
                "The container and vm tiers run an image, and this environment has none.",
                source,
                ("tiers",),
                hint="Add `image:`, pinned by its digest (`registry/name@sha256:…`).",
            )
        if "backend" in tiers and not native:
            self.add(
                "environment-needs-packages",
                "The backend tier runs actions in a Python or conda environment Iki builds, and "
                "this environment declares neither.",
                source,
                ("tiers",),
                hint="Add `python: { version: '3.12', requirements: requirements.txt }`, or "
                "`conda: { lockfile: conda.lock }`.",
            )
        if "client" in tiers and not native and env.image is None:
            self.add(
                "environment-needs-packages",
                "The client tier needs a Python or conda environment to build on people's "
                "machines, or an image to run with Docker or Podman.",
                source,
                ("tiers",),
            )
        if native and not tiers & {"backend", "client"}:
            self.add(
                "unused-packages",
                "Only the backend and client tiers build Python or conda environments; the "
                "container and vm tiers use the image as it is.",
                source,
                ("python",) if env.requirements is not None else ("conda",),
                key=True,
                severity=Severity.WARNING,
                hint="Install the packages in the image instead.",
            )
        if env.image is not None and tiers == {"backend"}:
            self.add(
                "unused-image",
                "The backend tier doesn't run images, so this one is never used.",
                source,
                ("image",),
                key=True,
                severity=Severity.WARNING,
            )
        if env.resources.gpu_count and not tiers <= PROVISIONED:
            self.add(
                "gpu-wrong-tier",
                "Only the container and vm tiers have GPUs.",
                source,
                ("resources", "gpu"),
                key=True,
            )
        if env.requirements is not None:
            text = self.file(env.requirements, source, ("python", "requirements"))
            if text is not None:
                self.requirements(text, source)
        if env.conda_lockfile is not None:
            text = self.file(env.conda_lockfile, source, ("conda", "lockfile"))
            if text is not None:
                self.conda(text, source)
        self.volumes(env, source)

    def file(self, relative: str, source: Document, path: Path) -> str | None:
        """The text of a file the manifest names, or None after saying why it can't be read."""
        target = self.root / relative
        if any(p.is_symlink() for p in [target, *target.parents] if self._inside(p)):
            self.add(
                "symlink",
                f"`{relative}` is a symbolic link.",
                source,
                path,
                hint="Copy the file into the plugin instead, so what's reviewed is what's built.",
            )
            return None
        if not target.is_file():
            self.add("missing-file", f"There's no file `{relative}` in this plugin.", source, path)
            return None
        try:
            return target.read_text("utf-8")
        except (OSError, UnicodeDecodeError):
            self.add("unreadable-file", f"`{relative}` isn't a UTF-8 text file.", source, path)
            return None

    def _inside(self, path: FilePath) -> bool:
        try:
            path.relative_to(self.root)
        except ValueError:
            return False
        return path != self.root

    def requirements(self, text: str, source: Document) -> None:
        path: Path = ("python", "requirements")
        hint = (
            "Write it with `uv pip compile --generate-hashes`, or `pip-compile --generate-hashes`."
        )
        for number, line in _logical_lines(text):
            if line.startswith(("-r", "--requirement", "-c", "--constraint")):
                self.add(
                    "requirements-include",
                    f"Line {number} includes another file; list every package in this one.",
                    source,
                    path,
                    hint=hint,
                )
            elif line.startswith(("-e", "--editable")):
                self.add(
                    "requirements-unpinned",
                    f"Line {number} installs an editable package, which can't be pinned.",
                    source,
                    path,
                    hint=hint,
                )
            elif line.startswith("-"):
                continue  # an option, such as --index-url
            elif "==" not in line.split(";")[0] or "--hash=sha256:" not in line:
                self.add(
                    "requirements-unpinned",
                    f"Line {number} (`{line.split()[0]}`) isn't pinned with `==` and its "
                    "`--hash=sha256:`, so builds could install something else.",
                    source,
                    path,
                    hint=hint,
                )

    def conda(self, text: str, source: Document) -> None:
        path: Path = ("conda", "lockfile")
        lines = [ln.strip() for ln in text.splitlines()]
        if "@EXPLICIT" not in lines:
            self.add(
                "conda-not-explicit",
                "The lockfile isn't an explicit one (it has no `@EXPLICIT` line).",
                source,
                path,
                hint="Write it with `conda-lock --kind explicit`, or `conda list --explicit "
                "--md5`.",
            )
            return
        after = lines[lines.index("@EXPLICIT") + 1 :]
        for offset, line in enumerate(after):
            if not line or line.startswith("#"):
                continue
            if not re.search(r"#(sha256:)?[0-9a-f]{32,64}$", line):
                number = lines.index("@EXPLICIT") + offset + 2
                self.add(
                    "conda-unpinned",
                    f"Line {number} has no hash, so builds could install something else.",
                    source,
                    path,
                    hint="Write it with `conda list --explicit --md5`.",
                )

    def volumes(self, env: Environment, source: Document) -> None:
        if env.volumes and not set(env.tiers) <= PROVISIONED:
            self.add(
                "volumes-wrong-tier",
                "Volumes are mounted only on the container and vm tiers.",
                source,
                ("volumes",),
                key=True,
                hint="Run this environment on those tiers only, or fetch the data in the action.",
            )
        mounts: dict[str, str] = {}
        filled = False
        for name, volume in env.volumes.items():
            path: Path = ("volumes", name)
            for other, mount in mounts.items():
                if _nested(volume.mount, mount):
                    self.add(
                        "overlapping-mounts",
                        f"`{volume.mount}` overlaps where `{other}` is mounted (`{mount}`).",
                        source,
                        (*path, "mount"),
                    )
            mounts[name] = volume.mount
            if volume.populated_by == "setup":
                filled = True
                if env.setup_script is None:
                    self.add(
                        "volume-needs-setup",
                        f"`{name}` is filled by the setup, and this environment has none.",
                        source,
                        (*path, "populated_by"),
                        hint="Add `setup: { script: setup/fill.sh }`.",
                    )
        if env.setup_script is not None:
            self.file(env.setup_script, source, ("setup", "script"))
            if not filled:
                self.add(
                    "setup-fills-nothing",
                    "The setup fills volumes, and none here says `populated_by: setup`.",
                    source,
                    ("setup",),
                    key=True,
                    severity=Severity.WARNING,
                )

    # Actions ------------------------------------------------------------------------------------

    def action(self, action: Action, source: Document) -> None:
        names: dict[str, str] = {}
        for name, put in action.inputs.items():
            names[name] = "input"
            self.input(put, source, ("inputs", name))
        for name, param in action.params.items():
            path: Path = ("params", name)
            if name in names:
                self.add(
                    "duplicate-name",
                    f"`{name}` is both an input and a param.",
                    source,
                    path,
                    key=True,
                    hint="Runs read both by name, so give them different ones.",
                )
            self.param(param, source, path)
        self.effects(action, source)
        environment = self.runs_on(action, source)
        self.command(action, source, environment)
        self.reach(action, source)
        if action.timeout > MAX_TIMEOUT:
            self.add(
                "timeout-too-long",
                "Runs can take at most 7 days (`7d`).",
                source,
                ("timeout",),
                hint="Make it checkpointable and split the work, so no run holds a machine "
                "that long.",
            )
        if action.batchable > 1 and (
            len(action.inputs) != 1 or next(iter(action.inputs.values())).cardinality != "one"
        ):
            self.add(
                "batch-needs-one-input",
                "Only actions with one input, taking one entity, can be batched: each job runs "
                "it on several entities at once.",
                source,
                ("batchable",),
            )
        if action.batchable > 1 and action.artifacts:
            self.add(
                "batch-keeps-artifacts",
                "A batchable action can't keep artifacts: one job runs it on several entities, "
                "so its files wouldn't belong to any one run.",
                source,
                ("artifacts",),
                hint="Write one file per entity into its results instead, or don't batch it.",
            )
        if action.batchable > 1 and action.checkpointable:
            self.add(
                "batch-checkpoints",
                "A batchable action can't be checkpointable: one job runs it on several "
                "entities, so its progress wouldn't belong to any one run.",
                source,
                ("checkpointable",),
                hint="Batch small, quick runs; checkpoint long ones.",
            )
        self.retries(action, source)

    def input(self, put: Input, source: Document, path: Path) -> None:
        target = self.s.reference(put.type, source, (*path, "type"))
        if (
            target is not None
            and put.schema is not None
            and not _validate.satisfies(target.schema_version, put.schema)
        ):
            self.add(
                "input-schema-unsatisfied",
                f"`{put.type}` is at schema version {target.schema_version}, which "
                f"`{put.schema}` doesn't allow.",
                source,
                (*path, "schema"),
                hint="Update the action for the new version, then the constraint.",
            )
        if put.max is not None and put.cardinality != "many":
            self.add(
                "max-needs-many",
                "`max` is for inputs that take many entities.",
                source,
                (*path, "max"),
                key=True,
                hint="Add `cardinality: many`, or drop `max`.",
            )

    def param(self, param: Field, source: Document, path: Path) -> None:
        if param.required and param.has_default:
            self.add(
                "required-with-default",
                "A param with a default can't be required: runs that don't choose a value get "
                "the default.",
                source,
                (*path, "required"),
                key=True,
            )
        if _validate.looks_secret(param.name):
            self.add(
                "param-looks-secret",
                f"`{param.name}` looks like a secret. Params are kept with each run, for anyone "
                "who can see it.",
                source,
                path,
                key=True,
                severity=Severity.WARNING,
                hint="Declare it under `secrets:`; admins map it to the secret manager.",
            )
        self.s.options(param, source, path)
        if param.type == "quantity":
            self.s.quantity(param, source, path)
        if param.has_default:
            for message in _validate.value_problems(param, param.default):
                self.add("invalid-default", f"The default {message}", source, (*path, "default"))

    def effects(self, action: Action, source: Document) -> None:
        for index, creates in enumerate(action.creates):
            path: Path = ("effects", "creates", index)
            target = self.s.reference(creates.type, source, (*path, "type"))
            if target is not None and target.abstract:
                self.add(
                    "creates-abstract",
                    f"`{creates.type}` is abstract, so there are no entities of it to create.",
                    source,
                    (*path, "type"),
                    hint="Create one of its concrete subtypes.",
                )
            for n, name in enumerate(creates.derived_from):
                if name not in action.inputs:
                    self.unknown_input(action, name, source, (*path, "derived_from", n))
        seen: dict[str, int] = {}
        for index, updates in enumerate(action.updates):
            path = ("effects", "updates", index)
            if updates.input not in action.inputs:
                self.unknown_input(action, updates.input, source, (*path, "input"))
                continue
            if updates.input in seen:
                self.add(
                    "duplicate-updates",
                    f"`{updates.input}` is updated twice (in update {seen[updates.input] + 1} "
                    "too).",
                    source,
                    (*path, "input"),
                    hint="List all the fields it sets in one.",
                )
            seen[updates.input] = index
            self.updated_fields(action.inputs[updates.input], updates, source, path)
        if not action.creates and not action.updates and not action.artifacts:
            self.add(
                "action-does-nothing",
                "This action creates nothing, updates nothing and keeps no artifacts, so its "
                "runs leave nothing behind.",
                source,
                ("name",),
                hint="Declare what it writes under `effects`, or the files it keeps under "
                "`artifacts`.",
            )

    def unknown_input(self, action: Action, name: str, source: Document, path: Path) -> None:
        self.add(
            "unknown-input",
            f"This action has no input `{name}`.",
            source,
            path,
            hint=schemas._did_you_mean(name, list(action.inputs))
            or ("Its inputs: " + ", ".join(f"`{n}`" for n in action.inputs) + ".")
            if action.inputs
            else "It has no inputs.",
        )

    def updated_fields(self, put: Input, updates: Updates, source: Document, path: Path) -> None:
        target = self.s.types.get(put.type)
        if target is None:
            return  # reported on the input
        fields = {n: f for n, (f, _) in self.s.all_fields(target).items()}
        chain = {put.type, *(t.qualified_name for t in self.s.ancestors(target))}
        for extension in self.plugin.extensions:
            if extension.extends_type in chain:
                fields.update(extension.fields)
        for index, name in enumerate(updates.fields):
            f = fields.get(name)
            where: Path = (*path, "fields", index)
            if f is None or f.removed:
                self.add(
                    "unknown-field",
                    f"`{put.type}` has no field `{name}`"
                    + (" any more (it was removed)." if f is not None else "."),
                    source,
                    where,
                    hint=schemas._did_you_mean(
                        name, [n for n, x in fields.items() if not x.removed]
                    ),
                )
            elif f.immutable:
                self.add(
                    "updates-immutable",
                    f"`{name}` can't change once an entity has it.",
                    source,
                    where,
                    hint="Create a new entity instead, derived from this one.",
                )

    def runs_on(self, action: Action, source: Document) -> Environment | None:
        path: Path = ("runs_on",)
        environment = self.plugin.environments.get(action.environment)
        if environment is None:
            self.add(
                "unknown-environment",
                f"This plugin has no environment `{action.environment}`.",
                source,
                (*path, "environment"),
                hint=schemas._did_you_mean(action.environment, list(self.plugin.environments))
                or "Define it in a file under environments/.",
            )
        elif action.tier not in environment.tiers:
            self.add(
                "tier-not-supported",
                f"`{action.environment}` doesn't run on the {action.tier} tier; it runs on "
                + ", ".join(environment.tiers)
                + ".",
                source,
                (*path, "tier"),
                hint=f"Add `{action.tier}` to its `tiers`, or run this action on one of those.",
            )
        provisioned = action.tier in PROVISIONED
        if not provisioned and action.lifecycle is not None:
            self.add(
                "lifecycle-wrong-tier",
                "Only the container and vm tiers start machines, so only they have a `lifecycle`.",
                source,
                (*path, "lifecycle"),
                key=True,
            )
        wanted = action.resources
        if action.tier == "client" and wanted != Resources():
            self.add(
                "client-resources",
                "Client-tier actions run on the person's own machine, which Iki can't size.",
                source,
                (*path, "resources"),
                key=True,
            )
        elif wanted.gpu_count and not provisioned:
            self.add(
                "gpu-wrong-tier",
                "Only the container and vm tiers have GPUs.",
                source,
                (*path, "resources", "gpu"),
                key=True,
            )
        return environment

    def command(self, action: Action, source: Document, environment: Environment | None) -> None:
        for index, argument in enumerate(action.command):
            path: Path = ("command", index)
            if argument.startswith("./"):
                self.file(argument[2:], source, path)
            elif argument.startswith("/") and action.tier == "backend":
                self.add(
                    "absolute-path-on-backend",
                    f"`{argument}` is an absolute path, and backend-tier actions run without an "
                    "image.",
                    source,
                    path,
                    hint="Name a file in the plugin with `./`, such as `./scripts/run.py`.",
                )
            elif (
                argument.startswith("/")
                and action.tier == "client"
                and environment is not None
                and environment.image is None
            ):
                self.add(
                    "absolute-path-on-client",
                    f"`{argument}` is an absolute path, and `{environment.name}` has no image, "
                    "so it would name a file on the person's machine.",
                    source,
                    path,
                    hint="Name a file in the plugin with `./`, such as `./scripts/run.py`.",
                )

    def reach(self, action: Action, source: Document) -> None:
        if action.tier == "client":
            for key, what in (("secrets", action.secrets), ("network", action.network)):
                if what:
                    self.add(
                        f"client-{key}",
                        "Client-tier actions run on the person's own machine, so Iki can't "
                        + (
                            "keep secrets from them."
                            if key == "secrets"
                            else "limit what they reach."
                        ),
                        source,
                        (key,),
                        key=True,
                        hint="Run it on the backend tier, or leave this out.",
                    )
        seen: set[str] = set()
        for index, secret in enumerate(action.secrets):
            if secret.name in seen:
                self.add(
                    "secret-twice",
                    f"`{secret.name}` is declared more than once.",
                    source,
                    ("secrets", index),
                    hint="Declare each secret once.",
                )
            seen.add(secret.name)
            if secret.name.startswith("IKI_") or secret.name in RESERVED_VARIABLES:
                self.add(
                    "reserved-secret",
                    f"`{secret.name}` is an environment variable the worker sets itself.",
                    source,
                    ("secrets", index),
                    hint="Choose another name, such as `NCBI_API_KEY`.",
                )
        if action.deterministic and action.network:
            self.add(
                "deterministic-with-network",
                "This action reaches the network, so the same inputs may not give the same "
                "results, and Iki would reuse a run that's out of date.",
                source,
                ("deterministic",),
                key=True,
                severity=Severity.WARNING,
                hint="Drop `deterministic`, unless what it fetches is pinned by version.",
            )

    def retries(self, action: Action, source: Document) -> None:
        if action.deterministic and action.retry_max and "failed" in action.retry_on:
            self.add(
                "retries-deterministic-failure",
                "A deterministic action that fails on its own fails again with the same inputs.",
                source,
                ("retries", "on"),
                severity=Severity.WARNING,
                hint="Retry only `worker_lost`, `interrupted` and `timed_out`.",
            )


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """Requirement lines with continuations joined and comments dropped, numbered from 1."""
    out: list[tuple[int, str]] = []
    pending: tuple[int, str] | None = None
    for number, raw in enumerate(text.splitlines(), start=1):
        line = re.sub(r"(^|\s)#.*$", "", raw).strip()
        if pending is not None:
            start, so_far = pending
            line = f"{so_far} {line}"
            number = start
        if line.endswith("\\"):
            pending = (number, line[:-1].strip())
            continue
        pending = None
        if line:
            out.append((number, line))
    if pending is not None and pending[1]:
        out.append(pending)
    return out


def _nested(a: str, b: str) -> bool:
    a, b = a.rstrip("/") + "/", b.rstrip("/") + "/"
    return a.startswith(b) or b.startswith(a)
