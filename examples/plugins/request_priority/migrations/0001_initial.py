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
            name='SequencingRequest',
            fields=[
                ('plainrequest_ptr', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to='iki__requests.plainrequest')),
                ('project_rank', iki.BigIntegerField(null=True)),
            ],
            table='acme__request_priority_sequencingrequest',
            bases=('iki__requests.plainrequest',),
        ),
        iki.CreateModel(
            name='SequencingRequest_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('project_rank', iki.BigIntegerField(null=True)),
            ],
            table='acme__request_priority_sequencingrequest__history',
        ),
        iki.AppendOnly(
            model_name='SequencingRequest_History',
        ),
        iki.CreateModel(
            name='ServedProject_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('project_slug', iki.TextField(null=True)),
                ('project_name', iki.TextField(null=True)),
                ('rank', iki.BigIntegerField(null=True)),
                ('basis', iki.TextField(null=True)),
                ('status', iki.TextField(null=True)),
            ],
            table='acme__request_priority_servedproject__history',
        ),
        iki.AppendOnly(
            model_name='ServedProject_History',
        ),
        iki.CreateModel(
            name='ServedProject',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('project_slug', iki.CharField(db_index=True, max_length=100)),
                ('project_name', iki.CharField(max_length=200, null=True)),
                ('rank', iki.BigIntegerField()),
                ('basis', iki.TextField(null=True)),
                ('status', iki.StateField(db_comment='A state field (ADR 0125): one of proposed, agreed.', db_default='proposed', locking=['agreed'], states=['proposed', 'agreed'])),
            ],
            table='acme__request_priority_servedproject',
            indexes=[
                iki.GinIndex(fields=['project_slug'], name='project_sl_trgm_5aeb35e56dbd29', opclasses=['gin_trgm_ops']),
                iki.GinIndex(iki.SearchVector(iki.Left('basis', 100000), config='simple'), name='basis_fts_9ebb20edd3bb222b3ddc'),
            ],
            constraints=[
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('project_slug'), 1), name='acme__request_priority_servedproject__project_slug__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('project_slug__regex', '[\\r\\n]'), negated=True), name='acme__request_priority_servedproject__project_slug__one_line'),
                iki.CheckConstraint(condition=iki.Q(('project_slug__regex', '^(?:^[a-z0-9]+(-[a-z0-9]+)*$)$')), name='acme__request_priority_servedproject__project_slug__pattern'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('project_name'), 1), name='acme__request_priority_servedproject__project_name__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('project_name__regex', '[\\r\\n]'), negated=True), name='acme__request_priority_servedproject__project_name__one_line'),
                iki.CheckConstraint(condition=iki.Q(('rank__gte', 1)), name='acme__request_priority_servedproject__rank__min'),
                iki.CheckConstraint(condition=iki.Q(('rank__lte', 1000)), name='acme__request_priority_servedproject__rank__max'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('basis'), 1), name='acme__request_priority_servedproject__basis__not_empty'),
                iki.CheckConstraint(condition=iki.LessThanOrEqual(iki.Length('basis'), 2000), name='acme__request_priority_servedproject__basis__max_length'),
                iki.CheckConstraint(condition=iki.Q(('status__in', ['proposed', 'agreed'])), name='acme__request_priority_servedproject__status__known'),
            ],
            bases=(iki.ENTITY,),
        ),
    ]
