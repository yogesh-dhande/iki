"""The shared melting-temperature model and its documentation cite the same measured terms."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "source",
    [
        "worker/crates/iki-wasm/src/oligo.rs",
        "worker/crates/iki-wasm/src/thermodynamics.rs",
        "docs/adr/0200-one-implementation-places-primers-and-cuts-dna.md",
        "docs/adr/0201-a-primers-checks-use-each-fields-own-parameter-set.md",
        "plugins/plasmid/README.md",
    ],
)
def test_santalucia_parameters_temperature_and_salt_provenance(source: str) -> None:
    text = (ROOT / source).read_text().lower()
    assert "table 2" in text
    assert re.search(r"equation 3\b", text)
    assert re.search(r"equation 8\b", text)
    assert not re.search(r"\bequation(?:s 3 and)? 7\b", text)
