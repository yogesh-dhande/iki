"""Iki deployed for real on AWS, checked end to end, then torn down.

    IKI_AWS_ROLE_ARN=<role> IKI_AWS_TOKEN_COMMAND=<command> make check-aws-live

It follows deploy/aws/production/README.md's runbook in the role's account: the
bootstrap, the infrastructure (made small: two system nodes, a runs pool from none to one,
the smallest Multi-AZ database, one NAT gateway), the AWS Load Balancer Controller, the
Cluster Autoscaler, ExternalDNS and External Secrets, the secrets and database roles, and
Iki's chart, with server and worker images CodeBuild makes there. Iki is served by an
internal ALB only, named in the VPC's private zone, with a certificate this test makes and
imports into ACM; the test reaches it by `kubectl port-forward`, its backend worker
through the ALB. Then it checks what a deployment must do, and always tears it down with
deploy/aws/scripts/teardown.sh, even when setting up or a check failed. Last, it lists the
account again and fails if anything of the deployment's is left but what AWS keeps (KMS
keys pending deletion, and service-linked roles AWS made for its services). A run that
passes whole, of a clean tree, is recorded in deploy/live-runs.json
(deploy/live/live_runs.py): commit it.

It costs: EKS's fee and two or three small nodes, a Multi-AZ db.t4g.micro, a NAT gateway,
nine interface endpoints in two zones, an internal ALB, a CodeBuild build and a dozen KMS
keys for an hour: a few US dollars for its two hours or less (``make aws-live-cost`` after,
``aws_cost.py --estimate`` before). It never runs on CI (conftest.py), and never longer
than IKI_AWS_CAP_HOURS (2) before it tears down. What it shares with the Google Cloud one
is deploy/live/iki_live.py.

Who it acts as is IKI_AWS_ROLE_ARN, assumed with the web identity token
IKI_AWS_TOKEN_COMMAND prints (its last line: an OpenID Connect ID token the role trusts,
such as a Google service account's, audience sts.amazonaws.com), run again every 20
minutes (``aws_inventory.Aws``). No AWS keys, and none of your AWS configuration, are used.
Other settings:

- IKI_AWS_NAME: the deployment's name; by default each run gets its own,
  ``iki-test-<MMDDHHMM>`` (UTC), since some names can't be used again for a while (KMS
  aliases' keys wait 30 days, the database's final snapshot). Everything it makes is named
  for it, and tagged ``iki-test=true`` and ``iki-deployment=<name>``.
- IKI_AWS_REGION, IKI_AWS_DR_REGION, IKI_AWS_ZONES (comma separated): us-east-1,
  us-west-2, and the region's a and b zones by default.
- IKI_AWS_SYSTEM_TYPE, IKI_AWS_RUNS_TYPE, IKI_AWS_DATABASE_CLASS: m7i-flex.large,
  c7i-flex.large and db.t4g.micro, which an account on AWS's free plan may use.
- IKI_AWS_CAP_HOURS: how long a run may go before tearing down starts, 2 by default.
- IKI_AWS_WORKDIR: where it keeps OpenTofu's state, the kubeconfig and the certificate,
  ``build/aws-live/<name>`` by default. If a run is killed before tearing down, run
  ``IKI_TEARDOWN_YES=1 deploy/aws/scripts/teardown.sh <name> --bootstrap --purge``
  from there (build/aws-live/last-run holds the last run's name), as the role.
"""

import datetime
import ipaddress
import json
import shutil
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import aws_cost
import iki_live
import pytest
import requests
import yaml
from aws_inventory import Aws, Inventory, Settings, expected, inventory, leftovers
from iki_live import REPO, Iki, until

pytestmark = pytest.mark.aws_live

#: Bumped when what the suite checks changes: recorded with each passing run.
SUITE_VERSION = "1"
#: The cluster's controllers, as the runbook installs them: (repository, chart, version).
LOAD_BALANCER_CONTROLLER = (
    "https://aws.github.io/eks-charts",
    "aws-load-balancer-controller",
    "3.6.3",
)
CLUSTER_AUTOSCALER = ("https://kubernetes.github.io/autoscaler", "cluster-autoscaler", "9.60.0")
EXTERNAL_DNS = ("https://kubernetes-sigs.github.io/external-dns", "external-dns", "1.23.0")
#: Iki's secrets External Secrets copies in (not iki-run-secrets: with no plugin secrets,
#: its JSON object is empty, and External Secrets makes no Secret of nothing).
SECRETS = (
    "iki-secret-key",
    "iki-encryption-keys",
    "iki-bundle-signing-key",
    "iki-database",
    "iki-database-migrator",
)
IMAGES = ("iki-server-base", "iki-server", "iki-worker-base", "iki-worker")


def tags(s: Settings) -> dict[str, str]:
    """Every run's tags, besides the modules' iki.ikisci.org/deployment=<name>."""
    return {"iki-test": "true", "iki-deployment": s.name}


def cli_tags(s: Settings, key: str = "Key", value: str = "Value") -> list[str]:
    return [f"{key}={k},{value}={v}" for k, v in tags(s).items()]


# --- The deployment --------------------------------------------------------------------------


@dataclass
class Deployment(iki_live.Live):
    settings: Settings = None  # type: ignore[assignment]

    @property
    def aws(self) -> Aws:
        assert isinstance(self.tools, Aws)
        return self.tools

    def inventory(self) -> Inventory:
        return inventory(self.aws)

    def preconditions(self) -> None:
        check_preconditions(self.aws, self.settings, self.before)

    def stand_up(self) -> None:
        stand_up(self)

    def diagnose(self) -> None:
        iki_live.diagnose(self.aws, "public.ecr.aws/docker/library/postgres:17")

    def leftovers(self, before: Inventory, after: Inventory) -> list[str]:
        problems = leftovers(before, after, self.settings.name)
        kept = expected(before, after, self.settings.name)
        (self.workdir / "aws-expected-leftovers.txt").write_text("\n".join(kept) + "\n")
        return problems

    def cost_report(self, start: datetime.datetime) -> str:
        return cost_report(self, start)

    def teardown_steps(self) -> list[tuple[str, Callable[[], object]]]:
        s, aws = self.settings, self.aws
        before = self.before
        return [
            ("the facts the cost report needs", lambda: record_facts(aws, s)),
            ("teardown.sh", lambda: run_teardown(aws, s)),
            ("the images and their build", lambda: delete_build(aws, s)),
            (
                "the internal certificate",
                lambda: delete_certificates(aws, s),
            ),
            (
                "RDS's monitoring log group, if the run made it",
                lambda: delete_rds_os_metrics(aws, s, before),
            ),
        ]


def run_teardown(aws: Aws, s: Settings) -> None:
    """teardown.sh, and once more if it fails: it skips what's gone, so a second run
    finishes what a passing failure (an API's hiccup, a slow network interface) stopped."""
    for attempt in (1, 2):
        try:
            aws.stream(
                "deploy/aws/scripts/teardown.sh",
                s.name,
                "--bootstrap",
                "--purge",
                log="teardown.log",
                timeout=5400,
            )
            return
        except AssertionError:
            if attempt == 2:
                raise


def build_names(aws: Aws, s: Settings) -> dict[str, str]:
    account = aws.account()
    return {
        "bucket": f"{s.name}-build-{account}",
        "project": f"{s.name}-images",
        "role": f"{s.name}-codebuild",
        "registry": f"{account}.dkr.ecr.{s.region}.amazonaws.com",
        "logs": f"/aws/codebuild/{s.name}-images",
    }


def delete_build(aws: Aws, s: Settings) -> None:
    """What the test made to build the images: ECR's repositories, CodeBuild's project,
    its role and logs, and the bucket the source went up in."""
    names = build_names(aws, s)
    for image in IMAGES:
        aws.run(
            "aws",
            "ecr",
            "delete-repository",
            "--repository-name",
            f"{s.name}/{image}",
            "--force",
            check=False,
        )
    aws.run("aws", "codebuild", "delete-project", "--name", names["project"], check=False)
    aws.run(
        "aws",
        "iam",
        "delete-role-policy",
        "--role-name",
        names["role"],
        "--policy-name",
        "build",
        check=False,
    )
    aws.run("aws", "iam", "delete-role", "--role-name", names["role"], check=False)
    aws.run("aws", "logs", "delete-log-group", "--log-group-name", names["logs"], check=False)
    aws.run("aws", "s3", "rm", f"s3://{names['bucket']}", "--recursive", check=False)
    aws.run("aws", "s3api", "delete-bucket", "--bucket", names["bucket"], check=False)


def delete_certificates(aws: Aws, s: Settings) -> None:
    listed = aws.aws_json("acm", "list-certificates")["CertificateSummaryList"]
    for arn in [c["CertificateArn"] for c in listed if c["DomainName"] == s.host]:

        def deleted(arn: str = arn) -> bool:
            run = aws.run("aws", "acm", "delete-certificate", "--certificate-arn", arn, check=False)
            return run.returncode == 0

        # ACM lets go of a certificate a few minutes after its load balancer goes.
        until(f"ACM to let go of {s.host}'s certificate", deleted, 900, every=30)


def delete_rds_os_metrics(aws: Aws, s: Settings, before: Inventory | None) -> None:
    """RDS's enhanced monitoring writes to RDSOSMetrics, a log group it makes for the
    region's every database: deleted only if the run made it."""
    if before is not None and f"{s.region} RDSOSMetrics" not in before.listings.get(
        "log groups", set()
    ):
        aws.run("aws", "logs", "delete-log-group", "--log-group-name", "RDSOSMetrics", check=False)


def record_facts(aws: Aws, s: Settings) -> None:
    """What the cost report needs to know before it's gone: each billable resource of the
    deployment's, and how much it carried (aws_cost.py)."""
    path = s.workdir / "aws-facts.json"
    facts: dict[str, Any] = json.loads(path.read_text()) if path.exists() else {}
    window = json.loads((s.workdir / "aws-window.json").read_text())
    resources: dict[str, list[dict[str, Any]]] = {}
    facts["resources"] = resources
    region = s.region

    def q(*args: str, where: str = region) -> Any:
        return aws.aws_json(*args, region=where)

    if s.name in q("eks", "list-clusters")["clusters"]:
        resources["eks cluster"] = [{"id": s.name, "region": region}]
    instances = q(
        "ec2", "describe-instances", "--filters", f"Name=tag:eks:cluster-name,Values={s.name}"
    )
    resources["instance"] = []
    for i in (i for r in instances["Reservations"] for i in r["Instances"]):
        volumes = q(
            "ec2",
            "describe-volumes",
            "--filters",
            f"Name=attachment.instance-id,Values={i['InstanceId']}",
        )["Volumes"]
        resources["instance"].append(
            {
                "id": i["InstanceId"],
                "type": i["InstanceType"],
                "launched": i["LaunchTime"],
                "disk_gib": sum(v["Size"] for v in volumes) or 52,
                "region": region,
            }
        )
    vpc = aws.run("tofu", "-chdir=deploy/aws/production", "output", "-raw", "vpc_id", check=False)
    vpc_id = vpc.stdout.strip() if vpc.returncode == 0 and vpc.stdout.startswith("vpc-") else ""
    if vpc_id:
        on_vpc = ("--filter", f"Name=vpc-id,Values={vpc_id}")
        nats = [
            n
            for n in q("ec2", "describe-nat-gateways", *on_vpc)["NatGateways"]
            if n["State"] != "deleted"
        ]
        resources["nat gateway"] = [
            {
                "id": n["NatGatewayId"],
                "region": region,
                "bytes": nat_bytes(aws, s, n["NatGatewayId"], window["start"]),
            }
            for n in nats
        ]
        resources["address"] = [
            {"id": a["AllocationId"], "region": region}
            for n in nats
            for a in n.get("NatGatewayAddresses", [])
            if a.get("AllocationId")
        ]
        endpoints = q("ec2", "describe-vpc-endpoints", "--filters", f"Name=vpc-id,Values={vpc_id}")
        resources["vpc endpoint"] = [
            {
                "id": e["VpcEndpointId"],
                "label": e["ServiceName"].rsplit(".", 1)[-1],
                "zones": len(e.get("SubnetIds", [])),
                "region": region,
            }
            for e in endpoints["VpcEndpoints"]
            if e["VpcEndpointType"] == "Interface"
        ]
    balancers = q(
        "resourcegroupstaggingapi",
        "get-resources",
        "--resource-type-filters",
        "elasticloadbalancing:loadbalancer",
        "--tag-filters",
        f"Key=elbv2.k8s.aws/cluster,Values={s.name}",
    )["ResourceTagMappingList"]
    resources["load balancer"] = [
        {"id": b["ResourceARN"], "label": b["ResourceARN"].split("/")[-2], "region": region}
        for b in balancers
    ]
    found = aws.run(
        "aws",
        "rds",
        "describe-db-instances",
        "--db-instance-identifier",
        s.name,
        "--output",
        "json",
        check=False,
    )
    if found.returncode == 0:
        db = json.loads(found.stdout)["DBInstances"][0]
        resources["db instance"] = [
            {
                "id": s.name,
                "class": db["DBInstanceClass"],
                "storage_gib": db["AllocatedStorage"],
                "region": region,
            }
        ]
    resources["kms key"] = []
    resources["secret"] = []
    for where in s.regions:
        for key in q("kms", "list-keys", where=where)["Keys"]:
            meta = q("kms", "describe-key", "--key-id", key["KeyId"], where=where)["KeyMetadata"]
            if meta.get("Description", "").startswith(f"{s.name}:"):
                resources["kms key"].append(
                    {"id": meta["KeyId"], "label": meta["Description"], "region": where}
                )
    for secret in q("secretsmanager", "list-secrets", "--filters", f"Key=name,Values={s.name}/")[
        "SecretList"
    ]:
        resources["secret"].append(
            {
                "id": secret["Name"],
                "replicas": len(secret.get("ReplicationStatus") or []),
                "region": region,
            }
        )
    zones = q("route53", "list-hosted-zones-by-name", "--dns-name", s.host)["HostedZones"]
    resources["hosted zone"] = [
        {"id": z["Id"].rsplit("/", 1)[-1], "label": z["Name"], "region": region}
        for z in zones
        if z["Name"] == f"{s.host}."
    ]
    facts["log_bytes"] = {}
    for where in s.regions:
        for group in q("logs", "describe-log-groups", where=where)["logGroups"]:
            if s.name in group["logGroupName"] or group["logGroupName"] == "RDSOSMetrics":
                facts["log_bytes"][f"{where} {group['logGroupName']}"] = metric_sum(
                    aws,
                    where,
                    "AWS/Logs",
                    "IncomingBytes",
                    "LogGroupName",
                    group["logGroupName"],
                    window["start"],
                )
    facts["builds"] = stop_and_time_builds(aws, s)
    facts["image_bytes"] = 0
    for image in IMAGES:
        listed = aws.run(
            "aws",
            "ecr",
            "describe-images",
            "--repository-name",
            f"{s.name}/{image}",
            "--output",
            "json",
            check=False,
        )
        if listed.returncode == 0:
            facts["image_bytes"] += sum(
                d.get("imageSizeInBytes", 0) for d in json.loads(listed.stdout)["imageDetails"]
            )
    path.write_text(json.dumps(facts, indent=1))


def stop_and_time_builds(aws: Aws, s: Settings, within: float = 600) -> list[dict[str, Any]]:
    """Every build of the run's project, each stopped first if it still runs: a stand-up
    that failed before the images were done leaves CodeBuild building, and billing by the
    minute, until it ends."""
    project = build_names(aws, s)["project"]
    listed = aws.run(
        "aws",
        "codebuild",
        "list-builds-for-project",
        "--project-name",
        project,
        "--output",
        "json",
        check=False,
    )
    ids = json.loads(listed.stdout)["ids"] if listed.returncode == 0 else []
    if not ids:
        return []

    def builds() -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = aws.aws_json("codebuild", "batch-get-builds", "--ids", *ids)[
            "builds"
        ]
        return found

    for build in builds():
        if build["buildStatus"] == "IN_PROGRESS":
            aws.run("aws", "codebuild", "stop-build", "--id", build["id"], check=False)

    def all_ended() -> list[dict[str, Any]] | None:
        found = builds()
        return found if all(b["buildStatus"] != "IN_PROGRESS" for b in found) else None

    ended = until("CodeBuild to stop the run's builds", all_ended, within, every=15)
    return [{"id": b["id"], "seconds": build_seconds(b)} for b in ended]


def build_seconds(build: dict[str, Any]) -> int:
    started = datetime.datetime.fromisoformat(build["startTime"])
    ended = datetime.datetime.fromisoformat(build["endTime"])
    return round((ended - started).total_seconds())


def metric_sum(
    aws: Aws, region: str, namespace: str, metric: str, dimension: str, value: str, start: str
) -> float:
    found = aws.aws_json(
        "cloudwatch",
        "get-metric-statistics",
        "--namespace",
        namespace,
        "--metric-name",
        metric,
        "--dimensions",
        f"Name={dimension},Value={value}",
        "--start-time",
        start,
        "--end-time",
        datetime.datetime.now(datetime.UTC).isoformat(),
        "--period",
        "3600",
        "--statistics",
        "Sum",
        region=region,
    )
    return float(sum(p["Sum"] for p in found["Datapoints"]))


def nat_bytes(aws: Aws, s: Settings, nat: str, start: str) -> float:
    """What a NAT gateway processed: both ways, from the VPC and back to it."""
    return sum(
        metric_sum(aws, s.region, "AWS/NATGateway", m, "NatGatewayId", nat, start)
        for m in ("BytesInFromSource", "BytesInFromDestination")
    )


def cost_report(d: Deployment, start: datetime.datetime) -> str:
    """What the run cost, at list prices (aws_cost.py), in the work directory."""
    s = d.settings
    end = datetime.datetime.now(datetime.UTC)
    (s.workdir / "aws-window.json").write_text(
        json.dumps({"start": start.isoformat(), "end": end.isoformat()})
    )
    window = aws_cost.Window(s.name, s.regions, start, end)
    text = aws_cost.cost(d.aws.aws_json, window, s.workdir)
    d.cost_usd = json.loads((s.workdir / "aws-cost.json").read_text())["total"]
    return text


def check_preconditions(aws: Aws, s: Settings, before: Inventory) -> None:
    """Refuses to start where applying would change what isn't the test's."""
    for name in ("aws", "tofu", "kubectl", "helm", "jq", "curl", "openssl", "uv", "git"):
        iki_live.tool(name)
    me = aws.aws_json("sts", "get-caller-identity")["Arn"]
    assert f":assumed-role/{s.role_arn.rsplit('/', 1)[-1]}/" in me, f"acting as {me}, not the role"
    taken = [
        line
        for found in before.sections().values()
        for line in found
        if s.name in line and not line.endswith(("PendingDeletion", "DELETING"))
    ]
    assert not taken, f"{s.name}'s resources already exist; tear them down first: {taken[:10]}"
    for kind in (s.system_type, s.runs_type):
        offered = aws.aws_json(
            "ec2",
            "describe-instance-type-offerings",
            "--location-type",
            "availability-zone",
            "--filters",
            f"Name=instance-type,Values={kind}",
        )["InstanceTypeOfferings"]
        missing = set(s.zones) - {o["Location"] for o in offered}
        assert not missing, f"{kind} isn't offered in {sorted(missing)}: set IKI_AWS_ZONES"


def variables(s: Settings, aws: Aws, state_key: str, certificate: str, me: str) -> dict[str, Any]:
    """The production root's variables: the runbook's, as small as the modules allow."""
    return {
        "name": s.name,
        "region": s.region,
        "dr_region": s.dr_region,
        "zones": list(s.zones),
        "kubernetes_version": kubernetes_version(aws),
        "single_nat_gateway": True,
        # The role this runs as administers the cluster, from this machine's address only.
        "admin_principal_arns": [s.role_arn],
        "public_access_cidrs": [me],
        "node_pools": {
            "system": {
                "instance_types": [s.system_type],
                "min_size": 2,
                "max_size": 3,
                "disk_size_gib": 50,
            },
            "runs": {
                "instance_types": [s.runs_type],
                "min_size": 0,
                "max_size": 1,
                "disk_size_gib": 50,
            },
        },
        "database": {
            "instance_class": s.database_class,
            "allocated_storage_gib": 20,
            "max_allocated_storage_gib": 40,
            "backup_retention_days": 7,
        },
        "secrets_recovery_window_days": 7,
        "state_kms_key_arn": state_key,
        "public_host": s.host,
        "certificate_arn": certificate,
        "internal": True,
        "optional_secrets": ["backend-worker-token"],
        "tags": tags(s),
    }


def kubernetes_version(aws: Aws) -> str:
    """EKS's default version: the one standard support has longest."""
    versions = aws.aws_json("eks", "describe-cluster-versions", "--default-only")["clusterVersions"]
    version: str = versions[0]["clusterVersion"]
    major, minor = (int(p) for p in version.split(".")[:2])
    assert (major, minor) >= (1, 33), f"EKS's default is {version}"
    return version


def public_address() -> str:
    """This machine's public address, the one network the cluster's API admits."""
    found = requests.get("https://checkip.amazonaws.com", timeout=30).text.strip()
    return f"{ipaddress.IPv4Address(found)}/32"


#: CodeBuild's own Docker daemon builds each image in a BuildKit container of its own
#: (buildx's docker-container driver), as Cloud Build does for the GCP test, which pushes
#: it: the bases too, since such a builder can't see images in the daemon. Docker Hub's
#: images come through Google's mirror of it, which CodeBuild's shared addresses aren't
#: rate-limited at. The source is the repository at HEAD with the plugins built for it.
BUILDSPEC = """
version: 0.2
env:
  shell: bash
phases:
  build:
    commands:
      - aws s3 cp "s3://$BUCKET/source.tar.gz" /tmp/source.tar.gz
      - mkdir -p /tmp/src && tar -xzf /tmp/source.tar.gz -C /tmp/src
      - aws ecr get-login-password | docker login --username AWS --password-stdin "$REGISTRY"
      - printf '[registry."docker.io"]\\n  mirrors = ["mirror.gcr.io"]\\n' > /tmp/buildkitd.toml
      - docker buildx create --name iki --driver docker-container
        --config /tmp/buildkitd.toml --bootstrap
      - |
        cd /tmp/src
        build() { docker buildx build --builder iki --provenance=false "$@" --push; }
        (build -f server/Dockerfile -t "$SERVER_BASE" . &&
          build -f deploy/plugins.Dockerfile --build-arg "IKI_SERVER_IMAGE=$SERVER_BASE" \\
            -t "$SERVER" build/aws-live/server) &
        server=$!
        (build -f worker/Dockerfile -t "$WORKER_BASE" . &&
          build -f deploy/worker.Dockerfile --build-arg "IKI_WORKER_IMAGE=$WORKER_BASE" \\
            -t "$WORKER" build/aws-live/worker) &
        worker=$!
        wait "$server" && wait "$worker"
"""


def start_build(d: Deployment, archive: Path) -> str:
    """ECR's repositories, and CodeBuild building the images into them: started, not
    followed, so they build while the cluster and database are made."""
    s, aws = d.settings, d.aws
    names = build_names(aws, s)
    for image in IMAGES:
        aws.aws(
            "ecr",
            "create-repository",
            "--repository-name",
            f"{s.name}/{image}",
            "--image-tag-mutability",
            "MUTABLE",
            "--encryption-configuration",
            "encryptionType=AES256",
            "--tags",
            *cli_tags(s),
        )
    bucket = names["bucket"]
    create = ["s3api", "create-bucket", "--bucket", bucket]
    if s.region != "us-east-1":
        create += ["--create-bucket-configuration", f"LocationConstraint={s.region}"]
    aws.aws(*create)
    aws.aws(
        "s3api",
        "put-bucket-tagging",
        "--bucket",
        bucket,
        "--tagging",
        json.dumps({"TagSet": [{"Key": k, "Value": v} for k, v in tags(s).items()]}),
    )
    aws.aws(
        "s3api",
        "put-public-access-block",
        "--bucket",
        bucket,
        "--public-access-block-configuration",
        "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true",
    )
    aws.aws("s3", "cp", str(archive), f"s3://{bucket}/source.tar.gz", timeout=1800)
    account = aws.account()
    trust = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "codebuild.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {"StringEquals": {"aws:SourceAccount": account}},
            }
        ],
    }
    repositories = [f"arn:aws:ecr:{s.region}:{account}:repository/{s.name}/{i}" for i in IMAGES]
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
                "Resource": f"arn:aws:logs:{s.region}:{account}:log-group:{names['logs']}*",
            },
            {"Effect": "Allow", "Action": "s3:GetObject", "Resource": f"arn:aws:s3:::{bucket}/*"},
            {"Effect": "Allow", "Action": "ecr:GetAuthorizationToken", "Resource": "*"},
            {
                "Effect": "Allow",
                "Action": [
                    "ecr:BatchCheckLayerAvailability",
                    "ecr:BatchGetImage",
                    "ecr:GetDownloadUrlForLayer",
                    "ecr:InitiateLayerUpload",
                    "ecr:UploadLayerPart",
                    "ecr:CompleteLayerUpload",
                    "ecr:PutImage",
                ],
                "Resource": repositories,
            },
        ],
    }
    aws.aws(
        "iam",
        "create-role",
        "--role-name",
        names["role"],
        "--description",
        f"CodeBuild, building {s.name}'s images for its live test",
        "--assume-role-policy-document",
        json.dumps(trust),
        "--tags",
        *cli_tags(s),
    )
    aws.aws(
        "iam",
        "put-role-policy",
        "--role-name",
        names["role"],
        "--policy-name",
        "build",
        "--policy-document",
        json.dumps(policy),
    )
    registry = names["registry"]
    environment = {
        "type": "LINUX_CONTAINER",
        "image": "aws/codebuild/amazonlinux-x86_64-standard:6.0",
        "computeType": "BUILD_GENERAL1_LARGE",
        "privilegedMode": True,
        "environmentVariables": [
            {"name": "BUCKET", "value": bucket},
            {"name": "REGISTRY", "value": registry},
            *(
                {"name": key, "value": f"{registry}/{s.name}/{image}:live"}
                for key, image in (
                    ("SERVER_BASE", "iki-server-base"),
                    ("SERVER", "iki-server"),
                    ("WORKER_BASE", "iki-worker-base"),
                    ("WORKER", "iki-worker"),
                )
            ),
        ],
    }
    project = [
        "codebuild",
        "create-project",
        "--name",
        names["project"],
        "--source",
        json.dumps({"type": "NO_SOURCE", "buildspec": BUILDSPEC}),
        "--artifacts",
        "type=NO_ARTIFACTS",
        "--environment",
        json.dumps(environment),
        "--service-role",
        f"arn:aws:iam::{account}:role/{names['role']}",
        "--timeout-in-minutes",
        "90",
        "--logs-config",
        json.dumps({"cloudWatchLogs": {"status": "ENABLED", "groupName": names["logs"]}}),
        "--tags",
        *cli_tags(s, "key", "value"),
    ]
    # A new role takes a few seconds to be one CodeBuild can assume.
    until(
        "CodeBuild's project",
        lambda: aws.run("aws", *project, check=False).returncode == 0,
        180,
        every=15,
    )
    build: str = until(
        "CodeBuild's build",
        lambda: (
            json.loads(
                aws.run(
                    "aws",
                    "codebuild",
                    "start-build",
                    "--project-name",
                    names["project"],
                    "--output",
                    "json",
                    check=False,
                ).stdout
                or "{}"
            )
            .get("build", {})
            .get("id")
        ),
        180,
        every=15,
    )
    return build


def build_status(aws: Aws, build: str) -> dict[str, Any]:
    found: dict[str, Any] = aws.aws_json("codebuild", "batch-get-builds", "--ids", build)["builds"][
        0
    ]
    return found


def finish_build(d: Deployment, build: str) -> dict[str, str]:
    """Waits for the build and returns each image's digest. (Its minutes are recorded as
    tearing down starts, with any other build's: stop_and_time_builds.)"""
    s, aws = d.settings, d.aws
    status = until(
        "CodeBuild",
        lambda: b if (b := build_status(aws, build))["buildStatus"] != "IN_PROGRESS" else None,
        5400,
        every=30,
    )
    logs = build_names(aws, s)["logs"]
    assert status["buildStatus"] == "SUCCEEDED", (
        f"build {build}: {status['buildStatus']} (its log: CloudWatch {logs})"
    )
    digests = {}
    for image in ("iki-server", "iki-worker"):
        details = aws.aws_json(
            "ecr",
            "describe-images",
            "--repository-name",
            f"{s.name}/{image}",
            "--image-ids",
            "imageTag=live",
        )["imageDetails"]
        digests[image] = details[0]["imageDigest"]
    return digests


def write_kubeconfig(aws: Aws, s: Settings) -> None:
    """kubectl to the cluster's endpoint, authenticated by `aws eks get-token` as the
    role: what update-kubeconfig writes, but in the work directory."""
    cluster = aws.aws_json("eks", "describe-cluster", "--name", s.name)["cluster"]
    config = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [
            {
                "name": s.name,
                "cluster": {
                    "server": cluster["endpoint"],
                    "certificate-authority-data": cluster["certificateAuthority"]["data"],
                },
            }
        ],
        "users": [
            {
                "name": s.name,
                "user": {
                    "exec": {
                        "apiVersion": "client.authentication.k8s.io/v1beta1",
                        "command": "aws",
                        "args": [
                            "eks",
                            "get-token",
                            "--cluster-name",
                            s.name,
                            "--region",
                            s.region,
                            "--output",
                            "json",
                        ],
                        "interactiveMode": "Never",
                    }
                },
            }
        ],
        "contexts": [{"name": s.name, "context": {"cluster": s.name, "user": s.name}}],
        "current-context": s.name,
    }
    path = aws.root / "kubeconfig"
    path.write_text(yaml.safe_dump(config))
    path.chmod(0o600)


def helm_install(aws: Aws, chart: tuple[str, str, str], namespace: str, *args: str) -> None:
    repository, name, version = chart
    aws.helm("repo", "add", name, repository)
    aws.helm(
        "upgrade",
        "--install",
        name,
        f"{name}/{name}",
        "--version",
        version,
        "--namespace",
        namespace,
        "--create-namespace",
        *args,
        "--wait",
        "--timeout",
        "10m",
    )


def stand_up(d: Deployment) -> None:
    """The runbook, steps 1 to 5, in the work directory's copy of deploy/aws."""
    s, aws = d.settings, d.aws
    work = s.workdir
    shutil.copytree(
        REPO / "deploy" / "aws",
        work / "deploy" / "aws",
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(
            ".terraform", "*.tfstate*", "terraform.tfvars*", "backend.hcl"
        ),
    )
    (work / "aws-facts.json").write_text("{}")

    # EC2 Auto Scaling's service-linked role, which the nodes' disk key names: AWS makes
    # it the first time an account uses Auto Scaling, and keeps it.
    if (
        aws.run(
            "aws", "iam", "get-role", "--role-name", "AWSServiceRoleForAutoScaling", check=False
        ).returncode
        != 0
    ):
        aws.aws(
            "iam", "create-service-linked-role", "--aws-service-name", "autoscaling.amazonaws.com"
        )

    # 1. The state's bucket and key.
    boot = work / "deploy/aws/bootstrap"
    (boot / "terraform.tfvars.json").write_text(
        json.dumps({"name": s.name, "region": s.region, "tags": tags(s)})
    )
    aws.tofu("bootstrap", "init", "-input=false", "-lockfile=readonly", log="bootstrap.log")
    aws.tofu("bootstrap", "apply", "-input=false", "-auto-approve", log="bootstrap.log")
    backend = aws.run("tofu", "-chdir=deploy/aws/bootstrap", "output", "-raw", "backend_hcl").stdout
    (work / "deploy/aws/production/backend.hcl").write_text(backend)
    state_key = aws.run(
        "tofu", "-chdir=deploy/aws/bootstrap", "output", "-raw", "state_kms_key_arn"
    ).stdout

    # The internal ALB's certificate, imported into ACM as the runbook's reader would.
    ca, chain, key = iki_live.make_certificate(work, s.host)
    end = "-----END CERTIFICATE-----\n"
    leaf = work / "tls" / "leaf-only.crt"
    leaf.write_text(chain.read_text().split(end, 1)[0] + end)
    certificate = aws.aws_json(
        "acm",
        "import-certificate",
        "--certificate",
        f"fileb://{leaf}",
        "--certificate-chain",
        f"fileb://{ca}",
        "--private-key",
        f"fileb://{key}",
        "--tags",
        *cli_tags(s),
    )["CertificateArn"]

    # The images, built while the rest is made.
    iki_live.build_plugins(work)
    archive = iki_live.source_archive(work, "build/aws-live")
    build = start_build(d, archive)

    # 2. The infrastructure: everything but the cluster and what needs it first, so what
    # AWS refuses fails in minutes, before a cluster is made to be torn down.
    production = work / "deploy/aws/production"
    (production / "terraform.tfvars.json").write_text(
        json.dumps(variables(s, aws, state_key, certificate, public_address()), indent=1)
    )
    aws.tofu(
        "production", "init", "-input=false", "-lockfile=readonly", "-backend-config=backend.hcl"
    )
    aws.tofu("production", "apply", "-input=false", "-auto-approve", "-exclude=module.cluster")
    early = build_status(aws, build)["buildStatus"]
    assert early in ("IN_PROGRESS", "SUCCEEDED"), f"build {build}: {early}"
    aws.tofu("production", "apply", "-input=false", "-auto-approve")

    # 3. The cluster's controllers, each bound to its role by Pod Identity.
    write_kubeconfig(aws, s)
    helm_install(
        aws,
        LOAD_BALANCER_CONTROLLER,
        "kube-system",
        "--set",
        f"clusterName={s.name}",
        "--set",
        f"region={s.region}",
        "--set",
        f"vpcId={aws.output('vpc_id')}",
        "--set",
        "serviceAccount.name=aws-load-balancer-controller",
    )
    helm_install(
        aws,
        CLUSTER_AUTOSCALER,
        "kube-system",
        "--set",
        f"autoDiscovery.clusterName={s.name}",
        "--set",
        f"awsRegion={s.region}",
        "--set",
        "rbac.serviceAccount.name=cluster-autoscaler",
    )
    dns_values = work / "external-dns.yaml"
    dns_values.write_text(aws.output("external_dns_values"))
    helm_install(aws, EXTERNAL_DNS, "kube-system", "-f", str(dns_values))
    aws.helm("repo", "add", "external-secrets", iki_live.EXTERNAL_SECRETS[0])
    aws.helm(
        "upgrade",
        "--install",
        "external-secrets",
        "external-secrets/external-secrets",
        "--version",
        iki_live.EXTERNAL_SECRETS[1],
        "--namespace",
        "external-secrets",
        "--create-namespace",
        "--wait",
        "--timeout",
        "10m",
    )

    # 4. Secrets and the database's roles.
    aws.run("sh", "-c", aws.output("put_secrets"), timeout=300)
    aws.run(
        "deploy/aws/scripts/create-database-roles.sh",
        s.name,
        aws.output("database_master_secret_arn"),
        timeout=900,
    )
    aws.kubectl("apply", "-f", "-", input=aws.output("external_secrets"))
    iki_live.wait_for_secrets(aws, SECRETS)

    # 5. Iki.
    digests = finish_build(d, build)
    registry = build_names(aws, s)["registry"]
    (production / "values-aws.yaml").write_text(aws.output("helm_values"))
    live: dict[str, Any] = {
        "environment": "aws-live",
        "image": {"repository": f"{registry}/{s.name}/iki-server", "digest": digests["iki-server"]},
        # Its plugins come from this checkout, not commits a lockfile pins.
        "plugins": {"allowLocal": True},
        "web": {
            "replicas": 2,
            "autoscaling": {"minReplicas": 2},
            "podDisruptionBudget": {"minAvailable": 1},
        },
        "tasks": {"replicas": 1},
        "egressProxy": {"replicas": 1},
    }
    (production / "values-live.yaml").write_text(yaml.safe_dump(live))
    chart = (
        "upgrade",
        "--install",
        "iki",
        str(REPO / "deploy/helm/iki"),
        "--namespace",
        "iki",
        "-f",
        str(production / "values-production.yaml"),
        "-f",
        str(production / "values-aws.yaml"),
        "-f",
        str(production / "values-live.yaml"),
    )
    aws.helm(*chart, "--wait", "--timeout", "20m")
    # The bucket's CORS and lifecycle rules, which Iki sets itself on S3 (the runbook).
    aws.server("setup_storage")
    # The ALB, which the controller makes from Iki's Ingress, and its name in the VPC.
    until(
        "the internal ALB",
        lambda: aws.kubectl(
            "-n",
            "iki",
            "get",
            "ingress",
            "iki-web",
            "-o",
            "jsonpath={.status.loadBalancer.ingress[0].hostname}",
        ).strip(),
        900,
    )

    # Backend workers: a registration token a Compute admin makes, kept in Secrets Manager
    # and copied in by External Secrets; they trust the test's CA and the public ones.
    token = aws.server(
        "create_registration_token", "--tier", "backend", "--label", "AWS live test"
    ).strip()
    aws.run(
        "aws",
        "secretsmanager",
        "put-secret-value",
        "--secret-id",
        f"{s.name}/backend-worker-token",
        "--secret-string",
        "file:///dev/stdin",
        input=token,
    )
    aws.kubectl(
        "-n",
        "iki",
        "annotate",
        "externalsecret",
        "iki-backend-worker-token",
        f"force-sync={int(time.time())}",
        "--overwrite",
    )
    iki_live.wait_for_secrets(aws, ("iki-backend-worker-token",))
    import certifi  # the public CAs, for S3's presigned URLs

    bundle = work / "tls" / "workers-ca.crt"
    bundle.write_text(ca.read_text() + Path(certifi.where()).read_text())
    aws.kubectl(
        "-n", "iki", "create", "secret", "generic", "iki-test-ca", f"--from-file=ca.crt={bundle}"
    )
    live["backendWorkers"] = {
        "enabled": True,
        "image": {"repository": f"{registry}/{s.name}/iki-worker", "digest": digests["iki-worker"]},
        "registrationToken": {"existingSecret": "iki-backend-worker-token", "key": "token"},
        "caSecret": {"name": "iki-test-ca", "key": "ca.crt"},
        "replicas": 1,
    }
    (production / "values-live.yaml").write_text(yaml.safe_dump(live))
    aws.helm(*chart, "--timeout", "20m")


@pytest.fixture(scope="module")
def deployment(request: pytest.FixtureRequest) -> Iterator[Deployment]:
    s = Settings.from_env(new=True)
    d = Deployment(
        cloud="aws",
        name=s.name,
        workdir=s.workdir,
        tools=Aws(s),
        suite_version=SUITE_VERSION,
        cap_seconds=s.cap_hours * 3600,
        settings=s,
    )
    yield from iki_live.lifecycle(d, request)


@pytest.fixture(scope="module")
def iki(deployment: Deployment) -> Iki:
    """A web pod, through a port-forward, with no one signed in."""
    return iki_live.forward_to_web(deployment.aws, deployment.forwards, deployment.settings.host)


@pytest.fixture(scope="module")
def admin(deployment: Deployment, iki: Iki) -> Iki:
    """The same, signed in as an Org Admin in a team of their own (iki_live.sign_in_admin)."""
    signed_in, deployment.team = iki_live.sign_in_admin(deployment.aws, iki, "AWS live test")
    return signed_in


@pytest.fixture(scope="module")
def project(deployment: Deployment, admin: Iki) -> dict[str, Any]:
    return iki_live.make_project(admin, deployment.team)


# --- The checks ------------------------------------------------------------------------------


def test_web_pods_are_ready_with_their_database(iki: Iki) -> None:
    iki_live.check_ready(iki)


def test_the_database_is_reached_privately_and_over_verified_tls(deployment: Deployment) -> None:
    aws, s = deployment.aws, deployment.settings
    found = iki_live.database_connection(aws)
    # The network module's database subnets, in vpc_cidr's default 10.40.0.0/16.
    assert ipaddress.IPv4Address(found["address"]) in ipaddress.IPv4Network("10.40.0.0/16"), found
    db = aws.aws_json("rds", "describe-db-instances", "--db-instance-identifier", s.name)[
        "DBInstances"
    ][0]
    assert not db["PubliclyAccessible"] and db["StorageEncrypted"] and db["MultiAZ"], db
    assert db["DBSubnetGroup"]["VpcId"] == aws.output("vpc_id")


def test_the_setup_link_opens_the_wizard(deployment: Deployment, iki: Iki) -> None:
    iki_live.check_setup_link(deployment.aws, iki, deployment.settings.host)


def test_an_org_admin_creates_a_project_and_an_entity(admin: Iki, project: dict[str, Any]) -> None:
    iki_live.check_project_and_entity(admin, project)


def s3_hosts(bucket: str, region: str) -> set[str]:
    """The names a URL for the bucket is signed for: its region's, and in us-east-1 S3's
    global one too, which the SDK signs for there."""
    hosts = {f"{bucket}.s3.{region}.amazonaws.com"}
    if region == "us-east-1":
        hosts.add(f"{bucket}.s3.amazonaws.com")
    return hosts


def test_a_file_goes_up_and_comes_down_through_urls_signed_with_short_lived_credentials(
    deployment: Deployment, admin: Iki, project: dict[str, Any]
) -> None:
    s, aws = deployment.settings, deployment.aws
    hosts = s3_hosts(f"{s.name}-files-{aws.account()}", s.region)

    def signed(url: str) -> None:
        parts = urlsplit(url)
        assert parts.hostname in hosts, url
        query = parse_qs(parts.query)
        assert query.get("X-Amz-Algorithm") == ["AWS4-HMAC-SHA256"], url
        # The web pod's role, through Pod Identity: a session's key (ASIA...) and its token,
        # never a user's long-lived one (AKIA...).
        assert query["X-Amz-Credential"][0].startswith("ASIA") and "X-Amz-Security-Token" in query

    iki_live.check_file_round_trip(admin, project, signed, signed)


def test_a_backend_worker_computes_a_proteins_properties(
    admin: Iki, project: dict[str, Any]
) -> None:
    iki_live.check_backend_run(admin, project)


def test_the_alb_is_internal_and_named_only_inside_the_vpc(deployment: Deployment) -> None:
    aws, s = deployment.aws, deployment.settings
    host = aws.kubectl(
        "-n",
        "iki",
        "get",
        "ingress",
        "iki-web",
        "-o",
        "jsonpath={.status.loadBalancer.ingress[0].hostname}",
    ).strip()
    balancers = aws.aws_json("elbv2", "describe-load-balancers")["LoadBalancers"]
    balancer = next(b for b in balancers if b["DNSName"] == host)
    assert balancer["Scheme"] == "internal", balancer
    listeners = aws.aws_json(
        "elbv2", "describe-listeners", "--load-balancer-arn", balancer["LoadBalancerArn"]
    )
    assert [(x["Protocol"], x["Port"]) for x in listeners["Listeners"]] == [("HTTPS", 443)], (
        listeners
    )
    assert all(
        x["SslPolicy"].startswith("ELBSecurityPolicy-TLS13-1-2-") for x in listeners["Listeners"]
    )
    zones = aws.aws_json("route53", "list-hosted-zones-by-name", "--dns-name", s.host)[
        "HostedZones"
    ]
    zone = next(z for z in zones if z["Name"] == f"{s.host}.")
    assert zone["Config"]["PrivateZone"], zone
    records = aws.aws_json("route53", "list-resource-record-sets", "--hosted-zone-id", zone["Id"])[
        "ResourceRecordSets"
    ]
    alias = [r for r in records if r["Name"] == f"{s.host}." and r["Type"] == "A"]
    assert alias and alias[0]["AliasTarget"]["DNSName"].rstrip(".").endswith(host.strip()), records


def test_setup_storage_finds_the_bucket_as_iki_needs_it(deployment: Deployment) -> None:
    iki_live.check_setup_storage(deployment.aws)


def test_tearing_down_leaves_only_what_aws_keeps(deployment: Deployment) -> None:
    """Last: everything goes, and nothing else in the account changed."""
    problems = deployment.tear_down()
    deployment.torn_down_checked = True
    assert not problems, "\n".join(problems)
