#!/usr/bin/env bash
# The tools image copies the ImmuneBuilder weights from an immutable digest. Check that the
# digest is one the weights publisher released: it tags each one it publishes
# `sha256-<digest>` and keeps it, so a rebuild of any revision finds the weights it pinned.
#
#   check-immunebuilder-weights.sh            the pin is a retained release (any build)
#   check-immunebuilder-weights.sh --latest   and the last one published (development)
#
# Release rebuilds use the first: an older revision keeps the weights it was reviewed with
# after a newer release moves `published`. CI on the development branch uses the second,
# so a new release isn't left unadopted.
set -euo pipefail
cd -- "$(dirname -- "$0")/../.."
latest=no
case "${1:-}" in
  "") ;;
  --latest) latest=yes ;;
  *) printf 'Usage: %s [--latest]\n' "$0" >&2; exit 2 ;;
esac
image=ghcr.io/yogesh-dhande/iki-immunebuilder-weights
dockerfile=${IKI_WEIGHTS_DOCKERFILE:-plugins/antibody_3d/images/structure-tools/Dockerfile}
pinned=$(awk -v prefix="--from=$image@" '
  $1 == "COPY" && index($2, prefix) == 1 { print substr($2, length(prefix) + 1) }
' "$dockerfile")
if [[ ! "$pinned" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  printf 'Expected exactly one digest-pinned COPY from %s in %s\n' "$image" "$dockerfile" >&2
  exit 1
fi
# The manifest a reference names, by its digest.
digest_of() {
  local raw
  raw=$(docker buildx imagetools inspect --raw "$1") || return 1
  local hash
  hash=$(printf '%s' "$raw" | sha256sum)
  printf 'sha256:%s\n' "${hash%% *}"
}
retained=$(digest_of "$image:sha256-${pinned#sha256:}") || retained=""
if [[ "$retained" != "$pinned" ]]; then
  printf 'ImmuneBuilder weights %s are no release the publisher retained (%s:sha256-%s)\n' \
    "$pinned" "$image" "${pinned#sha256:}" >&2
  exit 1
fi
if [[ $latest == yes ]]; then
  published=$(digest_of "$image:published")
  if [[ "$pinned" != "$published" ]]; then
    printf 'ImmuneBuilder weight digest mismatch\n  Dockerfile: %s\n  Last published: %s\n' \
      "$pinned" "$published" >&2
    exit 1
  fi
  printf 'ImmuneBuilder weights pinned to the last published release %s\n' "$pinned"
else
  printf 'ImmuneBuilder weights pinned to the retained release %s\n' "$pinned"
fi
