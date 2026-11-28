"""Run the storage workflow's two commands with the reference data its setup produces."""

import importlib.util
import json
import os
import subprocess
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml

from iki_spec import schemas

PLUGIN = Path(__file__).resolve().parents[1]


def manifest(path: str) -> dict[str, Any]:
    document: dict[str, Any] = yaml.safe_load((PLUGIN / path).read_text(encoding="utf-8"))
    return document


@pytest.mark.parametrize(
    ("initial", "assigned", "label"),
    [
        (None, "-80", "-80 °C freezer"),
        (" LN2 ", " LN2 ", "Liquid nitrogen dewar"),
        ("4 °C fridge", "4 °C fridge", "4 °C fridge"),
        ("Shelf B", "Shelf B", "Shelf B"),
    ],
)
def test_store_sample(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, initial: str | None, assigned: str, label: str
) -> None:
    plugin = manifest("plugin.yaml")
    steps = manifest("workflows/store_sample.yaml")["steps"]
    assert steps["assign"]["inputs"] == {"sample": "$inputs.sample"}
    assert steps["decode"]["inputs"] == {"sample": "$steps.assign.inputs.sample"}

    reference = manifest("environments/reference.yaml")
    assert reference["setup"]["requires_approval"] is True
    volume = tmp_path / "storage-codes"
    volume.mkdir()
    output = tmp_path / "output.json"
    subprocess.run(  # noqa: S603 - run this plugin's setup script in a temporary volume
        [PLUGIN / reference["setup"]["script"]],
        cwd=PLUGIN,
        env={**os.environ, "IKI_VOLUME_STORAGE_CODES": str(volume), "IKI_OUTPUT": str(output)},
        check=True,
    )
    version = json.loads(output.read_text(encoding="utf-8"))["data_version"]
    assert version
    volumes = {
        "storage-codes": {
            "build": str(uuid4()),
            "hrid": "SETUP-000001",
            "version": 1,
            "data_version": version,
        }
    }
    given = tmp_path / "input.json"
    monkeypatch.setenv("IKI_INPUT", str(given))
    monkeypatch.setenv("IKI_OUTPUT", str(output))
    sample: dict[str, Any] = {
        "id": str(uuid4()),
        "hrid": f"{manifest('types/sample.yaml')['hrid']['prefix']}-000001",
        "type": f"{plugin['name']}.Sample",
        "types": [f"{plugin['name']}.Sample", "iki/core.Entity"],
        "schema_version": 1,
        "revision": 1,
        "values": {"name": "Frozen aliquot", "kind": "blood"},
    }
    if initial is not None:
        sample["values"]["storage"] = initial
    project = str(uuid4())

    # Apply each output to the very same sample. On replay both steps must leave it alone.
    for step, wanted in (
        ("assign", assigned),
        ("decode", label),
        ("assign", label),
        ("decode", label),
    ):
        action = manifest(f"actions/{steps[step]['action']}.yaml")
        environment = action["runs_on"]["environment"]
        run = {
            "apiVersion": "iki/v1",
            "kind": "RunInput",
            "run": {
                "id": str(uuid4()),
                "hrid": "RUN-000001",
                "attempt": 1,
                "action": f"{plugin['name']}.{action['name']}",
                "plugin": {"name": plugin["name"], "version": plugin["version"], "sha": None},
                "project": project,
            },
            "job": {"id": str(uuid4()), "timeout": 300, "resources": {}},
            "environment": f"{plugin['name']}.{environment}",
            "command": action["command"],
            "settings": {"default_storage": plugin["settings"]["default_storage"]["default"]},
            "params": {},
            "inputs": {"sample": [sample]},
            "files": {},
        }
        interpreter, script = action["command"]
        assert interpreter == "python"
        spec = importlib.util.spec_from_file_location(action["name"], PLUGIN / script)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if step == "decode":
            assert environment == reference["name"]
            mount = Path(reference["volumes"]["storage-codes"]["mount"])
            assert mount / "codes.json" == module.CODES
            monkeypatch.setattr(module, "CODES", volume / "codes.json")
            run["volumes"] = volumes

        assert schemas.check("run-input", run) == []
        given.write_text(json.dumps(run), encoding="utf-8")
        output.unlink(missing_ok=True)
        module.main()
        result = json.loads(output.read_text(encoding="utf-8"))
        assert schemas.check("run-output", result) == []
        updates = result["updates"]["sample"]
        if sample["values"].get("storage") == wanted:
            assert updates == []
        else:
            assert updates == [{"entity": sample["id"], "values": {"storage": wanted}}]
            sample["values"].update(updates[0]["values"])
            sample["revision"] += 1
