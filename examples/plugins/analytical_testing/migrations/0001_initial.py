# Made by `iki plugin makemigrations`. Review it, and commit it with the manifests it
# migrates: once a deployment has applied it, it never changes.

from iki_server.plugins import operations as iki


class Migration(iki.Migration):

    initial = True

    dependencies = [
        ('iki__requests', '0001_initial'),
        ('iki_anchors', '0001_iki_0_1'),
    ]

    operations = [
        iki.CreateModel(
            name='AnalyticalRequest_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('analyses', iki.ArrayField(base_field=iki.TextField(), null=True)),
            ],
            table='acme__analytical_testing_analyticalrequest__history',
        ),
        iki.AppendOnly(
            model_name='AnalyticalRequest_History',
        ),
        iki.CreateModel(
            name='AnalyticalResult_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('analysis', iki.TextField(null=True)),
                ('purity', iki.FloatField(null=True)),
                ('measured_mass', iki.QuantityField(null=True, unit='u')),
                ('peptide_coverage', iki.QuantityField(null=True, unit='%')),
                ('endotoxin', iki.QuantityField(null=True, unit='{EU}/mg')),
                ('result', iki.TextField(null=True)),
            ],
            table='acme__analytical_testing_analyticalresult__history',
        ),
        iki.AppendOnly(
            model_name='AnalyticalResult_History',
        ),
        iki.CreateModel(
            name='AnalyticalRequest',
            fields=[
                ('qcrequest_ptr', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to='iki__requests.qcrequest')),
                ('analyses', iki.ArrayField(base_field=iki.TextField(), null=True)),
            ],
            table='acme__analytical_testing_analyticalrequest',
            constraints=[
                iki.CheckConstraint(condition=iki.IsPlainList('analyses'), name='acme__analytical_testing_analyticalrequest__analyse__43a837bdd0'),
                iki.CheckConstraint(condition=iki.Q(('analyses__len__lte', 4)), name='acme__analytical_testing_analyticalrequest__analyses__max_items'),
                iki.CheckConstraint(condition=iki.Q(('analyses__contained_by', ['sec_purity', 'intact_mass', 'peptide_map', 'endotoxin'])), name='acme__analytical_testing_analyticalrequest__analyse__9a8cb4da69'),
            ],
            bases=('iki__requests.qcrequest',),
        ),
        iki.CreateModel(
            name='AnalyticalResult',
            fields=[
                ('qcdeliverable_ptr', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to='iki__requests.qcdeliverable')),
                ('analysis', iki.TextField()),
                ('purity', iki.FloatField(null=True)),
                ('measured_mass', iki.QuantityField(null=True, unit='u')),
                ('peptide_coverage', iki.QuantityField(null=True, unit='%')),
                ('endotoxin', iki.QuantityField(null=True, unit='{EU}/mg')),
                ('result', iki.TextField()),
            ],
            table='acme__analytical_testing_analyticalresult',
            constraints=[
                iki.CheckConstraint(condition=iki.Q(('analysis__in', ['sec_purity', 'intact_mass', 'peptide_map', 'endotoxin'])), name='acme__analytical_testing_analyticalresult__analysis__values'),
                iki.CheckConstraint(condition=iki.Q(('purity__gt', float('-inf')), ('purity__lt', float('inf'))), name='acme__analytical_testing_analyticalresult__purity__finite'),
                iki.CheckConstraint(condition=iki.Q(('purity__gte', 0)), name='acme__analytical_testing_analyticalresult__purity__min'),
                iki.CheckConstraint(condition=iki.Q(('purity__lte', 100)), name='acme__analytical_testing_analyticalresult__purity__max'),
                iki.CheckConstraint(condition=iki.Q(('measured_mass__gt', float('-inf')), ('measured_mass__lt', float('inf'))), name='acme__analytical_testing_analyticalresult__measured__fe88f75b85'),
                iki.CheckConstraint(condition=iki.Q(('measured_mass__gte', 0)), name='acme__analytical_testing_analyticalresult__measured_mass__min'),
                iki.CheckConstraint(condition=iki.Q(('peptide_coverage__gt', float('-inf')), ('peptide_coverage__lt', float('inf'))), name='acme__analytical_testing_analyticalresult__peptide___f96b25b4ea'),
                iki.CheckConstraint(condition=iki.Q(('peptide_coverage__gte', 0)), name='acme__analytical_testing_analyticalresult__peptide___2d8bfc7b70'),
                iki.CheckConstraint(condition=iki.Q(('peptide_coverage__lte', 100)), name='acme__analytical_testing_analyticalresult__peptide___7aa0c30a20'),
                iki.CheckConstraint(condition=iki.Q(('endotoxin__gt', float('-inf')), ('endotoxin__lt', float('inf'))), name='acme__analytical_testing_analyticalresult__endotoxin__finite'),
                iki.CheckConstraint(condition=iki.Q(('endotoxin__gte', 0)), name='acme__analytical_testing_analyticalresult__endotoxin__min'),
                iki.CheckConstraint(condition=iki.Q(('result__in', ['pass', 'fail'])), name='acme__analytical_testing_analyticalresult__result__values'),
            ],
            bases=('iki__requests.qcdeliverable',),
        ),
    ]
