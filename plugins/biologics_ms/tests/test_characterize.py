"""Test scientific edge cases with explicitly synthetic peptide and material records."""

import runpy
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parents[1] / "scripts"
# As the worker runs it: from its own directory, so it finds weights.py beside it.
sys.path.insert(0, str(SCRIPTS))
SCRIPT = runpy.run_path(str(SCRIPTS / "characterize.py"))


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
    lot = {"id": "lot", "type": "iki/lots.ConjugateLot", "values": {"conjugate": "different"}}
    with pytest.raises(ValueError, match="Select a lot"):
        SCRIPT["check_material"](antibody, adc, lot)
    lot["values"]["conjugate"] = "adc"
    SCRIPT["check_material"](antibody, adc, lot)
