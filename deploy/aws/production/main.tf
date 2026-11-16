# Iki's production deployment on AWS (ADR 0097): every module, put together. A starting
# point to copy into your own repository and change, not a module to call. What the chart
# needs from it comes out as values: tofu output -raw helm_values > values-aws.yaml.

locals {
  vm_tier = var.vm_tier != null
}

module "network" {
  source = "../modules/network"
  name   = var.name
  cidr   = var.vpc_cidr
  zones  = var.zones
  tags   = var.tags
}

module "cluster" {
  source               = "../modules/cluster"
  name                 = var.name
  kubernetes_version   = var.kubernetes_version
  vpc_id               = module.network.vpc_id
  subnet_ids           = module.network.private_subnet_ids
  public_access_cidrs  = var.public_access_cidrs
  admin_principal_arns = var.admin_principal_arns
  node_pools           = var.node_pools
  tags                 = var.tags
}

module "database" {
  source                    = "../modules/database"
  providers                 = { aws = aws, aws.dr = aws.dr }
  name                      = var.name
  vpc_id                    = module.network.vpc_id
  subnet_ids                = module.network.database_subnet_ids
  client_security_group_ids = [module.cluster.security_group_id]
  tags                      = var.tags
}

module "storage" {
  source    = "../modules/storage"
  providers = { aws = aws, aws.dr = aws.dr }
  name      = var.name
  tags      = var.tags
}

module "secrets" {
  source           = "../modules/secrets"
  providers        = { aws = aws, aws.dr = aws.dr }
  name             = var.name
  optional_secrets = var.optional_secrets
  tags             = var.tags
}

module "vm_tier" {
  source              = "../modules/vm-tier"
  count               = local.vm_tier ? 1 : 0
  name                = var.name
  vpc_id              = module.network.vpc_id
  vpc_cidr            = module.network.cidr
  s3_prefix_list_id   = module.network.s3_prefix_list_id
  ecr_repository_arns = var.vm_tier.ecr_repository_arns
  extra_https_cidrs   = var.vm_tier.extra_https_cidrs
  file_systems        = var.fsx == null ? [] : [{ kind = "lustre", security_group_id = module.fsx[0].security_group_id }]
  tags                = var.tags
}

module "fsx" {
  source                    = "../modules/fsx"
  count                     = var.fsx == null ? 0 : 1
  name                      = var.name
  vpc_id                    = module.network.vpc_id
  subnet_id                 = module.network.private_subnet_ids[0]
  client_security_group_ids = [module.cluster.security_group_id]
  storage_capacity_gib      = var.fsx.storage_capacity_gib
  throughput_per_tib        = var.fsx.throughput_per_tib
  reference_bucket          = var.fsx.reference_bucket
  root_squash               = { except_nids = var.fsx.root_squash_except_nids }
  tags                      = var.tags
}

module "access" {
  source       = "../modules/access"
  name         = var.name
  cluster_name = module.cluster.name
  cluster_arn  = module.cluster.arn
  files = {
    bucket_arn  = module.storage.bucket_arn
    kms_key_arn = module.storage.kms_key_arn
    prefix      = var.files_prefix
  }
  register_locations = var.register_locations
  external_secrets = {
    secret_arns = values(module.secrets.secret_arns)
    kms_key_arn = module.secrets.kms_key_arn
  }
  vm_tier = local.vm_tier ? {
    owner            = var.name
    worker_role_arn  = module.vm_tier[0].worker_role_arn
    disk_kms_key_arn = module.vm_tier[0].disk_kms_key_arn
    regions          = [var.region]
  } : null
  tags = var.tags
}
