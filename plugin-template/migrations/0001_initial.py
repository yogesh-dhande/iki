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
            name='Sample_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('name', iki.TextField(null=True)),
                ('storage', iki.TextField(null=True)),
            ],
            table='acme__example_sample__history',
        ),
        iki.AppendOnly(
            model_name='Sample_History',
        ),
        iki.CreateModel(
            name='Sample',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('name', iki.CharField(db_index=True, max_length=200)),
                ('storage', iki.CharField(max_length=100, null=True)),
            ],
            table='acme__example_sample',
            indexes=[
                iki.GinIndex(fields=['name'], name='name_trgm_a086676352f6756d0f3a', opclasses=['gin_trgm_ops']),
            ],
            constraints=[
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('name'), 1), name='acme__example_sample__name__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('name__regex', '[\\r\\n]'), negated=True), name='acme__example_sample__name__one_line'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('storage'), 1), name='acme__example_sample__storage__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('storage__regex', '[\\r\\n]'), negated=True), name='acme__example_sample__storage__one_line'),
            ],
            bases=(iki.ENTITY,),
        ),
    ]
