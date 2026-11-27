# Made by `iki plugin makemigrations`. Review it, and commit it with the manifests it
# migrates: once a deployment has applied it, it never changes.

from iki_server.plugins import operations as iki


class Migration(iki.Migration):

    initial = True

    dependencies = [
        ('iki_anchors', '0001_iki_0_1'),
    ]

    operations = [
        iki.CreateModel(
            name='Booking_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('purpose', iki.TextField(null=True)),
                ('instrument', iki.UUIDField(db_column='instrument_id', null=True)),
                ('starts_at', iki.DateTimeField(null=True)),
                ('ends_at', iki.DateTimeField(null=True)),
                ('cancelled', iki.BooleanField(null=True)),
                ('booked_by', iki.UUIDField(db_column='booked_by_id', null=True)),
                ('work', iki.UUIDField(db_column='work_id', null=True)),
            ],
            table='iki__equipment_booking__history',
        ),
        iki.AppendOnly(
            model_name='Booking_History',
        ),
        iki.CreateModel(
            name='CalibrationImpact_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('key', iki.TextField(null=True)),
                ('instrument', iki.UUIDField(db_column='instrument_id', null=True)),
                ('failure', iki.UUIDField(db_column='failure_id', null=True)),
                ('failure_revision', iki.BigIntegerField(null=True)),
                ('use', iki.UUIDField(db_column='use_id', null=True)),
                ('result', iki.UUIDField(db_column='result_id', null=True)),
                ('measurement_at', iki.DateTimeField(null=True)),
                ('review_flow', iki.TextField(null=True)),
                ('opened', iki.TextField(null=True)),
                ('opened_by', iki.UUIDField(db_column='opened_by_id', null=True)),
                ('opened_reason', iki.TextField(null=True)),
                ('disposition', iki.TextField(null=True)),
                ('reasoning', iki.TextField(null=True)),
                ('review', iki.TextField(null=True)),
            ],
            table='iki__equipment_calibrationimpact__history',
        ),
        iki.AppendOnly(
            model_name='CalibrationImpact_History',
        ),
        iki.CreateModel(
            name='EquipmentService_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('instrument', iki.UUIDField(db_column='instrument_id', null=True)),
                ('kind', iki.TextField(null=True)),
                ('due_on', iki.DateField(null=True)),
                ('procedure', iki.TextField(null=True)),
                ('review_flow', iki.TextField(null=True)),
                ('performed_by', iki.UUIDField(db_column='performed_by_id', null=True)),
                ('performed_at', iki.DateTimeField(null=True)),
                ('outcome', iki.TextField(null=True)),
                ('valid_until', iki.DateTimeField(null=True)),
                ('suspect_since', iki.DateTimeField(null=True)),
                ('report', iki.UUIDField(db_column='report_id', null=True)),
                ('notes', iki.TextField(null=True)),
                ('review', iki.TextField(null=True)),
            ],
            table='iki__equipment_equipmentservice__history',
        ),
        iki.AppendOnly(
            model_name='EquipmentService_History',
        ),
        iki.CreateModel(
            name='Instrument_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('name', iki.TextField(null=True)),
                ('location', iki.TextField(null=True)),
                ('service', iki.TextField(null=True)),
                ('calibration', iki.TextField(null=True)),
            ],
            table='iki__equipment_instrument__history',
        ),
        iki.AppendOnly(
            model_name='Instrument_History',
        ),
        iki.CreateModel(
            name='InstrumentUse_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('key', iki.TextField(null=True)),
                ('instrument', iki.UUIDField(db_column='instrument_id', null=True)),
                ('result', iki.UUIDField(db_column='result_id', null=True)),
                ('result_record_id', iki.TextField(null=True)),
                ('result_revision', iki.BigIntegerField(null=True)),
                ('calibration', iki.UUIDField(db_column='calibration_id', null=True)),
                ('calibration_revision', iki.BigIntegerField(null=True)),
                ('measurement_at', iki.DateTimeField(null=True)),
            ],
            table='iki__equipment_instrumentuse__history',
        ),
        iki.AppendOnly(
            model_name='InstrumentUse_History',
        ),
        iki.CreateModel(
            name='Instrument',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('name', iki.CharField(db_index=True, max_length=200)),
                ('location', iki.CharField(max_length=200, null=True)),
                ('service', iki.StateField(db_comment='A state field (ADR 0125): one of in_service, out_of_service, retired.', db_default='in_service', locking=['retired'], states=['in_service', 'out_of_service', 'retired'])),
                ('calibration', iki.StateField(db_comment='A state field (ADR 0125): one of due, calibrated, failed, not_required.', db_default='due', states=['due', 'calibrated', 'failed', 'not_required'])),
            ],
            table='iki__equipment_instrument',
            indexes=[
                iki.GinIndex(fields=['name'], name='name_trgm_6604f6d6e27d19e595aa', opclasses=['gin_trgm_ops']),
            ],
            constraints=[
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('name'), 1), name='iki__equipment_instrument__name__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('name__regex', '[\\r\\n]'), negated=True), name='iki__equipment_instrument__name__one_line'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('location'), 1), name='iki__equipment_instrument__location__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('location__regex', '[\\r\\n]'), negated=True), name='iki__equipment_instrument__location__one_line'),
                iki.CheckConstraint(condition=iki.Q(('service__in', ['in_service', 'out_of_service', 'retired'])), name='iki__equipment_instrument__service__known'),
                iki.CheckConstraint(condition=iki.Q(('calibration__in', ['due', 'calibrated', 'failed', 'not_required'])), name='iki__equipment_instrument__calibration__known'),
            ],
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='EquipmentService',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('kind', iki.TextField(db_index=True)),
                ('due_on', iki.DateField(db_index=True)),
                ('procedure', iki.CharField(max_length=240)),
                ('review_flow', iki.TextField(null=True)),
                ('performed_at', iki.DateTimeField(db_index=True, null=True)),
                ('outcome', iki.TextField(db_index=True, null=True)),
                ('valid_until', iki.DateTimeField(null=True)),
                ('suspect_since', iki.DateTimeField(null=True)),
                ('notes', iki.TextField(null=True)),
                ('review', iki.StateField(db_comment='A state field (ADR 0125): one of scheduled, cancelled, signed_off, submitted, reviewed, released.', db_default='scheduled', locking=['cancelled', 'signed_off', 'submitted', 'reviewed', 'released'], states=['scheduled', 'cancelled', 'signed_off', 'submitted', 'reviewed', 'released'])),
                ('performed_by', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to=iki.USER)),
                ('report', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to=iki.FILE)),
                ('instrument', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__equipment.instrument')),
            ],
            table='iki__equipment_equipmentservice',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='Booking',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('purpose', iki.CharField(max_length=200)),
                ('starts_at', iki.DateTimeField(db_index=True)),
                ('ends_at', iki.DateTimeField(db_index=True)),
                ('cancelled', iki.BooleanField(db_default=False, null=True)),
                ('booked_by', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to=iki.USER)),
                ('work', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to=iki.ENTITY)),
                ('instrument', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__equipment.instrument')),
            ],
            table='iki__equipment_booking',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='InstrumentUse',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('key', iki.CharField(db_index=True, max_length=64, null=True)),
                ('result_record_id', iki.CharField(max_length=36, null=True)),
                ('result_revision', iki.BigIntegerField(null=True)),
                ('calibration_revision', iki.BigIntegerField(null=True)),
                ('measurement_at', iki.DateTimeField(db_index=True)),
                ('calibration', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__equipment.equipmentservice')),
                ('instrument', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__equipment.instrument')),
                ('result', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to=iki.ENTITY)),
            ],
            table='iki__equipment_instrumentuse',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='CalibrationImpact',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('key', iki.CharField(db_index=True, max_length=64, null=True)),
                ('failure_revision', iki.BigIntegerField(null=True)),
                ('measurement_at', iki.DateTimeField(db_index=True)),
                ('review_flow', iki.TextField(null=True)),
                ('opened', iki.TextField(db_index=True, null=True)),
                ('opened_reason', iki.TextField(null=True)),
                ('disposition', iki.TextField(null=True)),
                ('reasoning', iki.TextField(null=True)),
                ('review', iki.StateField(db_comment='A state field (ADR 0125): one of recorded, signed_off, submitted, reviewed, released.', db_default='recorded', locking=['signed_off', 'submitted', 'reviewed', 'released'], states=['recorded', 'signed_off', 'submitted', 'reviewed', 'released'])),
                ('opened_by', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to=iki.USER)),
                ('result', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to=iki.ENTITY)),
                ('failure', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__equipment.equipmentservice')),
                ('instrument', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__equipment.instrument')),
                ('use', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to='iki__equipment.instrumentuse')),
            ],
            table='iki__equipment_calibrationimpact',
            bases=(iki.ENTITY,),
        ),
        iki.AddIndex(
            model_name='equipmentservice',
            index=iki.GinIndex(iki.SearchVector(iki.Left('notes', 100000), config='simple'), name='notes_fts_401d70191f0e1649fa9d'),
        ),
        iki.AddConstraint(
            model_name='equipmentservice',
            constraint=iki.CheckConstraint(condition=iki.Q(('kind__in', ['calibration', 'maintenance'])), name='iki__equipment_equipmentservice__kind__values'),
        ),
        iki.AddConstraint(
            model_name='equipmentservice',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('procedure'), 1), name='iki__equipment_equipmentservice__procedure__not_empty'),
        ),
        iki.AddConstraint(
            model_name='equipmentservice',
            constraint=iki.CheckConstraint(condition=iki.Q(('procedure__regex', '[\\r\\n]'), negated=True), name='iki__equipment_equipmentservice__procedure__one_line'),
        ),
        iki.AddConstraint(
            model_name='equipmentservice',
            constraint=iki.CheckConstraint(condition=iki.Q(('review_flow__in', ['research', 'regulated_qc'])), name='iki__equipment_equipmentservice__review_flow__values'),
        ),
        iki.AddConstraint(
            model_name='equipmentservice',
            constraint=iki.CheckConstraint(condition=iki.Q(('outcome__in', ['in_tolerance', 'out_of_tolerance', 'completed', 'requires_repair'])), name='iki__equipment_equipmentservice__outcome__values'),
        ),
        iki.AddConstraint(
            model_name='equipmentservice',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('notes'), 1), name='iki__equipment_equipmentservice__notes__not_empty'),
        ),
        iki.AddConstraint(
            model_name='equipmentservice',
            constraint=iki.CheckConstraint(condition=iki.LessThanOrEqual(iki.Length('notes'), 10000), name='iki__equipment_equipmentservice__notes__max_length'),
        ),
        iki.AddConstraint(
            model_name='equipmentservice',
            constraint=iki.CheckConstraint(condition=iki.Q(('review__in', ['scheduled', 'cancelled', 'signed_off', 'submitted', 'reviewed', 'released'])), name='iki__equipment_equipmentservice__review__known'),
        ),
        iki.AddConstraint(
            model_name='booking',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('purpose'), 1), name='iki__equipment_booking__purpose__not_empty'),
        ),
        iki.AddConstraint(
            model_name='booking',
            constraint=iki.CheckConstraint(condition=iki.Q(('purpose__regex', '[\\r\\n]'), negated=True), name='iki__equipment_booking__purpose__one_line'),
        ),
        iki.AddIndex(
            model_name='instrumentuse',
            index=iki.GinIndex(fields=['key'], name='key_trgm_a9b8d1741193f3925f407', opclasses=['gin_trgm_ops']),
        ),
        iki.AddConstraint(
            model_name='instrumentuse',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('key'), 1), name='iki__equipment_instrumentuse__key__not_empty'),
        ),
        iki.AddConstraint(
            model_name='instrumentuse',
            constraint=iki.CheckConstraint(condition=iki.Q(('key__regex', '[\\r\\n]'), negated=True), name='iki__equipment_instrumentuse__key__one_line'),
        ),
        iki.AddConstraint(
            model_name='instrumentuse',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('result_record_id'), 1), name='iki__equipment_instrumentuse__result_record_id__not_empty'),
        ),
        iki.AddConstraint(
            model_name='instrumentuse',
            constraint=iki.CheckConstraint(condition=iki.Q(('result_record_id__regex', '[\\r\\n]'), negated=True), name='iki__equipment_instrumentuse__result_record_id__one_line'),
        ),
        iki.AddConstraint(
            model_name='instrumentuse',
            constraint=iki.CheckConstraint(condition=iki.Q(('result_revision__gte', 1)), name='iki__equipment_instrumentuse__result_revision__min'),
        ),
        iki.AddConstraint(
            model_name='instrumentuse',
            constraint=iki.CheckConstraint(condition=iki.Q(('calibration_revision__gte', 1)), name='iki__equipment_instrumentuse__calibration_revision__min'),
        ),
        iki.AddIndex(
            model_name='calibrationimpact',
            index=iki.GinIndex(fields=['key'], name='key_trgm_6ef89d9984df0a4cde108', opclasses=['gin_trgm_ops']),
        ),
        iki.AddIndex(
            model_name='calibrationimpact',
            index=iki.GinIndex(iki.SearchVector(iki.Left('opened_reason', 100000), config='simple'), name='opened_rea_fts_92aba1e2bf411b0'),
        ),
        iki.AddIndex(
            model_name='calibrationimpact',
            index=iki.GinIndex(iki.SearchVector(iki.Left('reasoning', 100000), config='simple'), name='reasoning_fts_7333a5af30ddd2b6'),
        ),
        iki.AddConstraint(
            model_name='calibrationimpact',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('key'), 1), name='iki__equipment_calibrationimpact__key__not_empty'),
        ),
        iki.AddConstraint(
            model_name='calibrationimpact',
            constraint=iki.CheckConstraint(condition=iki.Q(('key__regex', '[\\r\\n]'), negated=True), name='iki__equipment_calibrationimpact__key__one_line'),
        ),
        iki.AddConstraint(
            model_name='calibrationimpact',
            constraint=iki.CheckConstraint(condition=iki.Q(('failure_revision__gte', 1)), name='iki__equipment_calibrationimpact__failure_revision__min'),
        ),
        iki.AddConstraint(
            model_name='calibrationimpact',
            constraint=iki.CheckConstraint(condition=iki.Q(('review_flow__in', ['research', 'regulated_qc'])), name='iki__equipment_calibrationimpact__review_flow__values'),
        ),
        iki.AddConstraint(
            model_name='calibrationimpact',
            constraint=iki.CheckConstraint(condition=iki.Q(('opened__in', ['traced', 'manually'])), name='iki__equipment_calibrationimpact__opened__values'),
        ),
        iki.AddConstraint(
            model_name='calibrationimpact',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('opened_reason'), 1), name='iki__equipment_calibrationimpact__opened_reason__not_empty'),
        ),
        iki.AddConstraint(
            model_name='calibrationimpact',
            constraint=iki.CheckConstraint(condition=iki.LessThanOrEqual(iki.Length('opened_reason'), 2000), name='iki__equipment_calibrationimpact__opened_reason__max_length'),
        ),
        iki.AddConstraint(
            model_name='calibrationimpact',
            constraint=iki.CheckConstraint(condition=iki.Q(('disposition__in', ['unaffected', 'repeat_required', 'invalidated'])), name='iki__equipment_calibrationimpact__disposition__values'),
        ),
        iki.AddConstraint(
            model_name='calibrationimpact',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('reasoning'), 1), name='iki__equipment_calibrationimpact__reasoning__not_empty'),
        ),
        iki.AddConstraint(
            model_name='calibrationimpact',
            constraint=iki.CheckConstraint(condition=iki.LessThanOrEqual(iki.Length('reasoning'), 10000), name='iki__equipment_calibrationimpact__reasoning__max_length'),
        ),
        iki.AddConstraint(
            model_name='calibrationimpact',
            constraint=iki.CheckConstraint(condition=iki.Q(('review__in', ['recorded', 'signed_off', 'submitted', 'reviewed', 'released'])), name='iki__equipment_calibrationimpact__review__known'),
        ),
    ]
