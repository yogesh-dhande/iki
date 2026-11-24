"""The workers' bundle on its own: what ``iki deploy build --workers`` writes."""

import hashlib
import io
import json
import platform
import shutil
import subprocess
import tarfile
from importlib.resources import files
from pathlib import Path

import pytest

from iki_cli import workers
from iki_cli.main import main
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


@pytest.mark.parametrize(
    ("plugin", "name"),
    [("plasmid", "plasmid-tools.wasm"), ("dna", "oligo-checks.wasm")],
)
def test_a_first_party_plugins_module_travels_from_iki_spec_into_its_bundle(
    tmp_path: Path, plugin: str, name: str
) -> None:
    """The whole path, with no stand-ins: a first-party plugin exported the way
    ``iki deploy build`` exports it, which carries the digest and not the module, bundled
    with the module iki-spec was built with (ADR 0200, ADR 0201).

    The tracked digest is of the Linux x86-64 build, since that is what every image and
    deployment runs, and rustc's precompiled wasm32 standard library differs between host
    platforms, so the comparison is only asserted where it can hold.
    """
    root = Path(__file__).parents[2]
    carried = modules.module(name)
    if carried is None:
        pytest.skip(f"iki-spec hasn't got {name}: build it, see scripts/")
    exported = tmp_path / "exported"
    exported.mkdir()
    archive = subprocess.run(  # noqa: S603 - git, on this checkout
        ["git", "-C", str(root), "archive", "--format=tar", f"HEAD:plugins/{plugin}"],  # noqa: S607
        capture_output=True,
        check=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(exported, filter="data")
    assert (exported / "data" / f"{name}.sha256").is_file()
    assert not (exported / "data" / name).exists()
    want = (exported / "data" / f"{name}.sha256").read_text().split()[0]

    # As `iki deploy build` does: the plugin's other first-party resources -- the plasmid
    # plugin's annotation module, the DNA plugin's measurement -- are staged first, and
    # then the bundle places this one.
    staged = workers.prepared_source(f"iki/{plugin}", exported, tmp_path / "staged", pinned=True)
    workers.bundle(staged, tmp_path / "bundle.tar.gz")

    placed = staged / "data" / name
    assert placed.read_bytes() == carried.read_bytes()
    with tarfile.open(tmp_path / "bundle.tar.gz") as tar:
        assert f"data/{name}" in tar.getnames()
    # Where the digests hold -- Linux x86-64, what a worker runs -- the bytes are the
    # tracked ones; elsewhere this checkout's own build went in, uncompared.
    if workers._digests_hold_here():
        assert hashlib.sha256(placed.read_bytes()).hexdigest() == want


def test_only_the_pinned_dna_plugin_receives_the_installed_measurement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    directory = tmp_path / "checkout"
    relative = Path("data/oligo-measurement.wasm")
    generated = directory / relative
    marker = generated.with_suffix(".sha256")
    marker.parent.mkdir(parents=True)
    (directory / "plugin.yaml").write_text(
        "apiVersion: iki/v1\nkind: Plugin\nname: iki/dna\nversion: 1.0.0\n"
        "title: DNA\nlicense: Apache-2.0\niki: '>=0.1'\n"
    )
    module = files("iki_spec").joinpath(relative.name).read_bytes()
    notice_name = "rust-standard-library.html"
    notice = files("iki_spec").joinpath(notice_name).read_bytes()
    assert b"Rust" in notice
    assert module.startswith(b"\0asm")
    assert workers.prepared_source("iki/dna", directory, tmp_path / "older") == directory
    marker.write_text(hashlib.sha256(module).hexdigest() + "\n")
    assert workers.prepared_source("other/dna", directory, tmp_path / "other") == directory
    assert not generated.exists()

    clean = workers.prepared_source("iki/dna", directory, tmp_path / "prepared")
    archive = tmp_path / "served.tar.gz"
    digest = workers.bundle(clean, archive)
    with tarfile.open(archive, "r:gz") as tar:
        payload = tar.extractfile(relative.as_posix())
        assert payload and payload.read() == module
        copyright_file = tar.extractfile(f"data/{notice_name}")
        assert copyright_file and copyright_file.read() == notice
    plugin = load_plugin(clean)
    assert plugin.plugin is not None, plugin.problems
    image = tmp_path / "image"
    workers.write([workers.WorkerPlugin(plugin.plugin, clean, None)], image)
    preinstalled = image / "plugins" / "iki__dna" / relative
    assert preinstalled.read_bytes() == module
    assert preinstalled.with_name(notice_name).read_bytes() == notice
    assert not generated.exists()

    generated.write_bytes(b"stale checkout artifact")
    generated.chmod(0o444)
    stale_notice = generated.with_name(notice_name)
    stale_notice.write_bytes(b"stale notice")
    stale_notice.chmod(0o444)
    stale = workers.prepared_source("iki/dna", directory, tmp_path / "stale")
    assert workers.bundle(stale, tmp_path / "rebuilt.tar.gz") == digest
    assert generated.read_bytes() == b"stale checkout artifact"
    assert stale_notice.read_bytes() == b"stale notice"
    assert generated.stat().st_mode & 0o777 == stale_notice.stat().st_mode & 0o777 == 0o444
    assert (stale / relative).stat().st_mode & 0o777 == 0o644

    marker.write_text("0" * 64)
    with pytest.raises(ValueError, match="pinned digest"):
        workers.prepared_source("iki/dna", directory, tmp_path / "mismatch")
    marker.unlink()
    historical = workers.prepared_source("iki/dna", directory, tmp_path / "pinned", pinned=True)
    assert (
        historical == directory and (historical / relative).read_bytes() == generated.read_bytes()
    )
    # A versioned module in a local checkout remains part of its plugin. An ignored
    # leftover after switching back to an older checkout is excluded.
    subprocess.run(["git", "init", "-q", directory], check=True)  # noqa: S603, S607
    subprocess.run(["git", "-C", directory, "add", str(relative)], check=True)  # noqa: S603, S607
    (directory / ".gitignore").write_text("*.wasm\n")
    assert workers.prepared_source("iki/dna", directory, tmp_path / "tracked") == directory
    subprocess.run(  # noqa: S603
        ["git", "-C", directory, "rm", "-q", "--cached", str(relative)],  # noqa: S607
        check=True,
    )
    old = workers.prepared_source("iki/dna", directory, tmp_path / "pre-measurement")
    assert not (old / relative).exists()


@pytest.mark.parametrize(
    ("system", "machine", "compares"),
    [
        ("Linux", "x86_64", True),
        ("Linux", "amd64", True),
        ("Darwin", "arm64", False),
        ("Linux", "aarch64", False),
    ],
)
def test_measurement_uses_a_platform_specific_digest(
    system: str,
    machine: str,
    compares: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(platform, "system", lambda: system)
    monkeypatch.setattr(platform, "machine", lambda: machine)
    checkout = tmp_path / "checkout"
    data = checkout / "data"
    data.mkdir(parents=True)
    marker = data / "oligo-measurement.sha256"
    package = tmp_path / "installed"
    package.mkdir()
    monkeypatch.setattr(workers, "files", lambda _name: package)
    module = package / "oligo-measurement.wasm"
    notice = package / "rust-standard-library.html"
    notice.write_text("Rust standard library notices")
    marker.write_text("0" * 64)
    with pytest.raises(ValueError, match="lacks oligo-measurement"):
        workers.prepared_source("iki/dna", checkout, tmp_path / "missing")
    module.write_bytes(b"")
    with pytest.raises(ValueError, match="is empty"):
        workers.prepared_source("iki/dna", checkout, tmp_path / "empty")
    module.write_bytes(b"locally built bytes")
    notice.write_bytes(b"")
    with pytest.raises(ValueError, match="is empty"):
        workers.prepared_source("iki/dna", checkout, tmp_path / "empty-notice")
    notice.write_text("Rust standard library notices")
    marker.write_text("missing")
    with pytest.raises(ValueError, match="SHA-256"):
        workers.prepared_source("iki/dna", checkout, tmp_path / "invalid-marker")
    marker.write_text("0" * 64)
    if compares:
        with pytest.raises(ValueError, match="pinned digest"):
            workers.prepared_source("iki/dna", checkout, tmp_path / "different")
    else:
        staged = workers.prepared_source("iki/dna", checkout, tmp_path / "different")
        assert (staged / "data" / module.name).read_bytes() == module.read_bytes()
        assert "Linux x86-64" in capsys.readouterr().err


def test_deploy_puts_verified_annotation_assets_in_both_worker_distributions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = b"\0asm\x01\0\0\0"
    notice = b"<html>Rust standard library</html>"
    installed = tmp_path / "iki-spec"
    installed.mkdir()
    (installed / "dna-annotation.wasm").write_bytes(module)
    (installed / "rust-standard-library.html").write_bytes(notice)
    monkeypatch.setattr(workers, "files", lambda _package: installed)

    source = tmp_path / "plasmid"
    data = source / "data"
    data.mkdir(parents=True)
    (source / "plugin.yaml").write_text(
        "apiVersion: iki/v1\nkind: Plugin\nname: iki/plasmid\nversion: 1.0.0\n"
    )
    marker = data / "dna-annotation.wasm.sha256"
    marker.write_text(hashlib.sha256(module).hexdigest() + "\n")
    stale = data / "dna-annotation.wasm"
    stale.write_bytes(b"stale build product")
    stale.chmod(0o444)
    (tmp_path / "plugins.lock.yaml").write_text(
        "apiVersion: iki/v1\nkind: PluginLock\nplugins:\n"
        "  - { name: iki/plasmid, local: ./plasmid, state: active }\n"
    )

    server, image = tmp_path / "server", tmp_path / "image"
    assert (
        main(
            [
                "deploy",
                "build",
                str(tmp_path),
                "--allow-local",
                "-o",
                str(server),
                "--workers",
                str(image),
            ]
        )
        == 0
    )
    registry = server / "iki_installed_plugins"
    archive = registry / "bundles" / "iki__plasmid-local.tar.gz"
    record = json.loads((registry / "lock.json").read_text())["plugins"][0]["bundle"]
    archive_bytes = archive.read_bytes()
    assert record == {
        "sha256": hashlib.sha256(archive_bytes).hexdigest(),
        "size": len(archive_bytes),
    }
    with tarfile.open(archive) as packed:
        for name, contents in (
            ("dna-annotation.wasm", module),
            ("rust-standard-library.html", notice),
        ):
            member = packed.getmember(f"data/{name}")
            stream = packed.extractfile(member)
            assert stream is not None and stream.read() == contents and member.mode == 0o644
            assert (image / "plugins" / "iki__plasmid" / "data" / name).read_bytes() == contents
    assert stale.read_bytes() == b"stale build product"
    assert not (data / "rust-standard-library.html").exists()


@pytest.mark.parametrize("marker_name", ["dna-annotation.wasm.sha256", "dna-annotation.sha256"])
def test_preparing_an_annotation_requires_a_matching_digest_and_regular_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, marker_name: str
) -> None:
    source = tmp_path / "source"
    data = source / "data"
    data.mkdir(parents=True)
    installed = tmp_path / "iki-spec"
    installed.mkdir()
    (installed / "dna-annotation.wasm").write_bytes(b"\0asm")
    (installed / "rust-standard-library.html").write_text("Rust standard library")
    monkeypatch.setattr(workers, "files", lambda _package: installed)
    output = tmp_path / "prepared"
    assert workers.prepared_source("iki/plasmid", source, output) == source

    marker = data / marker_name
    marker.write_text("0" * 64)
    assert workers.prepared_source("acme/plasmid", source, output) == source
    actual = hashlib.sha256(b"\0asm").hexdigest()
    with pytest.raises(ValueError, match=rf"pinned digest.*Expected {'0' * 64}, actual {actual}"):
        workers.prepared_source("iki/plasmid", source, output)
    marker.unlink()
    marker.symlink_to(tmp_path / "missing")
    with pytest.raises(ValueError, match="regular plugin files"):
        workers.prepared_source("iki/plasmid", source, output)
    assert not output.exists()
