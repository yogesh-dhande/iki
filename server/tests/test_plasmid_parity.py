"""The ported Rust gives what the Python it replaced gave, base for base and degree for
degree.

Primer placement, duplex melting temperatures and restriction digests moved from the
plasmid plugin's own Python into the shared Rust crate (ADR 0200). The implementation
that was there is kept in ``oracles/`` and run here beside the new one: over the cases
the plugin's own tests already cover, over real vectors, and over randomly generated
templates, primers with mismatches and random sets of enzymes drawn from Biopython's
REBASE-derived table. The two must agree exactly -- the sites and their melting
temperatures, the cuts and their overhangs, the fragments and the gel bands.

The melting temperatures are cross-checked against Biopython's own
``MeltingTemp.Tm_NN`` as well, under the conditions the plugin states, so the chain runs
from the published parameters (SantaLucia 1998) through Biopython to the Rust.

The random templates are synthetic, as a property test's inputs must be; the named
sequences are the public records the plugin's other tests use.
"""

import random
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
from Bio import Restriction
from Bio.Restriction import Restriction_Dictionary
from Bio.SeqUtils import MeltingTemp

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "plugins" / "plasmid" / "scripts"
ORACLES = Path(__file__).resolve().parent / "oracles"

#: NEB's M13/pUC sequencing primers (#S1201S and #S1233S).
M13_FORWARD = "GTAAAACGACGGCCAGT"
M13_REVERSE = "CAGGAAACAGCTATGAC"
#: pUC19's polylinker, from its INSDC record L09137.2, which this plugin's other tests
#: read in full. Enough of it to place the M13 primers and cut with the common enzymes.
PUC19_POLYLINKER = (
    "GTAAAACGACGGCCAGTGAATTCGAGCTCGGTACCCGGGGATCCTCTAGAGTCGACCTGCAGGCATGCAAGCT"
    "TGGCGTAATCATGGTCATAGCTGTTTCCTGTGTGAAATTGTTATCCGCTCACAATTCCACACAACATACGAGCC"
    "GGAAGCATAAAGTGTAAAGCCTGGGGTGCCTAATGAGTGAGCTAACTCACATTAATTGCGTTGCGCTCACTGCC"
)
#: Enzymes whose cut REBASE knows, which a random digest is drawn from. Biopython's table
#: holds about a thousand, a third of which it has no cut position for; those are refused
#: rather than guessed, which `test_plasmid_cloning.py` covers.
KNOWN_ENZYMES = sorted(
    name
    for name, described in Restriction_Dictionary.rest_dict.items()
    if described["ovhg"] is not None
)


def _module(name: str, where: Path) -> ModuleType:
    """One module imported by bare name from `where`, as a run imports a plugin's."""
    import importlib
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, where / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def ported() -> Iterator[tuple[ModuleType, ModuleType]]:
    """The plugin's modules as they are now: priming and cloning, both on the Rust."""
    kept = {name: sys.modules.get(name) for name in ("molecular", "priming", "cloning")}
    try:
        _module("molecular", SCRIPTS)
        yield _module("priming", SCRIPTS), _module("cloning", SCRIPTS)
    finally:
        for name, was in kept.items():
            if was is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = was


@pytest.fixture(scope="module")
def before() -> Iterator[tuple[ModuleType, ModuleType]]:
    """The pure-Python implementation that was replaced."""
    kept = {name: sys.modules.get(name) for name in ("priming_before", "cloning_before")}
    try:
        yield _module("priming_before", ORACLES), _module("cloning_before", ORACLES)
    finally:
        for name, was in kept.items():
            if was is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = was


def _sites(
    module: ModuleType,
    template: str,
    circular: bool,
    primers: list[dict[str, str]],
    anchor: int,
    penalty: int,
) -> list[tuple[object, ...]]:
    """Every site, as a tuple of everything a record keeps about it."""
    return [
        (
            site.hrid,
            site.name,
            site.strand,
            site.start,
            site.end,
            site.three_prime,
            site.annealed,
            site.tail,
            site.mismatches,
            site.penalty,
            site.tm,
        )
        for site in module.find_sites(
            template, circular, primers, anchor=anchor, most_penalty=penalty
        )
    ]


def _fragments(
    module: ModuleType, bases: str, circular: bool, names: list[str]
) -> list[tuple[object, ...]]:
    return [
        (
            one.part,
            one.bases,
            one.left.overhang,
            one.left.bases,
            one.right.overhang,
            one.right.bases,
            tuple(one.cut_with),
        )
        for one in module.digest("part", bases, circular, names)
    ]


def _bands(
    module: ModuleType, bases: str, circular: bool, names: list[str]
) -> list[tuple[object, ...]]:
    return [
        (one.length, one.start, one.end, one.left, one.right)
        for one in module.bands(bases, circular, names)
    ]


# --- the cases the plugin's own tests already cover ----------------------------------


@pytest.mark.parametrize("circular", [False, True])
@pytest.mark.parametrize(
    ("primer", "anchor", "penalty"),
    [
        (M13_FORWARD, 15, 2),
        (M13_REVERSE, 15, 2),
        (M13_FORWARD, 15, 0),
        # A primer with a 5' tail the template hasn't got, as a cloning primer has.
        ("GGGGGGGGGG" + M13_REVERSE, 15, 2),
        # The same primer with its 3'-most base changed: refused under weight 2, kept
        # under weight 3.
        (M13_FORWARD[:-1] + "A", 15, 2),
        (M13_FORWARD[:-1] + "A", 15, 3),
        # One further from the 3' end, which weighs 1.
        (M13_FORWARD[:6] + "A" + M13_FORWARD[7:], 15, 2),
        # Placed by all of a short primer.
        (M13_FORWARD[-9:], 9, 0),
        # An IUPAC code that pairs with whatever is there.
        ("GTAAAACGACGGCCANN", 15, 2),
    ],
)
def test_the_named_primers_are_placed_identically(
    ported: tuple[ModuleType, ModuleType],
    before: tuple[ModuleType, ModuleType],
    primer: str,
    anchor: int,
    penalty: int,
    circular: bool,
) -> None:
    primers = [{"hrid": "PRM-1", "name": "a primer", "bases": primer}]
    now = _sites(ported[0], PUC19_POLYLINKER, circular, primers, anchor, penalty)
    then = _sites(before[0], PUC19_POLYLINKER, circular, primers, anchor, penalty)
    assert now == then


@pytest.mark.parametrize(
    "bases",
    [
        M13_FORWARD,
        M13_REVERSE,
        "GAATTC",  # self-complementary
        "AT",  # the shortest duplex there are parameters for
        "ACGTACGTACGTACGTACGTACGTACGT",
        "AAAAAAAAAAAAAAAAAAAA",
        "GCGCGCGCGCGCGCGCGCGC",
    ],
)
def test_a_matched_duplex_melts_where_biopython_and_the_old_python_say(
    ported: tuple[ModuleType, ModuleType], before: tuple[ModuleType, ModuleType], bases: str
) -> None:
    # SantaLucia 1998, equations 3 and 7 (DOI 10.1073/pnas.95.4.1460), as Biopython's
    # Tm_NN computes them with DNA_NN3 and saltcorr=5, at the conditions the plugin
    # states: 50 mM Na+, no Mg2+ or dNTPs, 25 nM of each strand.
    def tm(**strands: float | bool) -> float:
        celsius: float = MeltingTemp.Tm_NN(  # type: ignore[no-untyped-call]
            bases, nn_table=MeltingTemp.DNA_NN3, Na=50, saltcorr=5, **strands
        )
        return round(celsius, 1)

    two_strands = tm(dnac1=25, dnac2=25)
    # A primer alone is read against its exact complement, and a palindrome's complement
    # is itself: one species at the whole 50 nM, carrying the symmetry term, which
    # Biopython gives with selfcomp=True and dnac1=50 -- the total, as Tm_NN reads dnac1
    # once selfcomp is set. selfcomp=True with dnac1=25, dnac2=25 left in place is half
    # the correction and further from right than neither: -13.758 for GGATCC, where the
    # two-strand reading gives -13.719 and the answer is -11.384.
    reference = (
        tm(selfcomp=True, dnac1=50)
        if bases == bases.translate(str.maketrans("ACGT", "TGCA"))[::-1]
        else two_strands
    )
    assert ported[0].melting_temperature(bases) == reference
    # The pure Python it was ported from read every primer as two strands.
    assert before[0].melting_temperature(bases) == two_strands


@pytest.mark.parametrize("circular", [False, True])
@pytest.mark.parametrize(
    "names",
    [
        ["EcoRI"],
        ["HindIII"],
        ["EcoRI", "HindIII"],
        ["SmaI"],  # blunt
        ["PstI"],  # a 3' overhang
        ["BsaI"],  # non-palindromic, cutting outside its site
        ["SacI", "SstI"],  # isoschizomers: one cut, two names
        ["HincII"],  # IUPAC codes in the site
        ["NotI"],  # eight bases, so it cuts nothing here
        ["EcoRI", "SmaI", "PstI", "KpnI", "BamHI"],
    ],
)
def test_the_named_digests_cut_identically(
    ported: tuple[ModuleType, ModuleType],
    before: tuple[ModuleType, ModuleType],
    names: list[str],
    circular: bool,
) -> None:
    assert ported[1].cuts(PUC19_POLYLINKER, circular, names) == before[1].cuts(
        PUC19_POLYLINKER, circular, names
    )
    assert _fragments(ported[1], PUC19_POLYLINKER, circular, names) == _fragments(
        before[1], PUC19_POLYLINKER, circular, names
    )
    assert _bands(ported[1], PUC19_POLYLINKER, circular, names) == _bands(
        before[1], PUC19_POLYLINKER, circular, names
    )


# --- and over random molecules, primers and enzyme sets ------------------------------


def _random_template(rng: random.Random, length: int) -> str:
    return "".join(rng.choice("ACGT") for _ in range(length))


def _with_mismatches(rng: random.Random, bases: str, how_many: int) -> str:
    """The same bases with `how_many` of them changed, anywhere along them."""
    changed = list(bases)
    for at in rng.sample(range(len(bases)), min(how_many, len(bases))):
        changed[at] = rng.choice([base for base in "ACGT" if base != changed[at]])
    return "".join(changed)


@pytest.mark.parametrize("seed", range(120))
def test_random_primers_are_placed_identically(
    ported: tuple[ModuleType, ModuleType], before: tuple[ModuleType, ModuleType], seed: int
) -> None:
    rng = random.Random(seed)  # noqa: S311 - made-up test data, not secrets
    template = _random_template(rng, rng.randint(40, 400))
    circular = bool(seed % 2)
    anchor = rng.randint(8, 20)
    penalty = rng.randint(0, 6)
    primers = []
    for which in range(rng.randint(1, 3)):
        at = rng.randrange(len(template))
        length = rng.randint(10, 30)
        # A stretch of the template, on either strand, with up to three mismatches and
        # sometimes a 5' tail the template hasn't got.
        taken = (template + template)[at : at + length]
        if rng.random() < 0.5:
            taken = taken.translate(str.maketrans("ACGT", "TGCA"))[::-1]
        bases = _with_mismatches(rng, taken, rng.randint(0, 3))
        if rng.random() < 0.3:
            bases = _random_template(rng, rng.randint(1, 8)) + bases
        primers.append({"hrid": f"PRM-{which}", "name": f"primer {which}", "bases": bases})
    now = _sites(ported[0], template, circular, primers, anchor, penalty)
    then = _sites(before[0], template, circular, primers, anchor, penalty)
    assert now == then, f"seed {seed}: {len(now)} sites against {len(then)}"


@pytest.mark.parametrize("seed", range(60))
def test_random_pcrs_amplify_identically(
    ported: tuple[ModuleType, ModuleType], before: tuple[ModuleType, ModuleType], seed: int
) -> None:
    rng = random.Random(1000 + seed)  # noqa: S311 - made-up test data, not secrets
    template = _random_template(rng, rng.randint(60, 300))
    circular = bool(seed % 2)
    at = rng.randrange(len(template) // 2)
    forward = (template + template)[at : at + rng.randint(16, 24)]
    back_at = rng.randrange(len(template) // 2, len(template))
    reverse = (template + template)[back_at : back_at + rng.randint(16, 24)]
    reverse = reverse.translate(str.maketrans("ACGT", "TGCA"))[::-1]
    primers = [
        {"hrid": "PRM-F", "name": "forward", "bases": forward},
        {"hrid": "PRM-R", "name": "reverse", "bases": reverse},
    ]
    sites_now = ported[0].find_sites(template, circular, primers, anchor=15, most_penalty=2)
    sites_then = before[0].find_sites(template, circular, primers, anchor=15, most_penalty=2)
    assert _sites(ported[0], template, circular, primers, 15, 2) == _sites(
        before[0], template, circular, primers, 15, 2
    )
    made_now = ported[0].products(template, circular, sites_now, longest=20_000)
    made_then = before[0].products(template, circular, sites_then, longest=20_000)
    assert [
        (one.bases, one.template_start, one.template_end, one.forward.hrid, one.reverse.hrid)
        for one in made_now
    ] == [
        (one.bases, one.template_start, one.template_end, one.forward.hrid, one.reverse.hrid)
        for one in made_then
    ], f"seed {seed}"


@pytest.mark.parametrize("seed", range(120))
def test_random_digests_cut_identically(
    ported: tuple[ModuleType, ModuleType], before: tuple[ModuleType, ModuleType], seed: int
) -> None:
    rng = random.Random(2000 + seed)  # noqa: S311 - made-up test data, not secrets
    bases = _random_template(rng, rng.randint(20, 800))
    circular = bool(seed % 2)
    names = rng.sample(KNOWN_ENZYMES, rng.randint(1, 4))
    try:
        then_cuts = before[1].cuts(bases, circular, names)
    except before[1].Impossible as why:
        # Two enzymes that break one bond differently: the module must refuse it too.
        with pytest.raises(ported[1].Impossible):
            ported[1].cuts(bases, circular, names)
        assert "different overhangs" in str(why)
        return
    assert ported[1].cuts(bases, circular, names) == then_cuts, f"seed {seed}: {names}"
    assert _fragments(ported[1], bases, circular, names) == _fragments(
        before[1], bases, circular, names
    ), f"seed {seed}: {names}"
    assert _bands(ported[1], bases, circular, names) == _bands(before[1], bases, circular, names), (
        f"seed {seed}: {names}"
    )


@pytest.mark.parametrize("seed", range(80))
def test_random_digests_of_molecules_holding_their_sites_cut_identically(
    ported: tuple[ModuleType, ModuleType], before: tuple[ModuleType, ModuleType], seed: int
) -> None:
    """A random molecule is unlikely to hold a rare site, so these are built to: each
    enzyme's own site is written into the molecule, twice, and once across its origin."""
    rng = random.Random(3000 + seed)  # noqa: S311 - made-up test data, not secrets
    names = rng.sample(KNOWN_ENZYMES, rng.randint(1, 3))
    pieces = []
    for name in names:
        site = getattr(Restriction, name).site
        # An ambiguity code stands for several bases: pick one of them, so the molecule
        # itself holds only A, C, G and T, as a record's does.
        picked = "".join(rng.choice(_may_be(code)) for code in site)
        pieces.append(_random_template(rng, rng.randint(5, 30)) + picked)
        pieces.append(_random_template(rng, rng.randint(5, 30)) + picked)
    bases = "".join(pieces) + _random_template(rng, rng.randint(5, 20))
    # And once across the origin, by rotating the molecule into the middle of a site.
    rotated = bases[len(bases) // 3 :] + bases[: len(bases) // 3]
    for molecule in (bases, rotated):
        for circular in (False, True):
            try:
                then = before[1].cuts(molecule, circular, names)
            except before[1].Impossible:
                with pytest.raises(ported[1].Impossible):
                    ported[1].cuts(molecule, circular, names)
                continue
            assert ported[1].cuts(molecule, circular, names) == then, f"seed {seed}: {names}"
            assert _fragments(ported[1], molecule, circular, names) == _fragments(
                before[1], molecule, circular, names
            ), f"seed {seed}: {names}"
            assert _bands(ported[1], molecule, circular, names) == _bands(
                before[1], molecule, circular, names
            ), f"seed {seed}: {names}"


#: Which bases each IUPAC code may be.
_CODES = {
    "A": "A", "C": "C", "G": "G", "T": "T", "R": "AG", "Y": "CT", "S": "CG", "W": "AT",
    "K": "GT", "M": "AC", "B": "CGT", "D": "AGT", "H": "ACT", "V": "ACG", "N": "ACGT",
}  # fmt: skip


def _may_be(code: str) -> str:
    return _CODES[code]


def test_every_enzyme_rebase_gives_a_cut_for_cuts_the_same_way(
    ported: tuple[ModuleType, ModuleType], before: tuple[ModuleType, ModuleType]
) -> None:
    """Each of Biopython's enzymes with a known cut, one at a time, on a molecule built
    to hold its own site: the ported module's cuts are the Python's, for all of them."""
    rng = random.Random(4242)  # noqa: S311 - made-up test data, not secrets
    checked = 0
    for name in KNOWN_ENZYMES:
        site = getattr(Restriction, name).site
        picked = "".join(rng.choice(_may_be(code)) for code in site)
        molecule = (
            _random_template(rng, 25)
            + picked
            + _random_template(rng, 25)
            + picked
            + _random_template(rng, 25)
        )
        for circular in (False, True):
            assert ported[1].cuts(molecule, circular, [name]) == before[1].cuts(
                molecule, circular, [name]
            ), f"{name} on {molecule}"
        checked += 1
    assert checked > 700, f"only {checked} enzymes have a cut REBASE knows"
