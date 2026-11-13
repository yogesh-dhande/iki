"""A deployment's compute.yaml: templates, bindings, and checking them against its plugins."""

import json
import textwrap
from pathlib import Path

import pytest

from iki_spec.compute import ComputeResult, compute_from_dict, compute_to_dict, load_compute
from iki_spec.model import Plugin
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
environment: protein.protein-tools
templates: [{ template: backend-small }]
---
apiVersion: iki/v1
kind: EnvironmentBinding
environment: structure_prediction.alphafold-gpu
templates: [{ template: a100-spot }]
---
apiVersion: iki/v1
kind: EnvironmentBinding
environment: structure_prediction.colabfold
templates: [{ template: gpu-t4 }, { template: gpu-t4-warm }]
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


def test_a_deployment_that_runs_every_action(tmp_path: Path, plugins: list[Plugin]) -> None:
    result = load(tmp_path, plugins, BACKEND, A100, BINDINGS)
    assert result.problems == []
    assert result.compute is not None
    a100 = result.compute.templates["a100-spot"]
    assert (a100.tier, a100.lifecycle, a100.capacity.gpu_count) == ("vm", "ephemeral", 8)
    assert a100.capacity.memory == 1152 * 1024**3
    assert result.compute.bindings["protein.protein-tools"].templates == (("backend-small", 0),)
    data = json.loads(json.dumps(compute_to_dict(result.compute)))
    assert compute_from_dict(data) == result.compute
    assert data["templates"]["a100-spot"]["aws"]["instance_type"] == "p4d.24xlarge"
    assert data["bindings"]["structure_prediction.alphafold-gpu"]["templates"] == [["a100-spot", 0]]


def test_without_compute_yaml_only_the_client_tier_runs(
    tmp_path: Path, plugins: list[Plugin]
) -> None:
    result = load_compute(tmp_path / "compute.yaml", plugins, "compute.yaml")
    assert result.ok
    assert result.compute is not None
    assert result.compute.templates == {}
    assert [(p.code, p.severity) for p in result.problems] == [
        ("unbound-environment", Severity.WARNING),
        ("unbound-environment", Severity.WARNING),
        ("unbound-environment", Severity.WARNING),
    ]
    assert result.problems[0].message == (
        "`protein.protein-tools` has no template for the backend tier, so its actions there "
        "can't run on this deployment."
    )
    # Only the tiers its actions use: predict_structure runs on vm, not container.
    assert "no template for the vm tier" in result.problems[1].message


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
        (BACKEND + "lifecycle: persistent\n", "`lifecycle` is for `aws_ec2` templates."),
        (
            BACKEND + "volumes: { db: { strategy: object_mount } }\n",
            "`volumes` is for `k8s_job` and `k8s_deployment` and `aws_ec2` templates.",
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
        "apiVersion: iki/v1\nkind: EnvironmentBinding\nenvironment: protein.protein-tool\n"
        "templates: [{ template: backend-smal }]\n"
    )
    result = load(tmp_path, plugins, BACKEND, A100, BINDINGS.split("---\n")[1], binding)
    assert "unknown-environment" in codes(result)
    problem = next(p for p in result.problems if p.code == "unknown-environment")
    assert problem.hint == "Did you mean `protein.protein-tools`?"
    fixed = binding.replace("protein-tool\n", "protein-tools\n")
    result = load(tmp_path, plugins, BACKEND, A100, BINDINGS.split("---\n")[1], fixed)
    problem = next(p for p in result.problems if p.code == "unknown-template")
    assert problem.message == "There's no compute template `backend-smal` in compute.yaml."
    assert problem.hint == "Did you mean `backend-small`?"


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
        "`a100-spot` is smaller than `structure_prediction.alphafold-gpu` needs: 8 CPUs (it has "
        "4), 64Gi of memory (it has 32Gi), 40Gi per GPU (it has 24Gi)."
    )


def test_every_action_fits_a_template(tmp_path: Path, plugins: list[Plugin]) -> None:
    # The environment needs 64Gi; predict_structure asks for 96Gi.
    tight = A100.replace("memory: 1152Gi", "memory: 64Gi")
    message = only(load(tmp_path, plugins, BACKEND, tight, BINDINGS), "action-does-not-fit")
    assert message.startswith("`structure_prediction.predict_structure` needs more than any vm")
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
