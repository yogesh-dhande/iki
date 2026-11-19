#!/bin/sh
# Copies the reference data into the run's output directory, and keeps how its volume is
# mounted, as the container sees it: from /proc/mounts, which says read-only as `ro`.
set -eu

cp /data/reference/reference.txt "$IKI_OUT_DIR/reference.txt"
grep ' /data/reference ' /proc/mounts > "$IKI_OUT_DIR/mount.txt" \
    || echo "/data/reference isn't in /proc/mounts" > "$IKI_OUT_DIR/mount.txt"
echo "Read $(wc -c < /data/reference/reference.txt) bytes of reference data."
printf '{"apiVersion": "iki/v1", "kind": "RunOutput"}' > "$IKI_OUTPUT"
