# A Google identity for each pod that calls Google Cloud, through Workload Identity
# Federation for GKE (ADR 0107), each allowed only what it uses. Backend workers and run
# pods get none (ADR 0043, 0061).

data "google_project" "this" {
  project_id = var.project_id
}

locals {
  pool = "${var.project_id}.svc.id.goog"
  # A Kubernetes service account as an IAM principal, without a Google service account.
  principal = "principal://iam.googleapis.com/projects/${data.google_project.this.number}/locations/global/workloadIdentityPools/${local.pool}/subject"
  # Iki's own server pods: web, task runners and the hourly maintenance job. They share a
  # Google service account, since the chart annotates all three alike; Cloud Audit Logs
  # name the Kubernetes service account behind each call.
  server = {
    web         = "${var.release}-web"
    tasks       = "${var.release}-tasks"
    maintenance = "${var.release}-maintenance"
  }
  role_prefix = replace(var.name, "-", "_")
  prefix      = var.files.prefix
  # Cloud Storage names objects projects/_/buckets/<bucket>/objects/<name>.
  objects = "projects/_/buckets/${var.files.bucket}/objects/${local.prefix}"
}

# --- Iki's server pods -------------------------------------------------------------------

resource "google_service_account" "server" {
  project      = var.project_id
  account_id   = "${var.name}-server"
  display_name = "${var.name}: Iki's server"
  description  = "Iki's web, tasks and maintenance pods (${var.namespace}): the files bucket under Iki's prefix, registered locations read-only"
}

resource "google_service_account_iam_member" "server" {
  for_each           = local.server
  service_account_id = google_service_account.server.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${local.pool}[${var.namespace}/${each.value}]"
}

# The files bucket under Iki's prefix: read, write, and delete versions (artifacts and
# bundles expire, ADR 0068 and 0096). Cloud Storage uses the bucket's key itself, so
# pods need no key of their own. The bucket's settings are OpenTofu's: pods can't change
# its versioning, policy, CORS or lifecycle.
resource "google_project_iam_custom_role" "files_objects" {
  project     = var.project_id
  role_id     = "${local.role_prefix}_files_objects"
  title       = "${var.name}: Iki's files"
  description = "Read, write and delete Iki's objects and their versions"
  permissions = [
    "storage.objects.create", "storage.objects.delete", "storage.objects.get",
    "storage.objects.list", "storage.objects.restore",
  ]
}

resource "google_project_iam_custom_role" "files_bucket" {
  project     = var.project_id
  role_id     = "${local.role_prefix}_files_bucket"
  title       = "${var.name}: Iki's bucket"
  description = "See the files bucket, list its objects and run multipart uploads"
  permissions = [
    "storage.buckets.get", "storage.objects.list",
    "storage.multipartUploads.create", "storage.multipartUploads.abort",
    "storage.multipartUploads.list", "storage.multipartUploads.listParts",
  ]
}

resource "google_storage_bucket_iam_member" "objects" {
  bucket = var.files.bucket
  role   = google_project_iam_custom_role.files_objects.id
  # Not before IAM knows the role everywhere (time_sleep.roles).
  depends_on = [time_sleep.roles]
  member     = google_service_account.server.member
  dynamic "condition" {
    for_each = local.prefix == "" ? [] : [local.prefix]
    content {
      title       = "under-iki-prefix"
      description = "Only objects under ${condition.value}"
      expression  = "resource.name.startsWith(\"${local.objects}\")"
    }
  }
}

resource "google_storage_bucket_iam_member" "bucket" {
  bucket = var.files.bucket
  role   = google_project_iam_custom_role.files_bucket.id
  # Not before IAM knows the role everywhere (time_sleep.roles).
  depends_on = [time_sleep.roles]
  member     = google_service_account.server.member
}

# Signed URLs (ADR 0109): pods have no key, so IAM signs each URL as the server's
# account (signBlob). Granted on that account alone, to itself, in a role of that one
# permission rather than Service Account Token Creator, which would also let it mint its
# own tokens and sign JWTs.
resource "google_project_iam_custom_role" "sign_urls" {
  project     = var.project_id
  role_id     = "${local.role_prefix}_sign_urls"
  title       = "${var.name}: sign Iki's links"
  description = "Sign bytes as a service account: Cloud Storage's signed URLs"
  permissions = ["iam.serviceAccounts.signBlob"]
}

resource "google_service_account_iam_member" "sign_as_itself" {
  service_account_id = google_service_account.server.name
  role               = google_project_iam_custom_role.sign_urls.id
  # Not before IAM knows the role everywhere (time_sleep.roles).
  depends_on = [time_sleep.roles]
  member     = google_service_account.server.member
}

# Registered locations, read only (and their buckets' versioning, to pin a version).
resource "google_project_iam_custom_role" "registered" {
  count       = length(var.register_locations) == 0 ? 0 : 1
  project     = var.project_id
  role_id     = "${local.role_prefix}_registered_files"
  title       = "${var.name}: registered files"
  description = "Read files registered in place, and see their buckets"
  permissions = ["storage.buckets.get", "storage.objects.get", "storage.objects.list"]
}

resource "google_storage_bucket_iam_member" "registered" {
  for_each = { for l in var.register_locations : "${l.bucket}/${l.prefix}" => l }
  bucket   = each.value.bucket
  role     = google_project_iam_custom_role.registered[0].id
  # Not before IAM knows the role everywhere (time_sleep.roles).
  depends_on = [time_sleep.roles]
  member     = google_service_account.server.member
  dynamic "condition" {
    for_each = each.value.prefix == "" ? [] : [each.value.prefix]
    content {
      title       = "under-registered-prefix"
      description = "Only objects under ${condition.value}, and the bucket itself"
      expression  = "resource.type == \"storage.googleapis.com/Bucket\" || resource.name.startsWith(\"projects/_/buckets/${each.value.bucket}/objects/${condition.value}\")"
    }
  }
}

# --- The VM tier's provisioner (ADR 0074) ------------------------------------------------

locals {
  vm = var.vm_tier != null
  # The provisioner names every machine, disk and image it makes <owner>-...; IAM's
  # conditions on Compute Engine's resource names hold it to those.
  its_own = local.vm ? join(" || ", [for kind in ["instances", "disks", "images"] :
    "resource.name.extract(\"/${kind}/{name}\").startsWith(\"${var.vm_tier.owner}-\")"
  ]) : ""
}

resource "google_service_account" "provisioner" {
  count        = local.vm ? 1 : 0
  project      = var.project_id
  account_id   = "${var.name}-provisioner"
  display_name = "${var.name}: Iki's provisioner"
  description  = "Iki's provisioner (${var.namespace}/${var.release}-provisioner): machines, disks and images named ${var.vm_tier.owner}-* only"
}

resource "google_service_account_iam_member" "provisioner" {
  count              = local.vm ? 1 : 0
  service_account_id = google_service_account.provisioner[0].name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${local.pool}[${var.namespace}/${var.release}-provisioner]"
}

# Starts, labels and ends its own machines; makes and deletes its own disks, and the
# images a setup build's disks become (ADR 0076, 0109): only those named for its
# deployment. Images rather than snapshots: Compute Engine makes at most six disks an
# hour from one snapshot in a zone, and runs start many machines from one.
resource "google_project_iam_custom_role" "machines" {
  count       = local.vm ? 1 : 0
  project     = var.project_id
  role_id     = "${local.role_prefix}_vm_machines"
  title       = "${var.name}: the VM tier's machines"
  description = "Start, label and end machines, disks and images, where a condition allows"
  permissions = [
    "compute.instances.create", "compute.instances.delete", "compute.instances.get",
    "compute.instances.setLabels", "compute.instances.setMetadata", "compute.instances.setServiceAccount",
    "compute.instances.setTags", "compute.instances.attachDisk", "compute.instances.detachDisk",
    "compute.instances.getSerialPortOutput",
    "compute.disks.create", "compute.disks.delete", "compute.disks.get", "compute.disks.setLabels",
    "compute.disks.use", "compute.disks.useReadOnly",
    "compute.images.create", "compute.images.delete", "compute.images.setLabels",
  ]
}

resource "google_project_iam_member" "machines" {
  count   = local.vm ? 1 : 0
  project = var.project_id
  role    = google_project_iam_custom_role.machines[0].id
  # Not before IAM knows the role everywhere (time_sleep.roles).
  depends_on = [time_sleep.roles]
  member     = google_service_account.provisioner[0].member
  condition {
    title       = "its-own-machines"
    description = "Only machines, disks and images named ${var.vm_tier.owner}-*"
    expression  = local.its_own
  }
}

# Seeing what's there, its operations' progress, and why a machine stopped (a zone's
# operations say whether Compute Engine preempted it): Compute Engine's list calls and
# operations can't be limited by name. Reading images is for any image, Google's
# Container-Optimized OS among them.
resource "google_project_iam_custom_role" "observe" {
  count       = local.vm ? 1 : 0
  project     = var.project_id
  role_id     = "${local.role_prefix}_vm_observe"
  title       = "${var.name}: see the VM tier"
  description = "List machines, disks and images, and follow operations"
  permissions = [
    "compute.instances.list", "compute.disks.list", "compute.images.list",
    "compute.zoneOperations.get", "compute.zoneOperations.list",
    "compute.regionOperations.get", "compute.globalOperations.get",
    "compute.zones.list", "compute.machineTypes.get", "compute.acceleratorTypes.get",
    "compute.images.get", "compute.images.getFromFamily", "compute.images.useReadOnly",
    "compute.instanceTemplates.get", "compute.instanceTemplates.useReadOnly",
  ]
}

resource "google_project_iam_member" "observe" {
  count   = local.vm ? 1 : 0
  project = var.project_id
  role    = google_project_iam_custom_role.observe[0].id
  # Not before IAM knows the role everywhere (time_sleep.roles).
  depends_on = [time_sleep.roles]
  member     = google_service_account.provisioner[0].member
}

# Machines go in the nodes' subnet, and nowhere else.
resource "google_project_iam_custom_role" "subnet" {
  count       = local.vm ? 1 : 0
  project     = var.project_id
  role_id     = "${local.role_prefix}_vm_subnet"
  title       = "${var.name}: the VM tier's subnet"
  description = "Put machines in a subnet, with no public address"
  permissions = ["compute.subnetworks.use"]
}

resource "google_compute_subnetwork_iam_member" "provisioner" {
  count      = local.vm ? 1 : 0
  project    = var.project_id
  region     = var.vm_tier.region
  subnetwork = var.vm_tier.subnet
  role       = google_project_iam_custom_role.subnet[0].id
  # Not before IAM knows the role everywhere (time_sleep.roles).
  depends_on = [time_sleep.roles]
  member     = google_service_account.provisioner[0].member
}

# Machines run as their own account, the only one the provisioner may hand them.
resource "google_service_account_iam_member" "act_as_machines" {
  count              = local.vm ? 1 : 0
  service_account_id = "projects/${var.project_id}/serviceAccounts/${var.vm_tier.machines_service_account}"
  role               = "roles/iam.serviceAccountUser"
  member             = google_service_account.provisioner[0].member
}

# --- External Secrets --------------------------------------------------------------------

# Reads this deployment's secrets, and no others, into the cluster: bound to External
# Secrets' Kubernetes service account directly, with no Google service account.
resource "google_secret_manager_secret_iam_member" "external_secrets" {
  for_each  = var.external_secrets.secret_ids
  project   = var.project_id
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = "${local.principal}/ns/${var.external_secrets.namespace}/sa/${var.external_secrets.service_account}"
}

# --- Custom roles, known everywhere ----------------------------------------------------------

# A custom role takes a while to reach the rest of Google Cloud: Cloud Storage refused a
# binding to one made seconds before ("does not exist in the resource's hierarchy"), on a
# live apply. Every binding to one of this module's roles waits a minute after them, and
# again whenever one is added.
resource "time_sleep" "roles" {
  create_duration = "60s"
  triggers = {
    roles = join(" ", concat(
      [
        google_project_iam_custom_role.files_objects.id,
        google_project_iam_custom_role.files_bucket.id,
        google_project_iam_custom_role.sign_urls.id,
      ],
      google_project_iam_custom_role.registered[*].id,
      google_project_iam_custom_role.machines[*].id,
      google_project_iam_custom_role.observe[*].id,
      google_project_iam_custom_role.subnet[*].id,
    ))
  }
}
