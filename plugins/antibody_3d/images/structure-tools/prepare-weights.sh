#!/usr/bin/env bash
# Assemble a data-only build context for the manually dispatched publisher.
set -euo pipefail
here=$(cd -- "$(dirname -- "$0")" && pwd)
context=${1:?usage: prepare-weights.sh NEW_CONTEXT_DIRECTORY}
source_url=https://zenodo.org/records/7258553/files
files=(antibody_model_{1..4} nanobody_model_{1..4})

# Validate the manifest's file list before using any names in download URLs.
diff -u <(printf '%s\n' "${files[@]}") <(awk '{print $2}' "$here/SHA256SUMS")
mkdir -- "$context"
for file in "${files[@]}"; do
  printf 'Downloading %s\n' "$file"
  curl --fail --silent --show-error --location \
    --retry 5 --retry-all-errors --retry-max-time 360 \
    --connect-timeout 15 --max-time 180 \
    --output "$context/$file" "$source_url/$file"
done
(
  cd -- "$context"
  sha256sum --check --strict "$here/SHA256SUMS"
)
cp -- "$here/weights.Dockerfile" "$context/Dockerfile"
cp -- "$here/WEIGHTS-NOTICE.txt" "$context/ATTRIBUTION"
TZ=UTC touch -t 197001010000 "$context"/*
