# Made by `iki plugin makemigrations`. Review it, and commit it with the manifests it
# migrates: once a deployment has applied it, it never changes.

from iki_server.plugins import operations as iki


class Migration(iki.Migration):

    initial = True

    dependencies = [
        ('iki__lots', '0001_initial'),
        ('iki_anchors', '0001_iki_0_1'),
    ]

    operations = [
        iki.CreateModel(
            name='StockLot',
            fields=[
                ('lot_ptr', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to='iki__lots.lot')),
                ('received_as', iki.TextField(db_index=True, null=True)),
                ('received_from', iki.CharField(max_length=200, null=True)),
                ('receipt_key', iki.CharField(db_index=True, max_length=64, null=True)),
                ('delivery_note', iki.CharField(max_length=160, null=True)),
                ('delivery_line', iki.CharField(max_length=100, null=True)),
                ('packs', iki.BigIntegerField(null=True)),
            ],
            table='iki__reagents_stocklot',
            bases=('iki__lots.lot',),
        ),
        iki.CreateModel(
            name='CountStockLot_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
            ],
            table='iki__reagents_countstocklot__history',
        ),
        iki.AppendOnly(
            model_name='CountStockLot_History',
        ),
        iki.CreateModel(
            name='MassStockLot_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
            ],
            table='iki__reagents_massstocklot__history',
        ),
        iki.AppendOnly(
            model_name='MassStockLot_History',
        ),
        iki.CreateModel(
            name='ReorderLine',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('order_reference', iki.CharField(db_index=True, max_length=100)),
                ('ordered_packs', iki.BigIntegerField()),
                ('received_packs', iki.BigIntegerField(null=True)),
            ],
            table='iki__reagents_reorderline',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='ReorderLine_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('order_reference', iki.TextField(null=True)),
                ('item', iki.UUIDField(db_column='item_id', null=True)),
                ('ordered_packs', iki.BigIntegerField(null=True)),
                ('received_packs', iki.BigIntegerField(null=True)),
            ],
            table='iki__reagents_reorderline__history',
        ),
        iki.AppendOnly(
            model_name='ReorderLine_History',
        ),
        iki.CreateModel(
            name='StockStore',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('name', iki.CharField(db_index=True, max_length=160)),
            ],
            table='iki__reagents_stockstore',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='StockArea_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
            ],
            table='iki__reagents_stockarea__history',
        ),
        iki.AppendOnly(
            model_name='StockArea_History',
        ),
        iki.CreateModel(
            name='StockItem',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('name', iki.CharField(db_index=True, max_length=200)),
                ('supplier', iki.CharField(max_length=200)),
                ('catalogue_number', iki.CharField(db_index=True, max_length=100)),
                ('stock_unit', iki.TextField()),
                ('pack_size', iki.FloatField()),
                ('low_stock_at', iki.FloatField(null=True)),
                ('release_requirement', iki.StateField(db_comment='A state field (ADR 0125): one of not_required, required.', db_default='not_required', states=['not_required', 'required'])),
            ],
            table='iki__reagents_stockitem',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='StockItem_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('name', iki.TextField(null=True)),
                ('supplier', iki.TextField(null=True)),
                ('catalogue_number', iki.TextField(null=True)),
                ('stock_unit', iki.TextField(null=True)),
                ('pack_size', iki.FloatField(null=True)),
                ('low_stock_at', iki.FloatField(null=True)),
                ('release_requirement', iki.TextField(null=True)),
            ],
            table='iki__reagents_stockitem__history',
        ),
        iki.AppendOnly(
            model_name='StockItem_History',
        ),
        iki.CreateModel(
            name='StockLocation_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('rows', iki.BigIntegerField(null=True)),
                ('columns', iki.BigIntegerField(null=True)),
            ],
            table='iki__reagents_stocklocation__history',
        ),
        iki.AppendOnly(
            model_name='StockLocation_History',
        ),
        iki.CreateModel(
            name='StockLot_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('item', iki.UUIDField(db_column='item_id', null=True)),
                ('order_line', iki.UUIDField(db_column='order_line_id', null=True)),
                ('received_as', iki.TextField(null=True)),
                ('received_from', iki.TextField(null=True)),
                ('receipt_key', iki.TextField(null=True)),
                ('delivery_note', iki.TextField(null=True)),
                ('delivery_line', iki.TextField(null=True)),
                ('packs', iki.BigIntegerField(null=True)),
            ],
            table='iki__reagents_stocklot__history',
        ),
        iki.AppendOnly(
            model_name='StockLot_History',
        ),
        iki.CreateModel(
            name='StockStore_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('name', iki.TextField(null=True)),
            ],
            table='iki__reagents_stockstore__history',
        ),
        iki.AppendOnly(
            model_name='StockStore_History',
        ),
        iki.CreateModel(
            name='VolumeStockLot_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
            ],
            table='iki__reagents_volumestocklot__history',
        ),
        iki.AppendOnly(
            model_name='VolumeStockLot_History',
        ),
        iki.CreateModel(
            name='CountStockLot',
            fields=[
                ('stocklot_ptr', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to='iki__reagents.stocklot')),
            ],
            table='iki__reagents_countstocklot',
            bases=('iki__reagents.stocklot',),
        ),
        iki.CreateModel(
            name='MassStockLot',
            fields=[
                ('stocklot_ptr', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to='iki__reagents.stocklot')),
            ],
            table='iki__reagents_massstocklot',
            bases=('iki__reagents.stocklot',),
        ),
        iki.CreateModel(
            name='VolumeStockLot',
            fields=[
                ('stocklot_ptr', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to='iki__reagents.stocklot')),
            ],
            table='iki__reagents_volumestocklot',
            bases=('iki__reagents.stocklot',),
        ),
        iki.AddField(
            model_name='stocklot',
            name='order_line',
            field=iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to='iki__reagents.reorderline'),
        ),
        iki.CreateModel(
            name='StockArea',
            fields=[
                ('stockstore_ptr', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to='iki__reagents.stockstore')),
            ],
            table='iki__reagents_stockarea',
            bases=('iki__reagents.stockstore',),
        ),
        iki.CreateModel(
            name='StockLocation',
            fields=[
                ('stockstore_ptr', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to='iki__reagents.stockstore')),
                ('rows', iki.BigIntegerField(db_default=1)),
                ('columns', iki.BigIntegerField(db_default=12)),
            ],
            table='iki__reagents_stocklocation',
            bases=('iki__reagents.stockstore',),
        ),
        iki.AddIndex(
            model_name='stockstore',
            index=iki.GinIndex(fields=['name'], name='name_trgm_28a070b0bd8c2fad61b1', opclasses=['gin_trgm_ops']),
        ),
        iki.AddConstraint(
            model_name='stockstore',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('name'), 1), name='iki__reagents_stockstore__name__not_empty'),
        ),
        iki.AddConstraint(
            model_name='stockstore',
            constraint=iki.CheckConstraint(condition=iki.Q(('name__regex', '[\\r\\n]'), negated=True), name='iki__reagents_stockstore__name__one_line'),
        ),
        iki.AddField(
            model_name='stocklot',
            name='item',
            field=iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__reagents.stockitem'),
        ),
        iki.AddIndex(
            model_name='stockitem',
            index=iki.GinIndex(fields=['name'], name='name_trgm_d7a6e1246ae0bfb85c1e', opclasses=['gin_trgm_ops']),
        ),
        iki.AddIndex(
            model_name='stockitem',
            index=iki.GinIndex(fields=['catalogue_number'], name='catalogue_trgm_60cfad27de3f0d5', opclasses=['gin_trgm_ops']),
        ),
        iki.AddConstraint(
            model_name='stockitem',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('name'), 1), name='iki__reagents_stockitem__name__not_empty'),
        ),
        iki.AddConstraint(
            model_name='stockitem',
            constraint=iki.CheckConstraint(condition=iki.Q(('name__regex', '[\\r\\n]'), negated=True), name='iki__reagents_stockitem__name__one_line'),
        ),
        iki.AddConstraint(
            model_name='stockitem',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('supplier'), 1), name='iki__reagents_stockitem__supplier__not_empty'),
        ),
        iki.AddConstraint(
            model_name='stockitem',
            constraint=iki.CheckConstraint(condition=iki.Q(('supplier__regex', '[\\r\\n]'), negated=True), name='iki__reagents_stockitem__supplier__one_line'),
        ),
        iki.AddConstraint(
            model_name='stockitem',
            constraint=iki.CheckConstraint(condition=iki.Q(('supplier__regex', '^(?:.*\\S.*)$')), name='iki__reagents_stockitem__supplier__pattern'),
        ),
        iki.AddConstraint(
            model_name='stockitem',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('catalogue_number'), 1), name='iki__reagents_stockitem__catalogue_number__not_empty'),
        ),
        iki.AddConstraint(
            model_name='stockitem',
            constraint=iki.CheckConstraint(condition=iki.Q(('catalogue_number__regex', '[\\r\\n]'), negated=True), name='iki__reagents_stockitem__catalogue_number__one_line'),
        ),
        iki.AddConstraint(
            model_name='stockitem',
            constraint=iki.CheckConstraint(condition=iki.Q(('catalogue_number__regex', '^(?:.*\\S.*)$')), name='iki__reagents_stockitem__catalogue_number__pattern'),
        ),
        iki.AddConstraint(
            model_name='stockitem',
            constraint=iki.CheckConstraint(condition=iki.Q(('stock_unit__in', ['mg', 'mL', '1'])), name='iki__reagents_stockitem__stock_unit__values'),
        ),
        iki.AddConstraint(
            model_name='stockitem',
            constraint=iki.CheckConstraint(condition=iki.Q(('pack_size__gt', float('-inf')), ('pack_size__lt', float('inf'))), name='iki__reagents_stockitem__pack_size__finite'),
        ),
        iki.AddConstraint(
            model_name='stockitem',
            constraint=iki.CheckConstraint(condition=iki.Q(('pack_size__gte', 0)), name='iki__reagents_stockitem__pack_size__min'),
        ),
        iki.AddConstraint(
            model_name='stockitem',
            constraint=iki.CheckConstraint(condition=iki.Q(('low_stock_at__gt', float('-inf')), ('low_stock_at__lt', float('inf'))), name='iki__reagents_stockitem__low_stock_at__finite'),
        ),
        iki.AddConstraint(
            model_name='stockitem',
            constraint=iki.CheckConstraint(condition=iki.Q(('low_stock_at__gte', 0)), name='iki__reagents_stockitem__low_stock_at__min'),
        ),
        iki.AddConstraint(
            model_name='stockitem',
            constraint=iki.CheckConstraint(condition=iki.Q(('release_requirement__in', ['not_required', 'required'])), name='iki__reagents_stockitem__release_requirement__known'),
        ),
        iki.AddField(
            model_name='reorderline',
            name='item',
            field=iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__reagents.stockitem'),
        ),
        iki.AddConstraint(
            model_name='stocklocation',
            constraint=iki.CheckConstraint(condition=iki.Q(('rows__gte', 1)), name='iki__reagents_stocklocation__rows__min'),
        ),
        iki.AddConstraint(
            model_name='stocklocation',
            constraint=iki.CheckConstraint(condition=iki.Q(('rows__lte', 26)), name='iki__reagents_stocklocation__rows__max'),
        ),
        iki.AddConstraint(
            model_name='stocklocation',
            constraint=iki.CheckConstraint(condition=iki.Q(('columns__gte', 1)), name='iki__reagents_stocklocation__columns__min'),
        ),
        iki.AddConstraint(
            model_name='stocklocation',
            constraint=iki.CheckConstraint(condition=iki.Q(('columns__lte', 48)), name='iki__reagents_stocklocation__columns__max'),
        ),
        iki.AddIndex(
            model_name='stocklot',
            index=iki.GinIndex(fields=['receipt_key'], name='receipt_ke_trgm_1b100215b6bfed', opclasses=['gin_trgm_ops']),
        ),
        iki.AddConstraint(
            model_name='stocklot',
            constraint=iki.CheckConstraint(condition=iki.Q(('received_as__in', ['order', 'delivery', 'transfer', 'opening_balance'])), name='iki__reagents_stocklot__received_as__values'),
        ),
        iki.AddConstraint(
            model_name='stocklot',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('received_from'), 1), name='iki__reagents_stocklot__received_from__not_empty'),
        ),
        iki.AddConstraint(
            model_name='stocklot',
            constraint=iki.CheckConstraint(condition=iki.Q(('received_from__regex', '[\\r\\n]'), negated=True), name='iki__reagents_stocklot__received_from__one_line'),
        ),
        iki.AddConstraint(
            model_name='stocklot',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('receipt_key'), 1), name='iki__reagents_stocklot__receipt_key__not_empty'),
        ),
        iki.AddConstraint(
            model_name='stocklot',
            constraint=iki.CheckConstraint(condition=iki.Q(('receipt_key__regex', '[\\r\\n]'), negated=True), name='iki__reagents_stocklot__receipt_key__one_line'),
        ),
        iki.AddConstraint(
            model_name='stocklot',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('delivery_note'), 1), name='iki__reagents_stocklot__delivery_note__not_empty'),
        ),
        iki.AddConstraint(
            model_name='stocklot',
            constraint=iki.CheckConstraint(condition=iki.Q(('delivery_note__regex', '[\\r\\n]'), negated=True), name='iki__reagents_stocklot__delivery_note__one_line'),
        ),
        iki.AddConstraint(
            model_name='stocklot',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('delivery_line'), 1), name='iki__reagents_stocklot__delivery_line__not_empty'),
        ),
        iki.AddConstraint(
            model_name='stocklot',
            constraint=iki.CheckConstraint(condition=iki.Q(('delivery_line__regex', '[\\r\\n]'), negated=True), name='iki__reagents_stocklot__delivery_line__one_line'),
        ),
        iki.AddConstraint(
            model_name='stocklot',
            constraint=iki.CheckConstraint(condition=iki.Q(('packs__gte', 1)), name='iki__reagents_stocklot__packs__min'),
        ),
        iki.AddIndex(
            model_name='reorderline',
            index=iki.GinIndex(fields=['order_reference'], name='order_refe_trgm_da79377bd19b3b', opclasses=['gin_trgm_ops']),
        ),
        iki.AddConstraint(
            model_name='reorderline',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('order_reference'), 1), name='iki__reagents_reorderline__order_reference__not_empty'),
        ),
        iki.AddConstraint(
            model_name='reorderline',
            constraint=iki.CheckConstraint(condition=iki.Q(('order_reference__regex', '[\\r\\n]'), negated=True), name='iki__reagents_reorderline__order_reference__one_line'),
        ),
        iki.AddConstraint(
            model_name='reorderline',
            constraint=iki.CheckConstraint(condition=iki.Q(('order_reference__regex', '^(?:.*\\S.*)$')), name='iki__reagents_reorderline__order_reference__pattern'),
        ),
        iki.AddConstraint(
            model_name='reorderline',
            constraint=iki.CheckConstraint(condition=iki.Q(('ordered_packs__gte', 1)), name='iki__reagents_reorderline__ordered_packs__min'),
        ),
        iki.AddConstraint(
            model_name='reorderline',
            constraint=iki.CheckConstraint(condition=iki.Q(('ordered_packs__lte', 1000000000)), name='iki__reagents_reorderline__ordered_packs__max'),
        ),
        iki.AddConstraint(
            model_name='reorderline',
            constraint=iki.CheckConstraint(condition=iki.Q(('received_packs__gte', 0)), name='iki__reagents_reorderline__received_packs__min'),
        ),
    ]
