"""Search (ADR 0037): pg_trgm, and indexes for finding entities by HRID and
listing them by when they changed.

The indexes are built without locking writes, since iki_entity holds every
entity: hence a migration that isn't one transaction.
"""

from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.operations import AddIndexConcurrently, TrigramExtension
from django.db import migrations, models


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("iki_entities", "0003_revisions_and_projects"),
    ]

    operations = [
        # A trusted extension: the database's owner (the migrator) may create it.
        TrigramExtension(),
        AddIndexConcurrently(
            model_name="entity",
            index=GinIndex(
                fields=["hrid"], opclasses=["gin_trgm_ops"], name="iki_entity_hrid_trgm"
            ),
        ),
        AddIndexConcurrently(
            model_name="entity",
            index=models.Index(fields=["type", "updated_at", "id"], name="iki_entity_updated"),
        ),
    ]
