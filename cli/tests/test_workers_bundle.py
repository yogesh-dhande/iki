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
from iki_spec import modules, plugin_runtime
from iki_spec.validate import load_plugin

PLUGINS = Path(__file__).parents[2] / "spec" / "tests" / "plugins"
HASH = "sha256:" + "0" * 64


def _protein(
    tmp_path: Path, requirements: str, *, runtime: bool = False, helm: bool = False
) -> workers.WorkerPlugin:
    directory = tmp_path / "protein"
    shutil.copytree(PLUGINS / "protein", directory)
    (directory / "requirements.txt").write_text(requirements)
    if runtime or helm:
        env = directory / "environments" / "protein-tools.yaml"
        declared = "{ helm: true }" if helm else "true"
        env.write_text(
            env.read_text().replace(
                "requirements: requirements.txt }",
                f"requirements: requirements.txt, iki_runtime: {declared} }}",
            )
        )
    loaded = load_plugin(directory, dependencies=[PLUGINS / "dna"])
    assert loaded.plugin is not None, loaded.problems
    return workers.WorkerPlugin(loaded.plugin, directory, None)


def test_iki_keeps_runtime_provenance_and_compiled_components(tmp_path: Path) -> None:
    products = Path(__file__).parents[2] / "plugins"
    predictor = tmp_path / "antibody_3d"
    workers.stage_runtime("iki/antibody_3d", products / "antibody_3d", predictor)
    weights = Path("images/structure-tools")
    for filename in ("WEIGHTS-NOTICE.txt", "SHA256SUMS"):
        weight_file = weights / filename
        assert (predictor / weight_file).read_bytes() == (
            products / "antibody_3d" / weight_file
        ).read_bytes()
    assert not (predictor / weights / "Dockerfile").exists()
    assert not (predictor / weights / "prepare-weights.sh").exists()
    # The antibody plugin itself carries no model image or weights.
    antibody = tmp_path / "antibody"
    workers.stage_runtime("iki/antibody", products / "antibody", antibody)
    assert not (antibody / weights).exists()

    oligo = tmp_path / "oligo"
    workers.stage_runtime("iki/oligo", products / "oligo", oligo)
    for path in (
        "data/README.md",
        "data/monomers.json",
        "requirements.txt",
        "components/duplex.js",
    ):
        assert (oligo / path).read_bytes() == (products / "oligo" / path).read_bytes()
    for path in ("web", "data/build_monomers.py", "requirements.in", "tests"):
        assert not (oligo / path).exists()


def test_no_distribution_carries_a_first_party_plugins_web_or_image_sources(
    tmp_path: Path,
) -> None:
    """Intended (ADR 0219): the server serves the built components/, not web/'s source, and
    a worker runs published images, never builds one. So the server's build, the archive
    self-hosted workers fetch and the worker image all leave web/ and images/' build inputs
    out, and the two worker distributions hold the same files."""
    source = tmp_path / "small_molecule"
    for relative, text in {
        "plugin.yaml": (
            "apiVersion: iki/v1\nkind: Plugin\nname: iki/small_molecule\nversion: 1.0.0\n"
        ),
        "components/depiction.yaml": (
            "apiVersion: iki/v1\nkind: Component\nname: depiction\nlabel: Structure\n"
            "description: The compound drawn from its SMILES.\n"
            "module: ./components/depiction.js\nelement: compound-depiction\n"
            "slots: [{ slot: entity.tab, type: iki/core.Complex }]\n"
        ),
        "components/depiction.js": (
            "customElements.define('compound-depiction', class extends HTMLElement {});\n"
        ),
        "web/src/depiction.js": "export const source = true;\n",
        "images/rdkit-tools/Dockerfile": "FROM scratch\n",
        "images/rdkit-tools/NOTICE.txt": "RDKit is BSD-3-Clause.\n",
        "scripts/descriptors.py": "print('descriptors')\n",
    }.items():
        (source / relative).parent.mkdir(parents=True, exist_ok=True)
        (source / relative).write_text(text)
    (tmp_path / "plugins.lock.yaml").write_text(
        "apiVersion: iki/v1\nkind: PluginLock\nplugins:\n"
        "  - { name: iki/small_molecule, local: ./small_molecule, state: active }\n"
    )
    server, image = tmp_path / "server", tmp_path / "image"
    command = ["deploy", "build", str(tmp_path), "--allow-local", "-o", str(server)]
    assert main([*command, "--workers", str(image)]) == 0

    archive = server / "iki_installed_plugins" / "bundles" / "iki__small_molecule-local.tar.gz"
    with tarfile.open(archive) as tar:
        archived = set(tar.getnames())
    staged = image / "plugins" / "iki__small_molecule"
    assert archived == {p.relative_to(staged).as_posix() for p in staged.rglob("*") if p.is_file()}
    assert archived == {
        "plugin.yaml",
        "components/depiction.yaml",
        "components/depiction.js",
        "images/rdkit-tools/NOTICE.txt",
        "scripts/descriptors.py",
    }
    package = server / "iki_plugin_iki__small_molecule"
    assert (package / "components" / "depiction.js").read_bytes() == (
        source / "components" / "depiction.js"
    ).read_bytes()
    shipped = [p.relative_to(server) for p in server.rglob("*")]
    assert not [p for p in shipped if p.name == "Dockerfile" or "web" in p.parts]


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


def test_an_environment_that_declares_the_runtime_installs_this_ikis_wheel_by_its_hash(
    tmp_path: Path,
) -> None:
    wheel = plugin_runtime.wheel()
    item = _protein(tmp_path, "# nothing\n", runtime=True)
    manifest = workers.write([item], tmp_path / "bundle", "/envs")
    bundle = tmp_path / "bundle"
    assert (bundle / workers.RUNTIME / wheel.name).read_bytes() == wheel.path.read_bytes()
    assert (bundle / workers.RUNTIME_REQUIREMENTS).read_text() == (
        f"./runtime/{wheel.name} --hash=sha256:{wheel.sha256}\n"
    )
    script = (bundle / workers.SCRIPT).read_text()
    assert (
        '(cd "$here" && uv pip install --quiet --python /envs/acme__protein/protein-tools '
        "--require-hashes --no-deps --no-index --requirement runtime/requirements.txt)"
    ) in script
    # The wheel's bytes are in the digest: another wheel, another build.
    with_runtime = manifest["plugins"][0]["environments"]["protein-tools"]["digest"]
    without = workers.write([_protein(tmp_path / "plain", "# nothing\n")], tmp_path / "plain" / "b")
    assert without["plugins"][0]["environments"]["protein-tools"]["digest"] != with_runtime
    assert not (tmp_path / "plain" / "b" / workers.RUNTIME).exists()
    definition = {
        "iki_runtime_sha256": wheel.sha256,
        "kind": "python",
        "python": "3.12",
        "requirements_sha256": hashlib.sha256(b"# nothing\n").hexdigest(),
    }
    canonical = json.dumps(definition, sort_keys=True, separators=(",", ":")).encode()
    assert with_runtime == "sha256:" + hashlib.sha256(canonical).hexdigest()


def test_a_bundle_carries_the_runtime_for_the_worker_to_build_with(tmp_path: Path) -> None:
    wheel = plugin_runtime.wheel()
    item = _protein(tmp_path, "# nothing\n", runtime=True)
    assert workers.wants_runtime(item.plugin)
    (entry,) = workers.environments(item.plugin, item.directory).values()
    assert entry["runtime"] == f".iki-runtime/{wheel.name}"
    out = tmp_path / "protein.tar.gz"
    workers.bundle("acme/protein", item.directory, out, runtime=True)
    with tarfile.open(out) as tar:
        member = tar.extractfile(entry["runtime"])
        assert member is not None
        assert member.read() == wheel.path.read_bytes()
    # A plugin can't put a file of its own where the wheel goes.
    (item.directory / ".iki-runtime").mkdir()
    (item.directory / ".iki-runtime" / "x.whl").write_text("not it")
    with pytest.raises(ValueError, match="carries the plugin runtime"):
        workers.bundle("acme/protein", item.directory, out, runtime=True)


#: Requirements that pin wasmtime, which the plugin runtime's HELM reader runs iki-wasm with.
WASMTIME = "wasmtime==51.0.0 --hash=sha256:" + "b" * 64 + "\n"


@pytest.fixture
def carried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> plugin_runtime.Module:
    """iki-wasm's module as iki-spec carries it, with the digest Iki pins for it: a module
    of its own, so what's checked doesn't depend on the platform this runs on."""
    path = tmp_path / "carried" / "iki_wasm.wasm"
    path.parent.mkdir()
    path.write_bytes(b"\x00asm\x01\x00\x00\x00 iki-wasm")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    module = plugin_runtime.Module(path, digest, digest)
    monkeypatch.setattr(plugin_runtime, "wasm", lambda: module)
    return module


def test_an_environment_with_the_helm_reader_gets_iki_wasm_by_its_digest(
    tmp_path: Path, carried: plugin_runtime.Module
) -> None:
    """ADR 0288: `iki_runtime: { helm: true }` puts iki-wasm's module beside the wheel, in
    the environment where `iki_runtime.helm` reads it, compiled there for its runs; and its
    SHA-256 is in the digest, beside the wheel's."""
    item = _protein(tmp_path, WASMTIME, helm=True)
    assert workers.wants_runtime(item.plugin)
    assert workers.wants_helm(item.plugin)
    manifest = workers.write([item], tmp_path / "bundle", "/envs")
    bundle = tmp_path / "bundle"
    assert (bundle / workers.RUNTIME_WASM).read_bytes() == carried.path.read_bytes()
    script = (bundle / workers.SCRIPT).read_text()
    prefix = "/envs/acme__protein/protein-tools"
    assert (
        f"mkdir -p {prefix}/share/iki-runtime\n"
        f'cp "$here"/runtime/iki_wasm.wasm {prefix}/share/iki-runtime/iki_wasm.wasm\n'
        f"{prefix}/bin/python -m iki_runtime.helm precompile "
        f"{prefix}/share/iki-runtime/iki_wasm.wasm >/dev/null\n"
    ) in script
    # After the runtime's wheel, which has the command that compiles it.
    assert script.index("--requirement runtime/requirements.txt") < script.index("precompile")
    definition = {
        "iki_runtime_sha256": plugin_runtime.wheel().sha256,
        "iki_wasm_sha256": carried.sha256,
        "kind": "python",
        "python": "3.12",
        "requirements_sha256": hashlib.sha256(WASMTIME.encode()).hexdigest(),
    }
    canonical = json.dumps(definition, sort_keys=True, separators=(",", ":")).encode()
    digest = manifest["plugins"][0]["environments"]["protein-tools"]["digest"]
    assert digest == "sha256:" + hashlib.sha256(canonical).hexdigest()
    # The runtime alone has neither the module nor its digest.
    plain = workers.write(
        [_protein(tmp_path / "plain", WASMTIME, runtime=True)], tmp_path / "plain" / "b"
    )
    assert plain["plugins"][0]["environments"]["protein-tools"]["digest"] != digest
    assert not (tmp_path / "plain" / "b" / workers.RUNTIME_WASM).exists()
    assert "precompile" not in (tmp_path / "plain" / "b" / workers.SCRIPT).read_text()


def test_a_bundle_carries_iki_wasm_for_the_worker_to_put_in_place(
    tmp_path: Path, carried: plugin_runtime.Module
) -> None:
    item = _protein(tmp_path, WASMTIME, helm=True)
    (entry,) = workers.environments(item.plugin, item.directory).values()
    assert entry["runtime"] == f".iki-runtime/{plugin_runtime.wheel().name}"
    assert entry["runtime_wasm"] == ".iki-runtime/iki_wasm.wasm"
    out = tmp_path / "protein.tar.gz"
    workers.bundle("acme/protein", item.directory, out, runtime=True, helm=True, pinned=True)
    with tarfile.open(out) as tar:
        member = tar.extractfile(entry["runtime_wasm"])
        assert member is not None
        assert member.read() == carried.path.read_bytes()


def test_iki_wasm_that_isnt_the_module_iki_pins_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, carried: plugin_runtime.Module
) -> None:
    """A pinned build, and any build on Linux x86-64, the platform the pin is of, refuses
    other bytes; a pinned build refuses a module it has no pin to check."""
    item = _protein(tmp_path, WASMTIME, helm=True)
    out = tmp_path / "protein.tar.gz"
    other = plugin_runtime.Module(carried.path, carried.sha256, "c" * 64)
    monkeypatch.setattr(plugin_runtime, "wasm", lambda: other)
    monkeypatch.setattr(workers, "_digests_hold_here", lambda: False)
    with pytest.raises(ValueError, match="isn't the module Iki pins"):
        workers.bundle("acme/protein", item.directory, out, helm=True, pinned=True)
    # A local plugin's build on another platform has that platform's module.
    workers.bundle("acme/protein", item.directory, out, helm=True)
    monkeypatch.setattr(workers, "_digests_hold_here", lambda: True)
    with pytest.raises(ValueError, match="isn't the module Iki pins"):
        workers.bundle("acme/protein", item.directory, out, helm=True)
    with pytest.raises(ValueError, match="isn't the module Iki pins"):
        workers.write([item], tmp_path / "bundle")
    unpinned = plugin_runtime.Module(carried.path, carried.sha256, None)
    monkeypatch.setattr(plugin_runtime, "wasm", lambda: unpinned)
    with pytest.raises(ValueError, match="not the digest Iki pins"):
        workers.bundle("acme/protein", item.directory, out, helm=True, pinned=True)


@pytest.mark.skipif(
    platform.system() != "Linux" or platform.machine().lower() not in {"x86_64", "amd64"},
    reason="Iki pins iki-wasm's Linux x86-64 build, which the release and CI build",
)
def test_iki_spec_carries_the_iki_wasm_iki_pins() -> None:
    module = plugin_runtime.wasm()
    assert module.pinned is not None
    assert module.sha256 == module.pinned


def test_names_with_spaces_or_quotes_are_quoted(tmp_path: Path) -> None:
    item = _protein(tmp_path, "# nothing\n")
    workers.write([item], tmp_path / "bundle", "/opt/my envs/it's")
    script = (tmp_path / "bundle" / workers.SCRIPT).read_text()
    assert "'/opt/my envs/it'\"'\"'s/acme__protein/protein-tools'" in script


def test_the_script_builds_the_environment(tmp_path: Path) -> None:
    assert shutil.which("uv"), "Install uv to build the worker environment"
    item = _protein(tmp_path, "# nothing\n", runtime=True)
    envs = tmp_path / "envs"
    manifest = workers.write([item], tmp_path / "bundle", str(envs))
    # The Python version the plugin asks for, or whichever this machine has.
    script = tmp_path / "bundle" / workers.SCRIPT
    script.write_text(script.read_text().replace("--python 3.12", "--python python3"))
    subprocess.run(["sh", str(script)], check=True, capture_output=True)  # noqa: S603, S607
    prefix = Path(manifest["plugins"][0]["environments"]["protein-tools"]["prefix"])
    assert (prefix / "bin" / "python").exists()
    # It declares the plugin runtime, which the build installed from the wheel beside it.
    found = subprocess.run(  # noqa: S603
        [
            str(prefix / "bin" / "python"),
            "-c",
            "import iki_runtime; print(iki_runtime.__version__)",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert found.stdout.strip() == plugin_runtime.wheel().version
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
    workers.bundle("acme/protein", directory, tmp_path / "bundle.tar.gz")

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
        workers.bundle("acme/protein", directory, tmp_path / "bundle.tar.gz")


def test_on_another_platform_a_module_is_still_required_but_not_compared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Local plugins may use host-built modules. Pinned plugins require canonical bytes."""
    monkeypatch.setattr(workers, "_digests_hold_here", lambda: False)
    directory = _with_a_module(tmp_path, "a" * 64, b"\x00asm\x01\x00\x00\x00 this platform's")
    with pytest.raises(ValueError, match="module its digest names"):
        workers.bundle("acme/protein", directory, tmp_path / "pinned.tar.gz", pinned=True)
    assert not (tmp_path / "pinned.tar.gz").exists()
    workers.bundle("acme/protein", directory, tmp_path / "bundle.tar.gz")
    with tarfile.open(tmp_path / "bundle.tar.gz") as tar:
        assert "data/tools.wasm" in tar.getnames()

    monkeypatch.setattr(modules, "module", lambda name: None)
    missing = _with_a_module(tmp_path / "none", "a" * 64, None)
    with pytest.raises(ValueError, match=r"isn't built and this iki-spec hasn't got it"):
        workers.bundle("acme/protein", missing, tmp_path / "none.tar.gz")


def test_a_module_nobody_has_is_refused_rather_than_left_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bundle without the module would deploy a plugin whose actions have nothing to
    call, and fail at the first run instead of at the build."""
    monkeypatch.setattr(modules, "module", lambda name: None)
    directory = _with_a_module(tmp_path, "b" * 64, None)
    with pytest.raises(ValueError, match=r"isn't built and this iki-spec hasn't got it"):
        workers.bundle("acme/protein", directory, tmp_path / "bundle.tar.gz")


def test_an_empty_digest_is_refused(tmp_path: Path) -> None:
    directory = _with_a_module(tmp_path, "", None)
    (directory / "data" / "tools.wasm.sha256").write_text("")
    with pytest.raises(ValueError, match=r"is empty; it holds a SHA-256"):
        workers.bundle("acme/protein", directory, tmp_path / "bundle.tar.gz")


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

    A locally packaged native build can differ from the canonical Linux x86-64
    release. A pinned deployment may only use bytes matching all the tracked digests.
    """
    root = Path(__file__).parents[2]
    carried = modules.module(name)
    assert carried is not None, f"Missing packaged {name}. Run make test-wasm."
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
    auxiliary = "oligo-measurement" if plugin == "dna" else "dna-annotation.wasm"
    aux_name = "oligo-measurement.wasm" if plugin == "dna" else auxiliary
    aux_pin = (exported / "data" / f"{auxiliary}.sha256").read_text().strip()
    native_matches = (
        hashlib.sha256(carried.read_bytes()).hexdigest() == want
        and hashlib.sha256(files("iki_spec").joinpath(aux_name).read_bytes()).hexdigest() == aux_pin
    )
    if workers._digests_hold_here():
        assert native_matches, "Rebuild the canonical release modules."
    if not native_matches:
        with pytest.raises(ValueError, match="digest"):
            workers.prepared_source(f"iki/{plugin}", exported, tmp_path / "staged", pinned=True)
        return
    staged = workers.prepared_source(f"iki/{plugin}", exported, tmp_path / "staged", pinned=True)
    workers.bundle(f"iki/{plugin}", staged, tmp_path / "bundle.tar.gz", pinned=True)

    placed = staged / "data" / name
    assert placed.read_bytes() == carried.read_bytes()
    with tarfile.open(tmp_path / "bundle.tar.gz") as tar:
        assert f"data/{name}" in tar.getnames()
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
    digest = workers.bundle("iki/dna", clean, archive)
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
    assert workers.bundle("iki/dna", stale, tmp_path / "rebuilt.tar.gz") == digest
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
    with pytest.raises(ValueError, match="pinned digest"):
        workers.prepared_source("iki/dna", checkout, tmp_path / "pinned", pinned=True)
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


@pytest.mark.parametrize(
    ("plugin", "name"), [("dna", "oligo-checks.wasm"), ("plasmid", "plasmid-tools.wasm")]
)
@pytest.mark.parametrize("matches", [True, False], ids=["matches-pin", "wrong-package-bytes"])
def test_deploy_resolves_ignored_modules_from_iki_spec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    plugin: str,
    name: str,
    matches: bool,
) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    built = b"\0asm\x01\0\0\0 fresh build"
    expected = hashlib.sha256(built).hexdigest()
    installed = tmp_path / "iki-spec-modules"
    installed.mkdir()
    carried = built if matches else b"\0asm\x01\0\0\0 wrong package bytes"
    (installed / name).write_bytes(carried)
    monkeypatch.setattr(modules, "BUILT", installed)

    source = tmp_path / plugin
    data = source / "data"
    data.mkdir(parents=True)
    (source / "plugin.yaml").write_text(
        f"apiVersion: iki/v1\nkind: Plugin\nname: iki/{plugin}\nversion: 1.0.0\n"
    )
    (source / ".gitignore").write_text("*.wasm\n")
    marker = data / f"{name}.sha256"
    marker.write_text(f"{expected}  {name}\n")
    stale = data / name
    stale.write_bytes(b"stale build in the checkout")
    stale.chmod(0o444)
    subprocess.run(["git", "init", "-q"], cwd=source, check=True)  # noqa: S607
    subprocess.run(["git", "add", "."], cwd=source, check=True)  # noqa: S607
    subprocess.run(
        [  # noqa: S607
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "Pin the plugin module",
        ],
        cwd=source,
        check=True,
    )
    subprocess.run(  # noqa: S603
        ["git", "check-ignore", "--quiet", "--", f"data/{name}"],  # noqa: S607
        cwd=source,
        check=True,
    )
    (tmp_path / "plugins.lock.yaml").write_text(
        "apiVersion: iki/v1\nkind: PluginLock\nplugins:\n"
        f"  - {{ name: iki/{plugin}, local: ./{plugin}, state: active }}\n"
    )

    server, image = tmp_path / "server", tmp_path / "image"
    result = main(
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
    assert stale.read_bytes() == b"stale build in the checkout"
    assert stale.stat().st_mode & 0o777 == 0o444
    assert marker.read_text() == f"{expected}  {name}\n"
    if matches:
        assert result == 0, capsys.readouterr().err
        archive = server / "iki_installed_plugins" / "bundles" / f"iki__{plugin}-local.tar.gz"
        with tarfile.open(archive) as packed:
            stream = packed.extractfile(f"data/{name}")
            assert stream is not None and stream.read() == built
        assert (image / "plugins" / f"iki__{plugin}" / "data" / name).read_bytes() == built
    else:
        assert result == 1
        diagnostic = capsys.readouterr().err
        assert name in diagnostic and expected in diagnostic
        assert hashlib.sha256(carried).hexdigest() in diagnostic
        assert not server.exists() and not image.exists()


def test_an_older_plugin_excludes_an_unpinned_ignored_module(tmp_path: Path) -> None:
    directory = tmp_path / "checkout"
    relative = Path("data/oligo-checks.wasm")
    module = directory / relative
    module.parent.mkdir(parents=True)
    module.write_bytes(b"left behind by a newer checkout")
    (directory / ".gitignore").write_text("*.wasm\n")
    subprocess.run(["git", "init", "-q", directory], check=True)  # noqa: S603, S607

    cleaned = workers.prepared_source("iki/dna", directory, tmp_path / "cleaned")
    assert not (cleaned / relative).exists()
    assert module.read_bytes() == b"left behind by a newer checkout"

    subprocess.run(  # noqa: S603
        ["git", "-C", directory, "add", "-f", "--", str(relative)],  # noqa: S607
        check=True,
    )
    assert workers.prepared_source("iki/dna", directory, tmp_path / "tracked") == directory


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
