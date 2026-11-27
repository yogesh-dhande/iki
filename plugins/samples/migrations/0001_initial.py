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
            name='Sample',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('run', iki.TextField(null=True)),
                ('receipt', iki.JSONField(null=True)),
                ('notes', iki.TextField(null=True)),
                ('due_on', iki.DateField(null=True)),
                ('purpose', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to=iki.ENTITY)),
                ('source', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to=iki.ENTITY)),
            ],
            table='iki__samples_sample',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='PlainSample_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('analyst', iki.UUIDField(db_column='analyst_id', null=True)),
                ('status', iki.TextField(null=True)),
            ],
            table='iki__samples_plainsample__history',
        ),
        iki.AppendOnly(
            model_name='PlainSample_History',
        ),
        iki.CreateModel(
            name='ReviewedSample_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('analyst', iki.UUIDField(db_column='analyst_id', null=True)),
                ('reviewer', iki.UUIDField(db_column='reviewer_id', null=True)),
                ('status', iki.TextField(null=True)),
            ],
            table='iki__samples_reviewedsample__history',
        ),
        iki.AppendOnly(
            model_name='ReviewedSample_History',
        ),
        iki.CreateModel(
            name='Sample_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('source', iki.UUIDField(db_column='source_id', null=True)),
                ('purpose', iki.UUIDField(db_column='purpose_id', null=True)),
                ('run', iki.TextField(null=True)),
                ('receipt', iki.JSONField(null=True)),
                ('measurements', iki.JSONField(null=True)),
                ('notes', iki.TextField(null=True)),
                ('due_on', iki.DateField(null=True)),
            ],
            table='iki__samples_sample__history',
        ),
        iki.AppendOnly(
            model_name='Sample_History',
        ),
        iki.CreateModel(
            name='PlainSample',
            fields=[
                ('sample_ptr', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to='iki__samples.sample')),
                ('status', iki.StateField(db_comment='A state field (ADR 0125): one of pending, received, in_testing, done, retesting.', db_default='pending', locking=['done'], states=['pending', 'received', 'in_testing', 'done', 'retesting'])),
                ('analyst', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to=iki.USER)),
            ],
            table='iki__samples_plainsample',
            bases=('iki__samples.sample',),
        ),
        iki.CreateModel(
            name='ReviewedSample',
            fields=[
                ('sample_ptr', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to='iki__samples.sample')),
                ('status', iki.StateField(db_comment='A state field (ADR 0125): one of pending, received, in_testing, in_review, reviewed, retesting.', db_default='pending', locking=['in_review', 'reviewed'], states=['pending', 'received', 'in_testing', 'in_review', 'reviewed', 'retesting'])),
                ('analyst', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to=iki.USER)),
                ('reviewer', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to=iki.USER)),
            ],
            table='iki__samples_reviewedsample',
            bases=('iki__samples.sample',),
        ),
        iki.CreateModel(
            name='Sample_measurements',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity', 'position')),
                ('entity', iki.ForeignKey(db_column='entity_id', on_delete=iki.CASCADE, related_name='+', to='iki__samples.sample')),
                ('position', iki.PositiveIntegerField()),
                ('method', iki.CharField(max_length=150)),
                ('value', iki.CharField(max_length=250)),
                ('unit', iki.CharField(max_length=40, null=True)),
                ('notes', iki.TextField(null=True)),
            ],
            table='iki__samples_sample__measurements',
            constraints=[
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('method'), 1), name='iki__samples_sample__measurements__method__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('method__regex', '[\\r\\n]'), negated=True), name='iki__samples_sample__measurements__method__one_line'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('value'), 1), name='iki__samples_sample__measurements__value__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('value__regex', '[\\r\\n]'), negated=True), name='iki__samples_sample__measurements__value__one_line'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('unit'), 1), name='iki__samples_sample__measurements__unit__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('unit__regex', '[\\r\\n]'), negated=True), name='iki__samples_sample__measurements__unit__one_line'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('notes'), 1), name='iki__samples_sample__measurements__notes__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('position__lt', 200)), name='iki__samples_sample__measurements__position__max_items'),
            ],
        ),
        iki.AddIndex(
            model_name='sample',
            index=iki.GinIndex(iki.SearchVector(iki.Left('notes', 100000), config='simple'), name='notes_fts_dfb83e3eeffb0e445d90'),
        ),
        iki.AddConstraint(
            model_name='sample',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('run'), 1), name='iki__samples_sample__run__not_empty'),
        ),
        iki.AddConstraint(
            model_name='sample',
            constraint=iki.CheckConstraint(condition=iki.Q(('run__regex', '[\\r\\n]'), negated=True), name='iki__samples_sample__run__one_line'),
        ),
        iki.AddConstraint(
            model_name='sample',
            constraint=iki.CheckConstraint(condition=iki.Q(('run__regex', '^(?:^RUN-[0-9]+$)$')), name='iki__samples_sample__run__pattern'),
        ),
        iki.AddConstraint(
            model_name='sample',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('notes'), 1), name='iki__samples_sample__notes__not_empty'),
        ),
        iki.AddConstraint(
            model_name='plainsample',
            constraint=iki.CheckConstraint(condition=iki.Q(('status__in', ['pending', 'received', 'in_testing', 'done', 'retesting'])), name='iki__samples_plainsample__status__known'),
        ),
        iki.AddConstraint(
            model_name='reviewedsample',
            constraint=iki.CheckConstraint(condition=iki.Q(('status__in', ['pending', 'received', 'in_testing', 'in_review', 'reviewed', 'retesting'])), name='iki__samples_reviewedsample__status__known'),
        ),
    ]
