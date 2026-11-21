"""Where primers bind, and what a PCR amplifies, on real vectors and with the real M13/pUC
primers, run as a worker runs the plugin's commands.

The primers are NEB's M13/pUC sequencing primers, the pair every pUC19 polylinker is read
and screened with: M13/pUC forward (-20), 5'-GTAAAACGACGGCCAGT-3', and M13/pUC reverse,
5'-CAGGAAACAGCTATGAC-3' (New England Biolabs #S1201S and #S1233S). Their product over
pUC19's polylinker is checked against the bases of the record itself (L09137.2), and their
melting temperatures against Biopython's own ``MeltingTemp.Tm_NN`` under the conditions the
plugin states.
"""

import importlib
import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from Bio.Seq import Seq
from Bio.SeqUtils import MeltingTemp
from test_plasmid_annotation import read

from iki_spec import schemas

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "plugins" / "plasmid" / "scripts"
SCRIPT = SCRIPTS / "plasmids.py"

#: NEB's M13/pUC sequencing primers.
M13_FORWARD = "GTAAAACGACGGCCAGT"
M13_REVERSE = "CAGGAAACAGCTATGAC"


@pytest.fixture(scope="module")
def priming() -> Iterator[ModuleType]:
    """The plugin's priming module, imported as a run imports it (by bare name)."""
    names = ("priming", "orfs", "cloning", "library")
    kept = {n: sys.modules.pop(n) for n in names if n in sys.modules}
    sys.path.insert(0, str(SCRIPTS))
    try:
        yield importlib.import_module("priming")
    finally:
        sys.path.remove(str(SCRIPTS))
        for n in names:
            sys.modules.pop(n, None)
        sys.modules.update(kept)


def command(tmp_path: Path, which: str, run: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Runs the plugin's command as a worker would, and gives its output and its log."""
    (tmp_path / "input.json").write_text(json.dumps(run))
    result = subprocess.run(  # noqa: S603 - our own script
        [sys.executable, str(SCRIPT), which],
        env={"IKI_INPUT": str(tmp_path / "input.json"), "IKI_OUTPUT": str(tmp_path / "out.json")},
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    if result.returncode != 0:
        return {}, result.stdout + result.stderr
    output: dict[str, Any] = json.loads((tmp_path / "out.json").read_text())
    assert not list(schemas.check("run-output", output))
    return output, result.stdout


def primer(n: int, bases: str, name: str) -> dict[str, Any]:
    return {
        "id": f"00000000-0000-0000-0000-{n:012d}",
        "hrid": f"PRM-{n:06d}",
        "values": {"bases": bases, "name": name},
    }


def given(bases: str, *, topology: str = "circular") -> dict[str, Any]:
    return {
        "id": "00000000-0000-0000-0000-000000000011",
        "hrid": "DNA-000001",
        "values": {"bases": bases, "topology": topology, "strandedness": "double"},
    }


def plasmid(sequence: dict[str, Any]) -> dict[str, Any]:
    values = sequence["values"]
    return {
        "id": "00000000-0000-0000-0000-000000000001",
        "hrid": "PLS-000001",
        "values": {
            "name": "pUC19",
            "length": len(values["bases"]),
            "topology": values["topology"],
        },
        "included": {"sequence": [sequence]},
    }


def reaction(
    template: dict[str, Any],
    forward: dict[str, Any],
    reverse: dict[str, Any],
    *,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "inputs": {
            "pcr": [
                {
                    "id": "00000000-0000-0000-0000-000000000021",
                    "hrid": "PCR-000001",
                    "values": {"name": "lacZ-alpha"},
                    "included": {
                        "template": [template],
                        "forward_primer": [forward],
                        "reverse_primer": [reverse],
                    },
                }
            ]
        },
        "params": params or {},
    }


def pair() -> tuple[dict[str, Any], dict[str, Any]]:
    return (
        primer(1, M13_FORWARD, "M13/pUC forward (-20)"),
        primer(2, M13_REVERSE, "M13/pUC reverse"),
    )


# --- where the primers bind ----------------------------------------------------------


def test_the_m13_primers_bind_puc19_where_its_bases_put_them(priming: ModuleType) -> None:
    """Each primer is found once on pUC19, on the strand its bases belong to, at the very
    place a plain search of the record's bases puts it: the forward primer's own bases on
    the forward strand, and the reverse primer's reverse complement there."""
    bases, _, _ = read("L09137.2")
    forward, reverse = pair()
    sites = priming.find_sites(
        bases,
        True,
        [
            {"hrid": p["hrid"], "name": p["values"]["name"], "bases": p["values"]["bases"]}
            for p in (forward, reverse)
        ],
    )
    assert bases.count(M13_FORWARD) == 1
    assert bases.count(str(Seq(M13_REVERSE).reverse_complement())) == 1  # type: ignore[no-untyped-call]
    at_forward = bases.index(M13_FORWARD) + 1
    at_reverse = bases.index(str(Seq(M13_REVERSE).reverse_complement())) + 1  # type: ignore[no-untyped-call]
    assert [(s.hrid, s.strand, s.start, s.end, s.three_prime) for s in sites] == [
        ("PRM-000001", "forward", at_forward, at_forward + 16, at_forward + 16),
        ("PRM-000002", "reverse", at_reverse, at_reverse + 16, at_reverse),
    ]
    assert all(s.annealed == 17 and s.tail == 0 and not s.mismatches for s in sites)


def test_a_sites_tm_is_biopythons_under_the_conditions_the_plugin_states(
    priming: ModuleType,
) -> None:
    """A site's melting temperature is SantaLucia's nearest-neighbour Tm as Biopython
    computes it with its default table, at 50 mM Na+ with no Mg2+ or dNTPs and 25 nM of
    each strand: the same conditions ``iki/dna`` gives a primer's own Tm under, so a
    primer's record and its site on a template agree where it matches exactly."""
    for bases in (M13_FORWARD, M13_REVERSE):
        mine = priming.melting_temperature(bases)
        theirs = MeltingTemp.Tm_NN(  # type: ignore[no-untyped-call]
            bases, Na=50, K=0, Tris=0, Mg=0, dNTPs=0, dnac1=25, dnac2=25, saltcorr=5
        )
        assert mine == round(float(theirs), 1)
    assert priming.melting_temperature(M13_FORWARD) == 49.2
    assert priming.melting_temperature(M13_REVERSE) == 43.6


def test_a_mismatch_at_the_3_prime_end_is_no_site_and_one_away_from_it_is(
    priming: ModuleType,
) -> None:
    """The 3' end is the end a polymerase extends from, so a mismatch there weighs 3 and
    one further away weighs 1, and a site is kept where they weigh no more than the run
    allows. Under the default of 2, the M13 forward primer with its last base changed
    binds nowhere, and the same primer with a base changed 13 from its 3' end still binds
    -- with that mismatch recorded, and a melting temperature lowered by it."""
    bases, _, _ = read("L09137.2")
    at_end = M13_FORWARD[:-1] + "A"
    away = M13_FORWARD[:4] + "C" + M13_FORWARD[5:]
    assert M13_FORWARD[-1] != "A"
    assert M13_FORWARD[4] == "A"
    none = priming.find_sites(bases, True, [{"hrid": "PRM-X", "name": "3' end", "bases": at_end}])
    assert none == []
    found = priming.find_sites(bases, True, [{"hrid": "PRM-Y", "name": "5' half", "bases": away}])
    assert len(found) == 1
    site = found[0]
    assert site.mismatches == ((13, "C", "A"),)
    assert site.penalty == 1
    assert site.annealed == 17
    assert site.tm is not None
    assert site.tm < priming.melting_temperature(M13_FORWARD)
    # A run that allows a 3'-terminal mismatch (weight 3) finds that site too.
    allowed = priming.find_sites(
        bases, True, [{"hrid": "PRM-X", "name": "3' end", "bases": at_end}], most_penalty=3
    )
    assert [(s.start, s.mismatches) for s in allowed] == [(379, ((1, "A", "T"),))]


def test_a_site_crosses_a_circles_origin_and_never_a_lines_ends(priming: ModuleType) -> None:
    """A circle has no ends, so a primer may pair across its origin -- the site is reported
    from its highest base round to its lowest. The same bases as a linear molecule have two
    ends, and there the primer pairs nowhere: beyond an end there is nothing to pair with.
    """
    bases, _, _ = read("L09137.2")
    # Written from four bases before the forward primer's site, so the primer straddles it.
    at = bases.index(M13_FORWARD)
    rotated = bases[at + 4 :] + bases[: at + 4]
    one = {"hrid": "PRM-000001", "name": "M13/pUC forward (-20)", "bases": M13_FORWARD}
    circle = priming.find_sites(rotated, True, [one])
    assert [(s.start, s.end, s.strand) for s in circle] == [(len(bases) - 3, 13, "forward")]
    assert circle[0].annealed == 17
    assert priming.find_sites(rotated, False, [one]) == []


# --- the binding-site run ------------------------------------------------------------


def test_the_run_tabulates_each_site_and_says_how_it_found_them(tmp_path: Path) -> None:
    bases, _, _ = read("L09137.2")
    forward, reverse = pair()
    output, log = command(
        tmp_path,
        "primer_sites",
        {
            "inputs": {"plasmid": [plasmid(given(bases))], "primers": [forward, reverse]},
            "params": {"least_three_prime_match": 15, "most_mismatch_weight": 2},
        },
    )
    values = output["updates"]["plasmid"][0]["values"]
    sites = values["primer_binding_sites"]
    assert [(s["primer"], s["strand"], s["start"], s["end"]) for s in sites] == [
        ("PRM-000001", "forward", 379, 395),
        ("PRM-000002", "reverse", 465, 481),
    ]
    assert sites[0]["name"] == "M13/pUC forward (-20)"
    assert sites[0]["mismatches"] == []
    assert sites[0]["melting_temperature"] == 49.2
    assert values["primer_binding_sites_found_with"] == (
        "placed by their 15 3' bases, mismatches weighing at most 2 (3 within the last 5 "
        "bases, 1 beyond); Tm by SantaLucia 1998 nearest-neighbour (Biopython Tm_NN, "
        "DNA_NN3), 50 mM Na+, no Mg2+ or dNTPs, 25 nM of each strand"
    )
    assert output["metrics"] == {"sites": 2, "exact": 2}
    assert "2 binding sites of 2 primers (2 exact)" in log


def test_a_primer_that_binds_nowhere_is_said_rather_than_left_out(tmp_path: Path) -> None:
    bases, _, _ = read("L09137.2")
    forward, _ = pair()
    stranger = primer(3, "TTTTTTTTTTTTTTTTTTTTACGGCGTATAG", "not on this plasmid")
    output, _ = command(
        tmp_path,
        "primer_sites",
        {
            "inputs": {"plasmid": [plasmid(given(bases))], "primers": [forward, stranger]},
            "params": {},
        },
    )
    found = output["updates"]["plasmid"][0]["values"]["primer_binding_sites"]
    assert [s["primer"] for s in found] == ["PRM-000001"]
    said = output["messages"][0]
    assert said["level"] == "warning"
    assert "PRM-000003 binds nowhere" in said["text"]


# --- the product ---------------------------------------------------------------------


def test_the_m13_pair_amplifies_puc19s_polylinker(tmp_path: Path) -> None:
    """The M13/pUC pair amplifies the polylinker of pUC19: a 103 bp product, which is the
    stretch of the record from the forward primer's first base to the reverse primer's
    last, and the bases of the record itself. Both primers match exactly, so the product
    is the template's own bases there.
    """
    bases, _, _ = read("L09137.2")
    forward, reverse = pair()
    output, log = command(tmp_path, "pcr", reaction(given(bases), forward, reverse))
    made = output["creates"][0]
    assert made["type"] == "iki/dna.DNASequence"
    assert made["derived_from"] == ["00000000-0000-0000-0000-000000000021"]
    assert made["values"]["topology"] == "linear"
    assert made["values"]["strandedness"] == "double"
    expected = bases[378:481]
    assert made["values"]["bases"] == expected
    assert len(expected) == 103
    assert expected.startswith(M13_FORWARD)
    assert expected.endswith(str(Seq(M13_REVERSE).reverse_complement()))  # type: ignore[no-untyped-call]
    # The polylinker it spans: pUC19's EcoRI site and its HindIII site are both in it.
    assert "GAATTC" in expected
    assert "AAGCTT" in expected
    values = output["updates"]["pcr"][0]["values"]
    # The product is the reaction's in the graph, derived from it, as an assembly's is.
    assert "product" not in values
    assert values["product_length"] == 103
    assert values["forward_melting_temperature"] == 49.2
    assert values["reverse_melting_temperature"] == 43.6
    assert values["products_found"] == 1
    assert values["tm_method"].startswith("SantaLucia 1998 nearest-neighbour")
    assert output["metrics"] == {"products": 1, "length": 103}
    assert "amplify 1 molecule" in log


def test_both_primers_5_prime_tails_are_in_the_product(tmp_path: Path) -> None:
    """A tail is what a primer adds: the bases of its 5' end that the template hasn't got,
    which is how a restriction site or an overlap is put on a fragment. The product has
    them whole, so a pair with ten extra bases each gives a product twenty base pairs
    longer than the same pair without them, and begins and ends with the tails.
    """
    bases, _, _ = read("L09137.2")
    forward = primer(1, "GCGCGAATTC" + M13_FORWARD, "forward with an EcoRI tail")
    reverse = primer(2, "GCGCGGATCC" + M13_REVERSE, "reverse with a BamHI tail")
    output, log = command(tmp_path, "pcr", reaction(given(bases), forward, reverse))
    product = output["creates"][0]["values"]["bases"]
    assert len(product) == 123
    assert product.startswith("GCGCGAATTC" + M13_FORWARD)
    assert product.endswith(str(Seq("GCGCGGATCC" + M13_REVERSE).reverse_complement()))  # type: ignore[no-untyped-call]
    assert product[10:-10] == bases[378:481]
    assert "10 bp 5' tail" in log


def test_a_product_may_cross_a_circular_templates_origin(tmp_path: Path) -> None:
    """A circle has no ends, so a product may run across its origin, and it is the same
    molecule as the one amplified from the same bases written from another base: pUC19
    written from inside the polylinker gives the very same 103 bp product."""
    bases, _, _ = read("L09137.2")
    forward, reverse = pair()
    rotated = bases[420:] + bases[:420]
    across, log = command(tmp_path, "pcr", reaction(given(rotated), forward, reverse))
    straight, _ = command(tmp_path, "pcr", reaction(given(bases), forward, reverse))
    assert across["creates"][0]["values"]["bases"] == straight["creates"][0]["values"]["bases"]
    assert across["updates"]["pcr"][0]["values"]["product_length"] == 103
    assert "across the template's origin" in log
    assert "across its origin" in across["creates"][0]["values"]["description"]


def test_a_linear_template_is_amplified_between_its_ends(tmp_path: Path) -> None:
    """A linear molecule has two ends, so only a pair with the forward primer to the left
    of the reverse one amplifies anything: the 300 bp of pUC19 around its polylinker, as a
    linear fragment, gives the same 103 bp product."""
    bases, _, _ = read("L09137.2")
    forward, reverse = pair()
    fragment = bases[300:600]
    output, _ = command(
        tmp_path, "pcr", reaction(given(fragment, topology="linear"), forward, reverse)
    )
    assert output["creates"][0]["values"]["bases"] == bases[378:481]
    assert output["creates"][0]["values"]["topology"] == "linear"


# --- what it refuses -----------------------------------------------------------------


def test_a_primer_that_doesnt_bind_the_template_is_refused_by_name(tmp_path: Path) -> None:
    bases, _, _ = read("L09137.2")
    forward, _ = pair()
    stranger = primer(3, "TTTTTTTTTTTTTTTTTTTTACGGCGTATAG", "not on this plasmid")
    output, log = command(tmp_path, "pcr", reaction(given(bases), forward, stranger))
    assert output == {}
    assert "::error PRM-000003 doesn't bind DNA-000001" in log
    assert "allow more mismatches" in log


def test_primers_the_wrong_way_round_are_refused_with_what_to_do(tmp_path: Path) -> None:
    """A PCR's two primers bind opposite strands and point at each other. Given the pair
    swapped -- the reverse primer named as the forward one -- the run says which primer
    binds which strand and to swap them, rather than silently amplifying nothing."""
    bases, _, _ = read("L09137.2")
    forward, reverse = pair()
    output, log = command(tmp_path, "pcr", reaction(given(bases), reverse, forward))
    assert output == {}
    assert "::error PRM-000002 binds DNA-000001 only on the reverse strand" in log
    assert "Swap the forward and reverse primers." in log


def test_several_products_register_none_and_say_which(tmp_path: Path) -> None:
    """A pair that amplifies more than one molecule doesn't say which it means, so nothing
    is registered and the run lists them: here a template carrying the forward primer's
    site twice, which gives two products from the one reverse site.
    """
    bases, _, _ = read("L09137.2")
    forward, reverse = pair()
    # The forward primer's site again, 200 bases before the one pUC19 has.
    at = bases.index(M13_FORWARD)
    twice = bases[: at - 200] + M13_FORWARD + bases[at - 200 + 17 :]
    output, log = command(tmp_path, "pcr", reaction(given(twice), forward, reverse))
    assert "creates" not in output
    values = output["updates"]["pcr"][0]["values"]
    assert values["products_found"] == 2
    assert values["product_length"] is None
    assert values["forward_melting_temperature"] is None
    said = output["messages"][0]
    assert said["level"] == "warning"
    assert "amplify 2 molecules of this template (103 bp, 303 bp)" in said["text"]
    assert "none is registered" in said["text"]
    assert output["metrics"]["products"] == 2
    assert "amplify 2 molecules" in log


def test_a_reaction_worked_out_again_gives_the_same_molecule(tmp_path: Path) -> None:
    """Working a reaction out again, after its primers or its template were corrected or
    for nothing at all, gives the product of what it now says: the same bases, which the
    registry gives back as the entry it has, so a rerun registers nothing twice."""
    bases, _, _ = read("L09137.2")
    forward, reverse = pair()
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    first, _ = command(tmp_path / "a", "pcr", reaction(given(bases), forward, reverse))
    again, _ = command(tmp_path / "b", "pcr", reaction(given(bases), forward, reverse))
    assert first["creates"] == again["creates"]
    assert first["creates"][0]["values"]["bases"] == bases[378:481]
