# Made by `iki plugin makemigrations`. Review it, and commit it with the manifests it
# migrates: once a deployment has applied it, it never changes.

from iki_server.plugins import operations as iki


class Migration(iki.Migration):

    initial = True

    dependencies = [
        ('iki__antibody', '0001_initial'),
        ('iki__conjugate', '0001_initial'),
        ('iki_anchors', '0001_iki_0_1'),
    ]

    operations = [
        iki.CreateModel(
            name='MSResult',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('report_sha256', iki.CharField(max_length=64, null=True)),
                ('analysis', iki.TextField(null=True)),
                ('report_format', iki.TextField(null=True)),
                ('source_software', iki.CharField(max_length=200, null=True)),
                ('comparison', iki.JSONField(null=True)),
                ('input_revisions', iki.JSONField(null=True)),
                ('review_flow', iki.TextField(null=True)),
                ('notes', iki.TextField(null=True)),
                ('review', iki.StateField(db_comment='A state field (ADR 0125): one of recorded, signed_off, submitted, reviewed, released.', db_default='recorded', locking=['signed_off', 'submitted', 'reviewed', 'released'], states=['recorded', 'signed_off', 'submitted', 'reviewed', 'released'])),
                ('adc', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to='iki__conjugate.conjugate_entry')),
                ('antibody', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__antibody.antibody_entry')),
                ('lot', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to=iki.ENTITY)),
                ('report', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to=iki.FILE)),
            ],
            table='iki__biologics_ms_msresult',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='MSResult_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('antibody', iki.UUIDField(db_column='antibody_id', null=True)),
                ('adc', iki.UUIDField(db_column='adc_id', null=True)),
                ('lot', iki.UUIDField(db_column='lot_id', null=True)),
                ('report', iki.UUIDField(db_column='report_id', null=True)),
                ('report_sha256', iki.TextField(null=True)),
                ('analysis', iki.TextField(null=True)),
                ('report_format', iki.TextField(null=True)),
                ('source_reports', iki.ArrayField(base_field=iki.UUIDField(), null=True)),
                ('source_software', iki.TextField(null=True)),
                ('comparison', iki.JSONField(null=True)),
                ('input_revisions', iki.JSONField(null=True)),
                ('review_flow', iki.TextField(null=True)),
                ('raw_files', iki.ArrayField(base_field=iki.UUIDField(), null=True)),
                ('notes', iki.TextField(null=True)),
                ('review', iki.TextField(null=True)),
            ],
            table='iki__biologics_ms_msresult__history',
        ),
        iki.AppendOnly(
            model_name='MSResult_History',
        ),
        iki.CreateModel(
            name='MSResult_raw_files',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity', 'position')),
                ('entity', iki.ForeignKey(db_column='entity_id', on_delete=iki.CASCADE, related_name='+', to='iki__biologics_ms.msresult')),
                ('position', iki.PositiveIntegerField()),
                ('item', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to=iki.FILE)),
            ],
            table='iki__biologics_ms_msresult__raw_files',
        ),
        iki.CreateModel(
            name='MSResult_source_reports',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity', 'position')),
                ('entity', iki.ForeignKey(db_column='entity_id', on_delete=iki.CASCADE, related_name='+', to='iki__biologics_ms.msresult')),
                ('position', iki.PositiveIntegerField()),
                ('item', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to=iki.FILE)),
            ],
            table='iki__biologics_ms_msresult__source_reports',
        ),
        iki.AddIndex(
            model_name='msresult',
            index=iki.GinIndex(iki.SearchVector(iki.Left('notes', 100000), config='simple'), name='notes_fts_eda3dbf952ce3643e5a5'),
        ),
        iki.AddConstraint(
            model_name='msresult',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('report_sha256'), 1), name='iki__biologics_ms_msresult__report_sha256__not_empty'),
        ),
        iki.AddConstraint(
            model_name='msresult',
            constraint=iki.CheckConstraint(condition=iki.Q(('report_sha256__regex', '[\\r\\n]'), negated=True), name='iki__biologics_ms_msresult__report_sha256__one_line'),
        ),
        iki.AddConstraint(
            model_name='msresult',
            constraint=iki.CheckConstraint(condition=iki.Q(('analysis__in', ['mass', 'peptide_map'])), name='iki__biologics_ms_msresult__analysis__values'),
        ),
        iki.AddConstraint(
            model_name='msresult',
            constraint=iki.CheckConstraint(condition=iki.Q(('report_format__in', ['iki_mass_table_1', 'iki_peptide_map_1'])), name='iki__biologics_ms_msresult__report_format__values'),
        ),
        iki.AddConstraint(
            model_name='msresult',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('source_software'), 1), name='iki__biologics_ms_msresult__source_software__not_empty'),
        ),
        iki.AddConstraint(
            model_name='msresult',
            constraint=iki.CheckConstraint(condition=iki.Q(('source_software__regex', '[\\r\\n]'), negated=True), name='iki__biologics_ms_msresult__source_software__one_line'),
        ),
        iki.AddConstraint(
            model_name='msresult',
            constraint=iki.CheckConstraint(condition=iki.Q(('review_flow__in', ['research', 'regulated_qc', 'record_only'])), name='iki__biologics_ms_msresult__review_flow__values'),
        ),
        iki.AddConstraint(
            model_name='msresult',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('notes'), 1), name='iki__biologics_ms_msresult__notes__not_empty'),
        ),
        iki.AddConstraint(
            model_name='msresult',
            constraint=iki.CheckConstraint(condition=iki.LessThanOrEqual(iki.Length('notes'), 3000), name='iki__biologics_ms_msresult__notes__max_length'),
        ),
        iki.AddConstraint(
            model_name='msresult',
            constraint=iki.CheckConstraint(condition=iki.Q(('review__in', ['recorded', 'signed_off', 'submitted', 'reviewed', 'released'])), name='iki__biologics_ms_msresult__review__known'),
        ),
        iki.AddConstraint(
            model_name='msresult_raw_files',
            constraint=iki.CheckConstraint(condition=iki.Q(('position__lt', 20)), name='iki__biologics_ms_msresult__raw_files__position__max_items'),
        ),
        iki.AddConstraint(
            model_name='msresult_source_reports',
            constraint=iki.CheckConstraint(condition=iki.Q(('position__lt', 8)), name='iki__biologics_ms_msresult__source_reports__position__max_items'),
        ),
    ]
