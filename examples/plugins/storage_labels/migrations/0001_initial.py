# Made by `iki plugin makemigrations`. Review it, and commit it with the manifests it
# migrates: once a deployment has applied it, it never changes.

from iki_server.plugins import operations as iki


class Migration(iki.Migration):

    initial = True

    dependencies = [
        ('iki__dna', '0001_initial'),
        ('iki_anchors', '0001_iki_0_1'),
    ]

    operations = [
        iki.CreateModel(
            name='Ext_iki_dna_DNASequence_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('construct_id', iki.TextField(null=True)),
                ('sequence_verified', iki.BooleanField(null=True)),
            ],
            table='acme__storage_labels__iki__dna_dnasequence__history',
        ),
        iki.AppendOnly(
            model_name='Ext_iki_dna_DNASequence_History',
        ),
        iki.CreateModel(
            name='Sample_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('name', iki.TextField(null=True)),
                ('kind', iki.TextField(null=True)),
                ('collected_on', iki.DateField(null=True)),
                ('volume', iki.QuantityField(null=True, unit='mL')),
                ('storage', iki.TextField(null=True)),
                ('derived_from', iki.UUIDField(db_column='derived_from_id', null=True)),
                ('construct', iki.UUIDField(db_column='construct_id', null=True)),
                ('notes', iki.TextField(null=True)),
            ],
            table='acme__storage_labels_sample__history',
        ),
        iki.AppendOnly(
            model_name='Sample_History',
        ),
        iki.CreateModel(
            name='Ext_iki_dna_DNASequence',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, primary_key=True, related_name='acme_storage_labels_Ext', serialize=False, to='iki__dna.dnasequence')),
                ('construct_id', iki.CharField(db_index=True, max_length=50, null=True)),
                ('sequence_verified', iki.BooleanField(db_default=False, null=True)),
            ],
            table='acme__storage_labels__iki__dna_dnasequence',
            indexes=[
                iki.GinIndex(fields=['construct_id'], name='construct_trgm_0f2be82f57981fb', opclasses=['gin_trgm_ops']),
            ],
            constraints=[
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('construct_id'), 1), name='acme__storage_labels__iki__dna_dnasequence__constru__5fa3313089'),
                iki.CheckConstraint(condition=iki.Q(('construct_id__regex', '[\\r\\n]'), negated=True), name='acme__storage_labels__iki__dna_dnasequence__constru__81315bfbbf'),
                iki.CheckConstraint(condition=iki.Q(('construct_id__regex', '^(?:^[A-Z]{2,5}-[0-9]+$)$')), name='acme__storage_labels__iki__dna_dnasequence__constru__7984fd075a'),
            ],
        ),
        iki.CreateModel(
            name='Sample',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('name', iki.CharField(db_index=True, max_length=200)),
                ('kind', iki.TextField()),
                ('collected_on', iki.DateField(null=True)),
                ('volume', iki.QuantityField(null=True, unit='mL')),
                ('storage', iki.CharField(db_index=True, max_length=100, null=True)),
                ('notes', iki.TextField(null=True)),
                ('construct', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to='iki__dna.dnasequence_entry')),
                ('derived_from', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to='acme__storage_labels.sample')),
            ],
            table='acme__storage_labels_sample',
            indexes=[
                iki.GinIndex(fields=['name'], name='name_trgm_95f791c4e5d6f2b1bd1a', opclasses=['gin_trgm_ops']),
                iki.GinIndex(fields=['storage'], name='storage_trgm_3e08f77a475215131', opclasses=['gin_trgm_ops']),
                iki.GinIndex(iki.SearchVector(iki.Left('notes', 100000), config='simple'), name='notes_fts_8b603c157b8fa33229fe'),
            ],
            constraints=[
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('name'), 1), name='acme__storage_labels_sample__name__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('name__regex', '[\\r\\n]'), negated=True), name='acme__storage_labels_sample__name__one_line'),
                iki.CheckConstraint(condition=iki.Q(('kind__in', ['tissue', 'cells', 'blood', 'dna', 'rna', 'protein', 'other'])), name='acme__storage_labels_sample__kind__values'),
                iki.CheckConstraint(condition=iki.Q(('volume__gt', float('-inf')), ('volume__lt', float('inf'))), name='acme__storage_labels_sample__volume__finite'),
                iki.CheckConstraint(condition=iki.Q(('volume__gte', 0)), name='acme__storage_labels_sample__volume__min'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('storage'), 1), name='acme__storage_labels_sample__storage__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('storage__regex', '[\\r\\n]'), negated=True), name='acme__storage_labels_sample__storage__one_line'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('notes'), 1), name='acme__storage_labels_sample__notes__not_empty'),
            ],
            bases=(iki.ENTITY,),
        ),
    ]
