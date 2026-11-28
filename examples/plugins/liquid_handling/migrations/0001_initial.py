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
            name='Plate_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('label', iki.TextField(null=True)),
                ('barcode', iki.TextField(null=True)),
                ('labware_type', iki.TextField(null=True)),
                ('wells', iki.TextField(null=True)),
            ],
            table='acme__liquid_handling_plate__history',
        ),
        iki.AppendOnly(
            model_name='Plate_History',
        ),
        iki.CreateModel(
            name='Transfer_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('source', iki.UUIDField(db_column='source_id', null=True)),
                ('source_well', iki.TextField(null=True)),
                ('destination', iki.UUIDField(db_column='destination_id', null=True)),
                ('destination_well', iki.TextField(null=True)),
                ('volume', iki.QuantityField(null=True, unit='uL')),
                ('liquid_class', iki.TextField(null=True)),
            ],
            table='acme__liquid_handling_transfer__history',
        ),
        iki.AppendOnly(
            model_name='Transfer_History',
        ),
        iki.CreateModel(
            name='Plate',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('label', iki.CharField(max_length=32)),
                ('barcode', iki.CharField(db_index=True, max_length=32, null=True)),
                ('labware_type', iki.CharField(max_length=64, null=True)),
                ('wells', iki.TextField()),
            ],
            table='acme__liquid_handling_plate',
            indexes=[
                iki.GinIndex(fields=['barcode'], name='barcode_trgm_5a184d3b4a0f5bb86', opclasses=['gin_trgm_ops']),
            ],
            constraints=[
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('label'), 1), name='acme__liquid_handling_plate__label__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('label__regex', '[\\r\\n]'), negated=True), name='acme__liquid_handling_plate__label__one_line'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('barcode'), 1), name='acme__liquid_handling_plate__barcode__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('barcode__regex', '[\\r\\n]'), negated=True), name='acme__liquid_handling_plate__barcode__one_line'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('labware_type'), 1), name='acme__liquid_handling_plate__labware_type__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('labware_type__regex', '[\\r\\n]'), negated=True), name='acme__liquid_handling_plate__labware_type__one_line'),
                iki.CheckConstraint(condition=iki.Q(('wells__in', ['plate_96', 'plate_384'])), name='acme__liquid_handling_plate__wells__values'),
            ],
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='Transfer',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('source_well', iki.CharField(max_length=4)),
                ('destination_well', iki.CharField(max_length=4)),
                ('volume', iki.QuantityField(unit='uL')),
                ('liquid_class', iki.CharField(max_length=64, null=True)),
                ('destination', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='acme__liquid_handling.plate')),
                ('source', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='acme__liquid_handling.plate')),
            ],
            table='acme__liquid_handling_transfer',
            constraints=[
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('source_well'), 1), name='acme__liquid_handling_transfer__source_well__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('source_well__regex', '[\\r\\n]'), negated=True), name='acme__liquid_handling_transfer__source_well__one_line'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('destination_well'), 1), name='acme__liquid_handling_transfer__destination_well__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('destination_well__regex', '[\\r\\n]'), negated=True), name='acme__liquid_handling_transfer__destination_well__one_line'),
                iki.CheckConstraint(condition=iki.Q(('volume__gt', float('-inf')), ('volume__lt', float('inf'))), name='acme__liquid_handling_transfer__volume__finite'),
                iki.CheckConstraint(condition=iki.Q(('volume__gte', 0)), name='acme__liquid_handling_transfer__volume__min'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('liquid_class'), 1), name='acme__liquid_handling_transfer__liquid_class__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('liquid_class__regex', '[\\r\\n]'), negated=True), name='acme__liquid_handling_transfer__liquid_class__one_line'),
            ],
            bases=(iki.ENTITY,),
        ),
    ]
