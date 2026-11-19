import datetime as dt
import hashlib
import uuid

import pytest

from iki_spec.identity import Kind, Placement, canonical, encode, identity_hash, place, sequence


def test_the_encoding_is_canonical_json() -> None:
    values = {"bases": ("string", "ACGT"), "copies": ("int", 2)}
    assert encode("acme/dna.DNASequence", values) == (
        b'{"type":"acme/dna.DNASequence","v":2,"values":{"bases":"ACGT","copies":2}}'
    )
    # Field order doesn't matter.
    reordered = {"copies": ("int", 2), "bases": ("string", "ACGT")}
    assert identity_hash("acme/dna.DNASequence", values) == identity_hash(
        "acme/dna.DNASequence", reordered
    )


def test_a_known_hash() -> None:
    """A test vector, for other implementations of the spec."""
    expected = hashlib.sha256(b'{"type":"acme/dna.DNASequence","v":2,"values":{"bases":"ACGT"}}')
    assert identity_hash("acme/dna.DNASequence", {"bases": ("string", "ACGT")}) == (
        expected.hexdigest()
    )


def test_an_optional_field_without_a_value_is_null() -> None:
    """A test vector: an antibody without a light chain (a single-domain one)."""
    heavy = uuid.UUID("01890a5d-ac96-774b-bcce-b302099a8057")
    values = {"heavy_chain": ("ref", heavy), "light_chain": ("ref", None)}
    assert encode("iki/antibody.Antibody", values) == (
        b'{"type":"iki/antibody.Antibody","v":2,"values":'
        b'{"heavy_chain":"01890a5d-ac96-774b-bcce-b302099a8057","light_chain":null}}'
    )
    # Without one isn't with one.
    light = uuid.UUID("01890a5d-ac96-774b-bcce-b302099a8058")
    paired = {**values, "light_chain": ("ref", light)}
    assert identity_hash("iki/antibody.Antibody", values) != identity_hash(
        "iki/antibody.Antibody", paired
    )
    with pytest.raises(ValueError, match="float fields can't be part of an identity"):
        canonical("float", None)


def test_the_type_is_part_of_the_hash() -> None:
    values = {"code": ("string", "A1")}
    assert identity_hash("lab.Sample", values) != identity_hash("lab.Specimen", values)


def test_values_by_type() -> None:
    assert canonical("string", "é") == "é"  # NFC: one way to write é
    assert (
        encode("a.B", {"name": ("text", "café")})
        == '{"type":"a.B","v":2,"values":{"name":"café"}}'.encode()
    )
    assert canonical("bool", True) is True
    assert canonical("date", dt.date(2026, 1, 2)) == "2026-01-02"
    moment = dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.timezone(dt.timedelta(hours=2)))
    assert canonical("datetime", moment) == "2026-01-02T01:04:05.000000Z"
    ref = uuid.UUID("0190F2A0-0000-7000-8000-000000000001")
    assert canonical("ref", ref) == "0190f2a0-0000-7000-8000-000000000001"
    assert canonical("file", "A" * 64) == "a" * 64


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        ("string", 1),
        ("int", True),
        ("int", 1.0),
        ("bool", 1),
        ("datetime", dt.datetime(2026, 1, 2)),  # noqa: DTZ001 - naive on purpose
        ("date", dt.datetime(2026, 1, 2, tzinfo=dt.UTC)),
        ("ref", "not a uuid"),
        ("file", "abc"),
        ("float", 1.5),
    ],
)
def test_values_that_cant_be_identities(kind: str, value: object) -> None:
    with pytest.raises(ValueError):
        canonical(kind, value)


# Version 2 (ADR 0138): sequences and lists read as what they are.

PROTEIN = Kind("string", format="protein_sequence")


def _dna(bases: str, *, circular: bool, double: bool) -> str:
    """As iki/dna.DNASequence's: its bases, read by its topology and strandedness, which
    are part of its identity too."""
    kind = Kind("string", format="dna_sequence", circular=circular, double=double)
    return identity_hash(
        "iki/dna.DNASequence",
        {
            "bases": (kind, bases),
            "topology": ("enum", "circular" if circular else "linear"),
            "strandedness": ("enum", "double" if double else "single"),
        },
    )


def test_a_protein_is_its_residues_whatever_their_case_and_terminal_stop() -> None:
    """F-003: MKWV, mkwv and MKWV* are one peptide; a stop inside it makes another."""
    hashes = {
        identity_hash("iki/protein.Protein", {"sequence": (PROTEIN, s)})
        for s in (
            "MKWV",
            "mkwv",
            "MKWV*",
            "MkWv**",
        )
    }
    assert len(hashes) == 1
    assert canonical(PROTEIN, "mkwv*") == "MKWV"
    assert canonical(PROTEIN, "MK*WV") == "MK*WV"
    inner = identity_hash("iki/protein.Protein", {"sequence": (PROTEIN, "MK*WV")})
    assert inner not in hashes


def test_a_circular_duplex_is_the_same_from_any_origin_on_either_strand() -> None:
    """F-056: AAGTCC, its rotation GTCCAA and its other strand GGACTT are one duplex."""
    for given in ("AAGTCC", "GTCCAA", "GGACTT", "ggactt"):
        assert sequence("dna_sequence", given, circular=True, double=True) == "AAGTCC"
    assert len({_dna(s, circular=True, double=True) for s in ("AAGTCC", "GTCCAA", "GGACTT")}) == 1


def test_a_single_stranded_circle_is_the_same_only_from_another_origin() -> None:
    """F-056: a single strand's complement is another molecule, so GGACTT is apart."""
    assert sequence("dna_sequence", "GTCCAA", circular=True) == "AAGTCC"
    assert sequence("dna_sequence", "GGACTT", circular=True) == "ACTTGG"
    single = {s: _dna(s, circular=True, double=False) for s in ("AAGTCC", "GTCCAA", "GGACTT")}
    assert single["AAGTCC"] == single["GTCCAA"] != single["GGACTT"]
    # And a single strand isn't the duplex it would pair into.
    assert single["AAGTCC"] != _dna("AAGTCC", circular=True, double=True)


def test_a_linear_duplex_is_the_lesser_of_its_strands_and_a_linear_strand_is_itself() -> None:
    # As SEGUID v2's ldseguid: the two strands, each 5' to 3', and the lesser of them.
    assert sequence("dna_sequence", "TTGC", double=True) == "GCAA"
    assert sequence("dna_sequence", "GCAA", double=True) == "GCAA"
    assert _dna("TTGC", circular=False, double=True) == _dna("GCAA", circular=False, double=True)
    # A rotation of a linear sequence is another sequence.
    assert _dna("TGCA", circular=False, double=True) != _dna("GCAT", circular=False, double=True)
    assert sequence("dna_sequence", "ttgc") == "TTGC"


def test_each_alphabet_keeps_its_own_bases_and_complements_iupac_codes() -> None:
    # R (A or G) pairs with Y (C or T); RNA's U pairs with A, and DNA has no U.
    assert sequence("dna_sequence", "ARN", double=True) == "ARN"
    assert sequence("dna_sequence", "TYN", double=True) == "NRA"
    assert sequence("rna_sequence", "UUGC", double=True) == "GCAA"
    assert sequence("rna_sequence", "uugc") == "UUGC"
    with pytest.raises(ValueError, match="no second strand"):
        sequence("protein_sequence", "MKWV", double=True)
    # A cyclic peptide is the same from any residue, but not read backwards.
    assert sequence("protein_sequence", "WVMK", circular=True) == "KWVM"


def test_a_list_is_a_multiset_of_its_items() -> None:
    a, b = uuid.UUID(int=1), uuid.UUID(int=2)
    refs = Kind("list", items=Kind("ref"))
    parts = {"parts": (refs, [b, a])}
    assert identity_hash("lab.Complex", parts) == identity_hash(
        "lab.Complex", {"parts": (refs, [a, b])}
    )
    # Twice one part isn't once.
    assert identity_hash("lab.Complex", parts) != identity_hash(
        "lab.Complex", {"parts": (refs, [a, b, b])}
    )
    assert encode("lab.Complex", parts) == (
        b'{"type":"lab.Complex","v":2,"values":{"parts":'
        b'["00000000-0000-0000-0000-000000000001","00000000-0000-0000-0000-000000000002"]}}'
    )


def test_rows_are_a_multiset_of_their_identity_fields() -> None:
    """A test vector: rows of parts, by part, copies and role, in any order: a multiset."""
    heavy, light = uuid.UUID(int=1), uuid.UUID(int=2)
    row = Kind("row", row=(("part", Kind("ref")), ("copies", Kind("int")), ("role", Kind("enum"))))
    rows = Kind("list", items=row)
    given = [
        {"part": light, "copies": 1, "role": "light", "chain": "L"},
        {"part": heavy, "copies": 2, "role": "heavy", "chain": "H"},
    ]
    assert encode("lab.Bispecific", {"components": (rows, given)}) == (
        b'{"type":"lab.Bispecific","v":2,"values":{"components":['
        b'{"copies":1,"part":"00000000-0000-0000-0000-000000000002","role":"light"},'
        b'{"copies":2,"part":"00000000-0000-0000-0000-000000000001","role":"heavy"}]}}'
    )
    # The chain's label isn't part of it; the order isn't either.
    relabelled = [{**given[1], "chain": "X"}, given[0]]
    assert identity_hash("lab.Bispecific", {"components": (rows, given)}) == identity_hash(
        "lab.Bispecific", {"components": (rows, relabelled)}
    )


def test_a_sequence_is_placed_in_the_one_its_identity_found() -> None:
    # GTCCAA is AAGTCC from its fifth base: its first base is AAGTCC's third.
    found = place("dna_sequence", "GTCCAA", "AAGTCC", circular=True, double=True)
    assert found == Placement(offset=4, reverse=False)
    assert [found.position(p, 6) for p in (1, 5, 6)] == [3, 1, 2]
    # GGACTT is AAGTCC's other strand: its first base pairs with AAGTCC's last.
    found = place("dna_sequence", "GGACTT", "AAGTCC", circular=True, double=True)
    assert found == Placement(offset=0, reverse=True)
    assert [found.position(p, 6) for p in (1, 6)] == [6, 1]
    # A single strand isn't placed on its complement.
    assert place("dna_sequence", "GGACTT", "AAGTCC", circular=True) is None
    assert place("protein_sequence", "mkwv*", "MKWV") == Placement()


@pytest.mark.wasm
def test_helm_is_read_canonical() -> None:
    """ADR 0144: one molecule written two ways is one identity, a modified one another."""
    kind = Kind("string", format="helm")
    same = [
        "RNA1{[R](A)[sP]'2'.R(C)}$$$$",
        'RNA1{R(A)[sP].R(A)[sP].R(C)}"an annotation"$$$$V2.0',
    ]
    assert {canonical(kind, h) for h in same} == {"RNA1{R(A)[sP].R(A)[sP].R(C)}$$$$V2.0"}
    assert canonical(kind, "RNA1{R(A)P.R(A)[sP].R(C)}$$$$V2.0") not in {
        canonical(kind, h) for h in same
    }
    with pytest.raises(ValueError, match="Expected a monomer"):
        canonical(kind, "RNA1{R(A).}$$$$V2.0")
