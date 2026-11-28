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
            name='Polymer_Entry',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('bigsmiles', iki.TextField()),
                ('architecture', iki.TextField(db_index=True)),
                ('sequence_distribution', iki.TextField(db_index=True, null=True)),
                ('end_groups', iki.CharField(max_length=1000, null=True)),
                ('design_detail', iki.CharField(max_length=1000, null=True)),
            ],
            table='iki__polymer_polymer__entry',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='Polymer_Entry_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('bigsmiles', iki.TextField(null=True)),
                ('architecture', iki.TextField(null=True)),
                ('sequence_distribution', iki.TextField(null=True)),
                ('end_groups', iki.TextField(null=True)),
                ('design_detail', iki.TextField(null=True)),
                ('monomers', iki.ArrayField(base_field=iki.UUIDField(), null=True)),
            ],
            table='iki__polymer_polymer__entry__history',
        ),
        iki.AppendOnly(
            model_name='Polymer_Entry_History',
        ),
        iki.CreateModel(
            name='Polymer_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('name', iki.TextField(null=True)),
                ('description', iki.TextField(null=True)),
                ('notation_status', iki.TextField(null=True)),
                ('notation_note', iki.TextField(null=True)),
                ('stochastic_objects', iki.BigIntegerField(null=True)),
                ('repeat_units', iki.JSONField(null=True)),
                ('implicit_end_groups', iki.JSONField(null=True)),
            ],
            table='iki__polymer_polymer__history',
        ),
        iki.AppendOnly(
            model_name='Polymer_History',
        ),
        iki.CreateModel(
            name='Polymer',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('name', iki.CharField(db_index=True, max_length=255, null=True)),
                ('description', iki.TextField(null=True)),
                ('notation_status', iki.TextField(db_index=True, null=True)),
                ('notation_note', iki.CharField(max_length=2000, null=True)),
                ('stochastic_objects', iki.BigIntegerField(null=True)),
                ('repeat_units', iki.JSONField(null=True)),
                ('implicit_end_groups', iki.JSONField(null=True)),
            ],
            table='iki__polymer_polymer',
            indexes=[
                iki.GinIndex(fields=['name'], name='name_trgm_fe27b81b123e1dceac72', opclasses=['gin_trgm_ops']),
                iki.GinIndex(iki.SearchVector(iki.Left('description', 100000), config='simple'), name='descriptio_fts_48a8684b1468a8b'),
            ],
            constraints=[
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('name'), 1), name='iki__polymer_polymer__name__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('name__regex', '[\\r\\n]'), negated=True), name='iki__polymer_polymer__name__one_line'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('description'), 1), name='iki__polymer_polymer__description__not_empty'),
                iki.CheckConstraint(condition=iki.LessThanOrEqual(iki.Length('description'), 10000), name='iki__polymer_polymer__description__max_length'),
                iki.CheckConstraint(condition=iki.Q(('notation_status__in', ['checked', 'unsupported', 'invalid'])), name='iki__polymer_polymer__notation_status__values'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('notation_note'), 1), name='iki__polymer_polymer__notation_note__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('notation_note__regex', '[\\r\\n]'), negated=True), name='iki__polymer_polymer__notation_note__one_line'),
                iki.CheckConstraint(condition=iki.Q(('stochastic_objects__gte', 1)), name='iki__polymer_polymer__stochastic_objects__min'),
            ],
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='Polymer_Entry_monomers',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity', 'position')),
                ('entity', iki.ForeignKey(db_column='entity_id', on_delete=iki.CASCADE, related_name='+', to='iki__polymer.polymer_entry')),
                ('position', iki.PositiveIntegerField()),
            ],
            table='iki__polymer_polymer__entry__monomers',
        ),
        iki.AddConstraint(
            model_name='polymer_entry',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('bigsmiles'), 1), name='iki__polymer_polymer__entry__bigsmiles__not_empty'),
        ),
        iki.AddConstraint(
            model_name='polymer_entry',
            constraint=iki.CheckConstraint(condition=iki.LessThanOrEqual(iki.Length('bigsmiles'), 20000), name='iki__polymer_polymer__entry__bigsmiles__max_length'),
        ),
        iki.AddConstraint(
            model_name='polymer_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('architecture__in', ['linear', 'branched', 'star', 'comb', 'graft', 'dendritic', 'network', 'cyclic', 'other', 'unspecified'])), name='iki__polymer_polymer__entry__architecture__values'),
        ),
        iki.AddConstraint(
            model_name='polymer_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('sequence_distribution__in', ['homopolymer', 'statistical', 'random', 'alternating', 'block', 'gradient', 'other'])), name='iki__polymer_polymer__entry__sequence_distribution__values'),
        ),
        iki.AddConstraint(
            model_name='polymer_entry',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('end_groups'), 1), name='iki__polymer_polymer__entry__end_groups__not_empty'),
        ),
        iki.AddConstraint(
            model_name='polymer_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('end_groups__regex', '[\\r\\n]'), negated=True), name='iki__polymer_polymer__entry__end_groups__one_line'),
        ),
        iki.AddConstraint(
            model_name='polymer_entry',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('design_detail'), 1), name='iki__polymer_polymer__entry__design_detail__not_empty'),
        ),
        iki.AddConstraint(
            model_name='polymer_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('design_detail__regex', '[\\r\\n]'), negated=True), name='iki__polymer_polymer__entry__design_detail__one_line'),
        ),
        iki.AddField(
            model_name='polymer_entry_monomers',
            name='item',
            field=iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__small_molecule.compound_entry'),
        ),
        iki.AddConstraint(
            model_name='polymer_entry_monomers',
            constraint=iki.CheckConstraint(condition=iki.Q(('position__lt', 128)), name='iki__polymer_polymer__entry__monomers__position__max_items'),
        ),
    ]
