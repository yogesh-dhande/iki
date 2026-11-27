# Made by `iki plugin makemigrations`. Review it, and commit it with the manifests it
# migrates: once a deployment has applied it, it never changes.

from iki_server.plugins import operations as iki


class Migration(iki.Migration):

    initial = True

    dependencies = [
        ('iki__protein', '0001_initial'),
        ('iki_anchors', '0001_iki_0_1'),
    ]

    operations = [
        iki.CreateModel(
            name='ProteinComplex_Entry_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('complex_portal_id', iki.TextField(null=True)),
            ],
            table='iki__complex_proteincomplex__entry__history',
        ),
        iki.AppendOnly(
            model_name='ProteinComplex_Entry_History',
        ),
        iki.CreateModel(
            name='ProteinComplex_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('structure', iki.UUIDField(db_column='structure_id', null=True)),
            ],
            table='iki__complex_proteincomplex__history',
        ),
        iki.AppendOnly(
            model_name='ProteinComplex_History',
        ),
        iki.CreateModel(
            name='ProteinComplex',
            fields=[
                ('complex_ptr', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to='iki__core.complex')),
                ('structure', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to='iki__protein.proteinstructure_entry')),
            ],
            table='iki__complex_proteincomplex',
            bases=('iki__core.complex',),
        ),
        iki.CreateModel(
            name='ProteinComplex_Entry',
            fields=[
                ('complex_entry_ptr', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to='iki__core.complex_entry')),
                ('complex_portal_id', iki.CharField(db_index=True, max_length=20, null=True)),
            ],
            table='iki__complex_proteincomplex__entry',
            indexes=[
                iki.GinIndex(fields=['complex_portal_id'], name='complex_po_trgm_13bee5c49f9230', opclasses=['gin_trgm_ops']),
            ],
            constraints=[
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('complex_portal_id'), 1), name='iki__complex_proteincomplex__entry__complex_portal___754a1de70b'),
                iki.CheckConstraint(condition=iki.Q(('complex_portal_id__regex', '[\\r\\n]'), negated=True), name='iki__complex_proteincomplex__entry__complex_portal_id__one_line'),
                iki.CheckConstraint(condition=iki.Q(('complex_portal_id__regex', '^(?:^CPX-[1-9][0-9]*$)$')), name='iki__complex_proteincomplex__entry__complex_portal_id__pattern'),
            ],
            bases=('iki__core.complex_entry',),
        ),
    ]
