# Made by `iki plugin makemigrations`. Review it, and commit it with the manifests it
# migrates: once a deployment has applied it, it never changes.

from iki_server.plugins import operations as iki


class Migration(iki.Migration):

    initial = True

    dependencies = [
        ('iki__inventory', '0001_initial'),
        ('iki_anchors', '0001_iki_0_1'),
    ]

    operations = [
        iki.CreateModel(
            name='CellBank_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('name', iki.TextField(null=True)),
                ('kind', iki.TextField(null=True)),
                ('line', iki.UUIDField(db_column='line_id', null=True)),
                ('banked_on', iki.DateField(null=True)),
                ('passage_number', iki.BigIntegerField(null=True)),
                ('passage_unknown', iki.BooleanField(null=True)),
                ('catalogue_source', iki.UUIDField(db_column='catalogue_source_id', null=True)),
                ('source_bank', iki.UUIDField(db_column='source_bank_id', null=True)),
            ],
            table='iki__cell_lines_cellbank__history',
        ),
        iki.AppendOnly(
            model_name='CellBank_History',
        ),
        iki.CreateModel(
            name='CellCheck_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('bank', iki.UUIDField(db_column='bank_id', null=True)),
                ('kind', iki.TextField(null=True)),
                ('lab_test', iki.UUIDField(db_column='lab_test_id', null=True)),
                ('checked_on', iki.DateField(null=True)),
                ('due_on', iki.DateField(null=True)),
                ('method', iki.TextField(null=True)),
                ('result', iki.TextField(null=True)),
                ('report', iki.UUIDField(db_column='report_id', null=True)),
                ('review', iki.TextField(null=True)),
            ],
            table='iki__cell_lines_cellcheck__history',
        ),
        iki.AppendOnly(
            model_name='CellCheck_History',
        ),
        iki.CreateModel(
            name='CellCheckTest',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
            ],
            table='iki__cell_lines_cellchecktest',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='CellCheckTest_Entry_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('code', iki.TextField(null=True)),
                ('label', iki.TextField(null=True)),
                ('description', iki.TextField(null=True)),
                ('position', iki.BigIntegerField(null=True)),
                ('result_family', iki.TextField(null=True)),
            ],
            table='iki__cell_lines_cellchecktest__entry__history',
        ),
        iki.AppendOnly(
            model_name='CellCheckTest_Entry_History',
        ),
        iki.CreateModel(
            name='CellCheckTest_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
            ],
            table='iki__cell_lines_cellchecktest__history',
        ),
        iki.AppendOnly(
            model_name='CellCheckTest_History',
        ),
        iki.CreateModel(
            name='CellCulture_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('event', iki.TextField(null=True)),
                ('event_on', iki.DateField(null=True)),
                ('passage_number', iki.BigIntegerField(null=True)),
                ('passage_unknown', iki.BooleanField(null=True)),
                ('bank', iki.UUIDField(db_column='bank_id', null=True)),
                ('line', iki.UUIDField(db_column='line_id', null=True)),
                ('vial', iki.UUIDField(db_column='vial_id', null=True)),
                ('parent_culture', iki.UUIDField(db_column='parent_culture_id', null=True)),
                ('str_check', iki.UUIDField(db_column='str_check_id', null=True)),
                ('mycoplasma_check', iki.UUIDField(db_column='mycoplasma_check_id', null=True)),
            ],
            table='iki__cell_lines_cellculture__history',
        ),
        iki.AppendOnly(
            model_name='CellCulture_History',
        ),
        iki.CreateModel(
            name='CellLineSource',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
            ],
            table='iki__cell_lines_celllinesource',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='CellLineSource_Entry_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('supplier', iki.UUIDField(db_column='supplier_id', null=True)),
                ('catalogue_id', iki.TextField(null=True)),
                ('line', iki.UUIDField(db_column='line_id', null=True)),
            ],
            table='iki__cell_lines_celllinesource__entry__history',
        ),
        iki.AppendOnly(
            model_name='CellLineSource_Entry_History',
        ),
        iki.CreateModel(
            name='CellLineSource_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
            ],
            table='iki__cell_lines_celllinesource__history',
        ),
        iki.AppendOnly(
            model_name='CellLineSource_History',
        ),
        iki.CreateModel(
            name='CellSample_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('name', iki.TextField(null=True)),
                ('culture', iki.UUIDField(db_column='culture_id', null=True)),
                ('collected_on', iki.DateField(null=True)),
                ('data_file', iki.UUIDField(db_column='data_file_id', null=True)),
            ],
            table='iki__cell_lines_cellsample__history',
        ),
        iki.AppendOnly(
            model_name='CellSample_History',
        ),
        iki.CreateModel(
            name='CellVial',
            fields=[
                ('tube_ptr', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to='iki__inventory.tube')),
            ],
            table='iki__cell_lines_cellvial',
            bases=('iki__inventory.tube',),
        ),
        iki.CreateModel(
            name='CellVial_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
            ],
            table='iki__cell_lines_cellvial__history',
        ),
        iki.AppendOnly(
            model_name='CellVial_History',
        ),
        iki.CreateModel(
            name='EngineeredChange',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
            ],
            table='iki__cell_lines_engineeredchange',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='EngineeredChange_Entry_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('code', iki.TextField(null=True)),
                ('label', iki.TextField(null=True)),
                ('description', iki.TextField(null=True)),
                ('position', iki.BigIntegerField(null=True)),
            ],
            table='iki__cell_lines_engineeredchange__entry__history',
        ),
        iki.AppendOnly(
            model_name='EngineeredChange_Entry_History',
        ),
        iki.CreateModel(
            name='EngineeredChange_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
            ],
            table='iki__cell_lines_engineeredchange__history',
        ),
        iki.AppendOnly(
            model_name='EngineeredChange_History',
        ),
        iki.CreateModel(
            name='EngineeredLine',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('name', iki.CharField(db_index=True, max_length=255)),
                ('parent', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to=iki.ENTITY)),
            ],
            table='iki__cell_lines_engineeredline',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='EngineeredLine_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('name', iki.TextField(null=True)),
                ('parent', iki.UUIDField(db_column='parent_id', null=True)),
                ('parent_source', iki.UUIDField(db_column='parent_source_id', null=True)),
                ('changes', iki.JSONField(null=True)),
            ],
            table='iki__cell_lines_engineeredline__history',
        ),
        iki.AppendOnly(
            model_name='EngineeredLine_History',
        ),
        iki.CreateModel(
            name='ParentalLine_Entry_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('catalogue', iki.TextField(null=True)),
                ('cellosaurus_id', iki.TextField(null=True)),
                ('local_id', iki.TextField(null=True)),
            ],
            table='iki__cell_lines_parentalline__entry__history',
        ),
        iki.AppendOnly(
            model_name='ParentalLine_Entry_History',
        ),
        iki.CreateModel(
            name='ParentalLine_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('name', iki.TextField(null=True)),
            ],
            table='iki__cell_lines_parentalline__history',
        ),
        iki.AppendOnly(
            model_name='ParentalLine_History',
        ),
        iki.CreateModel(
            name='CellBank',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('name', iki.CharField(db_index=True, max_length=160)),
                ('kind', iki.TextField()),
                ('banked_on', iki.DateField(db_index=True)),
                ('passage_number', iki.BigIntegerField(null=True)),
                ('passage_unknown', iki.BooleanField(db_default=False)),
                ('line', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to=iki.ENTITY)),
                ('source_bank', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to='iki__cell_lines.cellbank')),
            ],
            table='iki__cell_lines_cellbank',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='CellCheckTest_Entry',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('code', iki.CharField(db_index=True, max_length=64)),
                ('label', iki.CharField(db_index=True, max_length=200)),
                ('description', iki.TextField(null=True)),
                ('position', iki.BigIntegerField(null=True)),
                ('result_family', iki.TextField()),
            ],
            table='iki__cell_lines_cellchecktest__entry',
            indexes=[
                iki.GinIndex(fields=['code'], name='code_trgm_4d2a98e9a0221c18f6a4', opclasses=['gin_trgm_ops']),
                iki.GinIndex(fields=['label'], name='label_trgm_c746ffd3c7633b67764', opclasses=['gin_trgm_ops']),
                iki.GinIndex(iki.SearchVector(iki.Left('description', 100000), config='simple'), name='descriptio_fts_24ab72edd5052a5'),
            ],
            constraints=[
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('code'), 1), name='iki__cell_lines_cellchecktest__entry__code__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('code__regex', '[\\r\\n]'), negated=True), name='iki__cell_lines_cellchecktest__entry__code__one_line'),
                iki.CheckConstraint(condition=iki.Q(('code__regex', '^(?:^[A-Za-z0-9][A-Za-z0-9_.+-]*$)$')), name='iki__cell_lines_cellchecktest__entry__code__pattern'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('label'), 1), name='iki__cell_lines_cellchecktest__entry__label__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('label__regex', '[\\r\\n]'), negated=True), name='iki__cell_lines_cellchecktest__entry__label__one_line'),
                iki.CheckConstraint(condition=iki.Q(('label__regex', '^(?:^\\S(?:.*\\S)?$)$')), name='iki__cell_lines_cellchecktest__entry__label__pattern'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('description'), 1), name='iki__cell_lines_cellchecktest__entry__description__not_empty'),
                iki.CheckConstraint(condition=iki.LessThanOrEqual(iki.Length('description'), 2000), name='iki__cell_lines_cellchecktest__entry__description__max_length'),
                iki.CheckConstraint(condition=iki.Q(('position__gte', 1)), name='iki__cell_lines_cellchecktest__entry__position__min'),
                iki.CheckConstraint(condition=iki.Q(('position__lte', 10000)), name='iki__cell_lines_cellchecktest__entry__position__max'),
                iki.CheckConstraint(condition=iki.Q(('result_family__in', ['identity', 'contamination', 'conformance'])), name='iki__cell_lines_cellchecktest__entry__result_family__values'),
            ],
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='CellCheck',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('kind', iki.TextField(db_index=True)),
                ('checked_on', iki.DateField()),
                ('due_on', iki.DateField(null=True)),
                ('method', iki.CharField(max_length=240)),
                ('result', iki.TextField(db_index=True)),
                ('review', iki.StateField(db_comment='A state field (ADR 0125): one of recorded, accepted, signed_off, submitted, reviewed, released.', db_default='recorded', locking=['accepted', 'signed_off', 'submitted', 'reviewed', 'released'], states=['recorded', 'accepted', 'signed_off', 'submitted', 'reviewed', 'released'])),
                ('bank', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__cell_lines.cellbank')),
                ('report', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to=iki.FILE)),
                ('lab_test', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to='iki__cell_lines.cellchecktest_entry')),
            ],
            table='iki__cell_lines_cellcheck',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='CellCulture',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('event', iki.TextField()),
                ('event_on', iki.DateField(db_index=True)),
                ('passage_number', iki.BigIntegerField(null=True)),
                ('passage_unknown', iki.BooleanField(db_default=False)),
                ('bank', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to='iki__cell_lines.cellbank')),
                ('line', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to=iki.ENTITY)),
                ('mycoplasma_check', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to='iki__cell_lines.cellcheck')),
                ('parent_culture', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to='iki__cell_lines.cellculture')),
                ('str_check', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to='iki__cell_lines.cellcheck')),
                ('vial', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to='iki__cell_lines.cellvial')),
            ],
            table='iki__cell_lines_cellculture',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='CellLineSource_Entry',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('catalogue_id', iki.CharField(db_index=True, max_length=128)),
                ('supplier', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__inventory.supplier_entry')),
            ],
            table='iki__cell_lines_celllinesource__entry',
            bases=(iki.ENTITY,),
        ),
        iki.AddField(
            model_name='cellbank',
            name='catalogue_source',
            field=iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to='iki__cell_lines.celllinesource_entry'),
        ),
        iki.CreateModel(
            name='CellSample',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('name', iki.CharField(db_index=True, max_length=160)),
                ('collected_on', iki.DateField(db_index=True)),
                ('culture', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__cell_lines.cellculture')),
                ('data_file', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to=iki.FILE)),
            ],
            table='iki__cell_lines_cellsample',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='EngineeredChange_Entry',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('code', iki.CharField(db_index=True, max_length=64)),
                ('label', iki.CharField(db_index=True, max_length=200)),
                ('description', iki.TextField(null=True)),
                ('position', iki.BigIntegerField(null=True)),
            ],
            table='iki__cell_lines_engineeredchange__entry',
            indexes=[
                iki.GinIndex(fields=['code'], name='code_trgm_ba98b689febbc3ed98ae', opclasses=['gin_trgm_ops']),
                iki.GinIndex(fields=['label'], name='label_trgm_324813257b69f7ba1e0', opclasses=['gin_trgm_ops']),
                iki.GinIndex(iki.SearchVector(iki.Left('description', 100000), config='simple'), name='descriptio_fts_476df321d00f593'),
            ],
            constraints=[
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('code'), 1), name='iki__cell_lines_engineeredchange__entry__code__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('code__regex', '[\\r\\n]'), negated=True), name='iki__cell_lines_engineeredchange__entry__code__one_line'),
                iki.CheckConstraint(condition=iki.Q(('code__regex', '^(?:^[A-Za-z0-9][A-Za-z0-9_.+-]*$)$')), name='iki__cell_lines_engineeredchange__entry__code__pattern'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('label'), 1), name='iki__cell_lines_engineeredchange__entry__label__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('label__regex', '[\\r\\n]'), negated=True), name='iki__cell_lines_engineeredchange__entry__label__one_line'),
                iki.CheckConstraint(condition=iki.Q(('label__regex', '^(?:^\\S(?:.*\\S)?$)$')), name='iki__cell_lines_engineeredchange__entry__label__pattern'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('description'), 1), name='iki__cell_lines_engineeredchange__entry__description__not_empty'),
                iki.CheckConstraint(condition=iki.LessThanOrEqual(iki.Length('description'), 2000), name='iki__cell_lines_engineeredchange__entry__descriptio__1e65336530'),
                iki.CheckConstraint(condition=iki.Q(('position__gte', 1)), name='iki__cell_lines_engineeredchange__entry__position__min'),
                iki.CheckConstraint(condition=iki.Q(('position__lte', 10000)), name='iki__cell_lines_engineeredchange__entry__position__max'),
            ],
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='EngineeredLine_changes',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity', 'position')),
                ('entity', iki.ForeignKey(db_column='entity_id', on_delete=iki.CASCADE, related_name='+', to='iki__cell_lines.engineeredline')),
                ('position', iki.PositiveIntegerField()),
                ('description', iki.CharField(max_length=500)),
            ],
            table='iki__cell_lines_engineeredline__changes',
        ),
        iki.AddField(
            model_name='engineeredline',
            name='parent_source',
            field=iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to='iki__cell_lines.celllinesource_entry'),
        ),
        iki.CreateModel(
            name='ParentalLine',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('name', iki.CharField(db_index=True, max_length=255)),
            ],
            table='iki__cell_lines_parentalline',
            indexes=[
                iki.GinIndex(fields=['name'], name='name_trgm_7796ff1bce6d707e70ef', opclasses=['gin_trgm_ops']),
            ],
            constraints=[
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('name'), 1), name='iki__cell_lines_parentalline__name__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('name__regex', '[\\r\\n]'), negated=True), name='iki__cell_lines_parentalline__name__one_line'),
            ],
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='ParentalLine_Entry',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('catalogue', iki.TextField(db_default='cellosaurus', db_index=True)),
                ('cellosaurus_id', iki.CharField(db_index=True, max_length=9, null=True)),
                ('local_id', iki.CharField(db_index=True, max_length=64, null=True)),
            ],
            table='iki__cell_lines_parentalline__entry',
            indexes=[
                iki.GinIndex(fields=['cellosaurus_id'], name='cellosauru_trgm_3d6cbb5d16e180', opclasses=['gin_trgm_ops']),
                iki.GinIndex(fields=['local_id'], name='local_id_trgm_c0c2d9e8c7d9562d', opclasses=['gin_trgm_ops']),
            ],
            constraints=[
                iki.CheckConstraint(condition=iki.Q(('catalogue__in', ['cellosaurus', 'local'])), name='iki__cell_lines_parentalline__entry__catalogue__values'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('cellosaurus_id'), 1), name='iki__cell_lines_parentalline__entry__cellosaurus_id__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('cellosaurus_id__regex', '[\\r\\n]'), negated=True), name='iki__cell_lines_parentalline__entry__cellosaurus_id__one_line'),
                iki.CheckConstraint(condition=iki.Q(('cellosaurus_id__regex', '^(?:^CVCL_[A-Z0-9]{4}$)$')), name='iki__cell_lines_parentalline__entry__cellosaurus_id__pattern'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('local_id'), 1), name='iki__cell_lines_parentalline__entry__local_id__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('local_id__regex', '[\\r\\n]'), negated=True), name='iki__cell_lines_parentalline__entry__local_id__one_line'),
                iki.CheckConstraint(condition=iki.Q(('local_id__regex', '^(?:^[^\\s]+$)$')), name='iki__cell_lines_parentalline__entry__local_id__pattern'),
            ],
            bases=(iki.ENTITY,),
        ),
        iki.AddField(
            model_name='celllinesource_entry',
            name='line',
            field=iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__cell_lines.parentalline_entry'),
        ),
        iki.AddConstraint(
            model_name='cellcheck',
            constraint=iki.CheckConstraint(condition=iki.Q(('kind__in', ['str_authentication', 'mycoplasma', 'sterility', 'species_identity', 'karyotype', 'viral_testing', 'other'])), name='iki__cell_lines_cellcheck__kind__values'),
        ),
        iki.AddConstraint(
            model_name='cellcheck',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('method'), 1), name='iki__cell_lines_cellcheck__method__not_empty'),
        ),
        iki.AddConstraint(
            model_name='cellcheck',
            constraint=iki.CheckConstraint(condition=iki.Q(('method__regex', '[\\r\\n]'), negated=True), name='iki__cell_lines_cellcheck__method__one_line'),
        ),
        iki.AddConstraint(
            model_name='cellcheck',
            constraint=iki.CheckConstraint(condition=iki.Q(('result__in', ['match', 'mismatch', 'positive', 'negative', 'conforms', 'does_not_conform', 'inconclusive'])), name='iki__cell_lines_cellcheck__result__values'),
        ),
        iki.AddConstraint(
            model_name='cellcheck',
            constraint=iki.CheckConstraint(condition=iki.Q(('review__in', ['recorded', 'accepted', 'signed_off', 'submitted', 'reviewed', 'released'])), name='iki__cell_lines_cellcheck__review__known'),
        ),
        iki.AddIndex(
            model_name='cellbank',
            index=iki.GinIndex(fields=['name'], name='name_trgm_1a438c9804b30ad8a9b2', opclasses=['gin_trgm_ops']),
        ),
        iki.AddConstraint(
            model_name='cellbank',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('name'), 1), name='iki__cell_lines_cellbank__name__not_empty'),
        ),
        iki.AddConstraint(
            model_name='cellbank',
            constraint=iki.CheckConstraint(condition=iki.Q(('name__regex', '[\\r\\n]'), negated=True), name='iki__cell_lines_cellbank__name__one_line'),
        ),
        iki.AddConstraint(
            model_name='cellbank',
            constraint=iki.CheckConstraint(condition=iki.Q(('kind__in', ['master', 'working', 'stock'])), name='iki__cell_lines_cellbank__kind__values'),
        ),
        iki.AddConstraint(
            model_name='cellbank',
            constraint=iki.CheckConstraint(condition=iki.Q(('passage_number__gte', 0)), name='iki__cell_lines_cellbank__passage_number__min'),
        ),
        iki.AddIndex(
            model_name='cellsample',
            index=iki.GinIndex(fields=['name'], name='name_trgm_daa6b6091d37564db446', opclasses=['gin_trgm_ops']),
        ),
        iki.AddConstraint(
            model_name='cellsample',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('name'), 1), name='iki__cell_lines_cellsample__name__not_empty'),
        ),
        iki.AddConstraint(
            model_name='cellsample',
            constraint=iki.CheckConstraint(condition=iki.Q(('name__regex', '[\\r\\n]'), negated=True), name='iki__cell_lines_cellsample__name__one_line'),
        ),
        iki.AddConstraint(
            model_name='cellculture',
            constraint=iki.CheckConstraint(condition=iki.Q(('event__in', ['thaw', 'passage', 'start'])), name='iki__cell_lines_cellculture__event__values'),
        ),
        iki.AddConstraint(
            model_name='cellculture',
            constraint=iki.CheckConstraint(condition=iki.Q(('passage_number__gte', 0)), name='iki__cell_lines_cellculture__passage_number__min'),
        ),
        iki.AddField(
            model_name='engineeredline_changes',
            name='kind',
            field=iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__cell_lines.engineeredchange_entry'),
        ),
        iki.AddIndex(
            model_name='engineeredline',
            index=iki.GinIndex(fields=['name'], name='name_trgm_679c6fd38fd002791114', opclasses=['gin_trgm_ops']),
        ),
        iki.AddConstraint(
            model_name='engineeredline',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('name'), 1), name='iki__cell_lines_engineeredline__name__not_empty'),
        ),
        iki.AddConstraint(
            model_name='engineeredline',
            constraint=iki.CheckConstraint(condition=iki.Q(('name__regex', '[\\r\\n]'), negated=True), name='iki__cell_lines_engineeredline__name__one_line'),
        ),
        iki.AddIndex(
            model_name='celllinesource_entry',
            index=iki.GinIndex(fields=['catalogue_id'], name='catalogue_trgm_006d1ff0980c365', opclasses=['gin_trgm_ops']),
        ),
        iki.AddConstraint(
            model_name='celllinesource_entry',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('catalogue_id'), 1), name='iki__cell_lines_celllinesource__entry__catalogue_id__not_empty'),
        ),
        iki.AddConstraint(
            model_name='celllinesource_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('catalogue_id__regex', '[\\r\\n]'), negated=True), name='iki__cell_lines_celllinesource__entry__catalogue_id__one_line'),
        ),
        iki.AddConstraint(
            model_name='engineeredline_changes',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('description'), 1), name='iki__cell_lines_engineeredline__changes__description__not_empty'),
        ),
        iki.AddConstraint(
            model_name='engineeredline_changes',
            constraint=iki.CheckConstraint(condition=iki.Q(('description__regex', '[\\r\\n]'), negated=True), name='iki__cell_lines_engineeredline__changes__description__one_line'),
        ),
        iki.AddConstraint(
            model_name='engineeredline_changes',
            constraint=iki.CheckConstraint(condition=iki.Q(('position__lt', 100)), name='iki__cell_lines_engineeredline__changes__position__max_items'),
        ),
    ]
