# Cloud SQL for PostgreSQL (ADR 0107): highly available across two zones, private IP only,
# TLS only with a certificate that names its host, encrypted with a key of its own,
# logging connections and DDL through pgaudit, 35 days of point-in-time recovery, and a
# replica in the DR region seconds behind it, to promote if the region is lost.

locals {
  labels  = merge(var.labels, { iki-deployment = var.name })
  adopted = var.adopt != null
  # Recovering, the promoted replica is this database: its name and key come with it.
  instance = local.adopted ? var.adopt.instance : var.name
  key      = local.adopted ? var.adopt.kms_key : var.kms_key
  flags = {
    # People sign in with IAM's short-lived tokens, not passwords (iam_principals).
    "cloudsql.iam_authentication" = "on"
    # Who connected, from where, and for how long.
    "log_connections"    = "on"
    "log_disconnections" = "on"
    "log_statement"      = "ddl"
    # Checkpoints, waits for locks, and the statement behind each error.
    "log_checkpoints"         = "on"
    "log_lock_waits"          = "on"
    "log_min_error_statement" = "error"
    # Every change to the schema or to roles, through pgaudit: the database's own audit
    # trail, beside Iki's (ADR 0025).
    "cloudsql.enable_pgaudit" = "on"
    "pgaudit.log"             = "ddl,role"
    "pgaudit.log_parameter"   = "off"
  }
}

resource "google_sql_database_instance" "this" {
  #checkov:skip=CKV_GCP_51:Set in local.flags, a dynamic block Checkov doesn't read; tests/database.tftest.hcl checks the flags.
  #checkov:skip=CKV_GCP_52:As above.
  #checkov:skip=CKV_GCP_53:As above.
  #checkov:skip=CKV_GCP_54:As above.
  #checkov:skip=CKV_GCP_109:As above.
  #checkov:skip=CKV_GCP_110:As above.
  #checkov:skip=CKV_GCP_111:As above.
  #checkov:skip=CKV2_GCP_13:log_duration logs every statement's duration, a line per query; pgaudit and log_statement = ddl log what changes, and Query Insights times queries.
  #checkov:skip=CKV_GCP_108:log_hostname makes a reverse DNS lookup for every connection; connections are logged with their address.
  #checkov:skip=CKV_GCP_6:ENCRYPTED_ONLY refuses any connection without TLS. Iki verifies the server's certificate (verify-full) and signs in with a password or IAM; client certificates aren't used.
  #checkov:skip=CKV_GCP_79:PostgreSQL 17 by default, as deploy/aws and Iki's tests use; engine_version takes 18.
  project             = var.project_id
  name                = local.instance
  region              = var.region
  database_version    = "POSTGRES_${var.engine_version}"
  encryption_key_name = local.key
  deletion_protection = var.deletion_protection

  settings {
    edition           = var.edition
    tier              = var.tier
    availability_type = "REGIONAL"
    disk_type         = "PD_SSD"
    disk_size         = var.disk_size_gib
    disk_autoresize   = true
    # How far Cloud SQL may grow the disk by itself as it fills.
    disk_autoresize_limit       = var.max_disk_size_gib
    deletion_protection_enabled = var.deletion_protection
    # Kept when the instance is deleted, and a last backup taken first.
    retain_backups_on_delete = true
    final_backup_config {
      enabled        = true
      retention_days = 30
    }
    user_labels = local.labels

    ip_configuration {
      ipv4_enabled       = false
      private_network    = var.network_id
      allocated_ip_range = var.allocated_ip_range
      # TLS for every connection, from a CA Google runs for this instance, whose
      # certificates name its DNS name: Iki connects with sslmode=verify-full.
      ssl_mode                         = "ENCRYPTED_ONLY"
      server_ca_mode                   = "GOOGLE_MANAGED_CAS_CA"
      server_certificate_rotation_mode = "AUTOMATIC_ROTATION_DURING_MAINTENANCE"
    }

    backup_configuration {
      enabled                        = true
      start_time                     = var.backup_start_time
      point_in_time_recovery_enabled = true
      transaction_log_retention_days = var.backup_retention_days
      # Daily backups are stored in the DR region, so they outlive this one.
      location = var.backup_location
      # One more daily backup than days of logs, as Cloud SQL requires: recovering to the
      # oldest minute starts from the backup before it.
      backup_retention_settings {
        retained_backups = var.backup_retention_days + 1
        retention_unit   = "COUNT"
      }
    }

    dynamic "database_flags" {
      for_each = local.flags
      content {
        name  = database_flags.key
        value = database_flags.value
      }
    }

    insights_config {
      query_insights_enabled  = true
      query_string_length     = 1024
      record_application_tags = true
      record_client_address   = true
    }

    # Cloud SQL allows a minimum length of at most 30. COMPLEXITY_DEFAULT asks for upper
    # and lower case, a digit and a symbol: put-secrets.sh's passwords have each.
    password_validation_policy {
      enable_password_policy      = true
      min_length                  = 30
      complexity                  = "COMPLEXITY_DEFAULT"
      disallow_username_substring = true
    }

    maintenance_window {
      day          = var.maintenance_window.day
      hour         = var.maintenance_window.hour
      update_track = "stable"
    }
  }

  # Recovering, a promoted replica arrives with no backups and in one zone: the first
  # apply gives it both. Cloud SQL grows the disk itself; OpenTofu mustn't shrink it.
  lifecycle {
    ignore_changes = [settings[0].disk_size]
  }
}

resource "google_sql_database" "iki" {
  # A promoted replica already holds Iki's database.
  count    = local.adopted ? 0 : 1
  project  = var.project_id
  instance = google_sql_database_instance.this.name
  name     = "iki"
  # Deleting the resource never drops the database: the instance's deletion does that.
  deletion_policy = "ABANDON"
}

# People who sign in with IAM (Cloud SQL's IAM database authentication): they get no
# privileges in Postgres until granted some there, and need roles/cloudsql.instanceUser.
resource "google_sql_user" "iam" {
  for_each = { for p in var.iam_principals : p.email => p }
  project  = var.project_id
  instance = google_sql_database_instance.this.name
  name     = each.value.type == "service_account" ? trimsuffix(each.key, ".gserviceaccount.com") : each.key
  type     = { user = "CLOUD_IAM_USER", group = "CLOUD_IAM_GROUP", service_account = "CLOUD_IAM_SERVICE_ACCOUNT" }[each.value.type]
}

resource "google_project_iam_member" "iam" {
  for_each = { for p in var.iam_principals : p.email => p }
  project  = var.project_id
  role     = "roles/cloudsql.instanceUser"
  member   = "${each.value.type == "service_account" ? "serviceAccount" : each.value.type}:${each.key}"
}

# --- Its name inside the VPC ----------------------------------------------------------------

# The certificate names the instance's DNS name, which a private zone resolves to its
# private address: verify-full then checks the host Iki dialled is the one it reached.
# Reached through Private Services Access, the instance's own name is in sql-psa.goog,
# and its record is the customer's to make (dns_names; dns_name is PSC's and public IP's,
# and empty here). An empty name would make this a zone for "." in the VPC, answering
# for every name on the internet: the precondition refuses it.
locals {
  dns_name = trimsuffix(try(coalesce(
    one([
      for d in google_sql_database_instance.this.dns_names : d.name
      if d.connection_type == "PRIVATE_SERVICES_ACCESS" && d.dns_scope == "INSTANCE"
    ]),
    google_sql_database_instance.this.dns_name,
  ), ""), ".")
}

resource "google_dns_managed_zone" "this" {
  project     = var.project_id
  name        = "${var.name}-database"
  dns_name    = "${local.dns_name}."
  description = "${var.name}: the database's name, as its certificate gives it"
  visibility  = "private"
  labels      = local.labels

  private_visibility_config {
    networks {
      network_url = var.network_id
    }
  }

  lifecycle {
    precondition {
      condition     = length(split(".", local.dns_name)) >= 3
      error_message = "Cloud SQL gave the instance no DNS name for Private Services Access: its certificate names none to verify."
    }
  }
}

resource "google_dns_record_set" "this" {
  project      = var.project_id
  managed_zone = google_dns_managed_zone.this.name
  name         = google_dns_managed_zone.this.dns_name
  type         = "A"
  ttl          = 60
  rrdatas      = [google_sql_database_instance.this.private_ip_address]
}

# --- The DR region ---------------------------------------------------------------------------

# Cross-region replication, seconds behind: if the region is lost, the replica is
# promoted and becomes the recovered deployment's database (docs/disaster-recovery.md).
resource "google_sql_database_instance" "dr" {
  #checkov:skip=CKV_GCP_51:Set in local.flags, a dynamic block Checkov doesn't read; tests/database.tftest.hcl checks the flags.
  #checkov:skip=CKV_GCP_52:As above.
  #checkov:skip=CKV_GCP_53:As above.
  #checkov:skip=CKV_GCP_54:As above.
  #checkov:skip=CKV_GCP_109:As above.
  #checkov:skip=CKV_GCP_110:As above.
  #checkov:skip=CKV_GCP_111:As above.
  #checkov:skip=CKV2_GCP_13:log_duration logs every statement's duration, a line per query; pgaudit and log_statement = ddl log what changes, and Query Insights times queries.
  #checkov:skip=CKV_GCP_108:log_hostname makes a reverse DNS lookup for every connection; connections are logged with their address.
  #checkov:skip=CKV_GCP_6:ENCRYPTED_ONLY refuses any connection without TLS. Iki verifies the server's certificate (verify-full) and signs in with a password or IAM; client certificates aren't used.
  #checkov:skip=CKV_GCP_79:PostgreSQL 17 by default, as deploy/aws and Iki's tests use; engine_version takes 18.
  count                = var.dr_region == null ? 0 : 1
  project              = var.project_id
  name                 = "${var.name}-dr"
  region               = var.dr_region
  database_version     = google_sql_database_instance.this.database_version
  master_instance_name = google_sql_database_instance.this.name
  encryption_key_name  = var.dr_kms_key
  deletion_protection  = var.deletion_protection

  replica_configuration {
    failover_target = false
  }

  settings {
    edition                     = var.edition
    tier                        = var.tier
    availability_type           = "ZONAL"
    disk_type                   = "PD_SSD"
    disk_autoresize             = true
    disk_autoresize_limit       = var.max_disk_size_gib
    deletion_protection_enabled = var.deletion_protection
    user_labels                 = local.labels

    ip_configuration {
      ipv4_enabled                     = false
      private_network                  = var.network_id
      allocated_ip_range               = var.allocated_ip_range
      ssl_mode                         = "ENCRYPTED_ONLY"
      server_ca_mode                   = "GOOGLE_MANAGED_CAS_CA"
      server_certificate_rotation_mode = "AUTOMATIC_ROTATION_DURING_MAINTENANCE"
    }

    dynamic "database_flags" {
      for_each = local.flags
      content {
        name  = database_flags.key
        value = database_flags.value
      }
    }
  }

  lifecycle {
    ignore_changes = [settings[0].disk_size]
  }
}
