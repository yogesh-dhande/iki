"""The server's image may supply native code for its packaged WebAssembly module."""

import hashlib
import json
import logging
import os
import stat
import threading
from importlib.metadata import version
from pathlib import Path
from unittest.mock import patch

import pytest
import wasmtime

from iki_spec import rules, wasm

BUNDLED_WASM = wasm.WASM
TARGET = "x86_64-unknown-linux-gnu"
NATIVE = "iki_wasm.cwasm"
SIDECAR = "iki_wasm.json"


@pytest.fixture
def image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A private stand-in for the image directory, with a fresh runtime for each test."""
    location = tmp_path / "image"
    source = tmp_path / "package" / "iki_wasm.wasm"
    source.parent.mkdir()
    source.write_bytes(wasmtime.wat2wasm('(module (func (export "source_module")))'))
    monkeypatch.setattr(wasm, "WASM", source)
    monkeypatch.setattr(wasm, "_IMAGE_WASM", location)
    monkeypatch.setattr(wasm, "_host_target", lambda: TARGET)
    monkeypatch.setattr(wasm, "_compiled", None)
    monkeypatch.setattr(wasm, "_local", threading.local())
    return location


@pytest.fixture
def root_owned_image(image: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Emulate the root-owned mount without removing symlink or permission checks."""
    lstat = Path.lstat

    def image_stat(path: Path) -> os.stat_result:
        found = lstat(path)
        if path not in image.parents and path != image and path.parent != image:
            return found
        fields = list(found)
        fields[stat.ST_UID] = 0
        if path in image.parents:
            # Temporary directories above the fixture stand in for immutable /opt/iki.
            fields[stat.ST_MODE] = found.st_mode & ~0o022
        return os.stat_result(fields)

    monkeypatch.setattr(Path, "lstat", image_stat)
    return image


def packaged_pair(location: Path) -> dict[str, object]:
    location.mkdir(parents=True)
    config = wasmtime.Config()
    config.consume_fuel = True
    config.parallel_compilation = False
    module = wasmtime.Module.from_file(wasmtime.Engine(config), str(wasm.WASM))
    native = module.serialize()
    (location / NATIVE).write_bytes(native)
    metadata = {
        "format": 1,
        "consume_fuel": True,
        "wasm_sha256": hashlib.sha256(wasm.WASM.read_bytes()).hexdigest(),
        "wasmtime_version": version("wasmtime"),
        "target": TARGET,
        "cwasm_sha256": hashlib.sha256(native).hexdigest(),
    }
    (location / SIDECAR).write_text(json.dumps(metadata))
    return metadata


def bounded_source_compile() -> None:
    """Observe the settings used to load the source and the engine's actual fuel support."""
    parallel: list[bool] = []
    setter = wasmtime.Config.parallel_compilation.fset
    assert setter is not None

    def set_parallel(config: wasmtime.Config, enabled: bool) -> None:
        parallel.append(enabled)
        setter(config, enabled)

    with (
        patch.object(wasmtime.Config, "parallel_compilation", property(fset=set_parallel)),
        patch.object(
            wasmtime.Module, "deserialize_file", side_effect=AssertionError("native load")
        ),
        patch.object(wasmtime.Module, "deserialize", side_effect=AssertionError("native load")),
        patch.object(wasmtime.Module, "from_file", wraps=wasmtime.Module.from_file) as compile_wasm,
    ):
        engine, module = wasm._module()
        assert wasm._module() == (engine, module)
        assert [export.name for export in module.exports] == ["source_module"]
        store = wasmtime.Store(engine)
        store.set_fuel(123)
        assert store.get_fuel() == 123
        compile_wasm.assert_called_once_with(engine, str(wasm.WASM))
    assert parallel == [False]


@pytest.mark.parametrize(
    ("key", "replacement"),
    [
        ("wasm_sha256", "0" * 64),
        ("wasmtime_version", "0.0.0"),
        ("target", "aarch64-unknown-linux-gnu"),
        ("cwasm_sha256", "0" * 64),
        ("format", 2),
        ("consume_fuel", False),
    ],
)
def test_incompatible_artifact_warns_and_compiles_only_packaged_wasm(
    root_owned_image: Path,
    caplog: pytest.LogCaptureFixture,
    key: str,
    replacement: object,
) -> None:
    metadata = packaged_pair(root_owned_image)
    metadata[key] = replacement
    (root_owned_image / SIDECAR).write_text(json.dumps(metadata))
    with caplog.at_level(logging.WARNING, logger="iki_spec.wasm"):
        bounded_source_compile()
    assert "Compiling packaged Wasm" in caplog.text


@pytest.mark.parametrize("unsafe", ["writable_file", "writable_directory", "symlink", "missing"])
def test_unsafe_or_incomplete_image_pair_is_never_loaded(
    root_owned_image: Path, unsafe: str, caplog: pytest.LogCaptureFixture
) -> None:
    packaged_pair(root_owned_image)
    native = root_owned_image / NATIVE
    if unsafe == "writable_file":
        native.chmod(0o666)
    elif unsafe == "writable_directory":
        root_owned_image.chmod(0o777)
    elif unsafe == "symlink":
        target = native.rename(root_owned_image / "other.cwasm")
        native.symlink_to(target)
    else:
        native.unlink()
    with caplog.at_level(logging.WARNING, logger="iki_spec.wasm"):
        bounded_source_compile()
    assert "Compiling packaged Wasm" in caplog.text


def test_native_code_outside_image_is_never_loaded(
    image: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Valid copies in a writable plugin, beside the source, or under either cache directory
    # must never become executable inputs to Wasmtime.
    directories = [tmp_path / "plugin", tmp_path / "cache", tmp_path / "temp"]
    for path in directories:
        packaged_pair(path)
    for name in (NATIVE, SIDECAR):
        (wasm.WASM.parent / name).write_bytes((directories[0] / name).read_bytes())
    monkeypatch.chdir(directories[0])
    monkeypatch.setenv("XDG_CACHE_HOME", str(directories[1]))
    monkeypatch.setenv("TMPDIR", str(directories[2]))
    assert not image.exists()
    bounded_source_compile()


def test_image_serves_real_guards_without_runtime_compilation(
    root_owned_image: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert BUNDLED_WASM.is_file(), f"Missing {BUNDLED_WASM}. Run scripts/build-wasm.sh."
    monkeypatch.setattr(wasm, "WASM", BUNDLED_WASM)
    packaged_pair(root_owned_image)
    guard = 'purpose == "routine"'
    variables = {"purpose": {"type": "string"}}
    contexts = [{"purpose": "routine"}, {"purpose": "qa_sampling"}]
    raw: dict[str, object] = {
        "variables": variables,
        "expressions": [{"source": guard, "kind": rules.CONDITION}],
        "contexts": contexts,
    }
    deserialize = wasmtime.Module.deserialize_file

    def load_checked(engine: wasmtime.Engine, path: str) -> wasmtime.Module:
        # Wasmtime reads the same open inode whose digest Iki verified.
        assert Path(path).parent == Path("/dev/fd")
        assert os.path.samestat(os.fstat(int(Path(path).name)), (root_owned_image / NATIVE).stat())
        return deserialize(engine, path)

    with (
        patch.object(wasmtime.Module, "from_file", side_effect=AssertionError("cold compilation")),
        patch.object(wasmtime.Module, "deserialize_file", side_effect=load_checked) as load_native,
    ):
        for _ in range(2):
            outcomes = rules.evaluate(variables, [(guard, rules.CONDITION)], contexts)
            assert [row[0].value for row in outcomes] == [True, False]
        with pytest.raises(wasm.UnavailableError, match="fuel"):
            wasm.call("iki_evaluate_rules", raw, fuel=1)
        outcomes = rules.evaluate(variables, [(guard, rules.CONDITION)], contexts)
        assert [row[0].value for row in outcomes] == [True, False]
        load_native.assert_called_once()
