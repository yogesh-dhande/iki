# Runs against a mocked Google provider: no project, no credentials. Each run checks a
# property ADR 0107 promises.

mock_provider "google" {
  mock_resource "google_sql_database_instance" {
    defaults = {
      dns_name           = "0123456789ab.europe-west1.sql.goog."
      private_ip_address = "10.40.240.5"
      connection_name    = "iki-test-project:europe-west1:iki-test"
    }
  }
}

variables {
  name               = "iki-test"
  project_id         = "iki-test-project"
  region             = "europe-west1"
  network_id         = "projects/iki-test-project/global/networks/iki-test"
  allocated_ip_range = "iki-test-services"
  kms_key            = "projects/iki-test-project/locations/europe-west1/keyRings/iki-test/cryptoKeys/database"
  dr_region          = "europe-west4"
  dr_kms_key         = "projects/iki-test-project/locations/europe-west4/keyRings/iki-test-dr/cryptoKeys/database-dr"
  backup_location    = "europe-west4"
}

run "it_survives_losing_a_zone" {
  assert {
    condition     = one(google_sql_database_instance.this.settings).availability_type == "REGIONAL"
    error_message = "The database should be highly available across two zones."
  }
}

run "it_is_encrypted_with_a_key_of_its_own" {
  assert {
    condition     = google_sql_database_instance.this.encryption_key_name == var.kms_key && google_sql_database_instance.dr[0].encryption_key_name == var.dr_kms_key
    error_message = "The database and its replica should be encrypted with their own keys."
  }
}

run "nobody_outside_reaches_it_and_every_connection_is_tls" {
  assert {
    condition     = !one(one(google_sql_database_instance.this.settings).ip_configuration).ipv4_enabled && one(one(google_sql_database_instance.this.settings).ip_configuration).private_network == var.network_id
    error_message = "The database should have a private address only."
  }
  assert {
    condition     = one(one(google_sql_database_instance.this.settings).ip_configuration).ssl_mode == "ENCRYPTED_ONLY"
    error_message = "Every connection should use TLS."
  }
  assert {
    condition     = one(one(google_sql_database_instance.this.settings).ip_configuration).server_ca_mode == "GOOGLE_MANAGED_CAS_CA"
    error_message = "Its certificate should name its host, for verify-full."
  }
  assert {
    condition     = output.address == "0123456789ab.europe-west1.sql.goog" && google_dns_record_set.this.rrdatas == tolist(["10.40.240.5"])
    error_message = "Its DNS name should resolve to its private address inside the VPC."
  }
}

run "connections_and_ddl_are_logged_through_pgaudit" {
  assert {
    condition = alltrue(flatten([for i in [google_sql_database_instance.this, google_sql_database_instance.dr[0]] : [
      for f in [
        "log_connections=on", "log_disconnections=on", "log_checkpoints=on", "log_lock_waits=on",
        "log_min_error_statement=error", "log_statement=ddl", "cloudsql.enable_pgaudit=on",
        "pgaudit.log=ddl,role", "cloudsql.iam_authentication=on",
      ] : contains([for d in one(i.settings).database_flags : "${d.name}=${d.value}"], f)
    ]]))
    error_message = "Connections, locks, errors, DDL and role changes should be logged, on the database and its replica, and IAM sign-in on."
  }
}

run "no_password_enters_the_state" {
  assert {
    condition     = google_sql_database_instance.this.root_password == null && length(google_sql_user.iam) == 0
    error_message = "OpenTofu should set no password: put-secrets.sh sets postgres's, straight in Cloud SQL."
  }
}

run "it_recovers_to_any_minute_of_the_last_35_days" {
  assert {
    condition = (
      one(one(google_sql_database_instance.this.settings).backup_configuration).point_in_time_recovery_enabled
      && one(one(google_sql_database_instance.this.settings).backup_configuration).transaction_log_retention_days == 35
      && one(one(google_sql_database_instance.this.settings).backup_configuration).location == "europe-west4"
    )
    error_message = "Point-in-time recovery should reach back 35 days, with backups stored in the DR region."
  }
  assert {
    condition     = google_sql_database_instance.this.deletion_protection && one(google_sql_database_instance.this.settings).deletion_protection_enabled && one(google_sql_database_instance.this.settings).retain_backups_on_delete
    error_message = "It shouldn't be deletable by accident, and deleting it should keep its backups."
  }
}

run "deletable_only_when_told_to_tear_down" {
  variables {
    deletion_protection = false
  }
  assert {
    condition     = !google_sql_database_instance.this.deletion_protection && !one(google_sql_database_instance.this.settings).deletion_protection_enabled && !google_sql_database_instance.dr[0].deletion_protection && !one(google_sql_database_instance.dr[0].settings).deletion_protection_enabled
    error_message = "With deletion_protection off, OpenTofu and Cloud SQL should both let the database and its replica go."
  }
  assert {
    condition     = one(google_sql_database_instance.this.settings).retain_backups_on_delete
    error_message = "Deleting it should still keep its backups."
  }
}

run "a_replica_follows_it_in_the_dr_region" {
  assert {
    condition     = google_sql_database_instance.dr[0].master_instance_name == google_sql_database_instance.this.name && google_sql_database_instance.dr[0].region == "europe-west4"
    error_message = "A replica in the DR region should follow the database."
  }
}

run "iam_sign_in_for_the_people_named" {
  variables {
    iam_principals = [{ email = "dbas@example.com" }]
  }
  assert {
    condition     = google_sql_user.iam["dbas@example.com"].type == "CLOUD_IAM_GROUP" && google_project_iam_member.iam["dbas@example.com"].member == "group:dbas@example.com"
    error_message = "A group should sign in with IAM, and be allowed to connect."
  }
}

run "refuses_enterprise_with_35_days" {
  command = plan
  variables {
    edition = "ENTERPRISE"
  }
  expect_failures = [var.backup_retention_days]
}

run "refuses_a_replica_without_its_key" {
  command = plan
  variables {
    dr_kms_key = null
  }
  expect_failures = [var.dr_kms_key]
}

run "a_password_policy_cloud_sql_accepts" {
  assert {
    condition     = one(one(google_sql_database_instance.this.settings).password_validation_policy).min_length <= 30 && one(one(google_sql_database_instance.this.settings).password_validation_policy).complexity == "COMPLEXITY_DEFAULT"
    error_message = "Cloud SQL refuses a minimum length above 30; keep the complexity rule put-secrets.sh's passwords meet."
  }
}

run "a_backup_older_than_the_oldest_minute_it_recovers_to" {
  assert {
    condition     = one(one(one(google_sql_database_instance.this.settings).backup_configuration).backup_retention_settings).retained_backups > one(one(google_sql_database_instance.this.settings).backup_configuration).transaction_log_retention_days
    error_message = "Cloud SQL requires more daily backups kept than days of transaction logs."
  }
}
