#!/bin/sh
# Fills the reference environment's storage-codes volume, mounted writable at
# $IKI_VOLUME_STORAGE_CODES. Like an action's command, it reports progress with
# `::progress` lines; it says what the data is in $IKI_OUTPUT, which runs record.
set -eu

echo "::progress 0.2 Writing the lab's storage codes"
cat > "$IKI_VOLUME_STORAGE_CODES/codes.json" <<'CODES'
{
  "-80": "-80 °C freezer",
  "-20": "-20 °C freezer",
  "4": "4 °C fridge",
  "ln2": "Liquid nitrogen dewar",
  "rt": "Room temperature shelf"
}
CODES
echo "Wrote $(wc -c < "$IKI_VOLUME_STORAGE_CODES/codes.json") bytes of storage codes."
echo "::progress 1 Done"
printf '{"data_version": "Storage codes 2026-11"}' > "$IKI_OUTPUT"
