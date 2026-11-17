# Runs against a mocked Google provider: no project, no credentials. Each run checks a
# property ADR 0077 and 0107 promise.

mock_provider "google" {
  mock_resource "google_filestore_instance" {
    defaults = {
      networks = { ip_addresses = ["10.40.240.10"] }
    }
  }
}

variables {
  name         = "iki-test"
  project_id   = "iki-test-project"
  region       = "europe-west1"
  network_id   = "projects/iki-test-project/global/networks/iki-test"
  kms_key      = "projects/iki-test-project/locations/europe-west1/keyRings/iki-test/cryptoKeys/filestore"
  client_cidrs = ["10.40.0.0/20", "10.44.0.0/14"]
}

run "it_survives_losing_a_zone_encrypted_with_its_own_key" {
  assert {
    condition     = google_filestore_instance.this.tier == "REGIONAL" && google_filestore_instance.this.location == "europe-west1" && google_filestore_instance.this.kms_key_name == var.kms_key
    error_message = "It should be regional and encrypted with its own key."
  }
  assert {
    condition     = google_filestore_instance.this.deletion_protection_enabled
    error_message = "It shouldn't be deletable by accident."
  }
}

run "root_is_squashed_and_only_the_clients_named_mount_it" {
  assert {
    condition     = [for o in one(google_filestore_instance.this.file_shares).nfs_export_options : "${o.squash_mode} ${join(",", o.ip_ranges)}"] == ["ROOT_SQUASH 10.40.0.0/20,10.44.0.0/14"]
    error_message = "Every client's root should be nobody on it, and only the clients named should mount it."
  }
  assert {
    condition     = one(google_filestore_instance.this.networks).connect_mode == "PRIVATE_SERVICE_ACCESS"
    error_message = "It should take an address in the range for Google's managed services."
  }
}

run "it_tells_runs_where_to_mount_it" {
  assert {
    condition     = output.address == "10.40.240.10" && output.share == "iki" && output.cidr == "10.40.240.10/32"
    error_message = "Runs' PersistentVolume and the VM tier need its address and share."
  }
}

run "one_machine_makes_iki_s_directory_as_root" {
  variables {
    root_squash_except_cidrs = ["10.40.1.5/32"]
  }
  assert {
    condition     = [for o in one(google_filestore_instance.this.file_shares).nfs_export_options : o.squash_mode] == ["NO_ROOT_SQUASH", "ROOT_SQUASH"]
    error_message = "Only the machines named should keep root."
  }
}

run "refuses_too_small_an_instance" {
  command = plan
  variables {
    capacity_gib = 512
  }
  expect_failures = [var.capacity_gib]
}
