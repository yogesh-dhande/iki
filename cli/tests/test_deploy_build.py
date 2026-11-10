import hashlib
import json
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from iki_cli import fetch
from iki_cli.main import main

PLUGINS = Path(__file__).parents[2] / "spec" / "tests" / "plugins"
URL = "https://git.example/acme/plugins.git"


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
        f"  - name: dna\n    git: {URL}\n    path: plugins/dna\n    sha: {sha}\n    state: active\n"
    )
    if settings is not None:
        (directory / "settings" / "dna.yaml").write_text(settings)
        dna += "    settings: settings/dna.yaml\n"
    protein = f"  - name: protein\n    git: {URL}\n    path: plugins/protein\n    sha: {sha}\n"
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
def test_builds_the_pinned_commits(
    tmp_path: Path, upstream: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    _, sha = upstream
    out = tmp_path / "plugins"
    assert main(["deploy", "build", str(deployment(tmp_path, sha)), "-o", str(out)]) == 0
    printed = capsys.readouterr().out
    assert f"Fetched dna at {sha[:12]} from {URL}" in printed
    assert printed.endswith(f"Built {out}: dna 1.2.0, protein 3.0.0.\n")
    assert sorted(p.name for p in out.iterdir()) == [
        "iki_installed_plugins",
        "iki_plugin_dna",
        "iki_plugin_protein",
    ]
    assert json.loads((out / "iki_plugin_dna" / "settings.json").read_text()) == {
        "max_sequence_length": 10_000_000,
        "ncbi_email": "a@b.org",
    }
    assert sorted(p.name for p in (out / "iki_plugin_protein" / "migrations").iterdir()) == [
        "0001_initial.py",
        "__init__.py",
    ]
    installed = (out / "iki_installed_plugins" / "__init__.py").read_text()
    assert "IKI_VERSION = '0.1.0.dev0'" in installed
    assert "LOCAL = False" in installed
    record = json.loads((out / "iki_installed_plugins" / "lock.json").read_text())
    assert record["plugins"][0] == {
        "name": "dna",
        "git": URL,
        "path": "plugins/dna",
        "sha": sha,
        "state": "active",
        "version": "1.2.0",
        "settings_sha256": hashlib.sha256(b"ncbi_email: a@b.org\n").hexdigest(),
    }

    # The same lockfile builds the same bytes, from the cache.
    first = tree_hash(out)
    (tmp_path / "gitconfig").write_text("")
    assert main(["deploy", "build", str(tmp_path / "deployments/prod"), "-o", str(out)]) == 0
    assert tree_hash(out) == first


def test_git_cant_be_sent_to_other_transports(
    tmp_path: Path, upstream: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    # Only https and ssh: here a URL rewrite would make git read a file:// repository.
    code = main(["deploy", "build", str(deployment(tmp_path, upstream[1])), "-o", str(tmp_path)])
    assert code == 1
    err = capsys.readouterr().err
    assert f"error: dna: couldn't fetch commit {upstream[1][:12]} from {URL}" in err
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
    assert "error: dna: couldn't fetch commit 000000000000" in capsys.readouterr().err


@pytest.mark.usefixtures("allow_file")
def test_settings_are_checked(
    tmp_path: Path, upstream: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    directory = deployment(tmp_path, upstream[1], settings="ncbi_email: nope\nmax_length: 3\n")
    assert main(["deploy", "build", str(directory), "-o", str(tmp_path / "o")]) == 1
    err = capsys.readouterr().err
    assert "settings/dna.yaml:1:13: error: `ncbi_email`" in err
    assert "The dna plugin has no setting `max_length`." in err
    assert not (tmp_path / "o").exists()


@pytest.mark.usefixtures("allow_file")
def test_required_settings_need_a_file(
    tmp_path: Path, upstream: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    directory = deployment(tmp_path, upstream[1], settings=None)
    assert main(["deploy", "build", str(directory), "-o", str(tmp_path / "o")]) == 1
    err = capsys.readouterr().err
    assert "The dna plugin needs `ncbi_email` to be set." in err
    assert "with `settings:` in the lockfile" in err


@pytest.mark.usefixtures("allow_file")
def test_dependencies_must_be_in_the_lockfile(
    tmp_path: Path, upstream: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    directory = deployment(tmp_path, upstream[1])
    lockfile = directory / "plugins.lock.yaml"
    text = lockfile.read_text()
    lockfile.write_text(text[: text.index("  - name: protein")].replace("dna", "protein", 1))
    lockfile.write_text(lockfile.read_text().replace("plugins/dna", "plugins/protein"))
    lockfile.write_text(lockfile.read_text().replace("    settings: settings/dna.yaml\n", ""))
    assert main(["deploy", "build", str(directory), "-o", str(tmp_path / "o")]) == 1
    assert capsys.readouterr().err == (
        "error: protein needs the dna plugin: add it to the lockfile.\n"
    )


@pytest.mark.usefixtures("allow_file")
def test_the_name_must_match(
    tmp_path: Path, upstream: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    directory = deployment(tmp_path, upstream[1])
    lockfile = directory / "plugins.lock.yaml"
    lockfile.write_text(lockfile.read_text().replace("path: plugins/protein", "path: plugins/dna"))
    assert main(["deploy", "build", str(directory), "-o", str(tmp_path / "o")]) == 1
    err = capsys.readouterr().err
    assert "error: the lockfile's protein is the plugin `dna`" in err


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
            "  name: { type: string", "  notes: { type: text }\n  name: { type: string"
        )
    )
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.org", "commit", "-qam", "notes")
    (tmp_path / "gitconfig").write_text(f'[url "file://{repo}"]\n\tinsteadOf = {URL}\n')
    directory = deployment(tmp_path, git(repo, "rev-parse", "HEAD"))
    assert main(["deploy", "build", str(directory), "-o", str(tmp_path / "o")]) == 1
    err = capsys.readouterr().err
    assert "Add field notes to sequence" in err
    assert "run `iki plugin makemigrations`, commit the migration, and pin that commit" in err


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
        "  - {name: dna, local: ../local, state: active, settings: dna.yaml}\n"
    )
    (directory / "dna.yaml").write_text("ncbi_email: a@b.org\n")
    assert main(["deploy", "build", str(directory), "-o", str(out)]) == 1
    assert "dna come(s) from a local directory" in capsys.readouterr().err
    # The plugin has no migrations yet.
    assert main(["plugin", "makemigrations", str(local), "--no-input"]) == 0
    capsys.readouterr()
    assert main(["deploy", "build", str(directory), "-o", str(out), "--allow-local"]) == 0
    assert "LOCAL = True" in (out / "iki_installed_plugins" / "__init__.py").read_text()


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


def test_lockfile_problems_point_at_the_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    Path("plugins.lock.yaml").write_text(
        "apiVersion: iki/v1\nkind: PluginLock\nplugins:\n"
        f"  - {{name: dna, git: {URL}, sha: v1.2.0, state: active}}\n"
    )
    assert main(["deploy", "build", ".", "-o", "out"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("plugins.lock.yaml:4:")
    assert "hint: Give the full commit SHA." in err
