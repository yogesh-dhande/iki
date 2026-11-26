#!/usr/bin/env bash
# The image build uses an immutable digest. Compare it to the publisher's release tag.
set -euo pipefail
cd -- "$(dirname -- "$0")/../.."
image=ghcr.io/yogesh-dhande/iki-immunebuilder-weights
dockerfile=plugins/antibody_3d/images/structure-tools/Dockerfile
pinned=$(awk -v prefix="--from=$image@" '
  $1 == "COPY" && index($2, prefix) == 1 { print substr($2, length(prefix) + 1) }
' "$dockerfile")
if [[ ! "$pinned" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  printf 'Expected exactly one digest-pinned COPY from %s in %s\n' "$image" "$dockerfile" >&2
  exit 1
fi
manifest_hash=$(docker buildx imagetools inspect --raw "$image:published" | sha256sum)
published="sha256:${manifest_hash%% *}"
if [[ "$pinned" != "$published" ]]; then
  printf 'ImmuneBuilder weight digest mismatch\n  Dockerfile: %s\n  Last published: %s\n' "$pinned" "$published" >&2
  exit 1
fi
printf 'ImmuneBuilder weights pinned to the last published digest %s\n' "$pinned"
