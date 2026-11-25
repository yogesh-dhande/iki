"""Invalidate inherited evidence on editable plans changed since their recorded check."""

from django.db import migrations
from django.db.models import Max, Q

from iki_server.plugins.operations import MigrateValues


def restore_default_trigger(apps, schema_editor):
    alias = schema_editor.connection.alias
    # Legacy trigger settings could be switched off automatically or without a reason.
    # Only a reasoned project Admin opt-out applies to the new default-on recheck.
    project_trigger = apps.get_model("iki_runs", "ProjectTrigger")
    project_trigger.objects.using(alias).filter(
        Q(disabled_by_id__isnull=True) | Q(disabled_reason=""),
        trigger="iki/plasmid.check_stated_product",
        enabled=False,
    ).update(enabled=True, disabled_at=None, disabled_by_id=None, disabled_reason="")


def invalidate(plan):
    inputs = ["product", "method", "enzymes", "backbone", "inserts"]
    outputs = ["matches_product", "simulated_length", "simulated_topology", "junctions"]
    if plan.locked_by or all(getattr(plan, field) is None for field in outputs):
        return {}
    changes = plan.revisions.using(plan._state.db).aggregate(
        edited=Max("number", filter=Q(changed_fields__overlap=inputs)),
        checked=Max("number", filter=Q(changed_fields__contains=["matches_product"])),
    )
    if (changes["edited"] or 0) <= (changes["checked"] or 0):
        return {}
    return dict.fromkeys(outputs) | {"verification_note": "Needs re-verification"}


def keep_invalidated(plan):
    return {}


class Migration(migrations.Migration):
    dependencies = [
        ("iki__plasmid", "0007_assembly_verification_note_and_more"),
        ("iki_runs", "0051_editor_recheck"),
        ("iki_entities", "0027_migrated_revisions"),
    ]
    operations = [
        migrations.RunPython(restore_default_trigger, migrations.RunPython.noop),
        MigrateValues(
            "Assembly",
            invalidate,
            backwards=keep_invalidated,
            version="0.5.0",
            migration="0008_invalidate_edited_plans",
            reason="The assembly plan changed after its recorded check.",
        ),
    ]
