"""Iki 0.1's anchor: after the last of each of Iki's apps' migrations in 0.1 (ADR 0296)."""

from django.db import migrations

from iki_spec import migration_api


class Migration(migrations.Migration):
    dependencies = migration_api.anchor_dependencies("0.1")
    operations = []
