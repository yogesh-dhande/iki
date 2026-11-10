import textwrap
from pathlib import Path

import pytest

from iki_spec.lockfile import LockedPlugin, load_lock, lock_to_dict

SHA = "4f1c2b9e0d6a7c3b8e5f1a2d9c0b7e6f5a4d3c2b"


def lock(tmp_path: Path, plugins: str, *, settings: tuple[str, ...] = ()) -> Path:
    for name in settings:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("{}\n")
    file = tmp_path / "plugins.lock.yaml"
    file.write_text(
        "apiVersion: iki/v1\nkind: PluginLock\nplugins:\n"
        + textwrap.indent(textwrap.dedent(plugins), "  ")
    )
    return file


def codes(file: Path) -> list[str]:
    return [p.code for p in load_lock(file).problems]


def test_reads_plugins_from_git_and_local_directories(tmp_path: Path) -> None:
    file = lock(
        tmp_path,
        f"""\
        - name: dna
          git: https://github.com/acme/iki-plugins.git
          path: plugins/dna
          sha: {SHA}
          state: active
          settings: settings/dna.yaml
        - name: assays
          git: git@github.com:acme/assays.git
          sha: {"b" * 64}
          state: active
        - name: scratch
          local: ../scratch
          state: active
        """,
        settings=("settings/dna.yaml",),
    )
    result = load_lock(file)
    assert result.problems == []
    assert result.lock is not None
    assert result.lock.plugins[0] == LockedPlugin(
        name="dna",
        state="active",
        git="https://github.com/acme/iki-plugins.git",
        sha=SHA,
        path="plugins/dna",
        settings="settings/dna.yaml",
    )
    assert result.lock.has_local
    assert lock_to_dict(result.lock)["plugins"][2] == {
        "name": "scratch",
        "state": "active",
        "local": "../scratch",
    }


def test_an_empty_deployment(tmp_path: Path) -> None:
    file = tmp_path / "plugins.lock.yaml"
    file.write_text("apiVersion: iki/v1\nkind: PluginLock\nplugins: []\n")
    result = load_lock(file)
    assert result.ok
    assert result.lock is not None
    assert result.lock.plugins == ()


@pytest.mark.parametrize(
    ("entry", "code"),
    [
        ("- {name: dna, git: https://x.org/p, sha: main, state: active}", "schema"),
        (f"- {{name: dna, git: https://x.org/p, sha: {SHA[:12]}, state: active}}", "schema"),
        (f"- {{name: dna, git: 'file:///srv/p', sha: {SHA}, state: active}}", "schema"),
        (f"- {{name: dna, git: 'ext::sh -c id', sha: {SHA}, state: active}}", "schema"),
        (f"- {{name: dna, git: https://x.org/p, sha: {SHA}, state: removed}}", "schema"),
        (f"- {{name: dna, git: https://x.org/p, sha: {SHA}}}", "schema"),
        ("- {name: dna, git: https://x.org/p, state: active}", "missing-sha"),
        ("- {name: dna, state: active}", "missing-source"),
        (f"- {{name: dna, local: ../dna, sha: {SHA}, state: active}}", "mixed-source"),
        (
            f"- {{name: dna, git: https://x.org/p, sha: {SHA}, path: a/../../b, state: active}}",
            "path-outside",
        ),
        (
            f"- {{name: core, git: https://x.org/p, sha: {SHA}, state: active}}",
            "core-is-builtin",
        ),
        (
            f"- {{name: dna, git: https://x.org/p, sha: {SHA}, state: active, settings: s.yaml}}",
            "missing-settings-file",
        ),
        (
            f"- {{name: dna, git: https://x.org/p, sha: {SHA}, state: active, settings: /s.yaml}}",
            "absolute-path",
        ),
    ],
)
def test_refuses(tmp_path: Path, entry: str, code: str) -> None:
    assert codes(lock(tmp_path, entry)) == [code]


def test_each_plugin_once(tmp_path: Path) -> None:
    file = lock(
        tmp_path, "- {name: dna, local: a, state: active}\n- {name: dna, local: b, state: active}\n"
    )
    problems = load_lock(file).problems
    assert [p.code for p in problems] == ["duplicate-plugin"]
    assert problems[0].location is not None
    assert problems[0].location.line == 5


def test_explains_a_branch_given_as_a_sha(tmp_path: Path) -> None:
    file = lock(tmp_path, "- {name: dna, git: https://x.org/p, sha: main, state: active}")
    (problem,) = load_lock(file).problems
    assert problem.hint is not None
    assert "git ls-remote" in problem.hint


def test_a_sha_yaml_reads_as_a_number(tmp_path: Path) -> None:
    number = "1234567890" * 4
    file = lock(tmp_path, f"- {{name: dna, git: https://x.org/p, sha: {number}, state: active}}")
    (problem,) = load_lock(file).problems
    assert problem.hint == "YAML read this SHA as a number: put it in quotes."


def test_not_a_lockfile(tmp_path: Path) -> None:
    file = tmp_path / "plugins.lock.yaml"
    file.write_text("apiVersion: iki/v1\nkind: Plugin\nname: dna\n")
    assert [p.code for p in load_lock(file).problems] == ["not-a-lockfile"]
    assert [p.code for p in load_lock(tmp_path / "nope.yaml").problems] == ["missing-lockfile"]
