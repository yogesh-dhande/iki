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
            name='SequencingDeliverable',
            fields=[
                ('qcdeliverable_ptr', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to='iki__requests.qcdeliverable')),
                ('run_accession', iki.CharField(max_length=30, null=True)),
            ],
            table='acme__sequencing_service_sequencingdeliverable',
            bases=('iki__requests.qcdeliverable',),
        ),
        iki.CreateModel(
            name='SequencingDeliverable_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('data_files', iki.ArrayField(base_field=iki.UUIDField(), null=True)),
                ('run_accession', iki.TextField(null=True)),
            ],
            table='acme__sequencing_service_sequencingdeliverable__history',
        ),
        iki.AppendOnly(
            model_name='SequencingDeliverable_History',
        ),
        iki.CreateModel(
            name='SequencingRequest',
            fields=[
                ('qcrequest_ptr', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to='iki__requests.qcrequest')),
                ('method', iki.TextField()),
            ],
            table='acme__sequencing_service_sequencingrequest',
            bases=('iki__requests.qcrequest',),
        ),
        iki.CreateModel(
            name='SequencingRequest_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('method', iki.TextField(null=True)),
            ],
            table='acme__sequencing_service_sequencingrequest__history',
        ),
        iki.AppendOnly(
            model_name='SequencingRequest_History',
        ),
        iki.CreateModel(
            name='SequencingDeliverable_data_files',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity', 'position')),
                ('entity', iki.ForeignKey(db_column='entity_id', on_delete=iki.CASCADE, related_name='+', to='acme__sequencing_service.sequencingdeliverable')),
                ('position', iki.PositiveIntegerField()),
            ],
            table='acme__sequencing_service_sequencingdeliverable__data_files',
        ),
        iki.AddConstraint(
            model_name='sequencingdeliverable',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('run_accession'), 1), name='acme__sequencing_service_sequencingdeliverable__run__c94e1d9762'),
        ),
        iki.AddConstraint(
            model_name='sequencingdeliverable',
            constraint=iki.CheckConstraint(condition=iki.Q(('run_accession__regex', '[\\r\\n]'), negated=True), name='acme__sequencing_service_sequencingdeliverable__run__87a0552523'),
        ),
        iki.AddConstraint(
            model_name='sequencingdeliverable',
            constraint=iki.CheckConstraint(condition=iki.Q(('run_accession__regex', '^(?:^(SRR|ERR|DRR)[0-9]+$)$')), name='acme__sequencing_service_sequencingdeliverable__run__fb63ff7702'),
        ),
        iki.AddConstraint(
            model_name='sequencingrequest',
            constraint=iki.CheckConstraint(condition=iki.Q(('method__in', ['sanger', 'short_read', 'long_read'])), name='acme__sequencing_service_sequencingrequest__method__values'),
        ),
        iki.AddField(
            model_name='sequencingdeliverable_data_files',
            name='item',
            field=iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to=iki.FILE),
        ),
        iki.AddConstraint(
            model_name='sequencingdeliverable_data_files',
            constraint=iki.CheckConstraint(condition=iki.Q(('position__lt', 12)), name='acme__sequencing_service_sequencingdeliverable__dat__8509b79379'),
        ),
    ]
