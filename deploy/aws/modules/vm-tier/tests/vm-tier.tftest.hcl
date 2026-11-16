# Runs against a mocked AWS provider: no account, no credentials. Each run checks a
# property ADR 0074 and 0097 promise.

mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
  # The provider checks ARNs it's given, so mocked ones must look real.
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/iki-test-vm-worker" }
  }
  mock_resource "aws_kms_key" {
    defaults = { arn = "arn:aws:kms:eu-west-1:123456789012:key/00000000-0000-0000-0000-000000000000" }
  }
}

variables {
  name              = "iki-test"
  vpc_id            = "vpc-0123456789abcdef0"
  vpc_cidr          = "10.40.0.0/16"
  s3_prefix_list_id = "pl-6da54004"
}

run "nothing_gets_in" {
  assert {
    condition     = length(aws_security_group.machines.ingress) == 0
    error_message = "Machines take no inbound connections: their worker dials out."
  }
}

run "out_only_to_the_vpc_s3_and_dns" {
  assert {
    condition     = aws_vpc_security_group_egress_rule.vpc.cidr_ipv4 == "10.40.0.0/16" && aws_vpc_security_group_egress_rule.vpc.from_port == 443
    error_message = "Machines should reach Iki and AWS's endpoints in the VPC, over TLS."
  }
  assert {
    condition     = aws_vpc_security_group_egress_rule.s3.prefix_list_id == "pl-6da54004"
    error_message = "And S3 through its gateway endpoint."
  }
  assert {
    condition     = toset([for r in aws_vpc_security_group_egress_rule.dns : r.cidr_ipv4]) == toset(["10.40.0.2/32"])
    error_message = "DNS should go to the VPC's resolver alone."
  }
  assert {
    condition     = length(aws_vpc_security_group_egress_rule.https) == 0
    error_message = "Nothing else, unless asked."
  }
}

run "file_systems_by_their_ports" {
  variables {
    file_systems = [
      { kind = "nfs", security_group_id = "sg-00000000000000efs" },
      { kind = "lustre", security_group_id = "sg-0000000000000fsx0" },
    ]
  }
  assert {
    condition     = [for r in aws_vpc_security_group_egress_rule.nfs : "${r.referenced_security_group_id}:${r.from_port}"] == ["sg-00000000000000efs:2049"]
    error_message = "NFS on 2049, to the file system's group only."
  }
  assert {
    condition     = toset([for r in aws_vpc_security_group_egress_rule.lustre : "${r.from_port}-${r.to_port}"]) == toset(["988-988", "1018-1023"])
    error_message = "Lustre on 988 and 1018-1023."
  }
}

run "the_machines_role_can_only_pull_images" {
  variables {
    ecr_repository_arns = ["arn:aws:ecr:eu-west-1:123456789012:repository/iki/worker"]
  }
  assert {
    condition     = one([for s in data.aws_iam_policy_document.pull[0].statement : s.resources if s.sid == "PullTheirImages"]) == toset(["arn:aws:ecr:eu-west-1:123456789012:repository/iki/worker"])
    error_message = "Machines should pull only from the repositories named."
  }
  assert {
    condition     = toset(flatten([for s in data.aws_iam_policy_document.pull[0].statement : tolist(s.actions)])) == toset(["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability", "ecr:GetAuthorizationToken"])
    error_message = "And do nothing else."
  }
}

run "no_permissions_for_public_images" {
  assert {
    condition     = length(aws_iam_role_policy.pull) == 0
    error_message = "With images in a public registry, machines need no permissions at all."
  }
}

run "disks_have_their_own_rotated_key" {
  assert {
    condition     = aws_kms_key.disks.enable_key_rotation
    error_message = "The disks' key should rotate."
  }
}

run "refuses_an_unknown_file_system" {
  command = plan
  variables {
    file_systems = [{ kind = "smb", security_group_id = "sg-0" }]
  }
  expect_failures = [var.file_systems]
}
