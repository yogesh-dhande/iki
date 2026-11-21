"""What the plasmid plugin works out a cloning plan makes, on real vectors: the digests
against Biopython's own, pUC19 cut and ligated back into itself, a gene cloned into its
polylinker, Gibson assembly by terminal homology, and a clone sequenced from another
origin on the other strand recognised as the plan's product.

The plan's own arithmetic is checked against the records in ``data/plasmids/``: pUC19's
EcoRI and HindIII sites are where its published polylinker has them, and every product's
length is the lengths of the fragments that make it.
"""

import importlib
import json
import random
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from Bio import Restriction
from Bio.Seq import Seq
from test_plasmid_annotation import RECORDS, read, reverse_complement

from iki_spec import schemas

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "plugins" / "plasmid" / "scripts"
SCRIPT = SCRIPTS / "plasmids.py"


def read_fasta(accession: str) -> str:
    """A record's bases, from a FASTA file of one record."""
    text = (RECORDS / f"{accession}.fasta").read_text(encoding="utf-8")
    return "".join(line.strip() for line in text.splitlines() if not line.startswith(">")).upper()


@pytest.fixture(scope="module")
def cloning() -> Iterator[ModuleType]:
    """The plugin's cloning module, imported as its runs import it (by bare name), without
    leaving those names to another plugin's scripts of the same name."""
    names = ("cloning", "library")
    kept = {n: sys.modules.pop(n) for n in names if n in sys.modules}
    sys.path.insert(0, str(SCRIPTS))
    try:
        yield importlib.import_module("cloning")
    finally:
        sys.path.remove(str(SCRIPTS))
        for n in names:
            sys.modules.pop(n, None)
        sys.modules.update(kept)


def command(tmp_path: Path, which: str, run: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Runs the plugin's command as a worker would, and gives its output and its log."""
    (tmp_path / "input.json").write_text(json.dumps(run))
    # Without -I, so the script imports its siblings by bare name as a run does.
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


def sequence(
    n: int,
    bases: str,
    *,
    topology: str = "circular",
    name: str = "",
    strandedness: str = "double",
) -> dict[str, Any]:
    return {
        "id": f"00000000-0000-0000-0000-{n:012d}",
        "hrid": f"DNA-{n:06d}",
        "values": {
            "bases": bases,
            "topology": topology,
            "strandedness": strandedness,
            "name": name,
        },
    }


def plan(
    method: str,
    backbone: dict[str, Any],
    inserts: list[dict[str, Any]],
    *,
    enzymes: list[str] | None = None,
    product: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    assembly: dict[str, Any] = {
        "id": "00000000-0000-0000-0000-000000000099",
        "hrid": "ASM-000001",
        "values": {
            "method": method,
            "enzymes": enzymes or [],
            "name": "a plan",
            "product": product["id"] if product else None,
        },
        "included": {"backbone": [backbone], "inserts": inserts},
    }
    if product is not None:
        assembly["included"]["product"] = [product]
    return {"inputs": {"assembly": [assembly]}, "params": params or {}}


# --- the digests ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "bases"),
    [
        ("EcoRI", "AAAGAATTCTTT"),  # a 5' overhang
        ("PstI", "AAACTGCAGTTT"),  # a 3' overhang
        ("SmaI", "AAACCCGGGTTT"),  # blunt
        ("BamHI", "AAAGGATCCTTT"),
        ("BsaI", "AAAGGTCTCACGTACGTTTT"),  # Type IIS, cutting outside its site
    ],
)
def test_a_digest_gives_the_fragments_biopython_gives(
    cloning: ModuleType, name: str, bases: str
) -> None:
    enzyme = getattr(Restriction, name)
    mine = [f.bases for f in cloning.digest("x", bases, False, [name])]
    assert mine == [str(s) for s in enzyme.catalyse(Seq(bases), linear=True)]


def test_an_enzyme_nobody_knows_says_how_to_write_one(cloning: ModuleType) -> None:
    with pytest.raises(cloning.Impossible, match="No restriction enzyme is called EcoR1"):
        cloning.cuts("ACGT", False, ["EcoR1"])


def test_puc19_is_cut_where_its_polylinker_has_its_sites(cloning: ModuleType) -> None:
    """pUC19's polylinker has one EcoRI site and one HindIII site, at the ends of its
    multiple cloning site; EcoRI cuts after base 396 and HindIII after base 447, which
    are the positions pUC19's published map gives for G^AATTC and A^AGCTT."""
    bases, _, _ = read("L09137.2")
    assert bases[395:401] == "GAATTC"
    assert bases[446:452] == "AAGCTT"
    assert cloning.cuts(bases, True, ["EcoRI", "HindIII"]) == [
        (396, -4, ("EcoRI",)),
        (447, -4, ("HindIII",)),
    ]
    fragments = cloning.digest("pUC19", bases, True, ["EcoRI", "HindIII"])
    assert sorted(len(f.bases) for f in fragments) == [51, 2635]
    assert sum(len(f.bases) for f in fragments) == 2686


#: The fragments of a lambda DNA–EcoRI digest, as it is sold as a size marker (New
#: England Biolabs' lambda DNA-EcoRI Digest, and Thermo Fisher's): the classic ladder.
LAMBDA_ECORI = [21226, 7421, 5804, 5643, 4878, 3530]


def test_a_linear_molecule_gives_one_more_fragment_than_it_has_cuts(
    cloning: ModuleType,
) -> None:
    """Wild-type lambda (INSDC J02459.1, 48,502 bp, whose LOCUS line says `linear`) has
    five EcoRI sites, so the digest of the linear genome gives six fragments: the ladder
    the digest is sold as, 21,226 + 7,421 + 5,804 + 5,643 + 4,878 + 3,530 bases.

    The same bases read as a circle -- which lambda becomes in the cell, its cohesive
    ends joined -- give five fragments from the same five cuts: the two end fragments of
    the linear digest are one piece across the join, 21,226 + 3,530 = 24,756 bp. n cuts
    make n + 1 fragments of a linear molecule and n of a circle, and the two ends are the
    difference.
    """
    bases = read_fasta("J02459.1")
    assert len(bases) == 48502
    sites = cloning.cuts(bases, False, ["EcoRI"])
    assert [at for at, _, _ in sites] == [21226, 26104, 31747, 39168, 44972]

    line = cloning.digest("lambda", bases, False, ["EcoRI"])
    assert len(line) == len(sites) + 1 == 6
    assert sorted((len(f.bases) for f in line), reverse=True) == LAMBDA_ECORI
    assert sum(len(f.bases) for f in line) == 48502
    # The two ends of a linear molecule are ends: no enzyme made them, so they are blunt
    # here, and every other end carries EcoRI's AATT.
    assert (str(line[0].left), str(line[-1].right)) == ("blunt", "blunt")
    assert {str(f.right) for f in line[:-1]} == {"5' AATT"}
    assert [f.bases for f in line] == [
        str(s)
        for s in Restriction.EcoRI.catalyse(  # type: ignore[attr-defined]
            Seq(bases), linear=True
        )
    ]

    circle = cloning.digest("lambda", bases, True, ["EcoRI"])
    assert len(circle) == len(sites) == 5
    assert sorted((len(f.bases) for f in circle), reverse=True) == [
        21226 + 3530,
        *sorted(LAMBDA_ECORI[1:-1], reverse=True),
    ]
    assert sum(len(f.bases) for f in circle) == 48502
    assert {str(f.left) for f in circle} == {str(f.right) for f in circle} == {"5' AATT"}


def elucidated_overhang(enzyme: object) -> str:
    """The overhang an enzyme leaves, from Biopython's own ``elucidate()``: the bases
    between its two strands' cuts, whichever comes first. EcoRI's ``G^AATT_C`` leaves
    AATT after the top-strand cut, PstI's ``C_TGCA^G`` leaves TGCA before it."""
    shown: str = enzyme.elucidate()  # type: ignore[attr-defined]
    top, bottom = shown.index("^"), shown.index("_")
    return shown[min(top, bottom) + 1 : max(top, bottom)]


#: Each enzyme with the sequence it is tried on: a site with three bases either side, so
#: both strands' cuts fall inside the molecule. Written out rather than generated, so the
#: site each case means is on the page.
OVERHANG_CASES = [
    ("EcoRI", "AAAGAATTCTTT"),  # 5' AATT
    ("HindIII", "AAAAAGCTTTTT"),  # 5' AGCT
    ("BamHI", "AAAGGATCCTTT"),  # 5' GATC
    ("SalI", "AAAGTCGACTTT"),  # 5' TCGA
    ("PstI", "AAACTGCAGTTT"),  # 3' TGCA
    ("KpnI", "AAAGGTACCTTT"),  # 3' GTAC
    ("SacI", "AAAGAGCTCTTT"),  # 3' AGCT
    ("SphI", "AAAGCATGCTTT"),  # 3' CATG
    ("ApaI", "AAAGGGCCCTTT"),  # 3' GGCC
    ("AatII", "AAAGACGTCTTT"),  # 3' ACGT
    ("SmaI", "AAACCCGGGTTT"),  # blunt
]


@pytest.mark.parametrize(("name", "bases"), OVERHANG_CASES)
def test_an_end_carries_the_overhang_biopython_elucidates(
    cloning: ModuleType, name: str, bases: str
) -> None:
    """Every end's bases are the overhang REBASE gives that enzyme, 5' and 3' alike. A
    3' overhang lies before the top-strand cut, so it is as much a part of the fragment
    on the left as a 5' one is of the fragment on the right, and an end that doesn't
    carry it would let any two 3' cuts of the same length look alike."""
    enzyme = getattr(Restriction, name)
    want = elucidated_overhang(enzyme)
    fragments = cloning.digest("x", bases, False, [name])
    assert len(fragments) == 2
    left, right = fragments
    assert left.right.overhang == right.left.overhang == enzyme.ovhg
    assert left.right.bases == right.left.bases == want
    assert len(want) == abs(enzyme.ovhg)
    # The fragments' top strands are still Biopython's, overhang bases and all.
    assert [f.bases for f in fragments] == [
        str(s) for s in enzyme.catalyse(Seq(bases), linear=True)
    ]


def test_pstis_overhang_is_the_same_wherever_the_origin_is(cloning: ModuleType) -> None:
    """A 3' overhang reads the bases before the cut, which on a circle can be the bases
    before the origin. pUC19's single PstI site cuts after base 440, so rotating the
    record until that cut sits 2 bases in puts half the overhang at the far end of the
    sequence; the end's bases are TGCA either way."""
    bases, _, _ = read("L09137.2")
    assert cloning.cuts(bases, True, ["PstI"]) == [(439, 4, ("PstI",))]
    for at in (0, 437, 438, 439, 1500):
        rotated = bases[at:] + bases[:at]
        (fragment,) = cloning.digest("pUC19", rotated, True, ["PstI"])
        assert len(fragment.bases) == 2686
        assert fragment.left == fragment.right == cloning.End(4, "TGCA")


def test_two_different_3_prime_overhangs_dont_ligate(cloning: ModuleType) -> None:
    """pUC19's polylinker has one KpnI site and one PstI site. Both leave a four-base 3'
    overhang, but KpnI leaves GTAC and PstI TGCA, so neither of the two fragments closes
    on itself and the digest religates to nothing -- the 3' counterpart of the EcoRI and
    HindIII double digest. Cut with PstI alone, the same molecule closes again."""
    bases, _, _ = read("L09137.2")
    both = cloning.digest("pUC19", bases, True, ["KpnI", "PstI"])
    assert sorted(len(f.bases) for f in both) == [27, 2659]
    assert {str(f.left) for f in both} == {str(f.right) for f in both} == {"3' GTAC", "3' TGCA"}
    assert cloning.ligate([both]) == []
    # One enzyme, one overhang: the circle comes back, and the junction names it.
    alone = cloning.digest("pUC19", bases, True, ["PstI"])
    (made,) = cloning.ligate([alone])
    assert cloning.same_molecule(made.bases, bases)
    assert [j["overhang"] for j in made.junctions] == ["3' TGCA"]


def test_a_fragment_turned_round_twice_is_itself_again(cloning: ModuleType) -> None:
    """Every geometry a digest makes -- blunt, 5' and 3' ends in each combination -- comes
    back unchanged after being turned round twice, so the two strands' stretches survive
    the turn. A 5' overhang belongs to the strand that carries it, so the other strand is
    not the same length as the top one when the two ends are of different kinds."""
    bases = "AAAGAATTCAAACCCGGGTTTCTGCAGTTT"
    fragments = cloning.digest("x", bases, False, ["EcoRI", "SmaI", "PstI"])
    assert [(str(f.left), str(f.right)) for f in fragments] == [
        ("blunt", "5' AATT"),
        ("5' AATT", "blunt"),
        ("blunt", "3' TGCA"),
        ("3' TGCA", "blunt"),
    ]
    for fragment in fragments:
        assert fragment.reversed().reversed() == fragment
    # A 5' end on the left and a 3' end on the right are both on the top strand, so the
    # other strand is eight bases shorter; the other way round, eight longer.
    both_on_top = cloning.Fragment(
        "x", "AATTCAAACCCGGGTTTCTGCA", cloning.End(-4, "AATT"), cloning.End(4, "TGCA")
    )
    assert len(both_on_top.other_strand()) == len(both_on_top.bases) - 8
    assert both_on_top.reversed().reversed() == both_on_top
    both_below = both_on_top.reversed()
    assert len(both_below.other_strand()) == len(both_below.bases) + 8


def test_both_orientations_of_an_insert_are_whole_molecules(cloning: ModuleType) -> None:
    """The same plan, worked out directly: the two circles differ only in the insert's
    strand, and both keep a GAATTC at each junction. An inverted fragment that kept the
    top strand's own stretch would spell a junction that no longer has the site."""
    puc, _, _ = read("L09137.2")
    _, gene = neo_insert()
    insert = "TT" + "GAATTC" + gene + "GAATTC" + "TT"
    vector = cloning.digest("pUC19", puc, True, ["EcoRI"])
    cut = [
        f
        for f in cloning.digest("neo", insert, False, ["EcoRI"])
        if f.left.sticky and f.right.sticky
    ]
    assert [len(f.bases) for f in cut] == [801]
    made = cloning.ligate([vector, cut])
    assert len(made) == 2
    forward, inverted = (made[0], made[1]) if gene in made[0].bases * 2 else (made[1], made[0])
    for product in (forward, inverted):
        assert len(product.bases) == 2686 + 801 == 3487
        # Both sites are back: GAATTC twice around the circle.
        assert (product.bases + product.bases[:5]).count("GAATTC") == 2
        assert cloning.same_molecule(product.bases, puc) is False
    assert gene in forward.bases * 2
    assert reverse_complement(gene) in inverted.bases * 2
    assert not cloning.same_molecule(forward.bases, inverted.bases)


def test_isoschizomers_cut_once_where_they_agree(cloning: ModuleType) -> None:
    """SacI and SstI recognise GAGCTC and break the same two bonds, so pUC19 digested
    with both gives the one fragment either gives alone, not two. Both names are kept
    against the boundary, because both enzymes make it."""
    bases, _, _ = read("L09137.2")
    assert cloning.cuts(bases, True, ["SacI", "SstI"]) == [(406, 4, ("SacI", "SstI"))]
    (together,) = cloning.digest("pUC19", bases, True, ["SacI", "SstI"])
    (alone,) = cloning.digest("pUC19", bases, True, ["SacI"])
    assert together.bases == alone.bases == bases[406:] + bases[:406]
    assert len(together.bases) == 2686
    assert sorted(set(together.cut_with)) == ["SacI", "SstI"]


# --- the same molecule from any origin ----------------------------------------------


def test_the_least_rotation_is_the_least_rotation(cloning: ModuleType) -> None:
    """Booth's algorithm, against every rotation, on random sequences."""
    rolls = random.Random(20261119)  # noqa: S311 - made-up test data, not secrets
    for _ in range(300):
        bases = "".join(rolls.choice("ACGT") for _ in range(rolls.randint(1, 60)))
        assert cloning.least_rotation(bases) == min(
            bases[i:] + bases[:i] for i in range(len(bases))
        )


def test_puc19_is_the_same_molecule_from_any_origin_and_either_strand(
    cloning: ModuleType,
) -> None:
    bases, _, _ = read("L09137.2")
    for at in (1, 397, 1343, 2685):
        rotated = bases[at:] + bases[:at]
        assert cloning.same_molecule(rotated, bases)
        assert cloning.same_molecule(reverse_complement(rotated), bases)
    # One base changed is another molecule, however it's read.
    changed = ("T" if bases[0] != "T" else "G") + bases[1:]
    assert not cloning.same_molecule(changed, bases)
    assert not cloning.same_molecule(bases[:-1], bases)


def test_a_linear_molecule_is_the_same_from_either_strand_and_not_from_another_origin(
    cloning: ModuleType,
) -> None:
    """pGKL1 (X01095.1), a real linear plasmid: a linear duplex is the lesser of its two
    strands, since both are the molecule read from its other end, and a rotation of it is
    another molecule -- its two ends are somewhere else (ADR 0138). The same bases as a
    circle are the same molecule from any origin, which is what the two topologies
    differ in."""
    bases, _, _ = read("X01095.1")
    assert cloning.same_molecule(bases, bases, circular=False)
    assert cloning.same_molecule(reverse_complement(bases), bases, circular=False)
    for at in (1, 202, 4000, 8673):
        rotated = bases[at:] + bases[:at]
        assert not cloning.same_molecule(rotated, bases, circular=False)
        assert not cloning.same_molecule(reverse_complement(rotated), bases, circular=False)
        # The same bases read as a circle are one molecule from any origin.
        assert cloning.same_molecule(rotated, bases)
    # The canonical form a linear duplex is read by is one of its two strands, as it is.
    assert cloning.canonical(bases, circular=False) in (bases, reverse_complement(bases))


# --- cloning into pUC19 --------------------------------------------------------------

#: The 795 bp neo (NeoR/KanR) coding sequence of pcDNA3.1(+), as its own record gives it
#: at 2136..2930: a real gene to clone into pUC19's polylinker.
NEO_AT = (2136, 2930)


def neo_insert() -> tuple[str, str]:
    """The neo gene with an EcoRI site before it and a HindIII site after, as a primer
    pair would add them, and the gene itself."""
    bases, _, _ = read("MN996867.1")
    gene = bases[NEO_AT[0] - 1 : NEO_AT[1]]
    return "TT" + "GAATTC" + gene + "AAGCTT" + "TT", gene


def test_a_gene_cloned_into_puc19s_polylinker(tmp_path: Path) -> None:
    """pUC19 cut with EcoRI and HindIII keeps its 2,635 bp fragment (the 51 bp piece
    between the two sites goes), and the neo gene cut with the same two gives an 801 bp
    fragment: the 5 bases EcoRI leaves of its site (AATTC), the 795 bp gene, and the one
    base HindIII leaves of its own (A). The product is 3,436 bp, and holds the whole
    gene."""
    puc, _, _ = read("L09137.2")
    insert, gene = neo_insert()
    output, log = command(
        tmp_path,
        "assemble",
        plan(
            "restriction_ligation",
            sequence(1, puc, name="pUC19"),
            [sequence(2, insert, topology="linear", name="neo")],
            enzymes=["EcoRI", "HindIII"],
        ),
    )
    assert output, log
    made = output["creates"][0]
    assert made["type"] == "iki/dna.DNASequence"
    assert made["values"]["topology"] == "circular"
    product = made["values"]["bases"]
    assert len(product) == 2635 + 801 == 3436
    assert output["updates"]["assembly"][0]["values"]["simulated_length"] == 3436
    # What the plan makes is a prediction, so there's nothing to match yet: the clone's
    # sequence is stated as the plan's product afterwards, and `check` compares the two.
    assert "matches_product" not in output["updates"]["assembly"][0]["values"]
    # The whole gene is in it, and so is all of pUC19 but the 51 bases between the sites.
    assert gene in product
    assert puc[451:] + puc[:396] in product + product
    # The plasmid registered beside it is that sequence.
    assert output["creates"][1]["type"] == "iki/plasmid.Plasmid"
    assert output["creates"][1]["values"]["sequence"] == {"$create": "made"}
    # Its junctions are the two sites, each with the overhang that joined them.
    junctions = output["updates"]["assembly"][0]["values"]["junctions"]
    assert sorted(j["overhang"] for j in junctions) == ["5' AATT", "5' AGCT"]
    assert all(j["enzymes"] == ["EcoRI", "HindIII"] for j in junctions)


def test_a_clone_sequenced_from_another_origin_matches_its_plan(tmp_path: Path) -> None:
    """The same cloning plan, checked against the clone's sequence as a sequencing run
    would give it: starting somewhere else and read on the other strand. It's the same
    molecule (ADR 0138), so the plan matches."""
    puc, _, _ = read("L09137.2")
    insert, _ = neo_insert()
    output, log = command(
        tmp_path,
        "assemble",
        plan(
            "restriction_ligation",
            sequence(1, puc, name="pUC19"),
            [sequence(2, insert, topology="linear", name="neo")],
            enzymes=["EcoRI", "HindIII"],
        ),
    )
    assert output, log
    product = output["creates"][0]["values"]["bases"]
    as_sequenced = reverse_complement(product[1700:] + product[:1700])
    checked, log = command(
        tmp_path,
        "check",
        plan(
            "restriction_ligation",
            sequence(1, puc, name="pUC19"),
            [sequence(2, insert, topology="linear", name="neo")],
            enzymes=["EcoRI", "HindIII"],
            product=sequence(3, as_sequenced),
        ),
    )
    assert checked, log
    values = checked["updates"]["assembly"][0]["values"]
    assert values["matches_product"] is True
    assert values["simulated_length"] == len(as_sequenced) == 3436
    assert "the same molecule from any origin and on either strand" in log


def test_a_clone_that_isnt_the_plans_product_is_told_apart(tmp_path: Path) -> None:
    """A clone that kept the 51 bp piece of the polylinker instead of the insert is a
    different molecule of a different length, and the check says so rather than passing
    it."""
    puc, _, _ = read("L09137.2")
    insert, _ = neo_insert()
    checked, log = command(
        tmp_path,
        "check",
        plan(
            "restriction_ligation",
            sequence(1, puc, name="pUC19"),
            [sequence(2, insert, topology="linear", name="neo")],
            enzymes=["EcoRI", "HindIII"],
            product=sequence(3, puc),
        ),
    )
    assert checked, log
    values = checked["updates"]["assembly"][0]["values"]
    assert values["matches_product"] is False
    assert values["simulated_length"] == 3436
    assert "DNA-000003 has 2686 bp, 750 bp apart" in log


def test_puc19_cut_once_and_ligated_back_is_puc19(tmp_path: Path) -> None:
    """Nothing is lost by cutting and religating: pUC19 opened by EcoRI alone closes on
    its own two AATT overhangs, and the circle is pUC19 again — the empty-vector
    background of a ligation with no insert."""
    puc, _, _ = read("L09137.2")
    checked, log = command(
        tmp_path,
        "check",
        plan(
            "restriction_ligation",
            sequence(1, puc, name="pUC19"),
            [],
            enzymes=["EcoRI"],
            product=sequence(3, puc),
        ),
    )
    assert checked, log
    values = checked["updates"]["assembly"][0]["values"]
    assert values["simulated_length"] == 2686
    assert values["matches_product"] is True
    assert [j["overhang"] for j in values["junctions"]] == ["5' AATT"]


def test_a_double_digest_leaves_ends_that_cant_close_on_themselves(tmp_path: Path) -> None:
    """pUC19 cut with EcoRI and HindIII has an AATT overhang at one end and an AGCT one
    at the other, so neither fragment closes on itself: no circle comes out of a ligation
    with no insert, which is the little empty-vector background directional cloning has.
    What the plan makes is then what lies in the tube, the cut vector, a linear molecule
    -- the 2,635 bp band, and with `any`, either band, which the plan doesn't choose
    between."""
    puc, _, _ = read("L09137.2")
    output, log = command(
        tmp_path,
        "assemble",
        plan(
            "restriction_ligation",
            sequence(1, puc, name="pUC19"),
            [],
            enzymes=["EcoRI", "HindIII"],
            params={"backbone_fragment": "any"},
        ),
    )
    assert not output
    assert "make 2 different linear molecules (2635 bp, 51 bp)" in log
    # Its longest fragment, which is the band a vector digest is purified down to, is one
    # linear molecule: the cut vector, registered as a sequence and not as a plasmid.
    output, log = command(
        tmp_path,
        "assemble",
        plan(
            "restriction_ligation",
            sequence(1, puc, name="pUC19"),
            [],
            enzymes=["EcoRI", "HindIII"],
        ),
    )
    assert output, log
    assert "the plan makes a 2635 bp linear molecule" in log
    (made,) = output["creates"]
    assert made["values"]["topology"] == "linear"
    assert len(made["values"]["bases"]) == 2635
    values = output["updates"]["assembly"][0]["values"]
    assert values["simulated_topology"] == "linear"
    assert values["simulated_length"] == 2635
    assert values["junctions"] == []


def test_gibson_assembly_counts_each_shared_stretch_once(tmp_path: Path) -> None:
    """pUC19 linearised by EcoRI, and the neo gene with 25 bases of each of the
    backbone's ends added to it, as Gibson primers add them: 2,686 + 845 - 2 x 25 =
    3,481 bp."""
    puc, _, _ = read("L09137.2")
    _, gene = neo_insert()
    linear = puc[396:] + puc[:396]  # as EcoRI leaves it, cut at 396
    insert = linear[-25:] + gene + linear[:25]
    output, log = command(
        tmp_path,
        "assemble",
        plan(
            "gibson",
            sequence(1, puc, name="pUC19"),
            [sequence(2, insert, topology="linear", name="neo")],
            enzymes=["EcoRI"],
        ),
    )
    assert output, log
    product = output["creates"][0]["values"]["bases"]
    assert len(insert) == 25 + 795 + 25 == 845
    assert len(product) == 2686 + 845 - 50 == 3481
    assert gene in product + product
    junctions = output["updates"]["assembly"][0]["values"]["junctions"]
    assert [j["overlap"] for j in junctions] == ["25 bp", "25 bp"]


def test_a_gibson_overlap_shorter_than_asked_for_joins_nothing(tmp_path: Path) -> None:
    """Fragments sharing 12 bases don't join when the plan asks for 15, which is the
    shortest homology NEB's protocol gives."""
    puc, _, _ = read("L09137.2")
    _, gene = neo_insert()
    linear = puc[396:] + puc[:396]
    insert = linear[-12:] + gene + linear[:12]
    output, log = command(
        tmp_path,
        "assemble",
        plan(
            "gibson",
            sequence(1, puc, name="pUC19"),
            [sequence(2, insert, topology="linear", name="neo")],
            enzymes=["EcoRI"],
        ),
    )
    assert not output
    assert "make nothing sharing at least 15 bases at their ends" in log
    assert "nor end to end into a linear molecule" in log
    # Asking for 12 joins them, and nothing else changes.
    output, log = command(
        tmp_path,
        "assemble",
        plan(
            "gibson",
            sequence(1, puc, name="pUC19"),
            [sequence(2, insert, topology="linear", name="neo")],
            enzymes=["EcoRI"],
            params={"min_overlap": 12},
        ),
    )
    assert output, log
    assert len(output["creates"][0]["values"]["bases"]) == 2686 + 819 - 24 == 3481


def test_a_plan_with_a_product_already_isnt_assembled_again(tmp_path: Path) -> None:
    puc, _, _ = read("L09137.2")
    output, log = command(
        tmp_path,
        "assemble",
        plan(
            "restriction_ligation",
            sequence(1, puc),
            [],
            enzymes=["EcoRI", "HindIII"],
            product=sequence(3, puc),
        ),
    )
    assert not output
    assert "has a product already" in log


# --- a check's result stands for the plan it was found from ---------------------------


def test_a_single_stranded_product_isnt_what_a_plan_makes(tmp_path: Path) -> None:
    """A ligation closes a double-stranded circle. A molecule is its bases, its topology
    and its strand count (ADR 0138), so a single-stranded circle of the same spelling is
    another molecule, and the check says so rather than matching on the bases alone."""
    puc, _, _ = read("L09137.2")
    checked, log = command(
        tmp_path,
        "check",
        plan(
            "restriction_ligation",
            sequence(1, puc, name="pUC19"),
            [],
            enzymes=["EcoRI"],
            product=sequence(3, puc, strandedness="single"),
        ),
    )
    assert checked, log
    values = checked["updates"]["assembly"][0]["values"]
    assert values["matches_product"] is False
    assert "DNA-000003 is circular and single-stranded" in log
    # The same bases, double-stranded, are the plan's product.
    checked, log = command(
        tmp_path,
        "check",
        plan(
            "restriction_ligation",
            sequence(1, puc, name="pUC19"),
            [],
            enzymes=["EcoRI"],
            product=sequence(3, puc),
        ),
    )
    assert checked, log
    assert checked["updates"]["assembly"][0]["values"]["matches_product"] is True


def test_a_check_settles_a_plan_that_makes_several_circles(
    tmp_path: Path, cloning: ModuleType
) -> None:
    """A plan whose parts make more than one circle can't be assembled: it doesn't say
    which. The clone's own sequence does, which is what the ambiguity message tells the
    scientist to do, so the check finds the product among the circles and matches it --
    with a warning that the plan on its own didn't predict it."""
    puc, _, _ = read("L09137.2")
    _, gene = neo_insert()
    insert = "TT" + "GAATTC" + gene + "GAATTC" + "TT"
    both = plan(
        "restriction_ligation",
        sequence(1, puc, name="pUC19"),
        [sequence(2, insert, topology="linear", name="neo")],
        enzymes=["EcoRI"],
    )
    # The plan makes the insert either way round, so `assemble` makes neither.
    output, log = command(tmp_path, "assemble", both)
    assert not output
    assert "2 different circles (3487 bp, 3487 bp)" in log
    assert "give the product and check the plan against it" in log

    # Which one the clone is, the clone's sequence says.
    made = cloning.ligate(
        [
            cloning.digest("pUC19", puc, True, ["EcoRI"]),
            [
                f
                for f in cloning.digest("neo", insert, False, ["EcoRI"])
                if f.left.sticky and f.right.sticky
            ],
        ]
    )
    inverted = next(a for a in made if reverse_complement(gene) in a.bases * 2)
    checked, log = command(
        tmp_path,
        "check",
        plan(
            "restriction_ligation",
            sequence(1, puc, name="pUC19"),
            [sequence(2, insert, topology="linear", name="neo")],
            enzymes=["EcoRI"],
            product=sequence(3, inverted.bases),
        ),
    )
    assert checked, log
    values = checked["updates"]["assembly"][0]["values"]
    assert values["matches_product"] is True
    assert values["simulated_length"] == 3487
    assert "is one of them" in log
    warning = [m for m in checked["messages"] if m["level"] == "warning"]
    assert len(warning) == 1
    assert "doesn't say which of the circles it makes" in warning[0]["text"]

    # A product that is none of them doesn't match, and no length is claimed for a plan
    # that still doesn't say which circle it makes.
    checked, log = command(
        tmp_path,
        "check",
        plan(
            "restriction_ligation",
            sequence(1, puc, name="pUC19"),
            [sequence(2, insert, topology="linear", name="neo")],
            enzymes=["EcoRI"],
            product=sequence(3, puc),
        ),
    )
    assert checked, log
    values = checked["updates"]["assembly"][0]["values"]
    assert values["matches_product"] is False
    assert values["simulated_length"] is None
    assert values["junctions"] is None
    assert "is none of them" in log


def test_a_check_that_cant_reach_a_result_clears_the_old_one(tmp_path: Path) -> None:
    """A plan checked once keeps its result until a check finds otherwise, and Checked,
    Built and Verified all read that flag. A plan edited into one Iki doesn't work out --
    here a Gateway reaction -- leaves a stale result standing, so the check clears it
    along with the length and junctions it was found with."""
    puc, _, _ = read("L09137.2")
    checked, log = command(
        tmp_path,
        "check",
        plan("gateway_lr", sequence(1, puc, name="pUC19"), [], product=sequence(3, puc)),
    )
    assert checked, log
    assert checked["updates"]["assembly"][0]["values"] == {
        "matches_product": None,
        "simulated_length": None,
        "simulated_topology": None,
        "junctions": None,
    }
    assert checked["messages"][0]["level"] == "error"
    assert "recorded, not worked out" in checked["messages"][0]["text"]


# --- plans whose product is linear ---------------------------------------------------


def test_a_digest_makes_the_linear_molecule_it_leaves(tmp_path: Path) -> None:
    """A digest cuts one molecule and keeps a band, which is a linear molecule: pUC19
    linearised with ScaI, whose one site lies 2,179 bases in, inside the beta-lactamase
    gene. One cut in a circle leaves one fragment, as long as the circle, blunt at both
    ends -- the linearised vector a lab transforms or assembles into.

    What the plan makes is registered as the DNA sequence it is, and no plasmid record is
    registered for it: a cut vector is on its way to something else, not a vector of its
    own. The plan keeps its length and its topology, and claims no junctions, since a
    molecule with two free ends and no joins has none.
    """
    puc, _, _ = read("L09137.2")
    output, log = command(
        tmp_path,
        "assemble",
        plan("digest", sequence(1, puc, name="pUC19"), [], enzymes=["ScaI"]),
    )
    assert output, log
    assert "the plan makes a 2686 bp linear molecule by digest" in log
    assert "registered as a DNA sequence" in log
    (made,) = output["creates"]
    assert made["type"] == "iki/dna.DNASequence"
    assert made["values"]["topology"] == "linear"
    assert made["values"]["strandedness"] == "double"
    assert len(made["values"]["bases"]) == 2686
    # It is pUC19 opened at the cut, so it reads from there round to there.
    assert made["values"]["bases"] == puc[2179:] + puc[:2179]
    values = output["updates"]["assembly"][0]["values"]
    assert values["simulated_topology"] == "linear"
    assert values["simulated_length"] == 2686
    assert values["junctions"] == []


def test_a_digest_that_cuts_nothing_says_so(tmp_path: Path) -> None:
    """pUC19 has no NotI site, so a NotI digest of it leaves no linear molecule at all,
    and the run says which enzyme to name rather than claiming the circle."""
    puc, _, _ = read("L09137.2")
    output, log = command(
        tmp_path,
        "assemble",
        plan("digest", sequence(1, puc, name="pUC19"), [], enzymes=["NotI"]),
    )
    assert not output
    assert "isn't cut by NotI" in log
    assert "the digest leaves no linear molecule" in log


def test_a_digest_of_a_plasmid_cuts_out_the_band_asked_for(tmp_path: Path) -> None:
    """Two cuts in a circle leave two bands, and a digest's plan doesn't say which the
    lab kept, so it makes none and names both: pUC19 cut with EcoRI and HindIII leaves
    the 2,635 bp vector and the 51 bp piece of its polylinker. Its longest, which is what
    a vector digest is purified down to, is what the default keeps."""
    puc, _, _ = read("L09137.2")
    output, log = command(
        tmp_path,
        "assemble",
        plan(
            "digest",
            sequence(1, puc, name="pUC19"),
            [],
            enzymes=["EcoRI", "HindIII"],
            params={"backbone_fragment": "any"},
        ),
    )
    assert not output
    assert "make 2 different linear molecules (2635 bp, 51 bp)" in log
    output, log = command(
        tmp_path,
        "assemble",
        plan("digest", sequence(1, puc, name="pUC19"), [], enzymes=["EcoRI", "HindIII"]),
    )
    assert output, log
    assert output["updates"]["assembly"][0]["values"]["simulated_length"] == 2635


def test_golden_gate_fragments_that_cant_close_make_a_linear_construct(
    tmp_path: Path,
) -> None:
    """A Golden Gate reaction of two fragments whose inner overhangs pair and whose outer
    ends don't: BsaI cuts outside its site, so each fragment keeps a four-base overhang
    of its own. Here the two inner ends are both AGGA and the outer ends are TTCG and
    GCAC, which pair with nothing, so no arrangement closes and what the reaction leaves
    is the linear construct -- which is what a lab assembling a linear expression
    fragment wants.

    The pieces: each part is `GGTCTCN<overhang><body><overhang>NGAGACC` read on the
    strand BsaI cuts from, so a BsaI digest of each leaves one fragment that runs from
    the cut at its left to the cut at its right -- its own overhang, then its body. Laid
    end to end that is 4 + 23 + 4 + 23 = 54 bases, and the overhang at the far end of the
    construct lies beyond the last cut, in the piece the reaction throws away, so it
    isn't in the sequence: a linear molecule is read as a fully paired duplex of the top
    strand from cut to cut, which is the length a gel reports (ADR 0138). Its one junction
    is the pair of AGGA overhangs: a linear molecule of n parts has n - 1 junctions,
    where a circle has n.
    """
    first = "AAGGTCTCT" + "TTCG" + "ATGGCATGCGGTACCTTAGCCAT" + "AGGA" + "AGAGACCTT"
    second = "AAGGTCTCT" + "AGGA" + "CCGTTAACGGCTAGCTAACGTTT" + "GCAC" + "AGAGACCTT"
    output, log = command(
        tmp_path,
        "assemble",
        plan(
            "golden_gate",
            sequence(1, first, topology="linear", name="part one"),
            [sequence(2, second, topology="linear", name="part two")],
            enzymes=["BsaI"],
        ),
    )
    assert output, log
    (made,) = output["creates"]
    assert made["values"]["topology"] == "linear"
    bases = made["values"]["bases"]
    # Each fragment is its overhang, its body and the next overhang: 4 + 23 + 4 bases.
    assert len(bases) == 4 + 23 + 4 + 23 == 54
    assert "ATGGCATGCGGTACCTTAGCCAT" in bases
    assert "CCGTTAACGGCTAGCTAACGTTT" in bases
    values = output["updates"]["assembly"][0]["values"]
    assert values["simulated_topology"] == "linear"
    assert values["simulated_length"] == 54
    (junction,) = values["junctions"]
    assert junction["overhang"] == "5' AGGA"
    assert (junction["from"], junction["to"]) == (
        "part one (DNA-000001)",
        "part two (DNA-000002)",
    )


def test_a_gibson_assembly_that_shares_one_end_makes_a_linear_construct(
    tmp_path: Path,
) -> None:
    """Two linear fragments that share 25 bases at one end only: the neo gene of
    pcDNA3.1(+) with 25 bases of pUC19 added to its front, beside the 2,635 bp pUC19
    fragment they come from. They can't close -- nothing joins the far ends -- so what
    the plan makes is the linear molecule they share one stretch in: 2,635 + 820 - 25
    bases.
    """
    puc, _, _ = read("L09137.2")
    _, gene = neo_insert()
    vector = puc[447:] + puc[:396]  # as EcoRI and HindIII leave it, 2,635 bases
    assert len(vector) == 2635
    insert = vector[-25:] + gene
    output, log = command(
        tmp_path,
        "assemble",
        plan(
            "gibson",
            sequence(1, vector, topology="linear", name="pUC19 fragment"),
            [sequence(2, insert, topology="linear", name="neo")],
        ),
    )
    assert output, log
    (made,) = output["creates"]
    assert made["values"]["topology"] == "linear"
    assert len(made["values"]["bases"]) == 2635 + 820 - 25 == 3430
    assert gene in made["values"]["bases"]
    values = output["updates"]["assembly"][0]["values"]
    assert values["simulated_topology"] == "linear"
    (junction,) = values["junctions"]
    assert junction["overlap"] == "25 bp"


def test_a_linear_product_matches_from_either_strand_and_not_from_another_origin(
    tmp_path: Path,
) -> None:
    """The band a digest leaves, sequenced: read from the other strand it is the same
    molecule, so the plan matches it; rotated it is another molecule, since a linear
    molecule's two ends are its own, and the check says so rather than matching on the
    bases alone (ADR 0138)."""
    puc, _, _ = read("L09137.2")
    cut = puc[2179:] + puc[:2179]  # ScaI's one cut, 2,179 bases in
    checked, log = command(
        tmp_path,
        "check",
        plan(
            "digest",
            sequence(1, puc, name="pUC19"),
            [],
            enzymes=["ScaI"],
            product=sequence(3, reverse_complement(cut), topology="linear"),
        ),
    )
    assert checked, log
    values = checked["updates"]["assembly"][0]["values"]
    assert values["matches_product"] is True
    assert values["simulated_topology"] == "linear"
    assert "the same linear molecule, read from either strand" in log

    rotated = cut[700:] + cut[:700]
    checked, log = command(
        tmp_path,
        "check",
        plan(
            "digest",
            sequence(1, puc, name="pUC19"),
            [],
            enzymes=["ScaI"],
            product=sequence(3, rotated, topology="linear"),
        ),
    )
    assert checked, log
    values = checked["updates"]["assembly"][0]["values"]
    assert values["matches_product"] is False
    assert values["simulated_length"] == 2686
    assert "DNA-000003 has 2686 bp: the same length, so they differ in their bases" in log


def test_a_circular_product_isnt_what_a_digest_makes(tmp_path: Path) -> None:
    """A molecule is its bases, its topology and its strand count (ADR 0138). A digest
    leaves a linear molecule, so the circle it was cut from isn't what it makes, however
    its bases read -- the mirror of a ligation whose product is said to be linear."""
    puc, _, _ = read("L09137.2")
    checked, log = command(
        tmp_path,
        "check",
        plan(
            "digest",
            sequence(1, puc, name="pUC19"),
            [],
            enzymes=["ScaI"],
            product=sequence(3, puc),
        ),
    )
    assert checked, log
    values = checked["updates"]["assembly"][0]["values"]
    assert values["matches_product"] is False
    assert values["simulated_topology"] == "linear"
    assert "the plan leaves a linear double-stranded molecule" in log
    assert "DNA-000003 is circular and double-stranded" in log


def test_a_linear_product_isnt_what_a_ligation_closing_a_circle_makes(
    tmp_path: Path,
) -> None:
    """The other way round: a plan whose parts close a circle makes that circle, and the
    linear molecule of the same bases isn't it. So a clone stated as linear can't pass
    for a plasmid, and nobody reaches Checked on it."""
    puc, _, _ = read("L09137.2")
    checked, log = command(
        tmp_path,
        "check",
        plan(
            "restriction_ligation",
            sequence(1, puc, name="pUC19"),
            [],
            enzymes=["EcoRI"],
            product=sequence(3, puc, topology="linear"),
        ),
    )
    assert checked, log
    values = checked["updates"]["assembly"][0]["values"]
    assert values["matches_product"] is False
    assert values["simulated_topology"] == "circular"
    assert "the plan closes a circular double-stranded molecule" in log
    assert "DNA-000003 is linear and double-stranded" in log


# --- the virtual digest ----------------------------------------------------------------

LAMBDA_END, ECO = "End of the molecule", "EcoRI, 5' AATT"


def test_an_ambiguous_digest_names_its_bands_largest_first(tmp_path: Path) -> None:
    """A digest of lambda kept as `any` of its six EcoRI bands makes none, as designed,
    and names them largest first, as a gel reads them, and says where to see each."""
    lam = read_fasta("J02459.1")
    output, log = command(
        tmp_path,
        "assemble",
        plan(
            "digest",
            sequence(1, lam, topology="linear", name="lambda"),
            [],
            enzymes=["EcoRI"],
            params={"backbone_fragment": "any"},
        ),
    )
    assert not output
    lengths = ", ".join(f"{n} bp" for n in LAMBDA_ECORI)
    assert f"make 6 different linear molecules ({lengths})" in log
    assert "Digest a plan's parts virtually" in log


def test_a_virtual_digest_lists_every_fragment_largest_first(tmp_path: Path) -> None:
    """Lambda (J02459.1, 48,502 bp, linear) cut with EcoRI gives the six bands of the
    marker it's sold as (NEB's and Thermo Fisher's lambda DNA-EcoRI digests), each with
    where it lies and what made each end, as a result on the plan. Its sites are
    Biopython's (EcoRI.search)."""
    lam = read_fasta("J02459.1")
    sites = sorted(p - 1 for p in Restriction.EcoRI.search(Seq(lam), linear=True))  # type: ignore[attr-defined]
    assert sites == [21226, 26104, 31747, 39168, 44972]
    output, log = command(
        tmp_path,
        "digest",
        plan("digest", sequence(1, lam, topology="linear", name="lambda"), [], enzymes=["EcoRI"]),
    )
    assert output, log
    (update,) = output["updates"]["assembly"]
    rows = update["values"]["fragments"]
    part = "lambda (DNA-000001)"
    assert rows == [
        {"part": part, "length": 21226, "start": 1, "end": 21226, "left": LAMBDA_END, "right": ECO},
        {"part": part, "length": 7421, "start": 31748, "end": 39168, "left": ECO, "right": ECO},
        {"part": part, "length": 5804, "start": 39169, "end": 44972, "left": ECO, "right": ECO},
        {"part": part, "length": 5643, "start": 26105, "end": 31747, "left": ECO, "right": ECO},
        {"part": part, "length": 4878, "start": 21227, "end": 26104, "left": ECO, "right": ECO},
        {
            "part": part,
            "length": 3530,
            "start": 44973,
            "end": 48502,
            "left": ECO,
            "right": LAMBDA_END,
        },
    ]
    assert [r["length"] for r in rows] == LAMBDA_ECORI
    assert sum(r["length"] for r in rows) == len(lam)
    assert f"{part}: 6 fragments with EcoRI: 21226, 7421, 5804, 5643, 4878, 3530 bp" in log


def test_a_virtual_digest_of_a_circle_crosses_its_origin(tmp_path: Path) -> None:
    """pUC19 (L09137.2) cut by EcoRI after base 396 and HindIII after base 447: the
    2,635 bp vector, from base 448 round the origin to base 396, and the 51 bp piece of
    its polylinker. Every end of a circle's fragment is a cut."""
    puc, _, _ = read("L09137.2")
    output, log = command(
        tmp_path,
        "digest",
        plan(
            "restriction_ligation",
            sequence(1, puc, name="pUC19"),
            [],
            enzymes=["EcoRI", "HindIII"],
        ),
    )
    assert output, log
    rows = output["updates"]["assembly"][0]["values"]["fragments"]
    hind = "HindIII, 5' AGCT"
    assert rows == [
        {
            "part": "pUC19 (DNA-000001)",
            "length": 2635,
            "start": 448,
            "end": 396,
            "left": hind,
            "right": ECO,
        },
        {
            "part": "pUC19 (DNA-000001)",
            "length": 51,
            "start": 397,
            "end": 447,
            "left": ECO,
            "right": hind,
        },
    ]


def test_a_virtual_digest_says_what_isnt_cut(tmp_path: Path) -> None:
    """pUC19 has no NotI site: it stays a circle, which is no fragment, and the run says
    so rather than listing nothing."""
    puc, _, _ = read("L09137.2")
    output, log = command(
        tmp_path,
        "digest",
        plan("digest", sequence(1, puc, name="pUC19"), [], enzymes=["NotI"]),
    )
    assert output, log
    assert output["updates"]["assembly"][0]["values"]["fragments"] == []
    assert "isn't cut by NotI" in log


def test_a_virtual_digest_needs_the_plans_enzymes(tmp_path: Path) -> None:
    puc, _, _ = read("L09137.2")
    output, log = command(tmp_path, "digest", plan("gibson", sequence(1, puc, name="pUC19"), []))
    assert not output
    assert "names no enzymes" in log
