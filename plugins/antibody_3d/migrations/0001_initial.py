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
            name='Ext_iki_protein_ProteinStructure_Entry_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('immunebuilder_model', iki.TextField(null=True)),
                ('immunebuilder_version', iki.TextField(null=True)),
                ('predicted_error', iki.QuantityField(null=True, unit='Ao')),
                ('cdr_h3_predicted_error', iki.QuantityField(null=True, unit='Ao')),
            ],
            table='iki__antibody_3d__iki__protein_proteinstructure__entry__history',
        ),
        iki.AppendOnly(
            model_name='Ext_iki_protein_ProteinStructure_Entry_History',
        ),
        iki.CreateModel(
            name='Ext_iki_protein_ProteinStructure_Entry',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, primary_key=True, related_name='iki_antibody_3d_Entry', serialize=False, to='iki__protein.proteinstructure_entry')),
                ('immunebuilder_model', iki.TextField(db_index=True, null=True)),
                ('immunebuilder_version', iki.CharField(max_length=20, null=True)),
                ('predicted_error', iki.QuantityField(db_index=True, null=True, unit='Ao')),
                ('cdr_h3_predicted_error', iki.QuantityField(db_index=True, null=True, unit='Ao')),
            ],
            table='iki__antibody_3d__iki__protein_proteinstructure__entry',
            constraints=[
                iki.CheckConstraint(condition=iki.Q(('immunebuilder_model__in', ['abodybuilder2', 'nanobodybuilder2'])), name='iki__antibody_3d__iki__protein_proteinstructure__en__80cf9c7f37'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('immunebuilder_version'), 1), name='iki__antibody_3d__iki__protein_proteinstructure__en__7f4ad9c169'),
                iki.CheckConstraint(condition=iki.Q(('immunebuilder_version__regex', '[\\r\\n]'), negated=True), name='iki__antibody_3d__iki__protein_proteinstructure__en__b83a7124a8'),
                iki.CheckConstraint(condition=iki.Q(('predicted_error__gt', float('-inf')), ('predicted_error__lt', float('inf'))), name='iki__antibody_3d__iki__protein_proteinstructure__en__b37a7c67c7'),
                iki.CheckConstraint(condition=iki.Q(('predicted_error__gte', 0)), name='iki__antibody_3d__iki__protein_proteinstructure__en__b8b3e02d37'),
                iki.CheckConstraint(condition=iki.Q(('cdr_h3_predicted_error__gt', float('-inf')), ('cdr_h3_predicted_error__lt', float('inf'))), name='iki__antibody_3d__iki__protein_proteinstructure__en__67a3151489'),
                iki.CheckConstraint(condition=iki.Q(('cdr_h3_predicted_error__gte', 0)), name='iki__antibody_3d__iki__protein_proteinstructure__en__7668d764e3'),
            ],
        ),
    ]
