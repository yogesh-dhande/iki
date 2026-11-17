# Runs against a mocked Google provider: no project, no credentials. Each run checks a
# property ADR 0107 promises.

# time_sleep waits for nothing here.
mock_provider "time" {}

mock_provider "google" {
  mock_data "google_project" {
    defaults = { number = "123456789012" }
  }
  mock_resource "google_service_account" {
    defaults = {
      name   = "projects/iki-test-project/serviceAccounts/iki-test@iki-test-project.iam.gserviceaccount.com"
      email  = "iki-test@iki-test-project.iam.gserviceaccount.com"
      member = "serviceAccount:iki-test@iki-test-project.iam.gserviceaccount.com"
    }
  }
  mock_resource "google_project_iam_custom_role" {
    defaults = { id = "projects/iki-test-project/roles/iki_test" }
  }
}

variables {
  name       = "iki-test"
  project_id = "iki-test-project"
  files = {
    bucket = "iki-test-files-iki-test-project"
    prefix = "prod/"
  }
  external_secrets = {
    secret_ids = { "database" = "projects/iki-test-project/secrets/iki-test-database" }
  }
}

run "each_server_pod_uses_the_servers_identity_through_its_own_service_account" {
  assert {
    condition     = toset([for b in google_service_account_iam_member.server : b.member]) == toset(["serviceAccount:iki-test-project.svc.id.goog[iki/iki-web]", "serviceAccount:iki-test-project.svc.id.goog[iki/iki-tasks]", "serviceAccount:iki-test-project.svc.id.goog[iki/iki-maintenance]"])
    error_message = "Web, task and maintenance pods' service accounts should each be bound, and nothing else."
  }
  assert {
    condition     = alltrue([for b in google_service_account_iam_member.server : b.role == "roles/iam.workloadIdentityUser"])
    error_message = "Through Workload Identity, and no more."
  }
}

run "server_pods_reach_only_their_prefix_of_the_files_bucket" {
  assert {
    condition     = one(google_storage_bucket_iam_member.objects.condition).expression == "resource.name.startsWith(\"projects/_/buckets/iki-test-files-iki-test-project/objects/prod/\")"
    error_message = "Server pods should read and write only under Iki's prefix."
  }
  assert {
    condition = !anytrue([for p in setunion(google_project_iam_custom_role.files_objects.permissions, google_project_iam_custom_role.files_bucket.permissions) :
    contains(["storage.buckets.update", "storage.buckets.setIamPolicy", "storage.buckets.delete"], p)])
    error_message = "Server pods shouldn't change the bucket's settings or policy, or delete it."
  }
}

run "server_pods_sign_urls_as_themselves_and_no_one_else" {
  assert {
    condition     = google_project_iam_custom_role.sign_urls.permissions == toset(["iam.serviceAccounts.signBlob"])
    error_message = "Signing URLs needs signBlob, and nothing more: not getAccessToken, which Token Creator would also give."
  }
  assert {
    condition     = google_service_account_iam_member.sign_as_itself.service_account_id == google_service_account.server.name && google_service_account_iam_member.sign_as_itself.member == google_service_account.server.member
    error_message = "Only on the server's own account, to itself."
  }
}

run "registered_locations_are_read_only" {
  variables {
    register_locations = [{ bucket = "lab-data", prefix = "runs/" }]
  }
  assert {
    condition     = google_project_iam_custom_role.registered[0].permissions == toset(["storage.buckets.get", "storage.objects.get", "storage.objects.list"])
    error_message = "Iki should only read registered locations."
  }
  assert {
    condition     = strcontains(one(google_storage_bucket_iam_member.registered["lab-data/runs/"].condition).expression, "projects/_/buckets/lab-data/objects/runs/")
    error_message = "Only under each location's prefix."
  }
}

run "no_provisioner_without_the_vm_tier" {
  assert {
    condition     = length(google_service_account.provisioner) == 0 && output.provisioner_service_account == null
    error_message = "Without the VM tier, nothing should be able to start machines."
  }
}

run "the_provisioner_touches_only_its_own_machines" {
  variables {
    vm_tier = {
      owner                    = "iki-test"
      machines_service_account = "iki-test-vm@iki-test-project.iam.gserviceaccount.com"
      subnet                   = "iki-test-nodes"
      region                   = "europe-west1"
    }
  }
  assert {
    condition     = one(google_project_iam_member.machines[0].condition).expression == "resource.name.extract(\"/instances/{name}\").startsWith(\"iki-test-\") || resource.name.extract(\"/disks/{name}\").startsWith(\"iki-test-\") || resource.name.extract(\"/images/{name}\").startsWith(\"iki-test-\")"
    error_message = "It should make, change and delete only machines, disks and images named for its deployment."
  }
  assert {
    condition     = alltrue([for p in google_project_iam_custom_role.machines[0].permissions : startswith(p, "compute.instances.") || startswith(p, "compute.disks.") || startswith(p, "compute.images.")])
    error_message = "What it may change, under the condition, should be only machines, disks and images."
  }
  assert {
    condition     = !anytrue([for p in google_project_iam_custom_role.observe[0].permissions : endswith(p, ".create") || endswith(p, ".delete") || strcontains(p, ".set")])
    error_message = "What it may do anywhere should only read."
  }
  assert {
    condition     = google_service_account_iam_member.act_as_machines[0].service_account_id == "projects/iki-test-project/serviceAccounts/iki-test-vm@iki-test-project.iam.gserviceaccount.com" && google_service_account_iam_member.act_as_machines[0].role == "roles/iam.serviceAccountUser"
    error_message = "It should hand machines only their own service account."
  }
  assert {
    condition     = google_project_iam_custom_role.subnet[0].permissions == toset(["compute.subnetworks.use"])
    error_message = "It should put machines in the nodes' subnet, without public addresses."
  }
  assert {
    condition     = google_service_account_iam_member.provisioner[0].member == "serviceAccount:iki-test-project.svc.id.goog[iki/iki-provisioner]"
    error_message = "Its identity should be bound to the provisioner's service account."
  }
}

run "external_secrets_reads_this_deployments_secrets_only" {
  assert {
    condition     = [for m in google_secret_manager_secret_iam_member.external_secrets : m.member] == ["principal://iam.googleapis.com/projects/123456789012/locations/global/workloadIdentityPools/iki-test-project.svc.id.goog/subject/ns/external-secrets/sa/external-secrets"]
    error_message = "External Secrets' own Kubernetes service account should be the only reader."
  }
  assert {
    condition     = [for m in google_secret_manager_secret_iam_member.external_secrets : "${m.secret_id} ${m.role}"] == ["projects/iki-test-project/secrets/iki-test-database roles/secretmanager.secretAccessor"]
    error_message = "And only of the secrets it's given."
  }
}

run "bindings_to_its_roles_wait_until_iam_knows_them" {
  assert {
    condition     = time_sleep.roles.create_duration == "60s" && strcontains(time_sleep.roles.triggers.roles, google_project_iam_custom_role.files_objects.id)
    error_message = "Cloud Storage refuses a binding to a custom role made seconds before: bindings wait for time_sleep.roles."
  }
}
