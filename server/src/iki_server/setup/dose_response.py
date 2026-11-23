"""Example material and a MARS export for the dose-response starter (ADR 0198)."""

import hashlib
from pathlib import Path
from typing import Any

from django.db import transaction

from iki_server.accounts.models import User
from iki_server.audit.log import current_agent
from iki_server.entities import requests, services
from iki_server.entities.models import Entity
from iki_server.files import services as files
from iki_server.files import storage
from iki_server.org import permissions
from iki_server.org.models import Project
from iki_server.org.permissions import Action
from iki_server.org.services import Refused

REQUEST_TYPE = "iki/example_lims.AnalysisRequest"
# Packaged with the server for offline setup. The plate plugin's synthetic reader fixture
# and its generating model are documented in plugins/plate_assays/data/reader.md.
READER_FILE = Path(__file__).with_name("data") / "mars-96-series.csv"


def check_example(project: Project, requester_project: Project, *, by: User) -> None:
    """Check the two projects' access and intake without writing records or files."""
    permissions.require(by, Action.PROJECT_MANAGE, project)
    permissions.require(by, Action.PROJECT_EDIT, requester_project)
    if project.pk == requester_project.pk:
        raise Refused("Choose a separate project to request the example assay.")
    if project.registry_id != requester_project.registry_id:
        raise Refused("Choose a requesting project in the assay project's registry.")
    if not (requester_project.allow_sharing and project.allow_sharing):
        raise Refused("Both projects must allow sharing to send a request and return its result.")
    if not storage.configured():
        raise Refused(str(storage.NotConfigured()))
    if requests.accepts(project, requester_project, REQUEST_TYPE):
        return
    intake = requests.intake_of(project)
    if intake.types or intake.sources or intake.from_registry:
        raise Refused(
            "Configure this project's intake to accept analysis requests from the requesting "
            "project, or choose a requester its intake already accepts."
        )


def _assignments(sample: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = [
        {
            "well": f"A{column + 1}",
            "sample": sample,
            "concentration": 2**column,
            "concentration_unit": "nM",
            "replicate": 1,
        }
        for column in range(8)
    ]
    rows.extend(
        {"well": well, "control": control, "replicate": 1}
        for well, control in (("A9", "blank"), ("A10", "reference"))
    )
    return rows


@transaction.atomic
def seed_example(project: Project, requester_project: Project, *, by: User) -> dict[str, str]:
    """Prepare the request, its expected tube and the mapped plate once on first apply.

    The starter application holds the assay project lock and retains these IDs. The coordinator
    receives the tube and the assigned staff run and sign the fit using their own accounts.
    """
    check_example(project, requester_project, by=by)
    if not requests.accepts(project, requester_project, REQUEST_TYPE):
        requests.set_intake(
            project, types=[REQUEST_TYPE], sources=[requester_project], from_registry=False, by=by
        )
    protein = services.create(
        by,
        "iki/protein.Protein",
        requester_project,
        {"name": "Synthetic assay reference peptide", "sequence": "GGGGSGGGGSGGGGS"},
    ).entity
    sample = services.create(
        by,
        "iki/example_lims.Sample",
        requester_project,
        {
            "protein": str(protein.pk),
            "lot": "Synthetic dilution standard",
            "notes": "Practice sample. All concentrations and absorbance readings are simulated.",
        },
    ).entity
    tube = services.create(
        by,
        "iki/example_lims.Tube",
        requester_project,
        {"name": "Synthetic dilution standard", "sample": str(sample.pk)},
    ).entity
    saved = services.create(
        by,
        REQUEST_TYPE,
        requester_project,
        {
            "title": "Example dose-response assay (synthetic)",
            "tubes": [str(tube.pk)],
            "samples": [str(sample.pk)],
            "instructions": (
                "Report the IC50 for this synthetic dilution series. The simulated acquisition "
                "note records a dispensing error in A4. Review the exclusion with the raw data."
            ),
        },
    )
    request = Entity.objects.get(pk=saved.entity.pk)
    requests.submit(by, request, project, expected_revision=request.revision)

    # A failed application rolls back the File row with the entities. The small immutable
    # example's content is keyed by SHA-256, so retrying also reuses any bytes already stored.
    reader = files.add_written(
        READER_FILE,
        name=READER_FILE.name,
        media_type="text/csv",
        project=project,
        user=by,
        agent=current_agent(),
        sha256=hashlib.sha256(READER_FILE.read_bytes()).hexdigest(),
    )
    plate = services.create(
        by,
        "iki/plate_assays.AssayPlate",
        project,
        {
            "name": "Example MARS dose-response plate (synthetic)",
            "size": "plate_96",
            "reader_file": str(reader.pk),
            "request": str(request.pk),
            "assignments": _assignments(str(sample.pk)),
        },
    ).entity
    return {
        "requester_project": str(requester_project.pk),
        "request": str(request.pk),
        "protein": str(protein.pk),
        "sample": str(sample.pk),
        "tube": str(tube.pk),
        "plate": str(plate.pk),
        "reader_file": str(reader.pk),
    }
