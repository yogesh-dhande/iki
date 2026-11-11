"""The core plugin's app (server/src/iki_plugin_core) is current with its manifests."""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _build_core() -> object:
    spec = importlib.util.spec_from_file_location("build_core", ROOT / "scripts" / "build_core.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_generated_app_matches_the_manifests() -> None:
    behind = _build_core().stale()  # type: ignore[attr-defined]
    assert behind == [], f"run `make core`: {behind} are behind spec/src/iki_spec/core"


def test_its_migrations_are_current() -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("DJANGO_", "IKI_"))}
    result = subprocess.run(
        [sys.executable, "-m", "iki_server.plugins.makemigrations", "core", "--check", "--dry-run"],
        capture_output=True,
        text=True,
        check=False,
        env={**env, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert result.returncode == 0, f"run `make core`:\n{result.stdout}{result.stderr}"
