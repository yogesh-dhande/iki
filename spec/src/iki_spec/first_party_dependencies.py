"""Resolve declared plugin dependencies against a checkout's first-party plugin directories."""

from graphlib import CycleError, TopologicalSorter
from pathlib import Path

from iki_spec.yamlsource import read_documents


def _dependency_graph(plugins_dir: Path, roots: tuple[Path, ...]) -> dict[Path, tuple[Path, ...]]:
    # Explicit paths take precedence when a caller supplies its own copy of a dependency.
    root = plugins_dir.resolve()
    siblings = (
        path.parent
        for path in sorted(plugins_dir.glob("*/plugin.yaml"))
        if path.resolve() == root / path.parent.name / "plugin.yaml" and path.parent not in roots
    )
    paths: dict[str, Path] = {}
    required: dict[Path, tuple[str, ...]] = {}
    for directory in dict.fromkeys([*siblings, *roots]):
        documents, _ = read_documents(directory / "plugin.yaml", "plugin.yaml")
        manifest = documents[0].data if documents else None
        if isinstance(manifest, dict) and isinstance(name := manifest.get("name"), str):
            if directory not in roots and name != f"iki/{directory.name}":
                continue
            paths[name] = directory
            dependencies = manifest.get("dependencies")
            if isinstance(dependencies, dict):
                required[directory] = tuple(dependencies)

    # Only the requested roots and their reachable dependencies enter the build.
    # The normal plugin validation reports missing dependencies and malformed manifests.
    graph: dict[Path, tuple[Path, ...]] = {}
    pending = list(reversed(roots))
    while pending:
        directory = pending.pop()
        if directory not in graph:
            dependencies = tuple(
                paths[name] for name in required.get(directory, ()) if name in paths
            )
            graph[directory] = dependencies
            pending.extend(reversed(dependencies))
    return graph


def dependency_closures(plugins_dir: Path, *roots: Path) -> dict[Path, tuple[Path, ...]]:
    """Each reachable plugin's transitive dependencies, with prerequisites first in both orders."""
    graph = _dependency_graph(plugins_dir, roots)
    closure: dict[Path, tuple[Path, ...]] = {}
    try:
        order = tuple(TopologicalSorter(graph).static_order())
    except CycleError as error:
        cycle = " -> ".join(str(path) for path in error.args[1])
        raise ValueError(f"Plugin dependencies form a cycle ({cycle}).") from error
    for directory in order:
        closure[directory] = tuple(
            dict.fromkeys(dep for parent in graph[directory] for dep in (*closure[parent], parent))
        )
    return closure


def build_order(plugins_dir: Path, *roots: Path) -> tuple[Path, ...]:
    """The requested roots and all their available declared dependencies, dependencies first."""
    return tuple(dependency_closures(plugins_dir, *roots))
