# Kubernetes Secrets External Secrets makes, from the secrets module's entries, and the
# chart's values that name them.
locals {
  k8s_secrets = {
    "secret-key"           = { name = "iki-secret-key", data = { "secret-key" = null } }
    "encryption-keys"      = { name = "iki-encryption-keys", data = { keys = null } }
    "bundle-signing-key"   = { name = "iki-bundle-signing-key", data = { key = null } }
    "database"             = { name = "iki-database", data = { uri = null } }
    "database-migrator"    = { name = "iki-database-migrator", data = { uri = null } }
    "email"                = { name = "iki-email", data = { url = null } }
    "backend-worker-token" = { name = "iki-backend-worker-token", data = { token = null } }
    # The HMAC key is one JSON secret; each Kubernetes key is one of its fields.
    "files-hmac" = { name = "iki-files-hmac", data = { "access-key-id" = "access_id", "secret-access-key" = "secret" } }
  }
  runs  = module.cluster.node_pools["runs"]
  store = { name = "gcp-secret-manager", kind = "SecretStore" }
  # The Gateway, and what it sends to.
  web     = { group = "", kind = "Service", name = "iki-web" }
  gateway = { group = "gateway.networking.k8s.io", kind = "Gateway", name = "iki" }
}

output "helm_values" {
  description = "The chart's values this deployment decides, for beside values-production.yaml: tofu output -raw helm_values > values-gcp.yaml."
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
    # Cloud Storage through its S3-compatible API, with the server's HMAC key, until Iki
    # speaks Cloud Storage's own (the README's next steps).
    files = {
      bucket      = module.storage.bucket
      prefix      = var.files_prefix
      region      = "auto"
      endpoint    = "https://storage.googleapis.com"
      credentials = { existingSecret = local.k8s_secrets["files-hmac"].name }
    }
    # Web, tasks and maintenance pods' Google identity, through Workload Identity.
    serviceAccount = { annotations = { "iam.gke.io/gcp-service-account" = module.access.server_service_account } }
    # Google's load balancers keep idle connections to pods for 600 seconds: pods must
    # keep them longer, or answer 502 now and then.
    web = { keepAliveSeconds = 620 }
    # Served through the Gateway in the gateway output, not an Ingress.
    ingress = { enabled = false }
    backendWorkers = {
      nodeSelector = local.runs.node_selector
      tolerations  = local.runs.tolerations
    }
    # No Compute Engine provisioner yet: the VM tier's infrastructure is ready for one.
    vmTier = { enabled = false }
    networkPolicy = {
      # The load balancer's proxies and health checkers reach web pods' addresses.
      ingressFrom = [for c in(var.internal ? concat([module.network.proxy_cidr], local.google_front_ends) : local.google_front_ends) : { ipBlock = { cidr = c } }]
      egress = {
        database = { to = [{ ipBlock = { cidr = module.network.managed_services_cidr } }] }
        # Cloud Storage, at private.googleapis.com, where the VPC's DNS sends it.
        objectStorage = { to = [{ ipBlock = { cidr = module.network.google_apis_cidr } }] }
        # Pods with a Google identity get its tokens from GKE's metadata server, which
        # Dataplane V2 also serves on 988.
        podIdentity = {
          ports = [80, 988]
          to    = [{ ipBlock = { cidr = "169.254.169.254/32" } }, { ipBlock = { cidr = "169.254.169.252/32" } }]
        }
      }
    }
  })
}

output "gateway" {
  description = "The Gateway that serves Iki, and its policies: tofu output -raw gateway | kubectl apply -f -."
  value = join("---\n", [
    yamlencode({
      apiVersion = "gateway.networking.k8s.io/v1"
      kind       = "Gateway"
      metadata   = { name = "iki", namespace = "iki" }
      spec = {
        gatewayClassName = var.internal ? "gke-l7-rilb" : "gke-l7-global-external-managed"
        addresses        = [{ type = "NamedAddress", value = "${var.name}-web" }]
        listeners = [{
          name     = "https"
          protocol = "HTTPS"
          port     = 443
          tls = {
            mode    = "Terminate"
            options = { "networking.gke.io/pre-shared-certs" = var.internal ? var.internal_certificate : google_compute_managed_ssl_certificate.web[0].name }
          }
          allowedRoutes = { namespaces = { from = "Same" } }
        }]
      }
    }),
    yamlencode({
      apiVersion = "gateway.networking.k8s.io/v1"
      kind       = "HTTPRoute"
      metadata   = { name = "iki-web", namespace = "iki" }
      spec = {
        parentRefs = [{ name = "iki" }]
        hostnames  = [var.public_host]
        rules      = [{ backendRefs = [{ name = "iki-web", port = 8000 }] }]
      }
    }),
    yamlencode({
      apiVersion = "networking.gke.io/v1"
      kind       = "GCPGatewayPolicy"
      metadata   = { name = "iki", namespace = "iki" }
      spec = {
        targetRef = local.gateway
        default   = { sslPolicy = "${var.name}-web" }
      }
    }),
    yamlencode({
      apiVersion = "networking.gke.io/v1"
      kind       = "GCPBackendPolicy"
      metadata   = { name = "iki-web", namespace = "iki" }
      spec = {
        targetRef = local.web
        default = merge(
          # Uploads' parts stream through the bucket, not here; a minute covers any page.
          { timeoutSec = 60, logging = { enabled = true, sampleRate = 1000000 } },
          local.security_policy == null ? {} : { securityPolicy = local.security_policy },
        )
      }
    }),
    yamlencode({
      apiVersion = "networking.gke.io/v1"
      kind       = "HealthCheckPolicy"
      metadata   = { name = "iki-web", namespace = "iki" }
      spec = {
        targetRef = local.web
        default = {
          config = { type = "HTTP", httpHealthCheck = { port = 8000, requestPath = "/healthz/ready" } }
        }
      }
    }),
  ])
}

output "external_secrets" {
  description = "The SecretStore and ExternalSecrets that copy this deployment's secrets into Iki's namespace: tofu output -raw external_secrets | kubectl apply -f -."
  value = join("---\n", concat(
    [yamlencode({
      apiVersion = "external-secrets.io/v1"
      kind       = "SecretStore"
      metadata   = { name = local.store.name, namespace = "iki" }
      # External Secrets' controller authenticates as its own Kubernetes service account,
      # which the access module lets read these secrets.
      spec = { provider = { gcpsm = { projectID = var.project_id } } }
    })],
    [for k, s in local.k8s_secrets : yamlencode({
      apiVersion = "external-secrets.io/v1"
      kind       = "ExternalSecret"
      metadata   = { name = s.name, namespace = "iki" }
      spec = {
        refreshInterval = "1h"
        secretStoreRef  = local.store
        target          = { name = s.name, creationPolicy = "Owner", deletionPolicy = "Retain" }
        data = [for key, property in s.data : {
          secretKey = key
          remoteRef = merge({ key = module.secrets.secret_names[k] }, property == null ? {} : { property = property })
        }]
      }
    }) if contains(keys(module.secrets.secret_names), k)],
    [yamlencode({
      apiVersion = "external-secrets.io/v1"
      kind       = "ExternalSecret"
      metadata   = { name = "iki-run-secrets", namespace = "iki" }
      spec = {
        refreshInterval = "1h"
        secretStoreRef  = local.store
        target          = { name = "iki-run-secrets", creationPolicy = "Owner", deletionPolicy = "Retain" }
        # One key per plugin's secret, from the JSON object put-secrets.sh starts empty.
        dataFrom = [{ extract = { key = module.secrets.secret_names["run-secrets"] } }]
      }
    })],
  ))
}

output "put_secrets" {
  description = "The command that fills the secrets, once."
  value       = "deploy/gcp/scripts/put-secrets.sh ${var.project_id} ${var.name} ${module.database.instance} ${module.database.address} ${module.access.server_service_account}"
}

output "create_database_roles" {
  description = "The command that creates Iki's two database roles, once, after put_secrets."
  value       = "deploy/gcp/scripts/create-database-roles.sh ${var.project_id} ${var.name} ${module.database.instance}"
}

output "database_address" {
  description = "The database's host name, which its roles' URIs name and its certificate names."
  value       = module.database.address
}

output "database_instance" {
  description = "The Cloud SQL instance, for gcloud sql."
  value       = module.database.instance
}

output "database_replica" {
  description = "The replica in the DR region: what to promote after losing this region (the README)."
  value       = module.database.dr_instance
}

output "cluster_name" {
  description = "For gcloud container clusters get-credentials."
  value       = module.cluster.name
}

output "get_credentials" {
  description = "The command that points kubectl at the cluster."
  value       = "gcloud container clusters get-credentials ${module.cluster.name} --location ${module.cluster.location} --project ${var.project_id}${var.dns_endpoint ? " --dns-endpoint" : (length(var.public_access_cidrs) > 0 ? "" : " --internal-ip")}"
}

output "load_balancer_address" {
  description = "Where to point public_host's DNS record."
  value       = var.internal ? google_compute_address.web[0].address : google_compute_global_address.web[0].address
}

output "images" {
  description = "The repositories to push Iki's worker image and your plugins' images to."
  value       = module.registry.repositories
}

output "nat_ips" {
  description = "The addresses Iki's traffic leaves by, for allowlists elsewhere (an identity provider's, a LIMS's)."
  value       = module.network.nat_ips
}

output "vm_tier" {
  description = "What a Compute Engine provisioner would start machines with (the README's next steps), or null."
  value = local.vm_tier ? {
    instance_template = module.vm_tier[0].instance_template
    service_account   = module.vm_tier[0].service_account
    provisioner       = module.access.provisioner_service_account
    subnet            = module.network.subnet_id
    network_tag       = module.vm_tier[0].network_tag
    disk_kms_key      = module.vm_tier[0].disk_kms_key
  } : null
}

output "filestore" {
  description = "The shared file system, for runs' PersistentVolume, or null."
  value = var.filestore == null ? null : {
    address = module.filestore[0].address
    share   = module.filestore[0].share
  }
}

output "project_id" {
  description = "The project Iki runs in."
  value       = var.project_id
}

output "region" {
  description = "The region Iki runs in."
  value       = var.region
}
