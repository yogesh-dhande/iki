"""`iki plugin test` (ADR 0093), with a stand-in for iki-worker that answers as a test says."""

import json
import shutil
import sys
from pathlib import Path

import pytest

from iki_cli.main import main

PLUGINS = Path(__file__).parents[2] / "spec" / "tests" / "plugins"

#: Answers `run --local input.json …` with what $FAKE_ANSWERS gives for the run's
#: first entity's sequence, its params' accession, or its action; records each input it
#: was given.
WORKER = """\
import json, os, sys
given = json.load(open(sys.argv[3]))
with open(os.environ["FAKE_SEEN"], "a") as seen:
    seen.write(json.dumps({"input": given, "argv": sys.argv[1:]}) + "\\n")
answers = json.load(open(os.environ["FAKE_ANSWERS"]))
key = next(iter(given["inputs"].get("protein") or [{}]), {}).get("values", {}).get("sequence")
answer = answers[key or given["params"].get("accession") or given["run"]["action"]]
sys.stderr.write(answer.get("log", ""))
if "creates_file" in answer:
    # As iki-worker run --local describes a file the command wrote: in its own directory,
    # which is gone once the run is over.
    import hashlib
    path = os.path.join(os.path.dirname(os.path.abspath(sys.argv[3])), "creates.ndjson")
    with open(path, "w") as written:
        written.write(answer["creates_file"])
    data = open(path, "rb").read()
    answer["output"]["creates_file"] = {
        "$file": "creates.ndjson", "name": "creates.ndjson", "path": path,
        "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
        "media_type": "application/x-ndjson",
    }
if "output" in answer:
    print(json.dumps(answer["output"]))
sys.exit(answer.get("code", 0))
"""

OUTPUT = {"apiVersion": "iki/v1", "kind": "RunOutput"}


@pytest.fixture(autouse=True)
def _plain_output(monkeypatch: pytest.MonkeyPatch) -> None:
    # CI sets FORCE_COLOR; problems are compared as plain text.
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")


@pytest.fixture
def plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    shutil.copytree(PLUGINS / "protein", tmp_path / "protein")
    worker = tmp_path / "iki-worker"
    worker.write_text(f"#!{sys.executable}\n{WORKER}")
    worker.chmod(0o755)
    monkeypatch.setenv("IKI_WORKER", str(worker))
    monkeypatch.setenv("FAKE_SEEN", str(tmp_path / "seen.jsonl"))
    monkeypatch.setenv("FAKE_ANSWERS", str(tmp_path / "answers.json"))
    (tmp_path / "protein" / "tests").mkdir()
    return tmp_path / "protein"


def answer(plugin: Path, **answers: dict[str, object]) -> None:
    (plugin.parent / "answers.json").write_text(json.dumps(answers))


def fixture(plugin: Path, text: str) -> None:
    (plugin / "tests" / "properties.yaml").write_text(text)


PROPERTIES = """\
apiVersion: iki/v1
kind: ActionTest
name: mkv
action: compute_properties
inputs:
  protein:
    - values: { sequence: MKV }
expect:
  output:
    updates:
      protein:
        - values: { molecular_weight: 374.5 }
---
apiVersion: iki/v1
kind: ActionTest
name: missing
action: fetch_proteins
params: { accession: P99999 }
expect:
  error: No such accession
"""


def run(plugin: Path, *args: str) -> int:
    return main(["plugin", "test", str(plugin), "--with", str(PLUGINS / "dna"), *args])


def test_each_test_passes_or_says_why(plugin: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fixture(plugin, PROPERTIES)
    updated = {"updates": {"protein": [{"entity": "x", "values": {"molecular_weight": 374.49}}]}}
    answer(
        plugin,
        MKV={"output": {**OUTPUT, **updated}},
        P99999={"code": 1, "log": "Asking UniProt\n::error No such accession: P99999\n"},
    )
    assert run(plugin) == 1
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "Running 2 test(s) of acme/protein 3.0.0:"
    assert "  FAILED  mkv  (" in out
    assert "          tests/properties.yaml\n" in out
    assert "updates.protein[0].values.molecular_weight: expected 374.5, got 374.49" in out
    assert "  ok      missing  (" in out
    assert out.splitlines()[-1].startswith("1 passed, 1 failed in ")

    assert run(plugin, "-k", "missing") == 0
    assert "1 passed in" in capsys.readouterr().out
    assert run(plugin, "-k", "nothing") == 1
    assert "protein has no action tests matching nothing." in capsys.readouterr().out


def test_its_run_input_is_made_from_the_manifests(
    plugin: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture(plugin, PROPERTIES.split("---")[0])
    answer(plugin, MKV={"output": OUTPUT, "code": 1, "log": "::error Unknown residue\n"})
    assert run(plugin) == 1
    out = capsys.readouterr().out
    assert "It failed: Unknown residue" in out
    assert "          Its log ended:\n          | ::error Unknown residue\n" in out
    seen = json.loads((plugin.parent / "seen.jsonl").read_text().splitlines()[0])
    given = seen["input"]
    assert given["run"]["action"] == "acme/protein.compute_properties"
    assert given["command"] == ["python", "./scripts/properties.py"]
    assert given["inputs"]["protein"][0]["values"] == {"sequence": "MKV"}
    assert seen["argv"][:2] == ["run", "--local"]
    assert seen["argv"][3:] == ["--plugin", str(plugin.resolve()), "--timeout", "300"]


def test_what_an_action_may_not_change_fails_it(
    plugin: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture(plugin, PROPERTIES.split("---")[0].split("expect:")[0])
    updated = {"updates": {"protein": [{"entity": "x", "values": {"sequence": "M"}}]}}
    answer(plugin, MKV={"output": {**OUTPUT, **updated, "colour": "red"}})
    assert run(plugin) == 1
    out = capsys.readouterr().out
    assert "output.json: " in out
    assert (
        "It sets sequence on protein, which compute_properties doesn't declare it updates." in out
    )


FETCHED = """\
apiVersion: iki/v1
kind: ActionTest
name: fetched
action: fetch_proteins
params: { accession: P69905 }
"""


def test_an_output_the_server_would_refuse_fails_it(
    plugin: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Checked as the server checks a run's output, so a test fails where a real run would:
    here, `fills` names a value the create doesn't give."""
    fixture(plugin, FETCHED)
    given = {"sequence": "MVLSPADKTNV"}
    protein = {"type": "acme/protein.Protein", "values": given}
    filled = {**protein, "values": {**given, "hazard": "bsl1"}}
    answer(plugin, P69905={"output": {**OUTPUT, "creates": [{**filled, "fills": ["hazard"]}]}})
    assert run(plugin) == 0, capsys.readouterr().out
    capsys.readouterr()
    answer(
        plugin,
        P69905={
            "output": {
                **OUTPUT,
                "creates": [{**protein, "fills": ["hazard", "sequence"]}, protein],
            }
        },
    )
    assert run(plugin) == 1
    out = capsys.readouterr().out
    assert "  FAILED  fetched  (" in out
    assert "output.json at #/creates/0/fills/0: It gives no value for `hazard`." in out
    assert (
        "output.json at #/creates/0/fills/1: `sequence` is its identity, which an entry "
        "registered already has." in out
    )
    assert "output.json at #/creates/1: The action creates at most one acme/protein.Protein" in out


def test_a_creates_file_the_server_would_refuse_fails_it(
    plugin: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A bulk run's creates file (ADR 0056) is checked line by line, as the server checks
    each before writing it: an undeclared type fails a test in the file as it does in
    `creates`."""
    fixture(plugin, FETCHED)
    sample = {"type": "acme/protein.Sample", "values": {"label": "S-1"}}
    answer(plugin, P69905={"output": {**OUTPUT, "creates": [sample]}})
    assert run(plugin) == 1
    inline = capsys.readouterr().out
    assert (
        "output.json at #/creates/0/type: The action doesn't declare that it creates "
        "acme/protein.Sample (it creates: acme/protein.Protein)." in inline
    )
    protein = {"type": "acme/protein.Protein", "values": {"sequence": "MVLSPADKTNV"}}
    lines = [json.dumps(protein), "", json.dumps(sample), "{not json", json.dumps({"type": 1})]
    answer(plugin, P69905={"output": dict(OUTPUT), "creates_file": "\n".join(lines) + "\n"})
    assert run(plugin) == 1
    out = capsys.readouterr().out
    assert "  FAILED  fetched  (" in out
    assert (
        "creates.ndjson at #/creates_file/1: The action creates at most one "
        "acme/protein.Protein per run: give it in `creates`." in out
    )
    assert (
        "creates.ndjson at #/creates_file/3/type: The action doesn't declare that it creates "
        "acme/protein.Sample (it creates: acme/protein.Protein)." in out
    )
    assert "creates.ndjson at #/creates_file/4: The line isn't JSON: " in out
    assert "creates.ndjson at #/creates_file/5/type: " in out
    # What it declares, in the file, passes.
    fixture(plugin, IMPORTED)
    (plugin / "tests" / "abl1.fasta").write_text(">ABL1\nMLEICLKLVG\n")
    imported: dict[str, object] = {
        "output": dict(OUTPUT),
        "creates_file": json.dumps({**protein, "values": {"sequence": "MLEICLKLVG"}}),
    }
    answer(plugin, **{"acme/protein.import_proteins": imported})
    assert run(plugin) == 0, capsys.readouterr().out


IMPORTED = """\
apiVersion: iki/v1
kind: ActionTest
name: imported
action: import_proteins
params: { file: { $file: fasta } }
files: { fasta: tests/abl1.fasta }
"""


def test_a_broken_fixture_runs_nothing(plugin: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fixture(plugin, PROPERTIES.replace("action: fetch_proteins", "action: fold"))
    assert run(plugin) == 1
    captured = capsys.readouterr()
    assert (
        "tests/properties.yaml:17:9: error: acme/protein has no action fold. [unknown-action]"
        in (captured.err)
    )
    assert not (plugin.parent / "seen.jsonl").exists()


def test_it_needs_the_worker(
    plugin: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture(plugin, PROPERTIES)
    monkeypatch.delenv("IKI_WORKER")
    monkeypatch.setenv("PATH", str(plugin))
    assert run(plugin) == 1
    assert "iki-worker, which isn't on the PATH" in capsys.readouterr().err
    assert run(plugin, "--worker", str(plugin / "plugin.yaml")) == 1
    assert "isn't an iki-worker binary this can run" in capsys.readouterr().err
