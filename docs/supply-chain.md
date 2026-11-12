# Supply-chain security

Everything Iki publishes is signed, carries a record of how it was built, and
lists what's inside it. You can check all of this yourself before deploying.
The policy is set in [ADR 0017](adr/0017-supply-chain-security.md).

## What is published

| Artifact                    | Where                                            | Built by      |
| --------------------------- | ------------------------------------------------ | ------------- |
| Server image (amd64, arm64) | `ghcr.io/yogesh-dhande/iki-server`               | `publish.yml` |
| Worker image (amd64, arm64) | `ghcr.io/yogesh-dhande/iki-worker`               | `publish.yml` |
| Helm chart                  | `oci://ghcr.io/yogesh-dhande/charts/iki`         | `release.yml` |
| `iki-worker` binaries       | GitHub release assets (Linux musl static, macOS) | `release.yml` |

They live under `yogesh-dhande`, the GitHub account that hosts the repository,
until it moves to the `ikisci` organization. `docs/tests/test_release_owner.py`
lists every place that names it, so the move is one change.

The worker image holds the worker and micromamba but no plugins; each
deployment builds its own worker image on top of it with its plugins' code and
environments (`deploy/worker.Dockerfile`).

The `main` branch publishes each image as `:main` and `:sha-<commit>`. A
version tag `vX.Y.Z` publishes the images as `:X.Y.Z` and `:X.Y`, the chart
`X.Y.Z`, and a GitHub release.

Every artifact comes with:

- **A cosign signature**, made with Sigstore's keyless flow. There is no
  long-lived signing key; the signing certificate is bound to the GitHub
  Actions workflow that built the artifact, and the signature is recorded in
  Sigstore's public transparency log (Rekor).
- **SLSA build provenance**: a signed statement of which repository, commit
  and workflow built it, stored as a GitHub artifact attestation (and, for
  images and charts, in the registry next to the artifact).
- **A CycloneDX SBOM** (software bill of materials) attestation listing its
  dependencies. Images and binaries only.

Publishing fails if any image has a high or critical vulnerability that has a
fix available. Each workflow verifies its own signatures and attestations
before it finishes.

## Verifying

You need [cosign](https://docs.sigstore.dev/cosign/system_config/installation/)
3.x and the [GitHub CLI](https://cli.github.com/).

### Server and worker images

The worker image verifies the same way: set `IMAGE=ghcr.io/$OWNER/iki-worker`.
Always deploy images by digest. Resolve a tag once, verify it, then pin the
digest in your Helm values (`image.digest`).

```console
OWNER=yogesh-dhande
IMAGE=ghcr.io/$OWNER/iki-server
DIGEST=$(docker buildx imagetools inspect $IMAGE:main --format '{{json .Manifest.Digest}}' | tr -d '"')

# Signature: made by Iki's publish workflow on GitHub Actions.
cosign verify $IMAGE@$DIGEST \
  --certificate-identity-regexp "^https://github.com/$OWNER/iki/.github/workflows/publish.yml@refs/(heads/main|tags/v.*)$" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com

# Build provenance and SBOM.
gh attestation verify oci://$IMAGE@$DIGEST --repo $OWNER/iki --predicate-type https://slsa.dev/provenance/v1
gh attestation verify oci://$IMAGE@$DIGEST --repo $OWNER/iki --predicate-type https://cyclonedx.org/bom \
  --format json | jq '.[0].verificationResult.statement.predicate' > sbom.cdx.json
```

The SBOM is attached to each platform image (amd64, arm64), so when
verifying the SBOM, use the platform digest
(`docker buildx imagetools inspect $IMAGE@$DIGEST` lists them).

### Worker binaries

Each release asset `iki-worker-<version>-<target>.tar.gz` has a signature
bundle `<asset>.sigstore.json` next to it.

```console
OWNER=yogesh-dhande
FILE=iki-worker-1.0.0-x86_64-unknown-linux-musl.tar.gz
cosign verify-blob $FILE --bundle $FILE.sigstore.json \
  --certificate-identity-regexp "^https://github.com/$OWNER/iki/.github/workflows/release.yml@refs/tags/v.*$" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
gh attestation verify $FILE --repo $OWNER/iki
```

`iki-worker verify $FILE $FILE.sigstore.json --version 1.0.0` makes the same
signature check offline, with no cosign needed. Workers on people's computers
make it before updating themselves to a release
([ADR 0060](adr/0060-workers-update-themselves-to-signed-releases.md)).

### Helm chart

```console
OWNER=yogesh-dhande
cosign verify ghcr.io/$OWNER/charts/iki:1.0.0 \
  --certificate-identity-regexp "^https://github.com/$OWNER/iki/.github/workflows/release.yml@refs/tags/v.*$" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

## Dependency policy (CI)

| Check                   | Tool                                                       | Fails on                                                      |
| ----------------------- | ---------------------------------------------------------- | ------------------------------------------------------------- |
| Known vulnerabilities   | OSV-Scanner over `uv.lock`, `pnpm-lock.yaml`, `Cargo.lock` | any advisory without a documented, time-limited exception     |
| Licenses                | OSV-Scanner + cargo-deny                                   | a license outside `scripts/supply-chain/allowed-licenses.txt` |
| Rust crate sources      | cargo-deny                                                 | git dependencies or registries other than crates.io           |
| New dependencies in PRs | GitHub dependency review                                   | moderate+ advisories or disallowed licenses                   |
| Container image         | Grype (`.grype.yaml`)                                      | high/critical vulnerabilities with a fix available            |

Exceptions live in `osv-scanner.toml` (vulnerabilities expire on a set date)
and must explain why they are safe. Today there are two:

- psycopg, the PostgreSQL driver, is LGPL-3.0. There is no non-LGPL driver
  for Django, and Iki uses it unmodified as a dynamically imported library.
- A denial-of-service advisory in minimatch, reachable only through ESLint's
  handling of our own lint config. Expires 2027-01-15.

CI tools are installed by `scripts/ci/install-tools.sh` at pinned versions with
SHA-256 checksums. GitHub Actions are pinned by commit SHA.

Run the same checks locally with `make check-deps` and `make check-image`
(`brew install osv-scanner cargo-deny grype syft`).
