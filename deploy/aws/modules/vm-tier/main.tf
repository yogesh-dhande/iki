# What the VM tier's machines are given (ADR 0074): a role that can only pull their
# images, an encrypted disk key, and a security group that lets nothing in and lets them
# reach only Iki, AWS's endpoints and the file systems named. The provisioner starts them
# with these, requiring IMDSv2 with one hop and no public address; templates name them.

data "aws_caller_identity" "here" {}

data "aws_partition" "here" {}

locals {
  account   = data.aws_caller_identity.here.account_id
  partition = data.aws_partition.here.partition
  tags      = merge(var.tags, { "iki.ikisci.org/deployment" = var.name })
  lustre    = [for f in var.file_systems : f.security_group_id if f.kind == "lustre"]
  nfs       = [for f in var.file_systems : f.security_group_id if f.kind == "nfs"]
}

# --- The machines' role ------------------------------------------------------------------

data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account]
    }
  }
}

resource "aws_iam_role" "worker" {
  name               = "${var.name}-vm-worker"
  description        = "The VM tier's machines: pull their images, nothing else"
  assume_role_policy = data.aws_iam_policy_document.assume.json
  tags               = local.tags
}

# Pull only from the repositories named: a run's machine holds no other credentials.
data "aws_iam_policy_document" "pull" {
  count = length(var.ecr_repository_arns) == 0 ? 0 : 1
  statement {
    sid       = "PullTheirImages"
    actions   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"]
    resources = var.ecr_repository_arns
  }
  statement {
    #checkov:skip=CKV_AWS_356:ECR's login token isn't for a repository; the pulls above are.
    sid       = "LogInToEcr"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "pull" {
  count  = length(var.ecr_repository_arns) == 0 ? 0 : 1
  name   = "pull-images"
  role   = aws_iam_role.worker.id
  policy = data.aws_iam_policy_document.pull[0].json
}

resource "aws_iam_instance_profile" "worker" {
  name = "${var.name}-vm-worker"
  role = aws_iam_role.worker.name
  tags = local.tags
}

# --- Their disks' key --------------------------------------------------------------------

# The account's IAM decides who may use it: the provisioner's role is granted it in the
# access module, and EC2 uses it on the provisioner's behalf.
data "aws_iam_policy_document" "disks" {
  #checkov:skip=CKV_AWS_109:A key policy's "*" is the key itself; this is AWS's default statement, which hands the key's control to the account's IAM.
  #checkov:skip=CKV_AWS_111:As above: the account's IAM policies decide who may write with it.
  #checkov:skip=CKV_AWS_356:As above: in a key policy, "*" can only mean this key.
  statement {
    sid       = "TheAccountManagesIt"
    actions   = ["kms:*"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["arn:${local.partition}:iam::${local.account}:root"]
    }
  }
}

resource "aws_kms_key" "disks" {
  description             = "${var.name}: the VM tier's disks and their snapshots"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.disks.json
  tags                    = local.tags
}

resource "aws_kms_alias" "disks" {
  name          = "alias/${var.name}-vm-disks"
  target_key_id = aws_kms_key.disks.key_id
}

# --- Their security group ----------------------------------------------------------------

resource "aws_security_group" "machines" {
  #checkov:skip=CKV2_AWS_5:The provisioner attaches it to the machines it starts, which templates name it for.
  name        = "${var.name}-vm-machines"
  description = "The VM tier's machines: nothing in; out to Iki, AWS's endpoints and file systems only"
  vpc_id      = var.vpc_id
  tags        = merge(local.tags, { Name = "${var.name}-vm-machines" })
}

resource "aws_vpc_security_group_egress_rule" "vpc" {
  security_group_id = aws_security_group.machines.id
  description       = "Iki's server, the egress proxy and AWS's interface endpoints, over TLS"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = var.vpc_cidr
  tags              = local.tags
}

resource "aws_vpc_security_group_egress_rule" "s3" {
  security_group_id = aws_security_group.machines.id
  description       = "S3, through its gateway endpoint: images' layers and stored files"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  prefix_list_id    = var.s3_prefix_list_id
  tags              = local.tags
}

resource "aws_vpc_security_group_egress_rule" "dns" {
  for_each          = toset(["udp", "tcp"])
  security_group_id = aws_security_group.machines.id
  description       = "The VPC's DNS resolver"
  ip_protocol       = each.value
  from_port         = 53
  to_port           = 53
  cidr_ipv4         = "${cidrhost(var.vpc_cidr, 2)}/32"
  tags              = local.tags
}

resource "aws_vpc_security_group_egress_rule" "https" {
  for_each          = toset(var.extra_https_cidrs)
  security_group_id = aws_security_group.machines.id
  description       = "Over TLS, as asked"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = each.value
  tags              = local.tags
}

resource "aws_vpc_security_group_egress_rule" "nfs" {
  for_each                     = toset(local.nfs)
  security_group_id            = aws_security_group.machines.id
  description                  = "A shared file system, over NFS"
  ip_protocol                  = "tcp"
  from_port                    = 2049
  to_port                      = 2049
  referenced_security_group_id = each.value
  tags                         = local.tags
}

resource "aws_vpc_security_group_egress_rule" "lustre" {
  for_each                     = { for p in setproduct(local.lustre, [[988, 988], [1018, 1023]]) : "${p[0]}:${p[1][0]}" => p }
  security_group_id            = aws_security_group.machines.id
  description                  = "A shared file system, over Lustre"
  ip_protocol                  = "tcp"
  from_port                    = each.value[1][0]
  to_port                      = each.value[1][1]
  referenced_security_group_id = each.value[0]
  tags                         = local.tags
}
