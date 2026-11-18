"""aws_inventory.py lists the account and judges what a teardown left: checked offline,
with listings as the AWS CLI prints them, so it needs no credentials and runs everywhere."""

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aws_inventory
import pytest
from aws_inventory import Inventory, expected, leftovers, owner

NAME = "iki-test-11171530"


def test_whose_a_resource_is_comes_from_its_tags() -> None:
    assert owner([{"Key": "iki.ikisci.org/deployment", "Value": NAME}]) == f" [{NAME}]"
    assert owner([{"Key": f"kubernetes.io/cluster/{NAME}", "Value": "owned"}]) == f" [{NAME}]"
    assert owner(
        [{"Key": "elbv2.k8s.aws/cluster", "Value": NAME}, {"Key": "Name", "Value": "x"}]
    ) == (f" [{NAME}]")
    assert owner([{"Key": "Name", "Value": "someone-elses"}]) == ""
    assert owner(None) == ""


def test_each_listing_is_rendered_from_what_the_cli_prints() -> None:
    _, render = aws_inventory.REGIONAL["instances"]
    listed = {
        "Reservations": [
            {
                "Instances": [
                    {
                        "InstanceId": "i-1",
                        "InstanceType": "m7i-flex.large",
                        "State": {"Name": "running"},
                        "Tags": [{"Key": "eks:cluster-name", "Value": NAME}],
                    },
                    {
                        "InstanceId": "i-2",
                        "InstanceType": "t3.micro",
                        "State": {"Name": "terminated"},
                    },
                ]
            }
        ]
    }
    assert render(listed) == {f"i-1 m7i-flex.large [{NAME}]"}
    _, secrets = aws_inventory.REGIONAL["secrets"]
    assert secrets({"SecretList": [{"Name": f"{NAME}/django", "DeletedDate": "x"}]}) == {
        f"{NAME}/django DELETING"
    }


class FakeAws:
    """The CLI's answers for one region and the global sections: empty unless given."""

    def __init__(self, answers: dict[tuple[str, ...], Any]) -> None:
        self.answers = answers
        self.settings = aws_inventory.Settings(
            role_arn="arn:aws:iam::000000000000:role/r",
            token_command="true",
            name=NAME,
            region="us-east-1",
            dr_region="us-west-2",
            zones=("us-east-1a", "us-east-1b"),
            system_type="m7i-flex.large",
            runs_type="c7i-flex.large",
            database_class="db.t4g.micro",
            cap_hours=2,
            workdir=Path("/nonexistent"),
        )

    def run(self, *argv: str, check: bool = True) -> Any:
        """The CLI run for a listing: its JSON, or the free plan's refusal for FSx."""
        args = argv[1 : argv.index("--region")]
        if args[0] == "fsx":
            return SimpleNamespace(
                returncode=254,
                stdout="",
                stderr="(SubscriptionRequiredException) needs a subscription",
            )
        return SimpleNamespace(returncode=0, stdout=json.dumps(self.aws_json(*args)), stderr="")

    def aws_json(self, *args: str, region: str | None = None) -> Any:
        if args in self.answers:
            return self.answers[args]
        if args[:2] == ("kms", "list-keys"):
            return {"Keys": []}
        return empty_answer(args)


def empty_answer(argv: tuple[str, ...]) -> Any:
    """What a listing prints with nothing in it: each top-level list empty."""
    keys = {
        ("ec2", "describe-instances"): "Reservations",
        ("ec2", "describe-volumes"): "Volumes",
        ("ec2", "describe-snapshots"): "Snapshots",
        ("ec2", "describe-images"): "Images",
        ("ec2", "describe-vpcs"): "Vpcs",
        ("ec2", "describe-subnets"): "Subnets",
        ("ec2", "describe-security-groups"): "SecurityGroups",
        ("ec2", "describe-nat-gateways"): "NatGateways",
        ("ec2", "describe-addresses"): "Addresses",
        ("ec2", "describe-internet-gateways"): "InternetGateways",
        ("ec2", "describe-route-tables"): "RouteTables",
        ("ec2", "describe-vpc-endpoints"): "VpcEndpoints",
        ("ec2", "describe-network-interfaces"): "NetworkInterfaces",
        ("ec2", "describe-launch-templates"): "LaunchTemplates",
        ("ec2", "describe-flow-logs"): "FlowLogs",
        ("autoscaling", "describe-auto-scaling-groups"): "AutoScalingGroups",
        ("elbv2", "describe-load-balancers"): "LoadBalancers",
        ("elbv2", "describe-target-groups"): "TargetGroups",
        ("eks", "list-clusters"): "clusters",
        ("rds", "describe-db-instances"): "DBInstances",
        ("rds", "describe-db-snapshots"): "DBSnapshots",
        ("rds", "describe-db-instance-automated-backups"): "DBInstanceAutomatedBackups",
        ("rds", "describe-db-parameter-groups"): "DBParameterGroups",
        ("rds", "describe-db-subnet-groups"): "DBSubnetGroups",
        ("secretsmanager", "list-secrets"): "SecretList",
        ("logs", "describe-log-groups"): "logGroups",
        ("ecr", "describe-repositories"): "repositories",
        ("codebuild", "list-projects"): "projects",
        ("acm", "list-certificates"): "CertificateSummaryList",
        ("fsx", "describe-file-systems"): "FileSystems",
        ("iam", "list-roles"): "Roles",
        ("iam", "list-policies"): "Policies",
        ("iam", "list-instance-profiles"): "InstanceProfiles",
        ("iam", "list-open-id-connect-providers"): "OpenIDConnectProviderList",
        ("route53", "list-hosted-zones"): "HostedZones",
        ("s3api", "list-buckets"): "Buckets",
    }
    return {keys[argv[0], argv[1]]: []}


def test_every_section_is_listed_in_both_regions_and_globally_once() -> None:
    aws = FakeAws(
        {
            ("eks", "list-clusters"): {"clusters": [NAME]},
            ("iam", "list-roles"): {"Roles": [{"Path": "/", "RoleName": "iki-test-deployer"}]},
        }
    )
    found = aws_inventory.inventory(aws)  # type: ignore[arg-type]
    assert found.listings["eks clusters"] == {f"us-east-1 {NAME}", f"us-west-2 {NAME}"}
    assert found.listings["iam roles"] == {"/iki-test-deployer"}
    assert set(found.sections()) == {*aws_inventory.REGIONAL, *aws_inventory.GLOBAL, "kms keys"}
    # A service the account can't use holds nothing of it.
    assert found.listings["fsx file systems"] == set()


def inventory(**sections: set[str]) -> Inventory:
    keys = sections.pop("kms_keys", set())
    return Inventory({k.replace("_", " "): v for k, v in sections.items()}, keys)


def test_a_clean_teardown_leaves_only_what_aws_keeps() -> None:
    before = inventory(iam_roles={"/iki-test-deployer"}, log_groups={"us-east-1 other"})
    after = inventory(
        iam_roles={
            "/iki-test-deployer",
            "/aws-service-role/eks.amazonaws.com/AWSServiceRoleForAmazonEKS",
        },
        log_groups={"us-east-1 other"},
        kms_keys={f"us-east-1 k1 PendingDeletion {NAME}: node disks"},
    )
    assert leftovers(before, after, NAME) == []
    assert expected(before, after, NAME) == [
        f"kms keys: us-east-1 k1 PendingDeletion {NAME}: node disks",
        "iam roles: /aws-service-role/eks.amazonaws.com/AWSServiceRoleForAmazonEKS "
        "(AWS's, for every later use of its service)",
    ]


def test_whatever_else_is_left_is_found_named_for_the_run_or_not() -> None:
    before = inventory(security_groups=set(), iam_roles=set(), instances=set())
    after = inventory(
        security_groups={f"us-east-1 sg-1 k8s-iki-ikiweb-abc vpc-1 [{NAME}]"},
        iam_roles={f"/{NAME}-codebuild"},
        instances={"us-east-1 i-9 t3.micro", f"us-east-1 i-1 m7i-flex.large [{NAME}]"},
        kms_keys={f"us-east-1 k2 Enabled {NAME}: secrets"},
    )
    assert leftovers(before, after, NAME) == [
        f"security groups: us-east-1 sg-1 k8s-iki-ikiweb-abc vpc-1 [{NAME}]",
        f"iam roles: /{NAME}-codebuild",
        f"instances: us-east-1 i-1 m7i-flex.large [{NAME}]",
        f"kms keys: us-east-1 k2 Enabled {NAME}: secrets, not scheduled for deletion",
    ]


def test_what_the_run_deleted_of_someone_elses_is_found() -> None:
    before = inventory(buckets={"someone-elses"}, network_interfaces={"us-east-1 eni-1 theirs"})
    after = inventory(buckets=set(), network_interfaces=set())
    assert leftovers(before, after, NAME) == [
        "buckets: someone-elses is gone, which was there before"
    ]


def test_the_tools_act_as_the_role_and_nothing_of_the_callers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
    monkeypatch.setenv("AWS_PROFILE", "mine")
    monkeypatch.setenv("IKI_AWS_ROLE_ARN", "arn:aws:iam::000000000000:role/r")
    monkeypatch.setenv("IKI_AWS_TOKEN_COMMAND", "true")
    monkeypatch.setenv("IKI_AWS_NAME", NAME)
    monkeypatch.setenv("IKI_AWS_WORKDIR", str(tmp_path))
    aws = aws_inventory.Aws(aws_inventory.Settings.from_env())
    try:
        env = aws.env
        assert "AWS_ACCESS_KEY_ID" not in env and "AWS_PROFILE" not in env
        assert env["AWS_CONFIG_FILE"] == env["AWS_SHARED_CREDENTIALS_FILE"] == os.devnull
        assert env["AWS_WEB_IDENTITY_TOKEN_FILE"] == str(aws.token.path)
        assert env["AWS_ROLE_SESSION_NAME"] == NAME and env["AWS_REGION"] == "us-east-1"
    finally:
        aws.stop()
