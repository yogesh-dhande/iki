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
            name='Ext_iki_protein_Protein_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('lims_id', iki.TextField(null=True)),
            ],
            table='acme__lims_connector__iki__protein_protein__history',
        ),
        iki.AppendOnly(
            model_name='Ext_iki_protein_Protein_History',
        ),
        iki.CreateModel(
            name='Ext_iki_protein_Protein',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, primary_key=True, related_name='acme_lims_connector_Ext', serialize=False, to='iki__protein.protein')),
                ('lims_id', iki.CharField(db_index=True, max_length=64, null=True)),
            ],
            table='acme__lims_connector__iki__protein_protein',
            indexes=[
                iki.GinIndex(fields=['lims_id'], name='lims_id_trgm_73f707aaca2b2748d', opclasses=['gin_trgm_ops']),
            ],
            constraints=[
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('lims_id'), 1), name='acme__lims_connector__iki__protein_protein__lims_id__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('lims_id__regex', '[\\r\\n]'), negated=True), name='acme__lims_connector__iki__protein_protein__lims_id__one_line'),
            ],
        ),
    ]
