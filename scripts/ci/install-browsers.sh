#!/usr/bin/env bash
# Installs Playwright's browsers and the system packages they need, on Linux CI
# runners: scripts/ci/install-browsers.sh chromium firefox webkit
#
# With CI_APT_CACHE set to a directory the workflow caches, the packages downloaded
# once are kept there and installed from it next time, so a run downloads only what
# changed, and a slow mirror costs a run little.
#
# The runner's Ubuntu mirror sometimes stalls, waiting on every package in turn, or
# can't be resolved at all. So each try is bounded, and after a failed one apt moves
# to the next mirror. Any mirror is safe: apt checks every package against Ubuntu's
# signed release files, so a mirror can't change what's installed.
set -euo pipefail

if [ "$#" -eq 0 ]; then
  echo "usage: $0 BROWSER..." >&2
  exit 2
fi

# The runner's own mirror first, then Ubuntu's, then another with the same layout.
mirrors=(azure.archive.ubuntu.com archive.ubuntu.com mirrors.edge.kernel.org)

# A dead connection fails in seconds and is retried, rather than waiting out apt's
# default. And apt keeps what it downloads, for the cache.
printf '%s\n' 'Acquire::Retries "2";' 'Acquire::http::Timeout "15";' \
  'Acquire::https::Timeout "15";' 'APT::Keep-Downloaded-Packages "true";' \
  'Binary::apt::APT::Keep-Downloaded-Packages "true";' |
  sudo tee /etc/apt/apt.conf.d/80-ci-network >/dev/null

archives=/var/cache/apt/archives
cache="${CI_APT_CACHE:-}"
cache="${cache/#\~/$HOME}" # as the workflow writes it
if [ -n "$cache" ] && compgen -G "$cache/*.deb" >/dev/null; then
  echo "Reusing $(find "$cache" -name '*.deb' | wc -l) cached packages."
  sudo cp "$cache"/*.deb "$archives/"
fi

# After a try is cut short: its apt-get runs as root, so `timeout` can't stop it.
stop_apt() {
  sudo pkill -TERM -x apt-get || true
  for _ in $(seq 30); do
    pgrep -x 'apt-get|dpkg' >/dev/null || break
    sleep 1
  done
  sudo pkill -KILL -x apt-get || true
  sudo pkill -KILL -x dpkg || true
  sudo dpkg --configure -a || true
}

use_mirror() { # use_mirror <from-host> <to-host>
  grep -rlZ --fixed-strings "$1" /etc/apt 2>/dev/null |
    xargs -0 -r sudo sed -i "s/$(printf '%s' "$1" | sed 's/\./\\./g')/$2/g"
}

for try in "${!mirrors[@]}"; do
  if timeout --kill-after=30s 8m uv run playwright install-deps "$@"; then
    break
  fi
  stop_apt
  next=$((try + 1))
  if [ "$next" -ge "${#mirrors[@]}" ]; then
    echo "::error::Couldn't install the browsers' system packages from any mirror." >&2
    exit 1
  fi
  echo "::warning::Installing system packages from ${mirrors[$try]} failed or stalled: trying ${mirrors[$next]}."
  use_mirror "${mirrors[$try]}" "${mirrors[$next]}"
done

if [ -n "$cache" ]; then
  mkdir -p "$cache"
  find "$archives" -maxdepth 1 -name '*.deb' -exec cp -t "$cache" {} +
fi

# The browsers themselves come from Playwright's CDN, which retries by itself.
uv run playwright install "$@"
