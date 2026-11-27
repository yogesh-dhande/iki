"""Components: a plugin's Web Components, their modules and where they appear (ADR 0084)."""

import hashlib
import textwrap
from pathlib import Path

from iki_spec.components import MAX_MODULE_BYTES
from iki_spec.model import (
    ComponentEntities,
    ComponentSearch,
    ComponentSlot,
    plugin_from_dict,
    plugin_to_dict,
)
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
fields:
  assay: { type: ref, to: Assay }
  assays: { type: list, items: { type: ref, to: Assay } }
  well: { type: string, max_length: 4 }
  signal: { type: float }
  notes: { type: text }
  # The reader's raw export: machine text, without word search (ADR 0233).
  export: { type: text, searchable: false }
  read_at: { type: datetime }
  probe: { type: string, format: dna_sequence, max_length: 60 }
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


# --- Many entities (ADR 0117) ---------------------------------------------------------------

ENTITIES = "{ type: Readout, fields: [well, signal, assay] }"
EVERYWHERE = """
  - slot: entity.tab
    type: Assay
    label: Plate
    search: { where: { field: assay, op: eq, value: "{{ entity }}" }, sort: well }
  - { slot: search.results, label: Plate map }
  - { slot: project.page, label: Readouts, search: { sort: read_at, descending: true } }
  - slot: person.home
    label: Your readouts
    search:
      where:
        all:
          - { field: created_by, op: eq, value: "{{ me }}" }
          - { field: signal, op: gt, value: 0.5 }"""


def many(slots: str = EVERYWHERE, entities: str = ENTITIES) -> str:
    return component(entities=entities, slots=slots)


def test_a_component_can_show_many_entities_in_every_place(tmp_path: Path) -> None:
    result = made(tmp_path, many())
    assert result.problems == []
    assert result.plugin is not None
    (found,) = result.plugin.components.values()
    assert found.entities == ComponentEntities(
        type="acme/lab.Readout", fields=("well", "signal", "assay"), limit=500
    )
    tab, grid, project, home = found.slots
    assert tab == ComponentSlot(
        slot="entity.tab",
        type="acme/lab.Assay",
        label="Plate",
        search=ComponentSearch(
            where={"field": "assay", "op": "eq", "value": "{{ entity }}"}, sort="well"
        ),
    )
    # The other places show the component's own entities.
    assert grid == ComponentSlot(slot="search.results", type="acme/lab.Readout", label="Plate map")
    assert project.type == home.type == "acme/lab.Readout"
    assert project.search == ComponentSearch(sort="read_at", descending=True)
    assert result.plugin == plugin_from_dict(plugin_to_dict(result.plugin))


def test_a_plugin_built_before_many_entities_reads_back(tmp_path: Path) -> None:
    plugin = made(tmp_path, component()).plugin
    assert plugin is not None
    built = plugin_to_dict(plugin)
    # What a build made before ADR 0117 wrote.
    (old,) = built["components"].values()
    del old["entities"]
    for slot in old["slots"]:
        del slot["search"]
    assert plugin_from_dict(built) == plugin


def test_a_components_search_can_filter_on_a_state(tmp_path: Path) -> None:
    types = (
        TYPES
        + """\
  review:
    type: state
    initial: recorded
    states:
      recorded: { label: Recorded }
      signed: { label: Signed, set_by: [reviewer] }
      released: { label: Released, set_by: [reviewer] }
"""
    )
    slots = """
  - slot: entity.tab
    type: Assay
    search:
      where:
        all:
          - {field: assay, op: eq, value: "{{ entity }}"}
          - {field: review, op: in, value: [signed, released]}
"""
    manifest = many(slots=slots, entities="{ type: Readout, fields: [review] }")
    files = {
        "plugin.yaml": PLUGIN + "roles: {reviewer: {label: Reviewer}}\n",
        "types/types.yaml": types,
    }
    result = made(tmp_path, manifest, **files)
    assert result.problems == []


def test_the_places_that_show_many_need_entities(tmp_path: Path) -> None:
    for slots in (
        "[{ slot: search.results }]",
        "[{ slot: project.page, search: {} }]",
        "[{ slot: entity.tab, type: Assay, search: { sort: well } }]",
    ):
        assert codes(made(tmp_path, component(slots=slots))) == ["slot-needs-entities"]


def test_each_place_has_its_own_shape(tmp_path: Path) -> None:
    # A tab names its type; the others show the component's; sections say what they find.
    for slots in (
        "[{ slot: entity.tab }]",
        "[{ slot: search.results, type: Readout }]",
        "[{ slot: search.results, search: {} }]",
        "[{ slot: project.page }]",
        "[{ slot: person.home }]",
        "[{ slot: elsewhere, type: Assay }]",
    ):
        assert set(codes(made(tmp_path, many(slots=slots)))) == {"schema"}, slots
    for entities in (
        "{ type: Readout }",
        "{ type: Readout, fields: [] }",
        "{ type: Readout, fields: [well], limit: 1001 }",
    ):
        assert codes(made(tmp_path, many(entities=entities))) == ["schema"], entities


def test_its_fields_are_its_types(tmp_path: Path) -> None:
    result = made(tmp_path, many(entities="{ type: Readout, fields: [well, colour] }"))
    assert only(result, "unknown-field") == "`acme/lab.Readout` has no field `colour`."
    assert codes(made(tmp_path, many(entities="{ type: Reading, fields: [well] }"))) == [
        "unknown-type"
    ]


def test_each_place_is_given_once(tmp_path: Path) -> None:
    twice = "[{ slot: search.results }, { slot: search.results, label: Again }]"
    assert only(made(tmp_path, many(slots=twice)), "slot-twice") == (
        "Slot 0 is `search.results` too."
    )
    tabs = "[{ slot: entity.tab, type: Assay }, { slot: entity.tab, type: Readout }]"
    assert made(tmp_path, many(slots=tabs)).problems == []


def search(where: str, slot: str = "project.page") -> str:
    return many(slots=f"[{{ slot: {slot}, search: {{ where: {where} }} }}]")


def test_a_search_finds_a_sequence_by_a_motif_and_only_a_sequence(tmp_path: Path) -> None:
    """A string field with a sequence format takes `motif` (ADR 0175); others don't."""
    assert made(tmp_path, search("{ field: probe, op: motif, value: GAATTY }")).problems == []
    wrong = made(tmp_path, search("{ field: well, op: motif, value: GAATTC }"))
    assert only(wrong, "unknown-operator") == (
        "`well` takes `is_set`, `eq`, `ne`, `in`, `contains`, `starts_with`, not `motif`."
    )


def test_a_search_names_the_types_fields_with_their_operators(tmp_path: Path) -> None:
    assert made(tmp_path, search("{ field: notes, op: matches, value: edge }")).problems == []
    assert made(tmp_path, search("{ field: hrid, op: starts_with, value: RDO }")).problems == []
    wrong = made(tmp_path, search("{ field: notes, op: eq, value: edge }"))
    assert only(wrong, "unknown-operator") == (
        "`notes` takes `is_set`, `contains`, `matches`, not `eq`."
    )
    assert codes(made(tmp_path, search("{ field: colour, op: eq, value: red }"))) == [
        "unknown-field"
    ]
    # Text that isn't searchable has no words to match, only text it contains.
    assert made(tmp_path, search("{ field: export, op: contains, value: OD600 }")).problems == []
    wrong = made(tmp_path, search("{ field: export, op: matches, value: OD600 }"))
    assert only(wrong, "unknown-operator") == (
        "`export` takes `is_set`, `contains`, not `matches`."
    )
    nested = (
        "{ any: [{ not: { field: well, op: eq, value: A1 } }, "
        "{ field: signal, op: lt, value: 2 }] }"
    )
    assert made(tmp_path, search(nested)).problems == []
    # Every entity's columns, but not those a component has no business with.
    assert codes(made(tmp_path, search("{ field: owner_project, op: eq, value: x }"))) == [
        "unknown-field"
    ]


RULED = (
    PLUGIN + "roles:\n  qa: { label: QA }\n",
    TYPES
    + """\
rules:
  signal_in_range:
    check: record.signal < 10.0
    message: The signal is out of range.
    hold: { reviewed_by: [qa] }
  well_named:
    check: record.well != "X"
    message: Name its well.
""",
)


def test_a_search_finds_what_rules_caught_by_the_rules_name(tmp_path: Path) -> None:
    plugin, types = RULED

    def ruled(where: str) -> Result:
        return made(tmp_path, search(where), **{"plugin.yaml": plugin, "types/types.yaml": types})

    assert ruled("{ field: held, op: is_set, value: true }").problems == []
    assert ruled("{ field: held, op: eq, value: signal_in_range }").problems == []
    assert ruled("{ field: breaks, op: in, value: [well_named] }").problems == []
    assert only(ruled("{ field: held, op: eq, value: well_named }"), "unknown-rule") == (
        "`acme/lab.Readout` has no rule `well_named` that holds its records for review."
    )
    assert only(ruled("{ field: breaks, op: eq, value: wel_named }"), "unknown-rule") == (
        "`acme/lab.Readout` has no rule `wel_named` that its records can break once saved."
    )
    assert only(ruled("{ field: held, op: lt, value: x }"), "unknown-operator") == (
        "`held` takes `is_set`, `eq`, `ne`, `in`, not `lt`."
    )


def test_a_search_leaves_room_for_a_refinement(tmp_path: Path) -> None:
    leaf = "{ field: well, op: eq, value: A1 }"
    deep = leaf
    for _ in range(3):
        deep = f"{{ not: {deep} }}"
    assert made(tmp_path, search(deep)).problems == []
    assert only(made(tmp_path, search(f"{{ not: {deep} }}")), "search-too-big") == (
        "A component's search may have at most 20 conditions, 4 levels deep: this one has 1, "
        "5 deep."
    )
    wide = f"{{ any: [{', '.join([leaf] * 10)}] }}"
    assert made(tmp_path, search(f"{{ all: [{wide}, {wide}] }}")).problems == []
    assert codes(made(tmp_path, search(f"{{ all: [{wide}, {wide}, {leaf}] }}"))) == [
        "search-too-big"
    ]


def test_a_search_sorts_by_what_sorts(tmp_path: Path) -> None:
    slots = "[{ slot: project.page, search: { sort: notes } }]"
    assert only(made(tmp_path, many(slots=slots)), "unsortable-field") == (
        "`notes` can't be sorted by."
    )


def test_the_entity_is_a_tabs_and_a_reference_to_its_type(tmp_path: Path) -> None:
    tab = "entity.tab, type: Assay"
    assert (
        made(tmp_path, search('{ field: assays, op: has, value: "{{ entity }}" }', tab)).problems
        == []
    )
    on_project = made(tmp_path, search('{ field: assay, op: eq, value: "{{ entity }}" }'))
    assert "only a tab has one, not `project.page`" in only(on_project, "context-misplaced")
    not_a_ref = made(tmp_path, search('{ field: well, op: eq, value: "{{ entity }}" }', tab))
    assert "compared with a reference, not `well`" in only(not_a_ref, "context-misplaced")
    other_type = made(
        tmp_path,
        search('{ field: assay, op: eq, value: "{{ entity }}" }', "entity.tab, type: Readout"),
    )
    assert "refers to `acme/lab.Assay`" in only(other_type, "context-misplaced")


def test_a_tab_on_every_record_names_the_record_it_shows_on(tmp_path: Path) -> None:
    """A tab on every record (ADR 0237) shows only where its search finds something for the
    record, so its search names it."""
    measured = {"types/types.yaml": TYPES + "  measured: { type: ref, to: iki/core.Entity }\n"}
    every = "entity.tab, type: iki/core.Entity"
    entities = "{ type: Readout, fields: [well, measured] }"
    found = search('{ field: measured, op: eq, value: "{{ entity }}" }', every)
    assert made(tmp_path, found.replace(ENTITIES, entities), **measured).problems == []
    unnamed = many(slots=f"[{{ slot: {every}, search: {{ sort: well }} }}]", entities=entities)
    assert "its search names it" in only(made(tmp_path, unnamed, **measured), "tab-on-every-record")
    bare = many(slots=f"[{{ slot: {every} }}]", entities=entities)
    assert "its search names it" in only(made(tmp_path, bare, **measured), "tab-on-every-record")


def test_me_is_a_person(tmp_path: Path) -> None:
    mine = made(tmp_path, search('{ field: created_by, op: eq, value: "{{ me }}" }'))
    assert mine.problems == []
    wrong = made(tmp_path, search('{ field: well, op: eq, value: "{{ me }}" }'))
    assert "compared with `created_by`, `assigned` or a field naming people, not `well`" in only(
        wrong, "context-misplaced"
    )
    for value in ('"{{ you }}"', '"{{ me }} and more"', '"{{ entity.hrid }}"'):
        found = made(tmp_path, search(f"{{ field: well, op: eq, value: {value} }}"))
        assert codes(found) == ["unknown-context"], value


def test_a_persons_home_is_theirs(tmp_path: Path) -> None:
    everything = "[{ slot: person.home, search: { sort: well } }]"
    assert "its search names them" in only(
        made(tmp_path, many(slots=everything)), "home-not-personal"
    )


# --- What it offers to run -----------------------------------------------------------

ACTIONS = """\
apiVersion: iki/v1
kind: Action
name: fit_curve
inputs:
  assay: { type: Assay, schema: ">=1" }
effects:
  creates: [{ type: Readout, derived_from: [assay] }]
runs_on: { tier: backend, environment: tools }
command: [python, ./run.py]
---
apiVersion: iki/v1
kind: Action
name: read_plate
inputs:
  readout: { type: Readout, schema: ">=1" }
effects:
  updates: [{ input: readout, fields: [signal] }]
runs_on: { tier: backend, environment: tools }
command: [python, ./run.py]
"""
ENVIRONMENT = """\
apiVersion: iki/v1
kind: Environment
name: tools
tiers: [backend]
python: { version: "3.12", requirements: requirements.txt }
"""


def offering(tmp_path: Path, events: str, **changes: str) -> Result:
    return made(
        tmp_path,
        component(events=events, **changes),
        **{
            "actions/actions.yaml": ACTIONS,
            "environments/tools.yaml": ENVIRONMENT,
            "requirements.txt": "numpy==2.1.0 --hash=sha256:" + "a" * 64 + "\n",
            "run.py": "",
        },
    )


def test_a_component_can_offer_its_plugins_actions_on_its_entity(tmp_path: Path) -> None:
    # What an empty tab needs: the action that fills it, which Iki's run page then runs,
    # with its confirmations, for whoever may run it.
    result = offering(tmp_path, "{ run: { actions: [fit_curve] } }")
    assert result.problems == [], [(p.code, p.message) for p in result.problems]
    assert result.plugin is not None
    (found,) = result.plugin.components.values()
    assert found.runs == ("acme/lab.fit_curve",)
    assert plugin_from_dict(plugin_to_dict(result.plugin)) == result.plugin


def test_it_offers_only_actions_of_its_own_that_take_its_entity(tmp_path: Path) -> None:
    unknown = offering(tmp_path, "{ run: { actions: [fit_curves] } }")
    assert only(unknown, "component-unknown-action") == "This plugin has no action `fit_curves`."
    # read_plate takes a readout, and the tab is an assay's.
    other = offering(tmp_path, "{ run: { actions: [read_plate] } }")
    assert only(other, "component-action-takes-no-entity") == (
        "`read_plate` takes no `acme/lab.Assay`, whose tab this is: none of its inputs does."
    )
    # And it's offered only where there's an entity to run it on: a tab.
    many = offering(
        tmp_path,
        "{ run: { actions: [fit_curve] } }",
        slots="[{ slot: project.page, search: {} }]",
        entities="{ type: Readout, fields: [well] }",
    )
    assert codes(many) == ["component-run-needs-a-tab"]


def test_a_plugin_built_before_offering_runs_reads_back(tmp_path: Path) -> None:
    plugin = offering(tmp_path, "{}").plugin
    assert plugin is not None
    built = plugin_to_dict(plugin)
    (old,) = built["components"].values()
    del old["runs"]
    assert plugin_from_dict(built) == plugin
