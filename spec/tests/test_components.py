"""Components: a plugin's Web Components, their modules and where they appear (ADR 0084)."""

import hashlib
import textwrap
from pathlib import Path

from iki_spec.components import MAX_MODULE_BYTES
from iki_spec.model import ComponentSlot, plugin_from_dict, plugin_to_dict
from iki_spec.validate import Result, load_plugin

PLUGIN = """\
apiVersion: iki/v1
kind: Plugin
name: acme/lab
version: 1.0.0
"""

TYPES = """\
apiVersion: iki/v1
kind: EntityType
name: Assay
schema_version: 1
hrid: { prefix: ASY }
---
apiVersion: iki/v1
kind: EntityType
name: Readout
schema_version: 1
hrid: { prefix: RDO }
"""

MODULE = "customElements.define('acme-assay-plot', class extends HTMLElement {});\n"


def component(**changes: str) -> str:
    body = {
        "name": "assay_plot",
        "label": "Assay plot",
        "module": "./components/assay-plot.js",
        "element": "acme-assay-plot",
        "slots": "[{ slot: entity.tab, type: Assay }]",
        **changes,
    }
    lines = ["apiVersion: iki/v1", "kind: Component"]
    lines += [f"{k}: {v}" for k, v in body.items() if v != ""]
    return "\n".join(lines) + "\n"


def made(tmp_path: Path, manifest: str, **files: str) -> Result:
    root = tmp_path / "acme"
    every = {
        "plugin.yaml": PLUGIN,
        "types/types.yaml": TYPES,
        "components/assay-plot.yaml": manifest,
        "components/assay-plot.js": MODULE,
        **files,
    }
    for name, text in every.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        data = text.encode("utf-8", "surrogateescape")
        (root / name).write_bytes(textwrap.dedent(text).encode() if name.endswith("yaml") else data)
    return load_plugin(root)


def codes(result: Result) -> list[str]:
    return [p.code for p in result.problems]


def only(result: Result, code: str) -> str:
    assert codes(result) == [code], [(p.code, p.message) for p in result.problems]
    return result.problems[0].message


def test_a_component_is_read_with_its_modules_digest(tmp_path: Path) -> None:
    events = "{ select: { type: Readout, description: The readouts picked. } }"
    result = made(tmp_path, component(events=events))
    assert result.problems == []
    plugin = result.plugin
    assert plugin is not None
    (found,) = plugin.components.values()
    assert found.qualified_name == "acme/lab.assay_plot"
    assert found.slots == (
        ComponentSlot(slot="entity.tab", type="acme/lab.Assay", label="Assay plot"),
    )
    assert found.selects == "acme/lab.Readout"
    assert found.sha256 == hashlib.sha256(MODULE.encode()).hexdigest()
    assert plugin_from_dict(plugin_to_dict(plugin)) == plugin


def test_a_slot_can_name_its_tab(tmp_path: Path) -> None:
    slots = "[{ slot: entity.tab, type: Assay, label: Plot }]"
    plugin = made(tmp_path, component(slots=slots)).plugin
    assert plugin is not None
    (found,) = plugin.components.values()
    assert found.slots[0].label == "Plot"


def test_the_types_it_names_must_exist(tmp_path: Path) -> None:
    result = made(tmp_path, component(slots="[{ slot: entity.tab, type: Asay }]"))
    assert only(result, "unknown-type") == "This plugin has no type `Asay`."
    selects = component(events="{ select: { type: Nothing } }")
    assert codes(made(tmp_path, selects)) == ["unknown-type"]


def test_the_module_is_one_text_file_in_the_plugin(tmp_path: Path) -> None:
    missing = made(tmp_path, component(module="./components/gone.js"))
    assert codes(missing) == ["missing-file"]
    binary = made(tmp_path, component(), **{"components/assay-plot.js": "\udcff"})
    assert codes(binary) == ["unreadable-file"]
    # Only ./ paths to .js or .mjs files, never out of the plugin.
    for module in ("components/x.js", "./../x.js", "./components/x.wasm"):
        assert codes(made(tmp_path, component(module=module))) == ["schema"]


def test_a_symlinked_module_is_refused(tmp_path: Path) -> None:
    result = made(tmp_path, component())
    assert result.problems == []
    root = tmp_path / "acme"
    (root / "elsewhere.js").write_text(MODULE)
    module = root / "components" / "assay-plot.js"
    module.unlink()
    module.symlink_to(root / "elsewhere.js")
    assert [p.code for p in load_plugin(root).problems] == ["symlink"]


def test_a_module_over_ten_mebibytes_is_refused(tmp_path: Path) -> None:
    big = "x" * (MAX_MODULE_BYTES + 1)
    result = made(tmp_path, component(), **{"components/assay-plot.js": big})
    assert "more than the 10 MiB" in only(result, "component-too-big")


def test_elements_are_custom_element_names_each_defined_once(tmp_path: Path) -> None:
    assert codes(made(tmp_path, component(element="plot"))) == ["schema"]
    other = component(name="other_plot")
    result = made(tmp_path, component(), **{"components/other.yaml": other})
    assert only(result, "element-twice") == "`assay_plot` defines `acme-assay-plot` too."
