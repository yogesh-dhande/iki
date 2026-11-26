"""The example hosted-models plugin: its manifests, and its script run as a worker runs it,
calling a stand-in FoldCloud over HTTPS through a proxy, trusting only the CA it's given.

What the egress proxy adds (the API's key, each call's record, polling the parked job) is
tested in test_outside_apis.py, against the same stand-in."""

import json
import os
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from first_party_dependencies import PLUGINS, ROOT
from test_outside_apis import PDB, StandIn

from iki_spec import schemas
from iki_spec.first_party_dependencies import dependency_closures
from iki_spec.validate import load_plugin

PLUGIN = ROOT / "examples" / "plugins" / "hosted_models"
SCRIPT = PLUGIN / "scripts" / "predict.py"
HOST = "api.foldcloud.example"
PROTEIN = "01a1ee10-0000-7000-8000-000000000001"


def test_the_plugin_is_valid() -> None:
    result = load_plugin(PLUGIN, dependencies=dependency_closures(PLUGINS, PLUGIN)[PLUGIN])
    assert result.problems == []
    assert result.plugin is not None
    (api,) = result.plugin.apis.values()
    assert (api.qualified_name, api.label, api.host) == (
        "lab/hosted_models.foldcloud",
        "FoldCloud",
        "{settings.fold_api_host}",
    )
    assert api.credential is not None and api.credential.secret == "FOLDCLOUD_API_KEY"
    assert api.jobs is not None and api.jobs.webhook is not None
    (use,) = result.plugin.actions["predict_structure"].apis
    assert use.name == "foldcloud"
    assert use.inputs == {"protein": ("sequence",)}
    assert use.params == ("model", "num_recycles")


class Tunnel:
    """A proxy that takes CONNECT for the API's host and tunnels to the stand-in, as the
    egress proxy would to FoldCloud."""

    def __init__(self, port: int) -> None:
        self.server = socket.create_server(("127.0.0.1", 0))
        self.port = self.server.getsockname()[1]
        self.asked: list[str] = []
        self.target = port
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                client, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self._tunnel, args=(client,), daemon=True).start()

    def _tunnel(self, client: socket.socket) -> None:
        head = b""
        while b"\r\n\r\n" not in head:
            head += client.recv(4096)
        self.asked.append(head.split(b"\r\n")[0].decode())
        upstream = socket.create_connection(("127.0.0.1", self.target))
        client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")

        def pump(source: socket.socket, sink: socket.socket) -> None:
            try:
                while data := source.recv(65536):
                    sink.sendall(data)
            except OSError:
                pass
            finally:
                sink.close()

        threading.Thread(target=pump, args=(upstream, client), daemon=True).start()
        pump(client, upstream)


@pytest.fixture
def foldcloud(tmp_path: Path) -> Iterator[tuple[StandIn, Tunnel]]:
    stand_in = StandIn(tmp_path, host=HOST, api_key=None)
    tunnel = Tunnel(stand_in.port)
    yield stand_in, tunnel
    tunnel.server.close()
    stand_in.server.shutdown()
    stand_in.server.server_close()


def ran(tmp_path: Path, foldcloud: tuple[StandIn, Tunnel], run: dict[str, Any]) -> Any:
    stand_in, tunnel = foldcloud
    (tmp_path / "ca.pem").write_text(stand_in.ca_pem)
    (tmp_path / "input.json").write_text(json.dumps(run))
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", ""),
        "IKI_INPUT": str(tmp_path / "input.json"),
        "IKI_OUTPUT": str(tmp_path / "output.json"),
        "IKI_OUT_DIR": str(out),
        # As the worker sets them: the attempt's proxy and CA, and the API's address.
        "HTTPS_PROXY": f"http://job:token@127.0.0.1:{tunnel.port}",
        "SSL_CERT_FILE": str(tmp_path / "ca.pem"),
        "IKI_API_FOLDCLOUD_URL": f"https://{HOST}",
        "IKI_API_FOLDCLOUD_CALLBACK_URL": "https://iki.example/api/v1/outside-apis/callbacks/x",
    }
    done = subprocess.run(  # noqa: S603 - this interpreter, on the plugin's script
        [sys.executable, "-I", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60
    )
    return done, tmp_path


def _run(**extra: Any) -> dict[str, Any]:
    return {
        "apiVersion": "iki/v1",
        "kind": "RunInput",
        "settings": {"fold_api_host": HOST},
        "inputs": {
            "protein": [{"id": PROTEIN, "hrid": "PRT-000001", "values": {"sequence": "MKVLAAGIVG"}}]
        },
        "params": {"model": "fold-2", "num_recycles": 3},
        **extra,
    }


def test_it_submits_the_sequence_and_parks_on_the_job(
    tmp_path: Path, foldcloud: tuple[StandIn, Tunnel]
) -> None:
    done, where = ran(tmp_path, foldcloud, _run())
    assert done.returncode == 0, done.stderr
    output = json.loads((where / "output.json").read_text())
    assert not list(schemas.check("run-output", output))
    assert output["park"] == {
        "api": "foldcloud",
        "job": "job-1",
        "state": {"protein": PROTEIN, "hrid": "PRT-000001"},
    }
    stand_in, tunnel = foldcloud
    assert tunnel.asked == [f"CONNECT {HOST}:443 HTTP/1.1"]
    ((method, path, headers, body),) = stand_in.requests
    assert (method, path) == ("POST", "/v1/predictions")
    # It sends what the action says it sends, and no key: the egress proxy adds that.
    assert json.loads(body) == {
        "sequence": "MKVLAAGIVG",
        "model": "fold-2",
        "num_recycles": 3,
        "webhook": "https://iki.example/api/v1/outside-apis/callbacks/x",
    }
    assert "Authorization" not in headers


def test_resumed_it_imports_the_predicted_structure(
    tmp_path: Path, foldcloud: tuple[StandIn, Tunnel]
) -> None:
    answer = {"id": "job-1", "status": "succeeded", "model": "fold-2", "output": {"pdb": PDB}}
    resume = {
        "api": "foldcloud",
        "job": "job-1",
        "state": {"protein": PROTEIN, "hrid": "PRT-000001"},
        "status": "succeeded",
        "answer": answer,
        "parked_at": "2026-11-18T10:00:00Z",
    }
    done, where = ran(tmp_path, foldcloud, _run(resume=resume))
    assert done.returncode == 0, done.stderr
    output = json.loads((where / "output.json").read_text())
    assert not list(schemas.check("run-output", output))
    (create,) = output["creates"]
    assert create["type"] == "iki/protein.ProteinStructure"
    assert create["values"]["method"] == "predicted"
    assert create["values"]["chains"] == [PROTEIN]
    # Its chain, so its secondary structure can be assigned by chain.
    assert create["values"]["chain_ids"] == ["A"]
    assert create["values"]["structure"]["$file"] == "predicted.pdb"
    assert (where / "out" / "predicted.pdb").read_text() == PDB
    # Where it's from and how it was made (ADR 0106); the version FoldCloud reports is in
    # the run's record of its calls, not here.
    assert create["origin"] == {
        "source": "FoldCloud",
        "id": "job-1",
        "made": {
            "kind": "computational",
            "method": "Predicted by FoldCloud",
            "software": [{"name": "FoldCloud fold-2"}],
        },
    }
    # The answer came with the resume: it called nothing.
    assert foldcloud[0].requests == []


#: Crambin's first ten residues (PDB 1CRN, CC0 1.0), chain A.
CRAMBIN = (Path(__file__).parent / "data" / "crambin.pdb").read_text()


@pytest.mark.parametrize(
    ("pdb", "chain_ids"),
    [
        (CRAMBIN, ["A"]),
        # A homodimer of it: chains A and B, each the protein.
        (
            CRAMBIN.replace(
                "TER",
                "TER\n"
                + "\n".join(
                    line[:21] + "B" + line[22:]
                    for line in CRAMBIN.splitlines()
                    if line.startswith("ATOM")
                ),
            ),
            ["A", "B"],
        ),
        # A water on a chain of its own isn't a copy of the protein: chain W has no amino
        # acids, as wwPDB's HETATM records for solvent say.
        (
            CRAMBIN.replace(
                "TER",
                "TER\nHETATM  999  O   HOH W   1      10.000  10.000  10.000  1.00  0.00"
                "           O",
            ),
            ["A"],
        ),
        # A chain whose residues are all HETATM, as modified amino acids such as
        # selenomethionine (MSE) are written, is the protein: its residues have a backbone.
        (
            CRAMBIN.replace(
                "TER",
                "TER\n"
                + "\n".join(
                    "HETATM" + line[6:17] + "MSE B" + line[22:]
                    for line in CRAMBIN.splitlines()
                    if line.startswith("ATOM")
                ),
            ),
            ["A", "B"],
        ),
        # Two models of it: the first's chains.
        (
            "MODEL        1\n"
            + CRAMBIN.replace("END\n", "ENDMDL\nMODEL        2\n")
            + CRAMBIN.replace(" A ", " C ").replace("END\n", "ENDMDL\nEND\n"),
            ["A"],
        ),
    ],
)
def test_a_predicted_structure_has_each_chain_its_file_has(
    tmp_path: Path, foldcloud: tuple[StandIn, Tunnel], pdb: str, chain_ids: list[str]
) -> None:
    answer = {"id": "job-1", "status": "succeeded", "model": "fold-2", "output": {"pdb": pdb}}
    resume = {
        "api": "foldcloud",
        "job": "job-1",
        "state": {"protein": PROTEIN, "hrid": "PRT-000001"},
        "status": "succeeded",
        "answer": answer,
        "parked_at": "2026-11-18T10:00:00Z",
    }
    done, where = ran(tmp_path, foldcloud, _run(resume=resume))
    assert done.returncode == 0, done.stderr
    output = json.loads((where / "output.json").read_text())
    (create,) = output["creates"]
    assert create["values"]["chain_ids"] == chain_ids
    assert create["values"]["chains"] == [PROTEIN] * len(chain_ids)
    left_out = [m["text"] for m in output["messages"] if "no amino acids" in m["text"]]
    assert left_out == (
        [
            "FoldCloud's file also has chain W, with no amino acids (solvent or ligands): it "
            "is not the protein, so the structure's chains leave it out."
        ]
        if "HOH" in pdb
        else []
    )


def test_a_predicted_structure_without_chain_ids_says_so(
    tmp_path: Path, foldcloud: tuple[StandIn, Tunnel]
) -> None:
    blank = "".join(
        line[:21] + " " + line[22:] + "\n"
        for line in CRAMBIN.splitlines()
        if line.startswith("ATOM")
    )
    answer = {"id": "job-1", "status": "succeeded", "model": "fold-2", "output": {"pdb": blank}}
    resume = {
        "api": "foldcloud",
        "job": "job-1",
        "state": {"protein": PROTEIN, "hrid": "PRT-000001"},
        "status": "succeeded",
        "answer": answer,
        "parked_at": "2026-11-18T10:00:00Z",
    }
    done, where = ran(tmp_path, foldcloud, _run(resume=resume))
    assert done.returncode == 0, done.stderr
    output = json.loads((where / "output.json").read_text())
    (create,) = output["creates"]
    assert "chain_ids" not in create["values"]
    assert create["values"]["chains"] == [PROTEIN]
    assert {
        "level": "warning",
        "text": "FoldCloud's file names no chain, so the structure has no chain IDs: its "
        "secondary structure can't be assigned by chain.",
    } in output["messages"]


def test_what_the_api_refuses_fails_the_run_saying_why(tmp_path: Path) -> None:
    stand_in = StandIn(tmp_path, host=HOST, api_key="only-the-proxy-has-it")
    tunnel = Tunnel(stand_in.port)
    try:
        done, _ = ran(tmp_path, (stand_in, tunnel), _run())
    finally:
        tunnel.server.close()
        stand_in.server.shutdown()
        stand_in.server.server_close()
    assert done.returncode != 0
    assert "FoldCloud answered 401" in done.stderr
