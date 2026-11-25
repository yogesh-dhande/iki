"""Check product seed results in the deployed Django shell before CI runs fixture actions."""

import os
import sys

from iki_server.entities.models import Entity
from iki_server.org.models import Project
from iki_server.runs.models import ActionRun, RunStatus


def check_seed(atlas: Project) -> None:
    missing = []
    # The seed supplies the exact project it created, including on repeated runs.
    records = Entity.objects.filter(
        owner_project__team_id=atlas.team_id, archived_at__isnull=True, purged_at__isnull=True
    )

    def require(kind: str, count: int = 1, *, signature: str = "", returned_from: str = "") -> None:
        project = returned_from or atlas.name
        rows = records.filter(type=kind)
        if signature:
            rows = rows.filter(signatures__state=signature)
        if returned_from:
            rows = rows.filter(
                owner_project__name=returned_from,
                links__project=atlas,
                links__via_request__isnull=False,
            )
        else:
            rows = rows.filter(owner_project=atlas)
        found = rows.distinct().count()
        if found < count:
            details = f" with a {signature!r} signature" if signature else ""
            if returned_from:
                details += f" returned to {atlas.name}"
            missing.append(f"{project}: expected at least {count} {kind}{details}, found {found}")

    # Results of the eleven scenarios previously reached only through the example connector.
    require("iki/equipment.Booking", 3)
    require("iki/samples.Sample", signature="reviewed")
    require("iki/stability.Measurement", signature="signed")
    require("iki/plate_assays.ResearchAssayResult", 2, signature="signed")
    require("iki/reagents.CountStockLot", 2)
    require("iki/qc_release.ResearchCertificate", signature="signed_off")
    require("iki/sequencing.FASTQSummary", signature="signed_off")
    require("iki/sequencing_analysis.SequencingAnalysisResult")
    require("iki/proteomics.ProteomicsResult")
    require("iki/plasmid_sequencing.WholePlasmidAlignment", 2)

    # The assay cores each return both fits. The analytical core also returns the
    # requested protein and sequencing evidence. A private, signed result alone cannot pass.
    require(
        "iki/plate_assays.RequestedResearchAssayResult",
        2,
        signature="signed",
        returned_from="Research plate assays",
    )
    require(
        "iki/plate_assays.RequestedQCAssayResult",
        2,
        signature="released",
        returned_from="QC plate assays",
    )
    require(
        "iki/requests.ResearchDeliverable", signature="signed_off", returned_from="Analytical Core"
    )
    require("iki/requests.QCDeliverable", 2, signature="released", returned_from="Analytical Core")
    require("iki/lots.PlasmidVerification", signature="signed_off")

    # The original product scenarios still execute, including the protein operations
    # that followed the fake hosted prediction. Several produce a shared core type, so
    # check their successful runs rather than count generic alignments and structures.
    expected_actions = {
        "iki/antibody.assemble",
        "iki/conjugate.compute_composition",
        "iki/dna.align_reads",
        "iki/dna.compute_composition",
        "iki/dna.consensus",
        "iki/lots.compute_oligo_lot",
        "iki/mouse_strains.register_mgi_strain",
        "iki/plasmid.annotate_dna_sequence",
        "iki/plasmid.assemble",
        "iki/plasmid.register_plasmid",
        "iki/polymer.inspect_bigsmiles",
        "iki/protein.align_multiple",
        "iki/protein.align_pair",
        "iki/protein.align_reads",
        "iki/protein.assign_secondary_structure",
        "iki/protein.compute_properties",
        "iki/protein.fetch_structures_from_alphafold_db",
        "iki/protein.import_structure",
        "iki/protein.translate",
        "iki/small_molecule.substructure_search",
    }
    completed = ActionRun.objects.filter(project=atlas, status=RunStatus.SUCCEEDED)
    missing.extend(
        f"{atlas.name}: no successful run for {name}"
        for name in sorted(expected_actions.difference(completed.values_list("action", flat=True)))
    )
    # The conjugate and lot scenarios also register oligos. Only this scenario
    # registers nusinersen, then reuses its record from another representation.
    registrations = completed.filter(action="iki/oligo.register_oligonucleotide")
    nusinersen = registrations.filter(
        params__name="nusinersen", effects__entity__type="iki/oligo.Oligonucleotide"
    ).values("effects__entity_id")
    if not registrations.filter(effects__kind="reused", effects__entity_id__in=nusinersen).exists():
        missing.append(f"{atlas.name}: no reused nusinersen oligonucleotide")
    if missing:
        sys.exit("Missing dev seed evidence:\n" + "\n".join(missing))
    sys.stdout.write(f"Verified the domain seed and signed use cases for {atlas.slug}.\n")


check_seed(Project.objects.get(pk=os.environ["IKI_SEED_PROJECT"]))
