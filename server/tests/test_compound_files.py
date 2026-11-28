"""A compound as a Molfile and as an SDF record (ADR 0229), as BIOVIA's CTfile Formats
(2020) set them out, read back by RDKit, an independent reader. The structure is the
Molfile the compound keeps, which RDKit wrote on a worker; Iki only names it, says which
record it is, and adds its data."""

from typing import Any

import pytest
from rdkit import Chem
from rdkit.Chem import rdDepictor

from iki_server.entities.compound_files import CompoundFile, NotAMolfile, molfile, sdf

ASPIRIN = "CC(=O)Oc1ccccc1C(=O)O"
ASPIRIN_KEY = "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"


def drawn(smiles: str, *, v3000: bool = False) -> str:
    mol = Chem.MolFromSmiles(smiles)
    rdDepictor.SetPreferCoordGen(True)
    rdDepictor.Compute2DCoords(mol)
    return str(Chem.MolToMolBlock(mol, forceV3000=v3000))


def record(**given: Any) -> CompoundFile:
    values: dict[str, Any] = {
        "hrid": "CPD-000007",
        "uuid": "0190e9a4-0000-7000-8000-000000000007",
        "name": "aspirin",
        "revision": 3,
        "structure": drawn(ASPIRIN),
        **given,
    }
    return CompoundFile(**values)


def test_a_molfile_is_named_and_says_which_record_it_is() -> None:
    written = molfile(record())
    text = written.content.decode()
    lines = text.split("\n")
    # The header block: its name, the program line RDKit wrote, and a comment.
    assert lines[0] == "aspirin"
    assert "RDKit" in lines[1] and lines[1][20:22] == "2D"
    assert lines[2] == "Exported from Iki: CPD-000007 (revision 3)."
    assert lines[3].endswith("V2000")
    assert text.endswith("M  END\n")
    mol = Chem.MolFromMolBlock(text)
    assert Chem.MolToInchiKey(mol) == ASPIRIN_KEY  # type: ignore[no-untyped-call]
    assert mol.GetProp("_Name") == "aspirin"
    assert written.left_out == []


def test_a_v3000_molfile_is_kept_as_v3000() -> None:
    text = molfile(record(structure=drawn(ASPIRIN, v3000=True))).content.decode()
    assert text.split("\n")[3].rstrip().endswith("V3000")
    assert "M  V30 BEGIN CTAB" in text
    assert Chem.MolToInchiKey(Chem.MolFromMolBlock(text)) == ASPIRIN_KEY  # type: ignore[no-untyped-call]


def test_a_name_longer_than_the_header_line_holds_is_cut_saying_so() -> None:
    name = "2-acetoxybenzoic acid, as the lab's oldest stock bottle in the cold room labels it"
    written = molfile(record(name=name))
    assert written.content.decode().split("\n")[0] == name[:80].rstrip()
    assert written.left_out == [
        f"Its name is cut to the 80 characters a Molfile's first line holds: {name}."
    ]


@pytest.mark.parametrize(
    ("structure", "why"),
    [
        ("", "doesn't end with M  END"),
        ("aspirin\n\n\n", "doesn't end with M  END"),
        ("x\ny\nz\n  0  0\nM  END\n", "says no V2000 or V3000"),
    ],
)
def test_a_structure_that_isnt_a_molfile_isnt_written(structure: str, why: str) -> None:
    with pytest.raises(NotAMolfile, match=why):
        molfile(record(structure=structure))


def test_an_sdf_record_is_its_molfile_its_identity_its_data_and_its_end() -> None:
    written = sdf(record(data=(("name", "aspirin"), ("molecular_weight", "180.159"))))
    text = written.content.decode()
    _, rest = text.split("M  END\n")
    assert rest == (
        ">  <IKI_HRID>\nCPD-000007\n\n"
        ">  <IKI_UUID>\n0190e9a4-0000-7000-8000-000000000007\n\n"
        ">  <IKI_REVISION>\n3\n\n"
        ">  <name>\naspirin\n\n"
        ">  <molecular_weight>\n180.159\n\n"
        "$$$$\n"
    )
    supplier = Chem.SDMolSupplier()
    supplier.SetData(text)
    (mol,) = list(supplier)
    assert mol.GetPropsAsDict() == {
        "IKI_HRID": "CPD-000007",
        "IKI_UUID": "0190e9a4-0000-7000-8000-000000000007",
        "IKI_REVISION": 3,
        "name": "aspirin",
        "molecular_weight": 180.159,
    }


def test_what_an_sd_value_cant_hold_is_left_out_and_noted() -> None:
    written = sdf(
        record(
            data=(
                ("description", "First line.\n\nAfter a blank line."),
                ("notes", "fine\n$$$$\nthe end?"),
                ("comment", "> <name>\nnot a header"),
            )
        )
    )
    supplier = Chem.SDMolSupplier()
    supplier.SetData(written.content.decode())
    (mol,) = list(supplier)
    assert mol.GetProp("description") == "First line.\nAfter a blank line."
    assert not mol.HasProp("notes") and not mol.HasProp("comment")
    assert written.left_out == [
        "Its description's blank lines are left out: a blank line ends an SD item's value.",
        "Left out its notes: a line of it would read as the end of the record or another "
        "item's header.",
        "Left out its comment: a line of it would read as the end of the record or another "
        "item's header.",
    ]
    assert mol.GetProp("IKI_NOTES") == "\n".join(written.left_out)


def test_records_one_after_another_are_one_sdf() -> None:
    text = (sdf(record()).content + sdf(record(hrid="CPD-000008", name="other")).content).decode()
    supplier = Chem.SDMolSupplier()
    supplier.SetData(text)
    assert [m.GetProp("IKI_HRID") for m in supplier] == ["CPD-000007", "CPD-000008"]
