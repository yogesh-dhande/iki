# Antibody structure tools image

The ABodyBuilder2 and NanoBodyBuilder2 weights come from the ImmuneBuilder deposit
on Zenodo, version 0.0.1. The deposit's CC BY 4.0 licence permits redistribution
with attribution. The image includes the [attribution notice](WEIGHTS-NOTICE.txt).
[Zenodo deposit](https://doi.org/10.5281/zenodo.7258553) · [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)

The `immunebuilder-weights` stage in the [Dockerfile](Dockerfile) uses a published
GHCR image index pinned by digest. Its SLSA attestation links the digest to the
original Dockerfile that fetched the eight Zenodo files with SHA-256 checksums.
The final build copies only those files and compares their names and hashes with
[SHA256SUMS](SHA256SUMS) with networking disabled. Both target architectures
use the same file checksums. [Original checksummed build](https://github.com/yogesh-dhande/iki/commit/74527b96efa6971c60e791a672acb798e45c68d8)

Keep the source image digest in GHCR while any Dockerfile references it. A cold
builder may pull the source image's runtime layers to unpack the files. The
[structure-tools environment](../../environments/structure-tools.yaml) pins the
separate image used to execute actions.
