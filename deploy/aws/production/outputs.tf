# Kubernetes Secrets External Secrets makes, from the secrets module's entries, and the
# chart's values that name them.
locals {
  k8s_secrets = {
    "secret-key"           = { name = "iki-secret-key", key = "secret-key" }
    "encryption-keys"      = { name = "iki-encryption-keys", key = "keys" }
    "bundle-signing-key"   = { name = "iki-bundle-signing-key", key = "key" }
    "database"             = { name = "iki-database", key = "uri" }
    "database-migrator"    = { name = "iki-database-migrator", key = "uri" }
    "email"                = { name = "iki-email", key = "url" }
    "backend-worker-token" = { name = "iki-backend-worker-token", key = "token" }
  }
  runs = module.cluster.node_pools["runs"]
}

output "helm_values" {
  description = "The chart's values this deployment decides, for beside values-production.yaml: tofu output -raw helm_values > values-aws.yaml."
  value = yamlencode({
    publicUrl      = "https://${var.public_host}"
    allowedHosts   = [var.public_host]
    secretKey      = { existingSecret = local.k8s_secrets["secret-key"].name }
    encryptionKeys = { existingSecret = local.k8s_secrets["encryption-keys"].name }
    bundles        = { signingKey = { existingSecret = local.k8s_secrets["bundle-signing-key"].name } }
    runSecrets     = { existingSecret = "iki-run-secrets" }
    database = {
      existingSecret = local.k8s_secrets["database"].name
      migrator       = { existingSecret = local.k8s_secrets["database-migrator"].name }
      caSecret       = { name = "iki-database-ca", key = "ca.crt" }
    }
    files = {
      bucket = module.storage.bucket
      prefix = var.files_prefix
      region = module.storage.region
      # Recovering: records name the lost bucket; its objects are in this one (ADR 0099).
      bucketAliases = local.lost == null ? {} : { (local.lost.files) = module.storage.bucket }
    }
    # An ALB, made by the AWS Load Balancer Controller: HTTPS only, TLS 1.2 or later,
    # sending straight to web pods' addresses, dropping malformed headers.
    ingress = {
      enabled   = true
      className = "alb"
      host      = var.public_host
      annotations = merge({
        "alb.ingress.kubernetes.io/scheme"           = var.internal ? "internal" : "internet-facing"
        "alb.ingress.kubernetes.io/subnets"          = join(",", var.internal ? module.network.private_subnet_ids : module.network.public_subnet_ids)
        "alb.ingress.kubernetes.io/target-type"      = "ip"
        "alb.ingress.kubernetes.io/listen-ports"     = jsonencode([{ HTTPS = 443 }])
        "alb.ingress.kubernetes.io/certificate-arn"  = var.certificate_arn
        "alb.ingress.kubernetes.io/ssl-policy"       = "ELBSecurityPolicy-TLS13-1-2-Res-2021-06"
        "alb.ingress.kubernetes.io/inbound-cidrs"    = join(",", var.ingress_cidrs)
        "alb.ingress.kubernetes.io/healthcheck-path" = "/healthz/ready"
        # Idle connections close here first: the chart's web.keepAliveSeconds is 75.
        "alb.ingress.kubernetes.io/load-balancer-attributes" = join(",", [
          "routing.http.drop_invalid_header_fields.enabled=true",
          "deletion_protection.enabled=${var.deletion_protection}",
          "idle_timeout.timeout_seconds=60",
        ])
      }, var.waf_acl_arn == null ? {} : { "alb.ingress.kubernetes.io/wafv2-acl-arn" = var.waf_acl_arn })
    }
    backendWorkers = {
      nodeSelector = local.runs.node_selector
      tolerations  = local.runs.tolerations
    }
    # merge, not a conditional: a conditional would make enabled the string "false".
    vmTier = merge({ enabled = local.vm_tier }, local.vm_tier ? { owner = var.name, region = var.region } : {})
    networkPolicy = {
      # The ALB sends to web pods' addresses from its own, in its subnets.
      ingressFrom = [for c in(var.internal ? module.network.private_subnet_cidrs : module.network.public_subnet_cidrs) : { ipBlock = { cidr = c } }]
      egress = merge({
        database = { to = [for c in module.network.database_subnet_cidrs : { ipBlock = { cidr = c } }] }
        # Pods with an AWS role reach the node's Pod Identity agent for its credentials.
        podIdentity = { to = [{ ipBlock = { cidr = "169.254.170.23/32" } }, { ipBlock = { cidr = "fd00:ec2::23/128" } }] }
        }, var.internal ? {
        # Backend workers reach the server at publicUrl: an internal ALB, whose addresses
        # are in the private subnets (and change), not the public ones the chart allows.
        server = { ports = [443], to = [for c in module.network.private_subnet_cidrs : { ipBlock = { cidr = c } }] }
      } : {})
    }
  })
}

output "external_secrets" {
  description = "The SecretStore and ExternalSecrets that copy this deployment's secrets into Iki's namespace: tofu output -raw external_secrets | kubectl apply -f -."
  value = join("---\n", concat(
    [yamlencode({
      apiVersion = "external-secrets.io/v1"
      kind       = "SecretStore"
      metadata   = { name = "aws-secrets-manager", namespace = "iki" }
      spec       = { provider = { aws = { service = "SecretsManager", region = var.region } } }
    })],
    [for k, s in local.k8s_secrets : yamlencode({
      apiVersion = "external-secrets.io/v1"
      kind       = "ExternalSecret"
      metadata   = { name = s.name, namespace = "iki" }
      spec = {
        refreshInterval = "1h"
        secretStoreRef  = { name = "aws-secrets-manager", kind = "SecretStore" }
        target          = { name = s.name, creationPolicy = "Owner", deletionPolicy = "Retain" }
        data            = [{ secretKey = s.key, remoteRef = { key = module.secrets.secret_names[k] } }]
      }
    }) if contains(keys(module.secrets.secret_names), k)],
    [yamlencode({
      apiVersion = "external-secrets.io/v1"
      kind       = "ExternalSecret"
      metadata   = { name = "iki-run-secrets", namespace = "iki" }
      spec = {
        refreshInterval = "1h"
        secretStoreRef  = { name = "aws-secrets-manager", kind = "SecretStore" }
        target          = { name = "iki-run-secrets", creationPolicy = "Owner", deletionPolicy = "Retain" }
        # One key per plugin's secret, from the JSON object put-secrets.sh starts empty.
        dataFrom = [{ extract = { key = module.secrets.secret_names["run-secrets"] } }]
      }
    })],
  ))
}

output "put_secrets" {
  description = "The command that fills the secrets, once."
  value       = "deploy/aws/scripts/put-secrets.sh ${var.name} ${module.database.address}"
}

output "database_address" {
  description = "The database's host name, which its roles' URIs name."
  value       = module.database.address
}

output "database_security_group_id" {
  description = "The database's security group, for an instance restored beside it (docs/disaster-recovery.md)."
  value       = module.database.security_group_id
}

output "database_master_secret_arn" {
  description = "RDS's master user's password, for creating Iki's two roles once (the runbook)."
  value       = module.database.master_secret_arn
}

output "cluster_name" {
  description = "For aws eks update-kubeconfig --name."
  value       = module.cluster.name
}

output "controller_roles" {
  description = "The roles Pod Identity gives the controllers you install (the runbook)."
  value = {
    external_secrets         = module.access.external_secrets_role_arn
    load_balancer_controller = module.access.load_balancer_controller_role_arn
    cluster_autoscaler       = module.access.cluster_autoscaler_role_arn
    external_dns             = module.access.external_dns_role_arn
  }
}

output "external_dns_values" {
  description = "ExternalDNS's chart values, with internal_dns: it names the internal ALB public_host in the private zone, and only there (the runbook). Null otherwise."
  value = length(aws_route53_zone.internal) == 0 ? null : yamlencode({
    provider      = { name = "aws" }
    env           = [{ name = "AWS_DEFAULT_REGION", value = var.region }]
    sources       = ["ingress"]
    domainFilters = [var.public_host]
    # The private zone alone, and only records ExternalDNS made itself.
    extraArgs = ["--aws-zone-type=private", "--zone-id-filter=${aws_route53_zone.internal[0].zone_id}"]
    policy    = "sync"
    registry  = "txt"
    # Its ownership records beside the name, inside the zone: public_host is the zone's apex.
    txtPrefix      = "extdns-%%{record_type}."
    txtOwnerId     = var.name
    serviceAccount = { name = "external-dns" }
  })
}

output "vm_tier" {
  description = "What compute templates for the VM tier name (aws.instance_profile, aws.security_groups, aws.kms_key, subnets), or null."
  value = local.vm_tier ? {
    instance_profile = module.vm_tier[0].instance_profile_name
    security_groups  = [module.vm_tier[0].security_group_id]
    kms_key          = module.vm_tier[0].disk_kms_key_arn
    subnets          = module.network.private_subnet_ids
  } : null
}

output "fsx" {
  description = "The shared file system, for runs' PersistentVolume and the VM tier's templates, or null."
  value = var.fsx == null ? null : {
    file_system_id = module.fsx[0].file_system_id
    dns_name       = module.fsx[0].dns_name
    mount_name     = module.fsx[0].mount_name
  }
}

output "vpc_id" {
  description = "For the AWS Load Balancer Controller's vpcId: its pods can't ask the node's metadata service (one hop)."
  value       = module.network.vpc_id
}

output "region" {
  description = "The region Iki runs in."
  value       = var.region
}
