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
            name='MSFile_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('raw_file', iki.UUIDField(db_column='raw_file_id', null=True)),
                ('name', iki.TextField(null=True)),
                ('sample', iki.UUIDField(db_column='sample_id', null=True)),
            ],
            table='iki__proteomics_msfile__history',
        ),
        iki.AppendOnly(
            model_name='MSFile_History',
        ),
        iki.CreateModel(
            name='ProteomicsDataset',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('name', iki.CharField(db_index=True, max_length=200)),
                ('archive', iki.CharField(max_length=400, null=True)),
                ('sample', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to=iki.ENTITY)),
            ],
            table='iki__proteomics_proteomicsdataset',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='ProteomicsDataset_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('name', iki.TextField(null=True)),
                ('sample', iki.UUIDField(db_column='sample_id', null=True)),
                ('raw_files', iki.ArrayField(base_field=iki.UUIDField(), null=True)),
                ('archive', iki.TextField(null=True)),
            ],
            table='iki__proteomics_proteomicsdataset__history',
        ),
        iki.AppendOnly(
            model_name='ProteomicsDataset_History',
        ),
        iki.CreateModel(
            name='ProteomicsResult',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('dataset_revision', iki.BigIntegerField(null=True)),
                ('archive', iki.CharField(max_length=400, null=True)),
                ('format', iki.TextField(null=True)),
                ('version', iki.CharField(max_length=120, null=True)),
                ('summary', iki.JSONField(null=True)),
                ('review_flow', iki.TextField(null=True)),
                ('notes', iki.TextField(null=True)),
                ('review', iki.StateField(db_comment='A state field (ADR 0125): one of recorded, signed_off, submitted, reviewed, released.', db_default='recorded', locking=['signed_off', 'submitted', 'reviewed', 'released'], states=['recorded', 'signed_off', 'submitted', 'reviewed', 'released'])),
                ('dataset', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__proteomics.proteomicsdataset')),
            ],
            table='iki__proteomics_proteomicsresult',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='ProteomicsResult_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('dataset', iki.UUIDField(db_column='dataset_id', null=True)),
                ('dataset_revision', iki.BigIntegerField(null=True)),
                ('raw_files', iki.ArrayField(base_field=iki.UUIDField(), null=True)),
                ('archive', iki.TextField(null=True)),
                ('proteins', iki.ArrayField(base_field=iki.UUIDField(), null=True)),
                ('reports', iki.ArrayField(base_field=iki.UUIDField(), null=True)),
                ('format', iki.TextField(null=True)),
                ('version', iki.TextField(null=True)),
                ('summary', iki.JSONField(null=True)),
                ('review_flow', iki.TextField(null=True)),
                ('notes', iki.TextField(null=True)),
                ('review', iki.TextField(null=True)),
            ],
            table='iki__proteomics_proteomicsresult__history',
        ),
        iki.AppendOnly(
            model_name='ProteomicsResult_History',
        ),
        iki.CreateModel(
            name='ProteomicsResult_proteins',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity', 'position')),
                ('entity', iki.ForeignKey(db_column='entity_id', on_delete=iki.CASCADE, related_name='+', to='iki__proteomics.proteomicsresult')),
                ('position', iki.PositiveIntegerField()),
            ],
            table='iki__proteomics_proteomicsresult__proteins',
        ),
        iki.CreateModel(
            name='ProteomicsResult_raw_files',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity', 'position')),
                ('entity', iki.ForeignKey(db_column='entity_id', on_delete=iki.CASCADE, related_name='+', to='iki__proteomics.proteomicsresult')),
                ('position', iki.PositiveIntegerField()),
            ],
            table='iki__proteomics_proteomicsresult__raw_files',
        ),
        iki.CreateModel(
            name='ProteomicsResult_reports',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity', 'position')),
                ('entity', iki.ForeignKey(db_column='entity_id', on_delete=iki.CASCADE, related_name='+', to='iki__proteomics.proteomicsresult')),
                ('position', iki.PositiveIntegerField()),
            ],
            table='iki__proteomics_proteomicsresult__reports',
        ),
        iki.CreateModel(
            name='MSFile',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('name', iki.CharField(max_length=200)),
                ('raw_file', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to=iki.FILE)),
                ('sample', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to=iki.ENTITY)),
            ],
            table='iki__proteomics_msfile',
            constraints=[
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('name'), 1), name='iki__proteomics_msfile__name__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('name__regex', '[\\r\\n]'), negated=True), name='iki__proteomics_msfile__name__one_line'),
            ],
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='ProteomicsDataset_raw_files',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity', 'position')),
                ('entity', iki.ForeignKey(db_column='entity_id', on_delete=iki.CASCADE, related_name='+', to='iki__proteomics.proteomicsdataset')),
                ('position', iki.PositiveIntegerField()),
                ('item', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__proteomics.msfile')),
            ],
            table='iki__proteomics_proteomicsdataset__raw_files',
        ),
        iki.AddIndex(
            model_name='proteomicsdataset',
            index=iki.GinIndex(fields=['name'], name='name_trgm_0fea13ec222334ffae4e', opclasses=['gin_trgm_ops']),
        ),
        iki.AddConstraint(
            model_name='proteomicsdataset',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('name'), 1), name='iki__proteomics_proteomicsdataset__name__not_empty'),
        ),
        iki.AddConstraint(
            model_name='proteomicsdataset',
            constraint=iki.CheckConstraint(condition=iki.Q(('name__regex', '[\\r\\n]'), negated=True), name='iki__proteomics_proteomicsdataset__name__one_line'),
        ),
        iki.AddConstraint(
            model_name='proteomicsdataset',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('archive'), 1), name='iki__proteomics_proteomicsdataset__archive__not_empty'),
        ),
        iki.AddConstraint(
            model_name='proteomicsdataset',
            constraint=iki.CheckConstraint(condition=iki.Q(('archive__regex', '[\\r\\n]'), negated=True), name='iki__proteomics_proteomicsdataset__archive__one_line'),
        ),
        iki.AddConstraint(
            model_name='proteomicsdataset',
            constraint=iki.CheckConstraint(condition=iki.Q(('archive__regex', '^(?:^https://[^ ]+$)$')), name='iki__proteomics_proteomicsdataset__archive__pattern'),
        ),
        iki.AddField(
            model_name='proteomicsresult_proteins',
            name='item',
            field=iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__protein.protein_entry'),
        ),
        iki.AddField(
            model_name='proteomicsresult_raw_files',
            name='item',
            field=iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__proteomics.msfile'),
        ),
        iki.AddField(
            model_name='proteomicsresult_reports',
            name='item',
            field=iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to=iki.FILE),
        ),
        iki.AddIndex(
            model_name='proteomicsresult',
            index=iki.GinIndex(iki.SearchVector(iki.Left('notes', 100000), config='simple'), name='notes_fts_4f8b2f922c5c0dec4032'),
        ),
        iki.AddConstraint(
            model_name='proteomicsresult',
            constraint=iki.CheckConstraint(condition=iki.Q(('dataset_revision__gte', 1)), name='iki__proteomics_proteomicsresult__dataset_revision__min'),
        ),
        iki.AddConstraint(
            model_name='proteomicsresult',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('archive'), 1), name='iki__proteomics_proteomicsresult__archive__not_empty'),
        ),
        iki.AddConstraint(
            model_name='proteomicsresult',
            constraint=iki.CheckConstraint(condition=iki.Q(('archive__regex', '[\\r\\n]'), negated=True), name='iki__proteomics_proteomicsresult__archive__one_line'),
        ),
        iki.AddConstraint(
            model_name='proteomicsresult',
            constraint=iki.CheckConstraint(condition=iki.Q(('format__in', ['mztab_1_0', 'maxquant_txt', 'diann_tsv', 'fragpipe_tsv'])), name='iki__proteomics_proteomicsresult__format__values'),
        ),
        iki.AddConstraint(
            model_name='proteomicsresult',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('version'), 1), name='iki__proteomics_proteomicsresult__version__not_empty'),
        ),
        iki.AddConstraint(
            model_name='proteomicsresult',
            constraint=iki.CheckConstraint(condition=iki.Q(('version__regex', '[\\r\\n]'), negated=True), name='iki__proteomics_proteomicsresult__version__one_line'),
        ),
        iki.AddConstraint(
            model_name='proteomicsresult',
            constraint=iki.CheckConstraint(condition=iki.Q(('review_flow__in', ['research', 'regulated_qc', 'record_only'])), name='iki__proteomics_proteomicsresult__review_flow__values'),
        ),
        iki.AddConstraint(
            model_name='proteomicsresult',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('notes'), 1), name='iki__proteomics_proteomicsresult__notes__not_empty'),
        ),
        iki.AddConstraint(
            model_name='proteomicsresult',
            constraint=iki.CheckConstraint(condition=iki.LessThanOrEqual(iki.Length('notes'), 3000), name='iki__proteomics_proteomicsresult__notes__max_length'),
        ),
        iki.AddConstraint(
            model_name='proteomicsresult',
            constraint=iki.CheckConstraint(condition=iki.Q(('review__in', ['recorded', 'signed_off', 'submitted', 'reviewed', 'released'])), name='iki__proteomics_proteomicsresult__review__known'),
        ),
        iki.AddConstraint(
            model_name='proteomicsdataset_raw_files',
            constraint=iki.CheckConstraint(condition=iki.Q(('position__lt', 300)), name='iki__proteomics_proteomicsdataset__raw_files__posit__65867d58cb'),
        ),
        iki.AddConstraint(
            model_name='proteomicsresult_proteins',
            constraint=iki.CheckConstraint(condition=iki.Q(('position__lt', 200)), name='iki__proteomics_proteomicsresult__proteins__position__max_items'),
        ),
        iki.AddConstraint(
            model_name='proteomicsresult_raw_files',
            constraint=iki.CheckConstraint(condition=iki.Q(('position__lt', 300)), name='iki__proteomics_proteomicsresult__raw_files__positi__cd80f96ae8'),
        ),
        iki.AddConstraint(
            model_name='proteomicsresult_reports',
            constraint=iki.CheckConstraint(condition=iki.Q(('position__lt', 8)), name='iki__proteomics_proteomicsresult__reports__position__max_items'),
        ),
    ]
