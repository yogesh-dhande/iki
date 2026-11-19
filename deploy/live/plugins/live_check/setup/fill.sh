#!/bin/sh
# Fills the machine environment's reference volume, mounted writable at
# $IKI_VOLUME_REFERENCE, with a line no other build writes, and says it in $IKI_OUTPUT:
# a run that reads the line back read this build's version.
set -eu

token=$(cat /proc/sys/kernel/random/uuid)
echo "::progress 0.5 Writing the reference data"
printf 'Iki live check reference data %s\n' "$token" > "$IKI_VOLUME_REFERENCE/reference.txt"
echo "Wrote the reference data, $token."
echo "::progress 1 Done"
printf '{"data_version": "Live check %s"}' "$token" > "$IKI_OUTPUT"
