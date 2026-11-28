"""Run inputs carry their type's lineage and roles, and read only what an action reads
(ADR 0284); a sequence names the field holding its residues (ADR 0285)."""

import shutil
import textwrap
from pathlib import Path

from iki_spec import schemas
from iki_spec.action_tests import ActionTest, run_input
from iki_spec.effective import carried, resolve_one
from iki_spec.model import BUILTIN_TYPES
from iki_spec.validate import Result, load_plugin

PLUGIN = """\
apiVersion: iki/v1
kind: Plugin
name: acme/lab
version: 1.0.0
iki: ">=0.1"
"""

#: A lab's protein, a GMP lot of one under a lot, and an action that checks a lot is of
#: the protein it was given: the lot's material is read by its role, not its type's name.
TYPES = {
    "protein.yaml": """\
        apiVersion: iki/v1
        kind: EntityType
        name: Protein
        extends: iki/core.Sequence
        schema_version: 1
        hrid: { prefix: PRT }
        identity: [sequence]
        roles: { residues: sequence }
        fields:
          sequence: { type: string, format: protein_sequence, required: true, immutable: true }
          notes: { type: text }
        """,
    "lot.yaml": """\
        apiVersion: iki/v1
        kind: EntityType
        name: Lot
        abstract: true
        schema_version: 1
        fields:
          made_on: { type: date }
        """,
    "protein_lot.yaml": """\
        apiVersion: iki/v1
        kind: EntityType
        name: ProteinLot
        extends: Lot
        schema_version: 1
        hrid: { prefix: PLT }
        amount: { unit: mg, material: protein }
        fields:
          protein: { type: ref, to: Protein, required: true, immutable: true }
          supplier_note: { type: text }
        """,
    "gmp_lot.yaml": """\
        apiVersion: iki/v1
        kind: EntityType
        name: GmpProteinLot
        extends: ProteinLot
        schema_version: 1
        hrid: { prefix: GMP }
        fields:
          batch_record: { type: string, max_length: 40 }
        """,
}

ACTION = """\
apiVersion: iki/v1
kind: Action
name: check_lot
inputs:
  lot: { type: ProteinLot, reads: [made_on] }
  protein: { type: Protein }
effects:
  updates: [{ input: lot, fields: [made_on] }]
runs_on: { tier: backend, environment: tools }
command: [python, ./check.py]
"""

ENVIRONMENT = """\
apiVersion: iki/v1
kind: Environment
name: tools
tiers: [backend]
resources: { cpu: 0.5, memory: 512Mi }
python: { version: "3.12", requirements: requirements.txt }
"""


def make(tmp_path: Path, types: dict[str, str], *, action: str = ACTION) -> Result:
    root = tmp_path / "acme"
    shutil.rmtree(root, ignore_errors=True)
    for folder in ("types", "actions", "environments"):
        (root / folder).mkdir(parents=True)
    (root / "plugin.yaml").write_text(PLUGIN)
    for name, text in types.items():
        (root / "types" / name).write_text(textwrap.dedent(text))
    (root / "actions" / "check_lot.yaml").write_text(action)
    (root / "environments" / "tools.yaml").write_text(ENVIRONMENT)
    (root / "check.py").write_text("")
    (root / "requirements.txt").write_text("")
    return load_plugin(root)


def codes(result: Result) -> list[str]:
    return sorted(p.code for p in result.problems if p.severity.name == "ERROR")


def test_a_subtypes_input_carries_its_lineage_and_its_ancestors_roles(tmp_path: Path) -> None:
    loaded = make(tmp_path, TYPES)
    assert codes(loaded) == []
    assert loaded.plugin is not None
    plugin = loaded.plugin
    types = {**BUILTIN_TYPES, **plugin.types}
    test = ActionTest(
        name="a GMP lot is checked as a protein lot",
        file="tests/check_lot.yaml",
        action=plugin.actions["check_lot"],
        inputs={
            "lot": [
                {
                    "type": "acme/lab.GmpProteinLot",
                    "values": {
                        "protein": "p",
                        "made_on": "2026-11-02",
                        "supplier_note": "Shipped cold",
                        "batch_record": "BR-0042",
                    },
                }
            ],
            "protein": [{"values": {"sequence": "MKV", "notes": "From the cloning core"}}],
        },
    )
    document = run_input(test, plugin, types, tmp_path, {}, tmp_path)
    # As a worker is given it, with its lease's job.
    job = {"id": "00000000-0000-7000-8009-000000000001", "timeout": 60, "resources": {}}
    assert not list(schemas.check("run-input", {**document, "job": job}))
    (lot,) = document["inputs"]["lot"]
    assert lot["types"] == [
        "acme/lab.GmpProteinLot",
        "acme/lab.ProteinLot",
        "acme/lab.Lot",
        "iki/core.Entity",
    ]
    # The lot's material is its protein, by the role its parent's amount declares.
    assert lot["roles"] == {"material": "protein"}
    # It reads the date the lot was made, and its material: not its notes or batch record.
    assert lot["values"] == {"protein": "p", "made_on": "2026-11-02"}
    (protein,) = document["inputs"]["protein"]
    assert protein["roles"] == {"residues": {"field": "sequence", "alphabet": "protein"}}
    assert protein["types"][-2:] == ["iki/core.Sequence", "iki/core.Entity"]
    assert set(protein["values"]) == {"sequence", "notes"}


def test_carried_is_what_it_reads_and_what_its_roles_name(tmp_path: Path) -> None:
    loaded = make(tmp_path, TYPES)
    types = {**BUILTIN_TYPES, **(loaded.plugin.types if loaded.plugin else {})}
    gmp = resolve_one(types, "acme/lab.GmpProteinLot")
    assert carried(gmp, ()) is None
    assert carried(gmp, ("made_on",)) == frozenset({"made_on", "protein"})
    assert carried(gmp, ("made_on",), ("protein",)) == frozenset({"made_on", "protein"})


def test_an_input_reads_only_its_types_fields(tmp_path: Path) -> None:
    # A field of the GMP lot isn't every protein lot's.
    loaded = make(
        tmp_path, TYPES, action=ACTION.replace("reads: [made_on]", "reads: [batch_record]")
    )
    assert codes(loaded) == ["reads-unknown-field"]


def test_a_sequence_names_the_field_holding_its_residues(tmp_path: Path) -> None:
    unbound = TYPES["protein.yaml"].replace("        roles: { residues: sequence }\n", "")
    assert codes(make(tmp_path, {**TYPES, "protein.yaml": unbound})) == ["residues-unbound"]
    # Its residues are a string with a sequence format, which is their alphabet.
    notes = TYPES["protein.yaml"].replace("residues: sequence", "residues: notes")
    assert codes(make(tmp_path, {**TYPES, "protein.yaml": notes})) == ["role-field"]
    missing = TYPES["protein.yaml"].replace("residues: sequence", "residues: bases")
    assert codes(make(tmp_path, {**TYPES, "protein.yaml": missing})) == ["unknown-field"]


def test_a_subtype_has_its_parents_roles_and_cant_name_another(tmp_path: Path) -> None:
    mutant = """\
        apiVersion: iki/v1
        kind: EntityType
        name: Mutant
        extends: Protein
        schema_version: 1
        hrid: { prefix: MUT }
        roles: { residues: wild_type }
        fields:
          wild_type: { type: string, format: protein_sequence }
        """
    assert codes(make(tmp_path, {**TYPES, "mutant.yaml": mutant})) == ["role-rebound"]
    inherited = mutant.replace("        roles: { residues: wild_type }\n", "")
    loaded = make(tmp_path, {**TYPES, "mutant.yaml": inherited})
    assert codes(loaded) == []
    assert loaded.plugin is not None
    declared = resolve_one({**BUILTIN_TYPES, **loaded.plugin.types}, "acme/lab.Mutant")
    assert declared.roles == {"residues": "sequence"}
    assert declared.sources["roles.residues"] == "acme/lab.Protein"
