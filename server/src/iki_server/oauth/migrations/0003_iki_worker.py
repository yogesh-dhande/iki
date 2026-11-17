"""Once added Iki's worker as an OAuth client, for signing in on someone's own computer
(ADR 0058). Self-hosted workers replaced that (ADR 0112): they register with a token a
compute admin makes, so there's no such client. Kept so the migrations after it apply."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("iki_oauth", "0002_iki_cli")]
    operations = [migrations.RunPython(migrations.RunPython.noop, migrations.RunPython.noop)]
