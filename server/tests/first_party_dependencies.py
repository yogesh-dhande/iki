"""The repo's first-party plugins and template, and their dependency-ordered test fixtures."""

from pathlib import Path

from iki_spec.first_party_dependencies import dependency_closures

ROOT = Path(__file__).resolve().parents[2]
PLUGINS = ROOT / "plugins"
TEMPLATE = ROOT / "plugin-template"
FIRST_PARTY = dependency_closures(
    PLUGINS, *(path.parent for path in sorted(PLUGINS.glob("*/plugin.yaml"))), TEMPLATE
)


def build_order(*plugins: Path) -> tuple[Path, ...]:
    """Include each requested plugin and its declared dependencies once, prerequisites first."""
    return tuple(dict.fromkeys(dep for path in plugins for dep in (*FIRST_PARTY[path], path)))


def withs(dependencies: tuple[Path, ...]) -> list[str]:
    return [arg for dep in dependencies for arg in ("--with", str(dep))]
