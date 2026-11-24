"""Invalidate inherited evidence on editable plans changed since their recorded check."""

from django.db import migrations
from django.db.models import F, Max, Q
from django.db.models.functions import Now


def invalidate(apps, schema_editor):
    alias = schema_editor.connection.alias
    # Legacy trigger settings could be switched off automatically or without a reason.
    # Only a reasoned project Admin opt-out applies to the new default-on recheck.
    project_trigger = apps.get_model("iki_runs", "ProjectTrigger")
    project_trigger.objects.using(alias).filter(
        Q(disabled_by_id__isnull=True) | Q(disabled_reason=""),
        trigger="iki/plasmid.check_stated_product",
        enabled=False,
    ).update(enabled=True, disabled_at=None, disabled_by_id=None, disabled_reason="")
    assembly = apps.get_model("iki__plasmid", "Assembly")
    history = apps.get_model("iki__plasmid", "Assembly_History")
    revision = apps.get_model("iki_entities", "Revision")
    entity = apps.get_model("iki_entities", "Entity")
    plans = assembly.objects.using(alias).filter(locked_by=[])
    inputs = ["product", "method", "enzymes", "backbone", "inserts"]
    outputs = ["matches_product", "simulated_length", "simulated_topology", "junctions"]
    for plan in plans.exclude(**{f"{field}__isnull": True for field in outputs}).iterator():
        changes = (
            revision.objects.using(alias)
            .filter(entity_id=plan.pk)
            .aggregate(
                edited=Max("number", filter=Q(changed_fields__overlap=inputs)),
                checked=Max("number", filter=Q(changed_fields__contains=["matches_product"])),
            )
        )
        if (changes["edited"] or 0) <= (changes["checked"] or 0):
            continue
        prior = (
            history.objects.using(alias)
            .filter(entity_id=plan.pk)
            .order_by("-revision")
            .values()
            .first()
        )
        if prior is None:
            continue
        prior.pop("pk", None)
        cleared = {field: None for field in outputs}
        cleared["verification_note"] = "Needs re-verification"
        number = plan.revision + 1
        revision.objects.using(alias).create(
            entity_id=plan.pk,
            number=number,
            kind="edited",
            via="system",
            changed_fields=list(cleared),
            reason="The assembly plan changed after its recorded check.",
        )
        assembly.objects.using(alias).filter(pk=plan.pk).update(**cleared)
        entity.objects.using(alias).filter(pk=plan.pk).update(
            revision=F("revision") + 1, updated_at=Now()
        )
        history.objects.using(alias).create(**(prior | cleared | {"revision": number}))


class Migration(migrations.Migration):
    dependencies = [
        ("iki__plasmid", "0007_assembly_verification_note_and_more"),
        ("iki_runs", "0051_editor_recheck"),
    ]
    operations = [migrations.RunPython(invalidate, migrations.RunPython.noop)]
