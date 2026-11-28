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
            name='Box_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('name', iki.TextField(null=True)),
                ('rows', iki.BigIntegerField(null=True)),
                ('columns', iki.BigIntegerField(null=True)),
                ('barcode', iki.TextField(null=True)),
                ('contents', iki.TextField(null=True)),
            ],
            table='iki__inventory_box__history',
        ),
        iki.AppendOnly(
            model_name='Box_History',
        ),
        iki.CreateModel(
            name='Container',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('name', iki.CharField(max_length=40, null=True)),
                ('barcode', iki.CharField(db_index=True, max_length=64, null=True)),
                ('on_hold', iki.BooleanField(db_default=False, db_index=True, null=True)),
                ('hold_reason', iki.TextField(null=True)),
                ('prepared_on', iki.DateField(null=True)),
                ('contents', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to=iki.ENTITY)),
            ],
            table='iki__inventory_container',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='Container_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('name', iki.TextField(null=True)),
                ('barcode', iki.TextField(null=True)),
                ('contents', iki.UUIDField(db_column='contents_id', null=True)),
                ('on_hold', iki.BooleanField(null=True)),
                ('hold_reason', iki.TextField(null=True)),
                ('prepared_on', iki.DateField(null=True)),
            ],
            table='iki__inventory_container__history',
        ),
        iki.AppendOnly(
            model_name='Container_History',
        ),
        iki.CreateModel(
            name='Freezer_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('name', iki.TextField(null=True)),
                ('asset_tag', iki.TextField(null=True)),
                ('set_point_c', iki.BigIntegerField(null=True)),
                ('room', iki.TextField(null=True)),
            ],
            table='iki__inventory_freezer__history',
        ),
        iki.AppendOnly(
            model_name='Freezer_History',
        ),
        iki.CreateModel(
            name='Rack_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('name', iki.TextField(null=True)),
                ('rows', iki.BigIntegerField(null=True)),
                ('columns', iki.BigIntegerField(null=True)),
                ('barcode', iki.TextField(null=True)),
            ],
            table='iki__inventory_rack__history',
        ),
        iki.AppendOnly(
            model_name='Rack_History',
        ),
        iki.CreateModel(
            name='Shelf_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('name', iki.TextField(null=True)),
            ],
            table='iki__inventory_shelf__history',
        ),
        iki.AppendOnly(
            model_name='Shelf_History',
        ),
        iki.CreateModel(
            name='Supplier_Entry_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('name', iki.TextField(null=True)),
            ],
            table='iki__inventory_supplier__entry__history',
        ),
        iki.AppendOnly(
            model_name='Supplier_Entry_History',
        ),
        iki.CreateModel(
            name='Supplier_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('website', iki.TextField(null=True)),
            ],
            table='iki__inventory_supplier__history',
        ),
        iki.AppendOnly(
            model_name='Supplier_History',
        ),
        iki.CreateModel(
            name='Tube_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('concentration', iki.QuantityField(null=True, unit='mg/mL')),
            ],
            table='iki__inventory_tube__history',
        ),
        iki.AppendOnly(
            model_name='Tube_History',
        ),
        iki.CreateModel(
            name='Box',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('name', iki.CharField(db_index=True, max_length=100)),
                ('rows', iki.BigIntegerField(db_default=9)),
                ('columns', iki.BigIntegerField(db_default=9)),
                ('barcode', iki.CharField(db_index=True, max_length=64, null=True)),
                ('contents', iki.CharField(max_length=200, null=True)),
            ],
            table='iki__inventory_box',
            indexes=[
                iki.GinIndex(fields=['name'], name='name_trgm_bb76599ca1263c4cd8bc', opclasses=['gin_trgm_ops']),
                iki.GinIndex(fields=['barcode'], name='barcode_trgm_48531ea9d2c71e619', opclasses=['gin_trgm_ops']),
            ],
            constraints=[
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('name'), 1), name='iki__inventory_box__name__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('name__regex', '[\\r\\n]'), negated=True), name='iki__inventory_box__name__one_line'),
                iki.CheckConstraint(condition=iki.Q(('rows__gte', 1)), name='iki__inventory_box__rows__min'),
                iki.CheckConstraint(condition=iki.Q(('rows__lte', 16)), name='iki__inventory_box__rows__max'),
                iki.CheckConstraint(condition=iki.Q(('columns__gte', 1)), name='iki__inventory_box__columns__min'),
                iki.CheckConstraint(condition=iki.Q(('columns__lte', 24)), name='iki__inventory_box__columns__max'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('barcode'), 1), name='iki__inventory_box__barcode__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('barcode__regex', '[\\r\\n]'), negated=True), name='iki__inventory_box__barcode__one_line'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('contents'), 1), name='iki__inventory_box__contents__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('contents__regex', '[\\r\\n]'), negated=True), name='iki__inventory_box__contents__one_line'),
            ],
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='Freezer',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('name', iki.CharField(db_index=True, max_length=100)),
                ('asset_tag', iki.CharField(db_index=True, max_length=64, null=True)),
                ('set_point_c', iki.BigIntegerField(null=True)),
                ('room', iki.CharField(max_length=100, null=True)),
            ],
            table='iki__inventory_freezer',
            indexes=[
                iki.GinIndex(fields=['name'], name='name_trgm_7d184edc3cae5a9962af', opclasses=['gin_trgm_ops']),
                iki.GinIndex(fields=['asset_tag'], name='asset_tag_trgm_bb0e11ca6616730', opclasses=['gin_trgm_ops']),
            ],
            constraints=[
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('name'), 1), name='iki__inventory_freezer__name__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('name__regex', '[\\r\\n]'), negated=True), name='iki__inventory_freezer__name__one_line'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('asset_tag'), 1), name='iki__inventory_freezer__asset_tag__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('asset_tag__regex', '[\\r\\n]'), negated=True), name='iki__inventory_freezer__asset_tag__one_line'),
                iki.CheckConstraint(condition=iki.Q(('set_point_c__gte', -196)), name='iki__inventory_freezer__set_point_c__min'),
                iki.CheckConstraint(condition=iki.Q(('set_point_c__lte', 25)), name='iki__inventory_freezer__set_point_c__max'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('room'), 1), name='iki__inventory_freezer__room__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('room__regex', '[\\r\\n]'), negated=True), name='iki__inventory_freezer__room__one_line'),
            ],
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='Rack',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('name', iki.CharField(max_length=100)),
                ('rows', iki.BigIntegerField(db_default=5)),
                ('columns', iki.BigIntegerField(db_default=4)),
                ('barcode', iki.CharField(db_index=True, max_length=64, null=True)),
            ],
            table='iki__inventory_rack',
            indexes=[
                iki.GinIndex(fields=['barcode'], name='barcode_trgm_8e1989596c102022e', opclasses=['gin_trgm_ops']),
            ],
            constraints=[
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('name'), 1), name='iki__inventory_rack__name__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('name__regex', '[\\r\\n]'), negated=True), name='iki__inventory_rack__name__one_line'),
                iki.CheckConstraint(condition=iki.Q(('rows__gte', 1)), name='iki__inventory_rack__rows__min'),
                iki.CheckConstraint(condition=iki.Q(('rows__lte', 16)), name='iki__inventory_rack__rows__max'),
                iki.CheckConstraint(condition=iki.Q(('columns__gte', 1)), name='iki__inventory_rack__columns__min'),
                iki.CheckConstraint(condition=iki.Q(('columns__lte', 8)), name='iki__inventory_rack__columns__max'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('barcode'), 1), name='iki__inventory_rack__barcode__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('barcode__regex', '[\\r\\n]'), negated=True), name='iki__inventory_rack__barcode__one_line'),
            ],
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='Shelf',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('name', iki.CharField(max_length=100)),
            ],
            table='iki__inventory_shelf',
            constraints=[
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('name'), 1), name='iki__inventory_shelf__name__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('name__regex', '[\\r\\n]'), negated=True), name='iki__inventory_shelf__name__one_line'),
            ],
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='Supplier',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('website', iki.CharField(max_length=500, null=True)),
            ],
            table='iki__inventory_supplier',
            constraints=[
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('website'), 1), name='iki__inventory_supplier__website__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('website__regex', '[\\r\\n]'), negated=True), name='iki__inventory_supplier__website__one_line'),
                iki.CheckConstraint(condition=iki.Q(('website__regex', '^[A-Za-z][A-Za-z0-9+.-]*:[^\\s]+$')), name='iki__inventory_supplier__website__format'),
            ],
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='Supplier_Entry',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('name', iki.CharField(db_index=True, max_length=200)),
            ],
            table='iki__inventory_supplier__entry',
            indexes=[
                iki.GinIndex(fields=['name'], name='name_trgm_877676f7ff3be68e7fdd', opclasses=['gin_trgm_ops']),
            ],
            constraints=[
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('name'), 1), name='iki__inventory_supplier__entry__name__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('name__regex', '[\\r\\n]'), negated=True), name='iki__inventory_supplier__entry__name__one_line'),
                iki.CheckConstraint(condition=iki.Q(('name__regex', '^(?:.*\\S.*)$')), name='iki__inventory_supplier__entry__name__pattern'),
            ],
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='Tube',
            fields=[
                ('container_ptr', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to='iki__inventory.container')),
                ('concentration', iki.QuantityField(null=True, unit='mg/mL')),
            ],
            table='iki__inventory_tube',
            constraints=[
                iki.CheckConstraint(condition=iki.Q(('concentration__gt', float('-inf')), ('concentration__lt', float('inf'))), name='iki__inventory_tube__concentration__finite'),
                iki.CheckConstraint(condition=iki.Q(('concentration__gte', 0)), name='iki__inventory_tube__concentration__min'),
            ],
            bases=('iki__inventory.container',),
        ),
        iki.AddIndex(
            model_name='container',
            index=iki.GinIndex(fields=['barcode'], name='barcode_trgm_a8f4f553a2488629d', opclasses=['gin_trgm_ops']),
        ),
        iki.AddIndex(
            model_name='container',
            index=iki.GinIndex(iki.SearchVector(iki.Left('hold_reason', 100000), config='simple'), name='hold_reaso_fts_d54d786904adadc'),
        ),
        iki.AddConstraint(
            model_name='container',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('name'), 1), name='iki__inventory_container__name__not_empty'),
        ),
        iki.AddConstraint(
            model_name='container',
            constraint=iki.CheckConstraint(condition=iki.Q(('name__regex', '[\\r\\n]'), negated=True), name='iki__inventory_container__name__one_line'),
        ),
        iki.AddConstraint(
            model_name='container',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('barcode'), 1), name='iki__inventory_container__barcode__not_empty'),
        ),
        iki.AddConstraint(
            model_name='container',
            constraint=iki.CheckConstraint(condition=iki.Q(('barcode__regex', '[\\r\\n]'), negated=True), name='iki__inventory_container__barcode__one_line'),
        ),
        iki.AddConstraint(
            model_name='container',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('hold_reason'), 1), name='iki__inventory_container__hold_reason__not_empty'),
        ),
    ]
