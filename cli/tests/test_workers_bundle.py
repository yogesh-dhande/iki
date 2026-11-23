"""The workers' bundle on its own: what ``iki deploy build --workers`` writes."""

import hashlib
import io
import json
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from iki_cli import workers
from iki_spec import modules
from iki_spec.validate import load_plugin

PLUGINS = Path(__file__).parents[2] / "spec" / "tests" / "plugins"
HASH = "sha256:" + "0" * 64


def _protein(tmp_path: Path, requirements: str) -> workers.WorkerPlugin:
    directory = tmp_path / "protein"
    shutil.copytree(PLUGINS / "protein", directory)
    (directory / "requirements.txt").write_text(requirements)
    loaded = load_plugin(directory, dependencies=[PLUGINS / "dna"])
    assert loaded.plugin is not None, loaded.problems
    return workers.WorkerPlugin(loaded.plugin, directory, None)


def test_pinned_packages_are_installed_by_hash_and_nothing_else(tmp_path: Path) -> None:
    item = _protein(tmp_path, f"biopython==1.84 \\\n    --hash={HASH}\n")
    workers.write([item], tmp_path / "bundle", "/envs")
    script = (tmp_path / "bundle" / workers.SCRIPT).read_text()
    assert (
        "uv pip install --quiet --python /envs/acme__protein/protein-tools --require-hashes "
        '--no-deps --requirement "$here"/plugins/acme__protein/requirements.txt'
    ) in script


def test_an_environment_changes_digest_only_when_what_it_holds_does(tmp_path: Path) -> None:
    def digest(requirements: str, name: str) -> str:
        item = _protein(tmp_path / name, requirements)
        manifest = workers.write([item], tmp_path / name / "bundle")
        built = manifest["plugins"][0]["environments"]["protein-tools"]
        return str(built["digest"])

    first = digest("# nothing\n", "a")
    assert digest("# nothing\n", "b") == first
    assert digest(f"numpy==2.1.0 --hash={HASH}\n", "c") != first


def test_names_with_spaces_or_quotes_are_quoted(tmp_path: Path) -> None:
    item = _protein(tmp_path, "# nothing\n")
    workers.write([item], tmp_path / "bundle", "/opt/my envs/it's")
    script = (tmp_path / "bundle" / workers.SCRIPT).read_text()
    assert "'/opt/my envs/it'\"'\"'s/acme__protein/protein-tools'" in script


@pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv")
def test_the_script_builds_the_environment(tmp_path: Path) -> None:
    item = _protein(tmp_path, "# nothing\n")
    envs = tmp_path / "envs"
    manifest = workers.write([item], tmp_path / "bundle", str(envs))
    # The Python version the plugin asks for, or whichever this machine has.
    script = tmp_path / "bundle" / workers.SCRIPT
    script.write_text(script.read_text().replace("--python 3.12", "--python python3"))
    subprocess.run(["sh", str(script)], check=True, capture_output=True)  # noqa: S603, S607
    prefix = Path(manifest["plugins"][0]["environments"]["protein-tools"]["prefix"])
    assert (prefix / "bin" / "python").exists()
    listed = json.loads((tmp_path / "bundle" / "plugins" / workers.WORKER_PLUGINS).read_text())
    assert listed == manifest


def _with_a_module(tmp_path: Path, digest: str, built: bytes | None) -> Path:
    """A plugin directory that asks for a compiled module, as a `git archive` export of a
    first-party plugin does: the digest is there, the module only if `built` is given."""
    directory = tmp_path / "protein"
    shutil.copytree(PLUGINS / "protein", directory)
    data = directory / "data"
    data.mkdir(exist_ok=True)
    (data / "tools.wasm.sha256").write_text(f"{digest}  tools.wasm\n")
    if built is not None:
        (data / "tools.wasm").write_bytes(built)
    return directory


def test_a_module_is_taken_from_iki_spec_and_bundled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No .wasm is tracked in git, so an exported plugin has the digest and not the bytes.
    They come from iki-spec, which the build puts them in, and go into the bundle."""
    bytes_of_it = b"\x00asm\x01\x00\x00\x00 a module"
    digest = hashlib.sha256(bytes_of_it).hexdigest()
    carried = tmp_path / "carried.wasm"
    carried.write_bytes(bytes_of_it)
    monkeypatch.setattr(modules, "module", lambda name: carried if name == "tools.wasm" else None)

    directory = _with_a_module(tmp_path, digest, None)
    workers.bundle(directory, tmp_path / "bundle.tar.gz")

    placed = directory / "data" / "tools.wasm"
    assert placed.read_bytes() == bytes_of_it
    with tarfile.open(tmp_path / "bundle.tar.gz") as tar:
        assert "data/tools.wasm" in tar.getnames()


def test_a_module_that_isnt_the_one_its_digest_names_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On the platform the digests are of -- Linux x86-64, which is what a worker runs."""
    monkeypatch.setattr(workers, "_digests_hold_here", lambda: True)
    directory = _with_a_module(tmp_path, "a" * 64, b"\x00asm\x01\x00\x00\x00 another module")
    with pytest.raises(ValueError, match=r"isn't the module its digest names"):
        workers.bundle(directory, tmp_path / "bundle.tar.gz")


def test_on_another_platform_a_module_is_still_required_but_not_compared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Rust WebAssembly build is reproducible on one platform and not across them, so
    the digest is of the Linux x86-64 build -- the only bytes that ever reach a worker.
    Building a deployment on another platform, which only a dev cluster runs, gets the
    module that was built there; it is still required to be there."""
    monkeypatch.setattr(workers, "_digests_hold_here", lambda: False)
    directory = _with_a_module(tmp_path, "a" * 64, b"\x00asm\x01\x00\x00\x00 this platform's")
    workers.bundle(directory, tmp_path / "bundle.tar.gz")
    with tarfile.open(tmp_path / "bundle.tar.gz") as tar:
        assert "data/tools.wasm" in tar.getnames()

    monkeypatch.setattr(modules, "module", lambda name: None)
    missing = _with_a_module(tmp_path / "none", "a" * 64, None)
    with pytest.raises(ValueError, match=r"isn't built and this iki-spec hasn't got it"):
        workers.bundle(missing, tmp_path / "none.tar.gz")


def test_a_module_nobody_has_is_refused_rather_than_left_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bundle without the module would deploy a plugin whose actions have nothing to
    call, and fail at the first run instead of at the build."""
    monkeypatch.setattr(modules, "module", lambda name: None)
    directory = _with_a_module(tmp_path, "b" * 64, None)
    with pytest.raises(ValueError, match=r"isn't built and this iki-spec hasn't got it"):
        workers.bundle(directory, tmp_path / "bundle.tar.gz")


def test_an_empty_digest_is_refused(tmp_path: Path) -> None:
    directory = _with_a_module(tmp_path, "", None)
    (directory / "data" / "tools.wasm.sha256").write_text("")
    with pytest.raises(ValueError, match=r"is empty; it holds a SHA-256"):
        workers.bundle(directory, tmp_path / "bundle.tar.gz")


def test_a_first_party_plugins_module_travels_from_iki_spec_into_its_bundle(
    tmp_path: Path,
) -> None:
    """The whole path, with no stand-ins: the plasmid plugin exported the way
    ``iki deploy build`` exports it, which carries the digest and not the module, bundled
    with the module iki-spec was built with (ADR 0200).

    The tracked digest is of the Linux x86-64 build, since that is what every image and
    deployment runs, and rustc's precompiled wasm32 standard library differs between host
    platforms, so the comparison is only asserted where it can hold.
    """
    root = Path(__file__).parents[2]
    name = "plasmid-tools.wasm"
    carried = modules.module(name)
    if carried is None:
        pytest.skip(f"iki-spec hasn't got {name}: build it, see scripts/")
    exported = tmp_path / "exported"
    exported.mkdir()
    archive = subprocess.run(  # noqa: S603 - git, on this checkout
        ["git", "-C", str(root), "archive", "--format=tar", "HEAD:plugins/plasmid"],  # noqa: S607
        capture_output=True,
        check=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(exported, filter="data")
    assert (exported / "data" / f"{name}.sha256").is_file()
    assert not (exported / "data" / name).exists()
    want = (exported / "data" / f"{name}.sha256").read_text().split()[0]

    workers.bundle(exported, tmp_path / "bundle.tar.gz")

    placed = exported / "data" / name
    assert placed.read_bytes() == carried.read_bytes()
    with tarfile.open(tmp_path / "bundle.tar.gz") as tar:
        assert f"data/{name}" in tar.getnames()
    # Where the digests hold -- Linux x86-64, what a worker runs -- the bytes are the
    # tracked ones; elsewhere this checkout's own build went in, uncompared.
    if workers._digests_hold_here():
        assert hashlib.sha256(placed.read_bytes()).hexdigest() == want
