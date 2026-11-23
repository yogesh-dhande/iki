"""The server build checks raw and platform-specific modules against source-controlled pins."""

import hashlib
import json
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

VERIFY = Path(__file__).resolve().parents[2] / "verify-server-wasm.sh"
PINS = {
    "iki_wasm.wasm": "server/iki_wasm.wasm.sha256",
    "dna-annotation.wasm": "plugins/plasmid/data/dna-annotation.wasm.sha256",
    "oligo-measurement.wasm": "plugins/dna/data/oligo-measurement.sha256",
}
TARGETS = {"amd64": ("x86_64", 62), "arm64": ("aarch64", 183)}


@pytest.fixture
def payload(tmp_path: Path) -> tuple[Path, Path]:
    """Place synthetic files and their pins in independent directories."""
    source = tmp_path / "source"
    script = source / "scripts" / VERIFY.name
    script.parent.mkdir(parents=True)
    shutil.copyfile(VERIFY, script)
    shutil.copy(VERIFY.with_name("verify-server-aot.py"), script.parent)
    (source / "uv.lock").write_text('[[package]]\nname = "wasmtime"\nversion = "50.0.1"\n')
    artifact = tmp_path / "export"
    artifact.mkdir()
    for name, pin in PINS.items():
        data = name.encode()
        (artifact / name).write_bytes(data)
        marker = source / pin
        marker.parent.mkdir(parents=True)
        marker.write_text(hashlib.sha256(data).hexdigest() + "\n")
    (artifact / "rust-standard-library.html").write_text("Synthetic notice")
    wasm_hash = (source / PINS["iki_wasm.wasm"]).read_text().strip()
    for arch, (cpu, machine) in TARGETS.items():
        directory = artifact / arch
        directory.mkdir()
        # Minimal Wasmtime core module ELF header. Verification never executes this fixture.
        code = bytearray(64)
        code[:8] = b"\x7fELF\x02\x01\x01\xc8"
        struct.pack_into("<HHI", code, 16, 1, machine, 1)
        struct.pack_into("<I", code, 48, 1)
        (directory / "iki_wasm.cwasm").write_bytes(code)
        native_hash = hashlib.sha256(code).hexdigest()
        (source / "server" / f"iki_wasm.{arch}.cwasm.sha256").write_text(native_hash + "\n")
        metadata = {
            "format": 1,
            "target": f"{cpu}-unknown-linux-gnu",
            "consume_fuel": True,
            "wasmtime_version": "50.0.1",
            "wasm_sha256": wasm_hash,
            "cwasm_sha256": native_hash,
        }
        (directory / "iki_wasm.json").write_text(json.dumps(metadata))
    return script, artifact


def verified(payload: tuple[Path, Path], *options: str) -> subprocess.CompletedProcess[str]:
    script, artifact = payload
    return subprocess.run(  # noqa: S603 - runs the repository's verifier with fixture paths
        ["/bin/sh", str(script), *options, str(artifact)],
        capture_output=True,
        check=False,
        text=True,
    )


def test_verifies_all_modules(payload: tuple[Path, Path]) -> None:
    result = verified(payload)
    assert result.returncode == 0, result.stderr
    for name in PINS:
        assert name in result.stdout
    for arch in TARGETS:
        assert f"{arch} precompiled module" in result.stdout


def test_dev_cli_can_stage_just_the_portable_files(payload: tuple[Path, Path]) -> None:
    _, artifact = payload
    for arch in TARGETS:
        shutil.rmtree(artifact / arch)
    result = verified(payload, "--portable-only")
    assert result.returncode == 0, result.stderr
    for name in PINS:
        assert name in result.stdout
    assert "precompiled" not in result.stdout


@pytest.mark.parametrize("arch", TARGETS)
def test_image_requires_each_native_target(payload: tuple[Path, Path], arch: str) -> None:
    _, artifact = payload
    shutil.rmtree(artifact / arch)
    result = verified(payload)
    assert result.returncode != 0
    assert str(artifact / arch / "iki_wasm.cwasm") in result.stderr


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
@pytest.mark.parametrize("options", [(), ("--portable-only",)])
def test_fails_before_using_an_incomplete_payload(
    payload: tuple[Path, Path], name: str, options: tuple[str, ...]
) -> None:
    _, artifact = payload
    (artifact / name).unlink()
    result = verified(payload, *options)
    assert result.returncode != 0
    assert name in result.stderr


@pytest.mark.parametrize("arch", TARGETS)
def test_replacing_native_code_and_its_manifest_still_fails_the_checkout_pin(
    payload: tuple[Path, Path], arch: str
) -> None:
    _, artifact = payload
    code = artifact / arch / "iki_wasm.cwasm"
    manifest = code.with_name("iki_wasm.json")
    metadata = json.loads(manifest.read_text())
    expected = metadata["cwasm_sha256"]
    replacement = code.read_bytes() + b"changed payload"
    actual = hashlib.sha256(replacement).hexdigest()
    code.write_bytes(replacement)
    manifest.write_text(json.dumps({**metadata, "cwasm_sha256": actual}))
    result = verified(payload)
    assert result.returncode != 0
    assert str(code) in result.stderr and expected in result.stderr and actual in result.stderr


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("target", "aarch64-unknown-linux-gnu"),
        ("wasmtime_version", "49.0.0"),
        ("wasm_sha256", "a" * 64),
        ("cwasm_sha256", "b" * 64),
        ("consume_fuel", False),
        ("format", True),
    ],
)
def test_native_metadata_must_match_the_target_locked_engine_and_source(
    payload: tuple[Path, Path], field: str, value: str | bool
) -> None:
    _, artifact = payload
    manifest = artifact / "amd64" / "iki_wasm.json"
    metadata = json.loads(manifest.read_text())
    manifest.write_text(json.dumps({**metadata, field: value}))
    result = verified(payload)
    assert result.returncode != 0
    assert str(manifest) in result.stderr


def test_the_arm64_pin_cannot_bless_a_module_for_the_wrong_cpu(payload: tuple[Path, Path]) -> None:
    script, artifact = payload
    arm = artifact / "arm64"
    code = (artifact / "amd64" / "iki_wasm.cwasm").read_bytes()
    digest = hashlib.sha256(code).hexdigest()
    (arm / "iki_wasm.cwasm").write_bytes(code)
    (script.parents[1] / "server/iki_wasm.arm64.cwasm.sha256").write_text(digest + "\n")
    manifest = arm / "iki_wasm.json"
    metadata = json.loads(manifest.read_text())
    manifest.write_text(json.dumps({**metadata, "cwasm_sha256": digest}))
    result = verified(payload)
    assert result.returncode != 0
    assert "aarch64" in result.stderr and str(arm / "iki_wasm.cwasm") in result.stderr
