"""The ImmuneBuilder weights pin (check-immunebuilder-weights.sh): any build accepts a
release the publisher retained, so an older revision still rebuilds after a newer release;
development also requires the last one published. The registry is a fake ``docker``."""

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "ci" / "check-immunebuilder-weights.sh"
IMAGE = "ghcr.io/yogesh-dhande/iki-immunebuilder-weights"
#: Two releases' manifests, as the registry serves them: the older, then the newer.
OLDER = '{"schemaVersion":2,"manifests":[{"digest":"sha256:' + "1" * 64 + '"}]}'
NEWER = '{"schemaVersion":2,"manifests":[{"digest":"sha256:' + "2" * 64 + '"}]}'


def digest(manifest: str) -> str:
    return "sha256:" + hashlib.sha256(manifest.encode()).hexdigest()


def registry(tmp_path: Path, tags: dict[str, str]) -> Path:
    """A ``docker`` whose ``buildx imagetools inspect --raw REF`` serves these tags."""
    served = tmp_path / "served"
    served.mkdir()
    for tag, manifest in tags.items():
        (served / tag).write_text(manifest, encoding="utf-8")
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    docker = bin_ / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        '[ "$1 $2 $3 $4" = "buildx imagetools inspect --raw" ] || exit 64\n'
        f"tag=${{5#{IMAGE}:}}\n"
        f'[ -f "{served}/$tag" ] || {{ echo "not found: $5" >&2; exit 1; }}\n'
        f'cat "{served}/$tag"\n',
        encoding="utf-8",
    )
    docker.chmod(0o755)
    return bin_


def check(tmp_path: Path, pinned: str, *args: str) -> subprocess.CompletedProcess[str]:
    tags = {
        f"sha256-{digest(OLDER).removeprefix('sha256:')}": OLDER,
        f"sha256-{digest(NEWER).removeprefix('sha256:')}": NEWER,
        "published": NEWER,
    }
    tmp_path.mkdir(parents=True, exist_ok=True)
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        f"FROM scratch\nCOPY --from={IMAGE}@{pinned} /weights /opt/weights\n", encoding="utf-8"
    )
    return subprocess.run(  # noqa: S603 - the repository's script
        ["bash", str(SCRIPT), *args],  # noqa: S607
        env={
            "PATH": f"{registry(tmp_path, tags)}{os.pathsep}{os.environ['PATH']}",
            "IKI_WEIGHTS_DOCKERFILE": str(dockerfile),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


@pytest.mark.parametrize("release", [OLDER, NEWER], ids=["older", "newer"])
def test_a_build_accepts_any_retained_release(tmp_path: Path, release: str) -> None:
    done = check(tmp_path, digest(release))
    assert done.returncode == 0, done.stderr
    assert "pinned to the retained release" in done.stdout


def test_development_requires_the_last_published_release(tmp_path: Path) -> None:
    older = check(tmp_path / "older", digest(OLDER), "--latest")
    assert older.returncode == 1
    assert "Last published: " + digest(NEWER) in older.stderr
    newer = check(tmp_path / "newer", digest(NEWER), "--latest")
    assert newer.returncode == 0, newer.stderr


def test_a_digest_no_release_retained_is_refused(tmp_path: Path) -> None:
    done = check(tmp_path, "sha256:" + "f" * 64)
    assert done.returncode == 1
    assert "no release the publisher retained" in done.stderr


def test_the_tools_image_pins_one_digest(tmp_path: Path) -> None:
    done = check(tmp_path, "latest")
    assert done.returncode == 1
    assert "Expected exactly one digest-pinned COPY" in done.stderr
