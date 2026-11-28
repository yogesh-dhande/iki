"""Actions and the environments they run in: reading their manifests, and the rules beyond them.

A plugin's actions live under `actions/` and its environments under
`environments/`. The schema checks each document's shape; the rules here
check what needs the rest of the plugin (and the plugins it depends on): that
inputs and effects name real types and fields, that each action's environment
supports its tier, and that what an environment installs is pinned.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path as FilePath
from typing import TYPE_CHECKING, Any

import yaml

from iki_spec import apis, schemas
from iki_spec import validate as _validate
from iki_spec.model import (
    LIST_FIELDS,
    Action,
    Creates,
    EntityType,
    Environment,
    Field,
    Input,
    Metric,
    Resources,
    Secret,
    Software,
    Updates,
    Volume,
    included_reference,
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

#: Machines Iki provisions; the self-hosted and backend tiers run on what's there.
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
        annotations=dict(body.get("annotations") or {}),
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
                include=tuple(spec.get("include") or ()),
                from_project=bool(spec.get("from_project", False)),
                reads=tuple(spec.get("reads") or ()),
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
                match_on=c.get("match_on"),
                on_match=c.get("on_match", "update"),
                receive_into=c.get("receive_into"),
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
        requires_settings=tuple(body.get("requires_settings", ())),
        apis=apis.read_uses(body),
        metrics={
            n: Metric(
                name=n,
                label=m.get("label") or n.replace("_", " ").capitalize(),
                description=m.get("description") or "",
            )
            for n, m in (body.get("metrics") or {}).items()
        },
        software=tuple(
            Software(name=w["name"], version=w["version"], url=w.get("url", ""))
            for w in body.get("software") or ()
        ),
        timeout=parse_duration(body["timeout"]) if "timeout" in body else 3600,
        # Runs whose machine went away are retried, unless they reach the network or an
        # outside API: a retry could repeat what they did elsewhere, such as placing an order
        # or paying for a prediction again.
        retry_max=retries.get("max", 0 if body.get("network") or body.get("apis") else 2),
        retry_on=tuple(retries.get("on", ("worker_lost", "interrupted"))),
        file=document.file,
    )


def read_environment(plugin: str, body: dict[str, Any], document: Document) -> Environment:
    python = body.get("python") or {}
    # `true`, or `{ helm: true }` for the HELM reader too (ADR 0288).
    runtime = python.get("iki_runtime", (body.get("conda") or {}).get("iki_runtime", False))
    setup = body.get("setup") or {}
    name = body["name"]
    return Environment(
        plugin=plugin,
        name=name,
        annotations=dict(body.get("annotations") or {}),
        label=body.get("label") or name.replace("-", " ").capitalize(),
        description=body.get("description"),
        tiers=tuple(body["tiers"]),
        labels=tuple(sorted(set(body.get("labels") or ()))),
        resources=Resources.from_manifest(body.get("resources")),
        image=body.get("image"),
        customer_image_required=body.get("customer_image_required", False),
        python_version=python.get("version"),
        requirements=python.get("requirements"),
        conda_lockfile=(body.get("conda") or {}).get("lockfile"),
        iki_runtime=runtime is True or isinstance(runtime, dict),
        iki_runtime_helm=isinstance(runtime, dict) and runtime.get("helm") is True,
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
        if tiers & PROVISIONED and env.image is None and not env.customer_image_required:
            self.add(
                "environment-needs-image",
                "The container and vm tiers run an image, and this environment has none.",
                source,
                ("tiers",),
                hint="Add `image:` pinned by digest, or declare `customer_image_required: true`.",
            )
        if env.customer_image_required and not tiers & PROVISIONED:
            self.add(
                "customer-image-wrong-tier",
                "A deployment can supply an image for the container or vm tier.",
                source,
                ("customer_image_required",),
                key=True,
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
        if "self_hosted" in tiers and not native and env.image is None:
            self.add(
                "environment-needs-packages",
                "The self-hosted tier needs a Python or conda environment to build on each "
                "machine, or an image to run with Docker or Podman.",
                source,
                ("tiers",),
            )
        if native and not tiers & {"backend", "self_hosted"}:
            self.add(
                "unused-packages",
                "Only the backend and self-hosted tiers build Python or conda environments; the "
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
        if env.resources.gpu_count and "backend" in tiers:
            self.add(
                "gpu-wrong-tier",
                "Backend workers have no GPUs: use the self-hosted, container or vm tier.",
                source,
                ("resources", "gpu"),
                key=True,
            )
        if env.labels and "self_hosted" not in tiers:
            self.add(
                "labels-wrong-tier",
                "Labels pick self-hosted machines, and this environment doesn't run on the "
                "self-hosted tier.",
                source,
                ("labels",),
                key=True,
            )
        if env.requirements is not None:
            text = self.file(env.requirements, source, ("python", "requirements"))
            if text is not None:
                self.requirements(text, source)
                if env.iki_runtime_helm and not any(
                    re.match(r"wasmtime\s*==", line, re.IGNORECASE)
                    for _, line in _logical_lines(text)
                ):
                    self.add(
                        "helm-needs-wasmtime",
                        "It installs the plugin runtime's HELM reader, which runs iki-wasm "
                        "with wasmtime, and its requirements don't pin wasmtime.",
                        source,
                        ("python", "iki_runtime"),
                        hint="Add `wasmtime` to the requirements, pinned and hashed: Iki's own "
                        "plugins pin wasmtime==51.0.0.",
                    )
        if env.conda_lockfile is not None:
            text = self.file(env.conda_lockfile, source, ("conda", "lockfile"))
            if text is not None:
                self.conda(text, source, env.conda_lockfile)
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

    def conda(self, text: str, source: Document, name: str) -> None:
        """A conda lockfile: an explicit one, for one platform, or conda-lock's own, which
        locks each of several platforms and which micromamba installs for the platform it
        runs on (ADR 0208). Either way, each package is pinned by its hash."""
        path: Path = ("conda", "lockfile")
        lines = [ln.strip() for ln in text.splitlines()]
        if "@EXPLICIT" not in lines:
            self.conda_lock(text, source, name)
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

    def conda_lock(self, text: str, source: Document, name: str) -> None:
        path: Path = ("conda", "lockfile")
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError:
            data = None
        packages = data.get("package") if isinstance(data, dict) else None
        metadata = data.get("metadata") if isinstance(data, dict) else None
        platforms = metadata.get("platforms") if isinstance(metadata, dict) else None
        if (
            not isinstance(data, dict)
            or data.get("version") != 1
            or not isinstance(packages, list)
            or not isinstance(platforms, list)
            or not platforms
        ):
            self.add(
                "conda-not-explicit",
                "The lockfile is neither an explicit one (it has no `@EXPLICIT` line) nor "
                "conda-lock's (version 1, with its platforms and packages).",
                source,
                path,
                hint="Write it with `conda-lock lock` (for several platforms) or `conda-lock "
                "--kind explicit`, or `conda list --explicit --md5`.",
            )
            return
        if not name.endswith(("-lock.yml", "-lock.yaml")):
            self.add(
                "conda-lock-name",
                f"micromamba reads `{FilePath(name).name}` as an environment file, not a "
                "lockfile: it takes conda-lock's lockfiles by their name.",
                source,
                path,
                hint="Name it so it ends in `-lock.yml`, as conda-lock does.",
            )
        locked: set[str] = set()
        for index, package in enumerate(packages):
            if not isinstance(package, dict):
                package = {}
            label = f"`{package.get('name')}`" if package.get("name") else f"Package {index + 1}"
            sha256 = (
                (package.get("hash") or {}).get("sha256")
                if isinstance(package.get("hash"), dict)
                else None
            )
            if package.get("manager") == "conda":
                locked.add(str(package.get("platform")))
            if package.get("manager") != "conda":
                self.add(
                    "conda-unpinned",
                    f"{label} is installed by {package.get('manager') or 'nothing named'}, "
                    "not conda: a conda environment installs only conda packages.",
                    source,
                    path,
                    hint="Lock the environment's Python packages from conda-forge too.",
                )
            elif not (
                isinstance(package.get("url"), str)
                and isinstance(sha256, str)
                and re.fullmatch(r"[0-9a-f]{64}", sha256)
            ):
                self.add(
                    "conda-unpinned",
                    f"{label} has no URL and SHA-256, so builds could install something else.",
                    source,
                    path,
                    hint="Write the lockfile with `conda-lock lock`, which gives both.",
                )
        missing = [str(p) for p in platforms if str(p) not in locked]
        if missing:
            self.add(
                "conda-unpinned",
                f"The lockfile names {', '.join(missing)} but locks no package for it.",
                source,
                path,
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
        self.setup_network(env, source)
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

    def setup_network(self, env: Environment, source: Document) -> None:
        """The hosts a setup build downloads from. None may be an outside API's: a build
        belongs to the deployment, so there's no project to approve the API for, nobody to
        consent to what's sent, and no project to bill (ADR 0141)."""
        apis = {api.host: api for api in self.plugin.apis.values() if api.host}
        for index, host in enumerate(env.setup_network):
            at: Path = ("setup", "network", index)
            if host.startswith("{settings."):
                self.network_setting(host.removeprefix("{settings.")[:-1], source, index, at)
            api = apis.get(host)
            if api is not None:
                self.add(
                    "setup-calls-api",
                    f"`{host}` is {api.label}'s host, an outside API, and setup builds can't "
                    "call outside APIs: a build belongs to the deployment, so there's no "
                    "project to approve it for and none to bill (ADR 0141).",
                    source,
                    at,
                    hint="Drop it. A setup downloads its data, such as a model's weights, "
                    "from the host that serves the files.",
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
        self.runs_on(action, source)
        self.command(action, source)
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
        self.software(action, source)

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
        if target is not None:
            self.include(put, target, source, path)
            self.reads(put, target, source, path)
        if put.from_project and put.cardinality != "many":
            self.add(
                "from-project-one",
                "An input `from_project` is given every record of its type the project owns, "
                "and there can be any number.",
                source,
                (*path, "from_project"),
                key=True,
                hint="Add `cardinality: many`.",
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

    def include(self, put: Input, target: EntityType, source: Document, path: Path) -> None:
        """An input's `include`: its type's references, its own or a parent's."""
        fields = {n: f for n, (f, _) in self.s.all_fields(target).items()}
        for index, name in enumerate(put.include):
            found = included_reference(fields, name)
            where = (*path, "include", index)
            if found is None:
                self.add(
                    "include-unknown-field",
                    f"`{put.type}` has no field `{name}`.",
                    source,
                    where,
                    hint=f"Name one of its references: {_references(fields)}.",
                )
            elif not _reference(found):
                self.add(
                    "include-not-reference",
                    f"`{name}` isn't a reference: only the entities references name can be "
                    "included, and the input's own values are in its entities already.",
                    source,
                    where,
                )
            else:
                self.curated(found, source, where)

    def reads(self, put: Input, target: EntityType, source: Document, path: Path) -> None:
        """An input's `reads` (ADR 0284): fields of its type, its own or a parent's, as each
        entity a run is given is one of its type's."""
        fields = {n: f for n, (f, _) in self.s.all_fields(target).items() if not f.removed}
        for index, name in enumerate(put.reads):
            if name not in fields:
                self.add(
                    "reads-unknown-field",
                    f"`{put.type}` has no field `{name}`: a run's entities may be of its "
                    "subtypes, but each has its fields.",
                    source,
                    (*path, "reads", index),
                    hint=schemas._did_you_mean(name, sorted(fields)),
                )

    def curated(self, f: Field, source: Document, path: Path) -> None:
        """An included lab list's values a calculation can work from or branch on, its
        enums, numbers and Booleans, are fixed once an option is added (ADR 0143): what a
        calculation or worker reads is the plugin's, and a curator's change mustn't change
        what it gives for records already made (ADR 0270). Its text, such as an SOP, and its
        references to other lists, which a run reads as IDs, are the curators' to change."""
        ref = f.items if f.type == "list" and f.items is not None else f
        for target in ref.to:
            listed = self.s.types.get(target)
            if listed is None or not listed.is_lab_list:
                continue
            changeable = sorted(
                n
                for n, (x, _) in self.s.all_fields(listed).items()
                if n not in LIST_FIELDS and not x.immutable and not x.removed and _drives(x)
            )
            if changeable:
                shown = ", ".join(f"`{n}`" for n in changeable)
                self.add(
                    "include-curated-fact",
                    f"The run reads {listed.name}'s {shown}, which the list's curators can "
                    "change: what a calculation or worker reads is the plugin's (ADR 0270).",
                    source,
                    path,
                    hint="Declare each `immutable: true`, so it's fixed once an option is added "
                    "and a wrong one is superseded (ADR 0143).",
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
        if param.items and param.items.media_types:
            self.s.file_choices(param.items, source, (*path, "items"))
        if param.type == "quantity":
            self.s.quantity(param, source, path)
        if param.has_default:
            for message in _validate.value_problems(param, param.default):
                self.add("invalid-default", f"The default {message}", source, (*path, "default"))

    def match_on(self, name: str, target: EntityType, source: Document, path: Path) -> None:
        """A create's `match_on`: a unique field of the type, its own, a parent's, or one an
        extension of this plugin adds."""
        fields = {n: f for n, (f, _) in self.s.all_fields(target).items()}
        names = {target.qualified_name, *(a.qualified_name for a in self.s.ancestors(target))}
        for extension in self.s.plugin.extensions:
            if extension.extends_type in names:
                fields.update(extension.fields)
        found = fields.get(name)
        if found is None:
            self.add(
                "match-on-unknown-field",
                f"`{target.qualified_name}` has no field `{name}` to match on.",
                source,
                path,
            )
        elif not found.unique:
            self.add(
                "match-on-not-unique",
                f"`{name}` isn't unique, so a value could match more than one entity: only "
                "unique fields can be matched on.",
                source,
                path,
                hint=f"Mark `{name}` `unique: true` (and `indexed: true`).",
            )
        elif found.registry is not None:
            self.add(
                "match-on-registry-field",
                f"`{name}` is a registry {found.registry}, unique across the registry: a "
                "create matches on a field of each project's record, such as another system's "
                "ID for it.",
                source,
                path,
                hint="A create of an identity type finds its registry entry by its identity "
                "already (ADR 0115).",
            )

    def receipt(
        self,
        action: Action,
        effect: Creates,
        target: EntityType | None,
        source: Document,
        path: Path,
    ) -> None:
        name = effect.receive_into
        assert name is not None  # noqa: S101 - caller checked
        dest = action.inputs.get(name)
        if dest is None:
            self.unknown_input(action, name, source, path)
            return
        if dest.optional or dest.cardinality != "one":
            self.add(
                "receipt-container-input",
                "Receiving needs one required storage input.",
                source,
                path,
            )
        if effect.match_on is not None:
            self.add(
                "receipt-match", "A receipt creates a fresh record for the delivery.", source, path
            )
        if target is None:
            return
        lineage = [target, *self.s.ancestors(target)]
        if not any(t.amount for t in lineage) or self.s.storage_of(target) is None:
            self.add(
                "receipt-lot-type", "A received type needs an amount and storage.", source, path
            )
        container = self.s.types.get(dest.type)
        if container is None:
            return
        storage = self.s.storage_of(container)
        if storage is None or not {t.qualified_name for t in lineage}.intersection(storage.holds):
            self.add(
                "receipt-container-type", f"`{dest.type}` must hold `{effect.type}`.", source, path
            )

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
            if creates.receive_into is not None:
                self.receipt(action, creates, target, source, (*path, "receive_into"))
            if creates.match_on is not None and target is not None:
                self.match_on(creates.match_on, target, source, (*path, "match_on"))
            if creates.on_match != "update" and creates.match_on is None:
                self.add(
                    "on-match-without-match-on",
                    "`on_match` says what a create that matches an entity does, and this one "
                    "matches on no field.",
                    source,
                    (*path, "on_match"),
                    hint="Give `match_on`, a unique field, or drop `on_match`.",
                )
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
        lineage = [target, *self.s.ancestors(target)]
        chain = {t.qualified_name for t in lineage}
        ledger_fields = {t.draws_received for t in lineage if t.draws_received}
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
            elif name in ledger_fields:
                self.add(
                    "updates-draws-received",
                    f"Only the amounts ledger may append to `{name}` when material is drawn.",
                    source,
                    where,
                )
            elif f.type == "state":
                self.add(
                    "updates-state",
                    f"`{name}` is a state field: people with its states' lab roles set it, "
                    "one revision each (ADR 0125), not an action's results.",
                    source,
                    where,
                    hint="Read it as an input instead, or trigger the action on "
                    "`entity.state_changed`.",
                )
            elif f.cel is not None:
                self.add(
                    "updates-cel-computed",
                    f"`{name}` is worked out from its record's own values at every save "
                    "(ADR 0297): no one sets it, not even a run.",
                    source,
                    where,
                    hint="Update the fields it's worked out from instead.",
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
        # A self-hosted worker says what it has, GPUs included, and leases what fits.
        if action.resources.gpu_count and action.tier == "backend":
            self.add(
                "gpu-wrong-tier",
                "Backend workers have no GPUs: use the self-hosted, container or vm tier.",
                source,
                (*path, "resources", "gpu"),
                key=True,
            )
        return environment

    def command(self, action: Action, source: Document) -> None:
        for index, argument in enumerate(action.command):
            path: Path = ("command", index)
            if argument.startswith("./"):
                self.file(argument[2:], source, path)
                self.runtime(action, argument[2:], source, path)
            elif argument.startswith("/") and action.tier == "backend":
                self.add(
                    "absolute-path-on-backend",
                    f"`{argument}` is an absolute path, and backend-tier actions run without an "
                    "image.",
                    source,
                    path,
                    hint="Name a file in the plugin with `./`, such as `./scripts/run.py`.",
                )

    def runtime(self, action: Action, script: str, source: Document, path: Path) -> None:
        """A script that imports the plugin runtime runs where it's installed (ADR 0288):
        in an environment Iki builds that declares it, with its HELM reader if it imports
        ``iki_runtime.helm``. An image's is the image's own."""
        env = self.plugin.environments.get(action.environment)
        if env is None or (env.iki_runtime and env.iki_runtime_helm):
            return
        if env.requirements is None and env.conda_lockfile is None:
            return
        target = self.root / script
        if target.suffix != ".py" or not self._inside(target) or not target.is_file():
            return
        imported = _imports_runtime(target)
        section = "python" if env.requirements is not None else "conda"
        if "iki_runtime" in imported and not env.iki_runtime:
            self.add(
                "runtime-not-installed",
                f"`{script}` imports `iki_runtime`, and the `{env.name}` environment it runs "
                "in doesn't install it.",
                source,
                path,
                hint=f"Add `iki_runtime: true` to `{env.name}`'s `{section}`"
                + (
                    ", as `iki_runtime: { helm: true }` for its HELM reader."
                    if "helm" in imported
                    else "."
                ),
            )
        elif "helm" in imported and not env.iki_runtime_helm:
            self.add(
                "runtime-not-installed",
                f"`{script}` imports `iki_runtime.helm`, which reads HELM with iki-wasm's "
                f"module, and the `{env.name}` environment it runs in doesn't install it.",
                source,
                path,
                hint=f"Write `iki_runtime: {{ helm: true }}` in `{env.name}`'s `{section}`, and "
                "pin wasmtime in its packages.",
            )

    def reach(self, action: Action, source: Document) -> None:
        if action.tier == "self_hosted":
            for key, what in (("secrets", action.secrets), ("network", action.network)):
                if what:
                    self.add(
                        f"self-hosted-{key}",
                        "Self-hosted actions run on machines their teams look after, so Iki "
                        + (
                            "can't keep the deployment's secrets from whoever runs them."
                            if key == "secrets"
                            else "can't limit what they reach."
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
        for index, host in enumerate(action.network):
            if host.startswith("{settings."):
                self.network_setting(host.removeprefix("{settings.")[:-1], source, index)
        for index, name in enumerate(action.requires_settings):
            self.required_setting(name, source, index)
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

    def required_setting(self, name: str, source: Document, index: int) -> None:
        """A setting the action can't run without: one of the plugin's, which a deployment
        may leave unset."""
        at: Path = ("requires_settings", index)
        setting = self.plugin.settings.get(name)
        if setting is None:
            self.add(
                "requires-unknown-setting",
                f"The plugin has no setting `{name}`.",
                source,
                at,
                hint=schemas._did_you_mean(name, list(self.plugin.settings))
                or "Declare it under `settings:` in plugin.yaml.",
            )
        elif setting.required or setting.has_default:
            self.add(
                "requires-setting-always-set",
                f"`{name}` is "
                + ("required" if setting.required else "given a default")
                + ", so every deployment has it.",
                source,
                at,
                severity=Severity.WARNING,
                hint="Leave it out of `requires_settings`.",
            )

    def network_setting(
        self, name: str, source: Document, index: int, path: Path | None = None
    ) -> None:
        """A host in `network` the deployment gives: a plugin setting that holds a hostname."""
        at: Path = path or ("network", index)
        setting = self.plugin.settings.get(name)
        if setting is None:
            self.add(
                "network-unknown-setting",
                f"The plugin has no setting `{name}`.",
                source,
                at,
                hint=schemas._did_you_mean(name, list(self.plugin.settings))
                or "Declare it under `settings:` in plugin.yaml, with `format: hostname`.",
            )
        elif setting.type != "string" or setting.format != "hostname":
            self.add(
                "network-setting-not-hostname",
                f"`{name}` isn't a host name, so a run can't reach it.",
                source,
                at,
                hint=f"Give `{name}` `type: string` and `format: hostname`.",
            )

    def software(self, action: Action, source: Document) -> None:
        """Each tool once: what its runs record they made entities with goes by name."""
        seen: set[str] = set()
        for index, tool in enumerate(action.software):
            if tool.name.casefold() in seen:
                self.add(
                    "duplicate-software",
                    f"`{tool.name}` is listed twice.",
                    source,
                    ("software", index, "name"),
                    hint="List each tool once, at the version the action runs.",
                )
            seen.add(tool.name.casefold())

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


def _reference(f: Field) -> bool:
    """A reference, or a list of them."""
    return f.type == "ref" or (f.type == "list" and f.items is not None and f.items.type == "ref")


def _references(fields: dict[str, Field]) -> str:
    names = sorted(n for n, f in fields.items() if _reference(f))
    # And the references of its lists of rows (ADR 0161).
    names += sorted(
        f"{n}.{c}"
        for n, f in fields.items()
        if f.is_rows and f.items is not None
        for c, cell in f.items.fields.items()
        if cell.type == "ref"
    )
    return ", ".join(f"`{n}`" for n in names) or "it has none"


def _drives(f: Field) -> bool:
    """Whether a value is one a calculation works from or branches on: an enum, a number or
    a Boolean, or a list of them."""
    kind = f.items.type if f.type == "list" and f.items is not None else f.type
    return kind in {"enum", "bool", "int", "float", "quantity"}


def _imports_runtime(script: FilePath) -> set[str]:
    """What of the plugin runtime ``script``, or a module beside it that it imports (as
    ``python script`` finds them), imports: ``iki_runtime`` for any of it, and ``helm``
    as well for ``iki_runtime.helm``, which needs more than the package."""
    seen: set[FilePath] = set()
    pending = [script]
    found: set[str] = set()
    while pending:
        file = pending.pop()
        if file in seen:
            continue
        seen.add(file)
        try:
            tree = ast.parse(file.read_bytes())
        except (OSError, SyntaxError, ValueError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                names = [node.module]
                if node.module == "iki_runtime":
                    names += [f"iki_runtime.{alias.name}" for alias in node.names]
            else:
                continue
            for name in names:
                parts = name.split(".")
                if parts[0] == "iki_runtime":
                    found.add("iki_runtime")
                    if parts[1:2] == ["helm"]:
                        found.add("helm")
                    continue
                local = script.parent / f"{parts[0]}.py"
                if local.is_file() and not local.is_symlink():
                    pending.append(local)
    return found
