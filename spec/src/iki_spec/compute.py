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
    ---
    apiVersion: iki/v1
    kind: ApiPrice
    api: acme/models.fold
    per_call: 0.02
    usage: { gpu_seconds: 0.0011 }
    currency: USD

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
    OutsideApi,
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

KINDS = {
    "ComputeTemplate": "compute-template",
    "EnvironmentBinding": "environment-binding",
    "ApiPrice": "api-price",
}

#: The tier each provisioner starts machines for.
TIER = {
    "backend_pool": "backend",
    "k8s_job": "container",
    "k8s_deployment": "container",
    # A Slurm job per run, which iki-worker on the cluster's login node submits (ADR 0133).
    "slurm": "container",
    "aws_ec2": "vm",
    "gcp_gce": "vm",
}

#: The provisioners that start Kubernetes pods.
KUBERNETES = {"k8s_job", "k8s_deployment"}

#: Volume strategies each provisioner supports.
STRATEGIES = {
    "k8s_job": {"clone_from_snapshot", "attach_exclusive", "shared_filesystem", "object_mount"},
    # Each pod of a warm pool gets its own clone, as a run's pod does.
    "k8s_deployment": {
        "clone_from_snapshot",
        "attach_exclusive",
        "shared_filesystem",
        "object_mount",
    },
    "aws_ec2": {"clone_from_snapshot", "attach_exclusive", "shared_filesystem", "object_mount"},
    # Persistent disks from an image; no shared file systems or buckets yet.
    "gcp_gce": {"clone_from_snapshot", "attach_exclusive"},
}

#: A price's rates: a machine's by the hour, or a Slurm job's by what it was given.
RATES = ("per_hour", "per_cpu_hour", "per_gpu_hour", "per_memory_gib_hour")

#: What a cloud machine's disk needs besides the scratch space runs get: at least this
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
    #: On Kubernetes: the StorageClass of its disks, and the VolumeSnapshotClass of its
    #: snapshots (the cluster's defaults unless set).
    storage_class: str | None = None
    snapshot_class: str | None = None
    #: On AWS: each disk's provisioned IOPS and throughput (MiB/s), as gp3 allows (EBS's
    #: baseline unless set).
    iops: int | None = None
    throughput: int | None = None
    #: With `shared_filesystem` on Kubernetes: the PersistentVolumeClaim of the file
    #: system (FSx for Lustre, EFS, NFS), in the namespace runs go in; with
    #: `object_mount`, of a bucket, through Mountpoint for Amazon S3. On `aws_ec2`, what
    #: machines mount: an NFS export (`nfs`, such as EFS's) or a Lustre file system's
    #: (`lustre`, such as FSx for Lustre's), or with `object_mount`, the S3 `bucket` they
    #: mount with Mountpoint. Either way, `path` is the directory in it Iki keeps versions
    #: under.
    claim: str | None = None
    path: str | None = None
    nfs: str | None = None
    lustre: str | None = None
    bucket: str | None = None

    @property
    def root(self) -> str:
        return self.path or "iki"

    @property
    def filesystem(self) -> dict[str, str] | None:
        """What an EC2 machine mounts: its type and source."""
        if self.nfs is not None:
            return {"type": "nfs", "source": self.nfs}
        if self.lustre is not None:
            return {"type": "lustre", "source": self.lustre}
        if self.bucket is not None:
            return {"type": "s3", "source": self.bucket}
        return None


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
    gcp: dict[str, Any] = field(default_factory=dict)
    #: For `slurm`: the cluster, partition, QOS, default account, constraint, GPU type,
    #: ``max_walltime`` (seconds) and where compute nodes reach the egress proxy.
    slurm: dict[str, Any] = field(default_factory=dict)
    volumes: dict[str, TemplateVolume] = field(default_factory=dict)
    max_concurrent: int | None = None
    min_warm: int = 0
    idle_shutdown: int | None = None  # seconds
    price_per_hour: float | None = None
    currency: str | None = None
    #: For `slurm`: a job pays for what Slurm gave it, for as long as it ran.
    price_per_cpu_hour: float | None = None
    price_per_gpu_hour: float | None = None
    price_per_memory_gib_hour: float | None = None

    @property
    def tier(self) -> str:
        return TIER[self.provisioner]

    def serves(self, lifecycle: str | None) -> bool:
        """Whether an action that asks for `lifecycle` machines runs here. Each Slurm job is
        an allocation of its own, as an ephemeral action asks; a persistent one asks only for
        a machine that's warm, which no cluster's queue promises, so it runs there too."""
        return lifecycle is None or self.provisioner == "slurm" or self.lifecycle == lifecycle

    @property
    def priced(self) -> bool:
        rates = (
            self.price_per_hour,
            self.price_per_cpu_hour,
            self.price_per_gpu_hour,
            self.price_per_memory_gib_hour,
        )
        return self.currency is not None and any(rate is not None for rate in rates)

    def per_hour(self, wanted: Resources) -> float | None:
        """What one of its machines costs an hour, or for a Slurm job, one given `wanted`."""
        if not self.priced:
            return None
        if self.provisioner != "slurm":
            return self.price_per_hour
        return (
            (wanted.cpu or 1) * (self.price_per_cpu_hour or 0)
            + wanted.gpu_count * (self.price_per_gpu_hour or 0)
            + (wanted.memory or 0) / 1024**3 * (self.price_per_memory_gib_hour or 0)
        )

    def why_not(self, action: Action) -> str | None:
        """Why `action` can't run here though it fits, or None: a Slurm partition's time limit
        is shorter than its timeout, or the cluster's compute nodes can't reach the egress
        proxy (ADR 0133)."""
        if self.provisioner != "slurm":
            return None
        walltime = self.slurm.get("max_walltime")
        if walltime is not None and action.timeout > walltime:
            return (
                f"its timeout ({_duration(action.timeout)}) is longer than `{self.name}`'s "
                f"partition allows ({_duration(walltime)})"
            )
        if (action.network or action.apis) and not self.slurm.get("egress_proxy_url"):
            # Outside APIs are called through the egress proxy too (ADR 0130).
            reaches = "calls outside APIs" if not action.network else "reaches the internet"
            return f"it {reaches}, and `{self.name}`'s compute nodes can't reach the egress proxy"
        return None

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
    templates: tuple[tuple[str, int], ...] = ()
    #: What this deployment's machines need for its self-hosted runs, sorted (ADR 0112).
    labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class ApiPrice:
    """What the deployment pays for a plugin's outside API (ADR 0130): per call answered,
    and per unit of each usage it reports."""

    api: str  # plugin.api
    currency: str
    per_call: float | None = None
    usage: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Compute:
    templates: dict[str, Template] = field(default_factory=dict)
    bindings: dict[str, Binding] = field(default_factory=dict)
    #: Outside APIs' prices, by ``plugin.api``.
    prices: dict[str, ApiPrice] = field(default_factory=dict)


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

    A deployment without the file runs only self-hosted actions, so its absence is no error;
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
    prices: dict[str, tuple[ApiPrice, Document]] = {}
    for document in documents:
        if document.data is None:
            continue
        data = document.data
        kind = data.get("kind") if isinstance(data, dict) else None
        if kind not in KINDS:
            problems.add(
                "unknown-kind",
                "compute.yaml holds compute templates, environment bindings and API prices, "
                f"not `{kind}`."
                if kind
                else "Each document in compute.yaml needs a `kind`.",
                document,
                ("kind",) if kind else (),
                hint="Use `kind: ComputeTemplate`, `kind: EnvironmentBinding` or `kind: ApiPrice`.",
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
        elif kind == "ApiPrice":
            price = ApiPrice(
                api=data["api"],
                currency=data["currency"],
                per_call=data.get("per_call"),
                usage=dict(data.get("usage") or {}),
            )
            if price.api in prices:
                problems.add(
                    "duplicate-price",
                    f"`{price.api}` is priced twice (first on line "
                    f"{prices[price.api][1].start.line}).",
                    document,
                    ("api",),
                )
                continue
            prices[price.api] = (price, document)
        else:
            binding = Binding(
                environment=data["environment"],
                templates=tuple(
                    (t["template"], t.get("priority", 0)) for t in data.get("templates", ())
                ),
                labels=tuple(sorted(set(data.get("labels", ())))),
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
    _Rules(templates, bindings, plugins, problems, prices).run()
    compute = Compute(
        templates={n: t for n, (t, _) in templates.items()},
        bindings={n: b for n, (b, _) in bindings.items()},
        prices={n: p for n, (p, _) in prices.items()},
    )
    errors = any(p.is_error for p in problems.problems)
    return ComputeResult(None if errors else compute, problems.problems)


#: Keys only some provisioners take.
_ONLY_FOR = {
    "aws": ("aws_ec2",),
    "gcp": ("gcp_gce",),
    "lifecycle": ("aws_ec2", "gcp_gce"),
    "kubernetes": ("backend_pool", "k8s_job", "k8s_deployment"),
    "slurm": ("slurm",),
    "volumes": ("k8s_job", "k8s_deployment", "aws_ec2", "gcp_gce"),
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
    return _price_rates(data, document, problems) and ok


def _price_rates(data: dict[str, Any], document: Document, problems: _Problems) -> bool:
    """A machine is priced by the hour; a Slurm job by what it's given (ADR 0133)."""
    price = data.get("price") or {}
    slurm = data["provisioner"] == "slurm"
    ok = True
    for rate in RATES:
        if rate in price and (rate == "per_hour") == slurm:
            ok = False
            problems.add(
                "wrong-price",
                "A Slurm job pays for what it's given: price it `per_cpu_hour`, "
                "`per_gpu_hour` and `per_memory_gib_hour`."
                if slurm
                else f"`{rate}` is for `slurm` templates: price a machine `per_hour`.",
                document,
                ("price", rate),
                key=True,
            )
    if price and not any(rate in price for rate in RATES):
        ok = False
        problems.add(
            "price-without-rate",
            "A `price` needs what it costs, not only its currency.",
            document,
            ("price",),
            hint="Add `per_cpu_hour`, `per_gpu_hour` or `per_memory_gib_hour`."
            if slurm
            else "Add `per_hour`.",
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
        gcp=dict(data.get("gcp") or {}),
        slurm=_slurm(data.get("slurm")),
        volumes={
            n: TemplateVolume(
                v["strategy"],
                bool(v.get("fast_snapshot_restore", False)),
                storage_class=v.get("storage_class"),
                snapshot_class=v.get("snapshot_class"),
                iops=v.get("iops"),
                throughput=v.get("throughput"),
                claim=v.get("claim"),
                path=v.get("path"),
                nfs=v.get("nfs"),
                lustre=v.get("lustre"),
                bucket=v.get("bucket"),
            )
            for n, v in (data.get("volumes") or {}).items()
        },
        max_concurrent=limits.get("max_concurrent"),
        min_warm=limits.get("min_warm", 0),
        idle_shutdown=parse_duration(limits["idle_shutdown"])
        if "idle_shutdown" in limits
        else None,
        price_per_hour=price.get("per_hour"),
        currency=price.get("currency"),
        price_per_cpu_hour=price.get("per_cpu_hour"),
        price_per_gpu_hour=price.get("per_gpu_hour"),
        price_per_memory_gib_hour=price.get("per_memory_gib_hour"),
    )


def _slurm(given: dict[str, Any] | None) -> dict[str, Any]:
    """A Slurm template's settings, with its time limit in seconds."""
    found = dict(given or {})
    if "max_walltime" in found:
        found["max_walltime"] = parse_duration(found["max_walltime"])
    return found


def _duration(seconds: int) -> str:
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size and seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


class _Rules:
    def __init__(
        self,
        templates: dict[str, tuple[Template, Document]],
        bindings: dict[str, tuple[Binding, Document]],
        plugins: Sequence[Plugin],
        problems: _Problems,
        prices: dict[str, tuple[ApiPrice, Document]] | None = None,
    ) -> None:
        self.templates = templates
        self.bindings = bindings
        self.api_prices = prices or {}
        self.apis: dict[str, OutsideApi] = {
            api.qualified_name: api for plugin in plugins for api in plugin.apis.values()
        }
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
            wanted = {a.tier for a in self.actions.get(qualified, [])} - {"self_hosted"}
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
        """Costs add up in one currency, for reports and budgets (ADR 0070): templates' and
        outside APIs' (ADR 0130)."""
        priced: list[tuple[str, str, Document, DocPath]] = [
            (t.name, t.currency, d, ("price", "currency"))
            for t, d in self.templates.values()
            if t.currency is not None
        ]
        priced += [(p.api, p.currency, d, ("currency",)) for p, d in self.api_prices.values()]
        if len({currency for _, currency, _, _ in priced}) > 1:
            first_name, first, _, _ = priced[0]
            for name, currency, document, path in priced:
                if currency != first:
                    self.add(
                        "mixed-currencies",
                        f"`{name}` is priced in {currency}, but `{first_name}` in {first}: a "
                        "deployment's costs and budgets are in one currency.",
                        document,
                        path,
                        hint="Convert its price.",
                    )
        for price, document in self.api_prices.values():
            api = self.apis.get(price.api)
            if api is None:
                self.add(
                    "unknown-api",
                    f"No installed plugin has the outside API `{price.api}`.",
                    document,
                    ("api",),
                    hint=schemas._did_you_mean(price.api, list(self.apis)),
                )
                continue
            reported = {*api.usage, *(api.jobs.usage if api.jobs else {})}
            for name in price.usage:
                if name not in reported:
                    self.add(
                        "unknown-usage",
                        f"{api.label} doesn't report `{name}`"
                        + (
                            f": it reports {', '.join(f'`{u}`' for u in sorted(reported))}."
                            if reported
                            else ", or any usage."
                        ),
                        document,
                        ("usage", name),
                        key=True,
                    )
        for qualified, api in sorted(self.apis.items()):
            if qualified not in self.api_prices:
                self.add(
                    "unpriced-api",
                    f"`{qualified}` ({api.label}) has no price, so calls to it cost nothing in "
                    "Iki's records, and budgets can't hold them back.",
                    None,
                    severity=Severity.WARNING,
                    hint="Add an ApiPrice for it, with `per_call` or `usage`.",
                )
        for template, document in self.templates.values():
            if template.tier in ("container", "vm") and not template.priced:
                self.add(
                    "unpriced-template",
                    f"`{template.name}` has no `price`, so runs on it cost nothing in Iki's "
                    "records, and budgets can't hold them back.",
                    document,
                    ("name",),
                    severity=Severity.WARNING,
                    hint="Give it a `price: { per_cpu_hour: …, per_gpu_hour: …, currency: … }`."
                    if template.provisioner == "slurm"
                    else "Give it a `price: { per_hour: …, currency: … }`.",
                )

    def template(self, template: Template, document: Document) -> None:
        if template.provisioner == "backend_pool" and template.capacity.gpu_count:
            self.add(
                "gpu-wrong-tier",
                "Backend worker pods have no GPUs; use `k8s_job`, `aws_ec2` or `gcp_gce` for GPU "
                "work.",
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
        for cloud, machine in (("aws", template.aws), ("gcp", template.gcp)):
            if machine.get("fallback") == "on_demand" and not machine.get("spot"):
                self.add(
                    "fallback-without-spot",
                    "`fallback` is for spot instances, and this template doesn't use them.",
                    document,
                    (cloud, "fallback"),
                    key=True,
                )
            root = machine.get("root_disk")
            if (
                root is not None
                and (parse_quantity(root) or 0)
                < (template.capacity.scratch_disk or 0) + ROOT_SYSTEM
            ):
                self.add(
                    "root-disk-too-small",
                    "`root_disk` must hold `capacity.scratch_disk` and at least 8Gi more for "
                    "the system and the run's image.",
                    document,
                    (cloud, "root_disk"),
                    hint="Leave it out for `scratch_disk` and 40Gi.",
                )
        if template.provisioner == "gcp_gce":
            self.gcp(template, document)
        if (
            template.provisioner == "slurm"
            and template.slurm.get("gpu_type")
            and not template.capacity.gpu_count
        ):
            self.add(
                "gpu-type-without-gpus",
                "`gpu_type` names the GPUs jobs ask for, and this template has none.",
                document,
                ("slurm", "gpu_type"),
                key=True,
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
            for option in ("storage_class", "snapshot_class"):
                if getattr(volume, option) is not None and template.provisioner not in KUBERNETES:
                    self.add(
                        "volume-option-kubernetes-only",
                        f"`{option}` is for the Kubernetes provisioners.",
                        document,
                        (*path, option),
                        key=True,
                    )
            shared = volume.strategy == "shared_filesystem"
            mounted = volume.strategy in ("shared_filesystem", "object_mount")
            for option, given, fits in (
                (
                    "claim",
                    "`shared_filesystem` or `object_mount` on the Kubernetes provisioners",
                    mounted and template.provisioner in KUBERNETES,
                ),
                ("nfs", "`shared_filesystem` on `aws_ec2`", shared),
                ("lustre", "`shared_filesystem` on `aws_ec2`", shared),
                ("bucket", "`object_mount` on `aws_ec2`", volume.strategy == "object_mount"),
                ("path", "`shared_filesystem` or `object_mount`", mounted),
            ):
                if option in ("nfs", "lustre", "bucket"):
                    fits = fits and template.provisioner == "aws_ec2"
                if getattr(volume, option) is not None and not fits:
                    self.add(
                        "volume-option-shared-filesystem-only",
                        f"`{option}` is for {given}.",
                        document,
                        (*path, option),
                        key=True,
                    )
            if mounted and template.provisioner in KUBERNETES and volume.claim is None:
                self.add(
                    "shared-filesystem-claim-missing",
                    f"`{volume.strategy}` on Kubernetes needs the `claim` it's mounted from.",
                    document,
                    (*path, "strategy"),
                    hint="Add `claim: <PersistentVolumeClaim>`: one that many pods can mount "
                    "(ReadWriteMany), in the namespace runs go in"
                    + (
                        ", of the Mountpoint for Amazon S3 CSI driver."
                        if volume.strategy == "object_mount"
                        else "."
                    ),
                )
            if (
                shared
                and template.provisioner == "aws_ec2"
                and (volume.nfs is None) == (volume.lustre is None)
            ):
                self.add(
                    "shared-filesystem-source-missing",
                    "A shared file system on `aws_ec2` needs what machines mount: `nfs` or "
                    "`lustre`, not both.",
                    document,
                    (*path, "strategy"),
                    hint="Add `nfs: fs-0123.efs.us-east-1.amazonaws.com:/` for EFS, or "
                    "`lustre: fs-0123.fsx.us-east-1.amazonaws.com@tcp:/abcdefgh` for FSx for "
                    "Lustre.",
                )
            if (
                volume.strategy == "object_mount"
                and template.provisioner == "aws_ec2"
                and volume.bucket is None
            ):
                self.add(
                    "object-mount-bucket-missing",
                    "`object_mount` on `aws_ec2` needs the S3 `bucket` machines mount.",
                    document,
                    (*path, "strategy"),
                    hint="Add `bucket: <name>`, in the template's region, which its machines' "
                    "role can read, and write and delete under `path`.",
                )
            for option in ("iops", "throughput"):
                if getattr(volume, option) is not None and template.provisioner != "aws_ec2":
                    self.add(
                        "volume-option-aws-only",
                        f"`{option}` is for `aws_ec2`.",
                        document,
                        (*path, option),
                        key=True,
                    )
            if (
                volume.iops is not None
                and volume.throughput is not None
                and volume.throughput * 4 > volume.iops
            ):
                # gp3 gives at most 0.25 MiB/s for each IOPS provisioned.
                self.add(
                    "volume-throughput-above-iops",
                    f"A disk with {volume.iops} IOPS gives at most {volume.iops // 4} MiB/s.",
                    document,
                    (*path, "throughput"),
                    hint=f"Raise `iops` to at least {volume.throughput * 4}.",
                )
            if volume.snapshot_class is not None and volume.strategy != "clone_from_snapshot":
                self.add(
                    "snapshot-class-wrong-strategy",
                    "`snapshot_class` is for `clone_from_snapshot`.",
                    document,
                    (*path, "snapshot_class"),
                    key=True,
                )

    def gcp(self, template: Template, document: Document) -> None:
        """What a Compute Engine template must agree on that its schema can't say."""
        given = template.gcp
        region = str(given["region"])
        for index, zone in enumerate(given["zones"]):
            if not str(zone).startswith(f"{region}-"):
                self.add(
                    "zone-outside-region",
                    f"`{zone}` isn't in `{region}`.",
                    document,
                    ("gcp", "zones", index),
                    hint=f"Its zones are named {region}-a, {region}-b and so on.",
                )
        source = str(given.get("instance_template") or "")
        if "/regions/" in source and f"/regions/{region}/" not in source:
            self.add(
                "instance-template-outside-region",
                f"The instance template is in another region than `{region}`.",
                document,
                ("gcp", "instance_template"),
                hint="A regional instance template starts machines in its own region only.",
            )
        if given.get("gpu_type") and not template.capacity.gpu_count:
            self.add(
                "gpu-type-without-gpus",
                "`gpu_type` attaches `capacity.gpu.count` GPUs, and this template has none.",
                document,
                ("gcp", "gpu_type"),
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
        if binding.labels and "self_hosted" not in env.tiers:
            self.add(
                "labels-wrong-tier",
                f"Labels pick self-hosted machines, and `{binding.environment}` runs on "
                + ", ".join(env.tiers)
                + ".",
                document,
                ("labels",),
            )
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
                on_tier = [t for t in on_tier if t.serves(action.lifecycle)] or on_tier
            fitting = [t for t in on_tier if t.fits(wanted)]
            if not fitting:
                largest = max(on_tier, key=lambda t: (t.capacity.memory or 0, t.capacity.cpu or 0))
                self.add(
                    "action-does-not-fit",
                    f"`{action.qualified_name}` needs more than any {action.tier}-tier template "
                    f"bound to `{binding.environment}` has: " + _shortfall(largest, wanted) + ".",
                    document,
                    ("templates",),
                    hint="Bind a larger template.",
                )
            elif all(t.why_not(action) for t in fitting):
                self.cannot_run(action, binding, fitting, document)
            elif action.lifecycle is not None and not any(
                t.serves(action.lifecycle) for t in on_tier
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

    def cannot_run(
        self, action: Action, binding: Binding, fitting: list[Template], document: Document
    ) -> None:
        """An action that fits Slurm templates, none of which can run it (ADR 0133). A
        timeout no partition allows is a mistake; reaching the internet from a cluster is
        the deployment's to allow."""
        too_long = any(
            action.timeout > t.slurm.get("max_walltime", action.timeout) for t in fitting
        )
        self.add(
            "action-cannot-run-here",
            f"`{action.qualified_name}` can't run on any template bound to "
            f"`{binding.environment}`: " + "; ".join(str(t.why_not(action)) for t in fitting) + ".",
            document,
            ("templates",),
            severity=Severity.ERROR if too_long else Severity.WARNING,
            hint="Bind a template whose partition allows it, or shorten its timeout."
            if too_long
            else "Give the Slurm template the `egress_proxy_url` its compute nodes reach the "
            "egress proxy at, or bind another template.",
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
            if template.provisioner == "slurm":
                self.add(
                    "volume-on-slurm",
                    f"`{template.name}` is a Slurm partition, and Iki doesn't give Slurm jobs "
                    f"volumes yet, so it can't run `{binding.environment}`, which reads `{name}`.",
                    document,
                    path,
                    hint="Bind the environment to a template Iki starts machines on.",
                )
            elif given is None:
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
        "prices": {n: asdict(p) for n, p in sorted(compute.prices.items())},
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
                labels=tuple(b.get("labels", ())),
            )
            for n, b in data["bindings"].items()
        },
        # Deployments built before outside APIs price none.
        prices={n: ApiPrice(**p) for n, p in (data.get("prices") or {}).items()},
    )
