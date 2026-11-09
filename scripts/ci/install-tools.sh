#!/usr/bin/env bash
# Installs pinned, checksum-verified CLI tools into /usr/local/bin on Linux CI
# runners (amd64 or arm64). Usage: scripts/ci/install-tools.sh tilt grype ...
# Bump a tool by updating its version and both checksums together.
set -euo pipefail

dest=/usr/local/bin
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

case "$(uname -m)" in
  x86_64) arch=amd64 ;;
  aarch64 | arm64) arch=arm64 ;;
  *) echo "unsupported architecture: $(uname -m)" >&2; exit 2 ;;
esac

# pick <amd64-sha256> <arm64-sha256>
pick() { if [ "$arch" = amd64 ]; then echo "$1"; else echo "$2"; fi; }

fetch() { # fetch <url> <sha256> <file>
  curl -fsSL --retry 3 -o "$tmp/$3" "$1"
  echo "$2  $tmp/$3" | sha256sum --check --quiet
}

install_tool() {
  local v
  case "$1" in
    tilt)
      v=0.37.8
      local tilt_arch; tilt_arch=$(pick x86_64 arm64)
      fetch "https://github.com/tilt-dev/tilt/releases/download/v$v/tilt.$v.linux.$tilt_arch.tar.gz" \
        "$(pick e39513f958705d30524d55f68b1d46fbef2b46a0da568efd00e6383dee6be44f \
                7a21281e830930be158202b7f8405ba62445613788b4d9708512ffe51f5cce90)" tilt.tgz
      sudo tar -xzf "$tmp/tilt.tgz" -C "$dest" tilt ;;
    osv-scanner)
      v=2.7.0
      fetch "https://github.com/google/osv-scanner/releases/download/v$v/osv-scanner_linux_$arch" \
        "$(pick 160c6253e2922e9e0a6a9771c5c4c40a4b3c6090b34a3cdd5e80c96cc2e4218b \
                4061ef4c021ed3c47f203b818f337e351336cc5cd67c0274fc36bd1dfc728dd7)" osv-scanner
      sudo install -m 0755 "$tmp/osv-scanner" "$dest/osv-scanner" ;;
    cargo-deny)
      v=0.20.2
      local triple; triple="$(pick x86_64 aarch64)-unknown-linux-musl"
      fetch "https://github.com/EmbarkStudios/cargo-deny/releases/download/$v/cargo-deny-$v-$triple.tar.gz" \
        "$(pick 9f12ed4c49936e09b48bf862b595cde2fe64fcbd9d74dfacac6131ca824c8d5f \
                995c82be0defc7a025cae49a2aa2644ce8245c9a3318fc4103907c6a285e8c7d)" cargo-deny.tgz
      tar -xzf "$tmp/cargo-deny.tgz" -C "$tmp"
      sudo install -m 0755 "$tmp/cargo-deny-$v-$triple/cargo-deny" "$dest/cargo-deny" ;;
    grype)
      v=0.121.2
      fetch "https://github.com/anchore/grype/releases/download/v$v/grype_${v}_linux_$arch.tar.gz" \
        "$(pick 9c8965ced9df657e5e851c14021bae838802f711edd7a48f9bc3c923a8d28f12 \
                d7d2e160255b0f087280feabf6f213cf4c45d475690f3f1fe5dc336eb1037bf0)" grype.tgz
      sudo tar -xzf "$tmp/grype.tgz" -C "$dest" grype ;;
    syft)
      v=1.57.0
      fetch "https://github.com/anchore/syft/releases/download/v$v/syft_${v}_linux_$arch.tar.gz" \
        "$(pick 9c077a7d43029b3b13e98a8db720661e700c4d58db6352153a06395ae4a2a704 \
                410cbfa0ae0f63246bc0057843760b1de2833cd23919e025e368fd7681308cdb)" syft.tgz
      sudo tar -xzf "$tmp/syft.tgz" -C "$dest" syft ;;
    *)
      echo "unknown tool: $1" >&2
      exit 2 ;;
  esac
  echo "installed $1 $v ($arch)"
}

for tool in "$@"; do install_tool "$tool"; done
