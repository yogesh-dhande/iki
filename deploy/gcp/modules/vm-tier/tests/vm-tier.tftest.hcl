# Runs against a mocked Google provider: no project, no credentials. Each run checks a
# property ADR 0074 and 0107 promise.

mock_provider "google" {
  mock_resource "google_service_account" {
    defaults = {
      email  = "iki-test-vm@iki-test-project.iam.gserviceaccount.com"
      member = "serviceAccount:iki-test-vm@iki-test-project.iam.gserviceaccount.com"
    }
  }
}

variables {
  name        = "iki-test"
  project_id  = "iki-test-project"
  region      = "europe-west1"
  network_id  = "projects/iki-test-project/global/networks/iki-test"
  subnet_id   = "projects/iki-test-project/regions/europe-west1/subnetworks/iki-test-nodes"
  vpc_cidr    = "10.40.0.0/16"
  network_tag = "iki-test-vm"
  kms_key     = "projects/iki-test-project/locations/europe-west1/keyRings/iki-test/cryptoKeys/vm-disks"
}

run "machines_have_no_public_address" {
  assert {
    condition     = alltrue([for n in google_compute_region_instance_template.machines.network_interface : length(n.access_config) == 0])
    error_message = "Machines take no public address: their worker dials out."
  }
}

run "machines_are_shielded_and_nobody_signs_in_with_a_stray_key" {
  assert {
    condition = (
      one(google_compute_region_instance_template.machines.shielded_instance_config).enable_secure_boot
      && one(google_compute_region_instance_template.machines.shielded_instance_config).enable_vtpm
    )
    error_message = "Machines should boot securely, with a vTPM."
  }
  assert {
    condition = (
      google_compute_region_instance_template.machines.metadata["block-project-ssh-keys"] == "TRUE"
      && google_compute_region_instance_template.machines.metadata["enable-oslogin"] == "TRUE"
      && google_compute_region_instance_template.machines.metadata["serial-port-enable"] == "FALSE"
    )
    error_message = "Only OS Login should let anyone in; no project SSH keys, no serial port."
  }
}

run "disks_are_encrypted_with_their_own_key" {
  assert {
    condition     = alltrue([for d in google_compute_region_instance_template.machines.disk : one(d.disk_encryption_key).kms_key_self_link == var.kms_key])
    error_message = "Every disk should be encrypted with the VM tier's key."
  }
}

run "out_only_to_the_vpc_unless_asked" {
  assert {
    condition     = google_compute_firewall.vpc.destination_ranges == toset(["10.40.0.0/16"]) && one(google_compute_firewall.vpc.allow).ports == tolist(["443"])
    error_message = "Machines should reach Iki in the VPC, over TLS."
  }
  assert {
    condition     = google_compute_firewall.vpc.target_tags == toset(["iki-test-vm"]) && length(google_compute_firewall.https) == 0 && length(google_compute_firewall.nfs) == 0
    error_message = "Nothing else, unless asked."
  }
}

run "file_systems_by_their_addresses" {
  variables {
    nfs_cidrs = ["10.40.240.8/29"]
  }
  assert {
    condition     = google_compute_firewall.nfs[0].destination_ranges == toset(["10.40.240.8/29"]) && contains(one(google_compute_firewall.nfs[0].allow).ports, "2049")
    error_message = "NFS on 2049, to the file system only."
  }
}

run "refuses_a_network_that_isnt_one" {
  command = plan
  variables {
    extra_https_cidrs = ["registry.example.com"]
  }
  expect_failures = [var.extra_https_cidrs]
}
