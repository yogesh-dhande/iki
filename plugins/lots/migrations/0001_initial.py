# Made by `iki plugin makemigrations`. Review it, and commit it with the manifests it
# migrates: once a deployment has applied it, it never changes.

from iki_server.plugins import operations as iki


class Migration(iki.Migration):

    initial = True

    dependencies = [
        ('iki__conjugate', '0001_initial'),
        ('iki__inventory', '0001_initial'),
        ('iki__oligo', '0001_initial'),
        ('iki__plasmid', '0001_initial'),
        ('iki__polymer', '0001_initial'),
        ('iki_anchors', '0001_iki_0_1'),
    ]

    operations = [
        iki.CreateModel(
            name='AggregatesMethod',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
            ],
            table='iki__lots_aggregatesmethod',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='AggregatesMethod_Entry_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('code', iki.TextField(null=True)),
                ('label', iki.TextField(null=True)),
                ('description', iki.TextField(null=True)),
                ('position', iki.BigIntegerField(null=True)),
            ],
            table='iki__lots_aggregatesmethod__entry__history',
        ),
        iki.AppendOnly(
            model_name='AggregatesMethod_Entry_History',
        ),
        iki.CreateModel(
            name='AggregatesMethod_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
            ],
            table='iki__lots_aggregatesmethod__history',
        ),
        iki.AppendOnly(
            model_name='AggregatesMethod_History',
        ),
        iki.CreateModel(
            name='Lot',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('lot_number', iki.CharField(db_index=True, max_length=100, null=True)),
                ('source', iki.TextField(db_index=True, null=True)),
                ('supplier', iki.CharField(db_index=True, max_length=200, null=True)),
                ('catalogue_number', iki.CharField(db_index=True, max_length=100, null=True)),
                ('made_on', iki.DateField(null=True)),
                ('received_on', iki.DateField(null=True)),
                ('retest_on', iki.DateField(null=True)),
                ('expires_on', iki.DateField(null=True)),
                ('storage', iki.CharField(max_length=200, null=True)),
                ('notes', iki.TextField(null=True)),
                ('requires_qa_release', iki.BooleanField(db_default=False, null=True)),
                ('release_requirement', iki.StateField(db_comment='A state field (ADR 0125): one of as_recorded, required, not_required.', db_default='as_recorded', states=['as_recorded', 'required', 'not_required'])),
                ('disposition', iki.StateField(db_comment='A state field (ADR 0125): one of quarantined, released, available, retest_due, on_hold, rejected, expired.', db_default='quarantined', locking=['released', 'rejected', 'expired'], states=['quarantined', 'released', 'available', 'retest_due', 'on_hold', 'rejected', 'expired'])),
                ('made_by', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to=iki.USER)),
                ('responsible', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to=iki.USER)),
            ],
            table='iki__lots_lot',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='BiologicLot_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('concentration', iki.QuantityField(null=True, unit='mg/mL')),
                ('concentration_method', iki.UUIDField(db_column='concentration_method_id', null=True)),
                ('a280', iki.FloatField(null=True)),
                ('path_length', iki.QuantityField(null=True, unit='cm')),
                ('dilution_factor', iki.FloatField(null=True)),
                ('extinction_coefficient', iki.QuantityField(null=True, unit='mL/(mg.cm)')),
                ('a280_extinction', iki.QuantityField(null=True, unit='mL/(mg.cm)')),
                ('a280_basis', iki.TextField(null=True)),
                ('a280_estimate', iki.BooleanField(null=True)),
                ('a280_concentration', iki.QuantityField(null=True, unit='mg/mL')),
                ('purity', iki.QuantityField(null=True, unit='%')),
                ('purity_method', iki.UUIDField(db_column='purity_method_id', null=True)),
                ('aggregates', iki.QuantityField(null=True, unit='%')),
                ('aggregates_method', iki.UUIDField(db_column='aggregates_method_id', null=True)),
                ('endotoxin', iki.QuantityField(null=True, unit='{EU}/mg')),
                ('buffer', iki.TextField(null=True)),
            ],
            table='iki__lots_biologiclot__history',
        ),
        iki.AppendOnly(
            model_name='BiologicLot_History',
        ),
        iki.CreateModel(
            name='ConjugateLot_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('conjugate', iki.UUIDField(db_column='conjugate_id', null=True)),
                ('dar_method', iki.TextField(null=True)),
                ('distribution_method', iki.TextField(null=True)),
                ('drug_loads', iki.JSONField(null=True)),
                ('dar', iki.FloatField(null=True)),
                ('distribution_dar', iki.FloatField(null=True)),
                ('unconjugated', iki.QuantityField(null=True, unit='%')),
                ('free_drug', iki.QuantityField(null=True, unit='%')),
            ],
            table='iki__lots_conjugatelot__history',
        ),
        iki.AppendOnly(
            model_name='ConjugateLot_History',
        ),
        iki.CreateModel(
            name='DNAConcentrationMethod',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
            ],
            table='iki__lots_dnaconcentrationmethod',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='DNAConcentrationMethod_Entry_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('code', iki.TextField(null=True)),
                ('label', iki.TextField(null=True)),
                ('description', iki.TextField(null=True)),
                ('position', iki.BigIntegerField(null=True)),
                ('calculation', iki.TextField(null=True)),
            ],
            table='iki__lots_dnaconcentrationmethod__entry__history',
        ),
        iki.AppendOnly(
            model_name='DNAConcentrationMethod_Entry_History',
        ),
        iki.CreateModel(
            name='DNAConcentrationMethod_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
            ],
            table='iki__lots_dnaconcentrationmethod__history',
        ),
        iki.AppendOnly(
            model_name='DNAConcentrationMethod_History',
        ),
        iki.CreateModel(
            name='GenericCountLot_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('of', iki.UUIDField(db_column='of_id', null=True)),
                ('item', iki.TextField(null=True)),
                ('purity', iki.QuantityField(null=True, unit='%')),
                ('purity_method', iki.TextField(null=True)),
            ],
            table='iki__lots_genericcountlot__history',
        ),
        iki.AppendOnly(
            model_name='GenericCountLot_History',
        ),
        iki.CreateModel(
            name='GenericLot_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('of', iki.UUIDField(db_column='of_id', null=True)),
                ('concentration', iki.QuantityField(null=True, unit='mg/mL')),
                ('purity', iki.QuantityField(null=True, unit='%')),
                ('purity_method', iki.TextField(null=True)),
            ],
            table='iki__lots_genericlot__history',
        ),
        iki.AppendOnly(
            model_name='GenericLot_History',
        ),
        iki.CreateModel(
            name='GenericVolumeLot_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('of', iki.UUIDField(db_column='of_id', null=True)),
                ('concentration', iki.QuantityField(null=True, unit='mg/mL')),
                ('purity', iki.QuantityField(null=True, unit='%')),
                ('purity_method', iki.TextField(null=True)),
            ],
            table='iki__lots_genericvolumelot__history',
        ),
        iki.AppendOnly(
            model_name='GenericVolumeLot_History',
        ),
        iki.CreateModel(
            name='GlycerolStock',
            fields=[
                ('tube_ptr', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to='iki__inventory.tube')),
            ],
            table='iki__lots_glycerolstock',
            bases=('iki__inventory.tube',),
        ),
        iki.CreateModel(
            name='GlycerolStock_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('isolate', iki.UUIDField(db_column='isolate_id', null=True)),
            ],
            table='iki__lots_glycerolstock__history',
        ),
        iki.AppendOnly(
            model_name='GlycerolStock_History',
        ),
        iki.CreateModel(
            name='Lot_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('lot_number', iki.TextField(null=True)),
                ('source', iki.TextField(null=True)),
                ('supplier', iki.TextField(null=True)),
                ('catalogue_number', iki.TextField(null=True)),
                ('made_by', iki.UUIDField(db_column='made_by_id', null=True)),
                ('made_on', iki.DateField(null=True)),
                ('received_on', iki.DateField(null=True)),
                ('retest_on', iki.DateField(null=True)),
                ('expires_on', iki.DateField(null=True)),
                ('responsible', iki.UUIDField(db_column='responsible_id', null=True)),
                ('storage', iki.TextField(null=True)),
                ('certificates', iki.ArrayField(base_field=iki.UUIDField(), null=True)),
                ('notes', iki.TextField(null=True)),
                ('requires_qa_release', iki.BooleanField(null=True)),
                ('release_requirement', iki.TextField(null=True)),
                ('disposition', iki.TextField(null=True)),
            ],
            table='iki__lots_lot__history',
        ),
        iki.AppendOnly(
            model_name='Lot_History',
        ),
        iki.CreateModel(
            name='MolarMassMethod',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
            ],
            table='iki__lots_molarmassmethod',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='MolarMassMethod_Entry_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('code', iki.TextField(null=True)),
                ('label', iki.TextField(null=True)),
                ('description', iki.TextField(null=True)),
                ('position', iki.BigIntegerField(null=True)),
                ('averages', iki.ArrayField(base_field=iki.TextField(), null=True)),
            ],
            table='iki__lots_molarmassmethod__entry__history',
        ),
        iki.AppendOnly(
            model_name='MolarMassMethod_Entry_History',
        ),
        iki.CreateModel(
            name='MolarMassMethod_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
            ],
            table='iki__lots_molarmassmethod__history',
        ),
        iki.AppendOnly(
            model_name='MolarMassMethod_History',
        ),
        iki.CreateModel(
            name='OligoCounterion',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
            ],
            table='iki__lots_oligocounterion',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='OligoCounterion_Entry_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('code', iki.TextField(null=True)),
                ('label', iki.TextField(null=True)),
                ('description', iki.TextField(null=True)),
                ('position', iki.BigIntegerField(null=True)),
            ],
            table='iki__lots_oligocounterion__entry__history',
        ),
        iki.AppendOnly(
            model_name='OligoCounterion_Entry_History',
        ),
        iki.CreateModel(
            name='OligoCounterion_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
            ],
            table='iki__lots_oligocounterion__history',
        ),
        iki.AppendOnly(
            model_name='OligoCounterion_History',
        ),
        iki.CreateModel(
            name='OligoLot_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('oligonucleotide', iki.UUIDField(db_column='oligonucleotide_id', null=True)),
                ('synthesis_scale', iki.QuantityField(null=True, unit='umol')),
                ('purification', iki.UUIDField(db_column='purification_id', null=True)),
                ('counterion', iki.UUIDField(db_column='counterion_id', null=True)),
                ('yield_od', iki.FloatField(null=True)),
                ('extinction_260', iki.QuantityField(null=True, unit='L/(mol.cm)')),
                ('yield_amount', iki.QuantityField(null=True, unit='nmol')),
                ('concentration', iki.QuantityField(null=True, unit='umol/L')),
                ('purity', iki.QuantityField(null=True, unit='%')),
                ('purity_method', iki.UUIDField(db_column='purity_method_id', null=True)),
                ('measured_mass', iki.QuantityField(null=True, unit='u')),
                ('mass_basis', iki.TextField(null=True)),
                ('mass_tolerance', iki.QuantityField(null=True, unit='u')),
                ('mass_specification', iki.TextField(null=True)),
                ('expected_mass', iki.QuantityField(null=True, unit='u')),
                ('mass_error', iki.QuantityField(null=True, unit='u')),
            ],
            table='iki__lots_oligolot__history',
        ),
        iki.AppendOnly(
            model_name='OligoLot_History',
        ),
        iki.CreateModel(
            name='OligoPurification',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
            ],
            table='iki__lots_oligopurification',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='OligoPurification_Entry_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('code', iki.TextField(null=True)),
                ('label', iki.TextField(null=True)),
                ('description', iki.TextField(null=True)),
                ('position', iki.BigIntegerField(null=True)),
            ],
            table='iki__lots_oligopurification__entry__history',
        ),
        iki.AppendOnly(
            model_name='OligoPurification_Entry_History',
        ),
        iki.CreateModel(
            name='OligoPurification_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
            ],
            table='iki__lots_oligopurification__history',
        ),
        iki.AppendOnly(
            model_name='OligoPurification_History',
        ),
        iki.CreateModel(
            name='OligoPurityMethod',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
            ],
            table='iki__lots_oligopuritymethod',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='OligoPurityMethod_Entry_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('code', iki.TextField(null=True)),
                ('label', iki.TextField(null=True)),
                ('description', iki.TextField(null=True)),
                ('position', iki.BigIntegerField(null=True)),
            ],
            table='iki__lots_oligopuritymethod__entry__history',
        ),
        iki.AppendOnly(
            model_name='OligoPurityMethod_Entry_History',
        ),
        iki.CreateModel(
            name='OligoPurityMethod_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
            ],
            table='iki__lots_oligopuritymethod__history',
        ),
        iki.AppendOnly(
            model_name='OligoPurityMethod_History',
        ),
        iki.CreateModel(
            name='PlasmidPrep_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('plasmid', iki.UUIDField(db_column='plasmid_id', null=True)),
                ('scale', iki.TextField(null=True)),
                ('method', iki.UUIDField(db_column='method_id', null=True)),
                ('kit', iki.TextField(null=True)),
                ('host_strain', iki.TextField(null=True)),
                ('culture_volume', iki.QuantityField(null=True, unit='mL')),
                ('a260', iki.FloatField(null=True)),
                ('path_length', iki.QuantityField(null=True, unit='cm')),
                ('dilution_factor', iki.FloatField(null=True)),
                ('concentration', iki.QuantityField(null=True, unit='ng/uL')),
                ('a260_concentration', iki.QuantityField(null=True, unit='ng/uL')),
                ('concentration_method', iki.UUIDField(db_column='concentration_method_id', null=True)),
                ('a260_a280', iki.FloatField(null=True)),
                ('a260_a230', iki.FloatField(null=True)),
                ('supercoiled', iki.QuantityField(null=True, unit='%')),
                ('endotoxin', iki.QuantityField(null=True, unit='{EU}/ug')),
                ('verified_by', iki.UUIDField(db_column='verified_by_id', null=True)),
            ],
            table='iki__lots_plasmidprep__history',
        ),
        iki.AppendOnly(
            model_name='PlasmidPrep_History',
        ),
        iki.CreateModel(
            name='PlasmidPrepMethod',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
            ],
            table='iki__lots_plasmidprepmethod',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='PlasmidPrepMethod_Entry_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('code', iki.TextField(null=True)),
                ('label', iki.TextField(null=True)),
                ('description', iki.TextField(null=True)),
                ('position', iki.BigIntegerField(null=True)),
            ],
            table='iki__lots_plasmidprepmethod__entry__history',
        ),
        iki.AppendOnly(
            model_name='PlasmidPrepMethod_Entry_History',
        ),
        iki.CreateModel(
            name='PlasmidPrepMethod_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
            ],
            table='iki__lots_plasmidprepmethod__history',
        ),
        iki.AppendOnly(
            model_name='PlasmidPrepMethod_History',
        ),
        iki.CreateModel(
            name='PlasmidVerification_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('prep', iki.UUIDField(db_column='prep_id', null=True)),
                ('plasmid', iki.UUIDField(db_column='plasmid_id', null=True)),
                ('plasmid_record_id', iki.TextField(null=True)),
                ('plasmid_revision', iki.BigIntegerField(null=True)),
                ('alignment', iki.UUIDField(db_column='alignment_id', null=True)),
                ('alignment_revision', iki.BigIntegerField(null=True)),
                ('report', iki.UUIDField(db_column='report_id', null=True)),
                ('outcome', iki.TextField(null=True)),
                ('override_reason', iki.TextField(null=True)),
                ('notes', iki.TextField(null=True)),
                ('reviewer', iki.UUIDField(db_column='reviewer_id', null=True)),
                ('review_flow', iki.TextField(null=True)),
                ('review', iki.TextField(null=True)),
            ],
            table='iki__lots_plasmidverification__history',
        ),
        iki.AppendOnly(
            model_name='PlasmidVerification_History',
        ),
        iki.CreateModel(
            name='PlasmidVerificationMethod',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
            ],
            table='iki__lots_plasmidverificationmethod',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='PlasmidVerificationMethod_Entry_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('code', iki.TextField(null=True)),
                ('label', iki.TextField(null=True)),
                ('description', iki.TextField(null=True)),
                ('position', iki.BigIntegerField(null=True)),
            ],
            table='iki__lots_plasmidverificationmethod__entry__history',
        ),
        iki.AppendOnly(
            model_name='PlasmidVerificationMethod_Entry_History',
        ),
        iki.CreateModel(
            name='PlasmidVerificationMethod_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
            ],
            table='iki__lots_plasmidverificationmethod__history',
        ),
        iki.AppendOnly(
            model_name='PlasmidVerificationMethod_History',
        ),
        iki.CreateModel(
            name='PolymerLot_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('polymer', iki.UUIDField(db_column='polymer_id', null=True)),
                ('mn', iki.QuantityField(null=True, unit='g/mol')),
                ('mw', iki.QuantityField(null=True, unit='g/mol')),
                ('mp', iki.QuantityField(null=True, unit='g/mol')),
                ('dispersity', iki.FloatField(null=True)),
                ('mn_method', iki.UUIDField(db_column='mn_method_id', null=True)),
                ('mw_method', iki.UUIDField(db_column='mw_method_id', null=True)),
                ('calibration_standard', iki.UUIDField(db_column='calibration_standard_id', null=True)),
                ('eluent', iki.TextField(null=True)),
            ],
            table='iki__lots_polymerlot__history',
        ),
        iki.AppendOnly(
            model_name='PolymerLot_History',
        ),
        iki.CreateModel(
            name='ProteinConcentrationMethod',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
            ],
            table='iki__lots_proteinconcentrationmethod',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='ProteinConcentrationMethod_Entry_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('code', iki.TextField(null=True)),
                ('label', iki.TextField(null=True)),
                ('description', iki.TextField(null=True)),
                ('position', iki.BigIntegerField(null=True)),
                ('calculation', iki.TextField(null=True)),
            ],
            table='iki__lots_proteinconcentrationmethod__entry__history',
        ),
        iki.AppendOnly(
            model_name='ProteinConcentrationMethod_Entry_History',
        ),
        iki.CreateModel(
            name='ProteinConcentrationMethod_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
            ],
            table='iki__lots_proteinconcentrationmethod__history',
        ),
        iki.AppendOnly(
            model_name='ProteinConcentrationMethod_History',
        ),
        iki.CreateModel(
            name='ProteinLot_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('protein', iki.UUIDField(db_column='protein_id', null=True)),
                ('cysteines', iki.TextField(null=True)),
            ],
            table='iki__lots_proteinlot__history',
        ),
        iki.AppendOnly(
            model_name='ProteinLot_History',
        ),
        iki.CreateModel(
            name='PurityMethod',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
            ],
            table='iki__lots_puritymethod',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='PurityMethod_Entry_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('code', iki.TextField(null=True)),
                ('label', iki.TextField(null=True)),
                ('description', iki.TextField(null=True)),
                ('position', iki.BigIntegerField(null=True)),
            ],
            table='iki__lots_puritymethod__entry__history',
        ),
        iki.AppendOnly(
            model_name='PurityMethod_Entry_History',
        ),
        iki.CreateModel(
            name='PurityMethod_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
            ],
            table='iki__lots_puritymethod__history',
        ),
        iki.AppendOnly(
            model_name='PurityMethod_History',
        ),
        iki.CreateModel(
            name='SECCalibrationStandard',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
            ],
            table='iki__lots_seccalibrationstandard',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='SECCalibrationStandard_Entry_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('code', iki.TextField(null=True)),
                ('label', iki.TextField(null=True)),
                ('description', iki.TextField(null=True)),
                ('position', iki.BigIntegerField(null=True)),
            ],
            table='iki__lots_seccalibrationstandard__entry__history',
        ),
        iki.AppendOnly(
            model_name='SECCalibrationStandard_Entry_History',
        ),
        iki.CreateModel(
            name='SECCalibrationStandard_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
            ],
            table='iki__lots_seccalibrationstandard__history',
        ),
        iki.AppendOnly(
            model_name='SECCalibrationStandard_History',
        ),
        iki.CreateModel(
            name='StrainIsolate_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('strain', iki.UUIDField(db_column='strain_id', null=True)),
                ('isolate_label', iki.TextField(null=True)),
                ('transformation', iki.UUIDField(db_column='transformation_id', null=True)),
                ('parent_stock', iki.UUIDField(db_column='parent_stock_id', null=True)),
                ('verification_evidence', iki.ArrayField(base_field=iki.UUIDField(), null=True)),
            ],
            table='iki__lots_strainisolate__history',
        ),
        iki.AppendOnly(
            model_name='StrainIsolate_History',
        ),
        iki.CreateModel(
            name='AggregatesMethod_Entry',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('code', iki.CharField(db_index=True, max_length=64)),
                ('label', iki.CharField(db_index=True, max_length=200)),
                ('description', iki.TextField(null=True)),
                ('position', iki.BigIntegerField(null=True)),
            ],
            table='iki__lots_aggregatesmethod__entry',
            indexes=[
                iki.GinIndex(fields=['code'], name='code_trgm_ead18311bc0dadf81433', opclasses=['gin_trgm_ops']),
                iki.GinIndex(fields=['label'], name='label_trgm_0f5180713cd52d92452', opclasses=['gin_trgm_ops']),
                iki.GinIndex(iki.SearchVector(iki.Left('description', 100000), config='simple'), name='descriptio_fts_2e4571363ab3b92'),
            ],
            constraints=[
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('code'), 1), name='iki__lots_aggregatesmethod__entry__code__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('code__regex', '[\\r\\n]'), negated=True), name='iki__lots_aggregatesmethod__entry__code__one_line'),
                iki.CheckConstraint(condition=iki.Q(('code__regex', '^(?:^[A-Za-z0-9][A-Za-z0-9_.+-]*$)$')), name='iki__lots_aggregatesmethod__entry__code__pattern'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('label'), 1), name='iki__lots_aggregatesmethod__entry__label__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('label__regex', '[\\r\\n]'), negated=True), name='iki__lots_aggregatesmethod__entry__label__one_line'),
                iki.CheckConstraint(condition=iki.Q(('label__regex', '^(?:^\\S(?:.*\\S)?$)$')), name='iki__lots_aggregatesmethod__entry__label__pattern'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('description'), 1), name='iki__lots_aggregatesmethod__entry__description__not_empty'),
                iki.CheckConstraint(condition=iki.LessThanOrEqual(iki.Length('description'), 2000), name='iki__lots_aggregatesmethod__entry__description__max_length'),
                iki.CheckConstraint(condition=iki.Q(('position__gte', 1)), name='iki__lots_aggregatesmethod__entry__position__min'),
                iki.CheckConstraint(condition=iki.Q(('position__lte', 10000)), name='iki__lots_aggregatesmethod__entry__position__max'),
            ],
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='BiologicLot',
            fields=[
                ('lot_ptr', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to='iki__lots.lot')),
                ('concentration', iki.QuantityField(db_index=True, null=True, unit='mg/mL')),
                ('a280', iki.FloatField(null=True)),
                ('path_length', iki.QuantityField(db_default=1.0, null=True, unit='cm')),
                ('dilution_factor', iki.FloatField(db_default=1.0, null=True)),
                ('extinction_coefficient', iki.QuantityField(null=True, unit='mL/(mg.cm)')),
                ('a280_extinction', iki.QuantityField(null=True, unit='mL/(mg.cm)')),
                ('a280_basis', iki.TextField(null=True)),
                ('a280_estimate', iki.BooleanField(null=True)),
                ('a280_concentration', iki.QuantityField(null=True, unit='mg/mL')),
                ('purity', iki.QuantityField(db_index=True, null=True, unit='%')),
                ('aggregates', iki.QuantityField(null=True, unit='%')),
                ('endotoxin', iki.QuantityField(null=True, unit='{EU}/mg')),
                ('buffer', iki.CharField(max_length=300, null=True)),
                ('aggregates_method', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to='iki__lots.aggregatesmethod_entry')),
            ],
            table='iki__lots_biologiclot',
            bases=('iki__lots.lot',),
        ),
        iki.CreateModel(
            name='PlasmidPrep',
            fields=[
                ('lot_ptr', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to='iki__lots.lot')),
                ('scale', iki.TextField(db_index=True, null=True)),
                ('kit', iki.CharField(max_length=200, null=True)),
                ('host_strain', iki.CharField(max_length=200, null=True)),
                ('culture_volume', iki.QuantityField(null=True, unit='mL')),
                ('a260', iki.FloatField(null=True)),
                ('path_length', iki.QuantityField(db_default=1.0, null=True, unit='cm')),
                ('dilution_factor', iki.FloatField(db_default=1.0, null=True)),
                ('concentration', iki.QuantityField(db_index=True, null=True, unit='ng/uL')),
                ('a260_concentration', iki.QuantityField(null=True, unit='ng/uL')),
                ('a260_a280', iki.FloatField(null=True)),
                ('a260_a230', iki.FloatField(null=True)),
                ('supercoiled', iki.QuantityField(null=True, unit='%')),
                ('endotoxin', iki.QuantityField(null=True, unit='{EU}/ug')),
            ],
            table='iki__lots_plasmidprep',
            bases=('iki__lots.lot',),
        ),
        iki.CreateModel(
            name='StrainIsolate',
            fields=[
                ('lot_ptr', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to='iki__lots.lot')),
                ('isolate_label', iki.CharField(db_index=True, max_length=100)),
                ('parent_stock', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to='iki__lots.glycerolstock')),
                ('strain', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to=iki.ENTITY)),
                ('transformation', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to=iki.ENTITY)),
            ],
            table='iki__lots_strainisolate',
            bases=('iki__lots.lot',),
        ),
        iki.CreateModel(
            name='DNAConcentrationMethod_Entry',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('code', iki.CharField(db_index=True, max_length=64)),
                ('label', iki.CharField(db_index=True, max_length=200)),
                ('description', iki.TextField(null=True)),
                ('position', iki.BigIntegerField(null=True)),
                ('calculation', iki.TextField(null=True)),
            ],
            table='iki__lots_dnaconcentrationmethod__entry',
            indexes=[
                iki.GinIndex(fields=['code'], name='code_trgm_1149bd65346073a7725b', opclasses=['gin_trgm_ops']),
                iki.GinIndex(fields=['label'], name='label_trgm_ea8d663518adfa2561c', opclasses=['gin_trgm_ops']),
                iki.GinIndex(iki.SearchVector(iki.Left('description', 100000), config='simple'), name='descriptio_fts_6c3f03ce6f9d074'),
            ],
            constraints=[
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('code'), 1), name='iki__lots_dnaconcentrationmethod__entry__code__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('code__regex', '[\\r\\n]'), negated=True), name='iki__lots_dnaconcentrationmethod__entry__code__one_line'),
                iki.CheckConstraint(condition=iki.Q(('code__regex', '^(?:^[A-Za-z0-9][A-Za-z0-9_.+-]*$)$')), name='iki__lots_dnaconcentrationmethod__entry__code__pattern'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('label'), 1), name='iki__lots_dnaconcentrationmethod__entry__label__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('label__regex', '[\\r\\n]'), negated=True), name='iki__lots_dnaconcentrationmethod__entry__label__one_line'),
                iki.CheckConstraint(condition=iki.Q(('label__regex', '^(?:^\\S(?:.*\\S)?$)$')), name='iki__lots_dnaconcentrationmethod__entry__label__pattern'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('description'), 1), name='iki__lots_dnaconcentrationmethod__entry__description__not_empty'),
                iki.CheckConstraint(condition=iki.LessThanOrEqual(iki.Length('description'), 2000), name='iki__lots_dnaconcentrationmethod__entry__descriptio__11cb02ef60'),
                iki.CheckConstraint(condition=iki.Q(('position__gte', 1)), name='iki__lots_dnaconcentrationmethod__entry__position__min'),
                iki.CheckConstraint(condition=iki.Q(('position__lte', 10000)), name='iki__lots_dnaconcentrationmethod__entry__position__max'),
                iki.CheckConstraint(condition=iki.Q(('calculation__in', ['a260'])), name='iki__lots_dnaconcentrationmethod__entry__calculation__values'),
            ],
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='MolarMassMethod_Entry',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('code', iki.CharField(db_index=True, max_length=64)),
                ('label', iki.CharField(db_index=True, max_length=200)),
                ('description', iki.TextField(null=True)),
                ('position', iki.BigIntegerField(null=True)),
                ('averages', iki.ArrayField(base_field=iki.TextField(), null=True)),
            ],
            table='iki__lots_molarmassmethod__entry',
            indexes=[
                iki.GinIndex(fields=['code'], name='code_trgm_bdb92082099f1f746af7', opclasses=['gin_trgm_ops']),
                iki.GinIndex(fields=['label'], name='label_trgm_85e4a10d39f50afd891', opclasses=['gin_trgm_ops']),
                iki.GinIndex(iki.SearchVector(iki.Left('description', 100000), config='simple'), name='descriptio_fts_86dc31236274cbd'),
            ],
            constraints=[
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('code'), 1), name='iki__lots_molarmassmethod__entry__code__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('code__regex', '[\\r\\n]'), negated=True), name='iki__lots_molarmassmethod__entry__code__one_line'),
                iki.CheckConstraint(condition=iki.Q(('code__regex', '^(?:^[A-Za-z0-9][A-Za-z0-9_.+-]*$)$')), name='iki__lots_molarmassmethod__entry__code__pattern'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('label'), 1), name='iki__lots_molarmassmethod__entry__label__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('label__regex', '[\\r\\n]'), negated=True), name='iki__lots_molarmassmethod__entry__label__one_line'),
                iki.CheckConstraint(condition=iki.Q(('label__regex', '^(?:^\\S(?:.*\\S)?$)$')), name='iki__lots_molarmassmethod__entry__label__pattern'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('description'), 1), name='iki__lots_molarmassmethod__entry__description__not_empty'),
                iki.CheckConstraint(condition=iki.LessThanOrEqual(iki.Length('description'), 2000), name='iki__lots_molarmassmethod__entry__description__max_length'),
                iki.CheckConstraint(condition=iki.Q(('position__gte', 1)), name='iki__lots_molarmassmethod__entry__position__min'),
                iki.CheckConstraint(condition=iki.Q(('position__lte', 10000)), name='iki__lots_molarmassmethod__entry__position__max'),
                iki.CheckConstraint(condition=iki.IsPlainList('averages'), name='iki__lots_molarmassmethod__entry__averages__plain_list'),
                iki.CheckConstraint(condition=iki.Q(('averages__len__lte', 2)), name='iki__lots_molarmassmethod__entry__averages__max_items'),
                iki.CheckConstraint(condition=iki.Q(('averages__contained_by', ['mn', 'mw'])), name='iki__lots_molarmassmethod__entry__averages__item_values'),
            ],
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='OligoCounterion_Entry',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('code', iki.CharField(db_index=True, max_length=64)),
                ('label', iki.CharField(db_index=True, max_length=200)),
                ('description', iki.TextField(null=True)),
                ('position', iki.BigIntegerField(null=True)),
            ],
            table='iki__lots_oligocounterion__entry',
            indexes=[
                iki.GinIndex(fields=['code'], name='code_trgm_59ef55c273bcd3bb93e1', opclasses=['gin_trgm_ops']),
                iki.GinIndex(fields=['label'], name='label_trgm_6cce45e1204b8375cd7', opclasses=['gin_trgm_ops']),
                iki.GinIndex(iki.SearchVector(iki.Left('description', 100000), config='simple'), name='descriptio_fts_57b6de2d111c86e'),
            ],
            constraints=[
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('code'), 1), name='iki__lots_oligocounterion__entry__code__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('code__regex', '[\\r\\n]'), negated=True), name='iki__lots_oligocounterion__entry__code__one_line'),
                iki.CheckConstraint(condition=iki.Q(('code__regex', '^(?:^[A-Za-z0-9][A-Za-z0-9_.+-]*$)$')), name='iki__lots_oligocounterion__entry__code__pattern'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('label'), 1), name='iki__lots_oligocounterion__entry__label__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('label__regex', '[\\r\\n]'), negated=True), name='iki__lots_oligocounterion__entry__label__one_line'),
                iki.CheckConstraint(condition=iki.Q(('label__regex', '^(?:^\\S(?:.*\\S)?$)$')), name='iki__lots_oligocounterion__entry__label__pattern'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('description'), 1), name='iki__lots_oligocounterion__entry__description__not_empty'),
                iki.CheckConstraint(condition=iki.LessThanOrEqual(iki.Length('description'), 2000), name='iki__lots_oligocounterion__entry__description__max_length'),
                iki.CheckConstraint(condition=iki.Q(('position__gte', 1)), name='iki__lots_oligocounterion__entry__position__min'),
                iki.CheckConstraint(condition=iki.Q(('position__lte', 10000)), name='iki__lots_oligocounterion__entry__position__max'),
            ],
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='OligoPurification_Entry',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('code', iki.CharField(db_index=True, max_length=64)),
                ('label', iki.CharField(db_index=True, max_length=200)),
                ('description', iki.TextField(null=True)),
                ('position', iki.BigIntegerField(null=True)),
            ],
            table='iki__lots_oligopurification__entry',
            indexes=[
                iki.GinIndex(fields=['code'], name='code_trgm_326df4347cabd5e53c33', opclasses=['gin_trgm_ops']),
                iki.GinIndex(fields=['label'], name='label_trgm_1468c917564b0230eaa', opclasses=['gin_trgm_ops']),
                iki.GinIndex(iki.SearchVector(iki.Left('description', 100000), config='simple'), name='descriptio_fts_c5ba1e541b5396d'),
            ],
            constraints=[
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('code'), 1), name='iki__lots_oligopurification__entry__code__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('code__regex', '[\\r\\n]'), negated=True), name='iki__lots_oligopurification__entry__code__one_line'),
                iki.CheckConstraint(condition=iki.Q(('code__regex', '^(?:^[A-Za-z0-9][A-Za-z0-9_.+-]*$)$')), name='iki__lots_oligopurification__entry__code__pattern'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('label'), 1), name='iki__lots_oligopurification__entry__label__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('label__regex', '[\\r\\n]'), negated=True), name='iki__lots_oligopurification__entry__label__one_line'),
                iki.CheckConstraint(condition=iki.Q(('label__regex', '^(?:^\\S(?:.*\\S)?$)$')), name='iki__lots_oligopurification__entry__label__pattern'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('description'), 1), name='iki__lots_oligopurification__entry__description__not_empty'),
                iki.CheckConstraint(condition=iki.LessThanOrEqual(iki.Length('description'), 2000), name='iki__lots_oligopurification__entry__description__max_length'),
                iki.CheckConstraint(condition=iki.Q(('position__gte', 1)), name='iki__lots_oligopurification__entry__position__min'),
                iki.CheckConstraint(condition=iki.Q(('position__lte', 10000)), name='iki__lots_oligopurification__entry__position__max'),
            ],
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='OligoPurityMethod_Entry',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('code', iki.CharField(db_index=True, max_length=64)),
                ('label', iki.CharField(db_index=True, max_length=200)),
                ('description', iki.TextField(null=True)),
                ('position', iki.BigIntegerField(null=True)),
            ],
            table='iki__lots_oligopuritymethod__entry',
            indexes=[
                iki.GinIndex(fields=['code'], name='code_trgm_de41d88e14e02ca4cf0e', opclasses=['gin_trgm_ops']),
                iki.GinIndex(fields=['label'], name='label_trgm_cde2028d8b1bf5aa559', opclasses=['gin_trgm_ops']),
                iki.GinIndex(iki.SearchVector(iki.Left('description', 100000), config='simple'), name='descriptio_fts_264a4606d33bb77'),
            ],
            constraints=[
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('code'), 1), name='iki__lots_oligopuritymethod__entry__code__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('code__regex', '[\\r\\n]'), negated=True), name='iki__lots_oligopuritymethod__entry__code__one_line'),
                iki.CheckConstraint(condition=iki.Q(('code__regex', '^(?:^[A-Za-z0-9][A-Za-z0-9_.+-]*$)$')), name='iki__lots_oligopuritymethod__entry__code__pattern'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('label'), 1), name='iki__lots_oligopuritymethod__entry__label__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('label__regex', '[\\r\\n]'), negated=True), name='iki__lots_oligopuritymethod__entry__label__one_line'),
                iki.CheckConstraint(condition=iki.Q(('label__regex', '^(?:^\\S(?:.*\\S)?$)$')), name='iki__lots_oligopuritymethod__entry__label__pattern'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('description'), 1), name='iki__lots_oligopuritymethod__entry__description__not_empty'),
                iki.CheckConstraint(condition=iki.LessThanOrEqual(iki.Length('description'), 2000), name='iki__lots_oligopuritymethod__entry__description__max_length'),
                iki.CheckConstraint(condition=iki.Q(('position__gte', 1)), name='iki__lots_oligopuritymethod__entry__position__min'),
                iki.CheckConstraint(condition=iki.Q(('position__lte', 10000)), name='iki__lots_oligopuritymethod__entry__position__max'),
            ],
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='PlasmidPrepMethod_Entry',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('code', iki.CharField(db_index=True, max_length=64)),
                ('label', iki.CharField(db_index=True, max_length=200)),
                ('description', iki.TextField(null=True)),
                ('position', iki.BigIntegerField(null=True)),
            ],
            table='iki__lots_plasmidprepmethod__entry',
            indexes=[
                iki.GinIndex(fields=['code'], name='code_trgm_3777e5c8dabfa1e6f1a8', opclasses=['gin_trgm_ops']),
                iki.GinIndex(fields=['label'], name='label_trgm_f2126b9d5c55fe26e8f', opclasses=['gin_trgm_ops']),
                iki.GinIndex(iki.SearchVector(iki.Left('description', 100000), config='simple'), name='descriptio_fts_3d5b034686315e0'),
            ],
            constraints=[
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('code'), 1), name='iki__lots_plasmidprepmethod__entry__code__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('code__regex', '[\\r\\n]'), negated=True), name='iki__lots_plasmidprepmethod__entry__code__one_line'),
                iki.CheckConstraint(condition=iki.Q(('code__regex', '^(?:^[A-Za-z0-9][A-Za-z0-9_.+-]*$)$')), name='iki__lots_plasmidprepmethod__entry__code__pattern'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('label'), 1), name='iki__lots_plasmidprepmethod__entry__label__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('label__regex', '[\\r\\n]'), negated=True), name='iki__lots_plasmidprepmethod__entry__label__one_line'),
                iki.CheckConstraint(condition=iki.Q(('label__regex', '^(?:^\\S(?:.*\\S)?$)$')), name='iki__lots_plasmidprepmethod__entry__label__pattern'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('description'), 1), name='iki__lots_plasmidprepmethod__entry__description__not_empty'),
                iki.CheckConstraint(condition=iki.LessThanOrEqual(iki.Length('description'), 2000), name='iki__lots_plasmidprepmethod__entry__description__max_length'),
                iki.CheckConstraint(condition=iki.Q(('position__gte', 1)), name='iki__lots_plasmidprepmethod__entry__position__min'),
                iki.CheckConstraint(condition=iki.Q(('position__lte', 10000)), name='iki__lots_plasmidprepmethod__entry__position__max'),
            ],
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='PlasmidVerificationMethod_Entry',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('code', iki.CharField(db_index=True, max_length=64)),
                ('label', iki.CharField(db_index=True, max_length=200)),
                ('description', iki.TextField(null=True)),
                ('position', iki.BigIntegerField(null=True)),
            ],
            table='iki__lots_plasmidverificationmethod__entry',
            indexes=[
                iki.GinIndex(fields=['code'], name='code_trgm_b49b959d92c937e9753c', opclasses=['gin_trgm_ops']),
                iki.GinIndex(fields=['label'], name='label_trgm_2fb5f4fd8b353fde547', opclasses=['gin_trgm_ops']),
                iki.GinIndex(iki.SearchVector(iki.Left('description', 100000), config='simple'), name='descriptio_fts_5eb6f4d9f7fdd6a'),
            ],
            constraints=[
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('code'), 1), name='iki__lots_plasmidverificationmethod__entry__code__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('code__regex', '[\\r\\n]'), negated=True), name='iki__lots_plasmidverificationmethod__entry__code__one_line'),
                iki.CheckConstraint(condition=iki.Q(('code__regex', '^(?:^[A-Za-z0-9][A-Za-z0-9_.+-]*$)$')), name='iki__lots_plasmidverificationmethod__entry__code__pattern'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('label'), 1), name='iki__lots_plasmidverificationmethod__entry__label__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('label__regex', '[\\r\\n]'), negated=True), name='iki__lots_plasmidverificationmethod__entry__label__one_line'),
                iki.CheckConstraint(condition=iki.Q(('label__regex', '^(?:^\\S(?:.*\\S)?$)$')), name='iki__lots_plasmidverificationmethod__entry__label__pattern'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('description'), 1), name='iki__lots_plasmidverificationmethod__entry__descrip__2e26e13de7'),
                iki.CheckConstraint(condition=iki.LessThanOrEqual(iki.Length('description'), 2000), name='iki__lots_plasmidverificationmethod__entry__descrip__1a10a58fa0'),
                iki.CheckConstraint(condition=iki.Q(('position__gte', 1)), name='iki__lots_plasmidverificationmethod__entry__position__min'),
                iki.CheckConstraint(condition=iki.Q(('position__lte', 10000)), name='iki__lots_plasmidverificationmethod__entry__position__max'),
            ],
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='ProteinConcentrationMethod_Entry',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('code', iki.CharField(db_index=True, max_length=64)),
                ('label', iki.CharField(db_index=True, max_length=200)),
                ('description', iki.TextField(null=True)),
                ('position', iki.BigIntegerField(null=True)),
                ('calculation', iki.TextField(null=True)),
            ],
            table='iki__lots_proteinconcentrationmethod__entry',
            indexes=[
                iki.GinIndex(fields=['code'], name='code_trgm_5cf2ed4d9c29733304bc', opclasses=['gin_trgm_ops']),
                iki.GinIndex(fields=['label'], name='label_trgm_568b8e5e2f1d09a5fb7', opclasses=['gin_trgm_ops']),
                iki.GinIndex(iki.SearchVector(iki.Left('description', 100000), config='simple'), name='descriptio_fts_fc640ff6f12e6fe'),
            ],
            constraints=[
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('code'), 1), name='iki__lots_proteinconcentrationmethod__entry__code__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('code__regex', '[\\r\\n]'), negated=True), name='iki__lots_proteinconcentrationmethod__entry__code__one_line'),
                iki.CheckConstraint(condition=iki.Q(('code__regex', '^(?:^[A-Za-z0-9][A-Za-z0-9_.+-]*$)$')), name='iki__lots_proteinconcentrationmethod__entry__code__pattern'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('label'), 1), name='iki__lots_proteinconcentrationmethod__entry__label__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('label__regex', '[\\r\\n]'), negated=True), name='iki__lots_proteinconcentrationmethod__entry__label__one_line'),
                iki.CheckConstraint(condition=iki.Q(('label__regex', '^(?:^\\S(?:.*\\S)?$)$')), name='iki__lots_proteinconcentrationmethod__entry__label__pattern'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('description'), 1), name='iki__lots_proteinconcentrationmethod__entry__descri__1066f8408c'),
                iki.CheckConstraint(condition=iki.LessThanOrEqual(iki.Length('description'), 2000), name='iki__lots_proteinconcentrationmethod__entry__descri__7a755d8cc8'),
                iki.CheckConstraint(condition=iki.Q(('position__gte', 1)), name='iki__lots_proteinconcentrationmethod__entry__position__min'),
                iki.CheckConstraint(condition=iki.Q(('position__lte', 10000)), name='iki__lots_proteinconcentrationmethod__entry__position__max'),
                iki.CheckConstraint(condition=iki.Q(('calculation__in', ['a280'])), name='iki__lots_proteinconcentrationmethod__entry__calcul__7dcf9c7d35'),
            ],
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='PurityMethod_Entry',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('code', iki.CharField(db_index=True, max_length=64)),
                ('label', iki.CharField(db_index=True, max_length=200)),
                ('description', iki.TextField(null=True)),
                ('position', iki.BigIntegerField(null=True)),
            ],
            table='iki__lots_puritymethod__entry',
            indexes=[
                iki.GinIndex(fields=['code'], name='code_trgm_6d9e1c11fa1922031172', opclasses=['gin_trgm_ops']),
                iki.GinIndex(fields=['label'], name='label_trgm_c5c71dd32c8134f32a4', opclasses=['gin_trgm_ops']),
                iki.GinIndex(iki.SearchVector(iki.Left('description', 100000), config='simple'), name='descriptio_fts_a9315b9d521a3a4'),
            ],
            constraints=[
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('code'), 1), name='iki__lots_puritymethod__entry__code__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('code__regex', '[\\r\\n]'), negated=True), name='iki__lots_puritymethod__entry__code__one_line'),
                iki.CheckConstraint(condition=iki.Q(('code__regex', '^(?:^[A-Za-z0-9][A-Za-z0-9_.+-]*$)$')), name='iki__lots_puritymethod__entry__code__pattern'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('label'), 1), name='iki__lots_puritymethod__entry__label__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('label__regex', '[\\r\\n]'), negated=True), name='iki__lots_puritymethod__entry__label__one_line'),
                iki.CheckConstraint(condition=iki.Q(('label__regex', '^(?:^\\S(?:.*\\S)?$)$')), name='iki__lots_puritymethod__entry__label__pattern'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('description'), 1), name='iki__lots_puritymethod__entry__description__not_empty'),
                iki.CheckConstraint(condition=iki.LessThanOrEqual(iki.Length('description'), 2000), name='iki__lots_puritymethod__entry__description__max_length'),
                iki.CheckConstraint(condition=iki.Q(('position__gte', 1)), name='iki__lots_puritymethod__entry__position__min'),
                iki.CheckConstraint(condition=iki.Q(('position__lte', 10000)), name='iki__lots_puritymethod__entry__position__max'),
            ],
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='SECCalibrationStandard_Entry',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('code', iki.CharField(db_index=True, max_length=64)),
                ('label', iki.CharField(db_index=True, max_length=200)),
                ('description', iki.TextField(null=True)),
                ('position', iki.BigIntegerField(null=True)),
            ],
            table='iki__lots_seccalibrationstandard__entry',
            indexes=[
                iki.GinIndex(fields=['code'], name='code_trgm_3c7af78cc6c1f01bb9b5', opclasses=['gin_trgm_ops']),
                iki.GinIndex(fields=['label'], name='label_trgm_d610400a97a4583caee', opclasses=['gin_trgm_ops']),
                iki.GinIndex(iki.SearchVector(iki.Left('description', 100000), config='simple'), name='descriptio_fts_1a81a585ba9119b'),
            ],
            constraints=[
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('code'), 1), name='iki__lots_seccalibrationstandard__entry__code__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('code__regex', '[\\r\\n]'), negated=True), name='iki__lots_seccalibrationstandard__entry__code__one_line'),
                iki.CheckConstraint(condition=iki.Q(('code__regex', '^(?:^[A-Za-z0-9][A-Za-z0-9_.+-]*$)$')), name='iki__lots_seccalibrationstandard__entry__code__pattern'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('label'), 1), name='iki__lots_seccalibrationstandard__entry__label__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('label__regex', '[\\r\\n]'), negated=True), name='iki__lots_seccalibrationstandard__entry__label__one_line'),
                iki.CheckConstraint(condition=iki.Q(('label__regex', '^(?:^\\S(?:.*\\S)?$)$')), name='iki__lots_seccalibrationstandard__entry__label__pattern'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('description'), 1), name='iki__lots_seccalibrationstandard__entry__description__not_empty'),
                iki.CheckConstraint(condition=iki.LessThanOrEqual(iki.Length('description'), 2000), name='iki__lots_seccalibrationstandard__entry__descriptio__570148b818'),
                iki.CheckConstraint(condition=iki.Q(('position__gte', 1)), name='iki__lots_seccalibrationstandard__entry__position__min'),
                iki.CheckConstraint(condition=iki.Q(('position__lte', 10000)), name='iki__lots_seccalibrationstandard__entry__position__max'),
            ],
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='ConjugateLot',
            fields=[
                ('biologiclot_ptr', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to='iki__lots.biologiclot')),
                ('dar_method', iki.TextField(db_index=True, null=True)),
                ('distribution_method', iki.TextField(null=True)),
                ('dar', iki.FloatField(db_index=True, null=True)),
                ('distribution_dar', iki.FloatField(null=True)),
                ('unconjugated', iki.QuantityField(null=True, unit='%')),
                ('free_drug', iki.QuantityField(null=True, unit='%')),
            ],
            table='iki__lots_conjugatelot',
            bases=('iki__lots.biologiclot',),
        ),
        iki.CreateModel(
            name='ProteinLot',
            fields=[
                ('biologiclot_ptr', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to='iki__lots.biologiclot')),
                ('cysteines', iki.TextField(null=True)),
            ],
            table='iki__lots_proteinlot',
            bases=('iki__lots.biologiclot',),
        ),
        iki.AddField(
            model_name='biologiclot',
            name='concentration_method',
            field=iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to='iki__lots.proteinconcentrationmethod_entry'),
        ),
        iki.AddField(
            model_name='biologiclot',
            name='purity_method',
            field=iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to='iki__lots.puritymethod_entry'),
        ),
        iki.CreateModel(
            name='GenericCountLot',
            fields=[
                ('lot_ptr', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to='iki__lots.lot')),
                ('item', iki.CharField(max_length=100, null=True)),
                ('purity', iki.QuantityField(db_index=True, null=True, unit='%')),
                ('purity_method', iki.CharField(max_length=200, null=True)),
                ('of', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to=iki.ENTITY)),
            ],
            table='iki__lots_genericcountlot',
            bases=('iki__lots.lot',),
        ),
        iki.CreateModel(
            name='GenericLot',
            fields=[
                ('lot_ptr', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to='iki__lots.lot')),
                ('concentration', iki.QuantityField(null=True, unit='mg/mL')),
                ('purity', iki.QuantityField(db_index=True, null=True, unit='%')),
                ('purity_method', iki.CharField(max_length=200, null=True)),
                ('of', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to=iki.ENTITY)),
            ],
            table='iki__lots_genericlot',
            bases=('iki__lots.lot',),
        ),
        iki.CreateModel(
            name='GenericVolumeLot',
            fields=[
                ('lot_ptr', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to='iki__lots.lot')),
                ('concentration', iki.QuantityField(null=True, unit='mg/mL')),
                ('purity', iki.QuantityField(db_index=True, null=True, unit='%')),
                ('purity_method', iki.CharField(max_length=200, null=True)),
                ('of', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to=iki.ENTITY)),
            ],
            table='iki__lots_genericvolumelot',
            bases=('iki__lots.lot',),
        ),
        iki.CreateModel(
            name='Lot_certificates',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity', 'position')),
                ('entity', iki.ForeignKey(db_column='entity_id', on_delete=iki.CASCADE, related_name='+', to='iki__lots.lot')),
                ('position', iki.PositiveIntegerField()),
                ('item', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to=iki.FILE)),
            ],
            table='iki__lots_lot__certificates',
        ),
        iki.CreateModel(
            name='OligoLot',
            fields=[
                ('lot_ptr', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to='iki__lots.lot')),
                ('synthesis_scale', iki.QuantityField(db_index=True, null=True, unit='umol')),
                ('yield_od', iki.FloatField(null=True)),
                ('extinction_260', iki.QuantityField(null=True, unit='L/(mol.cm)')),
                ('yield_amount', iki.QuantityField(null=True, unit='nmol')),
                ('concentration', iki.QuantityField(null=True, unit='umol/L')),
                ('purity', iki.QuantityField(db_index=True, null=True, unit='%')),
                ('measured_mass', iki.QuantityField(null=True, unit='u')),
                ('mass_basis', iki.TextField(db_default='monoisotopic')),
                ('mass_tolerance', iki.QuantityField(null=True, unit='u')),
                ('mass_specification', iki.CharField(max_length=200, null=True)),
                ('expected_mass', iki.QuantityField(null=True, unit='u')),
                ('mass_error', iki.QuantityField(null=True, unit='u')),
                ('counterion', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to='iki__lots.oligocounterion_entry')),
                ('oligonucleotide', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__oligo.oligonucleotide_entry')),
                ('purification', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to='iki__lots.oligopurification_entry')),
                ('purity_method', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to='iki__lots.oligopuritymethod_entry')),
            ],
            table='iki__lots_oligolot',
            bases=('iki__lots.lot',),
        ),
        iki.CreateModel(
            name='PlasmidVerification',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('plasmid_record_id', iki.CharField(max_length=36, null=True)),
                ('plasmid_revision', iki.BigIntegerField(null=True)),
                ('alignment_revision', iki.BigIntegerField(null=True)),
                ('outcome', iki.TextField(db_index=True, null=True)),
                ('override_reason', iki.CharField(max_length=1000, null=True)),
                ('notes', iki.TextField(null=True)),
                ('review_flow', iki.TextField(db_index=True, null=True)),
                ('review', iki.StateField(db_comment='A state field (ADR 0125): one of recorded, signed_off, submitted, reviewed, released.', db_default='recorded', locking=['signed_off', 'submitted', 'reviewed', 'released'], states=['recorded', 'signed_off', 'submitted', 'reviewed', 'released'])),
                ('alignment', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to='iki__core.alignment')),
                ('plasmid', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__plasmid.plasmid_entry')),
                ('report', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to=iki.FILE)),
                ('reviewer', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to=iki.USER)),
                ('prep', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__lots.plasmidprep')),
            ],
            table='iki__lots_plasmidverification',
            bases=(iki.ENTITY,),
        ),
        iki.AddField(
            model_name='plasmidprep',
            name='concentration_method',
            field=iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to='iki__lots.dnaconcentrationmethod_entry'),
        ),
        iki.AddField(
            model_name='plasmidprep',
            name='method',
            field=iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to='iki__lots.plasmidprepmethod_entry'),
        ),
        iki.AddField(
            model_name='plasmidprep',
            name='plasmid',
            field=iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__plasmid.plasmid_entry'),
        ),
        iki.AddField(
            model_name='plasmidprep',
            name='verified_by',
            field=iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to='iki__lots.plasmidverificationmethod_entry'),
        ),
        iki.CreateModel(
            name='PolymerLot',
            fields=[
                ('lot_ptr', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to='iki__lots.lot')),
                ('mn', iki.QuantityField(db_index=True, null=True, unit='g/mol')),
                ('mw', iki.QuantityField(db_index=True, null=True, unit='g/mol')),
                ('mp', iki.QuantityField(null=True, unit='g/mol')),
                ('dispersity', iki.FloatField(db_index=True, null=True)),
                ('eluent', iki.CharField(max_length=200, null=True)),
                ('calibration_standard', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to='iki__lots.seccalibrationstandard_entry')),
                ('mn_method', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to='iki__lots.molarmassmethod_entry')),
                ('mw_method', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to='iki__lots.molarmassmethod_entry')),
                ('polymer', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__polymer.polymer_entry')),
            ],
            table='iki__lots_polymerlot',
            bases=('iki__lots.lot',),
        ),
        iki.CreateModel(
            name='StrainIsolate_verification_evidence',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity', 'position')),
                ('entity', iki.ForeignKey(db_column='entity_id', on_delete=iki.CASCADE, related_name='+', to='iki__lots.strainisolate')),
                ('position', iki.PositiveIntegerField()),
            ],
            table='iki__lots_strainisolate__verification_evidence',
        ),
        iki.AddField(
            model_name='glycerolstock',
            name='isolate',
            field=iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__lots.strainisolate'),
        ),
        iki.AddIndex(
            model_name='lot',
            index=iki.GinIndex(fields=['lot_number'], name='lot_number_trgm_0dc62e8b4465c5', opclasses=['gin_trgm_ops']),
        ),
        iki.AddIndex(
            model_name='lot',
            index=iki.GinIndex(fields=['supplier'], name='supplier_trgm_e57f5511f6781637', opclasses=['gin_trgm_ops']),
        ),
        iki.AddIndex(
            model_name='lot',
            index=iki.GinIndex(fields=['catalogue_number'], name='catalogue_trgm_48c8fa5fed51246', opclasses=['gin_trgm_ops']),
        ),
        iki.AddIndex(
            model_name='lot',
            index=iki.GinIndex(iki.SearchVector(iki.Left('notes', 100000), config='simple'), name='notes_fts_513ab10053d543cb60af'),
        ),
        iki.AddConstraint(
            model_name='lot',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('lot_number'), 1), name='iki__lots_lot__lot_number__not_empty'),
        ),
        iki.AddConstraint(
            model_name='lot',
            constraint=iki.CheckConstraint(condition=iki.Q(('lot_number__regex', '[\\r\\n]'), negated=True), name='iki__lots_lot__lot_number__one_line'),
        ),
        iki.AddConstraint(
            model_name='lot',
            constraint=iki.CheckConstraint(condition=iki.Q(('source__in', ['made', 'bought'])), name='iki__lots_lot__source__values'),
        ),
        iki.AddConstraint(
            model_name='lot',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('supplier'), 1), name='iki__lots_lot__supplier__not_empty'),
        ),
        iki.AddConstraint(
            model_name='lot',
            constraint=iki.CheckConstraint(condition=iki.Q(('supplier__regex', '[\\r\\n]'), negated=True), name='iki__lots_lot__supplier__one_line'),
        ),
        iki.AddConstraint(
            model_name='lot',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('catalogue_number'), 1), name='iki__lots_lot__catalogue_number__not_empty'),
        ),
        iki.AddConstraint(
            model_name='lot',
            constraint=iki.CheckConstraint(condition=iki.Q(('catalogue_number__regex', '[\\r\\n]'), negated=True), name='iki__lots_lot__catalogue_number__one_line'),
        ),
        iki.AddConstraint(
            model_name='lot',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('storage'), 1), name='iki__lots_lot__storage__not_empty'),
        ),
        iki.AddConstraint(
            model_name='lot',
            constraint=iki.CheckConstraint(condition=iki.Q(('storage__regex', '[\\r\\n]'), negated=True), name='iki__lots_lot__storage__one_line'),
        ),
        iki.AddConstraint(
            model_name='lot',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('notes'), 1), name='iki__lots_lot__notes__not_empty'),
        ),
        iki.AddConstraint(
            model_name='lot',
            constraint=iki.CheckConstraint(condition=iki.LessThanOrEqual(iki.Length('notes'), 10000), name='iki__lots_lot__notes__max_length'),
        ),
        iki.AddConstraint(
            model_name='lot',
            constraint=iki.CheckConstraint(condition=iki.Q(('release_requirement__in', ['as_recorded', 'required', 'not_required'])), name='iki__lots_lot__release_requirement__known'),
        ),
        iki.AddConstraint(
            model_name='lot',
            constraint=iki.CheckConstraint(condition=iki.Q(('disposition__in', ['quarantined', 'released', 'available', 'retest_due', 'on_hold', 'rejected', 'expired'])), name='iki__lots_lot__disposition__known'),
        ),
        iki.CreateModel(
            name='ConjugateLot_drug_loads',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity', 'position')),
                ('entity', iki.ForeignKey(db_column='entity_id', on_delete=iki.CASCADE, related_name='+', to='iki__lots.conjugatelot')),
                ('position', iki.PositiveIntegerField()),
                ('chain', iki.TextField(db_default='intact')),
                ('drug_load', iki.BigIntegerField()),
                ('area', iki.FloatField()),
            ],
            table='iki__lots_conjugatelot__drug_loads',
        ),
        iki.AddField(
            model_name='conjugatelot',
            name='conjugate',
            field=iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__conjugate.conjugate_entry'),
        ),
        iki.AddField(
            model_name='proteinlot',
            name='protein',
            field=iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to=iki.ENTITY),
        ),
        iki.AddConstraint(
            model_name='biologiclot',
            constraint=iki.CheckConstraint(condition=iki.Q(('concentration__gt', float('-inf')), ('concentration__lt', float('inf'))), name='iki__lots_biologiclot__concentration__finite'),
        ),
        iki.AddConstraint(
            model_name='biologiclot',
            constraint=iki.CheckConstraint(condition=iki.Q(('concentration__gte', 0)), name='iki__lots_biologiclot__concentration__min'),
        ),
        iki.AddConstraint(
            model_name='biologiclot',
            constraint=iki.CheckConstraint(condition=iki.Q(('a280__gt', float('-inf')), ('a280__lt', float('inf'))), name='iki__lots_biologiclot__a280__finite'),
        ),
        iki.AddConstraint(
            model_name='biologiclot',
            constraint=iki.CheckConstraint(condition=iki.Q(('a280__gte', 0)), name='iki__lots_biologiclot__a280__min'),
        ),
        iki.AddConstraint(
            model_name='biologiclot',
            constraint=iki.CheckConstraint(condition=iki.Q(('path_length__gt', float('-inf')), ('path_length__lt', float('inf'))), name='iki__lots_biologiclot__path_length__finite'),
        ),
        iki.AddConstraint(
            model_name='biologiclot',
            constraint=iki.CheckConstraint(condition=iki.Q(('path_length__gte', 0)), name='iki__lots_biologiclot__path_length__min'),
        ),
        iki.AddConstraint(
            model_name='biologiclot',
            constraint=iki.CheckConstraint(condition=iki.Q(('dilution_factor__gt', float('-inf')), ('dilution_factor__lt', float('inf'))), name='iki__lots_biologiclot__dilution_factor__finite'),
        ),
        iki.AddConstraint(
            model_name='biologiclot',
            constraint=iki.CheckConstraint(condition=iki.Q(('dilution_factor__gte', 1)), name='iki__lots_biologiclot__dilution_factor__min'),
        ),
        iki.AddConstraint(
            model_name='biologiclot',
            constraint=iki.CheckConstraint(condition=iki.Q(('extinction_coefficient__gt', float('-inf')), ('extinction_coefficient__lt', float('inf'))), name='iki__lots_biologiclot__extinction_coefficient__finite'),
        ),
        iki.AddConstraint(
            model_name='biologiclot',
            constraint=iki.CheckConstraint(condition=iki.Q(('extinction_coefficient__gte', 0)), name='iki__lots_biologiclot__extinction_coefficient__min'),
        ),
        iki.AddConstraint(
            model_name='biologiclot',
            constraint=iki.CheckConstraint(condition=iki.Q(('a280_extinction__gt', float('-inf')), ('a280_extinction__lt', float('inf'))), name='iki__lots_biologiclot__a280_extinction__finite'),
        ),
        iki.AddConstraint(
            model_name='biologiclot',
            constraint=iki.CheckConstraint(condition=iki.Q(('a280_extinction__gte', 0)), name='iki__lots_biologiclot__a280_extinction__min'),
        ),
        iki.AddConstraint(
            model_name='biologiclot',
            constraint=iki.CheckConstraint(condition=iki.Q(('a280_basis__in', ['measured', 'cystines', 'reduced'])), name='iki__lots_biologiclot__a280_basis__values'),
        ),
        iki.AddConstraint(
            model_name='biologiclot',
            constraint=iki.CheckConstraint(condition=iki.Q(('a280_concentration__gt', float('-inf')), ('a280_concentration__lt', float('inf'))), name='iki__lots_biologiclot__a280_concentration__finite'),
        ),
        iki.AddConstraint(
            model_name='biologiclot',
            constraint=iki.CheckConstraint(condition=iki.Q(('a280_concentration__gte', 0)), name='iki__lots_biologiclot__a280_concentration__min'),
        ),
        iki.AddConstraint(
            model_name='biologiclot',
            constraint=iki.CheckConstraint(condition=iki.Q(('purity__gt', float('-inf')), ('purity__lt', float('inf'))), name='iki__lots_biologiclot__purity__finite'),
        ),
        iki.AddConstraint(
            model_name='biologiclot',
            constraint=iki.CheckConstraint(condition=iki.Q(('purity__gte', 0)), name='iki__lots_biologiclot__purity__min'),
        ),
        iki.AddConstraint(
            model_name='biologiclot',
            constraint=iki.CheckConstraint(condition=iki.Q(('purity__lte', 100)), name='iki__lots_biologiclot__purity__max'),
        ),
        iki.AddConstraint(
            model_name='biologiclot',
            constraint=iki.CheckConstraint(condition=iki.Q(('aggregates__gt', float('-inf')), ('aggregates__lt', float('inf'))), name='iki__lots_biologiclot__aggregates__finite'),
        ),
        iki.AddConstraint(
            model_name='biologiclot',
            constraint=iki.CheckConstraint(condition=iki.Q(('aggregates__gte', 0)), name='iki__lots_biologiclot__aggregates__min'),
        ),
        iki.AddConstraint(
            model_name='biologiclot',
            constraint=iki.CheckConstraint(condition=iki.Q(('aggregates__lte', 100)), name='iki__lots_biologiclot__aggregates__max'),
        ),
        iki.AddConstraint(
            model_name='biologiclot',
            constraint=iki.CheckConstraint(condition=iki.Q(('endotoxin__gt', float('-inf')), ('endotoxin__lt', float('inf'))), name='iki__lots_biologiclot__endotoxin__finite'),
        ),
        iki.AddConstraint(
            model_name='biologiclot',
            constraint=iki.CheckConstraint(condition=iki.Q(('endotoxin__gte', 0)), name='iki__lots_biologiclot__endotoxin__min'),
        ),
        iki.AddConstraint(
            model_name='biologiclot',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('buffer'), 1), name='iki__lots_biologiclot__buffer__not_empty'),
        ),
        iki.AddConstraint(
            model_name='biologiclot',
            constraint=iki.CheckConstraint(condition=iki.Q(('buffer__regex', '[\\r\\n]'), negated=True), name='iki__lots_biologiclot__buffer__one_line'),
        ),
        iki.AddConstraint(
            model_name='genericcountlot',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('item'), 1), name='iki__lots_genericcountlot__item__not_empty'),
        ),
        iki.AddConstraint(
            model_name='genericcountlot',
            constraint=iki.CheckConstraint(condition=iki.Q(('item__regex', '[\\r\\n]'), negated=True), name='iki__lots_genericcountlot__item__one_line'),
        ),
        iki.AddConstraint(
            model_name='genericcountlot',
            constraint=iki.CheckConstraint(condition=iki.Q(('purity__gt', float('-inf')), ('purity__lt', float('inf'))), name='iki__lots_genericcountlot__purity__finite'),
        ),
        iki.AddConstraint(
            model_name='genericcountlot',
            constraint=iki.CheckConstraint(condition=iki.Q(('purity__gte', 0)), name='iki__lots_genericcountlot__purity__min'),
        ),
        iki.AddConstraint(
            model_name='genericcountlot',
            constraint=iki.CheckConstraint(condition=iki.Q(('purity__lte', 100)), name='iki__lots_genericcountlot__purity__max'),
        ),
        iki.AddConstraint(
            model_name='genericcountlot',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('purity_method'), 1), name='iki__lots_genericcountlot__purity_method__not_empty'),
        ),
        iki.AddConstraint(
            model_name='genericcountlot',
            constraint=iki.CheckConstraint(condition=iki.Q(('purity_method__regex', '[\\r\\n]'), negated=True), name='iki__lots_genericcountlot__purity_method__one_line'),
        ),
        iki.AddConstraint(
            model_name='genericlot',
            constraint=iki.CheckConstraint(condition=iki.Q(('concentration__gt', float('-inf')), ('concentration__lt', float('inf'))), name='iki__lots_genericlot__concentration__finite'),
        ),
        iki.AddConstraint(
            model_name='genericlot',
            constraint=iki.CheckConstraint(condition=iki.Q(('concentration__gte', 0)), name='iki__lots_genericlot__concentration__min'),
        ),
        iki.AddConstraint(
            model_name='genericlot',
            constraint=iki.CheckConstraint(condition=iki.Q(('purity__gt', float('-inf')), ('purity__lt', float('inf'))), name='iki__lots_genericlot__purity__finite'),
        ),
        iki.AddConstraint(
            model_name='genericlot',
            constraint=iki.CheckConstraint(condition=iki.Q(('purity__gte', 0)), name='iki__lots_genericlot__purity__min'),
        ),
        iki.AddConstraint(
            model_name='genericlot',
            constraint=iki.CheckConstraint(condition=iki.Q(('purity__lte', 100)), name='iki__lots_genericlot__purity__max'),
        ),
        iki.AddConstraint(
            model_name='genericlot',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('purity_method'), 1), name='iki__lots_genericlot__purity_method__not_empty'),
        ),
        iki.AddConstraint(
            model_name='genericlot',
            constraint=iki.CheckConstraint(condition=iki.Q(('purity_method__regex', '[\\r\\n]'), negated=True), name='iki__lots_genericlot__purity_method__one_line'),
        ),
        iki.AddConstraint(
            model_name='genericvolumelot',
            constraint=iki.CheckConstraint(condition=iki.Q(('concentration__gt', float('-inf')), ('concentration__lt', float('inf'))), name='iki__lots_genericvolumelot__concentration__finite'),
        ),
        iki.AddConstraint(
            model_name='genericvolumelot',
            constraint=iki.CheckConstraint(condition=iki.Q(('concentration__gte', 0)), name='iki__lots_genericvolumelot__concentration__min'),
        ),
        iki.AddConstraint(
            model_name='genericvolumelot',
            constraint=iki.CheckConstraint(condition=iki.Q(('purity__gt', float('-inf')), ('purity__lt', float('inf'))), name='iki__lots_genericvolumelot__purity__finite'),
        ),
        iki.AddConstraint(
            model_name='genericvolumelot',
            constraint=iki.CheckConstraint(condition=iki.Q(('purity__gte', 0)), name='iki__lots_genericvolumelot__purity__min'),
        ),
        iki.AddConstraint(
            model_name='genericvolumelot',
            constraint=iki.CheckConstraint(condition=iki.Q(('purity__lte', 100)), name='iki__lots_genericvolumelot__purity__max'),
        ),
        iki.AddConstraint(
            model_name='genericvolumelot',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('purity_method'), 1), name='iki__lots_genericvolumelot__purity_method__not_empty'),
        ),
        iki.AddConstraint(
            model_name='genericvolumelot',
            constraint=iki.CheckConstraint(condition=iki.Q(('purity_method__regex', '[\\r\\n]'), negated=True), name='iki__lots_genericvolumelot__purity_method__one_line'),
        ),
        iki.AddConstraint(
            model_name='lot_certificates',
            constraint=iki.CheckConstraint(condition=iki.Q(('position__lt', 20)), name='iki__lots_lot__certificates__position__max_items'),
        ),
        iki.AddConstraint(
            model_name='oligolot',
            constraint=iki.CheckConstraint(condition=iki.Q(('synthesis_scale__gt', float('-inf')), ('synthesis_scale__lt', float('inf'))), name='iki__lots_oligolot__synthesis_scale__finite'),
        ),
        iki.AddConstraint(
            model_name='oligolot',
            constraint=iki.CheckConstraint(condition=iki.Q(('synthesis_scale__gte', 0)), name='iki__lots_oligolot__synthesis_scale__min'),
        ),
        iki.AddConstraint(
            model_name='oligolot',
            constraint=iki.CheckConstraint(condition=iki.Q(('yield_od__gt', float('-inf')), ('yield_od__lt', float('inf'))), name='iki__lots_oligolot__yield_od__finite'),
        ),
        iki.AddConstraint(
            model_name='oligolot',
            constraint=iki.CheckConstraint(condition=iki.Q(('yield_od__gte', 0)), name='iki__lots_oligolot__yield_od__min'),
        ),
        iki.AddConstraint(
            model_name='oligolot',
            constraint=iki.CheckConstraint(condition=iki.Q(('extinction_260__gt', float('-inf')), ('extinction_260__lt', float('inf'))), name='iki__lots_oligolot__extinction_260__finite'),
        ),
        iki.AddConstraint(
            model_name='oligolot',
            constraint=iki.CheckConstraint(condition=iki.Q(('extinction_260__gte', 0)), name='iki__lots_oligolot__extinction_260__min'),
        ),
        iki.AddConstraint(
            model_name='oligolot',
            constraint=iki.CheckConstraint(condition=iki.Q(('yield_amount__gt', float('-inf')), ('yield_amount__lt', float('inf'))), name='iki__lots_oligolot__yield_amount__finite'),
        ),
        iki.AddConstraint(
            model_name='oligolot',
            constraint=iki.CheckConstraint(condition=iki.Q(('yield_amount__gte', 0)), name='iki__lots_oligolot__yield_amount__min'),
        ),
        iki.AddConstraint(
            model_name='oligolot',
            constraint=iki.CheckConstraint(condition=iki.Q(('concentration__gt', float('-inf')), ('concentration__lt', float('inf'))), name='iki__lots_oligolot__concentration__finite'),
        ),
        iki.AddConstraint(
            model_name='oligolot',
            constraint=iki.CheckConstraint(condition=iki.Q(('concentration__gte', 0)), name='iki__lots_oligolot__concentration__min'),
        ),
        iki.AddConstraint(
            model_name='oligolot',
            constraint=iki.CheckConstraint(condition=iki.Q(('purity__gt', float('-inf')), ('purity__lt', float('inf'))), name='iki__lots_oligolot__purity__finite'),
        ),
        iki.AddConstraint(
            model_name='oligolot',
            constraint=iki.CheckConstraint(condition=iki.Q(('purity__gte', 0)), name='iki__lots_oligolot__purity__min'),
        ),
        iki.AddConstraint(
            model_name='oligolot',
            constraint=iki.CheckConstraint(condition=iki.Q(('purity__lte', 100)), name='iki__lots_oligolot__purity__max'),
        ),
        iki.AddConstraint(
            model_name='oligolot',
            constraint=iki.CheckConstraint(condition=iki.Q(('measured_mass__gt', float('-inf')), ('measured_mass__lt', float('inf'))), name='iki__lots_oligolot__measured_mass__finite'),
        ),
        iki.AddConstraint(
            model_name='oligolot',
            constraint=iki.CheckConstraint(condition=iki.Q(('measured_mass__gte', 0)), name='iki__lots_oligolot__measured_mass__min'),
        ),
        iki.AddConstraint(
            model_name='oligolot',
            constraint=iki.CheckConstraint(condition=iki.Q(('mass_basis__in', ['monoisotopic', 'average'])), name='iki__lots_oligolot__mass_basis__values'),
        ),
        iki.AddConstraint(
            model_name='oligolot',
            constraint=iki.CheckConstraint(condition=iki.Q(('mass_tolerance__gt', float('-inf')), ('mass_tolerance__lt', float('inf'))), name='iki__lots_oligolot__mass_tolerance__finite'),
        ),
        iki.AddConstraint(
            model_name='oligolot',
            constraint=iki.CheckConstraint(condition=iki.Q(('mass_tolerance__gte', 0)), name='iki__lots_oligolot__mass_tolerance__min'),
        ),
        iki.AddConstraint(
            model_name='oligolot',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('mass_specification'), 1), name='iki__lots_oligolot__mass_specification__not_empty'),
        ),
        iki.AddConstraint(
            model_name='oligolot',
            constraint=iki.CheckConstraint(condition=iki.Q(('mass_specification__regex', '[\\r\\n]'), negated=True), name='iki__lots_oligolot__mass_specification__one_line'),
        ),
        iki.AddConstraint(
            model_name='oligolot',
            constraint=iki.CheckConstraint(condition=iki.Q(('expected_mass__gt', float('-inf')), ('expected_mass__lt', float('inf'))), name='iki__lots_oligolot__expected_mass__finite'),
        ),
        iki.AddConstraint(
            model_name='oligolot',
            constraint=iki.CheckConstraint(condition=iki.Q(('expected_mass__gte', 0)), name='iki__lots_oligolot__expected_mass__min'),
        ),
        iki.AddConstraint(
            model_name='oligolot',
            constraint=iki.CheckConstraint(condition=iki.Q(('mass_error__gt', float('-inf')), ('mass_error__lt', float('inf'))), name='iki__lots_oligolot__mass_error__finite'),
        ),
        iki.AddIndex(
            model_name='plasmidverification',
            index=iki.GinIndex(iki.SearchVector(iki.Left('notes', 100000), config='simple'), name='notes_fts_5e6eda76380101f93881'),
        ),
        iki.AddConstraint(
            model_name='plasmidverification',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('plasmid_record_id'), 1), name='iki__lots_plasmidverification__plasmid_record_id__not_empty'),
        ),
        iki.AddConstraint(
            model_name='plasmidverification',
            constraint=iki.CheckConstraint(condition=iki.Q(('plasmid_record_id__regex', '[\\r\\n]'), negated=True), name='iki__lots_plasmidverification__plasmid_record_id__one_line'),
        ),
        iki.AddConstraint(
            model_name='plasmidverification',
            constraint=iki.CheckConstraint(condition=iki.Q(('plasmid_record_id__regex', '^(?:^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$)$')), name='iki__lots_plasmidverification__plasmid_record_id__pattern'),
        ),
        iki.AddConstraint(
            model_name='plasmidverification',
            constraint=iki.CheckConstraint(condition=iki.Q(('plasmid_revision__gte', 1)), name='iki__lots_plasmidverification__plasmid_revision__min'),
        ),
        iki.AddConstraint(
            model_name='plasmidverification',
            constraint=iki.CheckConstraint(condition=iki.Q(('alignment_revision__gte', 1)), name='iki__lots_plasmidverification__alignment_revision__min'),
        ),
        iki.AddConstraint(
            model_name='plasmidverification',
            constraint=iki.CheckConstraint(condition=iki.Q(('outcome__in', ['match', 'discrepancy', 'inconclusive'])), name='iki__lots_plasmidverification__outcome__values'),
        ),
        iki.AddConstraint(
            model_name='plasmidverification',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('override_reason'), 1), name='iki__lots_plasmidverification__override_reason__not_empty'),
        ),
        iki.AddConstraint(
            model_name='plasmidverification',
            constraint=iki.CheckConstraint(condition=iki.Q(('override_reason__regex', '[\\r\\n]'), negated=True), name='iki__lots_plasmidverification__override_reason__one_line'),
        ),
        iki.AddConstraint(
            model_name='plasmidverification',
            constraint=iki.CheckConstraint(condition=iki.Q(('override_reason__regex', '^(?:\\s*\\S.*)$')), name='iki__lots_plasmidverification__override_reason__pattern'),
        ),
        iki.AddConstraint(
            model_name='plasmidverification',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('notes'), 1), name='iki__lots_plasmidverification__notes__not_empty'),
        ),
        iki.AddConstraint(
            model_name='plasmidverification',
            constraint=iki.CheckConstraint(condition=iki.LessThanOrEqual(iki.Length('notes'), 10000), name='iki__lots_plasmidverification__notes__max_length'),
        ),
        iki.AddConstraint(
            model_name='plasmidverification',
            constraint=iki.CheckConstraint(condition=iki.Q(('review_flow__in', ['research', 'regulated_qc'])), name='iki__lots_plasmidverification__review_flow__values'),
        ),
        iki.AddConstraint(
            model_name='plasmidverification',
            constraint=iki.CheckConstraint(condition=iki.Q(('review__in', ['recorded', 'signed_off', 'submitted', 'reviewed', 'released'])), name='iki__lots_plasmidverification__review__known'),
        ),
        iki.AddConstraint(
            model_name='plasmidprep',
            constraint=iki.CheckConstraint(condition=iki.Q(('scale__in', ['miniprep', 'midiprep', 'maxiprep', 'megaprep', 'gigaprep'])), name='iki__lots_plasmidprep__scale__values'),
        ),
        iki.AddConstraint(
            model_name='plasmidprep',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('kit'), 1), name='iki__lots_plasmidprep__kit__not_empty'),
        ),
        iki.AddConstraint(
            model_name='plasmidprep',
            constraint=iki.CheckConstraint(condition=iki.Q(('kit__regex', '[\\r\\n]'), negated=True), name='iki__lots_plasmidprep__kit__one_line'),
        ),
        iki.AddConstraint(
            model_name='plasmidprep',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('host_strain'), 1), name='iki__lots_plasmidprep__host_strain__not_empty'),
        ),
        iki.AddConstraint(
            model_name='plasmidprep',
            constraint=iki.CheckConstraint(condition=iki.Q(('host_strain__regex', '[\\r\\n]'), negated=True), name='iki__lots_plasmidprep__host_strain__one_line'),
        ),
        iki.AddConstraint(
            model_name='plasmidprep',
            constraint=iki.CheckConstraint(condition=iki.Q(('culture_volume__gt', float('-inf')), ('culture_volume__lt', float('inf'))), name='iki__lots_plasmidprep__culture_volume__finite'),
        ),
        iki.AddConstraint(
            model_name='plasmidprep',
            constraint=iki.CheckConstraint(condition=iki.Q(('culture_volume__gte', 0)), name='iki__lots_plasmidprep__culture_volume__min'),
        ),
        iki.AddConstraint(
            model_name='plasmidprep',
            constraint=iki.CheckConstraint(condition=iki.Q(('a260__gt', float('-inf')), ('a260__lt', float('inf'))), name='iki__lots_plasmidprep__a260__finite'),
        ),
        iki.AddConstraint(
            model_name='plasmidprep',
            constraint=iki.CheckConstraint(condition=iki.Q(('a260__gte', 0)), name='iki__lots_plasmidprep__a260__min'),
        ),
        iki.AddConstraint(
            model_name='plasmidprep',
            constraint=iki.CheckConstraint(condition=iki.Q(('path_length__gt', float('-inf')), ('path_length__lt', float('inf'))), name='iki__lots_plasmidprep__path_length__finite'),
        ),
        iki.AddConstraint(
            model_name='plasmidprep',
            constraint=iki.CheckConstraint(condition=iki.Q(('path_length__gte', 0)), name='iki__lots_plasmidprep__path_length__min'),
        ),
        iki.AddConstraint(
            model_name='plasmidprep',
            constraint=iki.CheckConstraint(condition=iki.Q(('dilution_factor__gt', float('-inf')), ('dilution_factor__lt', float('inf'))), name='iki__lots_plasmidprep__dilution_factor__finite'),
        ),
        iki.AddConstraint(
            model_name='plasmidprep',
            constraint=iki.CheckConstraint(condition=iki.Q(('dilution_factor__gte', 1)), name='iki__lots_plasmidprep__dilution_factor__min'),
        ),
        iki.AddConstraint(
            model_name='plasmidprep',
            constraint=iki.CheckConstraint(condition=iki.Q(('concentration__gt', float('-inf')), ('concentration__lt', float('inf'))), name='iki__lots_plasmidprep__concentration__finite'),
        ),
        iki.AddConstraint(
            model_name='plasmidprep',
            constraint=iki.CheckConstraint(condition=iki.Q(('concentration__gte', 0)), name='iki__lots_plasmidprep__concentration__min'),
        ),
        iki.AddConstraint(
            model_name='plasmidprep',
            constraint=iki.CheckConstraint(condition=iki.Q(('a260_concentration__gt', float('-inf')), ('a260_concentration__lt', float('inf'))), name='iki__lots_plasmidprep__a260_concentration__finite'),
        ),
        iki.AddConstraint(
            model_name='plasmidprep',
            constraint=iki.CheckConstraint(condition=iki.Q(('a260_concentration__gte', 0)), name='iki__lots_plasmidprep__a260_concentration__min'),
        ),
        iki.AddConstraint(
            model_name='plasmidprep',
            constraint=iki.CheckConstraint(condition=iki.Q(('a260_a280__gt', float('-inf')), ('a260_a280__lt', float('inf'))), name='iki__lots_plasmidprep__a260_a280__finite'),
        ),
        iki.AddConstraint(
            model_name='plasmidprep',
            constraint=iki.CheckConstraint(condition=iki.Q(('a260_a280__gte', 0)), name='iki__lots_plasmidprep__a260_a280__min'),
        ),
        iki.AddConstraint(
            model_name='plasmidprep',
            constraint=iki.CheckConstraint(condition=iki.Q(('a260_a230__gt', float('-inf')), ('a260_a230__lt', float('inf'))), name='iki__lots_plasmidprep__a260_a230__finite'),
        ),
        iki.AddConstraint(
            model_name='plasmidprep',
            constraint=iki.CheckConstraint(condition=iki.Q(('a260_a230__gte', 0)), name='iki__lots_plasmidprep__a260_a230__min'),
        ),
        iki.AddConstraint(
            model_name='plasmidprep',
            constraint=iki.CheckConstraint(condition=iki.Q(('supercoiled__gt', float('-inf')), ('supercoiled__lt', float('inf'))), name='iki__lots_plasmidprep__supercoiled__finite'),
        ),
        iki.AddConstraint(
            model_name='plasmidprep',
            constraint=iki.CheckConstraint(condition=iki.Q(('supercoiled__gte', 0)), name='iki__lots_plasmidprep__supercoiled__min'),
        ),
        iki.AddConstraint(
            model_name='plasmidprep',
            constraint=iki.CheckConstraint(condition=iki.Q(('supercoiled__lte', 100)), name='iki__lots_plasmidprep__supercoiled__max'),
        ),
        iki.AddConstraint(
            model_name='plasmidprep',
            constraint=iki.CheckConstraint(condition=iki.Q(('endotoxin__gt', float('-inf')), ('endotoxin__lt', float('inf'))), name='iki__lots_plasmidprep__endotoxin__finite'),
        ),
        iki.AddConstraint(
            model_name='plasmidprep',
            constraint=iki.CheckConstraint(condition=iki.Q(('endotoxin__gte', 0)), name='iki__lots_plasmidprep__endotoxin__min'),
        ),
        iki.AddConstraint(
            model_name='polymerlot',
            constraint=iki.CheckConstraint(condition=iki.Q(('mn__gt', float('-inf')), ('mn__lt', float('inf'))), name='iki__lots_polymerlot__mn__finite'),
        ),
        iki.AddConstraint(
            model_name='polymerlot',
            constraint=iki.CheckConstraint(condition=iki.Q(('mn__gte', 0)), name='iki__lots_polymerlot__mn__min'),
        ),
        iki.AddConstraint(
            model_name='polymerlot',
            constraint=iki.CheckConstraint(condition=iki.Q(('mw__gt', float('-inf')), ('mw__lt', float('inf'))), name='iki__lots_polymerlot__mw__finite'),
        ),
        iki.AddConstraint(
            model_name='polymerlot',
            constraint=iki.CheckConstraint(condition=iki.Q(('mw__gte', 0)), name='iki__lots_polymerlot__mw__min'),
        ),
        iki.AddConstraint(
            model_name='polymerlot',
            constraint=iki.CheckConstraint(condition=iki.Q(('mp__gt', float('-inf')), ('mp__lt', float('inf'))), name='iki__lots_polymerlot__mp__finite'),
        ),
        iki.AddConstraint(
            model_name='polymerlot',
            constraint=iki.CheckConstraint(condition=iki.Q(('mp__gte', 0)), name='iki__lots_polymerlot__mp__min'),
        ),
        iki.AddConstraint(
            model_name='polymerlot',
            constraint=iki.CheckConstraint(condition=iki.Q(('dispersity__gt', float('-inf')), ('dispersity__lt', float('inf'))), name='iki__lots_polymerlot__dispersity__finite'),
        ),
        iki.AddConstraint(
            model_name='polymerlot',
            constraint=iki.CheckConstraint(condition=iki.Q(('dispersity__gte', 1)), name='iki__lots_polymerlot__dispersity__min'),
        ),
        iki.AddConstraint(
            model_name='polymerlot',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('eluent'), 1), name='iki__lots_polymerlot__eluent__not_empty'),
        ),
        iki.AddConstraint(
            model_name='polymerlot',
            constraint=iki.CheckConstraint(condition=iki.Q(('eluent__regex', '[\\r\\n]'), negated=True), name='iki__lots_polymerlot__eluent__one_line'),
        ),
        iki.AddField(
            model_name='strainisolate_verification_evidence',
            name='item',
            field=iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to=iki.FILE),
        ),
        iki.AddIndex(
            model_name='strainisolate',
            index=iki.GinIndex(fields=['isolate_label'], name='isolate_la_trgm_63375b67c31ef3', opclasses=['gin_trgm_ops']),
        ),
        iki.AddConstraint(
            model_name='strainisolate',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('isolate_label'), 1), name='iki__lots_strainisolate__isolate_label__not_empty'),
        ),
        iki.AddConstraint(
            model_name='strainisolate',
            constraint=iki.CheckConstraint(condition=iki.Q(('isolate_label__regex', '[\\r\\n]'), negated=True), name='iki__lots_strainisolate__isolate_label__one_line'),
        ),
        iki.AddConstraint(
            model_name='conjugatelot_drug_loads',
            constraint=iki.CheckConstraint(condition=iki.Q(('chain__in', ['intact', 'light', 'heavy'])), name='iki__lots_conjugatelot__drug_loads__chain__values'),
        ),
        iki.AddConstraint(
            model_name='conjugatelot_drug_loads',
            constraint=iki.CheckConstraint(condition=iki.Q(('drug_load__gte', 0)), name='iki__lots_conjugatelot__drug_loads__drug_load__min'),
        ),
        iki.AddConstraint(
            model_name='conjugatelot_drug_loads',
            constraint=iki.CheckConstraint(condition=iki.Q(('drug_load__lte', 20)), name='iki__lots_conjugatelot__drug_loads__drug_load__max'),
        ),
        iki.AddConstraint(
            model_name='conjugatelot_drug_loads',
            constraint=iki.CheckConstraint(condition=iki.Q(('area__gt', float('-inf')), ('area__lt', float('inf'))), name='iki__lots_conjugatelot__drug_loads__area__finite'),
        ),
        iki.AddConstraint(
            model_name='conjugatelot_drug_loads',
            constraint=iki.CheckConstraint(condition=iki.Q(('area__gte', 0)), name='iki__lots_conjugatelot__drug_loads__area__min'),
        ),
        iki.AddConstraint(
            model_name='conjugatelot_drug_loads',
            constraint=iki.CheckConstraint(condition=iki.Q(('area__lte', 100)), name='iki__lots_conjugatelot__drug_loads__area__max'),
        ),
        iki.AddConstraint(
            model_name='conjugatelot_drug_loads',
            constraint=iki.CheckConstraint(condition=iki.Q(('position__lt', 40)), name='iki__lots_conjugatelot__drug_loads__position__max_items'),
        ),
        iki.AddConstraint(
            model_name='conjugatelot',
            constraint=iki.CheckConstraint(condition=iki.Q(('dar_method__in', ['hic', 'lc_ms_intact', 'lc_ms_reduced', 'rp_hplc_reduced', 'uv_vis'])), name='iki__lots_conjugatelot__dar_method__values'),
        ),
        iki.AddConstraint(
            model_name='conjugatelot',
            constraint=iki.CheckConstraint(condition=iki.Q(('distribution_method__in', ['hic', 'lc_ms_intact', 'lc_ms_reduced', 'rp_hplc_reduced'])), name='iki__lots_conjugatelot__distribution_method__values'),
        ),
        iki.AddConstraint(
            model_name='conjugatelot',
            constraint=iki.CheckConstraint(condition=iki.Q(('dar__gt', float('-inf')), ('dar__lt', float('inf'))), name='iki__lots_conjugatelot__dar__finite'),
        ),
        iki.AddConstraint(
            model_name='conjugatelot',
            constraint=iki.CheckConstraint(condition=iki.Q(('dar__gte', 0)), name='iki__lots_conjugatelot__dar__min'),
        ),
        iki.AddConstraint(
            model_name='conjugatelot',
            constraint=iki.CheckConstraint(condition=iki.Q(('dar__lte', 20)), name='iki__lots_conjugatelot__dar__max'),
        ),
        iki.AddConstraint(
            model_name='conjugatelot',
            constraint=iki.CheckConstraint(condition=iki.Q(('distribution_dar__gt', float('-inf')), ('distribution_dar__lt', float('inf'))), name='iki__lots_conjugatelot__distribution_dar__finite'),
        ),
        iki.AddConstraint(
            model_name='conjugatelot',
            constraint=iki.CheckConstraint(condition=iki.Q(('distribution_dar__gte', 0)), name='iki__lots_conjugatelot__distribution_dar__min'),
        ),
        iki.AddConstraint(
            model_name='conjugatelot',
            constraint=iki.CheckConstraint(condition=iki.Q(('distribution_dar__lte', 20)), name='iki__lots_conjugatelot__distribution_dar__max'),
        ),
        iki.AddConstraint(
            model_name='conjugatelot',
            constraint=iki.CheckConstraint(condition=iki.Q(('unconjugated__gt', float('-inf')), ('unconjugated__lt', float('inf'))), name='iki__lots_conjugatelot__unconjugated__finite'),
        ),
        iki.AddConstraint(
            model_name='conjugatelot',
            constraint=iki.CheckConstraint(condition=iki.Q(('unconjugated__gte', 0)), name='iki__lots_conjugatelot__unconjugated__min'),
        ),
        iki.AddConstraint(
            model_name='conjugatelot',
            constraint=iki.CheckConstraint(condition=iki.Q(('unconjugated__lte', 100)), name='iki__lots_conjugatelot__unconjugated__max'),
        ),
        iki.AddConstraint(
            model_name='conjugatelot',
            constraint=iki.CheckConstraint(condition=iki.Q(('free_drug__gt', float('-inf')), ('free_drug__lt', float('inf'))), name='iki__lots_conjugatelot__free_drug__finite'),
        ),
        iki.AddConstraint(
            model_name='conjugatelot',
            constraint=iki.CheckConstraint(condition=iki.Q(('free_drug__gte', 0)), name='iki__lots_conjugatelot__free_drug__min'),
        ),
        iki.AddConstraint(
            model_name='conjugatelot',
            constraint=iki.CheckConstraint(condition=iki.Q(('free_drug__lte', 100)), name='iki__lots_conjugatelot__free_drug__max'),
        ),
        iki.AddConstraint(
            model_name='proteinlot',
            constraint=iki.CheckConstraint(condition=iki.Q(('cysteines__in', ['cystines', 'reduced'])), name='iki__lots_proteinlot__cysteines__values'),
        ),
        iki.AddConstraint(
            model_name='strainisolate_verification_evidence',
            constraint=iki.CheckConstraint(condition=iki.Q(('position__lt', 20)), name='iki__lots_strainisolate__verification_evidence__pos__3d4b8585ac'),
        ),
    ]
