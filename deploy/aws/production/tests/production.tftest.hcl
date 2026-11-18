# Runs against mocked AWS providers, one per region: no account, no credentials. It
# checks that the modules fit together, and what the chart is told.

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
  mock_resource "aws_kms_key" {
    defaults = { arn = "arn:aws:kms:eu-west-1:123456789012:key/00000000-0000-0000-0000-000000000000" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/iki-test" }
  }
  mock_resource "aws_iam_policy" {
    defaults = { arn = "arn:aws:iam::123456789012:policy/iki-test" }
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
  mock_resource "aws_s3_bucket" {
    defaults = { arn = "arn:aws:s3:::iki-test-files-123456789012" }
  }
  mock_resource "aws_cloudwatch_log_group" {
    defaults = { arn = "arn:aws:logs:eu-west-1:123456789012:log-group:iki-test" }
  }
  mock_resource "aws_secretsmanager_secret" {
    defaults = { arn = "arn:aws:secretsmanager:eu-west-1:123456789012:secret:iki-test/x-AbCdEf" }
  }
  mock_resource "aws_db_instance" {
    defaults = { arn = "arn:aws:rds:eu-west-1:123456789012:db:iki-test" }
  }
  mock_resource "aws_route53_zone" {
    defaults = { arn = "arn:aws:route53:::hostedzone/Z0123456789", zone_id = "Z0123456789" }
  }
  # Recovering: the lost deployment's copy of the files, here.
  mock_data "aws_s3_bucket" {
    defaults = { id = "iki-lost-files-dr-123456789012", arn = "arn:aws:s3:::iki-lost-files-dr-123456789012" }
  }
  mock_data "aws_kms_key" {
    defaults = { arn = "arn:aws:kms:eu-west-1:123456789012:key/22222222-2222-2222-2222-222222222222" }
  }
}

mock_provider "aws" {
  alias = "dr"
  mock_data "aws_region" {
    defaults = { region = "eu-central-1" }
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
  mock_resource "aws_kms_key" {
    defaults = { arn = "arn:aws:kms:eu-central-1:123456789012:key/11111111-1111-1111-1111-111111111111" }
  }
  mock_resource "aws_s3_bucket" {
    defaults = { arn = "arn:aws:s3:::iki-test-files-dr-123456789012" }
  }
}

variables {
  name                 = "iki-test"
  region               = "eu-west-1"
  dr_region            = "eu-central-1"
  zones                = ["eu-west-1a", "eu-west-1b", "eu-west-1c"]
  kubernetes_version   = "1.35"
  admin_principal_arns = ["arn:aws:iam::123456789012:role/platform"]
  state_kms_key_arn    = "arn:aws:kms:eu-west-1:123456789012:key/state"
  public_host          = "iki.example.com"
  certificate_arn      = "arn:aws:acm:eu-west-1:123456789012:certificate/abc"
}

# Subnets' IDs are mocked, so pin them: the chart's values are then known exactly.
override_resource {
  target = module.network.aws_subnet.public
  values = { id = "subnet-0000000000public" }
}

override_resource {
  target = module.network.aws_subnet.private
  values = { id = "subnet-000000000private" }
}

override_resource {
  target = module.storage.aws_s3_bucket.files
  values = { id = "iki-test-files-123456789012", arn = "arn:aws:s3:::iki-test-files-123456789012" }
}

run "the_chart_gets_exactly_the_values_its_tests_use" {
  # deploy/helm/iki/tests renders the chart with this file and values-production.yaml.
  assert {
    condition     = yamldecode(output.helm_values) == yamldecode(file("tests/helm-values.yaml"))
    error_message = "helm_values changed: update tests/helm-values.yaml to match, and the chart's tests will check it."
  }
}

run "every_secret_reaches_the_cluster_under_the_name_the_chart_is_given" {
  assert {
    condition = toset([
      for d in split("---\n", output.external_secrets) : yamldecode(d).spec.target.name if yamldecode(d).kind == "ExternalSecret"
      ]) == toset([
      for v in [
        yamldecode(output.helm_values).secretKey, yamldecode(output.helm_values).encryptionKeys,
        yamldecode(output.helm_values).bundles.signingKey, yamldecode(output.helm_values).runSecrets,
        yamldecode(output.helm_values).database, yamldecode(output.helm_values).database.migrator,
      ] : v.existingSecret
    ])
    error_message = "Each Secret the chart is told of should be made by an ExternalSecret, and no others."
  }
}

run "the_alb_speaks_only_modern_tls" {
  assert {
    condition     = yamldecode(output.helm_values).ingress.annotations["alb.ingress.kubernetes.io/listen-ports"] == "[{\"HTTPS\":443}]"
    error_message = "The ALB should listen on HTTPS alone."
  }
  assert {
    condition     = startswith(yamldecode(output.helm_values).ingress.annotations["alb.ingress.kubernetes.io/ssl-policy"], "ELBSecurityPolicy-TLS13-1-2-")
    error_message = "And accept TLS 1.2 or later only."
  }
}

run "an_internal_alb_sits_in_the_private_subnets" {
  variables {
    internal = true
  }
  assert {
    condition     = yamldecode(output.helm_values).ingress.annotations["alb.ingress.kubernetes.io/scheme"] == "internal"
    error_message = "An internal ALB should be internal."
  }
  assert {
    condition     = [for r in yamldecode(output.helm_values).networkPolicy.ingressFrom : r.ipBlock.cidr] == module.network.private_subnet_cidrs
    error_message = "And web pods should admit it from the private subnets."
  }
}

run "the_vm_tier_when_asked" {
  variables {
    vm_tier = { ecr_repository_arns = ["arn:aws:ecr:eu-west-1:123456789012:repository/iki/worker"] }
  }
  assert {
    condition     = yamldecode(output.helm_values).vmTier == { enabled = true, owner = "iki-test", region = "eu-west-1" }
    error_message = "The chart should be told the VM tier's owner and region."
  }
  assert {
    condition     = module.access.provisioner_role_arn != null && output.vm_tier.instance_profile != null
    error_message = "The provisioner should get a role, and templates an instance profile."
  }
}

run "refuses_the_same_region_for_dr" {
  command = plan
  variables {
    dr_region = "eu-west-1"
  }
  expect_failures = [var.dr_region]
}

# The modules' own tests check the sizes and protections they're given; here, that the
# load balancer, which only the chart's values make, follows deletion_protection too.
run "the_load_balancer_refuses_deletion_by_default" {
  assert {
    condition     = strcontains(yamldecode(output.helm_values).ingress.annotations["alb.ingress.kubernetes.io/load-balancer-attributes"], "deletion_protection.enabled=true")
    error_message = "The ALB should refuse deletion."
  }
}

run "teardown_lets_the_load_balancer_go" {
  variables {
    database                     = { instance_class = "db.t4g.small", allocated_storage_gib = 20, max_allocated_storage_gib = 40, backup_retention_days = 7 }
    single_nat_gateway           = true
    deletion_protection          = false
    secrets_recovery_window_days = 7
  }
  assert {
    condition     = strcontains(yamldecode(output.helm_values).ingress.annotations["alb.ingress.kubernetes.io/load-balancer-attributes"], "deletion_protection.enabled=false")
    error_message = "With deletion_protection off, the ALB should let itself be deleted."
  }
}

run "an_internal_deployment_is_named_inside_its_vpc" {
  variables {
    internal = true
  }
  assert {
    condition     = length(aws_route53_zone.internal) == 1 && aws_route53_zone.internal[0].name == "iki.example.com" && one(aws_route53_zone.internal[0].vpc).vpc_id == module.network.vpc_id
    error_message = "A private zone for public_host alone, in this VPC only."
  }
  assert {
    condition     = output.controller_roles.external_dns != null && yamldecode(output.external_dns_values).domainFilters == ["iki.example.com"]
    error_message = "ExternalDNS gets a role, and values that keep it to that name."
  }
  assert {
    condition     = contains(yamldecode(output.external_dns_values).extraArgs, "--zone-id-filter=Z0123456789") && contains(yamldecode(output.external_dns_values).extraArgs, "--aws-zone-type=private")
    error_message = "And to that zone, which is private."
  }
  assert {
    condition     = [for r in yamldecode(output.helm_values).networkPolicy.egress.server.to : r.ipBlock.cidr] == module.network.private_subnet_cidrs
    error_message = "Workers should reach the internal ALB, in the private subnets."
  }
}

run "an_internal_deployment_named_by_your_own_dns" {
  variables {
    internal     = true
    internal_dns = false
  }
  assert {
    condition     = length(aws_route53_zone.internal) == 0 && output.external_dns_values == null && output.controller_roles.external_dns == null
    error_message = "Without internal_dns, no zone and no ExternalDNS."
  }
}

run "an_internet_facing_deployment_has_no_private_zone" {
  assert {
    condition     = length(aws_route53_zone.internal) == 0 && !contains(keys(yamldecode(output.helm_values).networkPolicy.egress), "server")
    error_message = "Its workers reach the public ALB as the chart allows by default."
  }
}
