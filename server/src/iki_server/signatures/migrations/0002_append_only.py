from django.db import migrations

import iki_server.plugins.operations


class Migration(migrations.Migration):
    """Signatures, their withdrawals, people's acknowledgments and the organization's
    certifications are records under 21 CFR Part 11: triggers refuse to change or delete
    them, for every role, the tables' owner included (ADR 0025). Whatever gets past that,
    a verification still finds (ADR 0126)."""

    dependencies = [
        ("iki_entities", "0002_history_append_only"),
        ("iki_signatures", "0001_signatures"),
    ]

    operations = [
        iki_server.plugins.operations.AppendOnly("Signature"),
        iki_server.plugins.operations.AppendOnly("Withdrawal"),
        iki_server.plugins.operations.AppendOnly("Acknowledgment"),
        iki_server.plugins.operations.AppendOnly("Certification"),
    ]
