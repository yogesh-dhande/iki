#!/usr/bin/env bash
# Give each gate its own run and each execution (including a VM retry) its own
# attempt. The VM client only fetches exact paths, so choose its run ID locally.
set -euo pipefail

base=build/browser-failures
new_run() {
  local directory
  mkdir -p "$base"
  directory=$(mktemp -d "$base/run-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXXXX")
  echo "${directory##*/}"
}

if [[ ${1:-} == --vm && $# -ge 3 ]]; then
  runner=$2
  shift 2
  run=$(new_run)
  archive="$base/$run/traces.tar.gz"
  marker="$base/$run/started-attempt.txt"
  echo "Submitting browser trace run $run" >&2
  status=0
  VM_RUN_FETCH="${VM_RUN_FETCH:+$VM_RUN_FETCH }$marker $archive" \
    "$runner" scripts/ci/with-browser-traces.sh --remote "$run" "$@" || status=$?
  if [[ ! -f "$archive" ]]; then
    echo "No result archive returned for $run" >&2
    rm -f "$marker"
    rmdir "$base/$run"
    if (( status == 0 )); then exit 125; fi
    exit "$status"
  fi
  # A receipt also arrives for a run that produced no traces. Its terminal
  # attempt can be green even if the VM replayed an earlier, failing attempt.
  if ! gzip -t "$archive" || ! receipt=$(tar -xOzf "$archive" collected-attempt.txt); then
    echo "Unconfirmed trace delivery for $run. Kept the unreadable archive or receipt at $archive." >&2
    if (( status == 0 )); then exit 125; fi
    exit "$status"
  fi
  echo "$receipt" >&2
  if [[ ! -f $marker ]] || ! grep -Fxq "attempt=$(cat "$marker")" <<< "$receipt"; then
    echo "The latest attempt of $run has no confirmed collection. The archive identifies its last collected attempt." >&2
    if (( status == 0 )); then status=125; fi
  fi
  if grep -Fxq 'retained_traces=0' <<< "$receipt"; then
    rm -f "$archive" "$marker"
    rmdir "$base/$run"
  else
    echo "Fetched browser failure traces in $archive" >&2
  fi
  exit "$status"
fi

remote=false
if [[ ${1:-} == --remote && $# -ge 3 ]]; then
  run=$2
  [[ $run =~ ^run-[[:alnum:]-]+$ ]] || { echo "Invalid browser trace run ID" >&2; exit 2; }
  remote=true
  export IKI_GATE=1
  shift 2
elif [[ $# -gt 0 && $1 != --vm && $1 != --remote ]]; then
  run=$(new_run)
else
  echo "usage: $0 [--vm VM_RUNNER] COMMAND [ARG ...]" >&2
  exit 2
fi

directory="$base/$run"
attempts="$directory/.attempts"
mkdir -p "$attempts"
export IKI_BROWSER_ARTIFACTS
IKI_BROWSER_ARTIFACTS=$(mktemp -d "$PWD/$attempts/attempt-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXXXX")
attempt=${IKI_BROWSER_ARTIFACTS##*/}
echo "Browser trace run $run, attempt $attempt" >&2
if [[ $remote == true ]]; then printf '%s\n' "$attempt" > "$directory/started-attempt.txt"; fi
status=0
"$@" || status=$?

if [[ -n $(find "$IKI_BROWSER_ARTIFACTS" -type f -name '*.zip' -print -quit) ]]; then
  current="recorded browser failure traces"
else
  current="recorded no browser failure traces"
  rmdir "$IKI_BROWSER_ARTIFACTS" 2>/dev/null || true
fi
result="Run $run, attempt $attempt exited $status and $current."
echo "$result" >&2
count=$(find "$attempts" -type f -name '*.zip' | wc -l | tr -d ' ')
if [[ $remote == false && $count == 0 ]]; then
  rm -rf "$directory"
  exit "$status"
fi

# The retrying VM needs one known transport path. Rebuild it from every retained
# attempt in this run, so an internal retry can only add to earlier evidence.
umask 077
work=$(mktemp -d "$directory/.pack-XXXXXXXX")
trap 'rm -rf "$work"' EXIT
printf '%s\n' "$result" "attempt=$attempt" "retained_traces=$count" > "$work/collected-attempt.txt"
# macOS tar otherwise adds metadata files even to an empty directory.
COPYFILE_DISABLE=1 tar -czf "$work/traces.tar.gz" -C "$work" collected-attempt.txt -C "$PWD/$attempts" .
mv "$work/traces.tar.gz" "$directory/traces.tar.gz"
if [[ $remote == false ]]; then
  rm -rf "$attempts"
fi
echo "Saved browser trace archive to $directory/traces.tar.gz" >&2
exit "$status"
