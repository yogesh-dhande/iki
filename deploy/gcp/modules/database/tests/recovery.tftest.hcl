# Recovering from a lost region (docs/disaster-recovery.md), in a file of its own: runs in
# a file share their state, and a database made new can't later become an adopted one.

mock_provider "google" {
  mock_resource "google_sql_database_instance" {
    defaults = {
      dns_name = ""
      dns_names = [
        { connection_type = "PRIVATE_SERVICES_ACCESS", dns_scope = "INSTANCE", name = "ba9876543210.3e753kupt8481.europe-west4.sql-psa.goog." },
        { connection_type = "PRIVATE_SERVICES_ACCESS", dns_scope = "CLUSTER", name = "2n3j020whr5io.3e753kupt8481.global.sql-psa.goog." },
      ]
      private_ip_address = "10.41.240.5"
    }
  }
}

variables {
  name               = "iki-test-r1"
  project_id         = "iki-test-project"
  region             = "europe-west4"
  network_id         = "projects/iki-test-project/global/networks/iki-test-r1"
  allocated_ip_range = "iki-test-r1-services"
  kms_key            = "projects/iki-test-project/locations/europe-west4/keyRings/iki-test-r1/cryptoKeys/database"
}

run "recovering_it_takes_on_the_lost_databases_promoted_replica" {
  variables {
    adopt = {
      instance = "iki-test-dr"
      kms_key  = "projects/iki-test-project/locations/europe-west4/keyRings/iki-test-dr/cryptoKeys/database-dr"
    }
  }
  assert {
    condition     = google_sql_database_instance.this.name == "iki-test-dr" && google_sql_database_instance.this.encryption_key_name == "projects/iki-test-project/locations/europe-west4/keyRings/iki-test-dr/cryptoKeys/database-dr"
    error_message = "The database should be the promoted replica, under its own name and key, so importing it changes neither."
  }
  assert {
    condition     = length(google_sql_database.iki) == 0 && output.adopted == "iki-test-dr"
    error_message = "Its database came with it: none should be made."
  }
  assert {
    condition     = one(google_sql_database_instance.this.settings).availability_type == "REGIONAL" && one(one(google_sql_database_instance.this.settings).backup_configuration).point_in_time_recovery_enabled && google_sql_database_instance.this.deletion_protection
    error_message = "A promoted replica should become as protected as a new database: highly available, backed up, deletion protected."
  }
  assert {
    condition     = length(google_sql_database_instance.dr) == 0
    error_message = "With the lost region down, no replica is made there."
  }
}
