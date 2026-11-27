# Antibody structure tools image

The ABodyBuilder2 and NanoBodyBuilder2 weights come from the ImmuneBuilder deposit
on Zenodo, version 0.0.1. The deposit's CC BY 4.0 licence permits redistribution
with attribution. The image includes the [attribution notice](WEIGHTS-NOTICE.txt).
[Zenodo deposit](https://doi.org/10.5281/zenodo.7258553) · [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)

The [weights publisher](../../../../.github/workflows/publish-immunebuilder-weights.yml)
downloads the eight Zenodo files with retries and verifies [SHA256SUMS](SHA256SUMS).
It publishes a data-only scratch image, `iki-immunebuilder-weights`, with the
weights and attribution. The workflow verifies the SLSA attestation before
tagging the image with `sha256-<digest>` and moving the `published` tag.

The [tools Dockerfile](Dockerfile) uses the weights image by immutable digest
and rechecks every file offline. Every build checks that the registry still serves
this digest, which the tags below keep, so an older revision rebuilds with the
weights it pinned. CI on the development branch also checks that it is the
`published` one: when dispatching a new weights release, update the Dockerfile pin
to the workflow's published digest.

Keep `iki-immunebuilder-weights` outside the retention policy for runtime images.
The digest tags and the bootstrap release's source-commit tag protect the
weight images from untagged-version cleanup. Previous versions are build inputs
for revisions pinned to their digests.
