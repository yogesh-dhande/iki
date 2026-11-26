#!/bin/sh
set -eu
python - <<'PYTHON'
import json
import os
from pathlib import Path

build = json.loads(Path(os.environ["IKI_INPUT"]).read_text())["setup"]["hrid"]
locations = {"-80": "-80 °C freezer", "ln2": "Liquid nitrogen dewar", "4": "4 °C fridge"}
codes = {code: f"{label} ({build})" for code, label in locations.items()}
table = Path(os.environ["IKI_VOLUME_STORAGE_CODES"]) / "codes.json"
table.write_text(json.dumps(codes), encoding="utf-8")
print(f"Wrote {table.stat().st_size} bytes of storage codes for {build}.")
Path(os.environ["IKI_OUTPUT"]).write_text(json.dumps({"data_version": build}))
PYTHON
