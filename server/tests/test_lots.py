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
from test_plugin_migrations import database, iki, migrate, orm, sync

from iki_spec import schemas
from iki_spec.validate import load_plugin

ROOT = Path(__file__).resolve().parents[2]
PLUGINS = ROOT / "plugins"
LOTS = PLUGINS / "lots"
DEPENDENCIES = [PLUGINS / n for n in ("dna", "protein", "oligo", "plasmid")]
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

__all__ = ["database"]


def _lots() -> Any:
    spec = importlib.util.spec_from_file_location("lots_script", LOTS / "scripts" / "lots.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lots = _lots()


def test_every_kind_of_lot_is_a_lot_with_its_own_amount() -> None:
    result = load_plugin(LOTS, dependencies=DEPENDENCIES)
    assert result.problems == []
    assert result.plugin is not None
    types = result.plugin.types
    lot = types["iki/lots.Lot"]
    assert lot.abstract and lot.amount is None
    concrete = {name.rsplit(".", 1)[1]: t for name, t in types.items() if not t.abstract}
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
    # entity couldn't have one unit for them all.
    biologic = types["iki/lots.BiologicLot"].amount
    assert biologic is not None
    units = {name: (t.amount or biologic).unit for name, t in concrete.items()}
    assert units == {
        "GenericLot": "mg",
        "OligoLot": "nmol",
        "PlasmidPrep": "ug",
        "PolymerLot": "mg",
        "ProteinLot": "mg",
        "ConjugateLot": "mg",
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
        "PolymerLot": ["iki/core.Entity"],
        # A protein, or a complex of them: an antibody, an Fc fusion, a multispecific.
        "ProteinLot": ["iki/protein.Protein", "iki/core.Complex"],
        "ConjugateLot": ["iki/core.Entity"],
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
        "not_its_maker",
        "a_person",
    }
    assert {s for st in states.values() for s in st.set_by} == {"iki/lots.qa"}


def test_each_kind_of_lot_works_out_its_values_when_its_recorded_and_measured() -> None:
    result = load_plugin(LOTS, dependencies=DEPENDENCIES)
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


def test_a_reduced_dar_is_twice_each_chains_mean() -> None:
    """Agilent 5991-6559EN, Table 1: Adcetris reduced, LC 43.3/56.7, HC 19.9/36.1/25.4/18.6,
    weighted average DAR 4.0: 2 × (0.567 + 1.427)."""
    rows = [("light", 0, 43.3), ("light", 1, 56.7)] + [
        ("heavy", k, a) for k, a in ((0, 19.9), (1, 36.1), (2, 25.4), (3, 18.6))
    ]
    dar, unconjugated, how = lots.dar([{"chain": c, "drug_load": k, "area": a} for c, k, a in rows])
    assert dar == pytest.approx(3.988)
    assert unconjugated is None
    assert how == "2 × (0.5670 light + 1.4270 heavy)"


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
    built = iki("plugin", "build", *DEPENDENCIES, COMPLEX, LOTS, "--output", out, cwd=ROOT)
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
        amounts.draw(alice, made, amounts.Given(Decimal("50"), "uL"), reason="Dosed")
        left = str(amounts.amounts_of([made])[made.pk].value)

        # A run finds its mass 0.88 Da light of inotersen's average: within 2 Da.
        made.refresh_from_db()
        services.update(
            alice, made, {{"expected_mass": 7183.08, "mass_error": -0.88}},
            expected_revision=made.revision, by_run=True,
        )
        within = [h.rule for h in holds.open_holds(made)]
        # Another lot, whose mass is volanesorsen's: held with the run as its reason.
        mixed = services.create(
            alice, "iki/lots.OligoLot", project,
            {{"oligonucleotide": str(oligo.pk), "mass_basis": "average",
              "measured_mass": 7164.3}},
            amount={{"value": 200, "unit": "nmol"}},
        ).entity
        mixed.refresh_from_db()
        mass = {{"expected_mass": 7183.08, "mass_error": -18.78}}
        refused_unheld = errors(lambda: services.update(
            alice, mixed, mass, expected_revision=mixed.revision, by_run=True,
        ))
        mixed.refresh_from_db()
        services.update(
            alice, mixed, mass, expected_revision=mixed.revision, by_run=True,
            hold_reason="Run RUN-000001's results",
        )
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
        amounts.draw(alice, prep, amounts.Given(Decimal("2"), "ug"))
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
        polymer = errors(lambda: services.create(
            alice, "iki/lots.PolymerLot", project,
            {{"polymer": str(puc.pk), "mn": 38560, "mw": 29184}},
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
            "polymer": polymer,
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
    assert result["polymer"] == {
        "mw": [
            "Its Mw, 29184 g/mol, is below its Mn, 38560 g/mol: a weight average is never "
            "below the number average of the same chains."
        ]
    }
    assert result["lots"] == [
        "iki/lots.GenericLot",
        "iki/lots.OligoLot",
        "iki/lots.OligoLot",
        "iki/lots.PlasmidPrep",
        "iki/lots.ProteinLot",
    ]
    assert result["hrids"] == ["GLOT", "OLOT", "OLOT", "PLOT", "PREP"]
