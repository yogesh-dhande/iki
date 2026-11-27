"""A type's effective declaration (ADR 0282) and refinements that narrow (ADR 0283)."""

import json
import re
import shutil
import textwrap
from pathlib import Path

from iki_spec import naming
from iki_spec.effective import (
    POLICIES,
    POLICY_TEXT,
    Effective,
    Policy,
    effective_from_dict,
    effective_to_dict,
    is_a,
    lineage,
    resolve,
    resolve_one,
)
from iki_spec.model import BUILTIN_TYPES, EntityType
from iki_spec.schemas import load
from iki_spec.validate import Result, load_plugin

PLUGIN = """\
apiVersion: iki/v1
kind: Plugin
name: acme/lab
version: 1.0.0
roles:
  analyst: { label: Analyst }
  qa: { label: QA }
"""

# A lot, a protein lot under it, and a lab's GMP protein lot under that: the shape of
# `iki/lots` with a lab's subtype, which every block's policy is read through.
LOT = """\
apiVersion: iki/v1
kind: EntityType
name: Lot
abstract: true
schema_version: 1
fields:
  name: { type: string, required: true }
  code: { type: string, unique: true, indexed: true, max_length: 40 }
  grade: { type: enum, values: [research, preclinical, gmp], default: research }
  purity: { type: float, min: 0, max: 100 }
  material: { type: ref, to: iki/core.Entity }
  made_on: { type: date }
  expiry: { type: date }
display: { title: "{{ name }}" }
storage: { barcode: code }
amount: { unit: mg }
sample: true
rules:
  expiry_after_made_on:
    check: record.expiry == null || record.made_on == null || record.expiry > record.made_on
    message: A lot expires after it's made.
draw_purposes:
  qa_sampling: { by: [analyst, qa] }
draw_guards:
  unexpired:
    check: record.expiry == null || record.expiry >= today
    message: The lot has expired.
notifications:
  expiring:
    label: A lot is expiring
    when: { field: expiry }
    tell: { creator: true }
"""

PROTEIN_LOT = """\
apiVersion: iki/v1
kind: EntityType
name: ProteinLot
extends: Lot
schema_version: 1
hrid: { prefix: PRL }
fields:
  concentration: { type: float, min: 0 }
refines:
  material: { to: Protein }
display: { title: "{{ name }} ({{ concentration }})" }
rules:
  concentrated:
    check: record.concentration == null || record.concentration > 0
    message: A protein lot has a concentration.
"""

GMP_LOT = """\
apiVersion: iki/v1
kind: EntityType
name: GmpProteinLot
extends: ProteinLot
schema_version: 1
hrid: { prefix: GPL }
fields:
  batch_record: { type: string }
refines:
  material: { required: true }
  grade: { values: [gmp], default: gmp }
  purity: { min: 95 }
draw_purposes:
  qa_sampling: { by: [qa] }
"""

PROTEIN = """\
apiVersion: iki/v1
kind: EntityType
name: Protein
schema_version: 1
hrid: { prefix: PRT }
identity: [sequence]
fields:
  sequence: { type: string, required: true, immutable: true }
"""


def make(tmp_path: Path, files: dict[str, str]) -> Result:
    root = tmp_path / "acme"
    shutil.rmtree(root, ignore_errors=True)
    (root / "types").mkdir(parents=True)
    (root / "plugin.yaml").write_text(PLUGIN)
    for name, text in files.items():
        (root / "types" / name).write_text(textwrap.dedent(text))
    return load_plugin(root)


def lab(tmp_path: Path, gmp: str = GMP_LOT, protein_lot: str = PROTEIN_LOT) -> Result:
    return make(
        tmp_path,
        {
            "lot.yaml": LOT,
            "protein_lot.yaml": protein_lot,
            "gmp_lot.yaml": gmp,
            "protein.yaml": PROTEIN,
        },
    )


def types_of(result: Result) -> dict[str, EntityType]:
    assert result.plugin is not None, [(p.code, p.message) for p in result.problems]
    return {**BUILTIN_TYPES, **result.plugin.types}


def codes(result: Result) -> list[str]:
    return [p.code for p in result.problems]


def test_every_key_of_a_type_has_one_policy_the_schema_says() -> None:
    """ADR 0282: the policy table is the contract of `extends`, published in the schema's
    description of each key, so a new key can't be added without one."""
    properties = load("entity-type")["properties"]
    blocks = {k for k in properties if k not in ("apiVersion", "kind")}
    assert blocks == set(POLICIES)
    for key in blocks:
        policy = POLICIES[key]
        found = re.search(r"Inheritance: `([a-z]+)`\. ", properties[key]["description"])
        assert found is not None, key
        assert found.group(1) == policy.value, key
        assert POLICY_TEXT[policy] in properties[key]["description"], key
    assert set(POLICY_TEXT) == set(Policy)


def test_a_lab_subtype_has_every_block_by_its_policy(tmp_path: Path) -> None:
    """ADR 0282: nearest, once, accumulate, narrow and merge, each as the table says, and
    whose declaration each inherited part is."""
    result = lab(tmp_path)
    assert codes(result) == [], [(p.code, p.message) for p in result.problems]
    types = types_of(result)
    gmp = resolve_one(types, "acme/lab.GmpProteinLot")
    assert gmp.lineage == (
        "acme/lab.GmpProteinLot",
        "acme/lab.ProteinLot",
        "acme/lab.Lot",
        naming.ENTITY,
    )
    assert gmp.chain == gmp.lineage[:-1]
    assert gmp.is_a("acme/lab.Lot") and gmp.is_a(naming.ENTITY)
    assert not gmp.is_a("acme/lab.Protein")
    # Fields accumulate, the ancestors' first, each from the type whose table holds it.
    assert list(gmp.fields) == [
        "name",
        "code",
        "grade",
        "purity",
        "material",
        "made_on",
        "expiry",
        "concentration",
        "batch_record",
    ]
    assert gmp.declared_by("material") == "acme/lab.Lot"
    assert gmp.declared_by("concentration") == "acme/lab.ProteinLot"
    assert list(gmp.own_fields("acme/lab.ProteinLot")) == ["concentration"]
    # ...narrowed by every type below the one declaring them, each within the one above.
    material = gmp.fields["material"]
    assert (material.required, material.to) == (True, ("acme/lab.Protein",))
    assert gmp.sources["refines.material"] == "acme/lab.GmpProteinLot"
    assert (gmp.fields["grade"].values, gmp.fields["grade"].default) == (("gmp",), "gmp")
    assert (gmp.fields["purity"].min, gmp.fields["purity"].max) == (95, 100)
    # The protein lot's own records keep what it narrows, and no more.
    protein_lot = resolve_one(types, "acme/lab.ProteinLot")
    assert protein_lot.fields["material"].required is False
    assert protein_lot.fields["material"].to == ("acme/lab.Protein",)
    assert protein_lot.fields["grade"].values == ("research", "preclinical", "gmp")
    # Nearest: the protein lot's title, the lot's storage.
    assert gmp.display_title == "{{ name }} ({{ concentration }})"
    assert gmp.sources["display.title"] == "acme/lab.ProteinLot"
    assert gmp.storage is not None and gmp.sources["storage"] == "acme/lab.Lot"
    # Accumulate: rules, notifications and draw guards, each named once; a sample's subtype
    # is a sample.
    assert list(gmp.rules) == ["expiry_after_made_on", "concentrated"]
    assert gmp.sources["rules.concentrated"] == "acme/lab.ProteinLot"
    assert list(gmp.notifications) == ["expiring"]
    assert list(gmp.draw_guards) == ["unexpired"]
    assert gmp.sample and gmp.sources["sample"] == "acme/lab.Lot"
    # Narrow: a purpose's roles, the nearest's.
    assert gmp.draw_purposes == {"qa_sampling": ("acme/lab.qa",)}
    assert protein_lot.draw_purposes == {"qa_sampling": ("acme/lab.analyst", "acme/lab.qa")}
    # Once: the protein's identity is its own, and nobody else's.
    protein = resolve_one(types, "acme/lab.Protein")
    assert (protein.identity_owner, protein.identity) == ("acme/lab.Protein", ("sequence",))
    assert gmp.identity_owner is None


def test_a_subtype_adds_service_fields_to_its_requests(tmp_path: Path) -> None:
    """ADR 0282, 0232: `request` merges: a subtype adds service fields of its own."""
    request = """\
apiVersion: iki/v1
kind: EntityType
name: Ask
abstract: true
schema_version: 1
fields:
  question: { type: text }
  answer: { type: text }
request: { service_fields: [answer] }
"""
    ranked = """\
apiVersion: iki/v1
kind: EntityType
name: RankedAsk
extends: Ask
schema_version: 1
hrid: { prefix: RNK }
fields:
  rank: { type: int }
request: { service_fields: [rank] }
"""
    types = types_of(make(tmp_path, {"ask.yaml": request, "ranked.yaml": ranked}))
    found = resolve_one(types, "acme/lab.RankedAsk")
    assert found.request is not None
    assert found.request.service_fields == ("answer", "rank")
    assert found.sources["request"] == "acme/lab.Ask"
    assert found.sources["request.service_fields.rank"] == "acme/lab.RankedAsk"


def test_it_survives_the_manifest(tmp_path: Path) -> None:
    """ADR 0282: what the build writes into manifest.json reads back to the same."""
    types = types_of(lab(tmp_path))
    every = resolve(types)
    assert set(every) == set(types)
    for name, declared in every.items():
        text = json.dumps(effective_to_dict(declared))
        assert effective_from_dict(name, json.loads(text)) == declared, name
    # Empty blocks are left out.
    assert set(effective_to_dict(every[naming.ENTITY])) == {"lineage"}


def test_lineage_stops_at_what_isnt_there_or_loops() -> None:
    """The validator resolves types it hasn't passed yet: a lineage then stops at a type
    that's missing, or one already in it, rather than raising or looping."""
    a = EntityType(plugin="acme/lab", name="A", schema_version=1, extends="acme/lab.B")
    b = EntityType(plugin="acme/lab", name="B", schema_version=1, extends="acme/lab.A")
    c = EntityType(plugin="acme/lab", name="C", schema_version=1, extends="acme/lab.Gone")
    types = {t.qualified_name: t for t in (a, b, c)}
    assert lineage(types, "acme/lab.A") == ("acme/lab.A", "acme/lab.B", naming.ENTITY)
    assert lineage(types, "acme/lab.C") == ("acme/lab.C", "acme/lab.Gone", naming.ENTITY)
    assert lineage(types, naming.ENTITY) == (naming.ENTITY,)
    assert is_a(types, "acme/lab.A", "acme/lab.B")
    assert not is_a(types, "acme/lab.Gone", naming.ENTITY)
    assert isinstance(resolve_one(types, "acme/lab.C"), Effective)


def test_a_refinement_narrows_and_never_widens(tmp_path: Path) -> None:
    """ADR 0283: each part of a refinement is one the field's kind has, within what its
    parent's records take."""

    def problems(old: str, new: str, *, at: str = GMP_LOT) -> list[str]:
        assert old in at, old
        return codes(lab(tmp_path, gmp=at.replace(old, new)))

    assert sorted(
        problems("grade: { values: [gmp], default: gmp }", "grade: { values: [gxp] }")
    ) == ["refines-default-outside", "refines-wider"]
    # Narrowing an enum past its parent's default needs a default of its own.
    assert problems("grade: { values: [gmp], default: gmp }", "grade: { values: [gmp] }") == [
        "refines-default-outside"
    ]
    assert problems(
        "grade: { values: [gmp], default: gmp }", "grade: { values: [gmp], default: research }"
    ) == ["invalid-default"]
    assert problems("purity: { min: 95 }", "purity: { min: -1 }") == ["refines-wider"]
    assert problems("purity: { min: 95 }", "purity: { max: 101 }") == ["refines-wider"]
    assert problems("purity: { min: 95 }", "purity: { min: 99, max: 98 }") == ["min-above-max"]
    assert problems("purity: { min: 95 }", "name: { min: 1 }") == ["refines-not-a-number"]
    assert problems("purity: { min: 95 }", "name: { values: [a] }") == ["refines-not-enum"]
    assert problems("purity: { min: 95 }", "made_on: { default: 1 }") == ["invalid-default"]
    assert problems("material: { required: true }", "material: { default: x }") == [
        "refines-default"
    ]
    assert problems("purity: { min: 95 }", "potency: { required: true }") == [
        "refines-unknown-field"
    ]
    assert problems("purity: { min: 95 }", "batch_record: { required: true }") == [
        "refines-own-field"
    ]
    # Each within the one above it: the protein lot's type, and the lot's.
    assert problems("material: { required: true }", "material: { to: Lot }") == ["refines-wider"]
    # Required can't be taken back.
    assert problems("material: { required: true }", "material: { required: false }") == ["schema"]
    # A subtype of the GMP lot is held within its 95% purity, not the lot's 0.
    below = textwrap.dedent("""\
        apiVersion: iki/v1
        kind: EntityType
        name: ClinicalLot
        extends: GmpProteinLot
        schema_version: 1
        hrid: { prefix: CLN }
        refines:
          purity: { min: 90 }
        """)
    files = {
        "lot.yaml": LOT,
        "protein_lot.yaml": PROTEIN_LOT,
        "gmp_lot.yaml": GMP_LOT,
        "protein.yaml": PROTEIN,
    }
    assert codes(make(tmp_path, {**files, "clinical.yaml": below})) == ["refines-wider"]
    within = make(tmp_path, {**files, "clinical.yaml": below.replace("min: 90", "min: 99")})
    assert codes(within) == []
    clinical = resolve_one(types_of(within), "acme/lab.ClinicalLot")
    assert (clinical.fields["purity"].min, clinical.fields["grade"].values) == (99, ("gmp",))
    assert clinical.sources["refines.purity"] == "acme/lab.ClinicalLot"


def test_a_computed_field_cant_be_required_below(tmp_path: Path) -> None:
    computed = LOT.replace("  made_on: { type: date }", "  made_on: { type: date, computed: true }")
    result = make(
        tmp_path,
        {
            "lot.yaml": computed,
            "protein_lot.yaml": PROTEIN_LOT,
            "gmp_lot.yaml": GMP_LOT.replace(
                "  purity: { min: 95 }", "  made_on: { required: true }"
            ),
            "protein.yaml": PROTEIN,
        },
    )
    assert codes(result) == ["refines-computed"]
