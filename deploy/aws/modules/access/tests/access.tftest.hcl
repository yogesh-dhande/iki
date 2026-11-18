# Runs against a mocked AWS provider: no account, no credentials. Each run checks a
# property ADR 0097 promises. The policies themselves are checked as written, through
# override_data, since a mocked aws_iam_policy_document renders nothing.

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
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/iki-test" }
  }
  mock_resource "aws_iam_policy" {
    defaults = { arn = "arn:aws:iam::123456789012:policy/iki-test" }
  }
}

variables {
  name         = "iki-test"
  cluster_name = "iki-test"
  cluster_arn  = "arn:aws:eks:eu-west-1:123456789012:cluster/iki-test"
  files = {
    bucket_arn  = "arn:aws:s3:::iki-test-files-123456789012"
    kms_key_arn = "arn:aws:kms:eu-west-1:123456789012:key/files"
    prefix      = "prod/"
  }
  external_secrets = {
    secret_arns = ["arn:aws:secretsmanager:eu-west-1:123456789012:secret:iki-test/django-AbCdEf"]
    kms_key_arn = "arn:aws:kms:eu-west-1:123456789012:key/secrets"
  }
}

run "each_server_pod_gets_its_own_role_bound_to_its_service_account" {
  assert {
    condition     = { for k, a in aws_eks_pod_identity_association.server : k => "${a.namespace}/${a.service_account}" } == { web = "iki/iki-web", tasks = "iki/iki-tasks", maintenance = "iki/iki-maintenance" }
    error_message = "Web, task and maintenance pods should each be bound to their own role."
  }
}

run "roles_trust_this_clusters_pods_only" {
  assert {
    condition     = one([for s in data.aws_iam_policy_document.assume.statement : s if contains(s.actions, "sts:AssumeRole")]).principals == toset([{ type = "Service", identifiers = toset(["pods.eks.amazonaws.com"]) }])
    error_message = "Roles should trust EKS Pod Identity alone."
  }
  assert {
    condition = contains([
      for c in one(data.aws_iam_policy_document.assume.statement).condition : "${c.variable}=${join(",", c.values)}"
    ], "aws:SourceArn=arn:aws:eks:eu-west-1:123456789012:cluster/iki-test")
    error_message = "Roles should trust pods of this cluster only."
  }
}

run "server_pods_reach_only_their_prefix_of_the_files_bucket" {
  assert {
    condition     = one([for s in data.aws_iam_policy_document.server.statement : s.resources if s.sid == "ReadAndWriteFiles"]) == toset(["arn:aws:s3:::iki-test-files-123456789012/prod/*"])
    error_message = "Server pods should read and write only under Iki's prefix."
  }
  assert {
    condition = one([
      for s in data.aws_iam_policy_document.server.statement : [for c in s.condition : "${c.variable}=${join(",", c.values)}"] if s.sid == "FindFiles"
    ]) == ["s3:prefix=prod/*"]
    error_message = "Listing should be limited to Iki's prefix too."
  }
  assert {
    condition     = !anytrue([for s in data.aws_iam_policy_document.server.statement : contains(s.actions, "s3:PutBucketVersioning") || contains(s.actions, "s3:PutBucketPolicy") || contains(s.actions, "s3:*")])
    error_message = "Server pods shouldn't change the bucket's versioning or policy."
  }
}

run "registered_locations_are_read_only" {
  variables {
    register_locations = [{ bucket_arn = "arn:aws:s3:::lab-data", prefix = "runs/", kms_key_arn = "arn:aws:kms:eu-west-1:123456789012:key/lab" }]
  }
  assert {
    condition     = one([for s in data.aws_iam_policy_document.server.statement : s.actions if s.sid == "ReadRegisteredFiles"]) == toset(["s3:GetObject", "s3:GetObjectVersion"])
    error_message = "Iki should only read registered locations."
  }
  assert {
    condition     = one([for s in data.aws_iam_policy_document.server.statement : s.resources if s.sid == "ReadRegisteredFiles"]) == toset(["arn:aws:s3:::lab-data/runs/*"])
    error_message = "Only under each location's prefix."
  }
}

run "no_provisioner_role_without_the_vm_tier" {
  assert {
    condition     = length(aws_iam_role.provisioner) == 0 && output.provisioner_role_arn == null
    error_message = "Without the VM tier, nothing should be able to start machines."
  }
}

run "the_provisioner_touches_only_its_own_machines" {
  variables {
    vm_tier = {
      owner            = "iki-test"
      worker_role_arn  = "arn:aws:iam::123456789012:role/iki-test-vm-worker"
      disk_kms_key_arn = "arn:aws:kms:eu-west-1:123456789012:key/disks"
      regions          = ["eu-west-1"]
    }
  }
  assert {
    condition = alltrue([
      for s in data.aws_iam_policy_document.provisioner[0].statement :
      contains([for c in s.condition : "${c.variable}=${join(",", c.values)}"], "aws:RequestTag/iki.ikisci.org/deployment=iki-test")
      if s.sid == "StartOnlyTaggedMachines"
    ])
    error_message = "It should start machines only with its deployment's tag."
  }
  assert {
    condition = alltrue([
      for s in data.aws_iam_policy_document.provisioner[0].statement :
      contains([for c in s.condition : "${c.variable}=${join(",", c.values)}"], "aws:ResourceTag/iki.ikisci.org/deployment=iki-test")
      if contains(s.actions, "ec2:TerminateInstances")
    ])
    error_message = "It should terminate only machines with its deployment's tag."
  }
  assert {
    condition = alltrue([
      for s in data.aws_iam_policy_document.provisioner[0].statement :
      contains([for c in s.condition : "${c.variable}=${join(",", c.values)}"], "aws:ResourceTag/iki.ikisci.org/deployment=iki-test")
      if contains(s.actions, "ec2:DeleteVolume") || contains(s.actions, "ec2:DeleteSnapshot")
    ])
    error_message = "It should delete only disks and snapshots with its deployment's tag (ADR 0076)."
  }
  assert {
    condition = alltrue([
      for s in data.aws_iam_policy_document.provisioner[0].statement :
      s.resources == toset(["arn:aws:kms:eu-west-1:123456789012:key/disks"])
      if anytrue([for a in s.actions : startswith(a, "kms:")])
    ])
    error_message = "It should use only the machines' disk key."
  }
  assert {
    condition     = one([for s in data.aws_iam_policy_document.provisioner[0].statement : s.resources if s.sid == "HandMachinesTheirRole"]) == toset(["arn:aws:iam::123456789012:role/iki-test-vm-worker"])
    error_message = "It should pass only the machines' own role."
  }
  assert {
    condition     = aws_eks_pod_identity_association.provisioner[0].service_account == "iki-provisioner"
    error_message = "The role should be bound to the provisioner's service account."
  }
}

run "external_secrets_reads_this_deployments_secrets_only" {
  assert {
    condition     = one([for s in data.aws_iam_policy_document.external_secrets.statement : s.resources if s.sid == "ReadThisDeploymentsSecrets"]) == toset(["arn:aws:secretsmanager:eu-west-1:123456789012:secret:iki-test/django-AbCdEf"])
    error_message = "External Secrets should read only the secrets it's given."
  }
  assert {
    condition     = aws_eks_pod_identity_association.external_secrets.namespace == "external-secrets"
    error_message = "Bound to External Secrets' own service account."
  }
}

run "controllers_are_optional" {
  variables {
    load_balancer_controller = { enabled = false }
    cluster_autoscaler       = { enabled = false }
  }
  assert {
    condition     = length(aws_iam_role.load_balancer_controller) == 0 && length(aws_iam_role.cluster_autoscaler) == 0
    error_message = "Turning a controller off should leave no role for it."
  }
}

run "the_autoscaler_scales_only_this_clusters_node_groups" {
  assert {
    condition = one([
      for s in data.aws_iam_policy_document.cluster_autoscaler[0].statement : [for c in s.condition : c.variable] if s.sid == "ScaleOnlyThisClustersNodeGroups"
    ]) == ["aws:ResourceTag/k8s.io/cluster-autoscaler/iki-test"]
    error_message = "The autoscaler should change only node groups tagged as this cluster's."
  }
}

run "no_external_dns_role_without_a_zone" {
  assert {
    condition     = length(aws_iam_role.external_dns) == 0 && output.external_dns_role_arn == null
    error_message = "Without a zone, ExternalDNS gets no role."
  }
}

run "external_dns_changes_only_its_zone" {
  variables {
    external_dns = { zone_arn = "arn:aws:route53:::hostedzone/Z0123456789" }
  }
  assert {
    condition     = one([for s in data.aws_iam_policy_document.external_dns[0].statement : s.resources if s.sid == "NameTheLoadBalancerInItsZone"]) == toset(["arn:aws:route53:::hostedzone/Z0123456789"])
    error_message = "ExternalDNS should change records in its zone alone."
  }
  assert {
    condition     = "${aws_eks_pod_identity_association.external_dns[0].namespace}/${aws_eks_pod_identity_association.external_dns[0].service_account}" == "kube-system/external-dns"
    error_message = "Bound to ExternalDNS's own service account."
  }
}
