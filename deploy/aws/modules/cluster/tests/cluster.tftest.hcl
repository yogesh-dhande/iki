# Runs against a mocked AWS provider: no account, no credentials. Each run checks a
# property ADR 0097 promises.

mock_provider "aws" {
  mock_data "aws_region" {
    defaults = { region = "eu-west-1" }
  }
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
  mock_resource "aws_kms_key" {
    defaults = { arn = "arn:aws:kms:eu-west-1:123456789012:key/00000000-0000-0000-0000-000000000000" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/iki-test" }
  }
  mock_resource "aws_eks_cluster" {
    defaults = {
      arn                   = "arn:aws:eks:eu-west-1:123456789012:cluster/iki-test"
      certificate_authority = [{ data = "LS0tLS1CRUdJTg==" }]
    }
  }
  mock_resource "aws_launch_template" {
    defaults = { id = "lt-0123456789abcdef0", latest_version = 1 }
  }
}

variables {
  name               = "iki-test"
  kubernetes_version = "1.35"
  vpc_id             = "vpc-0123"
  subnet_ids         = ["subnet-a", "subnet-b", "subnet-c"]
}

run "kubernetes_secrets_are_encrypted_with_a_rotated_key_of_their_own" {
  assert {
    condition     = one(aws_eks_cluster.this.encryption_config).resources == toset(["secrets"])
    error_message = "Kubernetes Secrets should be envelope-encrypted."
  }
  assert {
    condition     = aws_kms_key.secrets.enable_key_rotation
    error_message = "The Secrets key should rotate."
  }
}

run "every_control_plane_log_is_kept_encrypted" {
  assert {
    condition     = aws_eks_cluster.this.enabled_cluster_log_types == toset(["api", "audit", "authenticator", "controllerManager", "scheduler"])
    error_message = "All five control-plane logs should be on, the audit log above all."
  }
  assert {
    condition     = aws_cloudwatch_log_group.cluster.kms_key_id == aws_kms_key.logs.arn && aws_cloudwatch_log_group.cluster.retention_in_days == 365
    error_message = "Control-plane logs should be encrypted and kept a year."
  }
}

run "the_api_is_private_by_default" {
  assert {
    condition     = one(aws_eks_cluster.this.vpc_config).endpoint_private_access && !one(aws_eks_cluster.this.vpc_config).endpoint_public_access
    error_message = "With no public_access_cidrs, the Kubernetes API should be reachable only inside the VPC."
  }
}

run "the_api_opens_only_to_the_networks_listed" {
  variables {
    public_access_cidrs = ["203.0.113.0/24"]
  }
  assert {
    condition     = one(aws_eks_cluster.this.vpc_config).endpoint_public_access && one(aws_eks_cluster.this.vpc_config).public_access_cidrs == toset(["203.0.113.0/24"])
    error_message = "The public endpoint should admit only the networks listed."
  }
}

run "refuses_to_open_the_api_to_everyone" {
  command = plan
  variables {
    public_access_cidrs = ["0.0.0.0/0"]
  }
  expect_failures = [var.public_access_cidrs]
}

run "only_the_admins_named_get_in" {
  variables {
    admin_principal_arns = ["arn:aws:iam::123456789012:role/platform"]
  }
  assert {
    condition     = one(aws_eks_cluster.this.access_config).authentication_mode == "API" && !one(aws_eks_cluster.this.access_config).bootstrap_cluster_creator_admin_permissions
    error_message = "Access should be through access entries only, and the cluster's creator shouldn't become its admin."
  }
  assert {
    condition     = keys(aws_eks_access_policy_association.admin) == ["arn:aws:iam::123456789012:role/platform"]
    error_message = "Each named admin should get cluster-admin, and nobody else."
  }
  assert {
    condition     = endswith(aws_eks_access_policy_association.admin["arn:aws:iam::123456789012:role/platform"].policy_arn, "/AmazonEKSClusterAdminPolicy")
    error_message = "Admins should get EKS's cluster-admin access policy."
  }
}

run "pods_cant_take_the_nodes_role" {
  assert {
    condition = alltrue([
      for t in aws_launch_template.node :
      one(t.metadata_options).http_tokens == "required" && one(t.metadata_options).http_put_response_hop_limit == 1
    ])
    error_message = "Nodes should require IMDSv2 with a hop limit of 1, so pods can't reach the node's credentials."
  }
}

run "node_disks_are_encrypted_with_their_own_key" {
  assert {
    condition = alltrue(flatten([
      for t in aws_launch_template.node : [
        for d in t.block_device_mappings : one(d.ebs).encrypted == "true" && one(d.ebs).kms_key_id == aws_kms_key.nodes.arn
      ]
    ]))
    error_message = "Every node disk should be encrypted with the node-disks key."
  }
}

run "plugins_code_gets_nodes_of_its_own" {
  assert {
    condition     = length(aws_eks_node_group.this["system"].taint) == 0
    error_message = "The system pool runs Iki itself: no taint."
  }
  assert {
    condition = alltrue([
      for name in ["runs", "gpu"] :
      one(aws_eks_node_group.this[name].taint).key == "iki.ikisci.org/pool" && one(aws_eks_node_group.this[name].taint).value == name && one(aws_eks_node_group.this[name].taint).effect == "NO_SCHEDULE"
    ])
    error_message = "Dedicated pools should be tainted, so only runs that tolerate them land there."
  }
  assert {
    condition     = output.node_pools["runs"].tolerations[0].value == "runs" && length(output.node_pools["system"].tolerations) == 0
    error_message = "The node_pools output should give the toleration each dedicated pool needs."
  }
  assert {
    condition     = aws_eks_node_group.this["gpu"].ami_type == "BOTTLEROCKET_x86_64_NVIDIA" && aws_eks_node_group.this["runs"].ami_type == "BOTTLEROCKET_x86_64"
    error_message = "GPU nodes should run Bottlerocket's NVIDIA variant, the others plain Bottlerocket."
  }
}

run "nodes_hold_only_what_a_node_needs" {
  assert {
    condition     = toset(keys(aws_iam_role_policy_attachment.node)) == toset(["AmazonEKSWorkerNodePolicy", "AmazonEC2ContainerRegistryPullOnly"])
    error_message = "The node role should only join the cluster and pull images; the CNI's permissions belong to its pods."
  }
  assert {
    condition     = one(aws_eks_addon.vpc_cni.pod_identity_association).service_account == "aws-node"
    error_message = "The VPC CNI should get its permissions through Pod Identity."
  }
}

run "network_policies_are_enforced" {
  assert {
    condition     = jsondecode(aws_eks_addon.vpc_cni.configuration_values).enableNetworkPolicy == "true"
    error_message = "The VPC CNI should enforce the chart's NetworkPolicies."
  }
}

run "refuses_a_cluster_without_a_system_pool" {
  command = plan
  variables {
    node_pools = {
      runs = { instance_types = ["m7i.large"], min_size = 1, max_size = 2 }
    }
  }
  expect_failures = [var.node_pools]
}

run "refuses_a_system_pool_of_one" {
  command = plan
  variables {
    node_pools = {
      system = { instance_types = ["m7i.large"], min_size = 1, max_size = 2 }
    }
  }
  expect_failures = [var.node_pools]
}
