"""The facility examples' own request and deliverable types (ADR 0235): each service's
fields and evidence checks are its own, its deliverable answers only its requests, and its
checks run again at each signature."""

from pathlib import Path

import pytest
from first_party_dependencies import PLUGINS, ROOT, build_order, withs
from org_world import World
from test_core_facility import (
    EXAMPLES,
    REGULATED,
    REQUESTER,
    REQUESTS,
    RESEARCH,
    _facility,
    _make,
    _report,
    _send,
    _sign,
    _status,
)
from test_plugin_migrations import database, iki, made, migrate, orm, sync
from test_signatures import begin

from iki_server.entities import services
from iki_server.entities.models import Entity, ProjectLink
from iki_server.entities.values import Invalid
from iki_server.org.models import Project
from iki_server.org.services import Refused

__all__ = ["database"]

SERVICES = ROOT / "examples" / "plugins"
EXAMPLES_USED = ("protein_production", "sequencing_service", "analytical_testing")
PRODUCTION = "acme/protein_production."
SEQUENCING = "acme/sequencing_service."
ANALYTICAL = "acme/analytical_testing."


def test_each_service_checks_its_own_evidence(tmp_path: Path, database: str) -> None:
    dependencies = build_order(PLUGINS / "requests", PLUGINS / "protein")
    examples = [SERVICES / name for name in EXAMPLES_USED]
    for example in examples:
        made(example, *withs(dependencies), "--check", "--no-input", cwd=ROOT)
    build = tmp_path / "build"
    built = iki("plugin", "build", *dependencies, *examples, "--output", build, cwd=ROOT)
    assert built.returncode == 0, built.stdout + built.stderr
    migrate(build, database)
    sync(build, database)
    orm(
        build,
        database,
        """
        from test_facility_services import exercise_services
        exercise_services()
        result = {}
        """,
    )


def exercise_services() -> None:
    world, asking = _facility(
        PRODUCTION + "ProductionRequest",
        SEQUENCING + "SequencingRequest",
        ANALYTICAL + "AnalyticalRequest",
    )
    _production(world, asking)
    _sequencing(world, asking)
    _analysis(world, asking)


def _edit(world: World, who: str, entity: Entity, values: dict[str, object]) -> None:
    entity.refresh_from_db()
    services.update(world.users[who], entity, values, expected_revision=entity.revision)
    entity.refresh_from_db()


def _production(world: World, asking: Project) -> None:
    # UniProtKB P00330, Alcohol dehydrogenase 1, Saccharomyces cerevisiae S288c.
    # https://www.uniprot.org/uniprotkb/P00330/entry
    fasta = (EXAMPLES / "P00330.fasta").read_text().splitlines()
    protein = _make(
        world,
        REQUESTER,
        "iki/protein.Protein",
        asking,
        name="Yeast alcohol dehydrogenase 1",
        sequence="".join(fasta[1:]),
        uniprot_accession="P00330",
    )
    other = _make(world, REQUESTER, "iki/protein.Protein", asking, name="Other", sequence="MKV")
    construct = _make(world, REQUESTER, "iki/core.Collection", asking, name="ADH1 construct")
    asked = {"title": "Training: ADH1 production"}
    kind = PRODUCTION + "ProductionRequest"
    with pytest.raises(Invalid, match="positive mass"):
        _make(world, REQUESTER, kind, asking, **asked, records=[str(protein.pk)], requested_mass=0)
    with pytest.raises(Invalid, match="one protein"):
        _make(
            world,
            REQUESTER,
            kind,
            asking,
            **asked,
            records=[str(protein.pk), str(other.pk)],
            requested_mass=1,
        )
    # Its records are the protein to make (`refines`), and nothing else.
    with pytest.raises(Invalid, match=r"must refer to a iki/protein\.Protein"):
        _make(
            world, REQUESTER, kind, asking, **asked, records=[str(construct.pk)], requested_mass=1
        )
    request = _make(
        world, REQUESTER, kind, asking, **asked, records=[str(protein.pk)], requested_mass=1
    )
    _send(world, request, RESEARCH)

    report_path = EXAMPLES / "P00330-production.txt"
    report = _report(world.project, report_path.name, report_path.read_bytes())
    fields = {"request": str(request.pk), "outcome": "fulfilled", "report": report}
    # The deliverable its type extends doesn't answer a production request.
    with pytest.raises(Invalid, match="record a Production deliverable for it"):
        _make(world, "editor", REQUESTS + "Deliverable", world.project, **fields)
    delivered = PRODUCTION + "ProductionDeliverable"
    fields["protein"] = str(protein.pk)
    with pytest.raises(Invalid, match="lot number and a positive delivered mass"):
        _make(world, "editor", delivered, world.project, **fields, delivered_mass=0.8)
    lot = {"lot_number": "TRAINING-ADH1-01", "delivered_mass": 0.8}
    # A protein the request didn't send isn't the core's to name; the example's own tests
    # check the rule for one it can read.
    with pytest.raises(Invalid, match="refers to no entity you can read"):
        _make(
            world, "editor", delivered, world.project, **{**fields, "protein": str(other.pk)}, **lot
        )
    deliverable = _make(world, "editor", delivered, world.project, **fields, **lot)
    assert deliverable.hrid.startswith("PPD-")

    # The request changed since: the reviewer can't sign off the old answer.
    _edit(world, REQUESTER, request, {"records": [str(other.pk)]})
    with pytest.raises(Refused, match="Name the protein the request sent"):
        begin(world, "admin", str(deliverable.pk), "signed_off", field="review")
    _edit(world, REQUESTER, request, {"records": [str(protein.pk)]})
    _sign(world, "admin", deliverable, "signed_off")
    assert ProjectLink.objects.get(entity=deliverable, project=asking).via_request_id == request.pk
    _status(world, request, "admin", "delivered")


def _sequencing(world: World, asking: Project) -> None:
    # ERR13376132 is the public long-read run for natural yeast isolate CEY652.
    # https://www.ncbi.nlm.nih.gov/sra/ERX12747051
    stock = _make(world, REQUESTER, "iki/core.Collection", asking, name="CEY652 stock")
    tube = _make(
        world,
        REQUESTER,
        "iki/inventory.Tube",
        asking,
        name="CEY652",
        contents=str(stock.pk),
        amount={"value": 10, "unit": "uL"},
    )
    kind = SEQUENCING + "SequencingRequest"
    asked = {"title": "Training: long-read sequencing", "method": "long_read"}
    with pytest.raises(Invalid, match="Name the tubes"):
        _make(world, REQUESTER, kind, asking, **asked)
    request = _make(world, REQUESTER, kind, asking, **asked, tubes=[str(tube.pk)])
    _send(world, request, REGULATED)

    report_path = EXAMPLES / "ERR13376132-delivery.txt"
    report = _report(world.project, report_path.name, report_path.read_bytes())
    fields = {
        "request": str(request.pk),
        "outcome": "fulfilled",
        "material": str(tube.pk),
        "report": report,
    }
    with pytest.raises(Invalid, match="record a Sequencing deliverable for it"):
        _make(world, "editor", REQUESTS + "Deliverable", world.project, **fields)
    delivered = SEQUENCING + "SequencingDeliverable"
    with pytest.raises(Invalid, match="Attach the read files or give a public run accession"):
        _make(world, "editor", delivered, world.project, **fields)
    # The type narrows its inherited material to required (ADR 0283).
    with pytest.raises(Invalid, match="material: is required"):
        _make(
            world,
            "editor",
            delivered,
            world.project,
            **{**fields, "material": None},
            run_accession="ERR13376132",
        )
    deliverable = _make(
        world, "editor", delivered, world.project, **fields, run_accession="ERR13376132"
    )
    assert services.current_values(services.specific(deliverable))["run_accession"] == "ERR13376132"
    _sign(world, "editor", deliverable, "submitted")
    _sign(world, "admin", deliverable, "reviewed")
    _sign(world, "viewer", deliverable, "released")
    assert ProjectLink.objects.get(entity=deliverable, project=asking).via_request_id == request.pk
    _status(world, request, "admin", "delivered")


def _analysis(world: World, asking: Project) -> None:
    stock = _make(world, REQUESTER, "iki/core.Collection", asking, name="Antibody lot 12")
    tube = _make(
        world,
        REQUESTER,
        "iki/inventory.Tube",
        asking,
        name="Lot 12 retain",
        contents=str(stock.pk),
        amount={"value": 50, "unit": "uL"},
    )
    kind = ANALYTICAL + "AnalyticalRequest"
    asked = {"title": "Lot 12 release testing", "tubes": [str(tube.pk)]}
    with pytest.raises(Invalid, match="Choose what the core should measure"):
        _make(world, REQUESTER, kind, asking, **asked)
    request = _make(world, REQUESTER, kind, asking, **asked, analyses=["sec_purity"])
    _send(world, request, REGULATED)

    report = _report(world.project, "sec-purity.txt", b"SEC-HPLC, main peak 98.5 %")
    fields = {
        "request": str(request.pk),
        "outcome": "fulfilled",
        "material": str(tube.pk),
        "report": report,
    }
    with pytest.raises(Invalid, match="record an Analytical result for it"):
        _make(world, "editor", REQUESTS + "Deliverable", world.project, **fields)
    fields["result"] = "pass"
    measured = ANALYTICAL + "AnalyticalResult"
    with pytest.raises(Invalid, match="Measure one of the analyses the request asks for"):
        _make(
            world, "editor", measured, world.project, **fields, analysis="endotoxin", endotoxin=0.1
        )
    with pytest.raises(Invalid, match="Record the SEC purity"):
        _make(world, "editor", measured, world.project, **fields, analysis="sec_purity")
    result = _make(
        world, "editor", measured, world.project, **fields, analysis="sec_purity", purity=98.5
    )
    _sign(world, "editor", result, "submitted")

    # The request now asks for something else: the submitted purity no longer answers it.
    _edit(world, REQUESTER, request, {"analyses": ["endotoxin"]})
    with pytest.raises(Refused, match="Measure one of the analyses the request asks for"):
        begin(world, "admin", str(result.pk), "reviewed", field="review")
    _edit(world, REQUESTER, request, {"analyses": ["sec_purity", "endotoxin"]})
    _sign(world, "admin", result, "reviewed")
    _sign(world, "viewer", result, "released")
    assert ProjectLink.objects.get(entity=result, project=asking).via_request_id == request.pk
