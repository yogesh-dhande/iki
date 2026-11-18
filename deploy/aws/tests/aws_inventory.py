"""Everything in the AWS account a deploy/aws deployment could leave behind, listed.

    PYTHONPATH=deploy/live uv run python deploy/aws/tests/aws_inventory.py > before.txt

The live test (test_aws_live.py) lists the account before it makes anything and after it
has torn everything down, and fails if the second has anything of the deployment's but
what AWS keeps (``leftovers``). Each line is one resource: its region, ID or name, and
whose it is when a tag says (the deployment's, EKS's, the load balancer controller's), so
what Kubernetes made without the deployment's name is still found. Run alone, it acts as
the live test does (``Aws``): IKI_AWS_ROLE_ARN and IKI_AWS_TOKEN_COMMAND.
"""

import json
import os
import re
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import iki_live
from iki_live import REPO

#: Credentials and settings the caller's environment might hold, which the tools mustn't
#: use instead of the web identity token (Aws).
_CREDENTIAL_VARIABLES = {
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_SECURITY_TOKEN",
    "AWS_PROFILE",
    "AWS_DEFAULT_PROFILE",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
    "AWS_CREDENTIAL_EXPIRATION",
    "AWS_ROLE_ARN",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_ROLE_SESSION_NAME",
    "AWS_CONFIG_FILE",
    "AWS_SHARED_CREDENTIALS_FILE",
    "KUBECONFIG",
}


# --- Settings ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    role_arn: str
    token_command: str
    name: str
    region: str
    dr_region: str
    zones: tuple[str, ...]
    system_type: str
    runs_type: str
    database_class: str
    cap_hours: float
    workdir: Path

    @classmethod
    def from_env(cls, *, new: bool = False) -> "Settings":
        """The run's settings (test_aws_live.py's docstring lists them). Unless IKI_AWS_NAME
        names it, a new run gets a name of its own, and a later look takes the last."""
        env = os.environ
        name = iki_live.run_name("aws", env.get("IKI_AWS_NAME", ""), new=new)
        region = env.get("IKI_AWS_REGION", "us-east-1")
        zones = env.get("IKI_AWS_ZONES", f"{region}a,{region}b")
        return cls(
            role_arn=env["IKI_AWS_ROLE_ARN"],
            token_command=env["IKI_AWS_TOKEN_COMMAND"],
            name=name,
            region=region,
            dr_region=env.get("IKI_AWS_DR_REGION", "us-west-2"),
            zones=tuple(z.strip() for z in zones.split(",") if z.strip()),
            system_type=env.get("IKI_AWS_SYSTEM_TYPE", "m7i-flex.large"),
            runs_type=env.get("IKI_AWS_RUNS_TYPE", "c7i-flex.large"),
            database_class=env.get("IKI_AWS_DATABASE_CLASS", "db.t4g.small"),
            cap_hours=float(env.get("IKI_AWS_CAP_HOURS", "2")),
            workdir=Path(env.get("IKI_AWS_WORKDIR", REPO / "build" / "aws-live" / name)).resolve(),
        )

    @property
    def regions(self) -> tuple[str, str]:
        return (self.region, self.dr_region)

    @property
    def host(self) -> str:
        """public_host: a name only the deployment's VPC resolves (its private zone)."""
        return f"{self.name}.internal"


# --- The one place the AWS CLI, OpenTofu, kubectl and Helm run --------------------------------


class Aws(iki_live.Tools):
    """The tools as one identity: IKI_AWS_ROLE_ARN, assumed with the web identity token
    IKI_AWS_TOKEN_COMMAND prints (an OpenID Connect ID token the role trusts), which
    iki_live.Token keeps fresh in its file. The AWS CLI, OpenTofu's AWS provider and S3
    backend, and kubectl's `aws eks get-token` all read that file and assume the role
    themselves, again whenever their credentials near expiry. No AWS configuration or
    credentials of the caller's are read: both files are /dev/null, and the variables that
    could hold keys are taken out."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        token = iki_live.Token(
            settings.token_command, "IKI_AWS_TOKEN_COMMAND", prefix="iki-aws-live-"
        )
        super().__init__(
            settings.workdir,
            token,
            {
                "AWS_ROLE_ARN": settings.role_arn,
                "AWS_WEB_IDENTITY_TOKEN_FILE": str(token.path),
                "AWS_ROLE_SESSION_NAME": settings.name,
                "AWS_REGION": settings.region,
                "AWS_DEFAULT_REGION": settings.region,
                "AWS_CONFIG_FILE": os.devnull,
                "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
                "AWS_EC2_METADATA_DISABLED": "true",
                "AWS_PAGER": "",
                "AWS_CLI_AUTO_PROMPT": "off",
            },
            _CREDENTIAL_VARIABLES,
        )

    def aws(self, *args: str, region: str | None = None, **kwargs: Any) -> str:
        where = ("--region", region) if region else ()
        return self.run("aws", *args, *where, "--output", "json", **kwargs).stdout

    def aws_json(self, *args: str, region: str | None = None) -> Any:
        return json.loads(self.aws(*args, region=region) or "null")

    def account(self) -> str:
        found: str = self.aws_json("sts", "get-caller-identity")["Account"]
        return found

    def tofu(self, root: str, *args: str, log: str = "tofu.log", timeout: float = 4800) -> None:
        self.stream("tofu", f"-chdir=deploy/aws/{root}", *args, log=log, timeout=timeout)

    def output(self, name: str) -> str:
        return self.run("tofu", "-chdir=deploy/aws/production", "output", "-raw", name).stdout

    def output_json(self, name: str) -> Any:
        found = self.run("tofu", "-chdir=deploy/aws/production", "output", "-json", name)
        return json.loads(found.stdout)


# --- The listings ---------------------------------------------------------------------------

#: Tags that say whose a resource is: the modules', EKS's, its node groups', the AWS Load
#: Balancer Controller's, and the live test's own.
OWNER_TAGS = (
    "iki.ikisci.org/deployment",
    "iki-deployment",
    "eks:cluster-name",
    "elbv2.k8s.aws/cluster",
    "aws:eks:cluster-name",
)


def owner(tags: Iterable[dict[str, str]] | None) -> str:
    """Whose a resource is, by its tags: [<owner>], or '' when none says."""
    found = []
    for tag in tags or []:
        key, value = tag.get("Key", ""), tag.get("Value", "")
        if key in OWNER_TAGS:
            found.append(value)
        elif key.startswith("kubernetes.io/cluster/"):
            found.append(key.removeprefix("kubernetes.io/cluster/"))
    return f" [{','.join(sorted(set(found)))}]" if found else ""


def tag(tags: Iterable[dict[str, str]] | None, key: str) -> str:
    return next((t["Value"] for t in tags or [] if t.get("Key") == key), "")


def _lines(items: Iterable[Any], render: Callable[[Any], str]) -> set[str]:
    return {render(i) for i in items or []}


#: Each regional section: the AWS CLI command, and how each resource it lists is one line.
Regional = tuple[tuple[str, ...], Callable[[Any], set[str]]]
REGIONAL: dict[str, Regional] = {
    "instances": (
        ("ec2", "describe-instances"),
        lambda r: _lines(
            (
                i
                for res in r["Reservations"]
                for i in res["Instances"]
                if i["State"]["Name"] != "terminated"
            ),
            lambda i: f"{i['InstanceId']} {i['InstanceType']}{owner(i.get('Tags'))}",
        ),
    ),
    "volumes": (
        ("ec2", "describe-volumes"),
        lambda r: _lines(
            r["Volumes"], lambda v: f"{v['VolumeId']} {v['Size']}GiB{owner(v.get('Tags'))}"
        ),
    ),
    "snapshots": (
        ("ec2", "describe-snapshots", "--owner-ids", "self"),
        lambda r: _lines(r["Snapshots"], lambda v: f"{v['SnapshotId']}{owner(v.get('Tags'))}"),
    ),
    "images": (
        ("ec2", "describe-images", "--owners", "self"),
        lambda r: _lines(r["Images"], lambda v: f"{v['ImageId']} {v.get('Name', '')}"),
    ),
    "vpcs": (
        ("ec2", "describe-vpcs"),
        lambda r: _lines(
            r["Vpcs"],
            lambda v: f"{v['VpcId']} {tag(v.get('Tags'), 'Name')}{owner(v.get('Tags'))}",
        ),
    ),
    "subnets": (
        ("ec2", "describe-subnets"),
        lambda r: _lines(
            r["Subnets"],
            lambda v: f"{v['SubnetId']} {tag(v.get('Tags'), 'Name')}{owner(v.get('Tags'))}",
        ),
    ),
    "security groups": (
        ("ec2", "describe-security-groups"),
        lambda r: _lines(
            r["SecurityGroups"],
            lambda v: f"{v['GroupId']} {v['GroupName']} {v.get('VpcId', '')}{owner(v.get('Tags'))}",
        ),
    ),
    "nat gateways": (
        ("ec2", "describe-nat-gateways"),
        lambda r: _lines(
            (n for n in r["NatGateways"] if n["State"] not in ("deleted",)),
            lambda v: f"{v['NatGatewayId']} {v['State']}{owner(v.get('Tags'))}",
        ),
    ),
    "addresses": (
        ("ec2", "describe-addresses"),
        lambda r: _lines(
            r["Addresses"],
            lambda v: (
                f"{v.get('AllocationId', v.get('PublicIp'))} {tag(v.get('Tags'), 'Name')}"
                f"{owner(v.get('Tags'))}"
            ),
        ),
    ),
    "internet gateways": (
        ("ec2", "describe-internet-gateways"),
        lambda r: _lines(
            r["InternetGateways"],
            lambda v: (
                f"{v['InternetGatewayId']} {tag(v.get('Tags'), 'Name')}{owner(v.get('Tags'))}"
            ),
        ),
    ),
    "route tables": (
        ("ec2", "describe-route-tables"),
        lambda r: _lines(
            r["RouteTables"],
            lambda v: f"{v['RouteTableId']} {tag(v.get('Tags'), 'Name')}{owner(v.get('Tags'))}",
        ),
    ),
    "vpc endpoints": (
        ("ec2", "describe-vpc-endpoints"),
        lambda r: _lines(
            (e for e in r["VpcEndpoints"] if e["State"].lower() != "deleted"),
            lambda v: f"{v['VpcEndpointId']} {v['ServiceName']}{owner(v.get('Tags'))}",
        ),
    ),
    "network interfaces": (
        ("ec2", "describe-network-interfaces"),
        lambda r: _lines(
            r["NetworkInterfaces"],
            lambda v: (
                f"{v['NetworkInterfaceId']} {v.get('Description', '')}{owner(v.get('TagSet'))}"
            ),
        ),
    ),
    "launch templates": (
        ("ec2", "describe-launch-templates"),
        lambda r: _lines(
            r["LaunchTemplates"],
            lambda v: f"{v['LaunchTemplateName']}{owner(v.get('Tags'))}",
        ),
    ),
    "flow logs": (
        ("ec2", "describe-flow-logs"),
        lambda r: _lines(
            r["FlowLogs"],
            lambda v: f"{v['FlowLogId']} {v.get('LogGroupName', '')}{owner(v.get('Tags'))}",
        ),
    ),
    "auto scaling groups": (
        ("autoscaling", "describe-auto-scaling-groups"),
        lambda r: _lines(
            r["AutoScalingGroups"],
            lambda v: f"{v['AutoScalingGroupName']}{owner(v.get('Tags'))}",
        ),
    ),
    "load balancers": (
        ("elbv2", "describe-load-balancers"),
        lambda r: _lines(
            r["LoadBalancers"], lambda v: f"{v['LoadBalancerName']} {v['Scheme']} {v['VpcId']}"
        ),
    ),
    "target groups": (
        ("elbv2", "describe-target-groups"),
        lambda r: _lines(r["TargetGroups"], lambda v: f"{v['TargetGroupName']} {v.get('VpcId')}"),
    ),
    "eks clusters": (("eks", "list-clusters"), lambda r: set(r["clusters"])),
    "db instances": (
        ("rds", "describe-db-instances"),
        lambda r: _lines(
            r["DBInstances"], lambda v: f"{v['DBInstanceIdentifier']} {v['DBInstanceStatus']}"
        ),
    ),
    "db snapshots": (
        ("rds", "describe-db-snapshots"),
        lambda r: _lines(
            r["DBSnapshots"],
            lambda v: (
                f"{v['DBSnapshotIdentifier']} {v['SnapshotType']} {v['DBInstanceIdentifier']}"
            ),
        ),
    ),
    "db retained backups": (
        ("rds", "describe-db-instance-automated-backups"),
        lambda r: _lines(
            r["DBInstanceAutomatedBackups"],
            lambda v: f"{v['DBInstanceIdentifier']} {v['Status']} {v['DbiResourceId']}",
        ),
    ),
    "db parameter groups": (
        ("rds", "describe-db-parameter-groups"),
        lambda r: _lines(
            (
                g
                for g in r["DBParameterGroups"]
                if not g["DBParameterGroupName"].startswith("default.")
            ),
            lambda v: v["DBParameterGroupName"],
        ),
    ),
    "db subnet groups": (
        ("rds", "describe-db-subnet-groups"),
        lambda r: _lines(r["DBSubnetGroups"], lambda v: v["DBSubnetGroupName"]),
    ),
    "secrets": (
        ("secretsmanager", "list-secrets", "--include-planned-deletion"),
        lambda r: _lines(
            r["SecretList"],
            lambda v: f"{v['Name']}{' DELETING' if v.get('DeletedDate') else ''}",
        ),
    ),
    "log groups": (
        ("logs", "describe-log-groups"),
        lambda r: _lines(r["logGroups"], lambda v: v["logGroupName"]),
    ),
    "ecr repositories": (
        ("ecr", "describe-repositories"),
        lambda r: _lines(r["repositories"], lambda v: v["repositoryName"]),
    ),
    "codebuild projects": (("codebuild", "list-projects"), lambda r: set(r["projects"])),
    "certificates": (
        ("acm", "list-certificates"),
        lambda r: _lines(
            r["CertificateSummaryList"], lambda v: f"{v['DomainName']} {v['CertificateArn'][-12:]}"
        ),
    ),
    "fsx file systems": (
        ("fsx", "describe-file-systems"),
        lambda r: _lines(r["FileSystems"], lambda v: f"{v['FileSystemId']}{owner(v.get('Tags'))}"),
    ),
}

#: The account's global sections.
GLOBAL: dict[str, Regional] = {
    "iam roles": (
        ("iam", "list-roles"),
        lambda r: _lines(r["Roles"], lambda v: f"{v['Path']}{v['RoleName']}"),
    ),
    "iam policies": (
        ("iam", "list-policies", "--scope", "Local"),
        lambda r: _lines(r["Policies"], lambda v: f"{v['Path']}{v['PolicyName']}"),
    ),
    "iam instance profiles": (
        ("iam", "list-instance-profiles"),
        lambda r: _lines(r["InstanceProfiles"], lambda v: v["InstanceProfileName"]),
    ),
    "iam oidc providers": (
        ("iam", "list-open-id-connect-providers"),
        lambda r: _lines(r["OpenIDConnectProviderList"], lambda v: v["Arn"].split("/", 1)[-1]),
    ),
    "hosted zones": (
        ("route53", "list-hosted-zones"),
        lambda r: _lines(
            r["HostedZones"],
            lambda v: f"{v['Name']} {'private' if v['Config']['PrivateZone'] else 'public'}",
        ),
    ),
    "buckets": (("s3api", "list-buckets"), lambda r: _lines(r["Buckets"], lambda v: v["Name"])),
}


@dataclass
class Inventory:
    #: Each section's lines; a regional one's start with its region.
    listings: dict[str, set[str]] = field(default_factory=dict)
    #: The customer-managed KMS keys: '<region> <id> <state> <description>'.
    keys: set[str] = field(default_factory=set)

    def sections(self) -> dict[str, set[str]]:
        return {**self.listings, "kms keys": self.keys}

    def write(self, path: Path) -> None:
        iki_live.write_sections(path, self.sections())


#: What the AWS CLI says of a service the account can't use (some aren't, on AWS's free
#: plan): the account can't hold any of it, so its section is empty.
UNAVAILABLE = ("SubscriptionRequiredException", "OptInRequired")


def listing(aws: Aws, argv: tuple[str, ...], region: str) -> Any:
    """A regional listing as the CLI prints it, or None if the account can't use it."""
    found = aws.run("aws", *argv, "--region", region, "--output", "json", check=False)
    if found.returncode != 0 and any(u in found.stderr for u in UNAVAILABLE):
        return None
    if found.returncode != 0:
        raise AssertionError(f"aws {' '.join(argv)} in {region}: {found.stderr[-2000:]}")
    return json.loads(found.stdout or "null")


def inventory(aws: Aws) -> Inventory:
    """The account, now: every section in both of the run's regions, and the global ones."""
    found = Inventory()
    for region in aws.settings.regions:
        for section, (argv, render) in REGIONAL.items():
            listed = listing(aws, argv, region)
            lines = set() if listed is None else render(listed)
            found.listings.setdefault(section, set()).update(f"{region} {line}" for line in lines)
        for key in aws.aws_json("kms", "list-keys", region=region)["Keys"]:
            meta = aws.aws_json("kms", "describe-key", "--key-id", key["KeyId"], region=region)[
                "KeyMetadata"
            ]
            if meta["KeyManager"] == "CUSTOMER":
                found.keys.add(
                    f"{region} {meta['KeyId']} {meta['KeyState']} {meta.get('Description', '')}"
                )
    for section, (argv, render) in GLOBAL.items():
        found.listings[section] = render(aws.aws_json(*argv))
    return found


# --- What tearing down may leave ------------------------------------------------------------

#: Roles AWS makes for itself the first time an account uses a service (EKS, its node
#: groups, Auto Scaling, Elastic Load Balancing, RDS...), and keeps: one per service, for
#: every later use, never the deployment's.
SERVICE_LINKED = re.compile(r"^/aws-service-role/")
#: Sections whose lines come and go with anything in the account (another workload's
#: instance, an interface it attaches): only what's the deployment's is checked there.
VOLATILE = {"instances", "volumes", "network interfaces"}


def expected(before: Inventory, after: Inventory, name: str) -> list[str]:
    """What tearing down leaves that's expected, to say so: keys waiting out their deletion
    window, and service-linked roles AWS made for the deployment's services."""
    new = iki_live.added(before.sections(), after.sections())
    kept = [f"kms keys: {line}" for line in sorted(new.get("kms keys", set()))]
    kept += [
        f"iam roles: {line} (AWS's, for every later use of its service)"
        for line in sorted(new.get("iam roles", set()))
        if SERVICE_LINKED.match(line)
    ]
    return kept


def leftovers(before: Inventory, after: Inventory, name: str) -> list[str]:
    """What the deployment left that it may not: empty if only what AWS keeps."""
    problems = []
    for section, lines in iki_live.added(before.sections(), after.sections()).items():
        for line in sorted(lines):
            if section == "kms keys":
                # Each key is the deployment's (its description), waiting to be deleted.
                if " PendingDeletion " not in line:
                    problems.append(f"{section}: {line}, not scheduled for deletion")
                continue
            if section == "iam roles" and SERVICE_LINKED.match(line):
                continue
            if section in VOLATILE and name not in line:
                continue
            # The test account is otherwise quiet: anything new is the run's, named for it
            # or not (what Kubernetes made carries its cluster's tag, as [name]).
            problems.append(f"{section}: {line}")
    for section, lines in iki_live.removed(before.sections(), after.sections()).items():
        if section in VOLATILE:
            continue
        problems += [f"{section}: {line} is gone, which was there before" for line in sorted(lines)]
    return problems


def main() -> None:
    aws = Aws(Settings.from_env())
    aws.token.start()
    try:
        found = inventory(aws)
        for section, lines in found.sections().items():
            sys.stdout.write(f"=== {section} ({len(lines)})\n")
            sys.stdout.writelines(f"{line}\n" for line in sorted(lines))
    finally:
        aws.stop()


if __name__ == "__main__":
    main()
