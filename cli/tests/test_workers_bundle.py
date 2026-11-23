"""The workers' bundle on its own: what ``iki deploy build --workers`` writes."""

import hashlib
import json
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from iki_cli import workers
from iki_cli.main import main
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
