"""The lots plugin (ADR 0162): lots of any registered entity, each kind with its own
measurements, all extending one abstract Lot with its disposition, which QA sets; its
calculations, run as a worker runs them; and its lots kept in a database, with their
amounts, rules and states, beside the plugins whose entities they're lots of."""

import importlib.util
import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from first_party_dependencies import FIRST_PARTY, PLUGINS, ROOT, build_order
from test_plugin_migrations import database, iki, migrate, orm, sync

from iki_spec import schemas
from iki_spec.validate import load_plugin

LOTS = PLUGINS / "lots"
#: A concrete type of complex, for a protein lot of one.
COMPLEX = PLUGINS / "complex"
#: Inotersen (Tegsedi): a 5-10-5 2′-MOE gapmer, phosphorothioate throughout, every C a
#: 5-methylcytosine, as the oligo plugin's design action writes it.
INOTERSEN = (
    "RNA1{"
    + "[sP].".join(
        f"[{'MOE' if i < 5 or i >= 15 else 'dR'}]({'[5meC]' if b == 'C' else b})"
        for i, b in enumerate("TCTTGGTTACATGAAATCCC")
    )
    + "}$$$$V2.0"
)

#: M13 forward (GTAAAACGACGGCCAGTG), plain DNA.
M13 = "RNA1{" + "P.".join(f"[dR]({b})" for b in "GTAAAACGACGGCCAGTG") + "}$$$$V2.0"

__all__ = ["database"]


def _lots() -> Any:
    spec = importlib.util.spec_from_file_location("lots_script", LOTS / "scripts" / "lots.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lots = _lots()


def test_every_kind_of_lot_is_a_lot_with_its_own_amount() -> None:
    result = load_plugin(LOTS, dependencies=FIRST_PARTY[LOTS])
    assert result.problems == []
    assert result.plugin is not None
    types = result.plugin.types
    lot = types["iki/lots.Lot"]
    assert lot.abstract and lot.amount is None
    lot_parents = {"iki/lots.Lot", "iki/lots.BiologicLot"}
    concrete = {
        name.rsplit(".", 1)[1]: t
        for name, t in types.items()
        if not t.abstract and t.extends in lot_parents
    }
    # One search finds every kind: each extends Lot, directly or through BiologicLot.
    parents = {name: t.extends for name, t in concrete.items()}
    assert parents == {
        "GenericLot": "iki/lots.Lot",
        "OligoLot": "iki/lots.Lot",
        "PlasmidPrep": "iki/lots.Lot",
        "PolymerLot": "iki/lots.Lot",
        "ProteinLot": "iki/lots.BiologicLot",
        "ConjugateLot": "iki/lots.BiologicLot",
    }
    # Each kind's amount is in the unit it's made and used in: one lot type for any
    # entity couldn't have one unit for them all. And it's of what the lot is of, so a
    # transfer into one takes only the same molecule's (ADR 0162).
    assert all(t.amount is not None for t in concrete.values())
    units = {name: t.amount.unit for name, t in concrete.items() if t.amount is not None}
    assert units == {
        "GenericLot": "mg",
        "OligoLot": "nmol",
        "PlasmidPrep": "ug",
        "PolymerLot": "mg",
        "ProteinLot": "mg",
        "ConjugateLot": "mg",
    }
    materials = {n: t.amount.material for n, t in concrete.items() if t.amount is not None}
    assert materials == {
        "GenericLot": "of",
        "OligoLot": "oligonucleotide",
        "PlasmidPrep": "plasmid",
        "PolymerLot": "polymer",
        "ProteinLot": "protein",
        "ConjugateLot": "conjugate",
    }
    # Each names what it's a lot of, by its type where one is registered.
    refs = {
        name: [to for f in t.fields.values() if f.type == "ref" for to in f.to]
        for name, t in concrete.items()
    }
    assert refs == {
        "GenericLot": ["iki/core.Entity"],
        "OligoLot": ["iki/oligo.Oligonucleotide"],
        "PlasmidPrep": ["iki/plasmid.Plasmid"],
        "PolymerLot": ["iki/polymer.Polymer"],
        # A protein, or a complex of them: an antibody, an Fc fusion, a multispecific.
        "ProteinLot": ["iki/protein.Protein", "iki/core.Complex"],
        "ConjugateLot": ["iki/conjugate.Conjugate"],
    }
    # QA's disposition: quarantined until released, which locks it and is signed for;
    # on hold, rejected and expired hold its amount, whichever kind it is.
    states = lot.fields["disposition"].states
    assert lot.fields["disposition"].initial == "quarantined"
    assert [n for n, s in states.items() if s.holds_amount] == ["on_hold", "rejected", "expired"]
    assert [n for n, s in states.items() if s.locks] == ["released", "rejected", "expired"]
    assert [(s.meaning, s.label) for s in states["released"].signatures] == [
        ("approval", "QA release")
    ]
    assert set(states["released"].guards) == {
        "from_quarantine",
        "has_a_certificate",
        "says_where_it_came_from",
        "names_its_maker",
        "not_its_maker",
        "not_who_recorded_it",
        "not_who_edited_it",
        "a_person",
    }
    assert {s for st in states.values() for s in st.set_by} == {"iki/lots.qa"}


def test_each_kind_of_lot_works_out_its_values_when_its_recorded_and_measured() -> None:
    result = load_plugin(LOTS, dependencies=FIRST_PARTY[LOTS])
    assert result.plugin is not None
    triggers = result.plugin.triggers.values()
    fired = sorted((t.type.rsplit(".", 1)[1], t.on) for t in triggers)
    kinds = ["ConjugateLot", "OligoLot", "PlasmidPrep", "PolymerLot", "ProteinLot"]
    assert fired == sorted((k, on) for k in kinds for on in ("entity.created", "entity.updated"))
    # What each action writes is what its trigger watches the inputs of.
    actions = result.plugin.actions
    assert actions["compute_oligo_lot"].inputs["lot"].include == ("oligonucleotide",)
    assert actions["compute_protein_lot"].inputs["lot"].include == ("protein",)


def script(tmp_path: Path, kind: str, *entities: dict[str, Any]) -> dict[str, Any]:
    """Runs the plugin's script as the worker does, on ``entities``."""
    run_input = {"inputs": {"lot": list(entities)}, "params": {}}
    (tmp_path / "input.json").write_text(json.dumps(run_input))
    out = tmp_path / "out.json"
    done = subprocess.run(  # noqa: S603 - our own script
        [sys.executable, str(LOTS / "scripts" / "lots.py"), kind],
        env={"IKI_INPUT": str(tmp_path / "input.json"), "IKI_OUTPUT": str(out)},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    output: dict[str, Any] = json.loads(out.read_text())
    assert not list(schemas.check("run-output", output))
    return output


def lot(i: int, **values: Any) -> dict[str, Any]:
    return {"id": f"00000000-0000-0000-0000-{i:012d}", "hrid": f"LOT-{i}", "values": values}


def test_a_batch_of_lots_each_gets_its_own(tmp_path: Path) -> None:
    """A batch packs many runs into one job: each lot's values are its own, and a lot with
    nothing new to write writes nothing."""
    out = script(
        tmp_path,
        "polymer",
        lot(1, mn=236000, mw=241000),
        lot(2, mn=29184, mw=38560, dispersity=1.321),
        lot(3, mn={"value": 35800, "unit": "g/mol"}, mw={"value": 37400, "unit": "g/mol"}),
    )
    assert out["updates"]["lot"] == [
        {"entity": lot(1)["id"], "values": {"dispersity": 1.021}},
        {"entity": lot(3)["id"], "values": {"dispersity": 1.045}},
    ]


def test_a_distribution_without_area_has_no_dar(tmp_path: Path) -> None:
    out = script(
        tmp_path,
        "conjugate",
        lot(1, drug_loads=[{"chain": "intact", "drug_load": 4, "area": 0}]),
    )
    assert out["updates"]["lot"] == []
    assert out["messages"] == [
        {
            "level": "warning",
            "text": "LOT-1: its DAR isn't worked out: its distribution has no area.",
            "entity": lot(1)["id"],
        }
    ]


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        # Agilent 5994-3522EN, Table 1: brentuximab vedotin by HIC, "DAR 3.3".
        ([(0, 8.11), (2, 38.83), (4, 36.88), (6, 12.78), (8, 3.4)], 3.2906),
        # Thermo AN002598, Figure 9: polatuzumab vedotin by HIC, 3.43.
        ([(0, 6.07), (2, 31.49), (4, 49.27), (6, 11.35), (8, 1.83)], 3.428),
    ],
)
def test_an_intact_dar_is_the_mean_load_by_area(
    rows: list[tuple[int, float]], expected: float
) -> None:
    distribution = [{"chain": "intact", "drug_load": k, "area": a} for k, a in rows]
    dar, unconjugated, _ = lots.dar(distribution)
    assert dar == pytest.approx(expected, abs=5e-4)
    assert unconjugated == pytest.approx(100 * rows[0][1] / sum(a for _, a in rows))


def test_an_iggs_reduced_dar_is_twice_each_chains_mean() -> None:
    """Agilent 5991-6559EN, Table 1: Adcetris (an IgG1, two light and two heavy chains)
    reduced, LC 43.3/56.7, HC 19.9/36.1/25.4/18.6, weighted average DAR 4.0: 2 × 0.567 +
    2 × 1.427."""
    rows = [("light", 0, 43.3), ("light", 1, 56.7)] + [
        ("heavy", k, a) for k, a in ((0, 19.9), (1, 36.1), (2, 25.4), (3, 18.6))
    ]
    distribution = [{"chain": c, "drug_load": k, "area": a} for c, k, a in rows]
    dar, unconjugated, how = lots.dar(distribution, (2, 2))
    assert dar == pytest.approx(3.988)
    assert unconjugated is None
    assert how == "2 × 0.5670 light + 2 × 1.4270 heavy"


def test_a_reduced_dar_counts_the_chains_its_conjugate_has() -> None:
    """A Fab carries one heavy fragment and one light chain (IMGT), so one drug on its
    heavy fragment and none on its light chain is DAR 1, not an IgG's 2; and a conjugate
    that doesn't say what chains it has gets no reduced DAR, rather than an IgG's."""
    rows = [
        {"chain": "light", "drug_load": 0, "area": 100},
        {"chain": "heavy", "drug_load": 1, "area": 100},
    ]
    fab = (1, 1)
    assert lots.dar(rows, fab)[0] == pytest.approx(1.0)
    assert lots.dar(rows, (2, 2))[0] == pytest.approx(2.0)
    dar, _, why = lots.dar(rows, None)
    assert dar is None
    assert "doesn't say how many light and heavy chains" in why
    assert lots.chains({"carrier_heavy_copies": 1, "carrier_light_copies": 1}) == fab
    assert lots.chains({"carrier_heavy_copies": 1, "carrier_light_copies": 0}) is None
    assert lots.chains({}) is None


def compute(kind: str, *entities: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = lots.compute(kind, {"inputs": {"lot": list(entities)}})
    return result


def changes(output: dict[str, Any]) -> list[dict[str, Any]]:
    return [u["values"] for u in output["updates"]["lot"]]


def test_run_logs_keep_a_whole_numbers_zeros(capsys: pytest.CaptureFixture[str]) -> None:
    """M13 forward's ε260 is 182,800 L/(mol·cm) (Eurogentec's worked example), not
    182,8; 10 OD260 at it is 54.7 nmol."""
    compute(
        "oligo",
        lot(1, yield_od=10, extinction_260=182800),
        lot(2, yield_od=1, extinction_260=1500),
    )
    out = capsys.readouterr().out
    assert "LOT-1: 10 OD260 at ε260 182,800 L/(mol·cm) is 54.7 nmol" in out
    assert "at ε260 1,500 L/(mol·cm)" in out
    assert lots._shown(100, 0) == "100"
    assert lots._shown(2.50) == "2.5"


def test_a_mass_is_compared_with_its_tolerance_as_its_specification_gives_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """M13 forward's monoisotopic mass is 5554.9825 Da; a measured 5555.03254 is 0.05004
    Da heavy, outside a specification of ±0.05001 Da, however close: the difference isn't
    rounded to 0.0500 first, and the log shows the tolerance as given."""
    m13 = {"monoisotopic_mass": 5554.9825}
    out = compute(
        "oligo",
        lot(1, measured_mass=5555.03254, mass_basis="monoisotopic", mass_tolerance=0.05001)
        | {"included": {"oligonucleotide": [{"values": m13}]}},
        lot(2, measured_mass=5554.995, mass_basis="monoisotopic", mass_tolerance=0.015)
        | {"included": {"oligonucleotide": [{"values": m13}]}},
    )
    assert [c["mass_error"] for c in changes(out)] == [pytest.approx(0.05004, abs=1e-12), 0.0125]
    log = capsys.readouterr().out
    assert "LOT-1: measured 5,555.03254 Da, expected 5,554.9825 Da (monoisotopic), " in log
    assert "+0.05004 Da, outside ±0.05001 Da" in log
    assert "+0.0125 Da, within ±0.015 Da" in log


@pytest.mark.parametrize("measured", [1000.1000005, 999.8999995])
def test_mass_tolerance_has_no_unstated_margin(
    measured: float, capsys: pytest.CaptureFixture[str]
) -> None:
    compute(
        "oligo",
        lot(1, measured_mass=measured, mass_basis="average", mass_tolerance=0.1)
        | {"included": {"oligonucleotide": [{"values": {"molecular_weight": 1000.0}}]}},
    )
    assert "outside ±0.1 Da" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("kind", "values", "said"),
    [
        # A260 1 over 0 cm isn't 50 ng/uL: there's no path to divide by.
        ("plasmid", {"a260": 1, "dilution_factor": 1, "path_length": 0}, "path length is 0 cm"),
        ("plasmid", {"a260": 1, "dilution_factor": 1}, "it has no path length"),
        (
            "protein",
            {"a280": 1, "dilution_factor": 1, "path_length": 0, "extinction_coefficient": 1.5},
            "path length is 0 cm",
        ),
        # A measured e280 of 0 isn't replaced by its sequence's.
        (
            "protein",
            {"a280": 1, "dilution_factor": 1, "path_length": 1, "extinction_coefficient": 0},
            "its measured ε280 is 0 mL/(mg·cm)",
        ),
        # Nor an e260 of 0 by its oligonucleotide's.
        ("oligo", {"yield_od": 1, "extinction_260": 0}, "the ε260 it gives is 0"),
    ],
)
def test_a_zero_path_or_coefficient_gives_nothing(
    kind: str, values: dict[str, Any], said: str
) -> None:
    lysozyme = {
        "molecular_weight": 14313.0,
        "extinction_coefficient_oxidized": 37970,
        "extinction_coefficient_reduced": 37470,
    }
    m13 = {"monoisotopic_mass": 5554.9825, "extinction_260": 182800}
    entity = lot(1, **values) | {
        "included": {"protein": [{"values": lysozyme}], "oligonucleotide": [{"values": m13}]}
    }
    out = compute(kind, entity)
    assert not any(
        c.get("concentration") or c.get("yield_amount") or c.get("a280_concentration")
        for c in changes(out)
    )
    assert [m["level"] for m in out["messages"]] == ["warning"]
    assert said in out["messages"][0]["text"]


def test_a_concentration_follows_its_reading_not_an_older_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """What an A260 or an A280 gives is kept beside the concentration, which follows it.
    A reading taken out empties a concentration it gave (420 ng/µL, its A260 gone; 2
    mg/mL, its ε280 gone). One pooled since (ADR 0157: 1 mL at 1 mg/mL into 1 mL at 3 is
    2 mg/mL) or entered since isn't put back to what the reading before the pool gave,
    whatever else changes, such as a conjugate's drug loads."""
    lysozyme = {"molecular_weight": 14313.0, "extinction_coefficient_oxidized": 37970}
    out = compute(
        "plasmid",
        lot(1, concentration_method="a260", concentration=420, a260_concentration=420),
        lot(2, concentration_method="a260", concentration=500, a260_concentration=420),
    )
    assert changes(out) == [
        {"a260_concentration": None, "concentration": None},
        {"a260_concentration": None},
    ]
    assert out["messages"][0]["text"] == (
        "LOT-1: its concentration is emptied: it was its A260's, which gives none now."
    )
    gone = compute(
        "protein",
        lot(3, a280=1, path_length=1, concentration=2, a280_concentration=2, a280_extinction=0.5)
        | {"included": {"protein": [{"values": lysozyme}]}},
    )
    assert changes(gone) == [
        {"a280_extinction": None, "a280_concentration": None, "concentration": None}
    ]
    pooled = {
        "a280": 3,
        "dilution_factor": 1,
        "path_length": 1,
        "extinction_coefficient": 1,
        "concentration_method": "a280",
        "a280_extinction": 1,
        "a280_concentration": 3,
    }
    out = compute(
        "conjugate",
        lot(
            4,
            concentration=2,
            dar=3.29,
            distribution_dar=3.29,
            drug_loads=[
                {"chain": "intact", "drug_load": 0, "area": 8.11},
                {"chain": "intact", "drug_load": 4, "area": 91.89},
            ],
            **pooled,
        ),
        lot(5, concentration=None, **pooled),
    )
    assert [c.get("concentration", "kept") for c in changes(out)] == ["kept"]
    log = capsys.readouterr().out
    assert "LOT-4: its concentration is kept as it is, 2: it was set since its A280 gave 3" in log
    assert "LOT-5: its concentration is kept as it is, not known" in log
    # A new reading of the pool is what it's worked out from.
    again = compute("protein", lot(6, **{**pooled, "a280": 2.1, "concentration": 2}))
    assert changes(again) == [{"a280_concentration": 2.1, "concentration": 2.1}]


def test_a_dar_follows_its_distribution_unless_its_measured_another_way() -> None:
    """A DAR measured by UV/Vis is as entered, whatever distribution there is; one its
    distribution gave is emptied when the distribution gives none (its areas all 0, or
    its rows gone)."""
    intact = [
        {"chain": "intact", "drug_load": 0, "area": 0},
        {"chain": "intact", "drug_load": 4, "area": 0},
    ]
    four = [{"chain": "intact", "drug_load": 4, "area": 100}]
    out = compute(
        "conjugate",
        lot(1, dar_method="uv_vis", dar=3.4, drug_loads=four),
        lot(2, dar_method="hic", dar=4, distribution_dar=4, drug_loads=intact),
        lot(3, dar_method="hic", dar=4, distribution_dar=4, unconjugated=0),
    )
    assert changes(out) == [
        {"unconjugated": 0},
        {"distribution_dar": None, "dar": None},
        {"unconjugated": None, "distribution_dar": None, "dar": None},
    ]


def test_a_proteins_cystines_take_their_hydrogens_off_as_a_complexs_do(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Hen lysozyme (UniProt P00698, 19–147) has four disulfides: 14,313.0 g/mol by its
    sequence less 8 H (8.06 g/mol) is 14,304.94, so its 37,970 L/(mol·cm) is 2.6543
    mL/(mg·cm), as a complex's cystines are weighed; reduced, its sequence's 37,470 over
    14,313.0. A complex's disulfides are said only when its ε280 is what's used."""
    lysozyme = {
        "molecular_weight": 14313.0,
        "extinction_coefficient_oxidized": 37970,
        "extinction_coefficient_reduced": 37470,
    }
    out = compute(
        "protein",
        lot(1, a280=0.53, dilution_factor=10, path_length=1, cysteines="cystines")
        | {"included": {"protein": [{"values": lysozyme}]}},
    )
    assert changes(out) == [
        {"a280_extinction": 2.6543, "a280_concentration": 1.9967, "concentration": 1.9967}
    ]
    assert "its protein's 4 cystines take 8.06 g/mol off" in capsys.readouterr().out
    hemoglobin = {
        "molecular_weight": 64000.0,
        "extinction_coefficient_reduced": 125000,
        "components": [],
        "bonds": [{"kind": "disulfide"}],
    }
    compute(
        "protein",
        lot(2, a280=1, path_length=1, extinction_coefficient=1.4)
        | {"included": {"protein": [{"values": hemoglobin}]}},
    )
    log = capsys.readouterr().out
    assert "disulfide" not in log
    assert "by its measured ε280" in log


def test_lots_of_any_entity_keep_their_amounts_rules_and_disposition(
    tmp_path: Path, database: str
) -> None:
    """Lots in a database: an oligonucleotide lot drawn by volume at its concentration, its
    mass by ESI-MS held for QA's review when a run finds it outside its tolerance; a lot
    on hold, whichever kind, keeps its amount from being drawn; a plasmid prep only of a
    plasmid, and a protein lot of a complex too; a polymer's Mw never below its Mn;
    release signed for, on a certificate, by someone other than its maker; and one search
    finding every kind."""
    out = tmp_path / "build"
    built = iki("plugin", "build", *build_order(LOTS, COMPLEX), "--output", out, cwd=ROOT)
    assert built.returncode == 0, built.stdout + built.stderr
    migrate(out, database)
    sync(out, database)
    result = orm(
        out,
        database,
        f"""
        from decimal import Decimal
        from iki_server.accounts.models import User
        from iki_server.amounts import services as amounts
        from iki_server.entities import checking, holds, search, services, states
        from iki_server.org.models import LabRoleGrant, ProjectMembership, ProjectRole
        from iki_server.org.services import Refused

        alice = User.objects.create_user("alice@example.com")
        quinn = User.objects.create_user("quinn@example.com")
        for user in (alice, quinn):
            m = ProjectMembership.objects.create(
                project=project, user=user, role=ProjectRole.EDITOR
            )
            LabRoleGrant.objects.create(membership=m, role="iki/lots.qa")

        def errors(do):
            try:
                do()
            except services.Invalid as invalid:
                return invalid.errors
            except (Refused, amounts.Wrong) as refused:
                return str(getattr(refused, "errors", refused))
            return None

        # Inotersen, its mass computed by a run as the oligo plugin's computes it.
        oligo = services.create(
            alice, "iki/oligo.Oligonucleotide", project,
            {{"helm": {INOTERSEN!r}, "bases": "TCTTGGTTACATGAAATCCC",
              "molecular_weight": 7183.08, "monoisotopic_mass": 7178.056}},
            by_run=True,
        ).entity
        made = services.create(
            alice, "iki/lots.OligoLot", project,
            {{"oligonucleotide": str(oligo.pk), "lot_number": "TEG-2611", "source": "made",
              "made_by": str(alice.pk), "synthesis_scale": 1, "purification": "ie_hplc",
              "counterion": "sodium", "concentration": 100, "mass_basis": "average",
              "measured_mass": 7182.2}},
            amount={{"value": 500, "unit": "nmol"}},
        ).entity
        # 50 uL of a 100 uM solution is 5 nmol.
        amounts.draw(
            alice, made, amounts.Given(Decimal("50"), "uL"),
            purpose="qa_sampling", reason="Sampled for testing",
        )
        left = str(amounts.amounts_of([made])[made.pk].value)

        # A run finds its mass 0.88 Da light of inotersen's average: within 2 Da.
        made.refresh_from_db()
        services.update(
            alice, made, {{"expected_mass": 7183.08, "mass_error": -0.88}},
            expected_revision=made.revision, by_run=True,
        )
        within = [h.rule for h in holds.open_holds(made)]
        # Another lot, whose mass is volanesorsen's: held as it's saved, from its measured
        # mass and inotersen's, with no run needed, saying why.
        mixed_values = {{"oligonucleotide": str(oligo.pk), "mass_basis": "average",
                         "measured_mass": 7164.3}}
        refused_unheld = errors(lambda: services.create(
            alice, "iki/lots.OligoLot", project, mixed_values,
        ))
        mixed = services.create(
            alice, "iki/lots.OligoLot", project, mixed_values,
            amount={{"value": 200, "unit": "nmol"}},
            hold_reason="Its ESI-MS, as its supplier's certificate gives it",
        ).entity
        outside = [h.rule for h in holds.open_holds(mixed)]

        # On hold, an oligonucleotide lot's nmol can't be drawn: the abstract Lot's state
        # holds every kind's amount.
        made.refresh_from_db()
        states.set_state(
            quinn, made, "disposition", "on_hold", expected_revision=made.revision,
            reason="Deviation DEV-17: the synthesizer's amidite lot is under investigation",
        )
        on_hold = errors(lambda: amounts.draw(alice, made, amounts.Given(Decimal("1"), "nmol")))
        made.refresh_from_db()
        # Released only on a certificate, by QA who didn't make it, signing for it: its
        # guards are checked at each signature, with its signer acting.
        released = states.state_fields(made.type)["disposition"].states["released"]
        no_certificate = states.condition(quinn, made, "disposition", released)
        from iki_server.files.models import File
        coa = File.objects.create(
            blob=blob, name="TEG-2611 CoA.pdf", media_type="application/pdf", project=project
        )
        states.set_state(
            quinn, made, "disposition", "quarantined", expected_revision=made.revision,
            reason="Investigation closed: no impact",
        )
        made.refresh_from_db()
        services.update(
            alice, made, {{"certificates": [str(coa.pk)]}}, expected_revision=made.revision,
        )
        made.refresh_from_db()
        by_its_maker = states.condition(alice, made, "disposition", released)
        by_quinn = states.condition(quinn, made, "disposition", released)
        try:
            states.set_state(
                quinn, made, "disposition", "released", expected_revision=made.revision,
            )
            signed = None
        except states.NeedsSignatures as needs:
            signed = type(needs).__name__

        # A plasmid prep is of a plasmid, not of any DNA sequence; a polymer's Mw isn't
        # below its Mn.
        sequence = services.create(
            alice, "iki/dna.DNASequence", project,
            {{"bases": "ACGTACGTAC", "topology": "circular", "name": "pUC19"}},
        ).entity
        puc = services.create(
            alice, "iki/plasmid.Plasmid", project,
            {{"sequence": str(sequence.entry_id), "name": "pUC19"}},
        ).entity
        prep = services.create(
            alice, "iki/lots.PlasmidPrep", project,
            {{"plasmid": str(puc.pk), "scale": "maxiprep", "a260": 0.42,
              "dilution_factor": 20, "concentration": 420}},
            amount={{"value": 840, "unit": "ug"}},
        ).entity
        # 2 ug at 420 ng/uL: 4.76 uL, drawn by mass.
        amounts.draw(alice, prep, amounts.Given(Decimal("2"), "ug"), purpose="qa_sampling")
        of_a_sequence = errors(lambda: services.create(
            alice, "iki/lots.PlasmidPrep", project, {{"plasmid": str(sequence.pk)}},
        ))
        # A protein lot of a complex: hemoglobin A's two alpha and two beta chains.
        alpha, beta = (
            services.create(
                alice, "iki/protein.Protein", project, {{"sequence": seq, "name": name}},
            ).entity
            for seq, name in (("VLSPADKTNVKAAW", "alpha"), ("VHLTPEEKSAVTALW", "beta"))
        )
        hemoglobin = services.create(
            alice, "iki/complex.ProteinComplex", project,
            {{"name": "hemoglobin A", "components": [
                {{"part": str(alpha.entry_id), "copies": 2, "role": "alpha"}},
                {{"part": str(beta.entry_id), "copies": 2, "role": "beta"}},
            ]}},
        ).entity
        of_a_complex = services.create(
            alice, "iki/lots.ProteinLot", project, {{"protein": str(hemoglobin.pk)}},
            amount={{"value": 10, "unit": "mg"}},
        ).entity
        # Synthetic conjugate for testing the link. Its component is aspirin (PubChem 2244).
        aspirin = services.create(
            alice, "iki/small_molecule.Compound", project,
            {{"inchikey": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N", "smiles": "CC(=O)Oc1ccccc1C(=O)O"}},
        ).entity
        def register_conjugate(rows, kind="oligo_conjugate"):
            return services.create(
                alice, "iki/conjugate.Conjugate", project,
                {{"carrier": [{{"kind": kind, "molecule": str(oligo.entry_id)}}],
                  "attachments": rows}},
            ).entity

        one = {{"part": str(aspirin.entry_id), "site": "site A", "copies": 1,
               "loss_formula": "H2O"}}
        two = {{**one, "site": "site B"}}
        conjugate = register_conjugate([one, two])
        reordered = register_conjugate([two, one])
        assert register_conjugate([{{**one, "loss_formula": "OH2"}}, two]).pk == conjugate.pk
        assert register_conjugate([one, {{**two, "loss_formula": "HHO"}}]).pk == conjugate.pk
        heavy = register_conjugate([{{**one, "loss_formula": "[2H]2O"}}])
        assert register_conjugate([{{**one, "loss_formula": "D2O"}}]).pk == heavy.pk
        assert heavy.pk != conjugate.pk
        # The registry keeps the text as first submitted.
        [attachment] = services.current_values_of([heavy])[heavy.pk]["attachments"]
        assert attachment["loss_formula"] == "[2H]2O"
        # Upgrade two differently spelled, previously distinct entries without rewriting
        # their IDs or anyone's submitted evidence. Reverse and repeat the same upgrade.
        from django.apps import apps
        from django.db import connection
        from importlib import import_module
        from unittest.mock import patch
        with patch("iki_spec.identity.canonical_loss", side_effect=lambda text: text):
            old = register_conjugate([{{**one, "site": "legacy", "loss_formula": "OH2"}}])
            duplicate = register_conjugate([{{**one, "site": "legacy", "loss_formula": "HHO"}}])
        assert old.entry_id != duplicate.entry_id
        app = apps.get_app_config("iki__conjugate").name
        migration = import_module(app + ".migrations.0003_atom_loss_identity")
        with connection.schema_editor() as editor:
            migration.forwards(apps, editor)
            assert register_conjugate([{{**one, "site": "legacy"}}]).pk == old.pk
            migration.backwards(apps, editor)
            migration.forwards(apps, editor)
        assert register_conjugate([{{**one, "site": "legacy", "loss_formula": "HHO"}}]).pk == old.pk
        [kept] = services.current_values_of([duplicate])[duplicate.pk]["attachments"]
        assert kept["loss_formula"] == "HHO"
        moved = register_conjugate([one, {{**two, "site": "site C"}}])
        another_ratio = register_conjugate([one, {{**two, "copies": 2}}])
        mismatched_kind = errors(lambda: register_conjugate([one], kind="antibody_drug"))
        conjugate_lot = services.create(
            alice, "iki/lots.ConjugateLot", project, {{"conjugate": str(conjugate.entry_id)}},
        ).entity
        of_carrier = errors(lambda: services.create(
            alice, "iki/lots.ConjugateLot", project, {{"conjugate": str(oligo.entry_id)}},
        ))
        # Styrene (NIST Chemistry WebBook 100-42-5) and the certified means of SRM 1478:
        # https://tsapps.nist.gov/srmext/certificates/1478.pdf
        styrene = services.create(
            alice, "iki/small_molecule.Compound", project,
            {{"inchikey": "PPBRXRYQALVLMV-UHFFFAOYSA-N", "smiles": "C=Cc1ccccc1"}},
        ).entity
        polystyrene = services.create(
            alice, "iki/polymer.Polymer", project,
            {{"bigsmiles": "{{[][<]CC(c1ccccc1)[>][]}}", "architecture": "linear",
              "sequence_distribution": "homopolymer", "monomers": [str(styrene.entry_id)]}},
        ).entity
        srm = services.create(
            alice, "iki/lots.PolymerLot", project,
            {{"polymer": str(polystyrene.entry_id), "catalogue_number": "SRM 1478",
              "mn": 35800, "mn_method": "membrane_osmometry",
              "mw": 37400, "mw_method": "sedimentation_equilibrium"}},
        ).entity
        wrong_polymer_weights = errors(lambda: services.create(
            alice, "iki/lots.PolymerLot", project,
            {{"polymer": str(polystyrene.entry_id), "mn": 38560, "mw": 29184}},
        ))
        of_monomer = errors(lambda: services.create(
            alice, "iki/lots.PolymerLot", project, {{"polymer": str(styrene.entry_id)}},
        ))
        generic = services.create(
            alice, "iki/lots.GenericLot", project, {{"of": str(oligo.pk)}},
            amount={{"value": 25, "unit": "mg"}},
        ).entity
        everything = search.queryset(alice, search.Search(type="iki/lots.Lot", project=project.pk))
        result = {{
            "left": left,
            "within": within,
            "refused_unheld": sorted(refused_unheld),
            "outside": outside,
            "on_hold": on_hold,
            "no_certificate": no_certificate,
            "by_its_maker": by_its_maker,
            "by_quinn": by_quinn,
            "signed": signed,
            "prep_left": str(amounts.amounts_of([prep])[prep.pk].value),
            "of_a_sequence": of_a_sequence,
            "of_a_complex": of_a_complex.type,
            "of_conjugate": services.current_values_of([conjugate_lot])[conjugate_lot.pk][
                "conjugate"
            ] == conjugate.entry_id,
            "conjugate_identities": [str(c.entry_id) for c in
                                    (conjugate, reordered, moved, another_ratio)],
            "mismatched_kind": mismatched_kind,
            "of_carrier": of_carrier,
            "of_polymer": services.current_values_of([srm])[srm.pk]["polymer"]
                          == polystyrene.entry_id,
            "wrong_polymer_weights": wrong_polymer_weights,
            "of_monomer": of_monomer,
            "lots": sorted(e.type for e in everything),
            "hrids": sorted(e.hrid.split("-")[0] for e in everything),
        }}
        """,
    )
    assert Decimal(result["left"]) == Decimal(495)
    assert result["within"] == []
    assert result["refused_unheld"] == ["measured_mass"]
    assert result["outside"] == ["mass_within_tolerance"]
    assert result["on_hold"] == {
        "amount": [
            "OLOT-000001 is held by its disposition: none of it can be taken until that changes."
        ]
    }
    assert result["no_certificate"] == "Attach its certificate of analysis before releasing it."
    assert result["by_its_maker"] == "You made this lot, so someone else in QA releases it."
    assert result["by_quinn"] is None
    assert result["signed"] == "NeedsSignatures"
    assert Decimal(result["prep_left"]) == Decimal(838)
    assert result["of_a_sequence"] == {
        "plasmid": ["must refer to a iki/plasmid.Plasmid, not a iki/dna.DNASequence."]
    }
    assert result["of_a_complex"] == "iki/lots.ProteinLot"
    assert result["of_conjugate"] is True
    first, reordered, moved, another_ratio = result["conjugate_identities"]
    assert first == reordered
    assert len({first, moved, another_ratio}) == 3
    assert result["mismatched_kind"] == {
        "carrier/0/molecule": [
            "must refer to a iki/antibody.Antibody in a antibody_drug row, "
            "not a iki/oligo.Oligonucleotide."
        ]
    }
    assert result["of_carrier"] == {
        "conjugate": ["must refer to a iki/conjugate.Conjugate, not a iki/oligo.Oligonucleotide."]
    }
    assert result["of_polymer"] is True
    assert result["of_monomer"] == {
        "polymer": ["must refer to a iki/polymer.Polymer, not a iki/small_molecule.Compound."]
    }
    assert result["wrong_polymer_weights"] == {
        "mw": [
            "Its Mw, 29184 g/mol, is below its Mn, 38560 g/mol: a weight average is never "
            "below the number average of the same chains."
        ]
    }
    assert result["lots"] == [
        "iki/lots.ConjugateLot",
        "iki/lots.GenericLot",
        "iki/lots.OligoLot",
        "iki/lots.OligoLot",
        "iki/lots.PlasmidPrep",
        "iki/lots.PolymerLot",
        "iki/lots.ProteinLot",
    ]
    assert result["hrids"] == ["CJLOT", "GLOT", "OLOT", "OLOT", "PLOT", "PREP", "PYLOT"]


def test_a_lot_is_released_as_it_is_by_someone_else_and_stays_what_it_is(
    tmp_path: Path, database: str
) -> None:
    """QA releases a lot knowing where it came from and that someone else made it and
    recorded it; its identity by ESI-MS is checked as it's saved, without a run, so an
    edit out of its tolerance, or a mass that can't be checked, holds it from release;
    and a transfer can't change what a lot is: a released one takes only released
    material, and any lot only its own molecule's, as an aliquot is its source's."""
    out = tmp_path / "build"
    built = iki("plugin", "build", *build_order(LOTS, COMPLEX), "--output", out, cwd=ROOT)
    assert built.returncode == 0, built.stdout + built.stderr
    migrate(out, database)
    sync(out, database)
    result = orm(
        out,
        database,
        f"""
        from decimal import Decimal
        from django.db import transaction
        from iki_server.accounts.models import User
        from iki_server.amounts import services as amounts
        from iki_server.entities import holds, services, states
        from iki_server.entities.models import Entity
        from iki_server.files.models import File
        from iki_server.org.models import LabRoleGrant, ProjectMembership, ProjectRole
        from iki_server.org.services import Refused

        alice, quinn, eve = (
            User.objects.create_user(f"{{n}}@example.com") for n in ("alice", "quinn", "eve")
        )
        for user in (alice, quinn, eve):
            m = ProjectMembership.objects.create(
                project=project, user=user, role=ProjectRole.EDITOR
            )
            if user is not eve:
                LabRoleGrant.objects.create(membership=m, role="iki/lots.qa")
        coa = File.objects.create(
            blob=blob, name="CoA.pdf", media_type="application/pdf", project=project
        )
        disposition = states.state_fields("iki/lots.OligoLot")["disposition"]
        released = disposition.states["released"]

        def errors(do):
            try:
                do()
            except services.Invalid as invalid:
                return invalid.errors
            except (Refused, amounts.Wrong) as refused:
                return getattr(refused, "errors", None) or str(refused)
            return None

        def why_not(who, lot):
            lot.refresh_from_db()
            return states.condition(who, lot, "disposition", released)

        def release(lot):
            # As its last signature does (ADR 0126), once its guards let it.
            with transaction.atomic():
                locked = Entity.objects.select_for_update().get(pk=lot.pk)
                assert states.condition(quinn, locked, "disposition", released) is None
                states.write(locked, disposition, "released", quinn, "")
            lot.refresh_from_db()

        # Inotersen and M13 forward, their masses computed by a run, and an
        # oligonucleotide of an unknown monomer, whose masses can't be.
        inotersen, m13, unknown = (
            services.create(alice, "iki/oligo.Oligonucleotide", project, v, by_run=True).entity
            for v in (
                {{"helm": {INOTERSEN!r}, "bases": "TCTTGGTTACATGAAATCCC",
                  "molecular_weight": 7183.08, "monoisotopic_mass": 7178.056}},
                {{"helm": {M13!r}, "bases": "GTAAAACGACGGCCAGTG",
                  "molecular_weight": 5557.68, "monoisotopic_mass": 5554.9825}},
                {{"helm": "RNA1{{[dR](A)P.[dR](C)}}$$$$V2.0", "bases": "AC"}},
            )
        )

        def oligo_lot(who, of=inotersen, hold_reason="", **values):
            return services.create(
                who, "iki/lots.OligoLot", project,
                {{"oligonucleotide": str(of.pk), "mass_basis": "average",
                  "certificates": [str(coa.pk)], "concentration": 100, **values}},
                amount={{"value": 50, "unit": "nmol"}}, hold_reason=hold_reason,
            ).entity

        # A late parent measurement schedules the affected lot even without another lot edit.
        from django.utils import timezone
        from iki_server.runs.models import ProjectTrigger, Recalculation, TriggerFiring
        admin = User.objects.create_user("lot-admin@example.com")
        ProjectMembership.objects.create(project=project, user=admin, role=ProjectRole.ADMIN)
        ProjectTrigger.objects.create(project=project,
                                      trigger="iki/lots.compute_oligo_lot_when_measured",
                                      enabled_by=admin, enabled_at=timezone.now())
        late = services.create(alice, "iki/oligo.Oligonucleotide", project,
                               {{"helm": "RNA1{{[dR](A)P.[dR](G)}}$$$$V2.0"}}).entity
        waiting = oligo_lot(alice, of=late)
        unrelated = oligo_lot(alice, of=m13)
        late.refresh_from_db()
        services.update(alice, late, {{"molecular_weight": 1234.5}}, by_run=True,
                        expected_revision=late.revision)
        refreshed = TriggerFiring.objects.filter(
            trigger__trigger="iki/lots.compute_oligo_lot_when_measured",
            entity__in=[waiting, unrelated],
        )
        assert list(refreshed.values_list("entity_id", flat=True)) == [waiting.pk]
        pending = Recalculation.objects.filter(action="iki/lots.compute_oligo_lot")
        assert pending.filter(entity=waiting).exists()
        assert not Recalculation.objects.filter(entity=unrelated).exists()

        # Who may release it: QA who neither made it nor recorded it, knowing where it
        # came from. Made by left empty isn't someone else.
        unsaid = oligo_lot(alice)
        nobody = oligo_lot(alice, source="made")
        by_eve = oligo_lot(alice, source="made", made_by=str(eve.pk))
        bought = oligo_lot(alice, source="bought", supplier="IDT")
        who = {{
            "unsaid": why_not(alice, unsaid),
            "nobody": why_not(alice, nobody),
            "nobody_by_quinn": why_not(quinn, nobody),
            "recorded_by_alice": why_not(alice, by_eve),
            "recorded_by_alice_by_quinn": why_not(quinn, by_eve),
            "bought_by_whoever_recorded_it": why_not(alice, bought),
        }}
        services.update(quinn, by_eve, {{"notes": "Corrected testing record"}},
                        expected_revision=by_eve.revision)
        who["edited_by_quinn"] = why_not(quinn, by_eve)

        # Its mass, checked as it's saved: an edit 18.8 Da off is held at once, with no
        # run, so it can't be released; exactly 2 Da off is within 2 Da, however the two
        # decimals' difference rounds in binary; a mass that can't be checked is held.
        lot = oligo_lot(eve, source="made", made_by=str(eve.pk), measured_mass=7182.2)
        within = [h.rule for h in holds.open_holds(lot)]
        lot.refresh_from_db()
        edit = {{"measured_mass": 7164.3}}
        unheld = errors(lambda: services.update(eve, lot, edit, expected_revision=lot.revision))
        lot.refresh_from_db()
        services.update(
            eve, lot, edit, expected_revision=lot.revision, hold_reason="Re-run on the QTOF",
        )
        edited = [h.rule for h in holds.open_holds(lot)]
        held_release = why_not(quinn, lot)
        edge = oligo_lot(eve, source="bought", measured_mass=7185.08)
        past = errors(lambda: oligo_lot(eve, source="bought", measured_mass=7185.09))
        unchecked = oligo_lot(
            eve, of=unknown, source="bought", measured_mass=600.1, hold_reason="New monomer"
        )
        mass = {{
            "within": within,
            "unheld": sorted(unheld),
            "edited": edited,
            "held_release": held_release,
            "held_hrid": lot.hrid,
            "edge": [h.rule for h in holds.open_holds(edge)],
            "past": sorted(past),
            "unchecked": [h.rule for h in holds.open_holds(unchecked)],
        }}

        # What goes into a lot: a released lot takes only released material, even at the
        # same 100 uM; any lot takes only its own molecule's; an aliquot is its source's.
        target = oligo_lot(eve, source="bought")
        release(target)
        quarantined = oligo_lot(eve, source="bought")
        ten_ul = amounts.Given(Decimal("10"), "uL")
        into_released = errors(lambda: amounts.transfer(eve, quarantined, target, ten_ul))
        other = oligo_lot(eve, source="bought")
        release(other)
        amounts.transfer(eve, other, target, ten_ul, reason="Topped up")
        of_m13 = oligo_lot(eve, of=m13, source="bought")
        mixed = errors(lambda: amounts.transfer(eve, of_m13, quarantined, ten_ul))
        sequence = services.create(
            alice, "iki/dna.DNASequence", project,
            {{"bases": "GTAAAACGACGGCCAGTG", "name": "M13 forward, as a sequence"}},
        ).entity
        generic = {{
            name: services.create(
                eve, "iki/lots.GenericLot", project, {{"of": str(of.pk), "concentration": 1}},
                amount={{"value": 25, "unit": "mg"}},
            ).entity
            for name, of in (("sequence", sequence), ("oligo", inotersen), ("again", inotersen))
        }}
        generic_mixed = errors(lambda: amounts.transfer(
            eve, generic["sequence"], generic["oligo"], amounts.Given(Decimal("1"), "mg"),
        ))
        amounts.transfer(
            eve, generic["again"], generic["oligo"], amounts.Given(Decimal("1"), "mg"),
        )
        split = errors(lambda: amounts.split(
            eve, quarantined, 1, amounts.Given(Decimal("1"), "nmol"),
            values={{"oligonucleotide": str(m13.pk)}},
        ))
        (aliquot,) = amounts.split(
            eve, quarantined, 1, amounts.Given(Decimal("1"), "nmol"),
        ).aliquots
        left = amounts.amounts_of([target, quarantined, of_m13, generic["oligo"]])
        moved = {{
            "into_released": into_released,
            "mixed": mixed,
            "generic_mixed": generic_mixed,
            "split": split,
            "aliquot_of": str(services.current_values_of([aliquot])[aliquot.pk]["oligonucleotide"]),
            "inotersen": str(inotersen.entry_id or inotersen.pk),
            "left": {{
                e.hrid: str(left[e.pk].value)
                for e in (target, quarantined, of_m13, generic["oligo"])
            }},
            "hrids": {{
                n: e.hrid for n, e in (
                    ("target", target), ("quarantined", quarantined), ("of_m13", of_m13),
                    ("sequence", sequence), ("generic", generic["oligo"]),
                    ("generic_sequence", generic["sequence"]), ("inotersen", inotersen),
                    ("m13", m13),
                )
            }},
        }}

        # A released lot's dates govern the next withdrawal even if QA hasn't changed
        # its disposition. These are inclusive calendar dates in UTC.
        from datetime import UTC, datetime, timedelta
        today = datetime.now(UTC).date()
        yesterday = today - timedelta(days=1)
        tomorrow = today + timedelta(days=1)
        expired = oligo_lot(eve, source="bought", expires_on=today.isoformat(),
                            retest_on=yesterday.isoformat())
        retest = oligo_lot(eve, source="bought", retest_on=today.isoformat(),
                           expires_on=tomorrow.isoformat())
        available = oligo_lot(eve, source="bought", retest_on=tomorrow.isoformat())
        for dated in (expired, retest, available):
            release(dated)
        one = amounts.Given(Decimal("1"), "nmol")
        def refusal(call, why):
            failed = errors(call)
            assert failed and why in failed["amount"][0].lower(), failed
        refusal(lambda: amounts.draw(eve, expired, one), "expir")
        refusal(lambda: amounts.draw(quinn, expired, one, purpose="qa_sampling"), "expir")
        refusal(lambda: amounts.draw(eve, retest, one), "retest")
        refusal(lambda: amounts.transfer(eve, retest, available, one), "retest")
        refusal(lambda: amounts.split(eve, expired, 1, one), "expir")
        from api_client import Api
        from iki_server.amounts.models import Movement

        # QA is a grant in the owning project. Even an editor with QA elsewhere or
        # the owner project's admin cannot withdraw a sample by changing its purpose.
        other_project = Project.objects.create(team=team, name="Other")
        remote = ProjectMembership.objects.create(project=other_project, user=eve,
                                                  role=ProjectRole.EDITOR)
        LabRoleGrant.objects.create(membership=remote, role="iki/lots.qa")
        admin = User.objects.create_user("project-admin@example.com")
        ProjectMembership.objects.create(project=project, user=admin, role=ProjectRole.ADMIN)
        sample_body = {{"amount": {{"value": 1, "unit": "nmol"}}, "purpose": "qa_sampling"}}
        qa_lots = (quarantined, retest, available)
        balances = amounts.amounts_of(qa_lots)
        before = {{e.pk: (balances[e.pk].value, Movement.objects.filter(entity=e).count())
                  for e in qa_lots}}
        api = Api()
        for user in (eve, admin):
            api.browser.force_login(user)
            for source in qa_lots:
                denied = api.post(f"/entities/{{source.hrid}}/amount/draw", sample_body)
                assert denied.status_code == 403, denied.content
                assert "QA" in denied.json()["detail"], denied.content
        for source in qa_lots:
            assert amounts.amounts_of([source])[source.pk].value == before[source.pk][0]
            assert Movement.objects.filter(entity=source).count() == before[source.pk][1]

        api.browser.force_login(quinn)
        for source in (quarantined, retest):
            page = api.get(f"/entities/{{source.hrid}}").json()
            assert page["can"]["draw_purposes"] == ["routine", "qa_sampling"]
            sampled = api.post(f"/entities/{{source.hrid}}/amount/draw", sample_body)
            assert sampled.status_code == 200, sampled.content
            entry = Movement.objects.get(pk=sampled.json()["movement"]["id"])
            assert (entry.user_id, entry.purpose, entry.change) == (quinn.pk, "qa_sampling", -1)
        amounts.draw(eve, available, one)
        for dated, purposes in ((expired, []), (retest, ["qa_sampling"]), (available, ["routine"])):
            assert list(Movement.objects.filter(entity=dated, kind="drawn")
                        .values_list("purpose", flat=True)) == purposes
            assert amounts.amounts_of([dated])[dated.pk].value == 50 - len(purposes)

        api.browser.force_login(eve)
        detail_response = api.get(f"/entities/{{retest.hrid}}")
        assert detail_response.status_code == 200, detail_response.content
        detail = detail_response.json()
        assert detail["locked"] and not detail["can"]["edit"] and detail["can"]["edit_amount"]
        assert detail["can"]["draw_purposes"] == ["routine"]
        assert list(detail["draw_refusals"]) == ["routine"]
        assert "retest" in detail["draw_refusals"]["routine"].lower()
        clock_dependent = api.call("GET", f"/entities/{{retest.hrid}}",
                                   headers={{"If-None-Match": detail_response["ETag"]}})
        assert clock_dependent.status_code == 200
        blank = services.create(
            eve, "iki/lots.GenericLot", project,
            {{"of": str(inotersen.pk), "expires_on": yesterday.isoformat()}},
        ).entity
        page = api.get(f"/entities/{{blank.hrid}}").json()
        assert page["amount"] is None
        assert set(page["draw_refusals"]) == {{"routine", "qa_sampling"}}
        assert "expir" in page["draw_refusals"]["routine"].lower()

        # A plasmid is a duplex, which its preps' A260 assumes.
        ss = services.create(
            alice, "iki/dna.DNASequence", project,
            {{"bases": "ACGTACGTAC", "topology": "circular", "strandedness": "single"}},
        ).entity
        single = errors(lambda: services.create(
            alice, "iki/plasmid.Plasmid", project, {{"sequence": str(ss.entry_id)}},
        ))
        result = {{"who": who, "mass": mass, "moved": moved, "single": single}}
        """,
    )
    assert result["who"] == {
        "unsaid": "Say whether it was made or bought before releasing it.",
        "nobody": "Say who made it before releasing it.",
        "nobody_by_quinn": "Say who made it before releasing it.",
        "recorded_by_alice": "You recorded this lot, so someone else in QA releases it.",
        "recorded_by_alice_by_quinn": None,
        "bought_by_whoever_recorded_it": None,
        "edited_by_quinn": "You edited this lot's record, so someone else in QA releases it.",
    }
    mass = result["mass"]
    assert mass["within"] == []
    assert mass["unheld"] == ["measured_mass"]
    assert mass["edited"] == ["mass_within_tolerance"]
    assert mass["held_release"] == (
        f"{mass['held_hrid']} is held for review (“Its mass is its oligonucleotide's”): "
        "it can't be set to a state that locks it until its holds are reviewed."
    )
    assert mass["edge"] == []
    assert mass["past"] == ["measured_mass"]
    assert mass["unchecked"] == ["mass_is_checked"]
    moved = result["moved"]
    h = moved["hrids"]
    assert moved["into_released"] == {
        "to": [
            f"{h['target']} is Released, which locks it, so only material that's Released "
            f"too goes into it, and {h['quarantined']} is Quarantined: neither was changed."
        ]
    }
    of_another = "pooling them would put another material in it, so neither was changed."
    assert moved["mixed"] == {
        "to": [
            f"{h['quarantined']} is of {h['inotersen']}, and {h['of_m13']} is of {h['m13']}: "
            f"{of_another} Record a mixture as a record of its own."
        ]
    }
    assert moved["generic_mixed"] == {
        "to": [
            f"{h['generic']} is of {h['inotersen']}, and {h['generic_sequence']} is of "
            f"{h['sequence']}: {of_another} Record a mixture as a record of its own."
        ]
    }
    assert moved["split"] == {
        "values/0/oligonucleotide": [
            f"is what it's split from's: an aliquot of {h['quarantined']} is of "
            f"{h['inotersen']}, as it is."
        ]
    }
    assert moved["aliquot_of"] == moved["inotersen"]
    # 10 uL at 100 uM is 1 nmol: only the released top-up went in, and nothing else moved.
    assert {k: Decimal(v) for k, v in moved["left"].items()} == {
        h["target"]: Decimal(51),
        h["quarantined"]: Decimal(49),
        h["of_m13"]: Decimal(50),
        h["generic"]: Decimal(26),
    }
    assert result["single"] == {
        "sequence": [
            "A plasmid is a double-stranded molecule, and DNA-000002 is single-stranded. "
            "Register a single-stranded circle, such as a phage's genome, as a DNA sequence."
        ]
    }
