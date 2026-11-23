"""The real plugins' flows that the web app's flow diagram tests read
(web/app/src/flow-fixtures.json) are what Iki serves for them (ADR 0198).

The diagram's tests draw example_lims' analysis results, samples and requests, lots and
plate assays' results, so their states, roles, signatures, guards and transitions are the
plugins' own, not a hand-copied likeness. When a plugin changes them, regenerate the file:

    uv run python server/tests/test_flow_fixtures.py
"""

import json
import sys
from pathlib import Path
from typing import Any

import django

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "web" / "app" / "src" / "flow-fixtures.json"

#: The plugins, and the types of each that the tests draw.
DRAWN = {
    "example_lims": ("AnalysisResult", "Sample", "AnalysisRequest"),
    "lots": ("Lot",),
    "plate_assays": ("ResearchAssayResult", "QCAssayResult"),
}


def served() -> dict[str, Any]:
    """Each drawn type's flow as the API serves it, and its plugins' lab roles'
    labels, by role."""
    from first_party_dependencies import FIRST_PARTY, PLUGINS

    from iki_server.api.entities import _flow_field
    from iki_spec.validate import load_plugin

    types: list[dict[str, Any]] = []
    roles: dict[str, str] = {}
    for plugin, names in DRAWN.items():
        directory = PLUGINS / plugin
        loaded = load_plugin(directory, dependencies=list(FIRST_PARTY[directory]))
        assert loaded.problems == [], loaded.problems
        assert loaded.plugin is not None
        for role in loaded.plugin.roles.values():
            roles[role.qualified_name] = role.label
        for name in names:
            spec = loaded.plugin.types[f"iki/{plugin}.{name}"]
            types.append(
                {
                    "type": spec.qualified_name,
                    "label": spec.label,
                    "fields": [
                        _flow_field(field)
                        for field in spec.fields.values()
                        if field.type == "state" and not field.removed
                    ],
                }
            )
    return {"roles": dict(sorted(roles.items())), "types": types}


def test_the_web_apps_flow_fixtures_are_the_plugins_own() -> None:
    assert json.loads(FIXTURES.read_text()) == served(), (
        "Regenerate them: uv run python server/tests/test_flow_fixtures.py"
    )


if __name__ == "__main__":
    import os

    sys.path.insert(0, str(Path(__file__).parent))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "iki_test_settings")
    django.setup()
    FIXTURES.write_text(json.dumps(served(), indent=2, ensure_ascii=False) + "\n")
