"""Iki's action helper, iki_action.py (ADR 0287): what it reads and writes, how a run fails
with it, and the check that a plugin's copy is one Iki published."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from iki_spec import action_helper
from iki_spec.validate import load_plugin

ROOT = Path(__file__).resolve().parents[2]
#: A type compared by its qualified name, as `part["type"] == "iki/protein.Protein"` does.
BY_NAME = re.compile(
    r"""(\[["']type["']\]|\.get\(["']type["']\))\s*(==|!=)\s*["'][a-z0-9_]+/[a-z0-9_]+\.[A-Z]"""
)
PROTEIN = "00000000-0000-7000-8000-000000000001"
ENTRY = "00000000-0000-7000-8000-0000000000e1"


def helper() -> ModuleType:
    spec = importlib.util.spec_from_file_location("iki_action", action_helper.SOURCE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


iki = helper()


def run_input(**inputs: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "apiVersion": "iki/v1",
        "kind": "RunInput",
        "run": {
            "id": "00000000-0000-7000-8000-0000000000a1",
            "hrid": "RUN-000001",
            "attempt": 1,
            "action": "acme/protein.compute",
            "plugin": {"name": "acme/protein", "version": "1.0.0", "sha": None},
            "project": "00000000-0000-7000-8000-0000000000b1",
        },
        "params": {"pH": 7.4},
        "inputs": inputs,
        "files": {"f1": {"path": "/job/f1.fasta"}},
    }


def entity(type_: str, *types: str, **values: Any) -> dict[str, Any]:
    return {
        "id": PROTEIN,
        "hrid": "PRT-000001",
        "type": type_,
        "types": [type_, *types, "iki/core.Entity"],
        "schema_version": 1,
        "revision": 3,
        "values": values,
    }


def test_a_run_reads_as_its_json_does_with_names_beside_it(tmp_path: Path) -> None:
    given = run_input(protein=[entity("acme/protein.Protein", name="hGH")])
    path = tmp_path / "input.json"
    path.write_text(json.dumps(given))
    run = iki.read(path)
    assert run["params"] == run.params == {"pH": 7.4}
    assert run.plugin == "acme/protein"
    assert run.action == "acme/protein.compute"
    protein = run.input("protein")
    assert protein["values"] == {"name": "hGH"}
    assert (protein.id, protein.hrid, protein.revision) == (PROTEIN, "PRT-000001", 3)
    assert run.inputs("absent") == []
    assert run.file("f1") == Path("/job/f1.fasta")
    with pytest.raises(iki.Refused, match="no file"):
        run.file("f2")
    with pytest.raises(iki.Refused, match="needs one ligand"):
        run.input("ligand")
    # Still the JSON: it writes as it was read.
    assert json.loads(json.dumps(run)) == given


def test_its_names_hide_no_dict_method() -> None:
    # Code that walks the JSON as dicts (`for v in entity.values()`) walks these too.
    for wrapper in (iki.Run, iki.Entity):
        names = {n for n, v in vars(wrapper).items() if not n.startswith("_")}
        assert not names & set(dir(dict)), wrapper
    record = iki.Entity(entity("acme/protein.Protein", name="hGH"))
    assert list(record.values())[2] == "acme/protein.Protein"


def test_a_run_input_of_another_version_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "input.json"
    path.write_text(json.dumps({**run_input(), "apiVersion": "iki/v2"}))
    with pytest.raises(iki.Refused, match="iki/v2, which this action helper"):
        iki.read(path)
    path.write_text(json.dumps({**run_input(), "kind": "RunOutput"}))
    with pytest.raises(iki.Refused, match="not a RunInput"):
        iki.read(path)
    # A test's own input may leave them out.
    path.write_text(json.dumps({"params": {"pH": 7}}))
    assert iki.read(path).params == {"pH": 7}


def test_a_number_json_cant_hold_is_refused(tmp_path: Path) -> None:
    with pytest.raises(iki.Refused, match="NaN or infinite"):
        iki.write({"metrics": {"kd": float("nan")}}, tmp_path / "output.json")


def test_a_type_is_compared_by_its_lineage() -> None:
    # A lab's own protein is a protein (ADR 0042: launch takes subtypes).
    lab = entity("acme/lab.GmpProtein", "iki/protein.Protein")
    assert iki.is_a(lab, "iki/protein.Protein")
    assert iki.is_a(lab, "acme/lab.GmpProtein")
    assert iki.is_a(lab, "iki/core.Entity")
    assert not iki.is_a(lab, "iki/dna.DNASequence")
    assert iki.Entity(lab).is_a("iki/protein.Protein")


def test_a_reference_names_the_registry_entry_if_there_is_one() -> None:
    record = entity("acme/protein.Protein")
    assert iki.ref(record) == PROTEIN
    assert iki.ref({**record, "entry_id": ENTRY}) == ENTRY
    assert iki.Entity({**record, "entry_id": ENTRY}).ref == ENTRY


def test_an_output_qualifies_its_own_types_and_says_only_what_it_has() -> None:
    run = iki.Run(run_input(protein=[entity("acme/protein.Protein")]))
    out = run.output()
    assert out.document() == {"apiVersion": "iki/v1", "kind": "RunOutput"}
    chain = out.create("Chain", {"sequence": "MKT"}, key="a")
    out.create("iki/core.Collection", {"name": "Set"}, derived_from=[PROTEIN])
    out.update("protein", run.input("protein"), {"chains": [chain]}, require_unchanged=True)
    out.warning("No signal peptide", entity=run.input("protein"))
    out.metric("chains", 1)
    assert out.document() == {
        "apiVersion": "iki/v1",
        "kind": "RunOutput",
        "updates": {
            "protein": [
                {
                    "entity": PROTEIN,
                    "values": {"chains": [{"$create": "a"}]},
                    "require_unchanged": True,
                }
            ]
        },
        "creates": [
            {"type": "acme/protein.Chain", "values": {"sequence": "MKT"}, "key": "a"},
            {
                "type": "iki/core.Collection",
                "values": {"name": "Set"},
                "derived_from": [PROTEIN],
            },
        ],
        "messages": [{"level": "warning", "text": "No signal peptide", "entity": PROTEIN}],
        "metrics": {"chains": 1},
    }
    with pytest.raises(ValueError, match="level"):
        out.message("x", "debug")


def test_messages_past_what_a_run_keeps_never_drop_an_error() -> None:
    out = iki.Output()
    for i in range(150):
        out.info(f"Row {i}")
    out.error("Row 7 has no bases", entity=PROTEIN)
    messages = out.document()["messages"]
    assert len(messages) == iki.MAX_MESSAGES
    assert messages[0] == {"level": "error", "text": "Row 7 has no bases", "entity": PROTEIN}
    assert messages[-1] == {"level": "warning", "text": "And 52 more."}
    many = iki.Output()
    for i in range(iki.MAX_MESSAGES + 1):
        many.error(f"Row {i} has no bases")
    with pytest.raises(iki.Refused, match="101 problems, the first: Row 0"):
        many.document()


def test_an_output_over_what_the_worker_reads_is_refused(tmp_path: Path) -> None:
    out = iki.Output()
    out.create("acme/protein.Protein", {"notes": "x" * iki.MAX_OUTPUT_BYTES})
    with pytest.raises(iki.Refused, match=r"2\.5 MB"):
        out.write(tmp_path / "output.json")
    assert not (tmp_path / "output.json").exists()


def command(tmp_path: Path, body: str, given: dict[str, Any], *args: str) -> tuple[int, str, Any]:
    (tmp_path / "iki_action.py").write_bytes(action_helper.source())
    (tmp_path / "command.py").write_text(body)
    (tmp_path / "input.json").write_text(json.dumps(given))
    env = {"IKI_INPUT": str(tmp_path / "input.json"), "IKI_OUTPUT": str(tmp_path / "out.json")}
    done = subprocess.run(  # noqa: S603 - this test's own command
        [sys.executable, "-E", "-s", str(tmp_path / "command.py"), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    out = tmp_path / "out.json"
    return done.returncode, done.stdout, json.loads(out.read_text()) if out.exists() else None


def test_main_writes_what_the_action_returns_and_fails_with_its_reason(tmp_path: Path) -> None:
    body = (
        "import iki_action as iki\n"
        "def count(run):\n"
        "    proteins = run.inputs('protein')\n"
        "    if not proteins:\n"
        "        raise iki.Refused('Give it a protein\\nto count.')\n"
        "    return {'metrics': {'proteins': len(proteins)}}\n"
        "def nothing(run):\n"
        "    return None\n"
        "iki.main({'count': count, 'nothing': nothing})\n"
    )
    given = run_input(protein=[entity("acme/protein.Protein")])
    code, _, output = command(tmp_path, body, given, "count")
    assert code == 0
    assert output == {"apiVersion": "iki/v1", "kind": "RunOutput", "metrics": {"proteins": 1}}
    code, _, output = command(tmp_path, body, given, "nothing")
    assert (code, output) == (0, {"apiVersion": "iki/v1", "kind": "RunOutput"})
    (tmp_path / "out.json").unlink()
    code, printed, output = command(tmp_path, body, run_input(), "count")
    # The run's page shows the last ::error line, on one line.
    assert (code, printed, output) == (1, "::error Give it a protein to count.\n", None)
    code, printed, _ = command(tmp_path, body, given, "fold")
    assert (code, printed) == (1, "::error Name one of these commands: count, nothing.\n")


def test_it_runs_on_the_oldest_python_an_image_may_bring() -> None:
    # Python's own standard library, and syntax Python 3.8 parses: images Iki doesn't
    # build (CRISPResso2's, Cell Ranger's) bring their own Python.
    import ast

    tree = ast.parse(action_helper.source(), feature_version=(3, 8))
    imported = {
        name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for name in (
            [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module]
        )
        if name
    }
    assert imported <= {"__future__", "json", "os", "sys", "pathlib", "typing"}


def test_every_published_version_keeps_its_digest() -> None:
    assert action_helper.PUBLISHED[action_helper.CURRENT] == (
        hashlib.sha256(action_helper.source()).hexdigest()
    ), "A change to iki_action.py is a new version: add it to PUBLISHED, and keep the old."
    assert iki.VERSION == action_helper.CURRENT
    assert len(set(action_helper.PUBLISHED.values())) == len(action_helper.PUBLISHED)


def plugin(tmp_path: Path) -> Path:
    root = tmp_path / "plugin"
    (root / "scripts").mkdir(parents=True)
    (root / "plugin.yaml").write_text(
        "apiVersion: iki/v1\nkind: Plugin\nname: acme/example\nversion: 1.0.0\n"
        "title: Example\ndescription: An example.\nlicense: Apache-2.0\n"
    )
    return root


def codes(root: Path) -> list[tuple[str, str | None]]:
    return [
        (p.code, p.file) for p in load_plugin(root).problems if p.code.startswith("action-helper")
    ]


def test_validate_takes_a_published_copy_and_refuses_an_edited_one(tmp_path: Path) -> None:
    root = plugin(tmp_path)
    copy = root / "scripts" / "iki_action.py"
    assert codes(root) == []
    copy.write_bytes(action_helper.source())
    assert codes(root) == []
    copy.write_bytes(action_helper.source() + b"\n# tidied\n")
    assert codes(root) == [("action-helper-changed", "scripts/iki_action.py")]
    # Wherever a copy is, as in an image's own build context.
    copy.write_bytes(action_helper.source())
    (root / "images" / "tools").mkdir(parents=True)
    (root / "images" / "tools" / "iki_action.py").write_text("print('mine')\n")
    assert codes(root) == [("action-helper-changed", "images/tools/iki_action.py")]


def test_validate_warns_of_an_earlier_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = plugin(tmp_path)
    (root / "scripts" / "iki_action.py").write_bytes(b"# an earlier helper\n")
    earlier = hashlib.sha256(b"# an earlier helper\n").hexdigest()
    monkeypatch.setitem(action_helper.PUBLISHED, "0.9.0", earlier)
    (problem,) = [p for p in load_plugin(root).problems if p.code == "action-helper-outdated"]
    assert not problem.is_error
    assert "0.9.0" in problem.message


def test_vendor_writes_this_ikis_helper(tmp_path: Path) -> None:
    from iki_cli.main import main

    root = plugin(tmp_path)
    assert main(["plugin", "vendor", str(root), "--check"]) == 1
    assert main(["plugin", "vendor", str(root)]) == 0
    assert (root / "scripts" / "iki_action.py").read_bytes() == action_helper.source()
    assert main(["plugin", "vendor", str(root), "--check"]) == 0


def test_first_party_scripts_read_and_write_runs_with_the_helper() -> None:
    """Every first-party and example plugin's scripts use the helper at this version, and
    none reads IKI_INPUT, writes IKI_OUTPUT or compares a type by its name by hand."""
    homes = [
        *sorted((ROOT / "plugins").iterdir()),
        *sorted((ROOT / "examples" / "plugins").iterdir()),
        ROOT / "plugin-template",
    ]
    by_hand: list[str] = []
    for home in homes:
        scripts = home / "scripts"
        if not (home / "plugin.yaml").is_file() or not scripts.is_dir():
            continue
        uses = False
        for script in sorted(scripts.rglob("*.py")):
            if script.name == action_helper.FILE_NAME:
                continue
            text = script.read_text()
            where = script.relative_to(ROOT).as_posix()
            if "IKI_INPUT" in text or "IKI_OUTPUT" in text:
                by_hand.append(f"{where} reads or writes the run by hand")
            if BY_NAME.search(text):
                by_hand.append(f"{where} compares a type by name: use iki.is_a")
            uses = uses or "iki_action" in text
        copy = scripts / action_helper.FILE_NAME
        if uses and (not copy.is_file() or copy.read_bytes() != action_helper.source()):
            by_hand.append(f"{copy.relative_to(ROOT)} isn't this Iki's helper")
    assert by_hand == []
