"""An instrument's use names a measured result, never an equipment record, whichever
type the lab gave that record."""

import runpy
from pathlib import Path

import pytest

SCRIPT = runpy.run_path(str(Path(__file__).parents[1] / "scripts/equipment.py"))
INSTRUMENT = "00000000-0000-7000-8003-000000000001"


def use_of(result):
    return {
        "inputs": {
            "instrument": [{"id": INSTRUMENT, "values": {"name": "UV-Vis spectrophotometer"}}],
            "result": [result],
            "calibration": [
                {
                    "id": "00000000-0000-7000-8003-000000000003",
                    "revision": 1,
                    "values": {
                        "instrument": INSTRUMENT,
                        "kind": "calibration",
                        "outcome": "in_tolerance",
                        "review": "signed_off",
                        "performed_at": "2026-01-05T09:00:00Z",
                        "valid_until": "2026-02-05T09:00:00Z",
                    },
                }
            ],
        },
        "params": {"measurement_at": "2026-01-05T10:00:00Z"},
    }


def record(type_name, *extends):
    return {
        "id": "00000000-0000-7000-8003-000000000002",
        "revision": 1,
        "type": type_name,
        "types": [type_name, *extends, "iki/core.Entity"],
        "values": {},
    }


def test_a_measured_result_is_recorded():
    (use,) = SCRIPT["record_use"](use_of(record("iki/core.Collection")))
    assert use["values"]["result"] == "00000000-0000-7000-8003-000000000002"


@pytest.mark.parametrize(
    "result",
    [
        record("iki/equipment.Instrument"),
        # A lab's balance, a kind of instrument: still not a measurement.
        record("acme/weighing.Balance", "iki/equipment.Instrument"),
    ],
)
def test_an_equipment_record_is_no_measured_result(result):
    with pytest.raises(ValueError, match="Select the actual measured result"):
        SCRIPT["record_use"](use_of(result))
