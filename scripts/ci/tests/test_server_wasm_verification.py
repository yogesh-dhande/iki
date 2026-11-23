"""The server build checks the exported modules against source-controlled pins."""

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

VERIFY = Path(__file__).resolve().parents[2] / "verify-server-wasm.sh"
PINS = {
    "iki_wasm.wasm": "server/iki_wasm.wasm.sha256",
    "dna-annotation.wasm": "plugins/plasmid/data/dna-annotation.wasm.sha256",
    "oligo-measurement.wasm": "plugins/dna/data/oligo-measurement.sha256",
}


@pytest.fixture
def payload(tmp_path: Path) -> tuple[Path, Path]:
    """Place synthetic files and their pins in independent directories."""
    source = tmp_path / "source"
    script = source / "scripts" / VERIFY.name
    script.parent.mkdir(parents=True)
    shutil.copyfile(VERIFY, script)
    artifact = tmp_path / "export"
    artifact.mkdir()
    for name, pin in PINS.items():
        data = name.encode()
        (artifact / name).write_bytes(data)
        marker = source / pin
        marker.parent.mkdir(parents=True)
        marker.write_text(hashlib.sha256(data).hexdigest() + "\n")
    (artifact / "rust-standard-library.html").write_text("Synthetic notice")
    return script, artifact


def verified(payload: tuple[Path, Path]) -> subprocess.CompletedProcess[str]:
    script, artifact = payload
    return subprocess.run(  # noqa: S603 - runs the repository's verifier with fixture paths
        ["/bin/sh", str(script), str(artifact)], capture_output=True, check=False, text=True
    )


def test_verifies_all_modules(payload: tuple[Path, Path]) -> None:
    result = verified(payload)
    assert result.returncode == 0, result.stderr
    for name in PINS:
        assert name in result.stdout


@pytest.mark.parametrize("name", PINS)
def test_verification_names_the_bad_module_and_both_digests(
    payload: tuple[Path, Path], name: str
) -> None:
    script, artifact = payload
    changed = b"synthetic replaced module"
    (artifact / name).write_bytes(changed)
    # A hash provided by the artifact itself cannot override the tracked pin.
    actual = hashlib.sha256(changed).hexdigest()
    (artifact / (name + ".sha256")).write_text(actual + "\n")
    expected = (script.parents[1] / PINS[name]).read_text().strip()
    result = verified(payload)
    assert result.returncode != 0
    assert name in result.stderr and expected in result.stderr and actual in result.stderr


@pytest.mark.parametrize("name", [*PINS, "rust-standard-library.html"])
def test_fails_before_using_an_incomplete_payload(payload: tuple[Path, Path], name: str) -> None:
    _, artifact = payload
    (artifact / name).unlink()
    result = verified(payload)
    assert result.returncode != 0
    assert name in result.stderr
