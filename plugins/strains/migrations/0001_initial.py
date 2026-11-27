# Made by `iki plugin makemigrations`. Review it, and commit it with the manifests it
# migrates: once a deployment has applied it, it never changes.

from iki_server.plugins import operations as iki


class Migration(iki.Migration):

    initial = True

    dependencies = [
        ('iki__plasmid', '0001_initial'),
        ('iki_anchors', '0001_iki_0_1'),
    ]

    operations = [
        iki.CreateModel(
            name='BaseStrain_Entry_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('authority', iki.TextField(null=True)),
                ('accession', iki.TextField(null=True)),
                ('organism', iki.UUIDField(db_column='organism_id', null=True)),
                ('kind', iki.TextField(null=True)),
                ('genotype', iki.TextField(null=True)),
            ],
            table='iki__strains_basestrain__entry__history',
        ),
        iki.AppendOnly(
            model_name='BaseStrain_Entry_History',
        ),
        iki.CreateModel(
            name='BaseStrain_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('name', iki.TextField(null=True)),
            ],
            table='iki__strains_basestrain__history',
        ),
        iki.AppendOnly(
            model_name='BaseStrain_History',
        ),
        iki.CreateModel(
            name='TransformedStrain_Entry',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
            ],
            table='iki__strains_transformedstrain__entry',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='TransformedStrain_Entry_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('base', iki.UUIDField(db_column='base_id', null=True)),
                ('plasmids', iki.ArrayField(base_field=iki.UUIDField(), null=True)),
            ],
            table='iki__strains_transformedstrain__entry__history',
        ),
        iki.AppendOnly(
            model_name='TransformedStrain_Entry_History',
        ),
        iki.CreateModel(
            name='TransformedStrain_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('name', iki.TextField(null=True)),
            ],
            table='iki__strains_transformedstrain__history',
        ),
        iki.AppendOnly(
            model_name='TransformedStrain_History',
        ),
        iki.CreateModel(
            name='BaseStrain',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('name', iki.CharField(db_index=True, max_length=255, null=True)),
            ],
            table='iki__strains_basestrain',
            indexes=[
                iki.GinIndex(fields=['name'], name='name_trgm_7027bb6a0b18896d0bc4', opclasses=['gin_trgm_ops']),
            ],
            constraints=[
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('name'), 1), name='iki__strains_basestrain__name__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('name__regex', '[\\r\\n]'), negated=True), name='iki__strains_basestrain__name__one_line'),
            ],
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='BaseStrain_Entry',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('authority', iki.CharField(db_index=True, max_length=64)),
                ('accession', iki.CharField(db_index=True, max_length=128)),
                ('kind', iki.TextField(db_index=True)),
                ('genotype', iki.CharField(max_length=4000, null=True)),
                ('organism', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__core.organism_entry')),
            ],
            table='iki__strains_basestrain__entry',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='TransformedStrain',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('name', iki.CharField(db_index=True, max_length=255, null=True)),
            ],
            table='iki__strains_transformedstrain',
            indexes=[
                iki.GinIndex(fields=['name'], name='name_trgm_248df23efad5b82cfe9d', opclasses=['gin_trgm_ops']),
            ],
            constraints=[
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('name'), 1), name='iki__strains_transformedstrain__name__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('name__regex', '[\\r\\n]'), negated=True), name='iki__strains_transformedstrain__name__one_line'),
            ],
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='TransformedStrain_Entry_plasmids',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity', 'position')),
                ('entity', iki.ForeignKey(db_column='entity_id', on_delete=iki.CASCADE, related_name='+', to='iki__strains.transformedstrain_entry')),
                ('position', iki.PositiveIntegerField()),
            ],
            table='iki__strains_transformedstrain__entry__plasmids',
        ),
        iki.AddField(
            model_name='transformedstrain_entry',
            name='base',
            field=iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__strains.basestrain_entry'),
        ),
        iki.AddIndex(
            model_name='basestrain_entry',
            index=iki.GinIndex(fields=['authority'], name='authority_trgm_9a687c74bb10e55', opclasses=['gin_trgm_ops']),
        ),
        iki.AddIndex(
            model_name='basestrain_entry',
            index=iki.GinIndex(fields=['accession'], name='accession_trgm_27d0529d07d6fde', opclasses=['gin_trgm_ops']),
        ),
        iki.AddConstraint(
            model_name='basestrain_entry',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('authority'), 1), name='iki__strains_basestrain__entry__authority__not_empty'),
        ),
        iki.AddConstraint(
            model_name='basestrain_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('authority__regex', '[\\r\\n]'), negated=True), name='iki__strains_basestrain__entry__authority__one_line'),
        ),
        iki.AddConstraint(
            model_name='basestrain_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('authority__regex', '^(?:^[A-Z][A-Z0-9_-]*$)$')), name='iki__strains_basestrain__entry__authority__pattern'),
        ),
        iki.AddConstraint(
            model_name='basestrain_entry',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('accession'), 1), name='iki__strains_basestrain__entry__accession__not_empty'),
        ),
        iki.AddConstraint(
            model_name='basestrain_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('accession__regex', '[\\r\\n]'), negated=True), name='iki__strains_basestrain__entry__accession__one_line'),
        ),
        iki.AddConstraint(
            model_name='basestrain_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('accession__regex', '^(?:^[^\\s]+$)$')), name='iki__strains_basestrain__entry__accession__pattern'),
        ),
        iki.AddConstraint(
            model_name='basestrain_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('kind__in', ['bacterium', 'yeast'])), name='iki__strains_basestrain__entry__kind__values'),
        ),
        iki.AddConstraint(
            model_name='basestrain_entry',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('genotype'), 1), name='iki__strains_basestrain__entry__genotype__not_empty'),
        ),
        iki.AddConstraint(
            model_name='basestrain_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('genotype__regex', '[\\r\\n]'), negated=True), name='iki__strains_basestrain__entry__genotype__one_line'),
        ),
        iki.AddConstraint(
            model_name='basestrain_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('genotype__regex', '^(?:^[^\\s][^\\t\\r\\n]*$)$')), name='iki__strains_basestrain__entry__genotype__pattern'),
        ),
        iki.AddField(
            model_name='transformedstrain_entry_plasmids',
            name='item',
            field=iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__plasmid.plasmid_entry'),
        ),
        iki.AddConstraint(
            model_name='transformedstrain_entry_plasmids',
            constraint=iki.CheckConstraint(condition=iki.Q(('position__lt', 32)), name='iki__strains_transformedstrain__entry__plasmids__po__0d86283368'),
        ),
    ]
