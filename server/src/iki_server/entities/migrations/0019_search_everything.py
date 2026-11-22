"""What someone changed lately, newest first: what searching everything shows before
anything is typed (ADR 0176). Built concurrently: revisions are the largest table, and
changes go on meanwhile."""

from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):
    atomic = False

    dependencies = [("iki_entities", "0018_hrid_sorted")]

    operations = [
        AddIndexConcurrently(
            model_name="revision",
            index=models.Index(fields=["user", "id"], name="iki_revision_by_user"),
        ),
    ]
