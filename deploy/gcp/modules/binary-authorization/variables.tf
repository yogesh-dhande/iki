variable "name" {
  description = "The deployment's name, for the policy's description."
  type        = string
}

variable "project_id" {
  description = "The project whose policy this is: one per project, so one deployment per project."
  type        = string
}

variable "repositories" {
  description = "The deployment's own image repositories, from the artifact-registry module: everything in them may run."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for r in var.repositories : can(regex("^[a-z0-9-]+-docker\\.pkg\\.dev/[^/*]+/[^/*]+$", r))])
    error_message = "Give Artifact Registry repositories, such as europe-west1-docker.pkg.dev/<project>/iki-production-worker."
  }
}

variable "images" {
  description = "Other images that may run, as Binary Authorization's patterns: an image (ghcr.io/acme/tool:1.2 or @sha256:...), or a path ending in * (within one level, so its tags and digests) or ** (and below it)."
  type        = list(string)
  default     = []

  validation {
    # A wildcard only at the end, after at least a registry and one path segment: never a
    # whole registry, such as docker.io/**.
    condition     = alltrue([for i in var.images : can(regex("^[a-z0-9.-]+(:[0-9]+)?/[^*\\s]+(\\*|\\*\\*)?$", i))])
    error_message = "Give images or paths under a registry, each with at most one wildcard (* or **) at the end, such as ghcr.io/acme/tool*: never a whole registry."
  }
}
