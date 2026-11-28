"""The fixture plugins (spec/tests/plugins), built and installed for the server's tests.

Each test session installs dna, protein and structure_prediction, so tests
use real generated models, tables and history; and the liquid_handling example, whose
Tecan worklist writer is a plugin's export format (ADR 0230), with its module as
``make test-wasm`` built it. The build is cached under
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
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PLUGINS = ROOT / "spec" / "tests" / "plugins"
#: Examples installed with them: (directory, name), each depending on none of them.
EXAMPLES = ((ROOT / "examples" / "plugins" / "liquid_handling", "liquid_handling"),)
#: What the build depends on: change any of it and the plugins are rebuilt.
INPUTS = (
    PLUGINS,
    *(directory for directory, _ in EXAMPLES),
    ROOT / "cli" / "src" / "iki_cli" / "plugin.py",
    ROOT / "codegen" / "src",
    ROOT / "spec" / "src" / "iki_spec",
    ROOT / "server" / "src" / "iki_server" / "plugins",
    ROOT / "server" / "src" / "iki_server" / "entities",
    # Plugin migrations depend on the latest of Iki's own.
    *sorted((ROOT / "server" / "src" / "iki_server").glob("*/migrations")),
)
ORDER = (
    ("dna",),
    ("protein", "dna"),
    ("structure_prediction", "protein", "dna"),
    *((name,) for _, name in EXAMPLES),
)


def _key() -> str:
    digest = hashlib.sha256()
    for root in INPUTS:
        for file in sorted(root.rglob("*")) if root.is_dir() else [root]:
            if file.is_file() and not {"__pycache__", "target"} & set(file.parts):
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


def ensure_built(variant: str = "", change: Callable[[Path], None] | None = None) -> Path:
    """The directory to put on ``sys.path``, building it first if needed.

    A ``variant`` is built from the plugins as ``change`` leaves them (given their
    directory), for a server whose plugins differ from the tests' in what doesn't touch
    the database, such as an environment's image."""
    final = (
        ROOT / ".pytest_cache" / "iki-test-plugins" / (_key() + (f"-{variant}" if variant else ""))
    )
    if (final / "iki_installed_plugins").is_dir():
        return final
    final.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=final.parent) as tmp:
        work = Path(tmp)
        shutil.copytree(PLUGINS, work / "plugins")
        for directory, name in EXAMPLES:
            shutil.copytree(
                directory, work / "plugins" / name, ignore=shutil.ignore_patterns("target")
            )
        if change is not None:
            change(work / "plugins")
        for name, *dependencies in ORDER:
            withs = [arg for d in dependencies for arg in ("--with", d)]
            _iki("plugin", "makemigrations", name, *withs, "--no-input", cwd=work / "plugins")
        _iki(
            "plugin",
            "build",
            "dna",
            "protein",
            "structure_prediction",
            *(name for _, name in EXAMPLES),
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


def run_output(returned: Any) -> dict[str, Any]:
    """What a plugin script's action function returns, as `iki.main` writes it into
    output.json (ADR 0287): an `Output`'s document, or run output's fields as a dict."""
    document = getattr(returned, "document", None)
    if callable(document):
        return dict(document())
    return {"apiVersion": "iki/v1", "kind": "RunOutput", **returned}
