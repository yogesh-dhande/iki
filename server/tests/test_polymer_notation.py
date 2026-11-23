"""The polymer inspector reads stochastic objects without changing their spelling (ADR 0183)."""

import json
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

from iki_spec import schemas

SCRIPT = Path(__file__).resolve().parents[2] / "plugins" / "polymer" / "scripts" / "bigsmiles.py"
inspect = runpy.run_path(str(SCRIPT))["inspect"]

# The examples for nylon-6,6, a polyether diblock, and branched PE in the BigSMILES
# v1.1 specification: https://olsenlabmit.github.io/BigSMILES/docs/line_notation.html .
NYLON = "[<]C(=O)CCCCC(=O)[<],[>]NCCCCCCN[>]"
DIBLOCK = "{[][<]OCC[>][<]}{[>][<]OC(C)C[>][]}"
BRANCHED = "{[][$]CC[$],[$]CC([$])[$][]}"


def test_end_groups_are_distinct_from_repeats() -> None:
    explicit = inspect("[H]O{[>]" + NYLON + "[<]}[H]")
    implicit = inspect("{[]" + NYLON + ";[>]O[H],[<][H][]}")
    assert explicit["notation_status"] == implicit["notation_status"] == "checked"
    assert implicit["stochastic_objects"] == explicit["stochastic_objects"] == 1
    assert (
        implicit["repeat_units"]
        == explicit["repeat_units"]
        == [{"object": 1, "fragment": fragment} for fragment in NYLON.split(",")]
    )
    assert explicit["implicit_end_groups"] == []
    assert implicit["implicit_end_groups"] == [
        {"object": 1, "fragment": "[>]O[H]"},
        {"object": 1, "fragment": "[<][H]"},
    ]


def test_branched_and_block_copolymers_retain_fragment_placement() -> None:
    branched = inspect(BRANCHED)
    assert branched["notation_status"] == "checked"
    assert branched["repeat_units"] == [
        {"object": 1, "fragment": "[$]CC[$]"},
        {"object": 1, "fragment": "[$]CC([$])[$]"},
    ]
    block = inspect(DIBLOCK)
    assert block["stochastic_objects"] == 2
    assert block["repeat_units"] == [
        {"object": 1, "fragment": "[<]OCC[>]"},
        {"object": 2, "fragment": "[<]OC(C)C[>]"},
    ]


@pytest.mark.parametrize(
    "source",
    [
        "C=Cc1ccccc1",  # the styrene precursor alone
        "{[][$]C1CC[$][]}",  # open SMILES ring
        "{[][$]CC([$][]}",  # open branch
        "{[][>]CC[<2][]}",  # distinct descriptor IDs never pair
        "{[][$]=CC[$][]}",  # single and double bonds at the same descriptor
        "{[][>][<][]}",  # descriptors bonded to each other
        "{[][$]CC[$];[$]O[$][]}",  # two bonds on an end group
        "{[][$]CC[$];[<]O[]}",  # end group has no matching repeat
        "C{[][$]CC[$][]}",  # exterior methyl on an empty boundary
        "{[][$]=CC=[$][$]}",  # a double bond to an implicit H
        "{[][$]CC[$][]",  # unclosed object
        "{[][$]CC[$][]}.N trailing",  # trailing name is not part of BigSMILES
    ],
)
def test_invalid_notation_retains_no_partial_summary(source: str) -> None:
    result = inspect(source)
    assert result["notation_status"] == "invalid"
    assert result["notation_note"]
    for value in ("stochastic_objects", "repeat_units", "implicit_end_groups"):
        assert result[value] is None


@pytest.mark.parametrize(
    "source",
    [
        "{[]CC,CC(CC)[]}",  # abbreviated AA descriptors
        "{[][$]CC(c1ccc(cc1)C{[$][$]CC[$][$]}Br)[$][]}",  # graft, nested object
        "{[][$]CC([#R])[$][]}.{#R=CO}",  # named fragment definition
        "{[][<1[>1]]CC[>1[<1]][]}",  # multi-atom ladder descriptor
        "{[][$]" + "C" * 3000 + "[$][]}",
        "{[]" + ",".join(["[$]C[$]"] * 1025) + "[]}",
    ],
)
def test_unsupported_conventions_and_limits_report_the_boundary(source: str) -> None:
    result = inspect(source)
    assert result["notation_status"] == "unsupported"
    assert result["notation_note"]
    assert result["repeat_units"] is None


def test_batch_writes_each_diagnostic_and_replaces_stale_output(tmp_path: Path) -> None:
    # The NIST SRM 1478 certificate documents the polystyrene material. Its repeating
    # structure encodes the styrene residue; the certificate does not specify end groups.
    # https://tsapps.nist.gov/srmext/certificates/1478.pdf
    polystyrene = "{[][<]CC(c1ccccc1)[>][]}"
    records = [
        {"id": "00000000-0000-7000-8000-000000000001", "values": {"bigsmiles": polystyrene}},
        {
            "id": "00000000-0000-7000-8000-000000000002",
            "values": {"bigsmiles": "CCC", "stochastic_objects": 1},
        },
    ]
    inputs = tmp_path / "input.json"
    output = tmp_path / "output.json"
    inputs.write_text(json.dumps({"inputs": {"polymer": records}}))
    subprocess.run(  # noqa: S603 - this interpreter and the worker's plugin script
        [sys.executable, str(SCRIPT)],
        env={"IKI_INPUT": str(inputs), "IKI_OUTPUT": str(output)},
        check=True,
        timeout=20,
    )
    result = json.loads(output.read_text())
    assert not list(schemas.check("run-output", result))
    checked, invalid = result["updates"]["polymer"]
    assert checked["entity"] == records[0]["id"]
    assert checked["values"]["repeat_units"] == [{"object": 1, "fragment": "[<]CC(c1ccccc1)[>]"}]
    assert invalid["entity"] == records[1]["id"]
    assert invalid["values"]["notation_status"] == "invalid"
    assert invalid["values"]["stochastic_objects"] is None
