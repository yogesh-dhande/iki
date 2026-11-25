# Runs against a mocked Google provider: no project, no credentials. It checks that the
# modules fit together, and what the chart and the cluster are told.

# time_sleep waits for nothing here.
mock_provider "time" {}

mock_provider "google" {
  mock_data "google_project" {
    defaults = { number = "123456789012" }
  }
  mock_data "google_storage_project_service_account" {
    defaults = { email_address = "service-123456789012@gs-project-accounts.iam.gserviceaccount.com" }
  }
  mock_data "google_logging_project_cmek_settings" {
    defaults = { service_account_id = "cmek-p123456789012@gcp-sa-logging.iam.gserviceaccount.com" }
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
  mock_resource "google_sql_database_instance" {
    defaults = {
      dns_name = ""
      dns_names = [
        { connection_type = "PRIVATE_SERVICES_ACCESS", dns_scope = "INSTANCE", name = "0123456789ab.3e753kupt8481.europe-west1.sql-psa.goog." },
        { connection_type = "PRIVATE_SERVICES_ACCESS", dns_scope = "CLUSTER", name = "1m2i919vgq4hn.3e753kupt8481.global.sql-psa.goog." },
      ]
      private_ip_address = "10.40.240.5"
    }
  }
  mock_resource "google_logging_project_bucket_config" {
    defaults = { id = "projects/iki-test-project/locations/europe-west1/buckets/iki-test" }
  }
  # Cloud SQL checks its network's ID is one.
  mock_resource "google_compute_network" {
    defaults = {
      id        = "projects/iki-test-project/global/networks/iki-test"
      self_link = "https://www.googleapis.com/compute/v1/projects/iki-test-project/global/networks/iki-test"
    }
  }
  mock_resource "google_filestore_instance" {
    defaults = { networks = { ip_addresses = ["10.40.240.10"] } }
  }
}

variables {
  name               = "iki-test"
  project_id         = "iki-test-project"
  region             = "europe-west1"
  dr_region          = "europe-west4"
  multi_region       = "europe"
  zones              = ["europe-west1-b", "europe-west1-c", "europe-west1-d"]
  kubernetes_version = "1.36"
  admin_members      = ["group:platform@example.com"]
  state_kms_key      = "projects/iki-test-project/locations/europe/keyRings/iki-test-tofu-state/cryptoKeys/state"
  public_host        = "iki.example.com"
}

# Service accounts' addresses are mocked, so pin the server's: the chart's values are then
# known exactly.
override_resource {
  target = module.access.google_service_account.server
  values = {
    name   = "projects/iki-test-project/serviceAccounts/iki-test-server@iki-test-project.iam.gserviceaccount.com"
    email  = "iki-test-server@iki-test-project.iam.gserviceaccount.com"
    member = "serviceAccount:iki-test-server@iki-test-project.iam.gserviceaccount.com"
  }
}

# And the external load balancer's address, which workers reach the server at.
override_resource {
  target = google_compute_global_address.web
  values = { address = "203.0.113.10" }
}

run "the_chart_gets_exactly_the_values_its_tests_use" {
  # deploy/helm/iki/tests renders the chart with this file and values-production.yaml.
  assert {
    condition     = yamldecode(output.helm_values) == yamldecode(file("tests/helm-values.yaml"))
    error_message = "helm_values changed: update tests/helm-values.yaml to match, and the chart's tests will check it."
  }
}

run "every_secret_reaches_the_cluster_under_the_name_the_chart_is_given" {
  assert {
    condition = toset([
      for d in split("---\n", output.external_secrets) : yamldecode(d).spec.target.name if yamldecode(d).kind == "ExternalSecret"
      ]) == toset([
      for v in [
        yamldecode(output.helm_values).secretKey, yamldecode(output.helm_values).encryptionKeys,
        yamldecode(output.helm_values).bundles.signingKey, yamldecode(output.helm_values).runSecrets,
        yamldecode(output.helm_values).database, yamldecode(output.helm_values).database.migrator,
      ] : v.existingSecret
    ])
    error_message = "Each Secret the chart is told of should be made by an ExternalSecret, and no others."
  }
}

run "files_reach_cloud_storage_as_the_server_with_no_key" {
  assert {
    condition     = yamldecode(output.helm_values).files.backend == "gcs" && !contains(keys(yamldecode(output.helm_values).files), "credentials")
    error_message = "Files should go through Cloud Storage's own APIs, as the server's service account (ADR 0109), with no key in a Secret."
  }
  assert {
    condition     = !strcontains(output.put_secrets, module.access.server_service_account)
    error_message = "put-secrets.sh makes no key for the server any more."
  }
}

run "the_load_balancer_speaks_only_modern_tls" {
  assert {
    condition     = google_compute_ssl_policy.web[0].min_tls_version == "TLS_1_2" && google_compute_ssl_policy.web[0].profile == "RESTRICTED"
    error_message = "The load balancer should accept TLS 1.2 or later, with modern ciphers only."
  }
  assert {
    condition     = [for l in yamldecode(split("---\n", output.gateway)[0]).spec.listeners : "${l.protocol}:${l.port}"] == ["HTTPS:443"]
    error_message = "The Gateway should listen on HTTPS alone."
  }
  assert {
    condition     = yamldecode(split("---\n", output.gateway)[0]).spec.gatewayClassName == "gke-l7-global-external-managed"
    error_message = "An external Gateway should be Google's global external Application Load Balancer."
  }
  assert {
    condition     = length(google_compute_security_policy.allowlist) == 0
    error_message = "Open to everyone, no allowlist is needed."
  }
}

run "only_the_networks_listed_reach_iki" {
  variables {
    ingress_cidrs = ["203.0.113.0/24"]
  }
  assert {
    condition     = toset([for r in google_compute_security_policy.allowlist[0].rule : "${r.action} ${join(",", one(one(r.match).config).src_ip_ranges)}"]) == toset(["allow 203.0.113.0/24", "deny(403) *"])
    error_message = "Cloud Armor should allow the networks listed and refuse everyone else."
  }
  assert {
    condition     = yamldecode(split("---\n", output.gateway)[3]).spec.default.securityPolicy == "iki-test-web"
    error_message = "The web Service's backend should use that policy."
  }
}

run "an_internal_load_balancer_sits_in_the_vpc" {
  variables {
    internal             = true
    internal_certificate = "iki-internal"
  }
  assert {
    condition     = yamldecode(split("---\n", output.gateway)[0]).spec.gatewayClassName == "gke-l7-rilb" && length(google_compute_global_address.web) == 0
    error_message = "An internal Gateway should be a regional internal Application Load Balancer, with no public address."
  }
  assert {
    condition     = contains([for r in yamldecode(output.helm_values).networkPolicy.ingressFrom : r.ipBlock.cidr], module.network.proxy_cidr)
    error_message = "And web pods should admit it from the proxy-only subnet."
  }
  assert {
    condition     = yamldecode(output.helm_values).networkPolicy.egress.server == { ports = [443], to = [{ ipBlock = { cidr = "${google_compute_address.web[0].address}/32" } }] }
    error_message = "Backend workers should reach the server through the internal load balancer's private address, and only it."
  }
  assert {
    condition     = google_dns_managed_zone.web[0].visibility == "private" && google_dns_managed_zone.web[0].dns_name == "iki.example.com." && one(one(google_dns_managed_zone.web[0].private_visibility_config).networks).network_url == module.network.network_id
    error_message = "public_host should resolve inside the VPC alone, from a private zone holding that one name."
  }
  assert {
    condition     = google_dns_record_set.web[0].type == "A" && length(google_dns_record_set.web[0].rrdatas) == 1 && google_dns_record_set.web[0].rrdatas[0] == google_compute_address.web[0].address
    error_message = "public_host should resolve to the internal load balancer's address."
  }
}

run "an_internal_load_balancer_named_by_your_own_dns" {
  variables {
    internal             = true
    internal_certificate = "iki-internal"
    internal_dns         = false
  }
  assert {
    condition     = length(google_dns_managed_zone.web) == 0 && length(google_dns_record_set.web) == 0
    error_message = "With internal_dns off, no zone should be made: the company's DNS names it."
  }
}

run "an_external_load_balancer_is_where_workers_reach_the_server" {
  assert {
    condition     = yamldecode(output.helm_values).networkPolicy.egress.server == { ports = [443], to = [{ ipBlock = { cidr = "${google_compute_global_address.web[0].address}/32" } }] }
    error_message = "Backend workers should reach the server at the external load balancer's own address, and only it."
  }
}

run "refuses_an_internal_load_balancer_without_a_certificate" {
  command = plan
  variables {
    internal = true
  }
  expect_failures = [var.internal_certificate]
}

run "the_cluster_runs_only_the_images_the_chart_and_runbook_use" {
  # deploy/helm/iki/tests checks every image the chart renders matches one of these.
  assert {
    condition     = output.allowed_images == jsondecode(file("tests/allowed-images.json"))
    error_message = "The images Binary Authorization allows changed: update tests/allowed-images.json to match, and the chart's tests will check the chart's images against it."
  }
}

run "the_cluster_runs_the_config_builds_two_pinned_images" {
  variables {
    config_image_files = ["tests/config-image.yaml"]
  }
  assert {
    condition = toset(output.allowed_images) == setunion(toset(jsondecode(file("tests/allowed-images.json"))), toset([
      "ghcr.io/acme/iki-production@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "ghcr.io/acme/iki-production-worker@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    ]))
    error_message = "Only the two exact references from the reviewed config build should be added."
  }
}

run "the_config_build_must_have_pinned_both_images" {
  command = plan
  variables {
    config_image_files = ["../../config-template/deployments/production/image.yaml"]
  }
  expect_failures = [var.config_image_files]
}

run "the_vm_tier_when_asked" {
  variables {
    vm_tier = {}
  }
  assert {
    condition     = output.vm_tier.provisioner != null && output.vm_tier.instance_template != null
    error_message = "The provisioner should get an identity, and machines a template."
  }
  assert {
    condition = (
      yamldecode(output.helm_values).vmTier.enabled && yamldecode(output.helm_values).vmTier.cloud == "gcp"
      && yamldecode(output.helm_values).vmTier.owner == "iki-test"
      && yamldecode(output.helm_values).vmTier.gcp.project == "iki-test-project"
      && yamldecode(output.helm_values).vmTier.gcp.instanceTemplate == output.vm_tier.instance_template
      && yamldecode(output.helm_values).vmTier.gcp.kmsKey == output.vm_tier.disk_kms_key
      && yamldecode(output.helm_values).vmTier.serviceAccountAnnotations["iam.gke.io/gcp-service-account"] == output.vm_tier.provisioner
    )
    error_message = "The chart's VM tier should start Compute Engine machines from this deployment's template, as the provisioner's own identity."
  }
  assert {
    condition     = !contains(keys(yamldecode(output.helm_values).vmTier), "serverUrl")
    error_message = "Machines reach publicUrl unless server_url says otherwise."
  }
  assert {
    condition     = alltrue([for r in values(module.registry.readers) : length(r) == 2])
    error_message = "Both the nodes and the VM tier's machines should pull from both repositories."
  }
}

run "the_vm_tiers_machines_reach_iki_where_told" {
  variables {
    vm_tier = { server_url = "https://iki.internal.example.com" }
  }
  assert {
    condition     = yamldecode(output.helm_values).vmTier.serverUrl == "https://iki.internal.example.com"
    error_message = "Machines should reach Iki at vm_tier.server_url, from the VPC."
  }
}

run "refuses_a_vm_tier_server_url_that_isnt_https" {
  command = plan
  variables {
    vm_tier = { server_url = "http://iki.internal.example.com" }
  }
  expect_failures = [var.vm_tier]
}

run "shared_reference_data_when_asked" {
  variables {
    filestore = {}
  }
  assert {
    condition     = output.filestore == { address = "10.40.240.10", share = "iki" } && module.cluster.name == "iki-test"
    error_message = "Runs should be told where the shared file system is."
  }
}

# Recovering imports the lost deployment's replica and bucket, which OpenTofu's tests can't
# do; the database and storage modules' tests check adopting them. Here: that the names
# main.tf's import blocks look for are the ones a deployment makes.
run "a_recovery_finds_what_this_deployment_leaves_in_its_dr_region" {
  assert {
    condition     = output.database_replica == "iki-test-dr" && yamldecode(output.helm_values).files.bucket == "iki-test-files-iki-test-project"
    error_message = "local.lost in main.tf names iki-test-dr and iki-test-files-iki-test-project: keep them in step with the modules' names."
  }
}

run "refuses_to_recover_under_the_lost_deployments_name" {
  command = plan
  variables {
    recovery = { name = "iki-test", lost_region = "europe-west3" }
  }
  expect_failures = [var.recovery]
}

run "refuses_to_make_anything_in_the_lost_region" {
  command = plan
  variables {
    name     = "iki-test-r1"
    recovery = { name = "iki-test", lost_region = "europe-west4" }
  }
  expect_failures = [var.recovery]
}

run "refuses_the_same_region_for_dr" {
  command = plan
  variables {
    dr_region = "europe-west1"
  }
  expect_failures = [var.dr_region]
}

run "everything_protected_from_deletion_by_default" {
  assert {
    condition     = module.cluster.deletion_protection && module.database.deletion_protection && module.secrets.deletion_protection
    error_message = "The cluster, the database and the secrets should refuse to be deleted unless told otherwise."
  }
}

run "a_teardown_turns_deletion_protection_off_everywhere" {
  variables {
    deletion_protection = false
  }
  assert {
    condition     = !module.cluster.deletion_protection && !module.database.deletion_protection && !module.secrets.deletion_protection
    error_message = "deletion_protection = false should let the cluster, the database and the secrets be destroyed."
  }
}

run "data_access_logs_for_the_services_named" {
  variables {
    data_access_services = ["secretmanager.googleapis.com", "cloudkms.googleapis.com"]
  }
  assert {
    condition     = toset(keys(module.logs.data_access_services)) == toset(["secretmanager.googleapis.com", "cloudkms.googleapis.com"])
    error_message = "Only the services named should have their Data Access audit logs turned on."
  }
}

run "a_pair_with_a_predefined_dual_region_is_made_as_it_with_its_key_there" {
  variables {
    region       = "us-central1"
    dr_region    = "us-east1"
    multi_region = "us"
    zones        = ["us-central1-b", "us-central1-c"]
  }
  assert {
    condition     = module.storage.location == "NAM4" && local.files_dual_region == "NAM4"
    error_message = "Cloud Storage refuses a custom dual-region of us-central1 and us-east1, and a key for NAM4 in us: it's NAM4, with its key in nam4."
  }
}

run "any_other_pair_is_a_custom_dual_region_with_its_key_in_the_multi_region" {
  assert {
    condition     = module.storage.location == "EU" && local.files_dual_region == null
    error_message = "europe-west1 and europe-west4 have no predefined dual-region: a custom one in EU."
  }
}
