"""Generate the docs site's file format reference from Iki's shipped plugin manifests."""

from __future__ import annotations

import argparse
import html
import sys
from collections.abc import Iterable
from pathlib import Path

from iki_spec.builtin import core_plugin
from iki_spec.first_party_dependencies import dependency_closures
from iki_spec.formats import FormatRegistry
from iki_spec.validate import load_plugin

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / "docs/site/src/content/reference/file-formats.mdx"


def shipped_formats() -> FormatRegistry:
    plugins = [core_plugin()]
    first_party = ROOT / "plugins"
    paths = sorted(manifest.parent for manifest in first_party.glob("*/plugin.yaml"))
    for directory, dependencies in dependency_closures(first_party, *paths).items():
        result = load_plugin(directory, dependencies=dependencies)
        if result.plugin is None:
            errors = "\n".join(f"{p.file}: {p.message}" for p in result.errors)
            raise ValueError(f"Cannot document {directory.name}:\n{errors}")
        plugins.append(result.plugin)
    return FormatRegistry(plugins)


def _codes(values: Iterable[str]) -> str:
    return ", ".join(f"`{value}`" for value in values)


def reference(registry: FormatRegistry) -> str:
    lines = [
        "---",
        "title: File formats",
        "description: Extensions, media types, readers and viewers for the formats Iki ships.",
        "---",
        "",
        "{/* Generated from plugin manifests by make core. */}",
        "",
        "Your deployment's `GET /api/v1/formats` lists the formats and actions its installed",
        "plugins provide. Signatures below give hexadecimal bytes and their offsets from the",
        "start of a file. Actions appear under each format their declared file inputs accept.",
    ]
    for fmt in sorted(registry.formats.values(), key=lambda f: (f.label.casefold(), f.id)):
        # A label is plain text even in MDX, where braces introduce JavaScript.
        label = (
            html.escape(" ".join(fmt.label.split())).replace("{", "&#123;").replace("}", "&#125;")
        )
        lines.extend(["", f"## {label}", "", f"- Format ID `{fmt.id}`"])
        lines.append(f"- Canonical media type `{fmt.canonical}`")
        if aliases := fmt.media_types[1:]:
            lines.append(f"- Media type aliases {_codes(aliases)}")
        if fmt.extensions:
            lines.append(f"- Extensions {_codes(fmt.extensions)}")
        if fmt.signatures:
            signatures = (f"`{s.hex}` at byte {s.offset}" for s in fmt.signatures)
            lines.append(f"- Byte signatures {', '.join(signatures)}")
        if fmt.reader:
            lines.append(f"- Metadata reader `{fmt.reader}`")
        if fmt.viewer:
            lines.append(f"- Viewer `{fmt.viewer}`")
        if parsers := registry.parsers(fmt.id):
            lines.append(f"- Actions {_codes(parsers)}")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="check the committed reference page")
    args = parser.parse_args()
    generated = reference(shipped_formats())
    if args.check:
        if REFERENCE.is_file() and REFERENCE.read_text(encoding="utf-8") == generated:
            return 0
        sys.stderr.write("Run `make core` to update the file format reference.\n")
        return 1
    REFERENCE.parent.mkdir(parents=True, exist_ok=True)
    REFERENCE.write_text(generated, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
