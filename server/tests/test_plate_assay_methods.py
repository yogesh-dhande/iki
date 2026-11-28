"""A lab adds its own, locally qualified dose-response method from its own plugin (ADR 0248).

Its results are plate assay results like 4PL-OLS's: the same review procedure, signatures
and release evidence apply to them. Only the method, recorded in `fit_version`, is the lab's.
"""

from pathlib import Path

from first_party_dependencies import FIRST_PARTY, PLUGINS

from iki_spec.validate import load_plugin

LOCAL_FIT = Path(__file__).parent / "data" / "local_fit_plugin"
PLATE = PLUGINS / "plate_assays"


def test_a_lab_plugin_records_its_own_fit_as_a_plate_assay_result() -> None:
    loaded = load_plugin(LOCAL_FIT, dependencies=[*FIRST_PARTY[PLATE], PLATE])
    assert loaded.problems == [], loaded.problems
    assert loaded.plugin is not None
    action = loaded.plugin.actions["fit_5pl"]
    assert {create.type for create in action.creates} == {"iki/plate_assays.AssayResult"}
