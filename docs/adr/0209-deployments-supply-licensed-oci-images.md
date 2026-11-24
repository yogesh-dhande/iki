# 0209. Deployments supply licensed OCI images

- Status: Accepted
- Date: 2026-11-24
- Refines [0040](0040-action-and-environment-manifests.md),
  [0041](0041-compute-templates-and-bindings.md),
  [0063](0063-the-cluster-admits-only-images-iki-signed-or-the-deployment-approved.md),
  and [0075](0075-environment-setup-builds-and-managed-volumes.md)

## Context

Some licensed tools can run on a deployment's cloud compute only if the
customer provides a container image. A plugin still needs to declare the
environment and its resource requirements. The deployment controls the
image it is licensed to use and the registry from which its machines pull it.

## Decision

A plugin marks an environment with `customer_image_required: true` when
its container or VM actions need an image supplied by the deployment.
In `compute.yaml`, a Compute admin binds the environment to compute
templates and can add one or more `EnvironmentImageBinding` documents.
Each names the qualified environment, a range of plugin releases, a
registry image pinned by SHA-256, and an optional pull Secret reference.

```yaml
apiVersion: iki/v1
kind: EnvironmentImageBinding
environment: acme/analysis.licensed-tools
plugin_version: ">=1.2,<2"
image: registry.example.org/lab/tools@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
pull_secret: lab-registry
```

The range uses the same numeric release comparisons as the plugin's
`iki` constraint. The installed version can match at most one binding.
A cloud action whose environment has no matching image is unavailable.
Its launch refusal tells the user which environment needs a binding.
A self-hosted action running installed software continues to select lab
machines by labels.

For Kubernetes, the optional Secret name is resolved by the kubelet in
the runs namespace. The deployment manages that Secret and any registry
approval. VM templates use their existing registry identity. Slurm uses
its existing worker image policy and registry access. Iki lists the
matching container image in `iki deploy build --images` for the
deployment's admission approval. The customer maintains the image in
their registry, and Iki's release workflow only publishes its own images.

A run pins the resolved image when queued. Its attempts record the image
digest in `environment_build` alongside the compute template and volume
versions. Retries keep the pin, warm machines serve compatible pins, and
deterministic reuse accounts for the digest. Exact reproduction requires
the deployment to still authorize the same image.

Compute admins can read the configured pins and installed-version
matches at `GET /api/v1/compute/image-bindings`. The admin Compute page
can show these bindings alongside Volumes from step 36. The API provides
Secret names to Compute admins. Registry credential values stay in the
deployment's secret store.

## Consequences

Deployments can run customer-licensed images through the same resource
and provenance paths as plugin-pinned images. The customer provides an
image compatible with Iki's worker and the plugin's commands, and keeps
the pinned digest accessible for queued runs and retries. The deployment
must provision its registry access and approve the image before the
cluster can run it.
