"""``iki-worker run --local``: an action's command run from a fixture, with no server.

The binary is built as for the end-to-end tests (``test_worker_e2e``).
"""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from test_worker_e2e import (
    PLUGINS,
    worker_bin,  # noqa: F401 - a fixture
)

pytestmark = pytest.mark.worker_e2e

PDB = b"ATOM      1  N   MET A   1      27.340  24.430   2.614  1.00  9.67           N\n"


def run_input(command: list[str], **extra: Any) -> dict[str, Any]:
    return {
        "apiVersion": "iki/v1",
        "kind": "RunInput",
        "run": {
            "id": "00000000-0000-7000-8000-000000000001",
            "hrid": "RUN-000001",
            "attempt": 1,
            "action": "protein.relax_structure",
            "plugin": {"name": "protein", "version": "1.0.0", "sha": "dev"},
            "project": "00000000-0000-7000-8000-000000000002",
        },
        "params": {},
        "inputs": {},
        "command": command,
        "environment": "protein.protein-tools",
        **extra,
    }


def run_local(
    binary: Path, tmp_path: Path, given: dict[str, Any], *args: str, plugin: Path
) -> subprocess.CompletedProcess[str]:
    (tmp_path / "input.json").write_text(json.dumps(given))
    env = {
        "PATH": os.environ["PATH"],
        "IKI_WORK_DIR": str(tmp_path / "work"),
        "IKI_REGISTRATION_TOKEN": "iki_wreg_must-not-reach-the-command",
    }
    return subprocess.run(  # noqa: S603 - the binary under test
        [str(binary), "run", "--local", "input.json", "--plugin", str(plugin), *args],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def plugin_with(tmp_path: Path, script: str) -> Path:
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / "main.py").write_text(script)
    return plugin


def test_a_fixture_runs_with_its_files_and_its_result_files_are_described(
    worker_bin: Path,  # noqa: F811 - the fixture
    tmp_path: Path,
) -> None:
    (tmp_path / "fixtures").mkdir()
    (tmp_path / "fixtures" / "abl.pdb").write_bytes(PDB)
    structure = {
        "id": "s1",
        "hrid": "PST-000001",
        "type": "protein.ProteinStructure",
        "schema_version": 1,
        "revision": 1,
        "values": {"structure": "f1", "method": "xray", "chains": ["c1"]},
    }
    given = run_input(
        [sys.executable, "./scripts/relax.py"],
        inputs={"structure": [structure]},
        # Only the path: the rest comes from the file.
        files={"f1": {"path": "fixtures/abl.pdb"}},
    )
    ran = run_local(worker_bin, tmp_path, given, plugin=PLUGINS / "protein")
    assert ran.returncode == 0, ran.stderr
    assert f"Read abl.pdb: {len(PDB)} bytes" in ran.stderr
    output = json.loads(ran.stdout)
    made = output["creates"][0]["values"]["structure"]
    relaxed = b"REMARK   1 RELAXED BY IKI EXAMPLE\n" + PDB
    assert made["$file"] == "relaxed.pdb"
    assert (made["name"], made["media_type"]) == ("relaxed.pdb", "chemical/x-pdb")
    assert (made["size"], made["sha256"]) == (len(relaxed), hashlib.sha256(relaxed).hexdigest())
    assert Path(made["path"]).read_bytes() == relaxed

    # A fixture that no longer matches its file says so.
    given["files"]["f1"]["sha256"] = "0" * 64
    ran = run_local(worker_bin, tmp_path, given, plugin=PLUGINS / "protein")
    assert ran.returncode == 1
    assert "abl.pdb's SHA-256 is" in ran.stderr
    assert ran.stdout == ""


def test_results_must_be_files_the_command_made(
    worker_bin: Path,  # noqa: F811 - the fixture
    tmp_path: Path,
) -> None:
    plugin = plugin_with(
        tmp_path,
        "import json, os\n"
        "json.dump({'apiVersion': 'iki/v1', 'kind': 'RunOutput', 'creates': [{'type': 't',"
        " 'values': {'structure': {'$file': '../input.json'}}}]},"
        " open(os.environ['IKI_OUTPUT'], 'w'))\n",
    )
    ran = run_local(worker_bin, tmp_path, run_input([sys.executable, "main.py"]), plugin=plugin)
    assert ran.returncode == 1
    assert "isn't a file in IKI_OUT_DIR" in ran.stderr


def test_the_command_sees_only_its_own_environment_and_is_stopped_at_its_timeout(
    worker_bin: Path,  # noqa: F811 - the fixture
    tmp_path: Path,
) -> None:
    plugin = plugin_with(
        tmp_path,
        "import json, os, sys, time\n"
        "print(json.dumps(sorted(os.environ)), flush=True)\n"
        "if sys.argv[1:] == ['--hang']:\n"
        "    time.sleep(60)\n"
        "json.dump({'apiVersion': 'iki/v1', 'kind': 'RunOutput'},"
        " open(os.environ['IKI_OUTPUT'], 'w'))\n",
    )
    ran = run_local(worker_bin, tmp_path, run_input([sys.executable, "main.py"]), plugin=plugin)
    assert ran.returncode == 0, ran.stderr
    assert json.loads(ran.stdout) == {"apiVersion": "iki/v1", "kind": "RunOutput"}
    seen = json.loads(next(line for line in ran.stderr.splitlines() if line.startswith("[")))
    assert "IKI_REGISTRATION_TOKEN" not in seen
    assert {"IKI_INPUT", "IKI_OUTPUT", "IKI_OUT_DIR", "HOME", "TMPDIR"} <= set(seen)

    hang = run_input([sys.executable, "main.py", "--hang"])
    ran = run_local(worker_bin, tmp_path, hang, "--timeout", "1", plugin=plugin)
    assert ran.returncode == 1
    assert "ran past its timeout of 1 second" in ran.stderr


def test_what_cant_be_run_is_a_usage_error(
    worker_bin: Path,  # noqa: F811 - the fixture
    tmp_path: Path,
) -> None:
    ran = run_local(worker_bin, tmp_path, {"kind": "RunInput"}, plugin=tmp_path)
    assert ran.returncode == 2
    assert "isn't a run input (run-input/v1)" in ran.stderr
