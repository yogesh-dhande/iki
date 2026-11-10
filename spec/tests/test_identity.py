import datetime as dt
import hashlib
import uuid

import pytest

from iki_spec.identity import canonical, encode, identity_hash


def test_the_encoding_is_canonical_json() -> None:
    values = {"bases": ("string", "ACGT"), "copies": ("int", 2)}
    assert encode("dna.DNASequence", values) == (
        b'{"type":"dna.DNASequence","v":1,"values":{"bases":"ACGT","copies":2}}'
    )
    # Field order doesn't matter.
    reordered = {"copies": ("int", 2), "bases": ("string", "ACGT")}
    assert identity_hash("dna.DNASequence", values) == identity_hash("dna.DNASequence", reordered)


def test_a_known_hash() -> None:
    """A test vector, for other implementations of the spec."""
    expected = hashlib.sha256(b'{"type":"dna.DNASequence","v":1,"values":{"bases":"ACGT"}}')
    assert identity_hash("dna.DNASequence", {"bases": ("string", "ACGT")}) == (expected.hexdigest())


def test_the_type_is_part_of_the_hash() -> None:
    values = {"code": ("string", "A1")}
    assert identity_hash("lab.Sample", values) != identity_hash("lab.Specimen", values)


def test_values_by_type() -> None:
    assert canonical("string", "é") == "é"  # NFC: one way to write é
    assert (
        encode("a.B", {"name": ("text", "café")})
        == '{"type":"a.B","v":1,"values":{"name":"café"}}'.encode()
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
