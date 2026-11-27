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
            name='FASTQSummary_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('read_set', iki.UUIDField(db_column='read_set_id', null=True)),
                ('sample', iki.UUIDField(db_column='sample_id', null=True)),
                ('read_set_revision', iki.BigIntegerField(null=True)),
                ('library_revision', iki.BigIntegerField(null=True)),
                ('files', iki.ArrayField(base_field=iki.UUIDField(), null=True)),
                ('statistics', iki.JSONField(null=True)),
                ('review_flow', iki.TextField(null=True)),
                ('notes', iki.TextField(null=True)),
                ('review', iki.TextField(null=True)),
            ],
            table='iki__sequencing_fastqsummary__history',
        ),
        iki.AppendOnly(
            model_name='FASTQSummary_History',
        ),
        iki.CreateModel(
            name='ReadSet_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('run', iki.UUIDField(db_column='run_id', null=True)),
                ('library', iki.UUIDField(db_column='library_id', null=True)),
                ('lane', iki.TextField(null=True)),
                ('read_1', iki.UUIDField(db_column='read_1_id', null=True)),
                ('read_2', iki.UUIDField(db_column='read_2_id', null=True)),
                ('run_accession', iki.TextField(null=True)),
            ],
            table='iki__sequencing_readset__history',
        ),
        iki.AppendOnly(
            model_name='ReadSet_History',
        ),
        iki.CreateModel(
            name='SequencingLibrary_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('name', iki.TextField(null=True)),
                ('sample', iki.UUIDField(db_column='sample_id', null=True)),
                ('strategy', iki.TextField(null=True)),
                ('preparation', iki.TextField(null=True)),
                ('sample_accession', iki.TextField(null=True)),
            ],
            table='iki__sequencing_sequencinglibrary__history',
        ),
        iki.AppendOnly(
            model_name='SequencingLibrary_History',
        ),
        iki.CreateModel(
            name='SequencingRun_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('name', iki.TextField(null=True)),
                ('instrument_model', iki.TextField(null=True)),
                ('started_on', iki.DateField(null=True)),
                ('flow_cell', iki.TextField(null=True)),
            ],
            table='iki__sequencing_sequencingrun__history',
        ),
        iki.AppendOnly(
            model_name='SequencingRun_History',
        ),
        iki.CreateModel(
            name='ReadSet',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('lane', iki.CharField(max_length=100, null=True)),
                ('run_accession', iki.CharField(max_length=30, null=True)),
                ('read_1', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to=iki.FILE)),
                ('read_2', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to=iki.FILE)),
            ],
            table='iki__sequencing_readset',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='FASTQSummary',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('read_set_revision', iki.BigIntegerField(null=True)),
                ('library_revision', iki.BigIntegerField(null=True)),
                ('statistics', iki.JSONField(null=True)),
                ('review_flow', iki.TextField(null=True)),
                ('notes', iki.TextField(null=True)),
                ('review', iki.StateField(db_comment='A state field (ADR 0125): one of recorded, signed_off, submitted, reviewed, released.', db_default='recorded', locking=['signed_off', 'submitted', 'reviewed', 'released'], states=['recorded', 'signed_off', 'submitted', 'reviewed', 'released'])),
                ('sample', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to=iki.ENTITY)),
                ('read_set', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__sequencing.readset')),
            ],
            table='iki__sequencing_fastqsummary',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='SequencingLibrary',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('name', iki.CharField(max_length=200)),
                ('strategy', iki.CharField(max_length=100)),
                ('preparation', iki.TextField(null=True)),
                ('sample_accession', iki.CharField(max_length=32, null=True)),
                ('sample', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to=iki.ENTITY)),
            ],
            table='iki__sequencing_sequencinglibrary',
            bases=(iki.ENTITY,),
        ),
        iki.AddField(
            model_name='readset',
            name='library',
            field=iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__sequencing.sequencinglibrary'),
        ),
        iki.CreateModel(
            name='SequencingRun',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('name', iki.CharField(max_length=200)),
                ('instrument_model', iki.CharField(max_length=200)),
                ('started_on', iki.DateField(null=True)),
                ('flow_cell', iki.CharField(max_length=100, null=True)),
            ],
            table='iki__sequencing_sequencingrun',
            constraints=[
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('name'), 1), name='iki__sequencing_sequencingrun__name__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('name__regex', '[\\r\\n]'), negated=True), name='iki__sequencing_sequencingrun__name__one_line'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('instrument_model'), 1), name='iki__sequencing_sequencingrun__instrument_model__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('instrument_model__regex', '[\\r\\n]'), negated=True), name='iki__sequencing_sequencingrun__instrument_model__one_line'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('flow_cell'), 1), name='iki__sequencing_sequencingrun__flow_cell__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('flow_cell__regex', '[\\r\\n]'), negated=True), name='iki__sequencing_sequencingrun__flow_cell__one_line'),
            ],
            bases=(iki.ENTITY,),
        ),
        iki.AddField(
            model_name='readset',
            name='run',
            field=iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__sequencing.sequencingrun'),
        ),
        iki.CreateModel(
            name='FASTQSummary_files',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity', 'position')),
                ('entity', iki.ForeignKey(db_column='entity_id', on_delete=iki.CASCADE, related_name='+', to='iki__sequencing.fastqsummary')),
                ('position', iki.PositiveIntegerField()),
                ('item', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to=iki.FILE)),
            ],
            table='iki__sequencing_fastqsummary__files',
        ),
        iki.AddIndex(
            model_name='fastqsummary',
            index=iki.GinIndex(iki.SearchVector(iki.Left('notes', 100000), config='simple'), name='notes_fts_0bf24ff0d026a9700054'),
        ),
        iki.AddConstraint(
            model_name='fastqsummary',
            constraint=iki.CheckConstraint(condition=iki.Q(('read_set_revision__gte', 1)), name='iki__sequencing_fastqsummary__read_set_revision__min'),
        ),
        iki.AddConstraint(
            model_name='fastqsummary',
            constraint=iki.CheckConstraint(condition=iki.Q(('library_revision__gte', 1)), name='iki__sequencing_fastqsummary__library_revision__min'),
        ),
        iki.AddConstraint(
            model_name='fastqsummary',
            constraint=iki.CheckConstraint(condition=iki.Q(('review_flow__in', ['research', 'regulated_qc', 'record_only'])), name='iki__sequencing_fastqsummary__review_flow__values'),
        ),
        iki.AddConstraint(
            model_name='fastqsummary',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('notes'), 1), name='iki__sequencing_fastqsummary__notes__not_empty'),
        ),
        iki.AddConstraint(
            model_name='fastqsummary',
            constraint=iki.CheckConstraint(condition=iki.LessThanOrEqual(iki.Length('notes'), 3000), name='iki__sequencing_fastqsummary__notes__max_length'),
        ),
        iki.AddConstraint(
            model_name='fastqsummary',
            constraint=iki.CheckConstraint(condition=iki.Q(('review__in', ['recorded', 'signed_off', 'submitted', 'reviewed', 'released'])), name='iki__sequencing_fastqsummary__review__known'),
        ),
        iki.AddIndex(
            model_name='sequencinglibrary',
            index=iki.GinIndex(iki.SearchVector(iki.Left('preparation', 100000), config='simple'), name='preparatio_fts_ea31f1803dc9d90'),
        ),
        iki.AddConstraint(
            model_name='sequencinglibrary',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('name'), 1), name='iki__sequencing_sequencinglibrary__name__not_empty'),
        ),
        iki.AddConstraint(
            model_name='sequencinglibrary',
            constraint=iki.CheckConstraint(condition=iki.Q(('name__regex', '[\\r\\n]'), negated=True), name='iki__sequencing_sequencinglibrary__name__one_line'),
        ),
        iki.AddConstraint(
            model_name='sequencinglibrary',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('strategy'), 1), name='iki__sequencing_sequencinglibrary__strategy__not_empty'),
        ),
        iki.AddConstraint(
            model_name='sequencinglibrary',
            constraint=iki.CheckConstraint(condition=iki.Q(('strategy__regex', '[\\r\\n]'), negated=True), name='iki__sequencing_sequencinglibrary__strategy__one_line'),
        ),
        iki.AddConstraint(
            model_name='sequencinglibrary',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('preparation'), 1), name='iki__sequencing_sequencinglibrary__preparation__not_empty'),
        ),
        iki.AddConstraint(
            model_name='sequencinglibrary',
            constraint=iki.CheckConstraint(condition=iki.LessThanOrEqual(iki.Length('preparation'), 2000), name='iki__sequencing_sequencinglibrary__preparation__max_length'),
        ),
        iki.AddConstraint(
            model_name='sequencinglibrary',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('sample_accession'), 1), name='iki__sequencing_sequencinglibrary__sample_accession__not_empty'),
        ),
        iki.AddConstraint(
            model_name='sequencinglibrary',
            constraint=iki.CheckConstraint(condition=iki.Q(('sample_accession__regex', '[\\r\\n]'), negated=True), name='iki__sequencing_sequencinglibrary__sample_accession__one_line'),
        ),
        iki.AddConstraint(
            model_name='sequencinglibrary',
            constraint=iki.CheckConstraint(condition=iki.Q(('sample_accession__regex', '^(?:^(SAMN|SAMEA|SAMD)[0-9]+$)$')), name='iki__sequencing_sequencinglibrary__sample_accession__pattern'),
        ),
        iki.AddConstraint(
            model_name='readset',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('lane'), 1), name='iki__sequencing_readset__lane__not_empty'),
        ),
        iki.AddConstraint(
            model_name='readset',
            constraint=iki.CheckConstraint(condition=iki.Q(('lane__regex', '[\\r\\n]'), negated=True), name='iki__sequencing_readset__lane__one_line'),
        ),
        iki.AddConstraint(
            model_name='readset',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('run_accession'), 1), name='iki__sequencing_readset__run_accession__not_empty'),
        ),
        iki.AddConstraint(
            model_name='readset',
            constraint=iki.CheckConstraint(condition=iki.Q(('run_accession__regex', '[\\r\\n]'), negated=True), name='iki__sequencing_readset__run_accession__one_line'),
        ),
        iki.AddConstraint(
            model_name='readset',
            constraint=iki.CheckConstraint(condition=iki.Q(('run_accession__regex', '^(?:^(SRR|ERR|DRR)[0-9]+$)$')), name='iki__sequencing_readset__run_accession__pattern'),
        ),
        iki.AddConstraint(
            model_name='fastqsummary_files',
            constraint=iki.CheckConstraint(condition=iki.Q(('position__lt', 2)), name='iki__sequencing_fastqsummary__files__position__max_items'),
        ),
    ]
