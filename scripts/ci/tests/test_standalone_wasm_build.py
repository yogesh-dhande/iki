"""A standalone pin check must ask Cargo for the module from the current source."""

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("plugin", "name"), [("dna", "oligo-checks"), ("plasmid", "plasmid-tools")]
)
@pytest.mark.parametrize("pin_matches_source", [False, True])
def test_pin_check_builds_before_hashing_a_warm_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    plugin: str,
    name: str,
    pin_matches_source: bool,
) -> None:
    filename = f"{name}.wasm"
    builder = tmp_path / "scripts" / f"build-{name}-wasm.sh"
    builder.parent.mkdir()
    shutil.copyfile(SCRIPTS / builder.name, builder)
    package = f"iki-{name}-wasm"
    cargo_output = f"{package.replace('-', '_')}.wasm"
    built = (
        tmp_path / "worker" / "target" / name / "wasm32-unknown-unknown" / "release" / cargo_output
    )
    built.parent.mkdir(parents=True)
    cached_bytes = b"the module from the previous checkout"
    source_bytes = b"the module built from this checkout"
    built.write_bytes(cached_bytes)
    compiled = tmp_path / "compiled.wasm"
    compiled.write_bytes(source_bytes)
    plugin_copy = tmp_path / "plugins" / plugin / "data" / filename
    plugin_copy.parent.mkdir(parents=True)
    plugin_copy.write_bytes(cached_bytes)
    carried = tmp_path / "spec" / "src" / "iki_spec" / "modules" / filename
    carried.parent.mkdir(parents=True)
    carried.write_bytes(cached_bytes)
    expected = hashlib.sha256(source_bytes if pin_matches_source else cached_bytes).hexdigest()
    actual = hashlib.sha256(source_bytes).hexdigest()
    pin = plugin_copy.with_suffix(".wasm.sha256")
    pin.write_text(f"{expected}  {filename}\n")

    # Stand in for Cargo's normal freshness check and compile without requiring a toolchain.
    # Use the existing Rustup path selection so a host's toolchain cannot shadow this stub.
    cargo_home = tmp_path / "cargo"
    binaries = cargo_home / "bin"
    binaries.mkdir(parents=True)
    for executable, body in {
        "rustup": "exit 1",
        "uname": 'case "$1" in -s) echo Linux ;; -m) echo x86_64 ;; *) exit 1 ;; esac',
        "cargo": (
            f'test "$1" = build && test "$4" = {package} && test "$8" = --target-dir\n'
            f'cp "$TEST_COMPILED_MODULE" "$9/wasm32-unknown-unknown/release/{cargo_output}"'
        ),
    }.items():
        stub = binaries / executable
        stub.write_text(f"#!/bin/sh\nset -e\n{body}\n")
        stub.chmod(0o755)
    monkeypatch.setenv("CARGO_HOME", str(cargo_home))
    monkeypatch.setenv("PATH", str(binaries), prepend=":")
    monkeypatch.setenv("TEST_COMPILED_MODULE", str(compiled))

    result = subprocess.run(  # noqa: S603 - the checkout's build script and a stub compiler
        ["/bin/sh", str(builder), "--check"], capture_output=True, check=False, text=True
    )
    if pin_matches_source:
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode != 0
        assert f"{filename} SHA-256 mismatch. Expected {expected}, actual {actual}" in result.stderr
    assert built.read_bytes() == source_bytes
    assert plugin_copy.read_bytes() == carried.read_bytes() == cached_bytes
