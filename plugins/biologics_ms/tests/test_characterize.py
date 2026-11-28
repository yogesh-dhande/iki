"""Test scientific edge cases with explicitly synthetic peptide and material records."""

import runpy
from pathlib import Path

import pytest

SCRIPT = runpy.run_path(str(Path(__file__).parents[1] / "scripts/characterize.py"))


def test_attachment_residue_has_no_free_water():
    # Hex is incorporated as C6H10O5 in Unimod, https://www.unimod.org/masses.html.
    # AME2020 Table I, https://doi.org/10.1088/1674-1137/abddaf.
    composition = {"hex": 1, "hexnac": 0, "dhex": 0, "neuac": 0, "neugc": 0}
    assert SCRIPT["glycan_mass"](composition, "monoisotopic") == pytest.approx(
        162.0528234, abs=1e-6
    )


def test_overlapping_peptides_count_each_position_once(tmp_path):
    source = tmp_path / "synthetic.tsv"
    source.write_text(
        'chain\tstart\tsequence\tmodifications\nheavy\t1\tACM\t""\nheavy\t2\tCMD\t2:UNIMOD:35\n'
    )
    got = SCRIPT["peptide_rows"](source, {"heavy": "ACMDG"})
    assert got["coverage"] == [{"chain": "heavy", "covered": 4, "length": 5, "percent": 80.0}]
    assert got["peptides"][1]["modifications"] == [
        {"name": "UNIMOD:35", "chain_position": 3, "site": "residue"}
    ]


def test_peptide_termini_and_the_first_residue_keep_separate_sites(tmp_path):
    source = tmp_path / "synthetic.tsv"
    source.write_text(
        "chain\tstart\tsequence\tmodifications\nheavy\t2\tKAP\t0:UNIMOD:1;1:UNIMOD:1;4:UNIMOD:2\n"
    )
    result = SCRIPT["peptide_rows"](source, {"heavy": "AKAPG"})
    assert result["coverage"][0]["covered"] == 3
    assert result["peptides"][0]["modifications"] == [
        {"name": "UNIMOD:1", "chain_position": 2, "site": "peptide_n_term"},
        {"name": "UNIMOD:1", "chain_position": 2, "site": "residue"},
        {"name": "UNIMOD:2", "chain_position": 4, "site": "peptide_c_term"},
    ]


@pytest.mark.parametrize("analysis", ["mass", "peptide"])
@pytest.mark.parametrize("blank_lines", ["", "\n\n"])
def test_header_without_observations_is_refused(tmp_path, analysis, blank_lines):
    source = tmp_path / "empty.tsv"
    if analysis == "mass":
        headings = ["name", "analyte", "observed_da", "disulfides", *SCRIPT["GLYCANS"]]
    else:
        headings = ["chain", "start", "sequence", "modifications"]
    source.write_text("\t".join(headings) + "\n" + blank_lines)
    with pytest.raises(ValueError, match="The report has no observations"):
        if analysis == "mass":
            SCRIPT["mass_rows"](source, {}, {"heavy": "ACM"}, 1, "average", 10)
        else:
            SCRIPT["peptide_rows"](source, {"heavy": "ACM"})


def test_an_adc_lot_must_belong_to_the_selected_conjugate():
    antibody = {"id": "antibody"}
    adc = {"id": "adc", "values": {"carrier": [{"kind": "antibody_drug", "molecule": "antibody"}]}}
    lot = {
        "id": "lot",
        "type": "iki/lots.ConjugateLot",
        "types": [
            "iki/lots.ConjugateLot",
            "iki/lots.BiologicLot",
            "iki/lots.Lot",
            "iki/core.Entity",
        ],
        "roles": {"material": "conjugate"},
        "values": {"conjugate": "different"},
    }
    with pytest.raises(ValueError, match="Select a lot"):
        SCRIPT["check_material"](antibody, adc, lot)
    lot["values"]["conjugate"] = "adc"
    SCRIPT["check_material"](antibody, adc, lot)


def lot_of(type_name, *extends, **values):
    """A lot of a lab's type extending Iki's protein or conjugate lot: what it is of is the
    field its material role names, which its type inherits."""
    material = "conjugate" if "iki/lots.ConjugateLot" in extends else "protein"
    return {
        "id": "lot",
        "type": type_name,
        "types": [type_name, *extends, "iki/lots.BiologicLot", "iki/lots.Lot", "iki/core.Entity"],
        "roles": {"material": material},
        "values": values,
    }


def test_a_labs_own_kind_of_lot_is_a_lot_of_its_antibody_or_conjugate():
    """A lab's lot types that extend the protein or conjugate lot, as the lot input offers
    them, are checked by what they are of, through the material role their type inherits,
    not by their type's name."""
    antibody = {"id": "antibody"}
    adc = {"id": "adc", "values": {"carrier": [{"kind": "antibody_drug", "molecule": "antibody"}]}}
    gmp = lot_of("acme/gmp.DrugSubstanceLot", "iki/lots.ProteinLot", protein="antibody")
    SCRIPT["check_material"](antibody, None, gmp)
    conjugated = lot_of("acme/gmp.ADCLot", "iki/lots.ConjugateLot", conjugate="adc")
    SCRIPT["check_material"](antibody, adc, conjugated)
    with pytest.raises(ValueError, match=r"Select a lot of the chosen antibody\.$"):
        SCRIPT["check_material"](antibody, None, conjugated)
    with pytest.raises(ValueError, match="Select a lot of the chosen antibody–drug conjugate"):
        SCRIPT["check_material"](antibody, adc, gmp)
    gmp["values"]["protein"] = "another antibody"
    with pytest.raises(ValueError, match=r"Select a lot of the chosen antibody\.$"):
        SCRIPT["check_material"](antibody, None, gmp)
