"""The Tecan worklist writer, run as Iki runs it: its module under Iki's sandbox runtime,
given transfers as an export gives them, the worklist checked line by line as Freedom
EVOware and FluentControl read it.

Build the module first (`make test-wasm` in Iki's checkout, or
`scripts/build-writer-wasm.sh writers/tecan-worklist --build-only`). The writer's own Rust
tests (`cargo test` in writers/tecan-worklist) cover each rule natively."""

import json
from pathlib import Path
from typing import Any

import pytest
import wasmtime

from iki_spec.validate import load_plugin
from iki_spec.writers.check import check_module
from iki_spec.writers.protocol import canonical
from iki_spec.writers.runtime import Instance, engine

EXAMPLE = Path(__file__).resolve().parents[1]
MODULE = EXAMPLE / "writers" / "tecan-worklist.wasm"
WRITER = "examples/liquid_handling.tecan-worklist"

pytestmark = pytest.mark.skipif(not MODULE.is_file(), reason="build the writer's module first")


def plate(hrid: str, label: str, wells: str, **fields: str) -> dict[str, Any]:
    return {
        "fields": {"label": label, "wells": wells, **fields},
        "hrid": hrid,
        "id": f"0190e9a4-0000-7000-8000-{hrid[-6:]:0>12}",
        "name": label,
        "revision": 1,
        "type": "examples/liquid_handling.Plate",
    }


SOURCE = plate("PLT-000001", "Compound source 1", "plate_384", barcode="CP-0412")
ASSAY = plate("PLT-000002", "Assay plate 7", "plate_96", labware_type="96 Well Corning Flat")


def transfer(at: str, to: str, volume: float, **roles: Any) -> dict[str, Any]:
    return {
        "hrid": "TRF-000001",
        "id": "0190e9a4-0000-7000-8000-000000000101",
        "latest": True,
        "name": f"{at} to {to}",
        "revised_at": "2026-11-25T09:00:00Z",
        "revision": 1,
        "roles": {
            "destination_plate": ASSAY,
            "destination_well": to,
            "source_plate": SOURCE,
            "source_well": at,
            "volume": {"unit": "uL", "value": volume},
            **roles,
        },
        "type": "examples/liquid_handling.Transfer",
        "withheld": [],
    }


def written(*records: dict[str, Any]) -> list[tuple[int, bytes, list[dict[str, Any]]]]:
    compiler = engine()
    writer = Instance(wasmtime.Module(compiler, MODULE.read_bytes()), compiler, 16 * 1024 * 1024)
    begin = {
        "abi": 1,
        "format": "examples/liquid_handling.tecan-gwl",
        "records": len(records),
        "writer": WRITER,
    }
    calls = [("begin", begin), *(("record", r) for r in records), ("end", {})]
    return [
        (done.status, done.output, done.notes)
        for done in (writer.call(entry, canonical(request)) for entry, request in calls)
    ]


def test_the_plugin_and_its_module_are_what_iki_runs() -> None:
    loaded = load_plugin(EXAMPLE)
    assert [p for p in loaded.problems if p.is_error] == []
    assert check_module(MODULE.read_bytes()) == []


def test_each_transfer_is_an_aspirate_a_dispense_and_a_wash() -> None:
    _, (status, out, notes), _ = written(transfer("B3", "A2", 2.5, liquid_class="DMSO Free Single"))
    assert (status, notes) == (0, [])
    assert out.split(b"\r\n") == [
        # B3 of a 384-well plate is position 34: 16 rows a column, counted down the columns.
        b"A;Compound source 1;CP-0412;;34;;2.5;DMSO Free Single;;;",
        # A2 of a 96-well plate is 9.
        b"D;Assay plate 7;;96 Well Corning Flat;9;;2.5;DMSO Free Single;;;",
        b"W;",
        b"",
    ]


def test_a_volume_finer_than_a_hundredth_of_a_microlitre_is_rounded_and_noted() -> None:
    _, (_, out, notes), _ = written(transfer("A1", "A1", 1.2345))
    assert b";;1.23;" in out
    assert [n["code"] for n in notes] == ["rounded"]
    assert notes[0]["role"] == "volume"


@pytest.mark.parametrize(
    ("record", "why"),
    [
        (transfer("Q1", "A1", 1), "isn't a well of a 384-well plate"),
        (transfer("A1", "I1", 1), "isn't a well of a 96-well plate"),
        (transfer("A1", "A1", 0), "volume"),
        ({**transfer("A1", "A1", 1), "withheld": ["source_plate"]}, "isn't a record you can read"),
    ],
)
def test_a_transfer_a_worklist_cant_say_is_refused(record: dict[str, Any], why: str) -> None:
    if record["withheld"]:
        del record["roles"]["source_plate"]
    _, (status, out, notes), _ = written(record)
    assert (status, out) == (1, b"")
    assert notes[0]["code"] == "refused"
    assert why in notes[0]["message"], json.dumps(notes)
