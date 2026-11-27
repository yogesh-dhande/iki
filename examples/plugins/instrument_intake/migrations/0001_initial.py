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
            name='QPCRRun',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('source_sha256', iki.CharField(db_index=True, max_length=64)),
                ('instrument', iki.CharField(db_index=True, max_length=200, null=True)),
                ('experiment_name', iki.CharField(max_length=255, null=True)),
                ('experiment_type', iki.CharField(max_length=100, null=True)),
                ('instrument_type', iki.CharField(max_length=100, null=True)),
                ('instrument_serial', iki.CharField(max_length=100, null=True)),
                ('block', iki.CharField(max_length=100, null=True)),
                ('chemistry', iki.CharField(max_length=50, null=True)),
                ('passive_reference', iki.CharField(max_length=50, null=True)),
                ('cycle_method', iki.CharField(max_length=20, null=True)),
                ('endogenous_control', iki.CharField(max_length=255, null=True)),
                ('reference_sample', iki.CharField(max_length=255, null=True)),
                ('run_ended', iki.CharField(max_length=64, null=True)),
                ('operator', iki.CharField(max_length=200, null=True)),
                ('expired_calibrations', iki.ArrayField(base_field=iki.CharField(max_length=100), null=True)),
                ('results_file', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to=iki.FILE)),
            ],
            table='acme__instrument_intake_qpcrrun',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='QPCRRun_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('results_file', iki.UUIDField(db_column='results_file_id', null=True)),
                ('source_sha256', iki.TextField(null=True)),
                ('instrument', iki.TextField(null=True)),
                ('experiment_name', iki.TextField(null=True)),
                ('experiment_type', iki.TextField(null=True)),
                ('instrument_type', iki.TextField(null=True)),
                ('instrument_serial', iki.TextField(null=True)),
                ('block', iki.TextField(null=True)),
                ('chemistry', iki.TextField(null=True)),
                ('passive_reference', iki.TextField(null=True)),
                ('cycle_method', iki.TextField(null=True)),
                ('endogenous_control', iki.TextField(null=True)),
                ('reference_sample', iki.TextField(null=True)),
                ('run_ended', iki.TextField(null=True)),
                ('operator', iki.TextField(null=True)),
                ('expired_calibrations', iki.ArrayField(base_field=iki.TextField(), null=True)),
                ('wells', iki.JSONField(null=True)),
                ('standard_curves', iki.JSONField(null=True)),
            ],
            table='acme__instrument_intake_qpcrrun__history',
        ),
        iki.AppendOnly(
            model_name='QPCRRun_History',
        ),
        iki.CreateModel(
            name='QPCRRun_standard_curves',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity', 'position')),
                ('entity', iki.ForeignKey(db_column='entity_id', on_delete=iki.CASCADE, related_name='+', to='acme__instrument_intake.qpcrrun')),
                ('position', iki.PositiveIntegerField()),
                ('target', iki.CharField(max_length=255)),
                ('slope', iki.FloatField(null=True)),
                ('y_intercept', iki.FloatField(null=True)),
                ('r_squared', iki.FloatField(null=True)),
                ('efficiency', iki.FloatField(null=True)),
            ],
            table='acme__instrument_intake_qpcrrun__standard_curves',
            constraints=[
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('target'), 1), name='acme__instrument_intake_qpcrrun__standard_curves__t__a15970d8bc'),
                iki.CheckConstraint(condition=iki.Q(('target__regex', '[\\r\\n]'), negated=True), name='acme__instrument_intake_qpcrrun__standard_curves__t__f0d417c148'),
                iki.CheckConstraint(condition=iki.Q(('slope__gt', float('-inf')), ('slope__lt', float('inf'))), name='acme__instrument_intake_qpcrrun__standard_curves__slope__finite'),
                iki.CheckConstraint(condition=iki.Q(('y_intercept__gt', float('-inf')), ('y_intercept__lt', float('inf'))), name='acme__instrument_intake_qpcrrun__standard_curves__y__8b01b7d392'),
                iki.CheckConstraint(condition=iki.Q(('r_squared__gt', float('-inf')), ('r_squared__lt', float('inf'))), name='acme__instrument_intake_qpcrrun__standard_curves__r__c3947c6b01'),
                iki.CheckConstraint(condition=iki.Q(('r_squared__gte', 0)), name='acme__instrument_intake_qpcrrun__standard_curves__r__755e012111'),
                iki.CheckConstraint(condition=iki.Q(('r_squared__lte', 1)), name='acme__instrument_intake_qpcrrun__standard_curves__r__e0d747cc1a'),
                iki.CheckConstraint(condition=iki.Q(('efficiency__gt', float('-inf')), ('efficiency__lt', float('inf'))), name='acme__instrument_intake_qpcrrun__standard_curves__e__2110016217'),
                iki.CheckConstraint(condition=iki.Q(('position__lt', 96)), name='acme__instrument_intake_qpcrrun__standard_curves__p__192cbac47f'),
            ],
        ),
        iki.CreateModel(
            name='QPCRRun_wells',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity', 'position')),
                ('entity', iki.ForeignKey(db_column='entity_id', on_delete=iki.CASCADE, related_name='+', to='acme__instrument_intake.qpcrrun')),
                ('position', iki.PositiveIntegerField()),
                ('well', iki.CharField(max_length=3)),
                ('sample', iki.CharField(max_length=255, null=True)),
                ('target', iki.CharField(max_length=255)),
                ('task', iki.CharField(max_length=40, null=True)),
                ('reporter', iki.CharField(max_length=50, null=True)),
                ('quencher', iki.CharField(max_length=50, null=True)),
                ('omitted', iki.BooleanField(null=True)),
                ('cq', iki.FloatField(null=True)),
                ('undetermined', iki.BooleanField(null=True)),
                ('threshold', iki.FloatField(null=True)),
                ('quantity', iki.FloatField(null=True)),
            ],
            table='acme__instrument_intake_qpcrrun__wells',
            constraints=[
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('well'), 1), name='acme__instrument_intake_qpcrrun__wells__well__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('well__regex', '[\\r\\n]'), negated=True), name='acme__instrument_intake_qpcrrun__wells__well__one_line'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('sample'), 1), name='acme__instrument_intake_qpcrrun__wells__sample__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('sample__regex', '[\\r\\n]'), negated=True), name='acme__instrument_intake_qpcrrun__wells__sample__one_line'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('target'), 1), name='acme__instrument_intake_qpcrrun__wells__target__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('target__regex', '[\\r\\n]'), negated=True), name='acme__instrument_intake_qpcrrun__wells__target__one_line'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('task'), 1), name='acme__instrument_intake_qpcrrun__wells__task__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('task__regex', '[\\r\\n]'), negated=True), name='acme__instrument_intake_qpcrrun__wells__task__one_line'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('reporter'), 1), name='acme__instrument_intake_qpcrrun__wells__reporter__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('reporter__regex', '[\\r\\n]'), negated=True), name='acme__instrument_intake_qpcrrun__wells__reporter__one_line'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('quencher'), 1), name='acme__instrument_intake_qpcrrun__wells__quencher__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('quencher__regex', '[\\r\\n]'), negated=True), name='acme__instrument_intake_qpcrrun__wells__quencher__one_line'),
                iki.CheckConstraint(condition=iki.Q(('cq__gt', float('-inf')), ('cq__lt', float('inf'))), name='acme__instrument_intake_qpcrrun__wells__cq__finite'),
                iki.CheckConstraint(condition=iki.Q(('cq__gte', 0)), name='acme__instrument_intake_qpcrrun__wells__cq__min'),
                iki.CheckConstraint(condition=iki.Q(('threshold__gt', float('-inf')), ('threshold__lt', float('inf'))), name='acme__instrument_intake_qpcrrun__wells__threshold__finite'),
                iki.CheckConstraint(condition=iki.Q(('threshold__gte', 0)), name='acme__instrument_intake_qpcrrun__wells__threshold__min'),
                iki.CheckConstraint(condition=iki.Q(('quantity__gt', float('-inf')), ('quantity__lt', float('inf'))), name='acme__instrument_intake_qpcrrun__wells__quantity__finite'),
                iki.CheckConstraint(condition=iki.Q(('quantity__gte', 0)), name='acme__instrument_intake_qpcrrun__wells__quantity__min'),
                iki.CheckConstraint(condition=iki.Q(('position__lt', 1536)), name='acme__instrument_intake_qpcrrun__wells__position__max_items'),
            ],
        ),
        iki.AddIndex(
            model_name='qpcrrun',
            index=iki.GinIndex(fields=['source_sha256'], name='source_sha_trgm_c23c4726bae64b', opclasses=['gin_trgm_ops']),
        ),
        iki.AddIndex(
            model_name='qpcrrun',
            index=iki.GinIndex(fields=['instrument'], name='instrument_trgm_6614739ca4b471', opclasses=['gin_trgm_ops']),
        ),
        iki.AddConstraint(
            model_name='qpcrrun',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('source_sha256'), 1), name='acme__instrument_intake_qpcrrun__source_sha256__not_empty'),
        ),
        iki.AddConstraint(
            model_name='qpcrrun',
            constraint=iki.CheckConstraint(condition=iki.Q(('source_sha256__regex', '[\\r\\n]'), negated=True), name='acme__instrument_intake_qpcrrun__source_sha256__one_line'),
        ),
        iki.AddConstraint(
            model_name='qpcrrun',
            constraint=iki.CheckConstraint(condition=iki.Q(('source_sha256__regex', '^(?:^[0-9a-f]{64}$)$')), name='acme__instrument_intake_qpcrrun__source_sha256__pattern'),
        ),
        iki.AddConstraint(
            model_name='qpcrrun',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('instrument'), 1), name='acme__instrument_intake_qpcrrun__instrument__not_empty'),
        ),
        iki.AddConstraint(
            model_name='qpcrrun',
            constraint=iki.CheckConstraint(condition=iki.Q(('instrument__regex', '[\\r\\n]'), negated=True), name='acme__instrument_intake_qpcrrun__instrument__one_line'),
        ),
        iki.AddConstraint(
            model_name='qpcrrun',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('experiment_name'), 1), name='acme__instrument_intake_qpcrrun__experiment_name__not_empty'),
        ),
        iki.AddConstraint(
            model_name='qpcrrun',
            constraint=iki.CheckConstraint(condition=iki.Q(('experiment_name__regex', '[\\r\\n]'), negated=True), name='acme__instrument_intake_qpcrrun__experiment_name__one_line'),
        ),
        iki.AddConstraint(
            model_name='qpcrrun',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('experiment_type'), 1), name='acme__instrument_intake_qpcrrun__experiment_type__not_empty'),
        ),
        iki.AddConstraint(
            model_name='qpcrrun',
            constraint=iki.CheckConstraint(condition=iki.Q(('experiment_type__regex', '[\\r\\n]'), negated=True), name='acme__instrument_intake_qpcrrun__experiment_type__one_line'),
        ),
        iki.AddConstraint(
            model_name='qpcrrun',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('instrument_type'), 1), name='acme__instrument_intake_qpcrrun__instrument_type__not_empty'),
        ),
        iki.AddConstraint(
            model_name='qpcrrun',
            constraint=iki.CheckConstraint(condition=iki.Q(('instrument_type__regex', '[\\r\\n]'), negated=True), name='acme__instrument_intake_qpcrrun__instrument_type__one_line'),
        ),
        iki.AddConstraint(
            model_name='qpcrrun',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('instrument_serial'), 1), name='acme__instrument_intake_qpcrrun__instrument_serial__not_empty'),
        ),
        iki.AddConstraint(
            model_name='qpcrrun',
            constraint=iki.CheckConstraint(condition=iki.Q(('instrument_serial__regex', '[\\r\\n]'), negated=True), name='acme__instrument_intake_qpcrrun__instrument_serial__one_line'),
        ),
        iki.AddConstraint(
            model_name='qpcrrun',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('block'), 1), name='acme__instrument_intake_qpcrrun__block__not_empty'),
        ),
        iki.AddConstraint(
            model_name='qpcrrun',
            constraint=iki.CheckConstraint(condition=iki.Q(('block__regex', '[\\r\\n]'), negated=True), name='acme__instrument_intake_qpcrrun__block__one_line'),
        ),
        iki.AddConstraint(
            model_name='qpcrrun',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('chemistry'), 1), name='acme__instrument_intake_qpcrrun__chemistry__not_empty'),
        ),
        iki.AddConstraint(
            model_name='qpcrrun',
            constraint=iki.CheckConstraint(condition=iki.Q(('chemistry__regex', '[\\r\\n]'), negated=True), name='acme__instrument_intake_qpcrrun__chemistry__one_line'),
        ),
        iki.AddConstraint(
            model_name='qpcrrun',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('passive_reference'), 1), name='acme__instrument_intake_qpcrrun__passive_reference__not_empty'),
        ),
        iki.AddConstraint(
            model_name='qpcrrun',
            constraint=iki.CheckConstraint(condition=iki.Q(('passive_reference__regex', '[\\r\\n]'), negated=True), name='acme__instrument_intake_qpcrrun__passive_reference__one_line'),
        ),
        iki.AddConstraint(
            model_name='qpcrrun',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('cycle_method'), 1), name='acme__instrument_intake_qpcrrun__cycle_method__not_empty'),
        ),
        iki.AddConstraint(
            model_name='qpcrrun',
            constraint=iki.CheckConstraint(condition=iki.Q(('cycle_method__regex', '[\\r\\n]'), negated=True), name='acme__instrument_intake_qpcrrun__cycle_method__one_line'),
        ),
        iki.AddConstraint(
            model_name='qpcrrun',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('endogenous_control'), 1), name='acme__instrument_intake_qpcrrun__endogenous_control__not_empty'),
        ),
        iki.AddConstraint(
            model_name='qpcrrun',
            constraint=iki.CheckConstraint(condition=iki.Q(('endogenous_control__regex', '[\\r\\n]'), negated=True), name='acme__instrument_intake_qpcrrun__endogenous_control__one_line'),
        ),
        iki.AddConstraint(
            model_name='qpcrrun',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('reference_sample'), 1), name='acme__instrument_intake_qpcrrun__reference_sample__not_empty'),
        ),
        iki.AddConstraint(
            model_name='qpcrrun',
            constraint=iki.CheckConstraint(condition=iki.Q(('reference_sample__regex', '[\\r\\n]'), negated=True), name='acme__instrument_intake_qpcrrun__reference_sample__one_line'),
        ),
        iki.AddConstraint(
            model_name='qpcrrun',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('run_ended'), 1), name='acme__instrument_intake_qpcrrun__run_ended__not_empty'),
        ),
        iki.AddConstraint(
            model_name='qpcrrun',
            constraint=iki.CheckConstraint(condition=iki.Q(('run_ended__regex', '[\\r\\n]'), negated=True), name='acme__instrument_intake_qpcrrun__run_ended__one_line'),
        ),
        iki.AddConstraint(
            model_name='qpcrrun',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('operator'), 1), name='acme__instrument_intake_qpcrrun__operator__not_empty'),
        ),
        iki.AddConstraint(
            model_name='qpcrrun',
            constraint=iki.CheckConstraint(condition=iki.Q(('operator__regex', '[\\r\\n]'), negated=True), name='acme__instrument_intake_qpcrrun__operator__one_line'),
        ),
        iki.AddConstraint(
            model_name='qpcrrun',
            constraint=iki.CheckConstraint(condition=iki.IsPlainList('expired_calibrations'), name='acme__instrument_intake_qpcrrun__expired_calibratio__ad8db2cd82'),
        ),
        iki.AddConstraint(
            model_name='qpcrrun',
            constraint=iki.CheckConstraint(condition=iki.Q(('expired_calibrations__len__lte', 50)), name='acme__instrument_intake_qpcrrun__expired_calibratio__0d5b3a4d7a'),
        ),
        iki.AddConstraint(
            model_name='qpcrrun',
            constraint=iki.CheckConstraint(condition=iki.AllLengthsBetween('expired_calibrations', 1, None), name='acme__instrument_intake_qpcrrun__expired_calibratio__62ed65f1a8'),
        ),
        iki.AddConstraint(
            model_name='qpcrrun',
            constraint=iki.CheckConstraint(condition=iki.NoneMatch('expired_calibrations', '[\\r\\n]'), name='acme__instrument_intake_qpcrrun__expired_calibratio__13b9bb9551'),
        ),
    ]
