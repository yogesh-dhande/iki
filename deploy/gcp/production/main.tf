# Iki's production deployment on Google Cloud (ADR 0107): every module, put together. A
# starting point to copy into your own repository and change, not a module to call. What
# the chart needs from it comes out as values: tofu output -raw helm_values > values-gcp.yaml.

locals {
  vm_tier = var.vm_tier != null
  # Recovering from a lost region: the lost deployment's promoted replica, and its files
  # bucket, which is in both regions. These are the modules' names for them.
  lost = var.recovery == null ? null : {
    instance = "${var.recovery.name}-dr"
    key      = "projects/${var.project_id}/locations/${var.region}/keyRings/${var.recovery.name}-dr/cryptoKeys/database-dr"
    bucket   = "${var.recovery.name}-files-${var.project_id}"
  }
  # The files bucket's two regions. Recovering, the lost deployment's bucket stays where
  # it is: changing its regions would replace it.
  files_regions = local.lost == null ? [var.region, var.dr_region] : [var.recovery.lost_region, var.region]
  # Cloud Storage makes no custom dual-region of a pair with a predefined one (Iowa and
  # South Carolina are NAM4's), and a predefined dual-region's key must be in it, not in
  # the multi-region around it. Such a pair is its predefined dual-region, and its key's
  # there; any other pair is a custom dual-region, with its key in the multi-region.
  predefined_dual_regions = {
    ASIA1 = ["ASIA-NORTHEAST1", "ASIA-NORTHEAST2"]
    EUR4  = ["EUROPE-NORTH1", "EUROPE-WEST4"]
    EUR5  = ["EUROPE-WEST1", "EUROPE-WEST2"]
    EUR7  = ["EUROPE-WEST2", "EUROPE-WEST3"]
    EUR8  = ["EUROPE-WEST3", "EUROPE-WEST6"]
    NAM4  = ["US-CENTRAL1", "US-EAST1"]
  }
  files_dual_region = one([
    for name, regions in local.predefined_dual_regions : name
    if sort(regions) == sort([for r in local.files_regions : upper(r)])
  ])
}

module "kms" {
  source           = "../modules/kms"
  name             = var.name
  project_id       = var.project_id
  region           = var.region
  dr_region        = var.dr_region
  files_location   = local.files_dual_region == null ? var.multi_region : lower(local.files_dual_region)
  protection_level = var.kms_protection_level
  labels           = var.labels
}

module "network" {
  source        = "../modules/network"
  name          = var.name
  project_id    = var.project_id
  region        = var.region
  cidr          = var.vpc_cidr
  pods_cidr     = var.pods_cidr
  services_cidr = var.services_cidr
  node_egress   = var.node_egress
  labels        = var.labels
}

module "logs" {
  source         = "../modules/logs"
  name           = var.name
  project_id     = var.project_id
  region         = var.region
  kms_key        = module.kms.keys["logs"]
  retention_days = var.log_retention_days
  # Data Access audit logs: project-wide, so a project shared with other workloads logs
  # (and pays for) their reads too.
  data_access_services = var.data_access_services
}

module "cluster" {
  source               = "../modules/cluster"
  name                 = var.name
  project_id           = var.project_id
  region               = var.region
  zones                = var.zones
  kubernetes_version   = var.kubernetes_version
  release_channel      = var.release_channel
  network_id           = module.network.network_id
  subnet_id            = module.network.subnet_id
  pods_range_name      = module.network.pods_range_name
  services_range_name  = module.network.services_range_name
  node_tag             = module.network.node_tag
  secrets_kms_key      = module.kms.keys["kubernetes-secrets"]
  nodes_kms_key        = module.kms.keys["node-disks"]
  admin_members        = var.admin_members
  public_access_cidrs  = var.public_access_cidrs
  private_access_cidrs = var.private_access_cidrs
  dns_endpoint         = var.dns_endpoint
  node_pools           = var.node_pools
  filestore_csi        = var.filestore != null
  deletion_protection  = var.deletion_protection
  labels               = var.labels
}

module "database" {
  source              = "../modules/database"
  name                = var.name
  project_id          = var.project_id
  region              = var.region
  network_id          = module.network.network_id
  allocated_ip_range  = module.network.managed_services_range_name
  kms_key             = module.kms.keys["database"]
  tier                = var.database_tier
  backup_location     = var.dr_region
  dr_region           = var.database_replica ? var.dr_region : null
  dr_kms_key          = module.kms.keys["database-dr"]
  iam_principals      = var.database_iam_principals
  deletion_protection = var.deletion_protection
  adopt               = local.lost == null ? null : { instance = local.lost.instance, kms_key = local.lost.key }
  labels              = var.labels
  # Cloud SQL's addresses come from the peering with Google's managed services.
  depends_on = [module.network]
}

module "storage" {
  source     = "../modules/storage"
  name       = var.name
  project_id = var.project_id
  location   = coalesce(local.files_dual_region, upper(var.multi_region == "europe" ? "eu" : var.multi_region))
  # A custom dual-region's regions; a predefined one's are its own.
  data_locations    = local.files_dual_region == null ? local.files_regions : []
  turbo_replication = var.turbo_replication
  kms_key           = module.kms.keys["files"]
  prefix            = var.files_prefix
  cors_origins      = ["https://${var.public_host}"]
  adopt             = local.lost == null ? null : { bucket = local.lost.bucket }
  labels            = var.labels
}

module "secrets" {
  source              = "../modules/secrets"
  name                = var.name
  project_id          = var.project_id
  region              = var.region
  dr_region           = var.dr_region
  kms_key             = module.kms.keys["secrets"]
  dr_kms_key          = module.kms.keys["secrets-dr"]
  optional_secrets    = var.optional_secrets
  deletion_protection = var.deletion_protection
  labels              = var.labels
}

module "registry" {
  source     = "../modules/artifact-registry"
  name       = var.name
  project_id = var.project_id
  location   = var.region
  kms_key    = module.kms.keys["registry"]
  readers = merge(
    { nodes = "serviceAccount:${module.cluster.node_service_account}" },
    local.vm_tier ? { machines = module.vm_tier[0].member } : {},
  )
  writers = var.image_writers
  labels  = var.labels
}

# The cluster runs only images from the repositories above, the ones the chart and this
# runbook use, and the configured image pins and additional patterns. Each refusal is
# written to the audit log.
locals {
  config_builds = [for path in var.config_image_files : yamldecode(file(path))]
  config_images = flatten([for build in local.config_builds : [
    for image in [build.image, build.backendWorkers.image] : "${image.repository}@${image.digest}"
  ]])

  runbook_images = [
    # The chart's: Iki's server (image.repository; the egress proxy runs it too) and the
    # worker the container tier copies into runs (containerTier.workerImage). A server or
    # worker built with your plugins can use the repositories above or the config pins.
    "ghcr.io/yogesh-dhande/iki-server*",
    "ghcr.io/yogesh-dhande/iki-worker*",
    # External Secrets' controller, webhook and certificate controller, as its chart
    # installs them (the README's step 3).
    "ghcr.io/external-secrets/external-secrets*",
    # create-database-roles.sh's psql pod.
    "mirror.gcr.io/library/postgres:17",
  ]
}

module "binary_authorization" {
  source       = "../modules/binary-authorization"
  name         = var.name
  project_id   = var.project_id
  repositories = values(module.registry.repositories)
  images       = distinct(concat(local.runbook_images, var.plugin_images, var.extra_images, local.config_images))
}

module "vm_tier" {
  source            = "../modules/vm-tier"
  count             = local.vm_tier ? 1 : 0
  name              = var.name
  project_id        = var.project_id
  region            = var.region
  network_id        = module.network.network_id
  subnet_id         = module.network.subnet_id
  vpc_cidr          = module.network.cidr
  network_tag       = module.network.vm_tag
  kms_key           = module.kms.keys["vm-disks"]
  nfs_cidrs         = var.filestore == null ? [] : [module.filestore[0].cidr]
  extra_https_cidrs = var.vm_tier.extra_https_cidrs
  labels            = var.labels
}

module "filestore" {
  source                   = "../modules/filestore"
  count                    = var.filestore == null ? 0 : 1
  name                     = var.name
  project_id               = var.project_id
  region                   = var.region
  network_id               = module.network.network_id
  kms_key                  = module.kms.keys["filestore"]
  tier                     = var.filestore.tier
  zone                     = var.zones[0]
  capacity_gib             = var.filestore.capacity_gib
  client_cidrs             = [module.network.nodes_cidr, var.pods_cidr]
  root_squash_except_cidrs = var.filestore.root_squash_except_cidrs
  labels                   = var.labels
  depends_on               = [module.network]
}

module "access" {
  source     = "../modules/access"
  name       = var.name
  project_id = var.project_id
  files = {
    bucket = module.storage.bucket
    prefix = var.files_prefix
  }
  register_locations = var.register_locations
  external_secrets = {
    secret_ids = module.secrets.secret_ids
  }
  vm_tier = local.vm_tier ? {
    owner                    = var.name
    machines_service_account = module.vm_tier[0].service_account
    subnet                   = module.network.subnet_name
    region                   = var.region
  } : null
}

# --- Recovering from a lost region --------------------------------------------------------

# The lost deployment's promoted replica and its files bucket become this deployment's,
# rather than new ones made: imported on the first apply, then managed as if made here.
import {
  for_each = local.lost == null ? {} : { database = local.lost.instance }
  to       = module.database.google_sql_database_instance.this
  id       = "projects/${var.project_id}/instances/${each.value}"
}

import {
  for_each = local.lost == null ? {} : { files = local.lost.bucket }
  to       = module.storage.google_storage_bucket.files
  id       = "${var.project_id}/${each.value}"
}
