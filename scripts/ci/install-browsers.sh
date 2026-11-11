#!/usr/bin/env bash
# Installs Playwright's browsers and the system packages they need, on Linux CI
# runners: scripts/ci/install-browsers.sh chromium firefox webkit
#
# The runner's Ubuntu mirror sometimes stalls, waiting on every package in turn. So
# each try is bounded, and after a stalled one apt moves to Ubuntu's own archive.
set -euo pipefail

if [ "$#" -eq 0 ]; then
  echo "usage: $0 BROWSER..." >&2
  exit 2
fi

# A dead connection fails in seconds and is retried, rather than waiting out apt's default.
printf '%s\n' 'Acquire::Retries "3";' 'Acquire::http::Timeout "20";' \
  'Acquire::https::Timeout "20";' | sudo tee /etc/apt/apt.conf.d/80-ci-network >/dev/null

for try in 1 2 3; do
  if timeout --kill-after=30s 6m uv run playwright install-deps "$@"; then
    break
  fi
  if [ "$try" -eq 3 ]; then
    echo "::error::Couldn't install the browsers' system packages in $try tries." >&2
    exit 1
  fi
  echo "::warning::Installing system packages failed or stalled (try $try): moving to archive.ubuntu.com."
  # A try cut short can leave dpkg half done.
  sudo dpkg --configure -a || true
  grep -rlZ 'azure\.archive\.ubuntu\.com' /etc/apt 2>/dev/null |
    xargs -0 -r sudo sed -i 's/azure\.archive\.ubuntu\.com/archive.ubuntu.com/g'
  sudo apt-get update -qq || true
done

# The browsers themselves come from Playwright's CDN, which retries by itself.
uv run playwright install "$@"
