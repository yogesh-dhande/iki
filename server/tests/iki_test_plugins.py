"""The fixture plugins (spec/tests/plugins), built and installed for the server's tests.

Each test session installs dna, protein and structure_prediction, so tests
use real generated models, tables and history. The build is cached under
``.pytest_cache`` by a hash of everything it depends on, so only the first
session after a change pays for it. Set ``IKI_TEST_PLUGINS=0`` for a
process that installs its own build (``test_plugin_migrations``).
"""

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLUGINS = ROOT / "spec" / "tests" / "plugins"
#: What the build depends on: change any of it and the plugins are rebuilt.
INPUTS = (
    PLUGINS,
    ROOT / "cli" / "src" / "iki_cli" / "plugin.py",
    ROOT / "codegen" / "src",
    ROOT / "spec" / "src" / "iki_spec",
    ROOT / "server" / "src" / "iki_server" / "plugins",
    ROOT / "server" / "src" / "iki_server" / "entities",
    # Plugin migrations depend on the latest of Iki's own.
    *sorted((ROOT / "server" / "src" / "iki_server").glob("*/migrations")),
)
ORDER = (("dna",), ("protein", "dna"), ("structure_prediction", "protein", "dna"))


def _key() -> str:
    digest = hashlib.sha256()
    for root in INPUTS:
        for file in sorted(root.rglob("*")) if root.is_dir() else [root]:
            if file.is_file() and "__pycache__" not in file.parts:
                digest.update(str(file.relative_to(ROOT)).encode() + b"\0" + file.read_bytes())
    return digest.hexdigest()[:20]


def _iki(*args: str | Path, cwd: Path) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("DJANGO_", "IKI_"))}
    result = subprocess.run(  # noqa: S603 - this interpreter, the iki CLI
        [sys.executable, "-m", "iki_cli", *map(str, args)],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env={**env, "NO_COLOR": "1", "PYTHONDONTWRITEBYTECODE": "1"},
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"iki {' '.join(map(str, args))} failed:\n{result.stdout}{result.stderr}"
        )


def ensure_built() -> Path:
    """The directory to put on ``sys.path``, building it first if needed."""
    final = ROOT / ".pytest_cache" / "iki-test-plugins" / _key()
    if (final / "iki_installed_plugins").is_dir():
        return final
    final.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=final.parent) as tmp:
        work = Path(tmp)
        shutil.copytree(PLUGINS, work / "plugins")
        for name, *dependencies in ORDER:
            withs = [arg for d in dependencies for arg in ("--with", d)]
            _iki("plugin", "makemigrations", name, *withs, "--no-input", cwd=work / "plugins")
        _iki(
            "plugin",
            "build",
            "dna",
            "protein",
            "structure_prediction",
            "--output",
            work / "build",
            cwd=work / "plugins",
        )
        try:
            (work / "build").rename(final)
        except OSError:
            if not (final / "iki_installed_plugins").is_dir():  # another worker won the race
                raise
    return final
