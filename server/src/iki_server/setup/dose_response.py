"""Example material and published Tox21 measurements for the dose-response starter (ADR 0198)."""

import hashlib

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
from iki_server.setup import public_plate

REQUEST_TYPE = "iki/example_lims.AnalysisRequest"
READER_FILE = public_plate.READER_FILE


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


@transaction.atomic
def seed_example(project: Project, requester_project: Project, *, by: User) -> dict[str, str]:
    """Prepare the compound shipment, the service’s reagent and its mapped plate on first apply.

    The starter application holds the assay project lock and retains these IDs. The coordinator
    receives the compound tubes. The assigned staff run and sign the fits using their own accounts.
    """
    check_example(project, requester_project, by=by)
    if not requests.accepts(project, requester_project, REQUEST_TYPE):
        requests.set_intake(
            project, types=[REQUEST_TYPE], sources=[requester_project], from_registry=False, by=by
        )
    protein = services.create(
        by,
        "iki/protein.Protein",
        project,
        {"name": "Firefly luciferase (Photinus pyralis)", "sequence": public_plate.LUCIFERASE},
    ).entity
    sample = services.create(
        by,
        "iki/example_lims.Sample",
        project,
        {
            "protein": str(protein.pk),
            "lot": "Firefly luciferase assay target",
            "notes": "Target enzyme for the Tox21 flavonoid dilution series (PubChem AID 1224835).",
        },
    ).entity
    reagent_tube = services.create(
        by,
        "iki/example_lims.Tube",
        project,
        {"name": "Firefly luciferase assay target", "sample": str(sample.pk)},
    ).entity
    compound_tubes = {}
    for key, series in public_plate.SERIES.items():
        compound = services.create(
            by, "iki/small_molecule.Compound", requester_project, public_plate.compound_values(key)
        ).entity
        lot = services.create(
            by,
            "iki/lots.GenericLot",
            requester_project,
            {"of": str(compound.pk), "lot_number": f"Tox21 SID {series['sid']}"},
        ).entity
        compound_tube = services.create(
            by,
            "iki/example_lims.Tube",
            requester_project,
            {"name": series["name"], "lot": str(lot.pk)},
        ).entity
        compound_tubes[key] = str(compound_tube.pk)
    saved = services.create(
        by,
        REQUEST_TYPE,
        requester_project,
        {
            "title": public_plate.PLATE_NAME,
            "tubes": list(compound_tubes.values()),
            "instructions": (
                "Fit the IC50 of apigenin and daidzein against the assay group's luciferase. "
                "Use all three replicate series from PubChem AID 1224835."
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
            "name": public_plate.PLATE_NAME,
            "procedure": public_plate.ASSAY,
            "procedure_revision": public_plate.SOURCE["procedure_revision"],
            "size": "plate_96",
            "reader_file": str(reader.pk),
            "request": str(request.pk),
            "reagent_tube": str(reagent_tube.pk),
            "assignments": public_plate.assignments(compound_tubes),
        },
    ).entity
    return {
        **compound_tubes,
        "requester_project": str(requester_project.pk),
        "request": str(request.pk),
        "protein": str(protein.pk),
        "sample": str(sample.pk),
        "tube": str(reagent_tube.pk),
        "plate": str(plate.pk),
        "reader_file": str(reader.pk),
    }
