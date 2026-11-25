"""`iki plugin test` (ADR 0093), with a stand-in for iki-worker that answers as a test says."""

import hashlib
import json
import shutil
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path

import pytest
import yaml

from iki_cli import upgrade_test, workers
from iki_cli.main import main
from iki_spec.builtin import CORE_DIRECTORY, core_plugin

PLUGINS = Path(__file__).parents[2] / "spec" / "tests" / "plugins"

#: Answers `run --local input.json …` with what $FAKE_ANSWERS gives for the run's
#: first entity's sequence, its params' accession, or its action; records each input it
#: was given.
WORKER = """\
import json, os, sys
given = json.load(open(sys.argv[3]))
with open(os.environ["FAKE_SEEN"], "a") as seen:
    call = {"input": given, "argv": sys.argv[1:], "work_dir": os.environ.get("IKI_WORK_DIR")}
    seen.write(json.dumps(call) + "\\n")
answers = json.load(open(os.environ["FAKE_ANSWERS"]))
key = next(iter(given["inputs"].get("protein") or [{}]), {}).get("values", {}).get("sequence")
answer = answers[key or given["params"].get("accession") or given["run"]["action"]]
if "annotation_digest" in answer:
    import hashlib
    directory = sys.argv[sys.argv.index("--plugin") + 1]
    with open(os.path.join(directory, "data", "dna-annotation.wasm"), "rb") as module:
        assert hashlib.sha256(module.read()).hexdigest() == answer["annotation_digest"]
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


@pytest.fixture
def first_party(tmp_path: Path) -> Path:
    for marker in ("scripts/first_party_trees.py", "spec/src/iki_spec/first_party_trees.json"):
        path = tmp_path / marker
        path.parent.mkdir(parents=True)
        path.touch()
    return tmp_path / "plugins"


def manifest(path: Path, name: str, *dependencies: str) -> Path:
    path.mkdir(parents=True)
    (path / "plugin.yaml").write_text(
        json.dumps(
            {
                "apiVersion": "iki/v1",
                "kind": "Plugin",
                "name": name,
                "version": "1.0.0",
                "dependencies": {dependency: {} for dependency in dependencies},
            }
        )
    )
    return path


def test_first_party_test_from_plugin_directory_finds_transitive_dependencies(
    first_party: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest(first_party / "dna", "iki/dna")
    manifest(first_party / "protein", "iki/protein", "iki/dna")
    plugin = manifest(first_party / "lots", "iki/lots", "iki/protein")
    monkeypatch.chdir(plugin)
    assert main(["plugin", "test"]) == 0
    assert "iki/lots has no action tests" in capsys.readouterr().out


def test_first_party_test_keeps_explicit_overrides_and_fixture_only_dependencies(
    first_party: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest(first_party / "dna", "iki/dna")
    manifest(first_party / "protein", "iki/protein", "legacy/missing")
    plugin = manifest(first_party / "lots", "iki/lots", "iki/protein")
    pinned = manifest(tmp_path / "pinned", "iki/protein", "iki/dna")
    fixture_plugin = manifest(tmp_path / "fixture", "lab/fixture", "lab/standards")
    standards = manifest(tmp_path / "standards", "lab/standards")

    run_test = ["plugin", "test", str(plugin), "--with", str(pinned), "--with", str(fixture_plugin)]
    assert main(run_test) == 1
    assert "lab/fixture needs the lab/standards plugin" in capsys.readouterr().err
    assert main([*run_test, "--with", str(standards)]) == 0
    assert "iki/lots has no action tests" in capsys.readouterr().out


@pytest.mark.parametrize("in_checkout", [True, False])
def test_third_party_and_off_tree_plugins_keep_explicit_dependencies(
    first_party: Path, tmp_path: Path, in_checkout: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    plugins = first_party if in_checkout else tmp_path / "other" / "plugins"
    base = manifest(plugins / "dna", "iki/dna")
    name = "lab/assays" if in_checkout else "iki/lots"
    plugin = manifest(plugins / "lots", name, "iki/dna")
    args = ["plugin", "test", str(plugin)]
    assert main(args) == 1
    assert "give it with --with" in capsys.readouterr().err
    assert main([*args, "--with", str(base)]) == 0


def test_first_party_dependency_cycle_is_a_cli_error(
    first_party: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plugin = manifest(first_party / "protein", "iki/protein", "iki/dna")
    manifest(first_party / "dna", "iki/dna", "iki/protein")
    assert main(["plugin", "test", str(plugin)]) == 1
    assert "cycle" in capsys.readouterr().err.lower()


def test_only_selected_annotation_tests_need_the_installed_module(
    plugin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = manifest(plugin.parent / "plasmid", "iki/plasmid")
    for folder in ("actions", "environments", "data", "tests"):
        (source / folder).mkdir()
    (source / "requirements.txt").touch()
    shutil.copyfile(
        PLUGINS / "protein" / "environments" / "protein-tools.yaml",
        source / "environments" / "protein-tools.yaml",
    )
    for action in ("annotate_dna_sequence", "register_plasmid"):
        (source / "actions" / f"{action}.yaml").write_text(
            f"apiVersion: iki/v1\nkind: Action\nname: {action}\n"
            "runs_on: { tier: backend, environment: protein-tools }\ncommand: [echo, ok]\n"
            "artifacts: { keep: [report.txt] }\n"
        )
        (source / "tests" / f"{action}.yaml").write_text(
            f"apiVersion: iki/v1\nkind: ActionTest\nname: {action}\naction: {action}\n"
            "expect:\n  output: { kind: RunOutput }\n"
        )
    installed = plugin.parent / "iki-spec"
    installed.mkdir()
    module = installed / "dna-annotation.wasm"
    module.write_bytes(b"\0asm\x01\0\0\0")
    (installed / "rust-standard-library.html").write_text("Rust standard library")
    digest = hashlib.sha256(module.read_bytes()).hexdigest()
    (source / "data" / "dna-annotation.wasm.sha256").write_text(digest + "\n")
    monkeypatch.setattr(workers, "files", lambda _package: installed)
    answers: dict[str, dict[str, object]] = {
        "iki/plasmid.annotate_dna_sequence": {"output": OUTPUT, "annotation_digest": digest},
        "iki/plasmid.register_plasmid": {"output": OUTPUT},
    }
    answer(source, **answers)

    command = ["plugin", "test", str(source), "-k"]
    assert main([*command, "annotate_dna_sequence"]) == 0
    assert not (source / "data" / module.name).exists()
    module.unlink()
    assert main([*command, "register_plasmid"]) == 0


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
    plugin: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = plugin.parent / "work"
    monkeypatch.setenv("IKI_WORK_DIR", str(root))
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
    assert seen["argv"][3:] == ["--plugin", str(plugin.resolve()), "--timeout", "300", "--keep"]
    work = Path(seen["argv"][2]).parent
    assert seen["work_dir"] == str(work)
    assert work.parent == root
    assert list(root.iterdir()) == []


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


def test_upgrade_builds_against_core_at_the_earlier_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    core = tmp_path / "spec/src/iki_spec/core"
    shutil.copytree(CORE_DIRECTORY, core)
    alignment = core / "types/alignment.yaml"
    old_core = yaml.safe_load(alignment.read_text())
    del old_core["fields"]["sample"]
    alignment.write_text(yaml.safe_dump(old_core))

    core_app = tmp_path / "server/src/iki_plugin_iki__core"
    core_app.mkdir(parents=True)
    (core_app / "manifest.json").write_text('{"earlier": true}')

    plugin = manifest(tmp_path / "plugins/lab", "lab/assays")
    plugin_file = plugin / "plugin.yaml"
    metadata = json.loads(plugin_file.read_text())
    metadata["settings"] = {"endpoint": {"type": "string"}}
    plugin_file.write_text(json.dumps(metadata))
    (plugin / "types").mkdir()
    result_type = plugin / "types/result.yaml"
    result_type.write_text(
        """\
apiVersion: iki/v1
kind: EntityType
name: Result
schema_version: 1
hrid: { prefix: RSL }
extends: iki/core.Alignment
fields:
  sample: { type: ref, to: iki/core.Entity }
"""
    )
    for args in (
        ["init", "-q"],
        ["add", "."],
        ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base"],
    ):
        subprocess.run(["git", *args], cwd=tmp_path, check=True)  # noqa: S603, S607
    result_type.write_text(
        result_type.read_text()
        .replace("schema_version: 1", "schema_version: 2")
        .replace("fields:\n  sample: { type: ref, to: iki/core.Entity }\n", "")
    )
    (core_app / "manifest.json").write_text('{"earlier": false}')
    metadata["version"] = "1.1.0"
    metadata["settings"]["endpoint"]["required"] = False
    plugin_file.write_text(json.dumps(metadata))

    class Scratch(nullcontext[str]):
        name = "dummy"
        left = False

    monkeypatch.setattr(upgrade_test, "_scratch", lambda *_: Scratch("unused"))
    steps = []

    def check_core(
        step: str, build: Path, *_: object, core: Path | None
    ) -> tuple[list[str], list[str]]:
        steps.append(step)
        archived = build / "iki_plugin_iki__core/manifest.json"
        if step == "after":
            assert core is None
            assert not archived.exists()
        else:
            assert core is not None
            assert (
                "sample"
                not in yaml.safe_load((core / "types/alignment.yaml").read_text())["fields"]
            )
            assert json.loads(archived.read_text()) == {"earlier": True}
        return [], []

    monkeypatch.setattr(upgrade_test, "_step", check_core)
    assert (
        main(["plugin", "test", str(plugin), "--upgrade-from", "HEAD", "--database", "unused"]) == 0
    )
    assert steps == ["before", "after", "rollback"]
    assert "sample" in core_plugin().types["iki/core.Alignment"].fields
    assert "upgraded: the samples and their history are intact" in capsys.readouterr().out
