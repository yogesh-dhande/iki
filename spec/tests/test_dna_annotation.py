"""The server's Wasm module exposes the feature matcher used by the action."""

import pytest

from iki_spec import wasm

pytestmark = pytest.mark.skipif(not wasm.WASM.exists(), reason="needs iki-wasm built")


def test_dna_annotation_uses_genbank_coordinates_and_provenance() -> None:
    # OL989372.1 has the T7 feature at 4984..5002. Search that exact interval with
    # synthetic flanks to verify coordinates relative to the supplied bases.
    result = wasm.call(
        "iki_annotate_dna_sequence",
        {
            "bases": "NTAATACGACTCACTATAGGNN",
            "circular": False,
            "double_stranded": True,
        },
        wasm.FUEL,
    )
    assert result["annotated_with"] == "Routine cloning-vector features 1.0"
    (feature,) = result["features"]
    assert (feature["name"], feature["start"], feature["end"], feature["strand"]) == (
        "T7 promoter",
        2,
        20,
        "forward",
    )
    assert feature["qualifiers"]["source"].startswith("INSDC OL989372.1 4984..5002 ")
