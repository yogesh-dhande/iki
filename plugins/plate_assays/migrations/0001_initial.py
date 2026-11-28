# Made by `iki plugin makemigrations`. Review it, and commit it with the manifests it
# migrates: once a deployment has applied it, it never changes.

from iki_server.plugins import operations as iki


class Migration(iki.Migration):

    initial = True

    dependencies = [
        ('iki__inventory', '0001_initial'),
        ('iki__requests', '0001_initial'),
        ('iki_anchors', '0001_iki_0_1'),
    ]

    operations = [
        iki.CreateModel(
            name='AssayPlate',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('procedure', iki.CharField(max_length=200, null=True)),
                ('procedure_revision', iki.CharField(max_length=80, null=True)),
                ('name', iki.CharField(max_length=200)),
                ('size', iki.TextField()),
                ('reader_format', iki.TextField(db_default='bmg_mars_absorbance')),
                ('preview', iki.JSONField(null=True)),
                ('committed', iki.BooleanField(null=True)),
                ('committed_layout', iki.JSONField(null=True)),
                ('committed_reads', iki.JSONField(null=True)),
                ('reader_file', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to=iki.FILE)),
                ('reagent_tube', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to='iki__inventory.tube')),
                ('request', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to=iki.ENTITY)),
            ],
            table='iki__plate_assays_assayplate',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='AssayPlate_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('procedure', iki.TextField(null=True)),
                ('procedure_revision', iki.TextField(null=True)),
                ('name', iki.TextField(null=True)),
                ('size', iki.TextField(null=True)),
                ('reader_format', iki.TextField(null=True)),
                ('reader_file', iki.UUIDField(db_column='reader_file_id', null=True)),
                ('request', iki.UUIDField(db_column='request_id', null=True)),
                ('reagent_tube', iki.UUIDField(db_column='reagent_tube_id', null=True)),
                ('assignments', iki.JSONField(null=True)),
                ('preview', iki.JSONField(null=True)),
                ('committed', iki.BooleanField(null=True)),
                ('committed_layout', iki.JSONField(null=True)),
                ('committed_reads', iki.JSONField(null=True)),
            ],
            table='iki__plate_assays_assayplate__history',
        ),
        iki.AppendOnly(
            model_name='AssayPlate_History',
        ),
        iki.CreateModel(
            name='AssayResult',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('procedure', iki.CharField(max_length=200, null=True)),
                ('procedure_revision', iki.CharField(max_length=80, null=True)),
                ('points', iki.JSONField(null=True)),
                ('channel', iki.CharField(max_length=100, null=True)),
                ('response_unit', iki.TextField(null=True)),
                ('concentration_unit', iki.TextField(null=True)),
                ('endpoint_kind', iki.TextField(null=True)),
                ('endpoint_value', iki.FloatField(null=True)),
                ('ci_low', iki.FloatField(null=True)),
                ('ci_high', iki.FloatField(null=True)),
                ('ci_method', iki.CharField(max_length=200, null=True)),
                ('bottom', iki.FloatField(null=True)),
                ('top', iki.FloatField(null=True)),
                ('hill_slope', iki.FloatField(null=True)),
                ('normalization', iki.CharField(max_length=100, null=True)),
                ('fit_version', iki.CharField(max_length=100, null=True)),
                ('fit_reason', iki.TextField(null=True)),
                ('fit_status', iki.TextField(null=True)),
                ('qualifier', iki.TextField(null=True)),
                ('point_count', iki.BigIntegerField(null=True)),
                ('review_flow', iki.TextField(null=True)),
                ('notes', iki.TextField(null=True)),
                ('sample', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to=iki.ENTITY)),
            ],
            table='iki__plate_assays_assayresult',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='AssayResult_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('procedure', iki.TextField(null=True)),
                ('procedure_revision', iki.TextField(null=True)),
                ('read', iki.UUIDField(db_column='read_id', null=True)),
                ('sample', iki.UUIDField(db_column='sample_id', null=True)),
                ('measurements', iki.ArrayField(base_field=iki.UUIDField(), null=True)),
                ('points', iki.JSONField(null=True)),
                ('channel', iki.TextField(null=True)),
                ('response_unit', iki.TextField(null=True)),
                ('concentration_unit', iki.TextField(null=True)),
                ('endpoint_kind', iki.TextField(null=True)),
                ('endpoint_value', iki.FloatField(null=True)),
                ('ci_low', iki.FloatField(null=True)),
                ('ci_high', iki.FloatField(null=True)),
                ('ci_method', iki.TextField(null=True)),
                ('bottom', iki.FloatField(null=True)),
                ('top', iki.FloatField(null=True)),
                ('hill_slope', iki.FloatField(null=True)),
                ('normalization', iki.TextField(null=True)),
                ('fit_version', iki.TextField(null=True)),
                ('fit_reason', iki.TextField(null=True)),
                ('fit_status', iki.TextField(null=True)),
                ('qualifier', iki.TextField(null=True)),
                ('point_count', iki.BigIntegerField(null=True)),
                ('review_flow', iki.TextField(null=True)),
                ('notes', iki.TextField(null=True)),
            ],
            table='iki__plate_assays_assayresult__history',
        ),
        iki.AppendOnly(
            model_name='AssayResult_History',
        ),
        iki.CreateModel(
            name='PlateRead',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('procedure', iki.CharField(max_length=200, null=True)),
                ('procedure_revision', iki.CharField(max_length=80, null=True)),
                ('name', iki.CharField(max_length=200, null=True)),
                ('import_key', iki.CharField(db_index=True, max_length=36, null=True)),
                ('reader_format', iki.CharField(max_length=100, null=True)),
                ('acquisition', iki.JSONField(null=True)),
                ('plate_revision', iki.BigIntegerField(null=True)),
                ('plate', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__plate_assays.assayplate')),
                ('reader_file', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to=iki.FILE)),
                ('request', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to=iki.ENTITY)),
            ],
            table='iki__plate_assays_plateread',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='PlateRead_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('procedure', iki.TextField(null=True)),
                ('procedure_revision', iki.TextField(null=True)),
                ('plate', iki.UUIDField(db_column='plate_id', null=True)),
                ('name', iki.TextField(null=True)),
                ('import_key', iki.TextField(null=True)),
                ('reader_format', iki.TextField(null=True)),
                ('acquisition', iki.JSONField(null=True)),
                ('plate_revision', iki.BigIntegerField(null=True)),
                ('reader_file', iki.UUIDField(db_column='reader_file_id', null=True)),
                ('request', iki.UUIDField(db_column='request_id', null=True)),
                ('measurements', iki.ArrayField(base_field=iki.UUIDField(), null=True)),
            ],
            table='iki__plate_assays_plateread__history',
        ),
        iki.AppendOnly(
            model_name='PlateRead_History',
        ),
        iki.CreateModel(
            name='QCAssayResult_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('review', iki.TextField(null=True)),
            ],
            table='iki__plate_assays_qcassayresult__history',
        ),
        iki.AppendOnly(
            model_name='QCAssayResult_History',
        ),
        iki.CreateModel(
            name='RequestedQCAssayResult_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('request', iki.UUIDField(db_column='request_id', null=True)),
            ],
            table='iki__plate_assays_requestedqcassayresult__history',
        ),
        iki.AppendOnly(
            model_name='RequestedQCAssayResult_History',
        ),
        iki.CreateModel(
            name='RequestedResearchAssayResult_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('request', iki.UUIDField(db_column='request_id', null=True)),
            ],
            table='iki__plate_assays_requestedresearchassayresult__history',
        ),
        iki.AppendOnly(
            model_name='RequestedResearchAssayResult_History',
        ),
        iki.CreateModel(
            name='ResearchAssayResult_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('review', iki.TextField(null=True)),
            ],
            table='iki__plate_assays_researchassayresult__history',
        ),
        iki.AppendOnly(
            model_name='ResearchAssayResult_History',
        ),
        iki.CreateModel(
            name='WellMeasurement_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('plate', iki.UUIDField(db_column='plate_id', null=True)),
                ('plate_revision', iki.BigIntegerField(null=True)),
                ('well', iki.TextField(null=True)),
                ('sample', iki.UUIDField(db_column='sample_id', null=True)),
                ('control', iki.TextField(null=True)),
                ('concentration', iki.FloatField(null=True)),
                ('concentration_unit', iki.TextField(null=True)),
                ('replicate', iki.BigIntegerField(null=True)),
                ('channel', iki.TextField(null=True)),
                ('readout', iki.TextField(null=True)),
                ('signal', iki.QuantityField(null=True, unit='1')),
                ('activity_percent', iki.QuantityField(null=True, unit='%')),
                ('reader_file', iki.UUIDField(db_column='reader_file_id', null=True)),
                ('source_row', iki.BigIntegerField(null=True)),
            ],
            table='iki__plate_assays_wellmeasurement__history',
        ),
        iki.AppendOnly(
            model_name='WellMeasurement_History',
        ),
        iki.CreateModel(
            name='AssayResult_measurements',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity', 'position')),
                ('entity', iki.ForeignKey(db_column='entity_id', on_delete=iki.CASCADE, related_name='+', to='iki__plate_assays.assayresult')),
                ('position', iki.PositiveIntegerField()),
            ],
            table='iki__plate_assays_assayresult__measurements',
        ),
        iki.CreateModel(
            name='QCAssayResult',
            fields=[
                ('assayresult_ptr', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to='iki__plate_assays.assayresult')),
                ('review', iki.StateField(db_comment='A state field (ADR 0125): one of recorded, submitted, reviewed, released.', db_default='recorded', locking=['submitted', 'reviewed', 'released'], states=['recorded', 'submitted', 'reviewed', 'released'])),
            ],
            table='iki__plate_assays_qcassayresult',
            bases=('iki__plate_assays.assayresult',),
        ),
        iki.CreateModel(
            name='ResearchAssayResult',
            fields=[
                ('assayresult_ptr', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to='iki__plate_assays.assayresult')),
                ('review', iki.StateField(db_comment='A state field (ADR 0125): one of recorded, signed.', db_default='recorded', locking=['signed'], states=['recorded', 'signed'])),
            ],
            table='iki__plate_assays_researchassayresult',
            bases=('iki__plate_assays.assayresult',),
        ),
        iki.CreateModel(
            name='PlateRead_measurements',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity', 'position')),
                ('entity', iki.ForeignKey(db_column='entity_id', on_delete=iki.CASCADE, related_name='+', to='iki__plate_assays.plateread')),
                ('position', iki.PositiveIntegerField()),
            ],
            table='iki__plate_assays_plateread__measurements',
        ),
        iki.AddField(
            model_name='assayresult',
            name='read',
            field=iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__plate_assays.plateread'),
        ),
        iki.CreateModel(
            name='WellMeasurement',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('plate_revision', iki.BigIntegerField(null=True)),
                ('well', iki.CharField(max_length=4, null=True)),
                ('control', iki.CharField(max_length=100, null=True)),
                ('concentration', iki.FloatField(null=True)),
                ('concentration_unit', iki.TextField(null=True)),
                ('replicate', iki.BigIntegerField(null=True)),
                ('channel', iki.CharField(max_length=100, null=True)),
                ('readout', iki.TextField(null=True)),
                ('signal', iki.QuantityField(null=True, unit='1')),
                ('activity_percent', iki.QuantityField(null=True, unit='%')),
                ('source_row', iki.BigIntegerField(null=True)),
                ('plate', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__plate_assays.assayplate')),
                ('reader_file', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to=iki.FILE)),
                ('sample', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to=iki.ENTITY)),
            ],
            table='iki__plate_assays_wellmeasurement',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='AssayPlate_assignments',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity', 'position')),
                ('entity', iki.ForeignKey(db_column='entity_id', on_delete=iki.CASCADE, related_name='+', to='iki__plate_assays.assayplate')),
                ('position', iki.PositiveIntegerField()),
                ('well', iki.CharField(max_length=4)),
                ('control', iki.CharField(max_length=100, null=True)),
                ('replicate', iki.BigIntegerField(db_default=1)),
                ('concentration', iki.FloatField(null=True)),
                ('concentration_unit', iki.TextField(null=True)),
                ('sample', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to=iki.ENTITY)),
            ],
            table='iki__plate_assays_assayplate__assignments',
        ),
        iki.AddConstraint(
            model_name='assayplate',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('procedure'), 1), name='iki__plate_assays_assayplate__procedure__not_empty'),
        ),
        iki.AddConstraint(
            model_name='assayplate',
            constraint=iki.CheckConstraint(condition=iki.Q(('procedure__regex', '[\\r\\n]'), negated=True), name='iki__plate_assays_assayplate__procedure__one_line'),
        ),
        iki.AddConstraint(
            model_name='assayplate',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('procedure_revision'), 1), name='iki__plate_assays_assayplate__procedure_revision__not_empty'),
        ),
        iki.AddConstraint(
            model_name='assayplate',
            constraint=iki.CheckConstraint(condition=iki.Q(('procedure_revision__regex', '[\\r\\n]'), negated=True), name='iki__plate_assays_assayplate__procedure_revision__one_line'),
        ),
        iki.AddConstraint(
            model_name='assayplate',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('name'), 1), name='iki__plate_assays_assayplate__name__not_empty'),
        ),
        iki.AddConstraint(
            model_name='assayplate',
            constraint=iki.CheckConstraint(condition=iki.Q(('name__regex', '[\\r\\n]'), negated=True), name='iki__plate_assays_assayplate__name__one_line'),
        ),
        iki.AddConstraint(
            model_name='assayplate',
            constraint=iki.CheckConstraint(condition=iki.Q(('size__in', ['plate_96', 'plate_384'])), name='iki__plate_assays_assayplate__size__values'),
        ),
        iki.AddConstraint(
            model_name='assayplate',
            constraint=iki.CheckConstraint(condition=iki.Q(('reader_format__in', ['bmg_mars_absorbance', 'iki_plate_matrix'])), name='iki__plate_assays_assayplate__reader_format__values'),
        ),
        iki.AddField(
            model_name='assayresult_measurements',
            name='item',
            field=iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__plate_assays.wellmeasurement'),
        ),
        iki.CreateModel(
            name='RequestedQCAssayResult',
            fields=[
                ('qcassayresult_ptr', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to='iki__plate_assays.qcassayresult')),
            ],
            table='iki__plate_assays_requestedqcassayresult',
            bases=('iki__plate_assays.qcassayresult',),
        ),
        iki.AddConstraint(
            model_name='qcassayresult',
            constraint=iki.CheckConstraint(condition=iki.Q(('review__in', ['recorded', 'submitted', 'reviewed', 'released'])), name='iki__plate_assays_qcassayresult__review__known'),
        ),
        iki.CreateModel(
            name='RequestedResearchAssayResult',
            fields=[
                ('researchassayresult_ptr', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to='iki__plate_assays.researchassayresult')),
            ],
            table='iki__plate_assays_requestedresearchassayresult',
            bases=('iki__plate_assays.researchassayresult',),
        ),
        iki.AddConstraint(
            model_name='researchassayresult',
            constraint=iki.CheckConstraint(condition=iki.Q(('review__in', ['recorded', 'signed'])), name='iki__plate_assays_researchassayresult__review__known'),
        ),
        iki.AddField(
            model_name='plateread_measurements',
            name='item',
            field=iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__plate_assays.wellmeasurement'),
        ),
        iki.AddIndex(
            model_name='plateread',
            index=iki.GinIndex(fields=['import_key'], name='import_key_trgm_f84cab512cc7a8', opclasses=['gin_trgm_ops']),
        ),
        iki.AddConstraint(
            model_name='plateread',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('procedure'), 1), name='iki__plate_assays_plateread__procedure__not_empty'),
        ),
        iki.AddConstraint(
            model_name='plateread',
            constraint=iki.CheckConstraint(condition=iki.Q(('procedure__regex', '[\\r\\n]'), negated=True), name='iki__plate_assays_plateread__procedure__one_line'),
        ),
        iki.AddConstraint(
            model_name='plateread',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('procedure_revision'), 1), name='iki__plate_assays_plateread__procedure_revision__not_empty'),
        ),
        iki.AddConstraint(
            model_name='plateread',
            constraint=iki.CheckConstraint(condition=iki.Q(('procedure_revision__regex', '[\\r\\n]'), negated=True), name='iki__plate_assays_plateread__procedure_revision__one_line'),
        ),
        iki.AddConstraint(
            model_name='plateread',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('name'), 1), name='iki__plate_assays_plateread__name__not_empty'),
        ),
        iki.AddConstraint(
            model_name='plateread',
            constraint=iki.CheckConstraint(condition=iki.Q(('name__regex', '[\\r\\n]'), negated=True), name='iki__plate_assays_plateread__name__one_line'),
        ),
        iki.AddConstraint(
            model_name='plateread',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('import_key'), 1), name='iki__plate_assays_plateread__import_key__not_empty'),
        ),
        iki.AddConstraint(
            model_name='plateread',
            constraint=iki.CheckConstraint(condition=iki.Q(('import_key__regex', '[\\r\\n]'), negated=True), name='iki__plate_assays_plateread__import_key__one_line'),
        ),
        iki.AddConstraint(
            model_name='plateread',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('reader_format'), 1), name='iki__plate_assays_plateread__reader_format__not_empty'),
        ),
        iki.AddConstraint(
            model_name='plateread',
            constraint=iki.CheckConstraint(condition=iki.Q(('reader_format__regex', '[\\r\\n]'), negated=True), name='iki__plate_assays_plateread__reader_format__one_line'),
        ),
        iki.AddConstraint(
            model_name='plateread',
            constraint=iki.CheckConstraint(condition=iki.Q(('plate_revision__gte', 1)), name='iki__plate_assays_plateread__plate_revision__min'),
        ),
        iki.AddIndex(
            model_name='assayresult',
            index=iki.GinIndex(iki.SearchVector(iki.Left('fit_reason', 100000), config='simple'), name='fit_reason_fts_5412bb7e1802626'),
        ),
        iki.AddIndex(
            model_name='assayresult',
            index=iki.GinIndex(iki.SearchVector(iki.Left('notes', 100000), config='simple'), name='notes_fts_ef4d495d982d245195c9'),
        ),
        iki.AddConstraint(
            model_name='assayresult',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('procedure'), 1), name='iki__plate_assays_assayresult__procedure__not_empty'),
        ),
        iki.AddConstraint(
            model_name='assayresult',
            constraint=iki.CheckConstraint(condition=iki.Q(('procedure__regex', '[\\r\\n]'), negated=True), name='iki__plate_assays_assayresult__procedure__one_line'),
        ),
        iki.AddConstraint(
            model_name='assayresult',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('procedure_revision'), 1), name='iki__plate_assays_assayresult__procedure_revision__not_empty'),
        ),
        iki.AddConstraint(
            model_name='assayresult',
            constraint=iki.CheckConstraint(condition=iki.Q(('procedure_revision__regex', '[\\r\\n]'), negated=True), name='iki__plate_assays_assayresult__procedure_revision__one_line'),
        ),
        iki.AddConstraint(
            model_name='assayresult',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('channel'), 1), name='iki__plate_assays_assayresult__channel__not_empty'),
        ),
        iki.AddConstraint(
            model_name='assayresult',
            constraint=iki.CheckConstraint(condition=iki.Q(('channel__regex', '[\\r\\n]'), negated=True), name='iki__plate_assays_assayresult__channel__one_line'),
        ),
        iki.AddConstraint(
            model_name='assayresult',
            constraint=iki.CheckConstraint(condition=iki.Q(('response_unit__in', ['AU', 'percent'])), name='iki__plate_assays_assayresult__response_unit__values'),
        ),
        iki.AddConstraint(
            model_name='assayresult',
            constraint=iki.CheckConstraint(condition=iki.Q(('concentration_unit__in', ['nM', 'uM', 'mM', 'M'])), name='iki__plate_assays_assayresult__concentration_unit__values'),
        ),
        iki.AddConstraint(
            model_name='assayresult',
            constraint=iki.CheckConstraint(condition=iki.Q(('endpoint_kind__in', ['IC50', 'EC50'])), name='iki__plate_assays_assayresult__endpoint_kind__values'),
        ),
        iki.AddConstraint(
            model_name='assayresult',
            constraint=iki.CheckConstraint(condition=iki.Q(('endpoint_value__gt', float('-inf')), ('endpoint_value__lt', float('inf'))), name='iki__plate_assays_assayresult__endpoint_value__finite'),
        ),
        iki.AddConstraint(
            model_name='assayresult',
            constraint=iki.CheckConstraint(condition=iki.Q(('ci_low__gt', float('-inf')), ('ci_low__lt', float('inf'))), name='iki__plate_assays_assayresult__ci_low__finite'),
        ),
        iki.AddConstraint(
            model_name='assayresult',
            constraint=iki.CheckConstraint(condition=iki.Q(('ci_high__gt', float('-inf')), ('ci_high__lt', float('inf'))), name='iki__plate_assays_assayresult__ci_high__finite'),
        ),
        iki.AddConstraint(
            model_name='assayresult',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('ci_method'), 1), name='iki__plate_assays_assayresult__ci_method__not_empty'),
        ),
        iki.AddConstraint(
            model_name='assayresult',
            constraint=iki.CheckConstraint(condition=iki.Q(('ci_method__regex', '[\\r\\n]'), negated=True), name='iki__plate_assays_assayresult__ci_method__one_line'),
        ),
        iki.AddConstraint(
            model_name='assayresult',
            constraint=iki.CheckConstraint(condition=iki.Q(('bottom__gt', float('-inf')), ('bottom__lt', float('inf'))), name='iki__plate_assays_assayresult__bottom__finite'),
        ),
        iki.AddConstraint(
            model_name='assayresult',
            constraint=iki.CheckConstraint(condition=iki.Q(('top__gt', float('-inf')), ('top__lt', float('inf'))), name='iki__plate_assays_assayresult__top__finite'),
        ),
        iki.AddConstraint(
            model_name='assayresult',
            constraint=iki.CheckConstraint(condition=iki.Q(('hill_slope__gt', float('-inf')), ('hill_slope__lt', float('inf'))), name='iki__plate_assays_assayresult__hill_slope__finite'),
        ),
        iki.AddConstraint(
            model_name='assayresult',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('normalization'), 1), name='iki__plate_assays_assayresult__normalization__not_empty'),
        ),
        iki.AddConstraint(
            model_name='assayresult',
            constraint=iki.CheckConstraint(condition=iki.Q(('normalization__regex', '[\\r\\n]'), negated=True), name='iki__plate_assays_assayresult__normalization__one_line'),
        ),
        iki.AddConstraint(
            model_name='assayresult',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('fit_version'), 1), name='iki__plate_assays_assayresult__fit_version__not_empty'),
        ),
        iki.AddConstraint(
            model_name='assayresult',
            constraint=iki.CheckConstraint(condition=iki.Q(('fit_version__regex', '[\\r\\n]'), negated=True), name='iki__plate_assays_assayresult__fit_version__one_line'),
        ),
        iki.AddConstraint(
            model_name='assayresult',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('fit_reason'), 1), name='iki__plate_assays_assayresult__fit_reason__not_empty'),
        ),
        iki.AddConstraint(
            model_name='assayresult',
            constraint=iki.CheckConstraint(condition=iki.Q(('fit_status__in', ['fitted', 'unreportable'])), name='iki__plate_assays_assayresult__fit_status__values'),
        ),
        iki.AddConstraint(
            model_name='assayresult',
            constraint=iki.CheckConstraint(condition=iki.Q(('qualifier__in', ['within_range', 'above_range', 'below_range', 'unavailable'])), name='iki__plate_assays_assayresult__qualifier__values'),
        ),
        iki.AddConstraint(
            model_name='assayresult',
            constraint=iki.CheckConstraint(condition=iki.Q(('point_count__gte', 0)), name='iki__plate_assays_assayresult__point_count__min'),
        ),
        iki.AddConstraint(
            model_name='assayresult',
            constraint=iki.CheckConstraint(condition=iki.Q(('review_flow__in', ['research', 'regulated_qc'])), name='iki__plate_assays_assayresult__review_flow__values'),
        ),
        iki.AddConstraint(
            model_name='assayresult',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('notes'), 1), name='iki__plate_assays_assayresult__notes__not_empty'),
        ),
        iki.AddConstraint(
            model_name='wellmeasurement',
            constraint=iki.CheckConstraint(condition=iki.Q(('plate_revision__gte', 1)), name='iki__plate_assays_wellmeasurement__plate_revision__min'),
        ),
        iki.AddConstraint(
            model_name='wellmeasurement',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('well'), 1), name='iki__plate_assays_wellmeasurement__well__not_empty'),
        ),
        iki.AddConstraint(
            model_name='wellmeasurement',
            constraint=iki.CheckConstraint(condition=iki.Q(('well__regex', '[\\r\\n]'), negated=True), name='iki__plate_assays_wellmeasurement__well__one_line'),
        ),
        iki.AddConstraint(
            model_name='wellmeasurement',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('control'), 1), name='iki__plate_assays_wellmeasurement__control__not_empty'),
        ),
        iki.AddConstraint(
            model_name='wellmeasurement',
            constraint=iki.CheckConstraint(condition=iki.Q(('control__regex', '[\\r\\n]'), negated=True), name='iki__plate_assays_wellmeasurement__control__one_line'),
        ),
        iki.AddConstraint(
            model_name='wellmeasurement',
            constraint=iki.CheckConstraint(condition=iki.Q(('concentration__gt', float('-inf')), ('concentration__lt', float('inf'))), name='iki__plate_assays_wellmeasurement__concentration__finite'),
        ),
        iki.AddConstraint(
            model_name='wellmeasurement',
            constraint=iki.CheckConstraint(condition=iki.Q(('concentration__gte', 0)), name='iki__plate_assays_wellmeasurement__concentration__min'),
        ),
        iki.AddConstraint(
            model_name='wellmeasurement',
            constraint=iki.CheckConstraint(condition=iki.Q(('concentration_unit__in', ['nM', 'uM', 'mM', 'M'])), name='iki__plate_assays_wellmeasurement__concentration_unit__values'),
        ),
        iki.AddConstraint(
            model_name='wellmeasurement',
            constraint=iki.CheckConstraint(condition=iki.Q(('replicate__gte', 1)), name='iki__plate_assays_wellmeasurement__replicate__min'),
        ),
        iki.AddConstraint(
            model_name='wellmeasurement',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('channel'), 1), name='iki__plate_assays_wellmeasurement__channel__not_empty'),
        ),
        iki.AddConstraint(
            model_name='wellmeasurement',
            constraint=iki.CheckConstraint(condition=iki.Q(('channel__regex', '[\\r\\n]'), negated=True), name='iki__plate_assays_wellmeasurement__channel__one_line'),
        ),
        iki.AddConstraint(
            model_name='wellmeasurement',
            constraint=iki.CheckConstraint(condition=iki.Q(('readout__in', ['absorbance', 'normalized_activity'])), name='iki__plate_assays_wellmeasurement__readout__values'),
        ),
        iki.AddConstraint(
            model_name='wellmeasurement',
            constraint=iki.CheckConstraint(condition=iki.Q(('signal__gt', float('-inf')), ('signal__lt', float('inf'))), name='iki__plate_assays_wellmeasurement__signal__finite'),
        ),
        iki.AddConstraint(
            model_name='wellmeasurement',
            constraint=iki.CheckConstraint(condition=iki.Q(('activity_percent__gt', float('-inf')), ('activity_percent__lt', float('inf'))), name='iki__plate_assays_wellmeasurement__activity_percent__finite'),
        ),
        iki.AddConstraint(
            model_name='wellmeasurement',
            constraint=iki.CheckConstraint(condition=iki.Q(('source_row__gte', 1)), name='iki__plate_assays_wellmeasurement__source_row__min'),
        ),
        iki.AddConstraint(
            model_name='assayplate_assignments',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('well'), 1), name='iki__plate_assays_assayplate__assignments__well__not_empty'),
        ),
        iki.AddConstraint(
            model_name='assayplate_assignments',
            constraint=iki.CheckConstraint(condition=iki.Q(('well__regex', '[\\r\\n]'), negated=True), name='iki__plate_assays_assayplate__assignments__well__one_line'),
        ),
        iki.AddConstraint(
            model_name='assayplate_assignments',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('control'), 1), name='iki__plate_assays_assayplate__assignments__control__not_empty'),
        ),
        iki.AddConstraint(
            model_name='assayplate_assignments',
            constraint=iki.CheckConstraint(condition=iki.Q(('control__regex', '[\\r\\n]'), negated=True), name='iki__plate_assays_assayplate__assignments__control__one_line'),
        ),
        iki.AddConstraint(
            model_name='assayplate_assignments',
            constraint=iki.CheckConstraint(condition=iki.Q(('replicate__gte', 1)), name='iki__plate_assays_assayplate__assignments__replicate__min'),
        ),
        iki.AddConstraint(
            model_name='assayplate_assignments',
            constraint=iki.CheckConstraint(condition=iki.Q(('replicate__lte', 384)), name='iki__plate_assays_assayplate__assignments__replicate__max'),
        ),
        iki.AddConstraint(
            model_name='assayplate_assignments',
            constraint=iki.CheckConstraint(condition=iki.Q(('concentration__gt', float('-inf')), ('concentration__lt', float('inf'))), name='iki__plate_assays_assayplate__assignments__concentr__524dc5cd60'),
        ),
        iki.AddConstraint(
            model_name='assayplate_assignments',
            constraint=iki.CheckConstraint(condition=iki.Q(('concentration__gte', 0)), name='iki__plate_assays_assayplate__assignments__concentration__min'),
        ),
        iki.AddConstraint(
            model_name='assayplate_assignments',
            constraint=iki.CheckConstraint(condition=iki.Q(('concentration_unit__in', ['nM', 'uM', 'mM', 'M'])), name='iki__plate_assays_assayplate__assignments__concentr__eb940873e1'),
        ),
        iki.AddConstraint(
            model_name='assayplate_assignments',
            constraint=iki.CheckConstraint(condition=iki.Q(('position__lt', 384)), name='iki__plate_assays_assayplate__assignments__position__max_items'),
        ),
        iki.AddConstraint(
            model_name='assayresult_measurements',
            constraint=iki.CheckConstraint(condition=iki.Q(('position__lt', 384)), name='iki__plate_assays_assayresult__measurements__positi__68fbba7bff'),
        ),
        iki.AddField(
            model_name='requestedqcassayresult',
            name='request',
            field=iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__requests.qcrequest'),
        ),
        iki.AddField(
            model_name='requestedresearchassayresult',
            name='request',
            field=iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__requests.researchrequest'),
        ),
        iki.AddConstraint(
            model_name='plateread_measurements',
            constraint=iki.CheckConstraint(condition=iki.Q(('position__lt', 384)), name='iki__plate_assays_plateread__measurements__position__max_items'),
        ),
    ]
