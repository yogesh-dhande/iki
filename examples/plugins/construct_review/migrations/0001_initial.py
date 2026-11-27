# Made by `iki plugin makemigrations`. Review it, and commit it with the manifests it
# migrates: once a deployment has applied it, it never changes.

from iki_server.plugins import operations as iki


class Migration(iki.Migration):

    initial = True

    dependencies = [
        ('iki__plasmid', '0001_initial'),
        ('iki_anchors', '0001_iki_0_1'),
    ]

    operations = [
        iki.CreateModel(
            name='ReviewedAssembly_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('status', iki.TextField(null=True)),
            ],
            table='acme__construct_review_reviewedassembly__history',
        ),
        iki.AppendOnly(
            model_name='ReviewedAssembly_History',
        ),
        iki.CreateModel(
            name='ReviewedAssembly',
            fields=[
                ('assembly_ptr', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to='iki__plasmid.assembly')),
                ('status', iki.StateField(db_comment='A state field (ADR 0125): one of planned, checked, built, verified, abandoned.', db_default='planned', locking=['verified'], states=['planned', 'checked', 'built', 'verified', 'abandoned'])),
            ],
            table='acme__construct_review_reviewedassembly',
            constraints=[
                iki.CheckConstraint(condition=iki.Q(('status__in', ['planned', 'checked', 'built', 'verified', 'abandoned'])), name='acme__construct_review_reviewedassembly__status__known'),
            ],
            bases=('iki__plasmid.assembly',),
        ),
    ]
