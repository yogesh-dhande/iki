# An IAM role for each pod that calls AWS, bound by EKS Pod Identity (ADR 0097), each
# allowed only what it uses. Backend workers and run pods get none (ADR 0043, 0061).

data "aws_caller_identity" "here" {}

data "aws_partition" "here" {}

data "aws_region" "here" {}

locals {
  account   = data.aws_caller_identity.here.account_id
  partition = data.aws_partition.here.partition
  tags      = merge(var.tags, { "iki.ikisci.org/deployment" = var.name })
  prefix    = var.files.prefix
  # Iki's own server pods: web, task runners and the hourly maintenance job. Each has its
  # own role, so CloudTrail says which one did what, with the same permissions.
  server = {
    web         = "${var.release}-web"
    tasks       = "${var.release}-tasks"
    maintenance = "${var.release}-maintenance"
  }
}

# Pods of this cluster only, in this account: Pod Identity passes the cluster's ARN.
data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]
    principals {
      type        = "Service"
      identifiers = ["pods.eks.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account]
    }
    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [var.cluster_arn]
    }
  }
}

# --- Iki's server pods -------------------------------------------------------------------

# The files bucket under Iki's prefix: read, write, and delete versions (artifacts and
# bundles expire, ADR 0068 and 0096), with its key. Registered locations, read only.
data "aws_iam_policy_document" "server" {
  statement {
    sid       = "FindFiles"
    actions   = ["s3:ListBucket", "s3:ListBucketVersions"]
    resources = [var.files.bucket_arn]
    dynamic "condition" {
      for_each = local.prefix == "" ? [] : [local.prefix]
      content {
        test     = "StringLike"
        variable = "s3:prefix"
        values   = ["${condition.value}*"]
      }
    }
  }
  statement {
    sid = "CheckTheBucket"
    # `iki-server setup_storage` checks versioning and sets Iki's CORS and lifecycle rules.
    actions = [
      "s3:GetBucketVersioning", "s3:GetBucketLocation", "s3:GetBucketCORS", "s3:PutBucketCORS",
      "s3:GetLifecycleConfiguration", "s3:PutLifecycleConfiguration",
    ]
    resources = [var.files.bucket_arn]
  }
  statement {
    sid = "ReadAndWriteFiles"
    actions = [
      "s3:GetObject", "s3:GetObjectVersion",
      "s3:PutObject", "s3:DeleteObject", "s3:DeleteObjectVersion",
      "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts",
    ]
    resources = ["${var.files.bucket_arn}/${local.prefix}*"]
  }
  statement {
    sid       = "UseTheFilesKey"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey", "kms:Encrypt", "kms:DescribeKey"]
    resources = [var.files.kms_key_arn]
  }

  dynamic "statement" {
    for_each = length(var.register_locations) == 0 ? [] : [1]
    content {
      sid       = "FindRegisteredFiles"
      actions   = ["s3:ListBucket", "s3:GetBucketVersioning", "s3:GetBucketLocation"]
      resources = distinct([for l in var.register_locations : l.bucket_arn])
    }
  }
  dynamic "statement" {
    for_each = length(var.register_locations) == 0 ? [] : [1]
    content {
      sid       = "ReadRegisteredFiles"
      actions   = ["s3:GetObject", "s3:GetObjectVersion"]
      resources = [for l in var.register_locations : "${l.bucket_arn}/${l.prefix}*"]
    }
  }
  dynamic "statement" {
    for_each = length(compact([for l in var.register_locations : l.kms_key_arn])) == 0 ? [] : [1]
    content {
      sid       = "DecryptRegisteredFiles"
      actions   = ["kms:Decrypt"]
      resources = distinct(compact([for l in var.register_locations : l.kms_key_arn]))
    }
  }
}

resource "aws_iam_policy" "server" {
  name        = "${var.name}-server"
  description = "Iki's server pods: the files bucket under its prefix, and registered locations read-only"
  policy      = data.aws_iam_policy_document.server.json
  tags        = local.tags
}

resource "aws_iam_role" "server" {
  for_each           = local.server
  name               = "${var.name}-${each.key}"
  description        = "Iki's ${each.key} pods (${var.namespace}/${each.value})"
  assume_role_policy = data.aws_iam_policy_document.assume.json
  tags               = local.tags
}

resource "aws_iam_role_policy_attachment" "server" {
  for_each   = local.server
  role       = aws_iam_role.server[each.key].name
  policy_arn = aws_iam_policy.server.arn
}

resource "aws_eks_pod_identity_association" "server" {
  for_each        = local.server
  cluster_name    = var.cluster_name
  namespace       = var.namespace
  service_account = each.value
  role_arn        = aws_iam_role.server[each.key].arn
  tags            = local.tags
}

# --- The VM tier's provisioner (ADR 0074) ------------------------------------------------

locals {
  regions  = var.vm_tier == null ? [] : var.vm_tier.regions
  instance = [for r in local.regions : "arn:${local.partition}:ec2:${r}:${local.account}:instance/*"]
  volume   = [for r in local.regions : "arn:${local.partition}:ec2:${r}:${local.account}:volume/*"]
  snapshot = [for r in local.regions : "arn:${local.partition}:ec2:${r}::snapshot/*"]
}

# Starts machines only if they carry its deployment's tag, and stops, tags or reads only
# those that do: deployments sharing an account never touch each other's. ADR 0074's
# statements, then ADR 0076's for volumes' disks and snapshots, then the disks' key.
data "aws_iam_policy_document" "provisioner" {
  count = var.vm_tier == null ? 0 : 1
  statement {
    sid     = "StartOnlyTaggedMachines"
    actions = ["ec2:RunInstances"]
    resources = flatten([for r in local.regions : [
      "arn:${local.partition}:ec2:${r}:${local.account}:instance/*",
      "arn:${local.partition}:ec2:${r}:${local.account}:volume/*",
      "arn:${local.partition}:ec2:${r}:${local.account}:network-interface/*",
    ]])
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/iki.ikisci.org/deployment"
      values   = [var.vm_tier.owner]
    }
  }
  statement {
    sid     = "FromTheTemplatesImagesAndNetworks"
    actions = ["ec2:RunInstances"]
    resources = flatten([for r in local.regions : [
      "arn:${local.partition}:ec2:${r}::image/*",
      "arn:${local.partition}:ec2:${r}:${local.account}:subnet/*",
      "arn:${local.partition}:ec2:${r}:${local.account}:security-group/*",
    ]])
  }
  statement {
    sid       = "TagThemAsTheyStart"
    actions   = ["ec2:CreateTags"]
    resources = flatten([for r in local.regions : "arn:${local.partition}:ec2:${r}:${local.account}:*/*"])
    condition {
      test     = "StringEquals"
      variable = "ec2:CreateAction"
      values   = ["RunInstances", "CreateSnapshot"]
    }
  }
  statement {
    sid       = "StopAndReadOnlyItsOwn"
    actions   = ["ec2:TerminateInstances", "ec2:GetConsoleOutput"]
    resources = local.instance
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/iki.ikisci.org/deployment"
      values   = [var.vm_tier.owner]
    }
  }
  statement {
    sid       = "RetagOnlyItsOwn"
    actions   = ["ec2:CreateTags", "ec2:DeleteTags"]
    resources = local.instance
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/iki.ikisci.org/deployment"
      values   = [var.vm_tier.owner]
    }
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "aws:TagKeys"
      values   = ["iki.ikisci.org/run", "iki.ikisci.org/project", "iki.ikisci.org/team"]
    }
  }
  statement {
    #checkov:skip=CKV_AWS_356:EC2's Describe calls can't be scoped to resources; the provisioner then acts only on machines with its tag.
    sid       = "SeeMachines"
    actions   = ["ec2:DescribeInstances"]
    resources = ["*"]
  }
  statement {
    sid       = "HandMachinesTheirRole"
    actions   = ["iam:PassRole"]
    resources = [var.vm_tier.worker_role_arn]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ec2.amazonaws.com"]
    }
  }
  statement {
    sid       = "StartFromItsOwnSnapshots"
    actions   = ["ec2:RunInstances"]
    resources = local.snapshot
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/iki.ikisci.org/deployment"
      values   = [var.vm_tier.owner]
    }
  }
  statement {
    sid       = "SnapshotItsOwnDisks"
    actions   = ["ec2:CreateSnapshot"]
    resources = local.volume
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/iki.ikisci.org/deployment"
      values   = [var.vm_tier.owner]
    }
  }
  statement {
    sid       = "MakeOnlyTaggedSnapshots"
    actions   = ["ec2:CreateSnapshot"]
    resources = local.snapshot
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/iki.ikisci.org/deployment"
      values   = [var.vm_tier.owner]
    }
  }
  statement {
    sid       = "NameItsOwnDisks"
    actions   = ["ec2:CreateTags"]
    resources = local.volume
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/iki.ikisci.org/deployment"
      values   = [var.vm_tier.owner]
    }
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "aws:TagKeys"
      values   = ["iki.ikisci.org/volume"]
    }
  }
  statement {
    sid       = "EndAndSpeedUpItsOwn"
    actions   = ["ec2:DeleteVolume", "ec2:DeleteSnapshot", "ec2:EnableFastSnapshotRestores", "ec2:DisableFastSnapshotRestores"]
    resources = concat(local.volume, local.snapshot)
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/iki.ikisci.org/deployment"
      values   = [var.vm_tier.owner]
    }
  }
  statement {
    #checkov:skip=CKV_AWS_356:EC2's Describe calls can't be scoped to resources.
    sid       = "SeeDisksAndSnapshots"
    actions   = ["ec2:DescribeVolumes", "ec2:DescribeSnapshots", "ec2:DescribeFastSnapshotRestores", "ec2:DescribeSubnets"]
    resources = ["*"]
  }
  # EC2 encrypts the machines' disks with this key on the provisioner's behalf.
  statement {
    sid       = "EncryptTheirDisks"
    actions   = ["kms:Encrypt", "kms:Decrypt", "kms:ReEncrypt*", "kms:GenerateDataKeyWithoutPlaintext", "kms:DescribeKey"]
    resources = [var.vm_tier.disk_kms_key_arn]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = [for r in local.regions : "ec2.${r}.amazonaws.com"]
    }
  }
  statement {
    sid       = "LetEc2UseTheKey"
    actions   = ["kms:CreateGrant"]
    resources = [var.vm_tier.disk_kms_key_arn]
    condition {
      test     = "Bool"
      variable = "kms:GrantIsForAWSResource"
      values   = ["true"]
    }
  }
  statement {
    sid       = "FindPublicImages"
    actions   = ["ssm:GetParameters"]
    resources = [for r in local.regions : "arn:${local.partition}:ssm:${r}::parameter/aws/service/*"]
  }
}

resource "aws_iam_role" "provisioner" {
  count              = var.vm_tier == null ? 0 : 1
  name               = "${var.name}-provisioner"
  description        = "Iki's provisioner (${var.namespace}/${var.release}-provisioner): machines tagged ${var.vm_tier.owner} only"
  assume_role_policy = data.aws_iam_policy_document.assume.json
  tags               = local.tags
}

resource "aws_iam_role_policy" "provisioner" {
  count  = var.vm_tier == null ? 0 : 1
  name   = "vm-tier"
  role   = aws_iam_role.provisioner[0].id
  policy = data.aws_iam_policy_document.provisioner[0].json
}

resource "aws_eks_pod_identity_association" "provisioner" {
  count           = var.vm_tier == null ? 0 : 1
  cluster_name    = var.cluster_name
  namespace       = var.namespace
  service_account = "${var.release}-provisioner"
  role_arn        = aws_iam_role.provisioner[0].arn
  tags            = local.tags
}

# --- External Secrets --------------------------------------------------------------------

# Reads this deployment's secrets, and no others, into the cluster.
data "aws_iam_policy_document" "external_secrets" {
  statement {
    sid       = "ReadThisDeploymentsSecrets"
    actions   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
    resources = var.external_secrets.secret_arns
  }
  statement {
    sid       = "DecryptThem"
    actions   = ["kms:Decrypt"]
    resources = [var.external_secrets.kms_key_arn]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["secretsmanager.${data.aws_region.here.region}.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "external_secrets" {
  name               = "${var.name}-external-secrets"
  description        = "External Secrets Operator: ${var.name}'s secrets only"
  assume_role_policy = data.aws_iam_policy_document.assume.json
  tags               = local.tags
}

resource "aws_iam_role_policy" "external_secrets" {
  name   = "read-secrets"
  role   = aws_iam_role.external_secrets.id
  policy = data.aws_iam_policy_document.external_secrets.json
}

resource "aws_eks_pod_identity_association" "external_secrets" {
  cluster_name    = var.cluster_name
  namespace       = var.external_secrets.namespace
  service_account = var.external_secrets.service_account
  role_arn        = aws_iam_role.external_secrets.arn
  tags            = local.tags
}

# --- The AWS Load Balancer Controller ----------------------------------------------------

resource "aws_iam_policy" "load_balancer_controller" {
  #checkov:skip=CKV_AWS_355:AWS's own policy for the controller (v3.6.3, unchanged): EC2 and ELB's Describe calls can't be scoped to resources.
  #checkov:skip=CKV_AWS_290:As above: its writes are scoped by the elbv2.k8s.aws/cluster tag wherever AWS allows.
  #checkov:skip=CKV_AWS_289:As above.
  count       = var.load_balancer_controller.enabled ? 1 : 0
  name        = "${var.name}-aws-load-balancer-controller"
  description = "AWS's policy for the AWS Load Balancer Controller, v3.6.3, unchanged"
  policy      = file("${path.module}/policies/aws-load-balancer-controller-v3.6.3.json")
  tags        = local.tags
}

resource "aws_iam_role" "load_balancer_controller" {
  count              = var.load_balancer_controller.enabled ? 1 : 0
  name               = "${var.name}-aws-load-balancer-controller"
  assume_role_policy = data.aws_iam_policy_document.assume.json
  tags               = local.tags
}

resource "aws_iam_role_policy_attachment" "load_balancer_controller" {
  count      = var.load_balancer_controller.enabled ? 1 : 0
  role       = aws_iam_role.load_balancer_controller[0].name
  policy_arn = aws_iam_policy.load_balancer_controller[0].arn
}

resource "aws_eks_pod_identity_association" "load_balancer_controller" {
  count           = var.load_balancer_controller.enabled ? 1 : 0
  cluster_name    = var.cluster_name
  namespace       = var.load_balancer_controller.namespace
  service_account = var.load_balancer_controller.service_account
  role_arn        = aws_iam_role.load_balancer_controller[0].arn
  tags            = local.tags
}

# --- The Cluster Autoscaler --------------------------------------------------------------

# Scales only this cluster's node groups, which the cluster module tags as its own.
data "aws_iam_policy_document" "cluster_autoscaler" {
  count = var.cluster_autoscaler.enabled ? 1 : 0
  statement {
    #checkov:skip=CKV_AWS_356:Auto Scaling's and EC2's Describe calls can't be scoped to resources.
    sid = "SeeNodeGroups"
    actions = [
      "autoscaling:DescribeAutoScalingGroups", "autoscaling:DescribeAutoScalingInstances",
      "autoscaling:DescribeLaunchConfigurations", "autoscaling:DescribeScalingActivities",
      "autoscaling:DescribeTags", "ec2:DescribeImages", "ec2:DescribeInstanceTypes",
      "ec2:DescribeLaunchTemplateVersions", "ec2:GetInstanceTypesFromInstanceRequirements",
    ]
    resources = ["*"]
  }
  statement {
    sid       = "SeeThisClustersNodeGroups"
    actions   = ["eks:DescribeNodegroup"]
    resources = ["arn:${local.partition}:eks:${data.aws_region.here.region}:${local.account}:nodegroup/${var.cluster_name}/*"]
  }
  statement {
    sid       = "ScaleOnlyThisClustersNodeGroups"
    actions   = ["autoscaling:SetDesiredCapacity", "autoscaling:TerminateInstanceInAutoScalingGroup"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/k8s.io/cluster-autoscaler/${var.cluster_name}"
      values   = ["owned"]
    }
  }
}

resource "aws_iam_role" "cluster_autoscaler" {
  count              = var.cluster_autoscaler.enabled ? 1 : 0
  name               = "${var.name}-cluster-autoscaler"
  assume_role_policy = data.aws_iam_policy_document.assume.json
  tags               = local.tags
}

resource "aws_iam_role_policy" "cluster_autoscaler" {
  count  = var.cluster_autoscaler.enabled ? 1 : 0
  name   = "scale-node-groups"
  role   = aws_iam_role.cluster_autoscaler[0].id
  policy = data.aws_iam_policy_document.cluster_autoscaler[0].json
}

resource "aws_eks_pod_identity_association" "cluster_autoscaler" {
  count           = var.cluster_autoscaler.enabled ? 1 : 0
  cluster_name    = var.cluster_name
  namespace       = var.cluster_autoscaler.namespace
  service_account = var.cluster_autoscaler.service_account
  role_arn        = aws_iam_role.cluster_autoscaler[0].arn
  tags            = local.tags
}
