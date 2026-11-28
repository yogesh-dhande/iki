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
            name='Experiment',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('title', iki.CharField(max_length=200)),
                ('eln_id', iki.CharField(max_length=256, null=True)),
                ('eln_url', iki.CharField(max_length=2048, null=True)),
                ('eln_revision', iki.CharField(max_length=256, null=True)),
                ('protocol_id', iki.CharField(max_length=2048, null=True)),
                ('protocol_revision', iki.CharField(max_length=256, null=True)),
                ('material_draws', iki.JSONField(null=True)),
                ('capture_id', iki.CharField(max_length=36, null=True)),
                ('review_flow', iki.TextField(null=True)),
                ('review', iki.StateField(db_comment='A state field (ADR 0125): one of recorded, signed, witnessed, submitted, reviewed, released.', db_default='recorded', locking=['signed', 'witnessed', 'submitted', 'reviewed', 'released'], states=['recorded', 'signed', 'witnessed', 'submitted', 'reviewed', 'released'])),
            ],
            table='iki__experiments_experiment',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='Experiment_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('title', iki.TextField(null=True)),
                ('eln_id', iki.TextField(null=True)),
                ('eln_url', iki.TextField(null=True)),
                ('eln_revision', iki.TextField(null=True)),
                ('protocol_id', iki.TextField(null=True)),
                ('protocol_revision', iki.TextField(null=True)),
                ('samples', iki.ArrayField(base_field=iki.UUIDField(), null=True)),
                ('observations', iki.JSONField(null=True)),
                ('attachments', iki.ArrayField(base_field=iki.UUIDField(), null=True)),
                ('material_draws', iki.JSONField(null=True)),
                ('result_pins', iki.JSONField(null=True)),
                ('capture_id', iki.TextField(null=True)),
                ('review_flow', iki.TextField(null=True)),
                ('review', iki.TextField(null=True)),
            ],
            table='iki__experiments_experiment__history',
        ),
        iki.AppendOnly(
            model_name='Experiment_History',
        ),
        iki.CreateModel(
            name='Experiment_attachments',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity', 'position')),
                ('entity', iki.ForeignKey(db_column='entity_id', on_delete=iki.CASCADE, related_name='+', to='iki__experiments.experiment')),
                ('position', iki.PositiveIntegerField()),
            ],
            table='iki__experiments_experiment__attachments',
        ),
        iki.CreateModel(
            name='Experiment_observations',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity', 'position')),
                ('entity', iki.ForeignKey(db_column='entity_id', on_delete=iki.CASCADE, related_name='+', to='iki__experiments.experiment')),
                ('position', iki.PositiveIntegerField()),
                ('observed_at', iki.DateTimeField()),
                ('note', iki.TextField()),
                ('observation_id', iki.CharField(max_length=36, null=True)),
            ],
            table='iki__experiments_experiment__observations',
        ),
        iki.CreateModel(
            name='Experiment_result_pins',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity', 'position')),
                ('entity', iki.ForeignKey(db_column='entity_id', on_delete=iki.CASCADE, related_name='+', to='iki__experiments.experiment')),
                ('position', iki.PositiveIntegerField()),
                ('record_id', iki.CharField(max_length=36)),
                ('record_revision', iki.BigIntegerField()),
                ('entry_id', iki.CharField(max_length=36, null=True)),
                ('entry_revision', iki.BigIntegerField(null=True)),
            ],
            table='iki__experiments_experiment__result_pins',
        ),
        iki.CreateModel(
            name='Experiment_samples',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity', 'position')),
                ('entity', iki.ForeignKey(db_column='entity_id', on_delete=iki.CASCADE, related_name='+', to='iki__experiments.experiment')),
                ('position', iki.PositiveIntegerField()),
            ],
            table='iki__experiments_experiment__samples',
        ),
        iki.AddConstraint(
            model_name='experiment',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('title'), 1), name='iki__experiments_experiment__title__not_empty'),
        ),
        iki.AddConstraint(
            model_name='experiment',
            constraint=iki.CheckConstraint(condition=iki.Q(('title__regex', '[\\r\\n]'), negated=True), name='iki__experiments_experiment__title__one_line'),
        ),
        iki.AddConstraint(
            model_name='experiment',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('eln_id'), 1), name='iki__experiments_experiment__eln_id__not_empty'),
        ),
        iki.AddConstraint(
            model_name='experiment',
            constraint=iki.CheckConstraint(condition=iki.Q(('eln_id__regex', '[\\r\\n]'), negated=True), name='iki__experiments_experiment__eln_id__one_line'),
        ),
        iki.AddConstraint(
            model_name='experiment',
            constraint=iki.CheckConstraint(condition=iki.Q(('eln_id__regex', '^(?:.*\\S.*)$')), name='iki__experiments_experiment__eln_id__pattern'),
        ),
        iki.AddConstraint(
            model_name='experiment',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('eln_url'), 1), name='iki__experiments_experiment__eln_url__not_empty'),
        ),
        iki.AddConstraint(
            model_name='experiment',
            constraint=iki.CheckConstraint(condition=iki.Q(('eln_url__regex', '[\\r\\n]'), negated=True), name='iki__experiments_experiment__eln_url__one_line'),
        ),
        iki.AddConstraint(
            model_name='experiment',
            constraint=iki.CheckConstraint(condition=iki.Q(('eln_url__regex', '^[A-Za-z][A-Za-z0-9+.-]*:[^\\s]+$')), name='iki__experiments_experiment__eln_url__format'),
        ),
        iki.AddConstraint(
            model_name='experiment',
            constraint=iki.CheckConstraint(condition=iki.Q(('eln_url__regex', '^(?:https?://[^\\s]+)$')), name='iki__experiments_experiment__eln_url__pattern'),
        ),
        iki.AddConstraint(
            model_name='experiment',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('eln_revision'), 1), name='iki__experiments_experiment__eln_revision__not_empty'),
        ),
        iki.AddConstraint(
            model_name='experiment',
            constraint=iki.CheckConstraint(condition=iki.Q(('eln_revision__regex', '[\\r\\n]'), negated=True), name='iki__experiments_experiment__eln_revision__one_line'),
        ),
        iki.AddConstraint(
            model_name='experiment',
            constraint=iki.CheckConstraint(condition=iki.Q(('eln_revision__regex', '^(?:.*\\S.*)$')), name='iki__experiments_experiment__eln_revision__pattern'),
        ),
        iki.AddConstraint(
            model_name='experiment',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('protocol_id'), 1), name='iki__experiments_experiment__protocol_id__not_empty'),
        ),
        iki.AddConstraint(
            model_name='experiment',
            constraint=iki.CheckConstraint(condition=iki.Q(('protocol_id__regex', '[\\r\\n]'), negated=True), name='iki__experiments_experiment__protocol_id__one_line'),
        ),
        iki.AddConstraint(
            model_name='experiment',
            constraint=iki.CheckConstraint(condition=iki.Q(('protocol_id__regex', '^(?:.*\\S.*)$')), name='iki__experiments_experiment__protocol_id__pattern'),
        ),
        iki.AddConstraint(
            model_name='experiment',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('protocol_revision'), 1), name='iki__experiments_experiment__protocol_revision__not_empty'),
        ),
        iki.AddConstraint(
            model_name='experiment',
            constraint=iki.CheckConstraint(condition=iki.Q(('protocol_revision__regex', '[\\r\\n]'), negated=True), name='iki__experiments_experiment__protocol_revision__one_line'),
        ),
        iki.AddConstraint(
            model_name='experiment',
            constraint=iki.CheckConstraint(condition=iki.Q(('protocol_revision__regex', '^(?:.*\\S.*)$')), name='iki__experiments_experiment__protocol_revision__pattern'),
        ),
        iki.AddConstraint(
            model_name='experiment',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('capture_id'), 1), name='iki__experiments_experiment__capture_id__not_empty'),
        ),
        iki.AddConstraint(
            model_name='experiment',
            constraint=iki.CheckConstraint(condition=iki.Q(('capture_id__regex', '[\\r\\n]'), negated=True), name='iki__experiments_experiment__capture_id__one_line'),
        ),
        iki.AddConstraint(
            model_name='experiment',
            constraint=iki.CheckConstraint(condition=iki.Q(('review_flow__in', ['author_signed', 'research', 'regulated_qc'])), name='iki__experiments_experiment__review_flow__values'),
        ),
        iki.AddConstraint(
            model_name='experiment',
            constraint=iki.CheckConstraint(condition=iki.Q(('review__in', ['recorded', 'signed', 'witnessed', 'submitted', 'reviewed', 'released'])), name='iki__experiments_experiment__review__known'),
        ),
        iki.AddField(
            model_name='experiment_attachments',
            name='item',
            field=iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to=iki.FILE),
        ),
        iki.AddConstraint(
            model_name='experiment_observations',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('note'), 1), name='iki__experiments_experiment__observations__note__not_empty'),
        ),
        iki.AddConstraint(
            model_name='experiment_observations',
            constraint=iki.CheckConstraint(condition=iki.LessThanOrEqual(iki.Length('note'), 10000), name='iki__experiments_experiment__observations__note__max_length'),
        ),
        iki.AddConstraint(
            model_name='experiment_observations',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('observation_id'), 1), name='iki__experiments_experiment__observations__observat__2ea1a95679'),
        ),
        iki.AddConstraint(
            model_name='experiment_observations',
            constraint=iki.CheckConstraint(condition=iki.Q(('observation_id__regex', '[\\r\\n]'), negated=True), name='iki__experiments_experiment__observations__observat__51bc43e7e6'),
        ),
        iki.AddConstraint(
            model_name='experiment_observations',
            constraint=iki.CheckConstraint(condition=iki.Q(('observation_id__regex', '^(?:^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$)$')), name='iki__experiments_experiment__observations__observat__77d15f4e80'),
        ),
        iki.AddConstraint(
            model_name='experiment_observations',
            constraint=iki.CheckConstraint(condition=iki.Q(('position__lt', 1000)), name='iki__experiments_experiment__observations__position__max_items'),
        ),
        iki.AddField(
            model_name='experiment_result_pins',
            name='result',
            field=iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to=iki.ENTITY),
        ),
        iki.AddField(
            model_name='experiment_samples',
            name='item',
            field=iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to=iki.ENTITY),
        ),
        iki.AddConstraint(
            model_name='experiment_attachments',
            constraint=iki.CheckConstraint(condition=iki.Q(('position__lt', 100)), name='iki__experiments_experiment__attachments__position__max_items'),
        ),
        iki.AddConstraint(
            model_name='experiment_result_pins',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('record_id'), 1), name='iki__experiments_experiment__result_pins__record_id__not_empty'),
        ),
        iki.AddConstraint(
            model_name='experiment_result_pins',
            constraint=iki.CheckConstraint(condition=iki.Q(('record_id__regex', '[\\r\\n]'), negated=True), name='iki__experiments_experiment__result_pins__record_id__one_line'),
        ),
        iki.AddConstraint(
            model_name='experiment_result_pins',
            constraint=iki.CheckConstraint(condition=iki.Q(('record_revision__gte', 1)), name='iki__experiments_experiment__result_pins__record_revision__min'),
        ),
        iki.AddConstraint(
            model_name='experiment_result_pins',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('entry_id'), 1), name='iki__experiments_experiment__result_pins__entry_id__not_empty'),
        ),
        iki.AddConstraint(
            model_name='experiment_result_pins',
            constraint=iki.CheckConstraint(condition=iki.Q(('entry_id__regex', '[\\r\\n]'), negated=True), name='iki__experiments_experiment__result_pins__entry_id__one_line'),
        ),
        iki.AddConstraint(
            model_name='experiment_result_pins',
            constraint=iki.CheckConstraint(condition=iki.Q(('entry_revision__gte', 1)), name='iki__experiments_experiment__result_pins__entry_revision__min'),
        ),
        iki.AddConstraint(
            model_name='experiment_result_pins',
            constraint=iki.CheckConstraint(condition=iki.Q(('position__lt', 1000)), name='iki__experiments_experiment__result_pins__position__max_items'),
        ),
        iki.AddConstraint(
            model_name='experiment_samples',
            constraint=iki.CheckConstraint(condition=iki.Q(('position__lt', 1000)), name='iki__experiments_experiment__samples__position__max_items'),
        ),
    ]
