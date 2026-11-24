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
    assert got["peptides"][1]["modifications"] == [{"name": "UNIMOD:35", "chain_position": 3}]


def test_an_adc_lot_must_belong_to_the_selected_conjugate():
    antibody = {"id": "antibody"}
    adc = {"id": "adc", "values": {"carrier": [{"kind": "antibody_drug", "molecule": "antibody"}]}}
    lot = {"id": "lot", "type": "iki/lots.ConjugateLot", "values": {"conjugate": "different"}}
    with pytest.raises(ValueError, match="Select a lot"):
        SCRIPT["check_material"](antibody, adc, lot)
    lot["values"]["conjugate"] = "adc"
    SCRIPT["check_material"](antibody, adc, lot)
