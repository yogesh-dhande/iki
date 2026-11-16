# EKS with private nodes (ADR 0097): Kubernetes Secrets encrypted with a key of their
# own, every control-plane log kept, access only through IAM access entries, and nodes
# that pods can't take the role of.

data "aws_region" "here" {}

data "aws_caller_identity" "here" {}

data "aws_partition" "here" {}

locals {
  account   = data.aws_caller_identity.here.account_id
  partition = data.aws_partition.here.partition
  region    = data.aws_region.here.region
  root      = "arn:${local.partition}:iam::${local.account}:root"
  tags      = merge(var.tags, { "iki.ikisci.org/deployment" = var.name })
  log_group = "/aws/eks/${var.name}/cluster"
}

# --- Keys --------------------------------------------------------------------------------

# Envelope encryption of Kubernetes Secrets: etcd holds them encrypted with this key.
resource "aws_kms_key" "secrets" {
  description             = "${var.name}: Kubernetes Secrets"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.account_key.json
  tags                    = local.tags
}

resource "aws_kms_alias" "secrets" {
  name          = "alias/${var.name}-kubernetes-secrets"
  target_key_id = aws_kms_key.secrets.key_id
}

data "aws_iam_policy_document" "account_key" {
  #checkov:skip=CKV_AWS_109:A key policy's "*" is the key itself; this is AWS's default statement, which hands the key's control to the account's IAM.
  #checkov:skip=CKV_AWS_111:As above: the account's IAM policies decide who may write with it.
  #checkov:skip=CKV_AWS_356:As above: in a key policy, "*" can only mean this key.
  statement {
    sid       = "TheAccountManagesIt"
    actions   = ["kms:*"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = [local.root]
    }
  }
}

# The nodes' disks. EC2 Auto Scaling starts the nodes, so its service-linked role uses it.
resource "aws_kms_key" "nodes" {
  description             = "${var.name}: node disks"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.nodes_key.json
  tags                    = local.tags
}

resource "aws_kms_alias" "nodes" {
  name          = "alias/${var.name}-node-disks"
  target_key_id = aws_kms_key.nodes.key_id
}

data "aws_iam_policy_document" "nodes_key" {
  #checkov:skip=CKV_AWS_109:A key policy's "*" is the key itself; this is AWS's default statement, which hands the key's control to the account's IAM.
  #checkov:skip=CKV_AWS_111:As above: the account's IAM policies decide who may write with it.
  #checkov:skip=CKV_AWS_356:As above: in a key policy, "*" can only mean this key.
  source_policy_documents = [data.aws_iam_policy_document.account_key.json]
  statement {
    sid       = "AutoScalingEncryptsNodeDisks"
    actions   = ["kms:Encrypt", "kms:Decrypt", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:DescribeKey"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["arn:${local.partition}:iam::${local.account}:role/aws-service-role/autoscaling.amazonaws.com/AWSServiceRoleForAutoScaling"]
    }
  }
  statement {
    sid       = "AutoScalingAttachesThem"
    actions   = ["kms:CreateGrant"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["arn:${local.partition}:iam::${local.account}:role/aws-service-role/autoscaling.amazonaws.com/AWSServiceRoleForAutoScaling"]
    }
    condition {
      test     = "Bool"
      variable = "kms:GrantIsForAWSResource"
      values   = ["true"]
    }
  }
}

resource "aws_kms_key" "logs" {
  description             = "${var.name}: EKS control-plane logs"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.logs_key.json
  tags                    = local.tags
}

resource "aws_kms_alias" "logs" {
  name          = "alias/${var.name}-eks-logs"
  target_key_id = aws_kms_key.logs.key_id
}

data "aws_iam_policy_document" "logs_key" {
  #checkov:skip=CKV_AWS_109:A key policy's "*" is the key itself; this is AWS's default statement, which hands the key's control to the account's IAM.
  #checkov:skip=CKV_AWS_111:As above: the account's IAM policies decide who may write with it.
  #checkov:skip=CKV_AWS_356:As above: in a key policy, "*" can only mean this key.
  source_policy_documents = [data.aws_iam_policy_document.account_key.json]
  statement {
    sid       = "CloudWatchLogsUsesIt"
    actions   = ["kms:Encrypt*", "kms:Decrypt*", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"]
    resources = ["*"]
    principals {
      type        = "Service"
      identifiers = ["logs.${local.region}.amazonaws.com"]
    }
    condition {
      test     = "ArnLike"
      variable = "kms:EncryptionContext:aws:logs:arn"
      values   = ["arn:${local.partition}:logs:${local.region}:${local.account}:log-group:${local.log_group}"]
    }
  }
}

# --- The cluster -------------------------------------------------------------------------

# Made before the cluster, so EKS writes into a group that's encrypted and expires.
resource "aws_cloudwatch_log_group" "cluster" {
  name              = local.log_group
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.logs.arn
  tags              = local.tags
}

data "aws_iam_policy_document" "cluster_assume" {
  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]
    principals {
      type        = "Service"
      identifiers = ["eks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "cluster" {
  name               = "${var.name}-eks-cluster"
  assume_role_policy = data.aws_iam_policy_document.cluster_assume.json
  tags               = local.tags
}

resource "aws_iam_role_policy_attachment" "cluster" {
  role       = aws_iam_role.cluster.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/AmazonEKSClusterPolicy"
}

resource "aws_eks_cluster" "this" {
  name     = var.name
  version  = var.kubernetes_version
  role_arn = aws_iam_role.cluster.arn

  vpc_config {
    subnet_ids              = var.subnet_ids
    endpoint_private_access = true
    endpoint_public_access  = length(var.public_access_cidrs) > 0
    public_access_cidrs     = length(var.public_access_cidrs) > 0 ? var.public_access_cidrs : null
  }

  encryption_config {
    resources = ["secrets"]
    provider {
      key_arn = aws_kms_key.secrets.arn
    }
  }

  enabled_cluster_log_types = ["api", "audit", "authenticator", "controllerManager", "scheduler"]

  # Access only through access entries: no aws-auth ConfigMap, and whoever creates the
  # cluster isn't made its admin by default.
  access_config {
    authentication_mode                         = "API"
    bootstrap_cluster_creator_admin_permissions = false
  }

  # Standard support only: a cluster left on an old version fails its upgrade checks
  # rather than drifting into extended support's charges.
  upgrade_policy {
    support_type = "STANDARD"
  }

  bootstrap_self_managed_addons = false

  tags       = local.tags
  depends_on = [aws_iam_role_policy_attachment.cluster, aws_cloudwatch_log_group.cluster]
}

resource "aws_eks_access_entry" "admin" {
  for_each      = toset(var.admin_principal_arns)
  cluster_name  = aws_eks_cluster.this.name
  principal_arn = each.key
  tags          = local.tags
}

resource "aws_eks_access_policy_association" "admin" {
  for_each      = toset(var.admin_principal_arns)
  cluster_name  = aws_eks_cluster.this.name
  principal_arn = aws_eks_access_entry.admin[each.key].principal_arn
  policy_arn    = "arn:${local.partition}:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
  access_scope {
    type = "cluster"
  }
}

# --- Pod Identity roles for the cluster's own add-ons ------------------------------------

data "aws_iam_policy_document" "pod_identity_assume" {
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
      values   = [aws_eks_cluster.this.arn]
    }
  }
}

# The VPC CNI's permissions go to its own pods, not to every node (EKS best practices).
resource "aws_iam_role" "vpc_cni" {
  name               = "${var.name}-vpc-cni"
  assume_role_policy = data.aws_iam_policy_document.pod_identity_assume.json
  tags               = local.tags
}

resource "aws_iam_role_policy_attachment" "vpc_cni" {
  role       = aws_iam_role.vpc_cni.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/AmazonEKS_CNI_Policy"
}

resource "aws_iam_role" "ebs_csi" {
  name               = "${var.name}-ebs-csi"
  assume_role_policy = data.aws_iam_policy_document.pod_identity_assume.json
  tags               = local.tags
}

resource "aws_iam_role_policy_attachment" "ebs_csi" {
  role       = aws_iam_role.ebs_csi.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
}

# --- Add-ons -----------------------------------------------------------------------------

# The Pod Identity agent and the CNI come first: nodes join once the CNI is there, and
# every other role needs the agent.
resource "aws_eks_addon" "pod_identity_agent" {
  cluster_name                = aws_eks_cluster.this.name
  addon_name                  = "eks-pod-identity-agent"
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"
  tags                        = local.tags
}

resource "aws_eks_addon" "vpc_cni" {
  cluster_name                = aws_eks_cluster.this.name
  addon_name                  = "vpc-cni"
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"
  # The chart's NetworkPolicies are only words unless something enforces them.
  configuration_values = jsonencode({ enableNetworkPolicy = "true" })
  pod_identity_association {
    role_arn        = aws_iam_role.vpc_cni.arn
    service_account = "aws-node"
  }
  tags       = local.tags
  depends_on = [aws_eks_addon.pod_identity_agent]
}

resource "aws_eks_addon" "kube_proxy" {
  cluster_name                = aws_eks_cluster.this.name
  addon_name                  = "kube-proxy"
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"
  tags                        = local.tags
}

resource "aws_eks_addon" "coredns" {
  cluster_name                = aws_eks_cluster.this.name
  addon_name                  = "coredns"
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"
  tags                        = local.tags
  depends_on                  = [aws_eks_node_group.this]
}

resource "aws_eks_addon" "ebs_csi" {
  cluster_name                = aws_eks_cluster.this.name
  addon_name                  = "aws-ebs-csi-driver"
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"
  pod_identity_association {
    role_arn        = aws_iam_role.ebs_csi.arn
    service_account = "ebs-csi-controller-sa"
  }
  tags       = local.tags
  depends_on = [aws_eks_node_group.this]
}

# --- Nodes -------------------------------------------------------------------------------

data "aws_iam_policy_document" "node_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "node" {
  name               = "${var.name}-eks-node"
  assume_role_policy = data.aws_iam_policy_document.node_assume.json
  tags               = local.tags
}

# Only what a node itself needs: to join, and to pull images. The CNI's permissions are
# its pods' (above), and nothing here can reach Iki's bucket.
resource "aws_iam_role_policy_attachment" "node" {
  for_each = toset([
    "AmazonEKSWorkerNodePolicy",
    "AmazonEC2ContainerRegistryPullOnly",
  ])
  role       = aws_iam_role.node.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/${each.key}"
}

# Bottlerocket: a minimal, read-only OS built to run containers, with SELinux enforcing.
# Its first disk holds the OS, its second images and pods' scratch space.
resource "aws_launch_template" "node" {
  for_each    = var.node_pools
  name_prefix = "${var.name}-${each.key}-"
  description = "${var.name}: the ${each.key} node pool"

  # IMDSv2 only, one hop: a pod can't reach the node's metadata service, so it can't
  # take the node's role.
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
    instance_metadata_tags      = "disabled"
  }

  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size           = 4
      volume_type           = "gp3"
      encrypted             = true
      kms_key_id            = aws_kms_key.nodes.arn
      delete_on_termination = true
    }
  }

  block_device_mappings {
    device_name = "/dev/xvdb"
    ebs {
      volume_size           = each.value.disk_size_gib
      volume_type           = "gp3"
      encrypted             = true
      kms_key_id            = aws_kms_key.nodes.arn
      delete_on_termination = true
    }
  }

  monitoring {
    enabled = true
  }

  tag_specifications {
    resource_type = "instance"
    tags          = merge(local.tags, { Name = "${var.name}-${each.key}", "iki.ikisci.org/pool" = each.key })
  }

  tag_specifications {
    resource_type = "volume"
    tags          = merge(local.tags, { Name = "${var.name}-${each.key}", "iki.ikisci.org/pool" = each.key })
  }

  tags = local.tags
}

resource "aws_eks_node_group" "this" {
  for_each        = var.node_pools
  cluster_name    = aws_eks_cluster.this.name
  node_group_name = each.key
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = var.subnet_ids
  ami_type        = each.value.gpu ? "BOTTLEROCKET_x86_64_NVIDIA" : "BOTTLEROCKET_x86_64"
  capacity_type   = each.value.capacity_type
  instance_types  = each.value.instance_types

  launch_template {
    id      = aws_launch_template.node[each.key].id
    version = aws_launch_template.node[each.key].latest_version
  }

  scaling_config {
    min_size     = each.value.min_size
    max_size     = each.value.max_size
    desired_size = coalesce(each.value.desired_size, each.value.min_size)
  }

  update_config {
    max_unavailable_percentage = 33
  }

  labels = { "iki.ikisci.org/pool" = each.key }

  dynamic "taint" {
    for_each = each.key == "system" ? [] : [each.key]
    content {
      key    = "iki.ikisci.org/pool"
      value  = taint.value
      effect = "NO_SCHEDULE"
    }
  }

  tags = merge(local.tags, {
    # The cluster autoscaler finds the pools it may scale by these.
    "k8s.io/cluster-autoscaler/enabled"     = "true"
    "k8s.io/cluster-autoscaler/${var.name}" = "owned"
  })

  # The cluster autoscaler moves desired_size; OpenTofu shouldn't move it back.
  lifecycle {
    ignore_changes = [scaling_config[0].desired_size]
  }

  depends_on = [aws_iam_role_policy_attachment.node, aws_eks_addon.vpc_cni]
}
