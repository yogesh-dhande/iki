# The container migration (ADR 0260) regenerated 0001_initial with the bounded word
# index on hold_reason. This migration keeps its name, which later plugins' migrations
# depend on.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('iki__inventory', '0001_initial'),
    ]

    operations = []
