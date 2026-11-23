"""The installed pre-commit dispatcher lives beside Git LFS and follows the worktree."""

import os
import subprocess
from pathlib import Path

INSTALL = Path(__file__).resolve().parents[2] / "git-hooks" / "install"


def run(directory: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = {key: val for key, val in os.environ.items() if not key.startswith("GIT_")}
    return subprocess.run(  # noqa: S603
        args,
        cwd=directory,
        env={**env, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
        check=check,
        capture_output=True,
        text=True,
    )


def test_hook_follows_the_committing_worktree_and_preserves_lfs(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    run(tmp_path, "git", "init", "-q", str(repo))
    run(
        repo,
        "git",
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "--allow-empty",
        "-qm",
        "initial",
    )
    linked = tmp_path / "linked"
    run(repo, "git", "worktree", "add", "-q", "--detach", str(linked))
    hooks = repo / ".git/hooks"
    lfs = {
        name: f"Git LFS {name}\n"
        for name in ("post-checkout", "post-commit", "post-merge", "pre-push")
    }
    for name, contents in lfs.items():
        (hooks / name).write_text(contents)

    run(linked, str(INSTALL))
    run(repo, str(INSTALL))
    # A branch from before the introduction of the hook can still commit.
    run(repo, "git", "hook", "run", "pre-commit")
    for tree in (repo, linked):
        hook = tree / "scripts/git-hooks/pre-commit"
        hook.parent.mkdir(parents=True)
        hook.write_text(f"#!/bin/sh\necho {tree.name}\n")
        hook.chmod(0o755)
        assert run(tree, "git", "hook", "run", "pre-commit").stderr.strip() == tree.name
    assert {name: (hooks / name).read_text() for name in lfs} == lfs


def test_custom_pre_commit_survives_installation(tmp_path: Path) -> None:
    run(tmp_path, "git", "init", "-q")
    existing = tmp_path / ".git/hooks/pre-commit"
    existing.write_text("#!/bin/sh\necho mine\n")
    assert run(tmp_path, str(INSTALL), check=False).returncode != 0
    assert existing.read_text() == "#!/bin/sh\necho mine\n"


def test_custom_hooks_path_survives_installation(tmp_path: Path) -> None:
    run(tmp_path, "git", "init", "-q")
    custom = tmp_path / "custom"
    custom.mkdir()
    run(tmp_path, "git", "config", "core.hooksPath", str(custom))
    assert run(tmp_path, str(INSTALL), check=False).returncode != 0
    assert list(custom.iterdir()) == []
