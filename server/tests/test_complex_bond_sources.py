"""A complex's bonds say where they come from (ADR 0267).

Bonds `assemble` places by an antibody's format, subclass and numbering are a pattern,
not read from the molecule. Curated bonds (a database entry, a structure) and observed
ones (a non-reduced peptide map) name their source, on an antibody and on any protein
complex, at every save.
"""

import pytest
from first_party_dependencies import FIRST_PARTY, PLUGINS

from iki_spec import builtin, rules
from iki_spec.action_tests import fields_of
from iki_spec.model import matches_format
from iki_spec.validate import load_plugin

RULE = "stated_bonds_name_their_source"


def test_the_bonds_basis_is_a_registry_fact_of_every_complex() -> None:
    complex_ = builtin.core_plugin().types["iki/core.Complex"]
    basis, source = complex_.fields["bonds_from"], complex_.fields["bonds_source"]
    assert tuple(basis.values or ()) == ("pattern", "curated", "observed")
    assert basis.registry == source.registry == "fact"
    assert source.format == "uri"
    # KEGG DRUG's trastuzumab entry, whose disulfides assemble's fixtures check, is one.
    assert matches_format("https://www.kegg.jp/entry/D03257", "uri")
    assert not matches_format("KEGG D03257", "uri")


@pytest.mark.parametrize(
    ("plugin", "name"),
    [("antibody", "iki/antibody.Antibody"), ("complex", "iki/complex.ProteinComplex")],
)
def test_curated_and_observed_bonds_name_their_source(plugin: str, name: str) -> None:
    directory = PLUGINS / plugin
    loaded = load_plugin(directory, dependencies=FIRST_PARTY[directory])
    assert loaded.plugin is not None, loaded.problems
    spec = loaded.plugin.types[name]
    # Its own fields and iki/core.Complex's, where the bonds are.
    types = {**builtin.core_plugin().types, **loaded.plugin.types}
    schema = rules.rule_variables(rules.record_schema(spec.label, fields_of(spec, types), None))
    rule = spec.rules[RULE]
    assert rule.check is not None and rule.when is not None
    kegg = "https://www.kegg.jp/entry/D03257"
    cases = [
        ({"bonds_from": "pattern", "bonds_source": None}, True),
        ({"bonds_from": None, "bonds_source": None}, True),
        ({"bonds_from": "curated", "bonds_source": None}, False),
        ({"bonds_from": "curated", "bonds_source": kegg}, True),
        ({"bonds_from": "observed", "bonds_source": None}, False),
        ({"bonds_from": "observed", "bonds_source": "doi:10.0000/example"}, True),
    ]
    for record, holds in cases:
        sources = [(rule.check, rules.CONDITION), (rule.when, rules.CONDITION)]
        (outcomes,) = rules.evaluate(schema, sources, [{"record": record}])
        assert (outcomes[1].value is not True or outcomes[0].value is True) is holds, record
