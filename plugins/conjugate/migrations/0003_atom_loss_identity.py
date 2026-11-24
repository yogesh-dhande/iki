"""Index conjugates by their isotope counts without changing their records or history."""

from django.db import migrations
from django.db.models import F

from iki_spec.identity import Kind, identity_hash


def reindex(apps, schema_editor, *, canonical):
    db = schema_editor.connection.alias
    entries = apps.get_model("iki_entities", "Entity").objects.using(db)
    carriers = apps.get_model("iki__conjugate", "Conjugate_Entry_carrier").objects.using(db)
    attachments = apps.get_model("iki__conjugate", "Conjugate_Entry_attachments").objects.using(db)
    carrier_kind = Kind(
        "list", items=Kind("row", row=(("kind", Kind("enum")), ("molecule", Kind("ref"))))
    )
    loss = Kind("string", format="atom_loss_formula" if canonical else None)
    attachment_kind = Kind(
        "list",
        items=Kind(
            "row",
            row=(
                ("part", Kind("ref")),
                ("site", Kind("string")),
                ("copies", Kind("int")),
                ("loss_formula", loss),
                ("peg_grade", Kind("string")),
            ),
        ),
    )
    kind = "iki/conjugate.Conjugate"
    live = entries.filter(type=kind, registry_id__isnull=False, purged_at__isnull=True)
    # A previously canonical key keeps its holder. Otherwise prefer the oldest active
    # entry. Existing duplicate IDs, their links and recorded spellings remain intact.
    for entry in live.order_by(
        F("superseded_at").asc(nulls_first=True), "created_at", "pk"
    ).iterator():
        carrier = list(carriers.filter(entity_id=entry.pk).values("kind", "molecule"))
        parts = list(
            attachments.filter(entity_id=entry.pk).values(
                "part", "site", "copies", "loss_formula", "peg_grade"
            )
        )
        digest = identity_hash(
            kind, {"carrier": (carrier_kind, carrier), "attachments": (attachment_kind, parts)}
        )
        if digest == entry.identity_hash:
            continue
        if (
            canonical
            and entries.filter(registry_id=entry.registry_id, identity_hash=digest).exists()
        ):
            continue
        entries.filter(pk=entry.pk).update(identity_hash=digest)


def forwards(apps, schema_editor):
    reindex(apps, schema_editor, canonical=True)


def backwards(apps, schema_editor):
    reindex(apps, schema_editor, canonical=False)


class Migration(migrations.Migration):
    dependencies = [("iki__conjugate", "0002_atom_loss_format")]
    operations = [migrations.RunPython(forwards, backwards)]
