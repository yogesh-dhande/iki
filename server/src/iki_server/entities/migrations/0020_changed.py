"""Every type's records by when they changed, newest first: the lists' default order across
types (ADR 0176), which the index on the type and the time can't serve. Built concurrently:
the entity table is large, and changes go on meanwhile."""

from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):
    atomic = False

    dependencies = [("iki_entities", "0019_search_everything")]

    operations = [
        AddIndexConcurrently(
            model_name="entity",
            index=models.Index(fields=["updated_at", "id"], name="iki_entity_changed"),
        ),
    ]
