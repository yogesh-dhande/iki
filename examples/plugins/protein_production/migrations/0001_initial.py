# Made by `iki plugin makemigrations`. Review it, and commit it with the manifests it
# migrates: once a deployment has applied it, it never changes.

from iki_server.plugins import operations as iki


class Migration(iki.Migration):

    initial = True

    dependencies = [
        ('iki__protein', '0001_initial'),
        ('iki__requests', '0001_initial'),
        ('iki_anchors', '0001_iki_0_1'),
    ]

    operations = [
        iki.CreateModel(
            name='ProductionDeliverable_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('protein', iki.UUIDField(db_column='protein_id', null=True)),
                ('lot_number', iki.TextField(null=True)),
                ('delivered_mass', iki.QuantityField(null=True, unit='mg')),
            ],
            table='acme__protein_production_productiondeliverable__history',
        ),
        iki.AppendOnly(
            model_name='ProductionDeliverable_History',
        ),
        iki.CreateModel(
            name='ProductionRequest_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('requested_mass', iki.QuantityField(null=True, unit='mg')),
            ],
            table='acme__protein_production_productionrequest__history',
        ),
        iki.AppendOnly(
            model_name='ProductionRequest_History',
        ),
        iki.CreateModel(
            name='ProductionRequest',
            fields=[
                ('researchrequest_ptr', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to='iki__requests.researchrequest')),
                ('requested_mass', iki.QuantityField(unit='mg')),
            ],
            table='acme__protein_production_productionrequest',
            constraints=[
                iki.CheckConstraint(condition=iki.Q(('requested_mass__gt', float('-inf')), ('requested_mass__lt', float('inf'))), name='acme__protein_production_productionrequest__request__36408fc458'),
                iki.CheckConstraint(condition=iki.Q(('requested_mass__gte', 0)), name='acme__protein_production_productionrequest__requested_mass__min'),
            ],
            bases=('iki__requests.researchrequest',),
        ),
        iki.CreateModel(
            name='ProductionDeliverable',
            fields=[
                ('researchdeliverable_ptr', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to='iki__requests.researchdeliverable')),
                ('lot_number', iki.CharField(max_length=100, null=True)),
                ('delivered_mass', iki.QuantityField(null=True, unit='mg')),
                ('protein', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__protein.protein_entry')),
            ],
            table='acme__protein_production_productiondeliverable',
            constraints=[
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('lot_number'), 1), name='acme__protein_production_productiondeliverable__lot__dd7573da51'),
                iki.CheckConstraint(condition=iki.Q(('lot_number__regex', '[\\r\\n]'), negated=True), name='acme__protein_production_productiondeliverable__lot__61ec8da0f2'),
                iki.CheckConstraint(condition=iki.Q(('delivered_mass__gt', float('-inf')), ('delivered_mass__lt', float('inf'))), name='acme__protein_production_productiondeliverable__del__5bd66cf455'),
                iki.CheckConstraint(condition=iki.Q(('delivered_mass__gte', 0)), name='acme__protein_production_productiondeliverable__del__0e3445b3db'),
            ],
            bases=('iki__requests.researchdeliverable',),
        ),
    ]
