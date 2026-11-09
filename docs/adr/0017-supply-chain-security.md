# 0017. Signed, attested artifacts verified at deploy time

- Status: Accepted
- Date: 2026-11-09

## Context

Enterprises run Iki images, worker binaries, Helm charts and their own
plugin images inside their cloud, close to sensitive data and with access
to compute budgets. A tampered artifact anywhere in that chain would be a
serious breach. Enterprise security teams also need to know what is inside
each artifact and where it came from.

## Decision

- Every Iki image, binary and Helm chart is **signed with cosign**
  (Sigstore's signing tool).
- Each release artifact carries **SLSA provenance**: a signed statement of
  how, where and from which source commit it was built.
- Each artifact ships a **CycloneDX SBOM** (software bill of materials: a
  machine-readable list of every component inside).
- CI runs **vulnerability and license scanning**.
- An **admission policy** (Sigstore policy-controller or Kyverno) verifies
  signatures before Kubernetes runs any pod.
- **Plugin images are pinned by digest**, never by mutable tag.
- Workers verify cosign signatures before applying a self-update
  (ADR 0010).
- Before any third-party tool goes into a first-party plugin, its license
  is checked (for example, AlphaFold3 weights are excluded).
- CI itself is hardened: actions pinned by commit SHA, least-privilege
  tokens and workflow linting (ADR 0019).

## Consequences

- Positive: a cluster refuses to run anything that was not built and
  signed by the expected pipeline.
- Positive: security teams get SBOMs and provenance they can feed into
  their own tooling.
- Positive: digest pinning makes deployments reproducible.
- Negative: signing and attestation add CI time, key and identity
  management, and more release steps.
- Negative: enterprises must sign their own plugin images and configure
  the admission policy to trust their signer.
- Negative: a misconfigured policy can block legitimate deploys, so the
  failure must be clear and documented.
