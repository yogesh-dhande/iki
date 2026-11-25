"""The LIMS field and actions are installed only with the lab's connector."""

import json
from pathlib import Path

import pytest
from first_party_dependencies import PLUGINS, ROOT, build_order
from test_plugin_migrations import django, iki


@pytest.mark.parametrize("connector", [False, True], ids=["protein", "protein-with-connector"])
def test_protein_install_exposes_the_selected_fields_and_actions(
    tmp_path: Path, connector: bool
) -> None:
    plugins = list(build_order(PLUGINS / "protein"))
    if connector:
        plugins.append(ROOT / "examples/plugins/lims_connector")
    output = tmp_path / "build"
    built = iki("plugin", "build", *plugins, "--output", output, cwd=ROOT)
    assert built.returncode == 0, built.stdout + built.stderr

    # Read the effective type and action catalog the running server exposes.
    catalog = django(
        output,
        "postgresql://localhost/unused",
        "-c",
        """
import django, json
django.setup()
from iki_server.entities import registry, values
from iki_server.runs import catalog
fields = values.shape("iki/protein.Protein").fields()
actions = catalog.actions()
print(json.dumps({
    "fields": sorted(fields),
    "actions": sorted(actions),
    "plugins": sorted(registry.plugins()),
}))
""",
    )
    assert catalog.returncode == 0, catalog.stdout + catalog.stderr
    installation = json.loads(catalog.stdout)
    assert "sequence" in installation["fields"]
    assert ("lims_id" in installation["fields"]) == connector
    assert ("acme/lims_connector" in installation["plugins"]) == connector
    lims_actions = {
        a for a in installation["actions"] if a.endswith((".import_from_lims", ".send_to_lims"))
    }
    assert lims_actions == (
        {"acme/lims_connector.import_from_lims", "acme/lims_connector.send_to_lims"}
        if connector
        else set()
    )
