"""Cross-check the canonical duplex measurement against an independent implementation."""

from itertools import product

import pytest
from Bio.Seq import reverse_complement
from Bio.SeqUtils import MeltingTemp

from iki_spec import wasm

pytestmark = pytest.mark.skipif(not wasm.WASM.exists(), reason="needs iki-wasm built")


@pytest.mark.parametrize(
    "bases",
    [
        # Synthetic doublets exercise every neighbour and terminal combination.
        *("".join(pair) for pair in product("ACGT", repeat=2)),
        "GAATTC",  # Self-complementary reference used in the sequence viewer.
        "GTAAAACGACGGCCAGT",  # Public M13/pUC forward record.
        "CAGGAAACAGCTATGAC",  # Public M13/pUC reverse record.
        "ACGT" * 50,  # Synthetic sequence at the length limit of the viewer.
    ],
)
def test_measured_temperature_matches_the_independent_reference(bases: str) -> None:
    # SantaLucia (1998), Table 2, equations 3 and 8, DOI 10.1073/pnas.95.4.1460:
    # 50 mM monovalent Na+, 50 nM total strands, initially equimolar. A self-complement
    # is one species at the full concentration and includes a symmetry entropy term.
    symmetric = bases == reverse_complement(bases)  # type: ignore[no-untyped-call]
    expected = MeltingTemp.Tm_NN(  # type: ignore[no-untyped-call]
        bases,
        nn_table=MeltingTemp.DNA_NN3,
        dnac1=50 if symmetric else 25,
        dnac2=0 if symmetric else 25,
        selfcomp=symmetric,
        Na=50,
        saltcorr=5,
    )
    actual = wasm.call("iki_melting_temperature", {"bases": bases}, wasm.FUEL)
    assert actual == {"celsius": round(expected, 1)}
