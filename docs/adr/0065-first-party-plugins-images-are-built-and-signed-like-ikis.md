# 0065. First-party plugins' images are built and signed like Iki's

- Status: Accepted
- Date: 2026-11-12
- Refines: [0017](0017-supply-chain-security.md),
  [0063](0063-the-cluster-admits-only-images-iki-signed-or-the-deployment-approved.md)

## Context

Some of the first-party plugins' actions need tools that a Python
environment can't install: the protein plugin's secondary structure needs
DSSP (`mkdssp`), a C++ program, and the Chemical Component Dictionary it
reads residues from. Those actions run on the container tier, from an image
the plugin pins by digest (ADR 0061).

Third-party plugins bring their own images, and each deployment approves
them (ADR 0063). Iki's own plugins could do the same with an image from
somewhere else. But Iki already builds, scans, signs and attests its images,
and an auditor reviewing a deployment wants first-party software to come
from Iki's pipeline, under one identity.

## Decision

**A first-party plugin's image lives in the plugin, built by Iki's
workflows.** Each image is a Dockerfile in `plugins/<plugin>/images/<name>/`,
built with the plugin's directory as its context, so it can install the same
pinned and hashed `requirements.txt` as the plugin's backend environment. It
has a `check.py` that shows its tools work.

- CI builds each one on every push, runs its check offline as the image's
  non-root user, and scans it with Grype. CI fails on fixable high or
  critical vulnerabilities, as for Iki's images.
- `publish.yml` builds it for amd64 and arm64 on `main` and on tags, and
  publishes it as `ghcr.io/<owner>/iki-<plugin>-<name>`. It is signed with
  cosign and attested with SLSA provenance and SBOMs, like `iki-server`.
- The plugin's environment pins the published multi-arch digest. Pinning
  it is a separate, later change, because the image must be published
  first. Changing an image takes two reviewed commits: one to the
  Dockerfile, then one to the digest. What ran is always a digest that
  Iki's workflow signed.

**The cluster admits them as Iki's.** Iki's publish workflow signed them, so
the policy from ADR 0063 admits them without a deployment approving each
one. A deployment that lists them in its approvals signs them a second
time, which does no harm.

**Data a tool can't start without goes in its image, pinned.** The protein
image carries the wwPDB's Chemical Component Dictionary (CC0 1.0, 490 MB),
taken from the PDBj annual snapshot by its checksum. A rebuild then gives the
same answers until a reviewed change moves the snapshot. Large reference
data that is updated separately from a tool goes on shared volumes, built
once per deployment: model weights, sequence and structure databases.

## Consequences

- The structure-tools image is about 1 GB, most of it the dictionary.
  Nodes pull it once and keep it.
- Residues defined after the snapshot are unknown to DSSP. The action
  names any it couldn't assign. A yearly change moves the snapshot forward.
- Builds install Debian's security updates (`apt-get upgrade`), so two
  builds of one Dockerfile can differ. The digest the plugin pins says
  exactly what ran.
- Third-party plugins keep publishing their own images, and deployments
  keep approving them (ADR 0063).
