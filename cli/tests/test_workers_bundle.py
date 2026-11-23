"""The workers' bundle on its own: what ``iki deploy build --workers`` writes."""

import hashlib
import json
import platform
import shutil
import subprocess
import tarfile
from importlib.resources import files
from pathlib import Path

import pytest

from iki_cli import workers
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
