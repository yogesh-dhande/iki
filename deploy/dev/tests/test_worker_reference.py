"""The labels read from a mounted volume identify the exact setup build that filled it."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/plugins/worker_execution"


@pytest.mark.parametrize("build", ["SET-0001", "SET-0002"])
def test_read_setup_version(tmp_path: Path, build: str) -> None:
    volume = tmp_path / "storage-codes"
    volume.mkdir()
    source, output = tmp_path / "input.json", tmp_path / "output.json"
    env = {**os.environ, "IKI_INPUT": str(source), "IKI_OUTPUT": str(output)}
    source.write_text(json.dumps({"setup": {"hrid": build}}))
    subprocess.run(  # noqa: S603 - checked-in setup command on a temporary volume
        [FIXTURE / "setup/storage_codes.sh"],
        env={**env, "IKI_VOLUME_STORAGE_CODES": str(volume)},
        check=True,
    )
    assert json.loads(output.read_text()) == {"data_version": build}

    records = [{"id": code, "values": {"storage": code}} for code in ("-80", "ln2", "4")]
    source.write_text(json.dumps({"inputs": {"sample": records}}))
    subprocess.run(  # noqa: S603 - the fixture's action reads the volume just populated
        [sys.executable, FIXTURE / "scripts/decode.py", volume / "codes.json"], env=env, check=True
    )
    updates = json.loads(output.read_text())["updates"]["sample"]
    assert [(row["entity"], row["values"]["storage"]) for row in updates] == [
        ("-80", f"-80 °C freezer ({build})"),
        ("ln2", f"Liquid nitrogen dewar ({build})"),
        ("4", f"4 °C fridge ({build})"),
    ]
