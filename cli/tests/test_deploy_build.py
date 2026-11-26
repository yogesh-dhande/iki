import hashlib
import json
import platform
import shutil
import subprocess
import tarfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from iki_cli import fetch
from iki_cli.main import main
from iki_spec import modules, naming

ROOT = Path(__file__).parents[2]
PLUGINS = ROOT / "spec" / "tests" / "plugins"
URL = "https://git.example/acme/plugins.git"

# These files travel with a lab's source checkout. Its runtime has no use for them.
AUTHOR_INPUTS = (
    ".github/workflows/check.yml",
    "examples/sample.txt",
    "tests/data/original.csv",
    "tests/data/fixture.wasm.sha256",
)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603 - a test's own git commands
        ["git", "-C", str(repo), *args],  # noqa: S607 - git from the PATH, as Iki runs it
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture(scope="module")
def upstream(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Path, str]]:
    """A plugin repository, with dna and protein and their migrations, and its commit."""
    repo = tmp_path_factory.mktemp("upstream")
    git(repo, "init", "--quiet", "--initial-branch=main")
    for name in ("dna", "protein"):
        shutil.copytree(PLUGINS / name, repo / "plugins" / name)
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("NO_COLOR", "1")
        assert main(["plugin", "makemigrations", str(repo / "plugins/dna"), "--no-input"]) == 0
        protein = ["plugin", "makemigrations", str(repo / "plugins/protein")]
        assert main([*protein, "--with", str(repo / "plugins/dna"), "--no-input"]) == 0
    protein_dir = repo / "plugins/protein"
    for relative in (
        *AUTHOR_INPUTS,
        "data/dictionary.json",
        "data/NOTICE",
        "lab-resources/examples/standards.csv",
        "web/chromatogram.js",
        "images/blank.png",
        "setup/install.sh",
    ):
        path = protein_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative)
    (protein_dir / "setup/install.sh").chmod(0o755)
    git(repo, "add", ".")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.org", "commit", "-qm", "plugins")
    yield repo, git(repo, "rev-parse", "HEAD")


@pytest.fixture(autouse=True)
def _environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, upstream: tuple[Path, str]
) -> None:
    """Git sends URL to the upstream repository on disk, as a credential helper might."""
    monkeypatch.setenv("NO_COLOR", "1")
    config = tmp_path / "gitconfig"
    config.write_text(f'[url "file://{upstream[0]}"]\n\tinsteadOf = {URL}\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


@pytest.fixture
def allow_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lets git read the repository on disk, as it would read one over https."""
    monkeypatch.setattr(fetch, "PROTOCOLS", ("https", "file"))


def deployment(tmp_path: Path, sha: str, *, settings: str | None = "ncbi_email: a@b.org\n") -> Path:
    directory = tmp_path / "deployments" / "prod"
    (directory / "settings").mkdir(parents=True)
    dna = (
        f"  - name: acme/dna\n    git: {URL}\n    path: plugins/dna\n    sha: {sha}\n"
        "    state: active\n"
    )
    if settings is not None:
        (directory / "settings" / "dna.yaml").write_text(settings)
        dna += "    settings: settings/dna.yaml\n"
    protein = f"  - name: acme/protein\n    git: {URL}\n    path: plugins/protein\n    sha: {sha}\n"
    (directory / "plugins.lock.yaml").write_text(
        "apiVersion: iki/v1\nkind: PluginLock\nplugins:\n" + dna + protein + "    state: active\n"
    )
    return directory


def tree_hash(directory: Path) -> str:
    digest = hashlib.sha256()
    for file in sorted(p for p in directory.rglob("*") if p.is_file()):
        digest.update(file.relative_to(directory).as_posix().encode() + b"\0" + file.read_bytes())
    return digest.hexdigest()


@pytest.mark.usefixtures("allow_file")
def test_a_pinned_build_checks_the_historical_modules_digest_on_arm(
    tmp_path: Path,
    upstream: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    native = tmp_path / "tool.wasm"
    native.write_bytes(b"current packaged bytes")
    monkeypatch.setattr(modules, "module", lambda name: native if name == native.name else None)
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(platform, "machine", lambda: "aarch64")
    repo = tmp_path / "release"
    git(tmp_path, "clone", "--quiet", str(upstream[0]), str(repo))
    git(repo, "config", "user.name", "Release test")
    git(repo, "config", "user.email", "release@example.org")
    marker = repo / "plugins/dna/tool.wasm.sha256"
    previous = hashlib.sha256(b"previous packaged bytes").hexdigest()
    marker.write_text(previous)
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "Pin the first module")
    pin = git(repo, "rev-parse", "HEAD")
    latest = hashlib.sha256(native.read_bytes()).hexdigest()
    marker.write_text(latest)
    git(repo, "commit", "-qam", "Pin the current module")
    (tmp_path / "gitconfig").write_text(f'[url "file://{repo}"]\n\tinsteadOf = {URL}\n')
    server, workers = tmp_path / "server", tmp_path / "workers"
    deploy = deployment(tmp_path, pin)
    command = ["deploy", "build", str(deploy), "-o", str(server), "--workers", str(workers)]
    assert main(command) == 1
    error = capsys.readouterr().err
    assert "tool.wasm" in error and previous in error and latest in error
    assert not server.exists() and not workers.exists()
    lock = deploy / "plugins.lock.yaml"
    lock.write_text(lock.read_text().replace(pin, git(repo, "rev-parse", "HEAD")))
    assert main(command) == 0, capsys.readouterr().err
    assert (workers / "plugins/acme__dna/tool.wasm").read_bytes() == native.read_bytes()


@pytest.mark.usefixtures("allow_file")
def test_builds_the_pinned_commits(
    tmp_path: Path, upstream: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    _, sha = upstream
    out = tmp_path / "plugins"
    assert main(["deploy", "build", str(deployment(tmp_path, sha)), "-o", str(out)]) == 0
    captured = capsys.readouterr()
    printed = captured.out
    assert f"Fetched acme/dna at {sha[:12]} from {URL}" in printed
    assert printed.endswith(f"Built {out}: acme/dna 1.2.0, acme/protein 3.0.0.\n")
    assert sorted(p.name for p in out.iterdir()) == [
        "compute.json",
        "iki_installed_plugins",
        "iki_plugin_acme__dna",
        "iki_plugin_acme__protein",
    ]
    # Without a compute.yaml, the deployment runs only self-hosted actions, and says so.
    assert (
        "warning: `acme/protein.protein-tools` has no template for the backend tier" in captured.err
    )
    assert json.loads((out / "compute.json").read_text()) == {
        "apiVersion": "iki/v1",
        "bindings": {},
        "prices": {},
        "templates": {},
    }
    assert json.loads((out / "iki_plugin_acme__dna" / "settings.json").read_text()) == {
        "max_sequence_length": 10_000_000,
        "ncbi_email": "a@b.org",
    }
    assert sorted(p.name for p in (out / "iki_plugin_acme__protein" / "migrations").iterdir()) == [
        "0001_initial.py",
        "__init__.py",
    ]
    installed = (out / "iki_installed_plugins" / "__init__.py").read_text()
    assert "IKI_VERSION = '0.1.0.dev0'" in installed
    assert "LOCAL = False" in installed
    assert "MIRRORS = []" in installed
    record = json.loads((out / "iki_installed_plugins" / "lock.json").read_text())
    bundles = out / "iki_installed_plugins" / "bundles"
    packed = (bundles / f"acme__dna-{sha}.tar.gz").read_bytes()
    assert record["plugins"][0] == {
        "name": "acme/dna",
        "git": URL,
        "path": "plugins/dna",
        "sha": sha,
        "state": "active",
        "version": "1.2.0",
        "fetched_from": URL,
        "settings_sha256": hashlib.sha256(b"ncbi_email: a@b.org\n").hexdigest(),
        "bundle": {"sha256": hashlib.sha256(packed).hexdigest(), "size": len(packed)},
        "environments": {},
    }
    assert sorted(p.name for p in bundles.iterdir()) == [
        f"acme__dna-{sha}.tar.gz",
        f"acme__protein-{sha}.tar.gz",
    ]
    protein = record["plugins"][1]["environments"]["protein-tools"]
    assert protein["kind"] == "python" and protein["python"] == "3.12"
    assert protein["digest"].startswith("sha256:")
    assert protein["requirements"] == "requirements.txt"
    assert protein["packages"] is False  # it names none
    # Each bundle is the plugin's code at its commit: regular files, no times or owners.
    with tarfile.open(bundles / f"acme__protein-{sha}.tar.gz") as tar:
        members = tar.getmembers()
        names = [m.name for m in members]
        assert "plugin.yaml" in names and "scripts/relax.py" in names
        assert all(m.isfile() and m.mtime == 0 and m.uid == 0 for m in members)
        relax = tar.extractfile("scripts/relax.py")
        assert relax is not None
        assert relax.read() == (upstream[0] / "plugins/protein/scripts/relax.py").read_bytes()

    # The same lockfile builds the same bytes, from the cache.
    first = tree_hash(out)
    (tmp_path / "gitconfig").write_text("")
    assert main(["deploy", "build", str(tmp_path / "deployments/prod"), "-o", str(out)]) == 0
    assert tree_hash(out) == first


@pytest.mark.usefixtures("allow_file")
def test_builds_what_workers_need_from_the_same_commits(
    tmp_path: Path, upstream: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    repo, sha = upstream
    out, bundle = tmp_path / "plugins", tmp_path / "worker"
    command = ["deploy", "build", str(deployment(tmp_path, sha)), "-o", str(out)]
    assert main([*command, "--workers", str(bundle)]) == 0
    assert f"Wrote what workers need to {bundle}." in capsys.readouterr().out
    assert sorted(p.name for p in bundle.iterdir()) == ["build-environments.sh", "plugins"]
    # The image and a worker downloading the server's archive get identical runtime files.
    source = repo / "plugins/protein"
    expected = set(git(repo, "ls-tree", "-r", "--name-only", f"{sha}:plugins/protein").splitlines())
    assert set(AUTHOR_INPUTS) <= expected
    expected -= set(AUTHOR_INPUTS)
    staged = bundle / "plugins/acme__protein"
    assert {
        path.relative_to(staged).as_posix() for path in staged.rglob("*") if path.is_file()
    } == expected
    with tarfile.open(out / "iki_installed_plugins/bundles" / f"acme__protein-{sha}.tar.gz") as tar:
        assert set(tar.getnames()) == expected
        for name in sorted(expected):
            member = tar.extractfile(name)
            assert member is not None
            assert member.read() == (source / name).read_bytes() == (staged / name).read_bytes()
        assert tar.getmember("setup/install.sh").mode == 0o755
        assert (staged / "setup/install.sh").stat().st_mode & 0o100
    manifest = json.loads((bundle / "plugins" / "plugins.json").read_text())
    requirements = (repo / "plugins/protein/requirements.txt").read_bytes()
    definition = {
        "kind": "python",
        "python": "3.12",
        "requirements_sha256": hashlib.sha256(requirements).hexdigest(),
    }
    canonical = json.dumps(definition, sort_keys=True, separators=(",", ":")).encode()
    assert manifest == {
        "apiVersion": "iki/v1",
        "kind": "WorkerPlugins",
        "plugins": [
            {"name": "acme/dna", "version": "1.2.0", "sha": sha, "environments": {}},
            {
                "name": "acme/protein",
                "version": "3.0.0",
                "sha": sha,
                "environments": {
                    "protein-tools": {
                        "digest": "sha256:" + hashlib.sha256(canonical).hexdigest(),
                        "kind": "python",
                        "prefix": "/opt/iki/envs/acme__protein/protein-tools",
                    }
                },
            },
        ],
    }
    built = (bundle / "build-environments.sh").read_text()
    assert "uv venv --quiet --python 3.12 /opt/iki/envs/acme__protein/protein-tools" in built
    # Its requirements name no packages, so there's nothing to install.
    assert "uv pip install" not in built

    # It replaces a previous bundle, but nothing else.
    first = tree_hash(bundle)
    assert main([*command, "--workers", str(bundle)]) == 0
    assert tree_hash(bundle) == first
    (bundle / "notes.txt").write_text("mine")
    assert main([*command, "--workers", str(bundle)]) == 1
    assert "holds files a build didn't write (notes.txt)" in capsys.readouterr().err


def test_deploy_validates_the_runtime_tree_before_emitting_artifacts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = tmp_path / "lab"
    (directory / "tests").mkdir(parents=True)
    (directory / "environments").mkdir()
    (directory / "plugin.yaml").write_text(
        "apiVersion: iki/v1\nkind: Plugin\nname: acme/assay\nversion: 1.0.0\n"
    )
    (directory / "tests/requirements.txt").write_text("")
    manifest = directory / "environments/assay.yaml"
    manifest.write_text(
        (PLUGINS / "protein/environments/protein-tools.yaml")
        .read_text()
        .replace("requirements.txt", "tests/requirements.txt")
    )
    (tmp_path / "plugins.lock.yaml").write_text(
        "apiVersion: iki/v1\nkind: PluginLock\nplugins:\n"
        "  - {name: acme/assay, local: lab, state: active}\n"
    )
    server, worker = tmp_path / "server", tmp_path / "worker"
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
                str(worker),
            ]
        )
        == 1
    )
    assert "tests/requirements.txt" in capsys.readouterr().err
    assert not server.exists() and not worker.exists()


def test_git_cant_be_sent_to_other_transports(
    tmp_path: Path, upstream: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    # Only https and ssh: here a URL rewrite would make git read a file:// repository.
    code = main(["deploy", "build", str(deployment(tmp_path, upstream[1])), "-o", str(tmp_path)])
    assert code == 1
    err = capsys.readouterr().err
    assert f"error: acme/dna: couldn't fetch commit {upstream[1][:12]} from {URL}" in err
    assert "transport 'file' not allowed" in err


@pytest.mark.usefixtures("allow_file")
def test_commits_that_arent_there(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(
        [
            "deploy",
            "build",
            str(deployment(tmp_path, '"' + "0" * 40 + '"')),
            "-o",
            str(tmp_path / "o"),
        ]
    )
    assert code == 1
    assert "error: acme/dna: couldn't fetch commit 000000000000" in capsys.readouterr().err


COMPUTE = """\
apiVersion: iki/v1
kind: ComputeTemplate
name: backend-small
provisioner: backend_pool
capacity: { cpu: 1, memory: 2Gi }
---
apiVersion: iki/v1
kind: EnvironmentBinding
environment: acme/protein.protein-tools
templates: [{ template: backend-small }]
---
apiVersion: iki/v1
kind: ApiPrice
api: acme/protein.fold
per_call: 0.02
currency: USD
"""


@pytest.mark.usefixtures("allow_file")
def test_compute_is_checked_against_the_plugins(
    tmp_path: Path, upstream: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    directory = deployment(tmp_path, upstream[1])
    out = tmp_path / "o"
    (directory / "compute.yaml").write_text(COMPUTE)
    assert main(["deploy", "build", str(directory), "-o", str(out)]) == 0
    assert capsys.readouterr().err == ""
    compute = json.loads((out / "compute.json").read_text())
    assert compute["bindings"]["acme/protein.protein-tools"]["templates"] == [["backend-small", 0]]
    assert compute["templates"]["backend-small"]["capacity"]["memory"] == 2 * 1024**3

    (directory / "compute.yaml").write_text(COMPUTE.replace("memory: 2Gi", "memory: 256Mi"))
    assert main(["deploy", "build", str(directory), "-o", str(out)]) == 1
    err = capsys.readouterr().err
    assert (
        "/compute.yaml:10:25: error: `backend-small` is smaller than `acme/protein.protein-tools`"
        in err
    )
    assert "512Mi of memory (it has 256Mi)" in err


@pytest.mark.usefixtures("allow_file")
def test_settings_are_checked(
    tmp_path: Path, upstream: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    directory = deployment(tmp_path, upstream[1], settings="ncbi_email: nope\nmax_length: 3\n")
    assert main(["deploy", "build", str(directory), "-o", str(tmp_path / "o")]) == 1
    err = capsys.readouterr().err
    assert "settings/dna.yaml:1:13: error: `ncbi_email`" in err
    assert "The acme/dna plugin has no setting `max_length`." in err
    assert not (tmp_path / "o").exists()


@pytest.mark.usefixtures("allow_file")
def test_required_settings_need_a_file(
    tmp_path: Path, upstream: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    directory = deployment(tmp_path, upstream[1], settings=None)
    assert main(["deploy", "build", str(directory), "-o", str(tmp_path / "o")]) == 1
    err = capsys.readouterr().err
    assert "The acme/dna plugin needs `ncbi_email` to be set." in err
    assert "with `settings:` in the lockfile" in err


@pytest.mark.usefixtures("allow_file")
def test_dependencies_must_be_in_the_lockfile(
    tmp_path: Path, upstream: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    directory = deployment(tmp_path, upstream[1])
    lockfile = directory / "plugins.lock.yaml"
    text = lockfile.read_text()
    lockfile.write_text(text[: text.index("  - name: acme/protein")].replace("dna", "protein", 1))
    lockfile.write_text(lockfile.read_text().replace("plugins/dna", "plugins/protein"))
    lockfile.write_text(lockfile.read_text().replace("    settings: settings/dna.yaml\n", ""))
    assert main(["deploy", "build", str(directory), "-o", str(tmp_path / "o")]) == 1
    assert capsys.readouterr().err == (
        "error: acme/protein needs the acme/dna plugin: add it to the lockfile.\n"
    )


@pytest.mark.usefixtures("allow_file")
def test_what_depends_on_a_detached_plugin_is_detached_too(
    tmp_path: Path, upstream: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    directory = deployment(tmp_path, upstream[1])
    lockfile = directory / "plugins.lock.yaml"
    text = lockfile.read_text()
    lockfile.write_text(text.replace("state: active", "state: detached", 1))
    assert main(["deploy", "build", str(directory), "-o", str(tmp_path / "o")]) == 1
    assert capsys.readouterr().err == (
        "error: acme/protein depends on acme/dna, which the lockfile detaches. "
        "Detach acme/protein too, or keep acme/dna active.\n"
    )
    lockfile.write_text(text.replace("state: active", "state: detached"))
    assert main(["deploy", "build", str(directory), "-o", str(tmp_path / "o")]) == 0
    record = json.loads((tmp_path / "o" / "iki_installed_plugins" / "lock.json").read_text())
    assert [p["state"] for p in record["plugins"]] == ["detached", "detached"]


@pytest.mark.usefixtures("allow_file")
def test_a_purged_plugin_isnt_built_but_is_recorded(
    tmp_path: Path, upstream: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    directory = deployment(tmp_path, upstream[1])
    lockfile = directory / "plugins.lock.yaml"
    text = lockfile.read_text()
    dna = text[text.index("  - name: acme/dna") : text.index("  - name: acme/protein")]
    lockfile.write_text(text.replace(dna, "  - name: acme/dna\n    state: purged\n"))
    assert main(["deploy", "build", str(directory), "-o", str(tmp_path / "o")]) == 1
    assert capsys.readouterr().err == (
        "error: acme/protein depends on acme/dna, which the lockfile purges. "
        "Purge acme/protein too, or keep acme/dna detached.\n"
    )
    protein = text[text.index("  - name: acme/protein") :]
    lockfile.write_text(
        text.replace(dna, "  - name: acme/dna\n    state: purged\n").replace(
            protein, "  - name: acme/protein\n    state: purged\n"
        )
    )
    assert main(["deploy", "build", str(directory), "-o", str(tmp_path / "o")]) == 0
    assert "no plugins besides core" in capsys.readouterr().out
    built = tmp_path / "o" / "iki_installed_plugins"
    record = json.loads((built / "lock.json").read_text())
    assert record["plugins"] == [
        {"name": "acme/dna", "state": "purged"},
        {"name": "acme/protein", "state": "purged"},
    ]
    assert not any(built.parent.glob("iki_plugin_*"))


@pytest.mark.usefixtures("allow_file")
def test_the_name_must_match(
    tmp_path: Path, upstream: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    directory = deployment(tmp_path, upstream[1])
    lockfile = directory / "plugins.lock.yaml"
    lockfile.write_text(lockfile.read_text().replace("path: plugins/protein", "path: plugins/dna"))
    assert main(["deploy", "build", str(directory), "-o", str(tmp_path / "o")]) == 1
    err = capsys.readouterr().err
    assert "error: the lockfile's acme/protein is the plugin `acme/dna`" in err


@pytest.mark.usefixtures("allow_file")
def test_paths_must_be_in_the_commit(
    tmp_path: Path, upstream: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    directory = deployment(tmp_path, upstream[1])
    lockfile = directory / "plugins.lock.yaml"
    lockfile.write_text(lockfile.read_text().replace("plugins/protein", "plugins/proteins"))
    assert main(["deploy", "build", str(directory), "-o", str(tmp_path / "o")]) == 1
    assert "has no directory `plugins/proteins`" in capsys.readouterr().err


@pytest.mark.usefixtures("allow_file")
def test_migrations_must_be_complete(
    tmp_path: Path, upstream: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "fork"
    shutil.copytree(upstream[0], repo)
    types = repo / "plugins/dna/types/sequence.yaml"
    types.write_text(
        types.read_text().replace(
            "extends: iki/core.Sequence\n",
            "extends: iki/core.Sequence\nfields:\n  notes: { type: text }\n",
        )
    )
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.org", "commit", "-qam", "notes")
    (tmp_path / "gitconfig").write_text(f'[url "file://{repo}"]\n\tinsteadOf = {URL}\n')
    directory = deployment(tmp_path, git(repo, "rev-parse", "HEAD"))
    assert main(["deploy", "build", str(directory), "-o", str(tmp_path / "o")]) == 1
    err = capsys.readouterr().err
    assert "Add field notes to sequence" in err
    assert "run `iki plugin makemigrations`, commit the migration, and pin that commit" in err


@pytest.mark.parametrize(
    ("repository", "state", "mirrors", "official"),
    [
        (naming.IKI_REPOSITORY, "active", [], True),
        (
            "https://mirror.example/iki.git",
            "detached",
            ["--mirror", "https://mirror.example/iki.git"],
            True,
        ),
        (URL, "active", [], False),
    ],
)
@pytest.mark.usefixtures("allow_file")
def test_building_a_pinned_recipe(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    repository: str,
    state: str,
    mirrors: list[str],
    official: bool,
) -> None:
    # A lab can copy a recipe, rename it and keep its directory layout. Only Iki's
    # repository and its configured mirrors reserve examples/plugins for teaching.
    repo = tmp_path / "repository"
    git(tmp_path, "init", "--quiet", "--initial-branch=main", str(repo))
    path = "examples/plugins/reader_check"
    recipe = repo / path
    shutil.copytree(ROOT / "deploy/live/plugins/live_check", recipe)
    manifest = recipe / "plugin.yaml"
    manifest.write_text(manifest.read_text().replace("acme/live_check", "acme/reader_check"))
    git(repo, "add", ".")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.org", "commit", "-qm", "recipe")
    sha = git(repo, "rev-parse", "HEAD")
    (tmp_path / "gitconfig").write_text(f'[url "file://{repo}"]\n\tinsteadOf = {repository}\n')
    (tmp_path / "plugins.lock.yaml").write_text(
        "apiVersion: iki/v1\nkind: PluginLock\nplugins:\n"
        f"  - name: acme/reader_check\n    git: {repository}\n"
        f"    path: {path}\n    sha: {sha}\n    state: {state}\n"
    )

    server, workers = tmp_path / "server", tmp_path / "workers"
    command = [
        "deploy",
        "build",
        str(tmp_path),
        "-o",
        str(server),
        "--workers",
        str(workers),
        *mirrors,
    ]
    if official:
        assert main(command) == 1
        diagnostic = capsys.readouterr().err
        assert "acme/reader_check" in diagnostic and "--test-only" in diagnostic
        assert not server.exists() and not workers.exists()
        command.append("--test-only")
    assert main(command) == 0, capsys.readouterr().err
    registry = server / "iki_installed_plugins"
    (entry,) = json.loads((registry / "lock.json").read_text())["plugins"]
    assert (entry["name"], entry["sha"], entry["state"]) == ("acme/reader_check", sha, state)
    assert (registry / "bundles" / f"acme__reader_check-{sha}.tar.gz").is_file()
    assert (workers / "plugins/acme__reader_check/plugin.yaml").is_file()


def test_building_an_official_local_fixture_requires_both_switches(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    checkout = tmp_path / "iki"
    git(tmp_path, "init", "--quiet", "--initial-branch=main", str(checkout))
    git(checkout, "remote", "add", "origin", naming.IKI_REPOSITORY)
    fixture = Path("deploy/live/plugins/live_check")
    live_check = checkout / fixture
    shutil.copytree(ROOT / fixture, live_check)
    (tmp_path / "plugins.lock.yaml").write_text(
        "apiVersion: iki/v1\nkind: PluginLock\nplugins:\n"
        f"  - {{name: acme/live_check, local: {live_check}, state: detached}}\n"
    )
    server, workers = tmp_path / "server", tmp_path / "workers"
    build = ["deploy", "build", str(tmp_path), "-o", str(server), "--workers", str(workers)]
    for flag, needed in (("--allow-local", "--test-only"), ("--test-only", "--allow-local")):
        assert main([*build, flag]) == 1
        assert needed in capsys.readouterr().err
        assert not server.exists() and not workers.exists()
    assert main([*build, "--allow-local", "--test-only"]) == 0, capsys.readouterr().err
    assert (server / "iki_plugin_acme__live_check").is_dir()
    assert (workers / "plugins/acme__live_check/plugin.yaml").is_file()


def test_local_plugins_are_for_development(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = tmp_path / "dev"
    directory.mkdir()
    shutil.copytree(PLUGINS / "structure_prediction", tmp_path / "unused")
    (directory / "plugins.lock.yaml").write_text(
        "apiVersion: iki/v1\nkind: PluginLock\nplugins: []\n"
    )
    out = tmp_path / "out"
    assert main(["deploy", "build", str(directory), "-o", str(out)]) == 0
    assert capsys.readouterr().out == f"Built {out}: no plugins besides core.\n"
    assert "LOCAL = False" in (out / "iki_installed_plugins" / "__init__.py").read_text()

    local = tmp_path / "local"
    shutil.copytree(PLUGINS / "dna", local)
    (directory / "plugins.lock.yaml").write_text(
        "apiVersion: iki/v1\nkind: PluginLock\nplugins:\n"
        "  - {name: acme/dna, local: ../local, state: active, settings: dna.yaml}\n"
    )
    (directory / "dna.yaml").write_text("ncbi_email: a@b.org\n")
    assert main(["deploy", "build", str(directory), "-o", str(out)]) == 1
    assert "dna come(s) from a local directory" in capsys.readouterr().err
    # The plugin has no migrations yet.
    assert main(["plugin", "makemigrations", str(local), "--no-input"]) == 0
    capsys.readouterr()
    assert main(["deploy", "build", str(directory), "-o", str(out), "--allow-local"]) == 0
    assert "LOCAL = True" in (out / "iki_installed_plugins" / "__init__.py").read_text()


def test_a_setup_build_cant_download_from_an_outside_apis_host(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A setup build belongs to the deployment, so it can't call an outside API (ADR 0141).
    Where a setting gives the host it downloads from, only the deployment's build knows it's
    another plugin's API's, and refuses it there."""
    directory = tmp_path / "dev"
    (directory / "settings").mkdir(parents=True)
    for name in ("dna", "protein", "structure_prediction"):
        shutil.copytree(PLUGINS / name, tmp_path / name)
    prediction = tmp_path / "structure_prediction"
    manifest = prediction / "plugin.yaml"
    manifest.write_text(
        manifest.read_text().replace(
            "settings:\n",
            "settings:\n  weights_host:\n    type: string\n    format: hostname\n"
            "    required: true\n",
        )
    )
    environment = prediction / "environments" / "alphafold-gpu.yaml"
    environment.write_text(
        environment.read_text().replace(
            "network: [storage.googleapis.com]", "network: ['{settings.weights_host}']"
        )
    )
    assert main(["plugin", "makemigrations", str(tmp_path / "dna"), "--no-input"]) == 0
    withs = ["--with", str(tmp_path / "dna")]
    assert main(["plugin", "makemigrations", str(tmp_path / "protein"), *withs, "--no-input"]) == 0
    withs += ["--with", str(tmp_path / "protein")]
    assert main(["plugin", "makemigrations", str(prediction), *withs, "--no-input"]) == 0
    (directory / "settings" / "dna.yaml").write_text("ncbi_email: a@b.org\n")
    (directory / "plugins.lock.yaml").write_text(
        "apiVersion: iki/v1\nkind: PluginLock\nplugins:\n"
        "  - {name: acme/dna, local: ../dna, state: active, settings: settings/dna.yaml}\n"
        "  - {name: acme/protein, local: ../protein, state: active}\n"
        "  - {name: acme/prediction, local: ../structure_prediction, state: active,"
        " settings: settings/prediction.yaml}\n"
    )
    out = tmp_path / "out"
    build = ["deploy", "build", str(directory), "-o", str(out), "--allow-local"]
    # acme/protein's FoldCloud is at fold.models.example.
    (directory / "settings" / "prediction.yaml").write_text("weights_host: fold.models.example\n")
    capsys.readouterr()
    assert main(build) == 1
    err = capsys.readouterr().err
    assert err.startswith(
        "error: acme/prediction's alphafold-gpu setup downloads from fold.models.example, which "
        "is acme/protein.fold's host (FoldCloud, an outside API). Setup builds can't call "
        "outside APIs"
    )
    assert "  hint: give the setting another host" in err
    assert not out.exists()
    # Another host builds, and the setup reaches the host the setting gives.
    (directory / "settings" / "prediction.yaml").write_text("weights_host: weights.example.org\n")
    assert main(build) == 0
    built = json.loads((out / "iki_plugin_acme__prediction" / "manifest.json").read_text())
    setup = built["plugin"]["environments"]["alphafold-gpu"]
    assert setup["setup_network"] == ["weights.example.org"]


def test_only_replaces_a_previous_build(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    directory = tmp_path / "dev"
    directory.mkdir()
    (directory / "plugins.lock.yaml").write_text(
        "apiVersion: iki/v1\nkind: PluginLock\nplugins: []\n"
    )
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "notes.txt").write_text("mine")
    assert main(["deploy", "build", str(directory), "-o", str(tmp_path / "out")]) == 1
    assert "holds files a build didn't write (notes.txt)" in capsys.readouterr().err
    assert (tmp_path / "out" / "notes.txt").exists()


SECRET = "ghp_EXAMPLE0SECRET"


@pytest.mark.parametrize(
    "git",
    [
        f"https://bob:{SECRET}@git.example/acme/plugins.git",
        f"https://git.example/acme/plugins.git?private_token={SECRET}",
    ],
)
@pytest.mark.parametrize("flow", [False, True], ids=["block", "flow"])
def test_a_lockfile_with_credentials_builds_nothing_and_never_prints_them(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], git: str, flow: bool
) -> None:
    directory = tmp_path / "prod"
    directory.mkdir()
    entry = (
        f'  - {{name: acme/dna, git: "{git}", sha: "{"a" * 40}", state: active}}\n'
        if flow
        else f'  - name: acme/dna\n    git: "{git}"\n    sha: "{"a" * 40}"\n    state: active\n'
    )
    (directory / "plugins.lock.yaml").write_text(
        "apiVersion: iki/v1\nkind: PluginLock\nplugins:\n" + entry
    )
    assert main(["deploy", "build", str(directory), "-o", str(tmp_path / "out")]) == 1
    printed = capsys.readouterr()
    assert "[git-credentials]" in printed.err
    assert "credential helper" in printed.err
    assert SECRET not in printed.out + printed.err
    assert not (tmp_path / "out").exists()


def test_a_failed_fetch_never_prints_the_credentials_gits_config_adds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A rewrite that adds credentials to the same repository is allowed (ADR 0114).
    (tmp_path / "gitconfig").write_text(
        f'[url "https://bob:{SECRET}@git.invalid/"]\n\tinsteadOf = https://git.invalid/\n'
    )
    directory = tmp_path / "prod"
    directory.mkdir()
    (directory / "plugins.lock.yaml").write_text(
        "apiVersion: iki/v1\nkind: PluginLock\nplugins:\n"
        f"  - name: acme/dna\n    git: https://git.invalid/acme/plugins.git\n"
        f'    sha: "{"a" * 40}"\n    state: active\n'
    )
    assert main(["deploy", "build", str(directory), "-o", str(tmp_path / "out")]) == 1
    printed = capsys.readouterr()
    assert "couldn't fetch commit aaaaaaaaaaaa from https://git.invalid/acme/plugins.git" in (
        printed.err
    )
    assert SECRET not in printed.out + printed.err


def test_lockfile_problems_point_at_the_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    Path("plugins.lock.yaml").write_text(
        "apiVersion: iki/v1\nkind: PluginLock\nplugins:\n"
        f"  - {{name: acme/dna, git: {URL}, sha: v1.2.0, state: active}}\n"
    )
    assert main(["deploy", "build", ".", "-o", "out"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("plugins.lock.yaml:4:")
    assert "hint: Give the full commit SHA." in err


def test_a_plugin_with_a_symbolic_link_is_refused(tmp_path: Path) -> None:
    from iki_cli import workers

    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / "plugin.yaml").write_text("x")
    (plugin / "secrets").symlink_to("/etc/passwd")
    with pytest.raises(ValueError, match="secrets is a symbolic link"):
        workers.bundle("acme/plugin", plugin, tmp_path / "out.tar.gz")
    (plugin / "secrets").unlink()
    first = workers.bundle("acme/plugin", plugin, tmp_path / "a.tar.gz")
    (plugin / "plugin.yaml").touch()  # a new time changes nothing
    assert workers.bundle("acme/plugin", plugin, tmp_path / "b.tar.gz") == first


def test_the_images_runs_use_are_those_bound_to_container_templates(tmp_path: Path) -> None:
    from iki_cli.deploy import run_images
    from iki_spec import compute as compute_spec
    from iki_spec.validate import load_plugin

    plugins = []
    for name, deps in {
        "dna": [],
        "protein": ["dna"],
        "structure_prediction": ["protein", "dna"],
    }.items():
        loaded = load_plugin(PLUGINS / name, dependencies=[PLUGINS / d for d in deps])
        assert loaded.plugin is not None
        plugins.append(loaded.plugin)
    file = tmp_path / "compute.yaml"
    file.write_text(
        "apiVersion: iki/v1\nkind: ComputeTemplate\nname: gpu\nprovisioner: k8s_job\n"
        "capacity: { cpu: 4, memory: 16Gi, gpu: { count: 1, memory: 16Gi } }\n"
    )
    unbound = compute_spec.load_compute(file, plugins, "compute.yaml").compute
    assert unbound is not None
    assert run_images(plugins, unbound) == []
    file.write_text(
        file.read_text() + "---\napiVersion: iki/v1\nkind: EnvironmentBinding\n"
        "environment: acme/prediction.colabfold\ntemplates: [{ template: gpu }]\n"
    )
    bound = compute_spec.load_compute(file, plugins, "compute.yaml").compute
    assert bound is not None
    assert run_images(plugins, bound) == [
        "ghcr.io/acme/colabfold@sha256:"
        "2c26b46b68ffc68ff99b453c1d30413413422d706483bfa0f98a5e886266e7ae"
    ]


@pytest.mark.usefixtures("allow_file")
def test_the_build_writes_the_images_runs_use(
    tmp_path: Path, upstream: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    directory = deployment(tmp_path, upstream[1])
    images = tmp_path / "build" / "images.txt"
    args = ["deploy", "build", str(directory), "-o", str(tmp_path / "o"), "--images", str(images)]
    assert main(args) == 0
    assert images.read_text() == ""
    assert "Wrote the 0 image(s) runs use" in capsys.readouterr().out


@pytest.mark.parametrize(("version_range", "matches"), [(">=1.2,<2", True), (">=2,<3", False)])
def test_the_build_approves_deployment_images_for_the_installed_release(
    tmp_path: Path, version_range: str, matches: bool
) -> None:
    from iki_spec.compute import ImageBinding, compute_from_dict

    plugin = tmp_path / "tools"
    (plugin / "environments").mkdir(parents=True)
    (plugin / "plugin.yaml").write_text(
        "apiVersion: iki/v1\nkind: Plugin\nname: acme/tools\nversion: 1.2.0\n"
    )
    (plugin / "environments" / "tools.yaml").write_text(
        "apiVersion: iki/v1\nkind: Environment\nname: tools\ntiers: [container]\n"
        "customer_image_required: true\n"
    )
    directory = tmp_path / "dev"
    directory.mkdir()
    (directory / "plugins.lock.yaml").write_text(
        "apiVersion: iki/v1\nkind: PluginLock\nplugins:\n"
        "  - { name: acme/tools, local: ../tools, state: active }\n"
    )
    image = "registry.example/team/tools@sha256:" + "a" * 64
    (directory / "compute.yaml").write_text(
        "apiVersion: iki/v1\nkind: ComputeTemplate\nname: customer-job\n"
        "provisioner: k8s_job\ncapacity: { cpu: 1, memory: 1Gi }\n"
        "price: { per_hour: 0.10, currency: USD }\n"
        "---\napiVersion: iki/v1\nkind: EnvironmentBinding\n"
        "environment: acme/tools.tools\ntemplates: [{ template: customer-job }]\n"
        "---\napiVersion: iki/v1\nkind: EnvironmentImageBinding\n"
        f"environment: acme/tools.tools\nplugin_version: '{version_range}'\n"
        f"image: {image}\npull_secret: private-registry\n"
    )
    out = tmp_path / "out"
    images = tmp_path / "images.txt"
    assert (
        main(
            [
                "deploy",
                "build",
                str(directory),
                "-o",
                str(out),
                "--allow-local",
                "--images",
                str(images),
            ]
        )
        == 0
    )
    compute = compute_from_dict(json.loads((out / "compute.json").read_text()))
    binding = ImageBinding("acme/tools.tools", version_range, image, "private-registry")
    assert compute.image_bindings == (binding,)
    assert compute.image_binding("acme/tools.tools", "1.2.0") == (binding if matches else None)
    assert images.read_text() == (image + "\n" if matches else "")
