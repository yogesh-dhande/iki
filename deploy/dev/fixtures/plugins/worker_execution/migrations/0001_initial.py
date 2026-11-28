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
            name='ExecutionSample_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('storage', iki.TextField(null=True)),
            ],
            table='fixtures__worker_execution_executionsample__history',
        ),
        iki.AppendOnly(
            model_name='ExecutionSample_History',
        ),
        iki.CreateModel(
            name='ExecutionSample',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('storage', iki.CharField(max_length=100, null=True)),
            ],
            table='fixtures__worker_execution_executionsample',
            constraints=[
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('storage'), 1), name='fixtures__worker_execution_executionsample__storage__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('storage__regex', '[\\r\\n]'), negated=True), name='fixtures__worker_execution_executionsample__storage__one_line'),
            ],
            bases=(iki.ENTITY,),
        ),
    ]
