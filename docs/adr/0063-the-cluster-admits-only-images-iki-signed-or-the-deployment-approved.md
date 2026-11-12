# 0063. The cluster admits only images Iki signed or the deployment approved

- Status: Accepted
- Date: 2026-11-12
- Refines: [0017](0017-supply-chain-security.md),
  [0061](0061-container-tier-runs-get-pods-of-their-own.md)

## Context

Container-tier runs start pods from plugins' images (ADR 0061). Plugins pin
each image by digest, the deployment's build checks the plugins, and only the
provisioner may create pods in the runs' namespace. ADR 0017 asks for one more
check: the cluster itself refuses to run what wasn't signed by the pipeline
that was meant to produce it.

Iki signs its own images. Plugins' images come from anywhere: Docker Hub, a
vendor's registry, the plugin's own CI. Most aren't signed, and a deployment
can't push signatures into registries it doesn't own. In a GxP setting, an
auditor asks who approved the software that ran. A digest in a plugin's
repository says what it is, not that this deployment's reviewers accepted it.

## Decision

**The deployment's build approves the images its runs use, by signing them.**
`iki deploy build --images FILE` lists each environment image that the
deployment's compute binds to a container-tier template. The config template's
CI signs each one on `main`, keyless, as its build workflow, after the same
review and checks that gate its server image. The signatures go to a package
of its own, `ghcr.io/<owner>/iki-<deployment>-approvals`
(`COSIGN_REPOSITORY`), so any registry's images can be approved. Signing an
image fails the build if it can't be read, which catches a typo in a digest
before deploying. This is how Sigstore suggests signing images you don't
publish.

**The cluster admits only images signed by Iki or by that build.** With
`containerTier.imagePolicy`, the chart adds a `ClusterImagePolicy` for
Sigstore's policy-controller. It matches Iki's run pods, and the Jobs that
make them, by the provisioner's label, wherever they are. It admits an image
signed either:

- by Iki's release workflow, which signs the worker image each pod copies
  its binary from, or
- by the deployment's build workflow, with its signature in the approvals
  package

policy-controller checks namespaces that opt in, and in those it refuses
pods that no policy matches. So in the runs' namespace, nothing else starts
either. The chart refuses to install when policy-controller is missing, or
when it can see a runs namespace without the opt-in label. We chose
policy-controller, Sigstore's own admission controller, because Iki already
signs with cosign. Sites that standardize on Kyverno can write the same
rule, and the README says so.

**A refused pod fails its runs, saying what to check.** Kubernetes refuses
the provisioner's Job when the policy denies its image. The launch ends as
`refused`, with what the cluster said. The provisioner tries again with its
usual backoff. After three refusals within half an hour, the environment's
queued runs fail. Their message says that an administrator can check that
the deployment's build approved the image and that the policy admits it.
Other errors from Kubernetes, which may pass, only back off.

## Consequences

- An image reaches a run pod only after the deployment's own pipeline
  approved it: a reviewed change to its lockfile or compute, built on `main`.
  The approval is a signature anyone can verify, logged in Sigstore's
  transparency log.
- Approving an image means trusting it. The build doesn't check plugin
  authors' own signatures on their images yet. Plugins that sign theirs
  could declare their signer, and the build could check it before approving.
- A site with a private registry logs the build in to read its images
  first. A site with its own Fulcio and Rekor, or a private approvals
  package, sets `fulcioUrl`, `rekorUrl` and `signaturePullSecrets`.
- The development cluster loads its images into kind without a registry,
  so it can't sign or check them. Tests render the policy, and its fields
  were checked against policy-controller's v1beta1 schema. The policy is
  first enforced on a real cluster.
