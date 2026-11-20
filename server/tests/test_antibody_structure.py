"""The antibody plugin's structure prediction: an antibody's chains, which its input includes
(ADR 0105), through ImmuneBuilder, into a predicted protein structure.

ImmuneBuilder needs PyTorch and its weights, which are in the plugin's image, not here: these
tests put stand-ins on the path that record what they were asked and answer with a fixed
model. The image's own check runs the real one."""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from iki_spec import schemas

SCRIPT = Path(__file__).resolve().parents[2] / "plugins" / "antibody" / "scripts" / "structure.py"

#: Stand-in torch: the script only sets its threads.
TORCH = "def set_num_threads(n):\n    pass\n"

#: Stand-in ImmuneBuilder: each builder records what it's asked, numbers each chain's
#: residues from 1 (so CDR-H3, IMGT 105 to 117, is the heavy chain's residues 105 to 117
#: when it's that long), and estimates each residue's error as its number over 100.
IMMUNEBUILDER = """\
import json, os
from pathlib import Path

class _Array:
    def __init__(self, rows):
        self.rows = rows
    def mean(self, axis):
        if axis == 0:
            return _Array([sum(c) / len(c) for c in zip(*self.rows)])
        raise NotImplementedError
    def sqrt(self):
        return _Array([v ** 0.5 for v in self.rows])
    def cpu(self):
        return self
    def numpy(self):
        return _Values(self.rows)

class _Values(list):
    def mean(self):
        return sum(self) / len(self)

class _Model:
    def __init__(self, sequences):
        self.numbered_sequences = {
            chain: [((i, " "), aa) for i, aa in enumerate(s, start=1)]
            for chain, s in sequences.items()
        }
        errors = [(i / 100) ** 2 for s in sequences.values() for i in range(1, len(s) + 1)]
        self.error_estimates = _Array([errors] * 4)
    def save(self, filename):
        Path(filename).write_text("REMARK  MODELLED\\nEND\\n")

class _Builder:
    def __init__(self, weights_dir=None):
        self.weights_dir = weights_dir
    def predict(self, sequences):
        if os.environ.get("REFUSE"):
            raise AssertionError(os.environ["REFUSE"])
        calls = Path(os.environ["CALLED"])
        calls.write_text(json.dumps(
            {"builder": type(self).__name__, "weights": self.weights_dir, "sequences": sequences}
        ))
        return _Model(sequences)

class ABodyBuilder2(_Builder):
    pass

class NanoBodyBuilder2(_Builder):
    pass
"""

HEAVY = "EVQLVESGGGLVQPGGSLRLSCAASGFNIKDTYIHWVRQAPGKGLEWVARIYPTNGYTRYADSVKGRFTISADTSKNTAYLQMNSLRAEDTAVYYCSRWGGDGFYAMDYWGQGTLVTVSS"  # noqa: E501
LIGHT = "DIQMTQSPSSLSASVGDRVTITCRASQDVNTAVAWYQQKPGKAPKLLIYSASFLYSGVPSRFSGSRSGTDFTLTISSLQPEDFATYYCQQHYTTPPTFGQGTKVEIK"  # noqa: E501


def protein(i: int, sequence: str) -> dict[str, Any]:
    return {
        "id": f"00000000-0000-0000-0000-{i:012d}",
        "hrid": f"PRT-{i:06d}",
        "type": "iki/protein.Protein",
        "schema_version": 2,
        "revision": 1,
        "values": {"sequence": sequence},
    }


def antibody(form: str, heavy: dict[str, Any], light: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "id": "00000000-0000-0000-0000-000000000100",
        "hrid": "ABY-000001",
        "type": "iki/antibody.Antibody",
        "schema_version": 2,
        "revision": 1,
        # Its chains are its parts (ADR 0161), included in the rows' order.
        "values": {
            "name": "Trastuzumab",
            "format": form,
            "components": [
                {"part": heavy["id"], "copies": 1, "role": "heavy"},
                *([{"part": light["id"], "copies": 1, "role": "light"}] if light else []),
            ],
        },
        "included": {"components.part": [heavy, *([light] if light else [])]},
    }


def run(
    tmp_path: Path, given: dict[str, Any], refuse: str = ""
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any] | None, dict[str, Any] | None]:
    fake = tmp_path / "fake"
    (fake / "ImmuneBuilder").mkdir(parents=True, exist_ok=True)
    (fake / "ImmuneBuilder" / "__init__.py").write_text(IMMUNEBUILDER)
    (fake / "torch.py").write_text(TORCH)
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    (tmp_path / "input.json").write_text(json.dumps({"inputs": {"antibody": [given]}}))
    result = subprocess.run(  # noqa: S603 - our own script
        [sys.executable, str(SCRIPT)],
        env={
            "PATH": os.environ["PATH"],
            "PYTHONPATH": str(fake),
            "IKI_INPUT": str(tmp_path / "input.json"),
            "IKI_OUTPUT": str(tmp_path / "output.json"),
            "IKI_OUT_DIR": str(out),
            "IMMUNEBUILDER_WEIGHTS": "/opt/immunebuilder/weights",
            "CALLED": str(tmp_path / "called.json"),
            **({"REFUSE": refuse} if refuse else {}),
        },
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    output = None
    if (tmp_path / "output.json").exists():
        output = json.loads((tmp_path / "output.json").read_text())
        assert not list(schemas.check("run-output", output))
    called = tmp_path / "called.json"
    return result, output, json.loads(called.read_text()) if called.exists() else None


def test_an_igg_is_modelled_from_its_two_chains(tmp_path: Path) -> None:
    heavy, light = protein(1, HEAVY + "*"), protein(2, LIGHT.lower())

    result, output, called = run(tmp_path, antibody("igg", heavy, light))

    assert result.returncode == 0, result.stderr
    assert called == {
        "builder": "ABodyBuilder2",
        "weights": "/opt/immunebuilder/weights",
        "sequences": {"H": HEAVY, "L": LIGHT},
    }
    assert output is not None
    (made,) = output["creates"]
    values = made["values"]
    assert made["type"] == "iki/protein.ProteinStructure"
    assert values["structure"] == {"$file": "ABY-000001.pdb", "media_type": "chemical/x-pdb"}
    assert (tmp_path / "out" / "ABY-000001.pdb").read_text().startswith("REMARK")
    assert values["name"] == "Trastuzumab, predicted by ABodyBuilder2"
    assert values["method"] == "predicted"
    assert values["chains"] == [heavy["id"], light["id"]]
    assert values["chain_ids"] == ["H", "L"]
    # Each residue's error is its number over 100: the mean over both chains', and over
    # the heavy chain's 105 to 117.
    numbers = [*range(1, len(HEAVY) + 1), *range(1, len(LIGHT) + 1)]
    assert values["predicted_error"] == round(sum(numbers) / len(numbers) / 100, 3)
    assert values["cdr_h3_predicted_error"] == 1.11
    assert output["metrics"] == {
        "predicted_error": values["predicted_error"],
        "cdr_h3_predicted_error": 1.11,
    }
    assert "Modelling Trastuzumab with ABodyBuilder2" in result.stdout
    assert "::progress" in result.stdout


def test_a_vhh_is_modelled_as_a_single_domain(tmp_path: Path) -> None:
    heavy = protein(1, HEAVY)

    result, output, called = run(tmp_path, antibody("vhh", heavy, None))

    assert result.returncode == 0, result.stderr
    assert called is not None
    assert (called["builder"], called["sequences"]) == ("NanoBodyBuilder2", {"H": HEAVY})
    assert output is not None
    values = output["creates"][0]["values"]
    assert (values["chains"], values["chain_ids"]) == ([heavy["id"]], ["H"])
    assert values["name"] == "Trastuzumab, predicted by NanoBodyBuilder2"


def test_an_scfv_is_modelled_from_both_domains_of_its_one_chain(tmp_path: Path) -> None:
    scfv = protein(1, HEAVY + "GGGGSGGGGSGGGGS" + LIGHT)

    result, output, called = run(tmp_path, antibody("scfv", scfv, None))

    assert result.returncode == 0, result.stderr
    assert called is not None
    sequence = scfv["values"]["sequence"]
    # ImmuneBuilder finds each domain in it by its type.
    assert called["sequences"] == {"H": sequence, "L": sequence}
    assert output is not None
    values = output["creates"][0]["values"]
    assert (values["chains"], values["chain_ids"]) == ([scfv["id"]] * 2, ["H", "L"])


def test_a_chain_immunebuilder_refuses_fails_the_run_saying_why(tmp_path: Path) -> None:
    refused = "Sequence provided as an L chain is not recognised as an L chain."

    result, output, _ = run(
        tmp_path, antibody("igg", protein(1, HEAVY), protein(2, HEAVY)), refuse=refused
    )

    assert result.returncode == 1
    assert output is None
    assert (
        "::error Trastuzumab: Sequence provided as an L chain is not recognised as an L chain."
        in result.stdout
    )


def test_it_uses_as_many_threads_as_its_container_has_cpus(tmp_path: Path) -> None:
    """A pod's CPU limit, not the machine's CPUs: more threads would only wait their turn."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("structure", SCRIPT)
    assert spec is not None and spec.loader is not None
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    quota = tmp_path / "cpu.max"
    for written, cpus in (("150000 100000\n", 2), ("100000 100000\n", 1), ("50000 100000", 1)):
        quota.write_text(written)
        assert script.cpus(quota) == cpus
    quota.write_text("max 100000\n")
    assert script.cpus(quota) >= 1
    assert script.cpus(tmp_path / "missing") >= 1


def test_a_chain_of_two_binding_heads_isnt_modelled_as_one(tmp_path: Path) -> None:
    """A tandem scFv, two scFvs on one chain as blinatumomab is (FDA's pharmacology review
    of Blincyto, BLA 125557): ImmuneBuilder would model its first heavy and first light
    domains alone, so the run refuses it, saying which domains numbering found, rather
    than model half of it as the whole."""
    linker = "GGGGS" * 3
    parts = [HEAVY, linker, LIGHT, "GGGGS", HEAVY, linker, LIGHT]
    tandem = protein(1, "".join(parts))
    domains, at = [], 0
    for part, kind in zip(parts, ["H", "", "K", "", "H", "", "K"], strict=True):
        if kind:
            domains.append({"chain": kind, "start": at + 1, "end": at + len(part)})
        at += len(part)
    tandem["values"]["variable_domains"] = domains
    given = antibody("other", tandem, None)

    result, output, called = run(tmp_path, given)

    assert result.returncode == 1
    assert output is None and called is None
    assert (
        "::error Trastuzumab: PRT-000001 has 4 variable domains (H 1-120, K 136-242, "
        "H 248-367, K 383-489), and ImmuneBuilder models one binding head (H and K/L), so "
        "it would leave the rest out: register each binding head as an antibody of its own "
        "(an scFv) to model it."
    ) in result.stdout
    # One scFv's chain, numbered, is one head.
    scfv = protein(1, HEAVY + linker + LIGHT)
    scfv["values"]["variable_domains"] = domains[:2]
    result, _, called = run(tmp_path, antibody("scfv", scfv, None))
    assert result.returncode == 0, result.stderr
