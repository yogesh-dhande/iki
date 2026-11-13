"""A deployment's compute: ``compute.yaml`` (spec ``iki/v1``).

Plugins say what their actions need (environments, ADR 0040); each
deployment says what it has. Its config repository keeps, beside the
lockfile, the machines it runs actions on and which environments run on
which::

    apiVersion: iki/v1
    kind: ComputeTemplate
    name: gpu-a100-spot
    provisioner: aws_ec2
    capacity: { cpu: 96, memory: 1152Gi, gpu: { count: 8, memory: 40Gi, model: A100 } }
    aws: { instance_type: p4d.24xlarge, spot: true, region: us-east-1, … }
    volumes: { af-db: { strategy: clone_from_snapshot } }
    ---
    apiVersion: iki/v1
    kind: EnvironmentBinding
    environment: structure_prediction.alphafold-gpu
    templates: [{ template: gpu-a100-spot }, { template: gpu-a100, priority: 1 }]

``iki deploy build`` checks them against the plugins the lockfile pins: every
binding's templates are big enough for its environment, every action fits
at least one of them, and every volume has a way to reach the machines.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from iki_spec import API_VERSION, schemas
from iki_spec.model import (
    Action,
    Environment,
    Plugin,
    Resources,
    parse_duration,
    parse_quantity,
)
from iki_spec.problems import Path as DocPath
from iki_spec.problems import Problem, Severity
from iki_spec.yamlsource import Document, read_documents

__all__ = [
    "FILENAME",
    "Binding",
    "Compute",
    "ComputeResult",
    "Template",
    "TemplateVolume",
    "compute_from_dict",
    "compute_to_dict",
    "load_compute",
]

FILENAME = "compute.yaml"

KINDS = {"ComputeTemplate": "compute-template", "EnvironmentBinding": "environment-binding"}

#: The tier each provisioner starts machines for.
TIER = {
    "backend_pool": "backend",
    "k8s_job": "container",
    "k8s_deployment": "container",
    "aws_ec2": "vm",
}

#: Volume strategies each provisioner supports.
STRATEGIES = {
    "k8s_job": {"clone_from_snapshot", "attach_exclusive", "shared_filesystem", "object_mount"},
    "k8s_deployment": {"attach_exclusive", "shared_filesystem", "object_mount"},
    "aws_ec2": {"clone_from_snapshot", "attach_exclusive", "shared_filesystem", "object_mount"},
}

#: What an EC2 instance's disk needs besides the scratch space runs get: at least this
#: (and unless the template says, :data:`ROOT_DEFAULT`) for the system and the run's image.
ROOT_SYSTEM = 8 * 1024**3
ROOT_DEFAULT = 40 * 1024**3

#: Strategies that give each machine its own copy or a shared read-only view, so many runs
#: can read at once.
SHARED_READ = {"clone_from_snapshot", "shared_filesystem", "object_mount"}


@dataclass(frozen=True)
class TemplateVolume:
    strategy: str
    fast_snapshot_restore: bool = False


@dataclass(frozen=True)
class Template:
    name: str
    provisioner: str
    lifecycle: str
    capacity: Resources
    label: str = ""
    description: str | None = None
    gpu_model: str | None = None
    kubernetes: dict[str, Any] = field(default_factory=dict)
    aws: dict[str, Any] = field(default_factory=dict)
    volumes: dict[str, TemplateVolume] = field(default_factory=dict)
    max_concurrent: int | None = None
    min_warm: int = 0
    idle_shutdown: int | None = None  # seconds
    price_per_hour: float | None = None
    currency: str | None = None

    @property
    def tier(self) -> str:
        return TIER[self.provisioner]

    def fits(self, wanted: Resources) -> bool:
        """Whether a job that needs `wanted` fits on one of these machines."""
        have = self.capacity
        return (
            (wanted.cpu is None or (have.cpu or 0) >= wanted.cpu)
            and (wanted.memory is None or (have.memory or 0) >= wanted.memory)
            and have.gpu_count >= wanted.gpu_count
            and (
                wanted.gpu_memory is None
                or not wanted.gpu_count
                or (have.gpu_memory or 0) >= wanted.gpu_memory
            )
            and (wanted.scratch_disk is None or (have.scratch_disk or 0) >= wanted.scratch_disk)
        )


@dataclass(frozen=True)
class Binding:
    environment: str  # plugin.environment
    #: (template, priority), in the order given.
    templates: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class Compute:
    templates: dict[str, Template] = field(default_factory=dict)
    bindings: dict[str, Binding] = field(default_factory=dict)


@dataclass
class ComputeResult:
    compute: Compute | None
    problems: list[Problem] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.compute is not None and not any(p.is_error for p in self.problems)


class _Problems:
    def __init__(self, name: str) -> None:
        self.name = name
        self.problems: list[Problem] = []

    def add(
        self,
        code: str,
        message: str,
        document: Document | None,
        path: DocPath = (),
        *,
        hint: str | None = None,
        key: bool = False,
        severity: Severity = Severity.ERROR,
    ) -> None:
        self.problems.append(
            Problem(
                code=code,
                message=message,
                severity=severity,
                file=self.name,
                location=document.locate(path, key=key) if document else None,
                path=path,
                hint=hint,
            )
        )


def load_compute(file: Path, plugins: Sequence[Plugin], name: str | None = None) -> ComputeResult:
    """Read a deployment's compute.yaml and check it against its plugins.

    A deployment without the file runs only client-tier actions, so its absence is no error;
    the actions it can't run are warned about.
    """
    name = name or str(file)
    problems = _Problems(name)
    documents: list[Document] = []
    if file.exists():
        documents, read_problems = read_documents(file, name)
        if read_problems:
            return ComputeResult(None, read_problems)
    templates: dict[str, tuple[Template, Document]] = {}
    bindings: dict[str, tuple[Binding, Document]] = {}
    for document in documents:
        if document.data is None:
            continue
        data = document.data
        kind = data.get("kind") if isinstance(data, dict) else None
        if kind not in KINDS:
            problems.add(
                "unknown-kind",
                f"compute.yaml holds compute templates and environment bindings, not `{kind}`."
                if kind
                else "Each document in compute.yaml needs a `kind`.",
                document,
                ("kind",) if kind else (),
                hint="Use `kind: ComputeTemplate` or `kind: EnvironmentBinding`.",
            )
            continue
        issues = schemas.check(KINDS[kind], data)
        for issue in issues:
            problems.add(
                "schema", issue.message, document, issue.path, hint=issue.hint, key=issue.at_key
            )
        if issues:
            continue
        if kind == "ComputeTemplate":
            if not _provisioner_keys(data, document, problems):
                continue
            template = _template(data)
            if template.name in templates:
                problems.add(
                    "duplicate-template",
                    f"`{template.name}` is defined twice (first on line "
                    f"{templates[template.name][1].start.line}).",
                    document,
                    ("name",),
                )
                continue
            templates[template.name] = (template, document)
        else:
            binding = Binding(
                environment=data["environment"],
                templates=tuple((t["template"], t.get("priority", 0)) for t in data["templates"]),
            )
            if binding.environment in bindings:
                problems.add(
                    "duplicate-binding",
                    f"`{binding.environment}` is bound twice (first on line "
                    f"{bindings[binding.environment][1].start.line}).",
                    document,
                    ("environment",),
                    hint="List all its templates in one binding.",
                )
                continue
            bindings[binding.environment] = (binding, document)
    if any(p.is_error for p in problems.problems):
        return ComputeResult(None, problems.problems)
    _Rules(templates, bindings, plugins, problems).run()
    compute = Compute(
        templates={n: t for n, (t, _) in templates.items()},
        bindings={n: b for n, (b, _) in bindings.items()},
    )
    errors = any(p.is_error for p in problems.problems)
    return ComputeResult(None if errors else compute, problems.problems)


#: Keys only some provisioners take.
_ONLY_FOR = {
    "aws": ("aws_ec2",),
    "lifecycle": ("aws_ec2",),
    "kubernetes": ("backend_pool", "k8s_job", "k8s_deployment"),
    "volumes": ("k8s_job", "k8s_deployment", "aws_ec2"),
}


def _provisioner_keys(data: dict[str, Any], document: Document, problems: _Problems) -> bool:
    ok = True
    for key, provisioners in _ONLY_FOR.items():
        if key in data and data["provisioner"] not in provisioners:
            ok = False
            problems.add(
                "wrong-provisioner",
                f"`{key}` is for " + " and ".join(f"`{p}`" for p in provisioners) + " templates.",
                document,
                (key,),
                key=True,
            )
    return ok


def _template(data: dict[str, Any]) -> Template:
    capacity = data["capacity"]
    gpu = capacity.get("gpu") or {}
    limits = data.get("limits") or {}
    price = data.get("price") or {}
    provisioner = data["provisioner"]
    return Template(
        name=data["name"],
        label=data.get("label") or data["name"].replace("-", " ").capitalize(),
        description=data.get("description"),
        provisioner=provisioner,
        lifecycle=data.get("lifecycle")
        or ("persistent" if provisioner in ("k8s_deployment", "backend_pool") else "ephemeral"),
        capacity=Resources(
            cpu=capacity["cpu"],
            memory=parse_quantity(capacity["memory"]),
            gpu_count=gpu.get("count", 0),
            gpu_memory=parse_quantity(gpu["memory"]) if gpu else None,
            scratch_disk=parse_quantity(capacity["scratch_disk"])
            if "scratch_disk" in capacity
            else None,
        ),
        gpu_model=gpu.get("model"),
        kubernetes=dict(data.get("kubernetes") or {}),
        aws=dict(data.get("aws") or {}),
        volumes={
            n: TemplateVolume(v["strategy"], bool(v.get("fast_snapshot_restore", False)))
            for n, v in (data.get("volumes") or {}).items()
        },
        max_concurrent=limits.get("max_concurrent"),
        min_warm=limits.get("min_warm", 0),
        idle_shutdown=parse_duration(limits["idle_shutdown"])
        if "idle_shutdown" in limits
        else None,
        price_per_hour=price.get("per_hour"),
        currency=price.get("currency"),
    )


class _Rules:
    def __init__(
        self,
        templates: dict[str, tuple[Template, Document]],
        bindings: dict[str, tuple[Binding, Document]],
        plugins: Sequence[Plugin],
        problems: _Problems,
    ) -> None:
        self.templates = templates
        self.bindings = bindings
        self.problems = problems
        self.add = problems.add
        self.environments: dict[str, Environment] = {}
        self.actions: dict[str, list[Action]] = {}
        for plugin in plugins:
            for env in plugin.environments.values():
                self.environments[f"{plugin.name}.{env.name}"] = env
            for action in plugin.actions.values():
                self.actions.setdefault(f"{plugin.name}.{action.environment}", []).append(action)

    def run(self) -> None:
        used: set[str] = set()
        for template, document in self.templates.values():
            self.template(template, document)
        self.prices()
        for binding, document in self.bindings.values():
            used.update(t for t, _ in binding.templates)
            self.binding(binding, document)
        for name, (_, document) in self.templates.items():
            if name not in used:
                self.add(
                    "unused-template",
                    f"No environment is bound to `{name}`.",
                    document,
                    ("name",),
                    severity=Severity.WARNING,
                    hint="Add it to an EnvironmentBinding's `templates`, or delete it.",
                )
        for qualified, env in sorted(self.environments.items()):
            wanted = {a.tier for a in self.actions.get(qualified, [])} - {"client"}
            unbound = [t for t in env.tiers if t in wanted and not self._bound(qualified, t)]
            if unbound:
                self.add(
                    "unbound-environment",
                    f"`{qualified}` has no template for the {' or '.join(unbound)} tier, so "
                    "its actions there can't run on this deployment.",
                    None,
                    severity=Severity.WARNING,
                    hint=f"Add an EnvironmentBinding for `{qualified}`, if this deployment "
                    "should run them.",
                )

    def _bound(self, qualified: str, tier: str) -> bool:
        binding = self.bindings.get(qualified)
        if binding is None:
            return False
        return any(
            name in self.templates and self.templates[name][0].tier == tier
            for name, _ in binding[0].templates
        )

    def prices(self) -> None:
        """Costs add up in one currency, for reports and budgets (ADR 0070)."""
        priced = [(t, d) for t, d in self.templates.values() if t.currency is not None]
        currencies = sorted({t.currency for t, _ in priced if t.currency})
        if len(currencies) > 1:
            first = priced[0][0].currency
            for template, document in priced:
                if template.currency != first:
                    self.add(
                        "mixed-currencies",
                        f"`{template.name}` is priced in {template.currency}, but "
                        f"`{priced[0][0].name}` in {first}: a deployment's costs and budgets "
                        "are in one currency.",
                        document,
                        ("price", "currency"),
                        hint="Convert its price.",
                    )
        for template, document in self.templates.values():
            if template.tier in ("container", "vm") and template.price_per_hour is None:
                self.add(
                    "unpriced-template",
                    f"`{template.name}` has no `price`, so runs on it cost nothing in Iki's "
                    "records, and budgets can't hold them back.",
                    document,
                    ("name",),
                    severity=Severity.WARNING,
                    hint="Give it a `price: { per_hour: …, currency: … }`.",
                )

    def template(self, template: Template, document: Document) -> None:
        if template.provisioner == "backend_pool" and template.capacity.gpu_count:
            self.add(
                "gpu-wrong-tier",
                "Backend worker pods have no GPUs; use `k8s_job` or `aws_ec2` for GPU work.",
                document,
                ("capacity", "gpu"),
                key=True,
            )
        if template.lifecycle == "ephemeral":
            for key in ("min_warm", "idle_shutdown"):
                if key == "min_warm" and not template.min_warm:
                    continue
                if key == "idle_shutdown" and template.idle_shutdown is None:
                    continue
                self.add(
                    "limit-needs-persistent",
                    f"`{key}` is for persistent machines; an ephemeral one stops after its run.",
                    document,
                    ("limits", key),
                    key=True,
                )
        if template.aws.get("fallback") == "on_demand" and not template.aws.get("spot"):
            self.add(
                "fallback-without-spot",
                "`fallback` is for spot instances, and this template doesn't use them.",
                document,
                ("aws", "fallback"),
                key=True,
            )
        root = template.aws.get("root_disk")
        if (
            root is not None
            and (parse_quantity(root) or 0) < (template.capacity.scratch_disk or 0) + ROOT_SYSTEM
        ):
            self.add(
                "root-disk-too-small",
                "`root_disk` must hold `capacity.scratch_disk` and at least 8Gi more for the "
                "system and the run's image.",
                document,
                ("aws", "root_disk"),
                hint="Leave it out for `scratch_disk` and 40Gi.",
            )
        for name, volume in template.volumes.items():
            path: DocPath = ("volumes", name)
            allowed = STRATEGIES.get(template.provisioner, set())
            if volume.strategy not in allowed:
                self.add(
                    "volume-strategy-unsupported",
                    f"`{template.provisioner}` can't give machines a volume by "
                    f"`{volume.strategy}`.",
                    document,
                    (*path, "strategy"),
                    hint="It supports " + ", ".join(f"`{s}`" for s in sorted(allowed)) + ".",
                )
            if volume.fast_snapshot_restore and (
                volume.strategy != "clone_from_snapshot" or template.provisioner != "aws_ec2"
            ):
                self.add(
                    "fast-restore-wrong-strategy",
                    "Fast snapshot restore is for `clone_from_snapshot` on `aws_ec2`.",
                    document,
                    (*path, "fast_snapshot_restore"),
                    key=True,
                )

    def binding(self, binding: Binding, document: Document) -> None:
        env = self.environments.get(binding.environment)
        if env is None:
            plugin = binding.environment.split(".")[0]
            known = sorted(e for e in self.environments if e.startswith(plugin + "."))
            self.add(
                "unknown-environment",
                f"No plugin this deployment installs has the environment `{binding.environment}`.",
                document,
                ("environment",),
                hint=schemas._did_you_mean(binding.environment, known)
                or (
                    "Its environments: " + ", ".join(f"`{e}`" for e in known) + "."
                    if known
                    else f"Check that `{plugin}` is in the lockfile."
                ),
            )
            return
        seen: set[str] = set()
        bound: list[Template] = []
        for index, (name, _) in enumerate(binding.templates):
            path: DocPath = ("templates", index, "template")
            if name in seen:
                self.add("duplicate-template", f"`{name}` is listed twice.", document, path)
                continue
            seen.add(name)
            found = self.templates.get(name)
            if found is None:
                self.add(
                    "unknown-template",
                    f"There's no compute template `{name}` in compute.yaml.",
                    document,
                    path,
                    hint=schemas._did_you_mean(name, list(self.templates)),
                )
                continue
            template = found[0]
            if template.tier not in env.tiers:
                self.add(
                    "template-wrong-tier",
                    f"`{name}` starts {template.tier}-tier machines, and "
                    f"`{binding.environment}` runs on " + ", ".join(env.tiers) + ".",
                    document,
                    path,
                )
                continue
            if not template.fits(env.resources):
                self.add(
                    "template-too-small",
                    f"`{name}` is smaller than `{binding.environment}` needs: "
                    + _shortfall(template, env.resources)
                    + ".",
                    document,
                    path,
                )
                continue
            self.volumes(template, env, binding, document, path)
            bound.append(template)
        for action in self.actions.get(binding.environment, []):
            on_tier = [t for t in bound if t.tier == action.tier]
            if not on_tier:
                continue  # reported as unbound, if at all
            wanted = env.resources.at_least(action.resources)
            if action.lifecycle is not None:
                on_tier = [t for t in on_tier if t.lifecycle == action.lifecycle] or on_tier
            if not any(t.fits(wanted) for t in on_tier):
                largest = max(on_tier, key=lambda t: (t.capacity.memory or 0, t.capacity.cpu or 0))
                self.add(
                    "action-does-not-fit",
                    f"`{action.qualified_name}` needs more than any {action.tier}-tier template "
                    f"bound to `{binding.environment}` has: " + _shortfall(largest, wanted) + ".",
                    document,
                    ("templates",),
                    hint="Bind a larger template.",
                )
            elif action.lifecycle is not None and not any(
                t.lifecycle == action.lifecycle for t in on_tier
            ):
                self.add(
                    "lifecycle-unbound",
                    f"`{action.qualified_name}` runs on {action.lifecycle} machines, and none "
                    f"bound to `{binding.environment}` are.",
                    document,
                    ("templates",),
                    severity=Severity.WARNING,
                    hint="Its runs will use the other templates.",
                )

    def volumes(
        self,
        template: Template,
        env: Environment,
        binding: Binding,
        document: Document,
        path: DocPath,
    ) -> None:
        for name, volume in env.volumes.items():
            given = template.volumes.get(name)
            if given is None:
                self.add(
                    "volume-strategy-missing",
                    f"`{template.name}` doesn't say how its machines get `{binding.environment}`'s "
                    f"volume `{name}`.",
                    document,
                    path,
                    hint=f"Add `volumes: {{ {name}: {{ strategy: clone_from_snapshot }} }}` to the "
                    "template.",
                )
            elif volume.access == "read_write_once" and given.strategy != "attach_exclusive":
                self.add(
                    "volume-strategy-access",
                    f"`{name}` is written by one run at a time, so `{template.name}` must attach "
                    "it exclusively.",
                    document,
                    path,
                    hint="Use `strategy: attach_exclusive`.",
                )
            elif volume.access == "read_only_many" and given.strategy not in SHARED_READ:
                self.add(
                    "volume-strategy-access",
                    f"`{name}` is read by many runs at once, which `attach_exclusive` doesn't "
                    "allow.",
                    document,
                    path,
                    severity=Severity.WARNING,
                    hint="Runs will wait for each other. Use `clone_from_snapshot` or "
                    "`shared_filesystem` to run them side by side.",
                )


def _shortfall(template: Template, wanted: Resources) -> str:
    have = template.capacity
    parts = []
    if wanted.cpu is not None and (have.cpu or 0) < wanted.cpu:
        parts.append(f"{_number(wanted.cpu)} CPUs (it has {_number(have.cpu or 0)})")
    if wanted.memory is not None and (have.memory or 0) < wanted.memory:
        parts.append(f"{_bytes(wanted.memory)} of memory (it has {_bytes(have.memory or 0)})")
    if have.gpu_count < wanted.gpu_count:
        parts.append(f"{wanted.gpu_count} GPU(s) (it has {have.gpu_count})")
    elif wanted.gpu_count and wanted.gpu_memory and (have.gpu_memory or 0) < wanted.gpu_memory:
        parts.append(f"{_bytes(wanted.gpu_memory)} per GPU (it has {_bytes(have.gpu_memory or 0)})")
    if wanted.scratch_disk is not None and (have.scratch_disk or 0) < wanted.scratch_disk:
        parts.append(
            f"{_bytes(wanted.scratch_disk)} of scratch disk (it has "
            f"{_bytes(have.scratch_disk or 0)})"
        )
    return ", ".join(parts)


def _number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def _bytes(value: int) -> str:
    for unit, size in (("Ti", 1024**4), ("Gi", 1024**3), ("Mi", 1024**2)):
        if value >= size and value % size == 0:
            return f"{value // size}{unit}"
    return f"{value // 1024**2}Mi"


def compute_to_dict(compute: Compute) -> dict[str, Any]:
    """JSON for the server: the templates and bindings it schedules jobs with."""
    return {
        "apiVersion": API_VERSION,
        "templates": {n: asdict(t) for n, t in sorted(compute.templates.items())},
        "bindings": {n: asdict(b) for n, b in sorted(compute.bindings.items())},
    }


def compute_from_dict(data: dict[str, Any]) -> Compute:
    """Reads `compute_to_dict`'s JSON back to an equal `Compute`."""
    return Compute(
        templates={
            n: Template(
                **{
                    **t,
                    "capacity": Resources(**t["capacity"]),
                    "volumes": {k: TemplateVolume(**v) for k, v in t["volumes"].items()},
                }
            )
            for n, t in data["templates"].items()
        },
        bindings={
            n: Binding(
                environment=b["environment"],
                templates=tuple((name, priority) for name, priority in b["templates"]),
            )
            for n, b in data["bindings"].items()
        },
    )
