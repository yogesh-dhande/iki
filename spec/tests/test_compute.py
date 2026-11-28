"""A deployment's compute.yaml: templates, bindings, and checking them against its plugins."""

import json
import textwrap
from dataclasses import replace
from pathlib import Path

import pytest

from iki_spec.compute import (
    Compute,
    ComputeResult,
    ImageBinding,
    compute_from_dict,
    compute_to_dict,
    load_compute,
)
from iki_spec.model import ApiUse, Environment, Plugin, Resources
from iki_spec.problems import Severity
from iki_spec.validate import load_plugin

PLUGINS = Path(__file__).parent / "plugins"


@pytest.fixture(scope="module")
def plugins() -> list[Plugin]:
    names = {"dna": [], "protein": ["dna"], "structure_prediction": ["protein", "dna"]}
    out = []
    for name, deps in names.items():
        result = load_plugin(PLUGINS / name, dependencies=[PLUGINS / d for d in deps])
        assert result.plugin is not None
        out.append(result.plugin)
    return out


BACKEND = """\
apiVersion: iki/v1
kind: ComputeTemplate
name: backend-small
provisioner: backend_pool
capacity: { cpu: 1, memory: 2Gi }
kubernetes: { runtime_class: gvisor }
"""

A100 = """\
apiVersion: iki/v1
kind: ComputeTemplate
name: gpu-t4
provisioner: k8s_job
capacity: { cpu: 4, memory: 16Gi, gpu: { count: 1, memory: 16Gi, model: T4 } }
kubernetes: { node_selector: { iki.ikisci.org/pool: gpu } }
price: { per_hour: 0.53, currency: USD }
---
apiVersion: iki/v1
kind: ComputeTemplate
name: gpu-t4-warm
provisioner: k8s_deployment
capacity: { cpu: 4, memory: 16Gi, gpu: { count: 1, memory: 16Gi, model: T4 } }
limits: { min_warm: 1, idle_shutdown: 15m }
price: { per_hour: 0.53, currency: USD }
---
apiVersion: iki/v1
kind: ComputeTemplate
name: a100-spot
provisioner: aws_ec2
capacity:
  cpu: 96
  memory: 1152Gi
  gpu: { count: 8, memory: 40Gi, model: A100 }
  scratch_disk: 8Ti
aws:
  instance_type: p4d.24xlarge
  image: resolve:ssm:/aws/service/ecs/optimized-ami/amazon-linux-2023/gpu/recommended/image_id
  spot: true
  fallback: on_demand
  region: us-east-1
  subnets: [subnet-0a1b2c3d4e5f60718]
  security_groups: [sg-0a1b2c3d4e5f60718]
  instance_profile: iki-vm-worker
volumes: { af-db: { strategy: clone_from_snapshot, fast_snapshot_restore: true } }
limits: { max_concurrent: 10 }
price: { per_hour: 32.77, currency: USD }
"""

BINDINGS = """\
apiVersion: iki/v1
kind: EnvironmentBinding
environment: acme/protein.protein-tools
templates: [{ template: backend-small }]
---
apiVersion: iki/v1
kind: EnvironmentBinding
environment: acme/prediction.alphafold-gpu
templates: [{ template: a100-spot }]
---
apiVersion: iki/v1
kind: EnvironmentBinding
environment: acme/prediction.colabfold
templates: [{ template: gpu-t4 }, { template: gpu-t4-warm }]
---
apiVersion: iki/v1
kind: ApiPrice
api: acme/protein.fold
per_call: 0.02
usage: { gpu_seconds: 0.0011 }
currency: USD
"""


def load(tmp_path: Path, plugins: list[Plugin], *documents: str) -> ComputeResult:
    file = tmp_path / "compute.yaml"
    file.write_text("---\n".join(textwrap.dedent(d) for d in documents))
    return load_compute(file, plugins, "compute.yaml")


def codes(result: ComputeResult) -> list[str]:
    return [p.code for p in result.problems]


def only(result: ComputeResult, code: str) -> str:
    assert codes(result) == [code], [(p.code, p.message) for p in result.problems]
    return result.problems[0].message


CUSTOMER_IMAGE = "registry.example/team/tools@sha256:" + "a" * 64
CUSTOMER_ENVIRONMENT = "acme/tools.analysis-tools"
IMAGE_BINDING = f"""\
apiVersion: iki/v1
kind: EnvironmentImageBinding
environment: {CUSTOMER_ENVIRONMENT}
plugin_version: ">=1.2,<2"
image: {CUSTOMER_IMAGE}
pull_secret: private-registry
"""
CUSTOMER_JOB = """\
apiVersion: iki/v1
kind: ComputeTemplate
name: customer-job
provisioner: k8s_job
capacity: { cpu: 1, memory: 2Gi }
price: { per_hour: 0.10, currency: USD }
"""
CUSTOMER_TEMPLATES = f"""\
apiVersion: iki/v1
kind: EnvironmentBinding
environment: {CUSTOMER_ENVIRONMENT}
templates: [{{ template: customer-job }}]
labels: [local-site]
"""


@pytest.fixture
def customer_plugin() -> Plugin:
    env = Environment(
        "acme/tools",
        "analysis-tools",
        ("container", "self_hosted"),
        labels=("local-tools",),
        python_version="3.12",
        requirements="requirements.txt",
        customer_image_required=True,
    )
    return Plugin("acme/tools", "1.2.0-rc.1+dev", environments={env.name: env})


def test_image_bindings_follow_plugin_releases(tmp_path: Path, customer_plugin: Plugin) -> None:
    later = IMAGE_BINDING.replace('">=1.2,<2"', '">=2,<3"')
    result = load(
        tmp_path, [customer_plugin], CUSTOMER_JOB, CUSTOMER_TEMPLATES, IMAGE_BINDING, later
    )
    assert result.problems == []
    compiled = result.compute
    assert compiled is not None
    assert compiled.image_binding(CUSTOMER_ENVIRONMENT, customer_plugin.version) == ImageBinding(
        CUSTOMER_ENVIRONMENT, ">=1.2,<2", CUSTOMER_IMAGE, "private-registry"
    )
    assert compiled.image_binding(CUSTOMER_ENVIRONMENT, "1.1.9") is None
    assert compiled.image_binding("acme/tools.other", "1.2.0") is None
    assert compiled.bindings[CUSTOMER_ENVIRONMENT].labels == ("local-site",)
    assert len(compiled.image_bindings) == 2
    assert compute_from_dict(json.loads(json.dumps(compute_to_dict(compiled)))) == compiled
    old = {"apiVersion": "iki/v1", "templates": {}, "bindings": {}, "prices": {}}
    assert compute_from_dict(old) == Compute()
    assert compute_to_dict(Compute()) == old


def test_an_unmatched_image_binding_still_builds(tmp_path: Path, customer_plugin: Plugin) -> None:
    later = IMAGE_BINDING.replace('">=1.2,<2"', '">=2,<3"')
    result = load(tmp_path, [customer_plugin], CUSTOMER_JOB, CUSTOMER_TEMPLATES, later)
    assert only(result, "unbound-environment-image")
    assert result.ok
    assert result.compute is not None
    assert result.compute.image_binding(CUSTOMER_ENVIRONMENT, customer_plugin.version) is None


def test_overlapping_image_bindings_are_ambiguous(tmp_path: Path, customer_plugin: Plugin) -> None:
    overlap = IMAGE_BINDING.replace('">=1.2,<2"', '">=1,<3"')
    result = load(tmp_path, [customer_plugin], IMAGE_BINDING, overlap)
    assert customer_plugin.version in only(result, "overlapping-image-bindings")
    assert not result.ok
    first = ImageBinding(CUSTOMER_ENVIRONMENT, ">=1.2,<2", CUSTOMER_IMAGE)
    second = ImageBinding(CUSTOMER_ENVIRONMENT, ">=1,<3", CUSTOMER_IMAGE)
    assert (
        Compute(image_bindings=(first, second)).image_binding(CUSTOMER_ENVIRONMENT, "1.2") is None
    )


@pytest.mark.parametrize(
    ("old", "new", "code"),
    [
        (CUSTOMER_ENVIRONMENT, "acme/tools.unknown-tools", "unknown-environment"),
        (CUSTOMER_ENVIRONMENT, "analysis-tools", "schema"),
        (CUSTOMER_IMAGE, "registry.example/team/tools:latest", "schema"),
        (CUSTOMER_IMAGE, json.dumps(CUSTOMER_IMAGE + "\n"), "schema"),
        ('">=1.2,<2"', '"~=1.2"', "schema"),
        ("private-registry", "not/a-secret", "schema"),
        ("private-registry", json.dumps("private-registry\n"), "schema"),
    ],
)
def test_image_bindings_check_their_references(
    tmp_path: Path, customer_plugin: Plugin, old: str, new: str, code: str
) -> None:
    result = load(tmp_path, [customer_plugin], IMAGE_BINDING.replace(old, new))
    assert only(result, code)
    assert not result.ok


def test_an_image_binding_needs_an_opted_in_environment(
    tmp_path: Path, customer_plugin: Plugin
) -> None:
    env = customer_plugin.environments["analysis-tools"]
    pinned = replace(env, customer_image_required=False, image=CUSTOMER_IMAGE)
    plugin = replace(customer_plugin, environments={pinned.name: pinned})
    assert only(load(tmp_path, [plugin], IMAGE_BINDING), "environment-image-not-requested")


def test_image_pull_secrets_need_kubernetes_templates(
    tmp_path: Path, customer_plugin: Plugin
) -> None:
    on_slurm = CUSTOMER_TEMPLATES.replace("customer-job", "hpc-gpu")
    slurm_binding = load(tmp_path, [customer_plugin], SLURM, on_slurm, IMAGE_BINDING)
    assert "hpc-gpu" in only(slurm_binding, "image-pull-secret-wrong-provisioner")
    without_secret = IMAGE_BINDING.replace("pull_secret: private-registry\n", "")
    assert load(tmp_path, [customer_plugin], SLURM, on_slurm, without_secret).problems == []


def test_a_pull_secret_does_not_change_native_templates(
    tmp_path: Path, customer_plugin: Plugin
) -> None:
    env = customer_plugin.environments["analysis-tools"]
    mixed = replace(env, tiers=(*env.tiers, "backend"))
    plugin = replace(customer_plugin, environments={mixed.name: mixed})
    templates = CUSTOMER_TEMPLATES.replace(
        "[{ template: customer-job }]", "[{ template: customer-job }, { template: backend-small }]"
    )
    result = load(tmp_path, [plugin], CUSTOMER_JOB, BACKEND, templates, IMAGE_BINDING)
    assert result.problems == []
    assert result.ok


def test_a_deployment_that_runs_every_action(tmp_path: Path, plugins: list[Plugin]) -> None:
    result = load(tmp_path, plugins, BACKEND, A100, BINDINGS)
    assert result.problems == []
    assert result.compute is not None
    a100 = result.compute.templates["a100-spot"]
    assert (a100.tier, a100.lifecycle, a100.capacity.gpu_count) == ("vm", "ephemeral", 8)
    assert a100.capacity.memory == 1152 * 1024**3
    assert result.compute.bindings["acme/protein.protein-tools"].templates == (
        ("backend-small", 0),
    )
    data = json.loads(json.dumps(compute_to_dict(result.compute)))
    assert compute_from_dict(data) == result.compute
    assert data["templates"]["a100-spot"]["aws"]["instance_type"] == "p4d.24xlarge"
    assert data["bindings"]["acme/prediction.alphafold-gpu"]["templates"] == [["a100-spot", 0]]


def test_without_compute_yaml_only_the_client_tier_runs(
    tmp_path: Path, plugins: list[Plugin]
) -> None:
    result = load_compute(tmp_path / "compute.yaml", plugins, "compute.yaml")
    assert result.ok
    assert result.compute is not None
    assert result.compute.templates == {}
    assert [(p.code, p.severity) for p in result.problems] == [
        # Its outside API's calls cost nothing either (ADR 0130).
        ("unpriced-api", Severity.WARNING),
        ("unbound-environment", Severity.WARNING),
        ("unbound-environment", Severity.WARNING),
        ("unbound-environment", Severity.WARNING),
    ]
    messages = [p.message for p in result.problems]
    assert (
        "`acme/protein.protein-tools` has no template for the backend tier, so its actions there "
        "can't run on this deployment."
    ) in messages
    # Only the tiers its actions use: predict_structure runs on vm, not container.
    assert any(
        m.startswith("`acme/prediction.alphafold-gpu` has no template for the vm tier")
        for m in messages
    )


@pytest.mark.parametrize(
    ("document", "fragment"),
    [
        (BACKEND.replace("backend_pool", "lambda"), "provisioner"),
        (BACKEND.replace("cpu: 1, ", ""), "cpu"),
        (A100.replace("  instance_profile: iki-vm-worker\n", ""), "instance_profile"),
        (A100.replace("subnet-0a1b2c3d4e5f60718", "my-subnet"), "subnets"),
        (A100.replace("currency: USD", "currency: dollars"), "currency"),
        # A VM needs an image, and security groups of its own.
        (A100.replace("  image: resolve:ssm:", "  not_image: resolve:ssm:"), "image"),
        (A100.replace("  security_groups: [sg-0a1b2c3d4e5f60718]\n", ""), "security_groups"),
        (A100.replace("resolve:ssm:/aws", "https://example.com/aws"), "image"),
        (A100.replace("  spot: true", "  tags: { iki.ikisci.org/run: x }\n  spot: true"), "tags"),
        (A100.replace("  spot: true", "  tags: { 'aws:cost': x }\n  spot: true"), "tags"),
        ("apiVersion: iki/v1\nkind: EnvironmentBinding\nenvironment: x\ntemplates: []\n", "x"),
    ],
)
def test_the_schemas(tmp_path: Path, plugins: list[Plugin], document: str, fragment: str) -> None:
    result = load(tmp_path, plugins, document)
    assert result.compute is None
    assert codes(result)[0] == "schema"
    assert any(fragment in p.message + str(p.path) for p in result.problems)


@pytest.mark.parametrize(
    ("document", "message"),
    [
        (
            BACKEND + "lifecycle: persistent\n",
            "`lifecycle` is for `aws_ec2` and `gcp_gce` templates.",
        ),
        (
            BACKEND + "volumes: { db: { strategy: object_mount } }\n",
            "`volumes` is for `k8s_job` and `k8s_deployment` and `aws_ec2` and `gcp_gce` "
            "templates.",
        ),
        (
            A100 + "kubernetes: { namespace: gpu }\n",
            "`kubernetes` is for `backend_pool` and `k8s_job` and `k8s_deployment` templates.",
        ),
    ],
)
def test_keys_for_other_provisioners(
    tmp_path: Path, plugins: list[Plugin], document: str, message: str
) -> None:
    result = load(tmp_path, plugins, document)
    assert only(result, "wrong-provisioner") == message


def test_unknown_kinds(tmp_path: Path, plugins: list[Plugin]) -> None:
    result = load(tmp_path, plugins, "apiVersion: iki/v1\nkind: PluginLock\nplugins: []\n")
    assert "not `PluginLock`" in only(result, "unknown-kind")


def test_names_are_unique(tmp_path: Path, plugins: list[Plugin]) -> None:
    assert "first on line 1" in only(
        load(tmp_path, plugins, BACKEND, BACKEND), "duplicate-template"
    )
    binding = BINDINGS.split("---\n")[0]
    result = load(tmp_path, plugins, BACKEND, binding, binding)
    assert only(result, "duplicate-binding")


def test_bindings_name_real_environments_and_templates(
    tmp_path: Path, plugins: list[Plugin]
) -> None:
    binding = (
        "apiVersion: iki/v1\nkind: EnvironmentBinding\nenvironment: acme/protein.protein-tool\n"
        "templates: [{ template: backend-smal }]\n"
    )
    result = load(tmp_path, plugins, BACKEND, A100, BINDINGS.split("---\n")[1], binding)
    assert "unknown-environment" in codes(result)
    problem = next(p for p in result.problems if p.code == "unknown-environment")
    assert problem.hint == "Did you mean `acme/protein.protein-tools`?"
    fixed = binding.replace("protein-tool\n", "protein-tools\n")
    result = load(tmp_path, plugins, BACKEND, A100, BINDINGS.split("---\n")[1], fixed)
    problem = next(p for p in result.problems if p.code == "unknown-template")
    assert problem.message == "There's no compute template `backend-smal` in compute.yaml."
    assert problem.hint == "Did you mean `backend-small`?"


def test_a_binding_gives_self_hosted_runs_this_deployments_labels(
    tmp_path: Path, plugins: list[Plugin]
) -> None:
    labelled = (
        "apiVersion: iki/v1\nkind: EnvironmentBinding\nenvironment: acme/protein.protein-tools\n"
        "labels: [lab-3, chromeleon]\n"
    )
    others = "---\n".join(BINDINGS.split("---\n")[1:])
    result = load(tmp_path, plugins, BACKEND, A100, others, labelled)
    assert result.ok, [(p.code, p.message) for p in result.problems]
    assert result.compute is not None
    bound = result.compute.bindings["acme/protein.protein-tools"]
    assert (bound.templates, bound.labels) == ((), ("chromeleon", "lab-3"))
    data = json.loads(json.dumps(compute_to_dict(result.compute)))
    assert compute_from_dict(data) == result.compute
    # Templates and labels together, for an environment on both kinds of tier.
    both = BINDINGS.replace(
        "templates: [{ template: backend-small }]",
        "templates: [{ template: backend-small }]\nlabels: [lab-3]",
    )
    assert load(tmp_path, plugins, BACKEND, A100, both).problems == []
    # Labels pick self-hosted machines: not for an environment that never runs on one.
    vm = BINDINGS.replace(
        "templates: [{ template: a100-spot }]", "templates: [{ template: a100-spot }]\nlabels: [x]"
    )
    assert only(load(tmp_path, plugins, BACKEND, A100, vm), "labels-wrong-tier")
    # A binding gives templates, labels or both.
    empty = (
        "apiVersion: iki/v1\nkind: EnvironmentBinding\nenvironment: acme/protein.protein-tools\n"
    )
    assert not load(tmp_path, plugins, BACKEND, A100, others, empty).ok


def test_templates_match_the_environments_tiers(tmp_path: Path, plugins: list[Plugin]) -> None:
    binding = BINDINGS.replace(
        "templates: [{ template: backend-small }]", "templates: [{ template: a100-spot }]"
    )
    result = load(tmp_path, plugins, A100, binding.split("---\n")[0])
    assert "template-wrong-tier" in codes(result)


def test_templates_are_big_enough_for_the_environment(
    tmp_path: Path, plugins: list[Plugin]
) -> None:
    small = A100.replace("cpu: 96\n  memory: 1152Gi", "cpu: 4\n  memory: 32Gi").replace(
        "memory: 40Gi, model", "memory: 24Gi, model"
    )
    result = load(tmp_path, plugins, BACKEND, small, BINDINGS)
    message = only(result, "template-too-small")
    assert message == (
        "`a100-spot` is smaller than `acme/prediction.alphafold-gpu` needs: 8 CPUs (it has "
        "4), 64Gi of memory (it has 32Gi), 40Gi per GPU (it has 24Gi)."
    )


def test_every_action_fits_a_template(tmp_path: Path, plugins: list[Plugin]) -> None:
    # The environment needs 64Gi; predict_structure asks for 96Gi.
    tight = A100.replace("memory: 1152Gi", "memory: 64Gi")
    message = only(load(tmp_path, plugins, BACKEND, tight, BINDINGS), "action-does-not-fit")
    assert message.startswith("`acme/prediction.predict_structure` needs more than any vm")
    assert "96Gi of memory (it has 64Gi)" in message


def test_actions_prefer_their_lifecycle(tmp_path: Path, plugins: list[Plugin]) -> None:
    persistent = A100.replace("provisioner: aws_ec2", "provisioner: aws_ec2\nlifecycle: persistent")
    result = load(tmp_path, plugins, BACKEND, persistent, BINDINGS)
    assert only(result, "lifecycle-unbound")
    assert result.ok


def test_volumes_reach_the_machines(tmp_path: Path, plugins: list[Plugin]) -> None:
    without = A100.replace(
        "volumes: { af-db: { strategy: clone_from_snapshot, fast_snapshot_restore: true } }\n", ""
    )
    message = only(load(tmp_path, plugins, BACKEND, without, BINDINGS), "volume-strategy-missing")
    assert "volume `af-db`" in message
    exclusive = A100.replace(
        "strategy: clone_from_snapshot, fast_snapshot_restore: true", "strategy: attach_exclusive"
    )
    result = load(tmp_path, plugins, BACKEND, exclusive, BINDINGS)
    assert only(result, "volume-strategy-access")
    assert result.ok  # a warning: runs wait for each other


def test_template_rules(tmp_path: Path, plugins: list[Plugin]) -> None:
    gpu_pod = BACKEND.replace("memory: 2Gi }", "memory: 2Gi, gpu: { count: 1, memory: 16Gi } }")
    assert "gpu-wrong-tier" in codes(load(tmp_path, plugins, gpu_pod))
    warm = A100 + "".join([])  # ephemeral by default
    warm = warm.replace(
        "limits: { max_concurrent: 10 }", "limits: { min_warm: 1, idle_shutdown: 30m }"
    )
    result = load(tmp_path, plugins, BACKEND, warm, BINDINGS)
    assert codes(result) == ["limit-needs-persistent", "limit-needs-persistent"]
    no_spot = A100.replace("  spot: true\n", "")
    assert only(load(tmp_path, plugins, BACKEND, no_spot, BINDINGS), "fallback-without-spot")
    deployment = (
        "apiVersion: iki/v1\nkind: ComputeTemplate\nname: pool\nprovisioner: k8s_deployment\n"
        "capacity: { cpu: 8, memory: 64Gi }\n"
        "volumes: { af-db: { strategy: object_mount, claim: s3, fast_snapshot_restore: true,"
        " snapshot_class: ebs } }\n"
        "price: { per_hour: 0.4, currency: USD }\n"
    )
    result = load(tmp_path, plugins, BACKEND, A100, BINDINGS, deployment)
    assert sorted(codes(result)) == [
        "fast-restore-wrong-strategy",
        "snapshot-class-wrong-strategy",
        "unused-template",
    ]
    classes = A100.replace(
        "fast_snapshot_restore: true", "fast_snapshot_restore: true, storage_class: gp3"
    )
    loaded = load(tmp_path, plugins, BACKEND, classes, BINDINGS)
    assert only(loaded, "volume-option-kubernetes-only")
    fast = A100.replace("fast_snapshot_restore: true", "fast_snapshot_restore: true, iops: 16000")
    quick = fast.replace("16000", "16000, throughput: 1000")
    disks = load(tmp_path, plugins, BACKEND, quick, BINDINGS)
    assert codes(disks) == []
    assert disks.compute is not None
    assert disks.compute.templates["a100-spot"].volumes["af-db"].iops == 16000
    assert disks.compute.templates["a100-spot"].volumes["af-db"].throughput == 1000
    slow = load(
        tmp_path, plugins, BACKEND, fast.replace("16000", "3000, throughput: 1000"), BINDINGS
    )
    assert only(slow, "volume-throughput-above-iops")
    pods = deployment.replace(
        "object_mount, claim: s3, fast_snapshot_restore: true, snapshot_class: ebs",
        "object_mount, claim: s3, iops: 4000",
    )
    result = load(tmp_path, plugins, BACKEND, A100, BINDINGS, pods)
    assert sorted(codes(result)) == ["unused-template", "volume-option-aws-only"]
    shared = deployment.replace(
        "object_mount, claim: s3, fast_snapshot_restore: true, snapshot_class: ebs",
        "shared_filesystem",
    )
    result = load(tmp_path, plugins, BACKEND, A100, BINDINGS, shared)
    assert sorted(codes(result)) == ["shared-filesystem-claim-missing", "unused-template"]
    claimed = shared.replace("shared_filesystem", "shared_filesystem, claim: fsx, path: data/iki")
    loaded = load(tmp_path, plugins, BACKEND, A100, BINDINGS, claimed)
    assert codes(loaded) == ["unused-template"]
    assert loaded.compute is not None
    given = loaded.compute.templates["pool"].volumes["af-db"]
    assert (given.claim, given.root) == ("fsx", "data/iki")
    misplaced = A100.replace(
        "fast_snapshot_restore: true", "fast_snapshot_restore: true, claim: fsx"
    )
    assert only(
        load(tmp_path, plugins, BACKEND, misplaced, BINDINGS),
        "volume-option-shared-filesystem-only",
    )
    # On EC2: what machines mount, NFS or Lustre, never a claim.
    vm = A100.replace("clone_from_snapshot, fast_snapshot_restore: true", "shared_filesystem")
    assert only(load(tmp_path, plugins, BACKEND, vm, BINDINGS), "shared-filesystem-source-missing")
    lustre = "fs-0123.fsx.us-east-1.amazonaws.com@tcp:/abcdefgh"
    on_fsx = vm.replace("shared_filesystem", f"shared_filesystem, lustre: {lustre}")
    fsx = load(tmp_path, plugins, BACKEND, on_fsx, BINDINGS)
    assert codes(fsx) == []
    assert fsx.compute is not None
    given = fsx.compute.templates["a100-spot"].volumes["af-db"]
    assert given.filesystem == {"type": "lustre", "source": lustre}
    both = on_fsx.replace("shared_filesystem,", "shared_filesystem, nfs: efs.example.com:/,")
    assert only(
        load(tmp_path, plugins, BACKEND, both, BINDINGS), "shared-filesystem-source-missing"
    )
    claimed = on_fsx.replace("shared_filesystem,", "shared_filesystem, claim: fsx,")
    assert only(
        load(tmp_path, plugins, BACKEND, claimed, BINDINGS), "volume-option-shared-filesystem-only"
    )
    efs = vm.replace("shared_filesystem", "shared_filesystem, nfs: fs-0123.efs.amazonaws.com:/")
    assert codes(load(tmp_path, plugins, BACKEND, efs, BINDINGS)) == []
    objects = shared.replace("shared_filesystem", "object_mount")
    result = load(tmp_path, plugins, BACKEND, A100, BINDINGS, objects)
    assert sorted(codes(result)) == ["shared-filesystem-claim-missing", "unused-template"]
    bucket = objects.replace("object_mount", "object_mount, claim: s3-reference, path: iki")
    assert codes(load(tmp_path, plugins, BACKEND, A100, BINDINGS, bucket)) == ["unused-template"]
    lustre_bucket = bucket.replace("path: iki", "lustre: a@tcp:/b")
    assert "volume-option-shared-filesystem-only" in codes(
        load(tmp_path, plugins, BACKEND, A100, BINDINGS, lustre_bucket)
    )
    # On EC2, a bucket's name, never a claim.
    vm_objects = vm.replace("shared_filesystem", "object_mount")
    assert only(
        load(tmp_path, plugins, BACKEND, vm_objects, BINDINGS), "object-mount-bucket-missing"
    )
    on_s3 = vm_objects.replace("object_mount", "object_mount, bucket: acme-reference, path: iki")
    s3 = load(tmp_path, plugins, BACKEND, on_s3, BINDINGS)
    assert codes(s3) == []
    assert s3.compute is not None
    given = s3.compute.templates["a100-spot"].volumes["af-db"]
    assert given.filesystem == {"type": "s3", "source": "acme-reference"}
    claimed_s3 = on_s3.replace("object_mount,", "object_mount, claim: s3,")
    nfs_bucket = vm.replace("shared_filesystem", "shared_filesystem, nfs: e.com:/, bucket: acme")
    for wrong in (claimed_s3, nfs_bucket):
        assert "volume-option-shared-filesystem-only" in codes(
            load(tmp_path, plugins, BACKEND, wrong, BINDINGS)
        ), wrong
    pods_bucket = bucket.replace("claim: s3-reference", "claim: s3-reference, bucket: acme")
    assert "volume-option-shared-filesystem-only" in codes(
        load(tmp_path, plugins, BACKEND, A100, BINDINGS, pods_bucket)
    )
    misnamed = on_s3.replace("acme-reference", "Acme_Ref")
    assert not load(tmp_path, plugins, BACKEND, misnamed, BINDINGS).ok
    nfs_on_pods = shared.replace("shared_filesystem", "shared_filesystem, claim: f, nfs: e.com:/")
    assert "volume-option-shared-filesystem-only" in codes(
        load(tmp_path, plugins, BACKEND, A100, BINDINGS, nfs_on_pods)
    )


def test_a_pools_pods_each_get_a_clone_of_the_volume(tmp_path: Path, plugins: list[Plugin]) -> None:
    pool = (
        "apiVersion: iki/v1\nkind: ComputeTemplate\nname: pool\nprovisioner: k8s_deployment\n"
        "capacity: { cpu: 96, memory: 1152Gi, gpu: { count: 8, memory: 40Gi, model: A100 },"
        " scratch_disk: 500Gi }\n"
        "volumes: { af-db: { strategy: clone_from_snapshot, storage_class: ebs-gp3,"
        " snapshot_class: ebs } }\n"
        "price: { per_hour: 0.4, currency: USD }\n"
    )
    result = load(tmp_path, plugins, BACKEND, pool)
    assert "volume-strategy-unsupported" not in codes(result)
    assert result.compute is not None
    assert result.compute.templates["pool"].volumes["af-db"].snapshot_class == "ebs"


def test_costs_are_in_one_currency(tmp_path: Path, plugins: list[Plugin]) -> None:
    euros = A100.replace("per_hour: 32.77, currency: USD", "per_hour: 30.10, currency: EUR")
    message = only(load(tmp_path, plugins, BACKEND, euros, BINDINGS), "mixed-currencies")
    assert message == (
        "`a100-spot` is priced in EUR, but `gpu-t4` in USD: a deployment's costs and budgets "
        "are in one currency."
    )


def test_container_and_vm_templates_should_have_a_price(
    tmp_path: Path, plugins: list[Plugin]
) -> None:
    free = A100.replace("price: { per_hour: 32.77, currency: USD }\n", "")
    result = load(tmp_path, plugins, BACKEND, free, BINDINGS)
    assert only(result, "unpriced-template").startswith("`a100-spot` has no `price`")
    assert result.ok


def test_unused_templates_are_mentioned(tmp_path: Path, plugins: list[Plugin]) -> None:
    spare = BACKEND.replace("backend-small", "backend-large")
    result = load(tmp_path, plugins, BACKEND, spare, A100, BINDINGS)
    assert only(result, "unused-template") == "No environment is bound to `backend-large`."
    assert result.ok


def test_a_vms_disk_holds_its_scratch_space(tmp_path: Path, plugins: list[Plugin]) -> None:
    small = A100.replace("  spot: true", "  root_disk: 8Ti\n  spot: true")
    (problem,) = [
        p for p in load(tmp_path, plugins, small).problems if p.code == "root-disk-too-small"
    ]
    assert "at least 8Gi more" in problem.message
    big = A100.replace("  spot: true", "  root_disk: 9Ti\n  spot: true")
    assert "root-disk-too-small" not in codes(load(tmp_path, plugins, big))


GCE = """\
apiVersion: iki/v1
kind: ComputeTemplate
name: a100-gce
provisioner: gcp_gce
capacity:
  cpu: 96
  memory: 1360Gi
  gpu: { count: 8, memory: 40Gi, model: A100 }
  scratch_disk: 2Ti
gcp:
  machine_type: a2-highgpu-8g
  image: projects/cos-cloud/global/images/family/cos-stable
  spot: true
  fallback: on_demand
  region: us-central1
  zones: [us-central1-a, us-central1-b]
  labels: { cost-center: rd-42 }
price: { per_hour: 29.39, currency: USD }
"""


def test_a_compute_engine_template(tmp_path: Path, plugins: list[Plugin]) -> None:
    result = load(tmp_path, plugins, GCE)
    assert not [p for p in result.problems if p.is_error]
    assert result.compute is not None
    template = result.compute.templates["a100-gce"]
    assert (template.tier, template.lifecycle) == ("vm", "ephemeral")
    assert template.gcp["zones"] == ["us-central1-a", "us-central1-b"]
    data = json.loads(json.dumps(compute_to_dict(result.compute)))
    assert compute_from_dict(data) == result.compute


@pytest.mark.parametrize(
    ("change", "fragment"),
    [
        (("  machine_type: a2-highgpu-8g\n", ""), "machine_type"),
        (("[us-central1-a, us-central1-b]", "[]"), "zones"),
        (("projects/cos-cloud/global/images/family/cos-stable", "cos-stable"), "image"),
        (("{ cost-center: rd-42 }", "{ iki-run: x }"), "labels"),
        (("{ cost-center: rd-42 }", "{ cost-center: RD-42 }"), "labels"),
        (("  spot: true", "  subnets: [subnet-0a1b2c3d4e5f60718]\n  spot: true"), "subnets"),
    ],
)
def test_the_compute_engine_schema(
    tmp_path: Path, plugins: list[Plugin], change: tuple[str, str], fragment: str
) -> None:
    result = load(tmp_path, plugins, GCE.replace(*change))
    assert result.compute is None
    assert codes(result)[0] == "schema"
    assert any(fragment in p.message + str(p.path) for p in result.problems)


def test_compute_engine_rules(tmp_path: Path, plugins: list[Plugin]) -> None:
    elsewhere = GCE.replace("[us-central1-a, us-central1-b]", "[us-central1-a, europe-west4-a]")
    found = only(_errors(tmp_path, plugins, elsewhere), "zone-outside-region")
    assert "isn't in `us-central1`" in found
    template = GCE.replace(
        "  spot: true",
        "  instance_template: projects/acme-iki/regions/europe-west4/instanceTemplates/vm\n"
        "  spot: true",
    )
    assert only(_errors(tmp_path, plugins, template), "instance-template-outside-region")
    no_gpus = GCE.replace("  gpu: { count: 8, memory: 40Gi, model: A100 }\n", "").replace(
        "  spot: true", "  gpu_type: nvidia-tesla-t4\n  spot: true"
    )
    assert only(_errors(tmp_path, plugins, no_gpus), "gpu-type-without-gpus")
    no_spot = GCE.replace("  spot: true\n", "")
    assert only(_errors(tmp_path, plugins, no_spot), "fallback-without-spot")
    small = GCE.replace("  spot: true", "  root_disk: 2Ti\n  spot: true")
    assert only(_errors(tmp_path, plugins, small), "root-disk-too-small")
    assert only(
        _errors(tmp_path, plugins, GCE + "kubernetes: { namespace: gpu }\n"),
        "wrong-provisioner",
    )
    # Persistent disks from an image; no shared file systems or buckets yet.
    disks = GCE + "volumes: { af-db: { strategy: clone_from_snapshot } }\n"
    assert not _errors(tmp_path, plugins, disks).problems
    shared = GCE + "volumes: { af-db: { strategy: shared_filesystem } }\n"
    assert "volume-strategy-unsupported" in codes(_errors(tmp_path, plugins, shared))
    # Fast snapshot restore and provisioned IOPS are EBS's.
    fast = (
        GCE + "volumes: { af-db: { strategy: clone_from_snapshot, fast_snapshot_restore: true } }\n"
    )
    assert only(_errors(tmp_path, plugins, fast), "fast-restore-wrong-strategy")
    iops = GCE + "volumes: { af-db: { strategy: clone_from_snapshot, iops: 16000 } }\n"
    assert only(_errors(tmp_path, plugins, iops), "volume-option-aws-only")


SLURM = """\
apiVersion: iki/v1
kind: ComputeTemplate
name: hpc-gpu
provisioner: slurm
capacity: { cpu: 64, memory: 512Gi, gpu: { count: 4, memory: 80Gi, model: A100 } }
slurm:
  cluster: hpc1
  partition: gpu
  qos: normal
  account: rd-shared
  gpu_type: a100
  max_walltime: 2d
price: { per_cpu_hour: 0.02, per_gpu_hour: 1.10, per_memory_gib_hour: 0.001, currency: USD }
limits: { max_concurrent: 50 }
"""

ON_SLURM = """\
apiVersion: iki/v1
kind: EnvironmentBinding
environment: acme/prediction.colabfold
templates: [{ template: hpc-gpu }]
"""


def test_a_slurm_template(tmp_path: Path, plugins: list[Plugin]) -> None:
    result = load(tmp_path, plugins, SLURM, ON_SLURM)
    # A persistent action runs on a cluster too: every job is a fresh allocation. (The
    # test plugins' unpriced outside API warns wherever they're deployed.)
    others = {"unbound-environment", "unpriced-api"}
    assert [p.code for p in result.problems if p.code not in others] == []
    assert result.compute is not None
    template = result.compute.templates["hpc-gpu"]
    assert (template.tier, template.lifecycle) == ("container", "ephemeral")
    assert template.slurm == {
        "cluster": "hpc1",
        "partition": "gpu",
        "qos": "normal",
        "account": "rd-shared",
        "gpu_type": "a100",
        "max_walltime": 2 * 86400,
    }
    assert template.priced
    data = json.loads(json.dumps(compute_to_dict(result.compute)))
    assert compute_from_dict(data) == result.compute


def test_a_slurm_job_pays_for_what_its_given(tmp_path: Path, plugins: list[Plugin]) -> None:
    result = load(tmp_path, plugins, SLURM, ON_SLURM)
    assert result.compute is not None
    template = result.compute.templates["hpc-gpu"]
    wanted = Resources(cpu=8, memory=64 * 1024**3, gpu_count=2)
    assert template.per_hour(wanted) == pytest.approx(8 * 0.02 + 2 * 1.10 + 64 * 0.001)
    machine = load(tmp_path, plugins, A100).compute
    assert machine is not None
    assert machine.templates["gpu-t4"].per_hour(wanted) == 0.53


@pytest.mark.parametrize(
    ("change", "fragment"),
    [
        (("  cluster: hpc1\n", ""), "cluster"),
        (("  partition: gpu\n", ""), "partition"),
        (("cluster: hpc1", "cluster: HPC_1"), "cluster"),
        (("partition: gpu", "partition: 'gpu --uid 0'"), "partition"),
        (("max_walltime: 2d", "max_walltime: two days"), "max_walltime"),
        (("  gpu_type: a100\n", "  egress_proxy_url: https://proxy\n"), "egress_proxy_url"),
        (("  qos: normal\n", "  qos: normal\n  script: rm -rf /\n"), "script"),
    ],
)
def test_the_slurm_schema(
    tmp_path: Path, plugins: list[Plugin], change: tuple[str, str], fragment: str
) -> None:
    result = load(tmp_path, plugins, SLURM.replace(*change))
    assert result.compute is None
    assert codes(result)[0] == "schema"
    assert any(fragment in p.message + str(p.path) for p in result.problems)


def test_slurm_rules(tmp_path: Path, plugins: list[Plugin]) -> None:
    machine = SLURM.replace("per_cpu_hour: 0.02, ", "per_hour: 4, ")
    assert "A Slurm job pays for what it's given" in only(
        _errors(tmp_path, plugins, machine), "wrong-price"
    )
    by_cpu = A100.replace("per_hour: 32.77", "per_cpu_hour: 0.3", 1)
    assert "`per_cpu_hour` is for `slurm` templates" in only(
        _errors(tmp_path, plugins, by_cpu), "wrong-price"
    )
    bare = SLURM.replace(
        "price: { per_cpu_hour: 0.02, per_gpu_hour: 1.10, per_memory_gib_hour: 0.001, ",
        "price: { ",
    )
    assert only(_errors(tmp_path, plugins, bare), "price-without-rate")
    no_gpus = SLURM.replace(", gpu: { count: 4, memory: 80Gi, model: A100 }", "")
    assert only(_errors(tmp_path, plugins, no_gpus), "gpu-type-without-gpus")
    # A cluster's volumes would be built by a setup job on it, which doesn't exist yet.
    volumes = SLURM + "volumes: { af-db: { strategy: shared_filesystem } }\n"
    assert only(_errors(tmp_path, plugins, volumes), "wrong-provisioner")
    warm = SLURM.replace("limits: { max_concurrent: 50 }", "limits: { min_warm: 1 }")
    assert only(_errors(tmp_path, plugins, warm), "limit-needs-persistent")
    pods = SLURM + "kubernetes: { namespace: gpu }\n"
    assert only(_errors(tmp_path, plugins, pods), "wrong-provisioner")


def test_a_cluster_runs_nothing_that_reads_volumes(tmp_path: Path, plugins: list[Plugin]) -> None:
    on_cluster = ON_SLURM.replace("acme/prediction.colabfold", "acme/prediction.alphafold-gpu")
    roomy = SLURM.replace("memory: 512Gi,", "memory: 512Gi, scratch_disk: 1Ti,")
    result = load(tmp_path, plugins, roomy, on_cluster)
    (message,) = [p.message for p in result.problems if p.code == "volume-on-slurm"]
    assert message == (
        "`hpc-gpu` is a Slurm partition, and Iki doesn't give Slurm jobs volumes yet, so it "
        "can't run `acme/prediction.alphafold-gpu`, which reads `af-db`."
    )


def test_an_action_must_fit_a_partitions_time_limit(tmp_path: Path, plugins: list[Plugin]) -> None:
    # predict_with_colabfold may run an hour.
    short = SLURM.replace("max_walltime: 2d", "max_walltime: 30m")
    result = load(tmp_path, plugins, short, ON_SLURM)
    (problem,) = [p for p in result.problems if p.code == "action-cannot-run-here"]
    assert problem.is_error
    assert problem.message == (
        "`acme/prediction.predict_with_colabfold` can't run on any template bound to "
        "`acme/prediction.colabfold`: its timeout (1h) is longer than `hpc-gpu`'s partition "
        "allows (30m)."
    )
    # Another template it fits on runs it instead.
    both = ON_SLURM.replace(
        "[{ template: hpc-gpu }]", "[{ template: hpc-gpu }, { template: gpu-t4 }]"
    )
    assert "action-cannot-run-here" not in codes(load(tmp_path, plugins, short, A100, both))


def test_a_cluster_runs_what_reaches_the_internet_only_through_the_proxy(
    tmp_path: Path, plugins: list[Plugin]
) -> None:
    result = load(tmp_path, plugins, SLURM, ON_SLURM)
    assert result.compute is not None
    template = result.compute.templates["hpc-gpu"]
    fetch = replace(plugins[2].actions["predict_with_colabfold"], network=("rest.uniprot.org",))
    assert template.why_not(fetch) == (
        "it reaches the internet, and `hpc-gpu`'s compute nodes can't reach the egress proxy"
    )
    reached = replace(
        template, slurm={**template.slurm, "egress_proxy_url": "http://proxy.hpc:3128"}
    )
    assert reached.why_not(fetch) is None
    # Outside APIs are called through the proxy too (ADR 0130).
    calls = replace(plugins[2].actions["predict_with_colabfold"], apis=(ApiUse(name="fold"),))
    assert template.why_not(calls) == (
        "it calls outside APIs, and `hpc-gpu`'s compute nodes can't reach the egress proxy"
    )
    assert reached.why_not(calls) is None


def _errors(tmp_path: Path, plugins: list[Plugin], document: str) -> ComputeResult:
    result = load(tmp_path, plugins, document)
    result.problems = [p for p in result.problems if p.is_error]
    return result


def test_compute_documents_take_annotations_and_x_keys(
    tmp_path: Path, plugins: list[Plugin]
) -> None:
    notes = "annotations: { acme.com/owner: Research IT }\nx-acme-cost-centre: '4410'\n"
    result = load(tmp_path, plugins, BACKEND + notes)
    assert result.ok, result.problems
