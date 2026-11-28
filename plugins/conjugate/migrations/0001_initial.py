# Made by `iki plugin makemigrations`. Review it, and commit it with the manifests it
# migrates: once a deployment has applied it, it never changes.

from iki_server.plugins import operations as iki


class Migration(iki.Migration):

    initial = True

    dependencies = [
        ('iki__small_molecule', '0001_initial'),
        ('iki_anchors', '0001_iki_0_1'),
    ]

    operations = [
        iki.CreateModel(
            name='Conjugate_Entry',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('carrier_heavy_copies', iki.BigIntegerField(null=True)),
                ('carrier_light_copies', iki.BigIntegerField(null=True)),
            ],
            table='iki__conjugate_conjugate__entry',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='Conjugate_Entry_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('carrier', iki.JSONField(null=True)),
                ('attachments', iki.JSONField(null=True)),
                ('carrier_heavy_copies', iki.BigIntegerField(null=True)),
                ('carrier_light_copies', iki.BigIntegerField(null=True)),
            ],
            table='iki__conjugate_conjugate__entry__history',
        ),
        iki.AppendOnly(
            model_name='Conjugate_Entry_History',
        ),
        iki.CreateModel(
            name='Conjugate_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('name', iki.TextField(null=True)),
                ('description', iki.TextField(null=True)),
                ('molecular_formula', iki.TextField(null=True)),
                ('molecular_weight', iki.QuantityField(null=True, unit='u')),
                ('monoisotopic_mass', iki.QuantityField(null=True, unit='u')),
                ('composition_note', iki.TextField(null=True)),
            ],
            table='iki__conjugate_conjugate__history',
        ),
        iki.AppendOnly(
            model_name='Conjugate_History',
        ),
        iki.CreateModel(
            name='Conjugate',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('name', iki.CharField(db_index=True, max_length=255, null=True)),
                ('description', iki.TextField(null=True)),
                ('molecular_formula', iki.CharField(max_length=1000, null=True)),
                ('molecular_weight', iki.QuantityField(null=True, unit='u')),
                ('monoisotopic_mass', iki.QuantityField(null=True, unit='u')),
                ('composition_note', iki.CharField(max_length=1000, null=True)),
            ],
            table='iki__conjugate_conjugate',
            indexes=[
                iki.GinIndex(fields=['name'], name='name_trgm_f968fba63963f1596188', opclasses=['gin_trgm_ops']),
                iki.GinIndex(iki.SearchVector(iki.Left('description', 100000), config='simple'), name='descriptio_fts_81e0e444a7966d0'),
            ],
            constraints=[
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('name'), 1), name='iki__conjugate_conjugate__name__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('name__regex', '[\\r\\n]'), negated=True), name='iki__conjugate_conjugate__name__one_line'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('description'), 1), name='iki__conjugate_conjugate__description__not_empty'),
                iki.CheckConstraint(condition=iki.LessThanOrEqual(iki.Length('description'), 10000), name='iki__conjugate_conjugate__description__max_length'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('molecular_formula'), 1), name='iki__conjugate_conjugate__molecular_formula__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('molecular_formula__regex', '[\\r\\n]'), negated=True), name='iki__conjugate_conjugate__molecular_formula__one_line'),
                iki.CheckConstraint(condition=iki.Q(('molecular_weight__gt', float('-inf')), ('molecular_weight__lt', float('inf'))), name='iki__conjugate_conjugate__molecular_weight__finite'),
                iki.CheckConstraint(condition=iki.Q(('molecular_weight__gte', 0)), name='iki__conjugate_conjugate__molecular_weight__min'),
                iki.CheckConstraint(condition=iki.Q(('monoisotopic_mass__gt', float('-inf')), ('monoisotopic_mass__lt', float('inf'))), name='iki__conjugate_conjugate__monoisotopic_mass__finite'),
                iki.CheckConstraint(condition=iki.Q(('monoisotopic_mass__gte', 0)), name='iki__conjugate_conjugate__monoisotopic_mass__min'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('composition_note'), 1), name='iki__conjugate_conjugate__composition_note__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('composition_note__regex', '[\\r\\n]'), negated=True), name='iki__conjugate_conjugate__composition_note__one_line'),
            ],
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='Conjugate_Entry_attachments',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity', 'position')),
                ('entity', iki.ForeignKey(db_column='entity_id', on_delete=iki.CASCADE, related_name='+', to='iki__conjugate.conjugate_entry')),
                ('position', iki.PositiveIntegerField()),
                ('site', iki.CharField(max_length=200)),
                ('copies', iki.BigIntegerField(null=True)),
                ('loss_formula', iki.CharField(max_length=200, null=True)),
                ('peg_grade', iki.CharField(max_length=200, null=True)),
            ],
            table='iki__conjugate_conjugate__entry__attachments',
        ),
        iki.CreateModel(
            name='Conjugate_Entry_carrier',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity', 'position')),
                ('entity', iki.ForeignKey(db_column='entity_id', on_delete=iki.CASCADE, related_name='+', to='iki__conjugate.conjugate_entry')),
                ('position', iki.PositiveIntegerField()),
                ('kind', iki.TextField()),
            ],
            table='iki__conjugate_conjugate__entry__carrier',
        ),
        iki.AddConstraint(
            model_name='conjugate_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('carrier_heavy_copies__gte', 0)), name='iki__conjugate_conjugate__entry__carrier_heavy_copies__min'),
        ),
        iki.AddConstraint(
            model_name='conjugate_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('carrier_light_copies__gte', 0)), name='iki__conjugate_conjugate__entry__carrier_light_copies__min'),
        ),
        iki.AddField(
            model_name='conjugate_entry_attachments',
            name='part',
            field=iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__small_molecule.compound_entry'),
        ),
        iki.AddField(
            model_name='conjugate_entry_carrier',
            name='molecule',
            field=iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to=iki.ENTITY),
        ),
        iki.AddConstraint(
            model_name='conjugate_entry_attachments',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('site'), 1), name='iki__conjugate_conjugate__entry__attachments__site__not_empty'),
        ),
        iki.AddConstraint(
            model_name='conjugate_entry_attachments',
            constraint=iki.CheckConstraint(condition=iki.Q(('site__regex', '[\\r\\n]'), negated=True), name='iki__conjugate_conjugate__entry__attachments__site__one_line'),
        ),
        iki.AddConstraint(
            model_name='conjugate_entry_attachments',
            constraint=iki.CheckConstraint(condition=iki.Q(('copies__gte', 1)), name='iki__conjugate_conjugate__entry__attachments__copies__min'),
        ),
        iki.AddConstraint(
            model_name='conjugate_entry_attachments',
            constraint=iki.CheckConstraint(condition=iki.Q(('copies__lte', 10000)), name='iki__conjugate_conjugate__entry__attachments__copies__max'),
        ),
        iki.AddConstraint(
            model_name='conjugate_entry_attachments',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('loss_formula'), 1), name='iki__conjugate_conjugate__entry__attachments__loss___b626725d8a'),
        ),
        iki.AddConstraint(
            model_name='conjugate_entry_attachments',
            constraint=iki.CheckConstraint(condition=iki.Q(('loss_formula__regex', '[\\r\\n]'), negated=True), name='iki__conjugate_conjugate__entry__attachments__loss___2615cc83e0'),
        ),
        iki.AddConstraint(
            model_name='conjugate_entry_attachments',
            constraint=iki.CheckConstraint(condition=iki.Q(('loss_formula__regex', '^(none|((\\[[1-9][0-9]*[A-Z][a-z]?\\]|[A-Z][a-z]?)([1-9][0-9]*)?)+)$')), name='iki__conjugate_conjugate__entry__attachments__loss___44702152be'),
        ),
        iki.AddConstraint(
            model_name='conjugate_entry_attachments',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('peg_grade'), 1), name='iki__conjugate_conjugate__entry__attachments__peg_g__1ab2f41f74'),
        ),
        iki.AddConstraint(
            model_name='conjugate_entry_attachments',
            constraint=iki.CheckConstraint(condition=iki.Q(('peg_grade__regex', '[\\r\\n]'), negated=True), name='iki__conjugate_conjugate__entry__attachments__peg_g__8691a92e6d'),
        ),
        iki.AddConstraint(
            model_name='conjugate_entry_attachments',
            constraint=iki.CheckConstraint(condition=iki.Q(('position__lt', 64)), name='iki__conjugate_conjugate__entry__attachments__posit__fcb6e54927'),
        ),
        iki.AddConstraint(
            model_name='conjugate_entry_carrier',
            constraint=iki.CheckConstraint(condition=iki.Q(('kind__in', ['antibody_drug', 'oligo_conjugate', 'pegylated_protein'])), name='iki__conjugate_conjugate__entry__carrier__kind__values'),
        ),
        iki.AddConstraint(
            model_name='conjugate_entry_carrier',
            constraint=iki.CheckConstraint(condition=iki.Q(('position__lt', 1)), name='iki__conjugate_conjugate__entry__carrier__position__max_items'),
        ),
    ]
