# Made by `iki plugin makemigrations`. Review it, and commit it with the manifests it
# migrates: once a deployment has applied it, it never changes.

from iki_server.plugins import operations as iki


class Migration(iki.Migration):

    initial = True

    dependencies = [
        ('iki__inventory', '0001_initial'),
        ('iki_anchors', '0001_iki_0_1'),
    ]

    operations = [
        iki.CreateModel(
            name='Deliverable',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('outcome', iki.TextField()),
                ('material', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to=iki.ENTITY)),
                ('report', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to=iki.FILE)),
            ],
            table='iki__requests_deliverable',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='Deliverable_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('outcome', iki.TextField(null=True)),
                ('report', iki.UUIDField(db_column='report_id', null=True)),
                ('material', iki.UUIDField(db_column='material_id', null=True)),
            ],
            table='iki__requests_deliverable__history',
        ),
        iki.AppendOnly(
            model_name='Deliverable_History',
        ),
        iki.CreateModel(
            name='PlainDeliverable_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('request', iki.UUIDField(db_column='request_id', null=True)),
                ('status', iki.TextField(null=True)),
            ],
            table='iki__requests_plaindeliverable__history',
        ),
        iki.AppendOnly(
            model_name='PlainDeliverable_History',
        ),
        iki.CreateModel(
            name='Request',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('title', iki.CharField(max_length=200)),
                ('needed_by', iki.DateField(null=True)),
                ('instructions', iki.TextField(null=True)),
                ('standing', iki.StateField(db_comment='A state field (ADR 0125): one of open, withdrawn.', db_default='open', states=['open', 'withdrawn'])),
                ('status', iki.StateField(db_comment='A state field (ADR 0125): one of submitted, accepted, in_progress, delivered, declined.', db_default='submitted', locking=['delivered'], states=['submitted', 'accepted', 'in_progress', 'delivered', 'declined'])),
                ('priority', iki.TextField(db_default='routine', null=True)),
                ('scheduled_on', iki.DateField(null=True)),
                ('service_notes', iki.TextField(null=True)),
                ('analyst', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to=iki.USER)),
            ],
            table='iki__requests_request',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='PlainRequest_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
            ],
            table='iki__requests_plainrequest__history',
        ),
        iki.AppendOnly(
            model_name='PlainRequest_History',
        ),
        iki.CreateModel(
            name='QCDeliverable_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('request', iki.UUIDField(db_column='request_id', null=True)),
                ('notes', iki.TextField(null=True)),
                ('review', iki.TextField(null=True)),
            ],
            table='iki__requests_qcdeliverable__history',
        ),
        iki.AppendOnly(
            model_name='QCDeliverable_History',
        ),
        iki.CreateModel(
            name='QCRequest_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
            ],
            table='iki__requests_qcrequest__history',
        ),
        iki.AppendOnly(
            model_name='QCRequest_History',
        ),
        iki.CreateModel(
            name='Request_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('title', iki.TextField(null=True)),
                ('records', iki.ArrayField(base_field=iki.UUIDField(), null=True)),
                ('materials', iki.ArrayField(base_field=iki.UUIDField(), null=True)),
                ('tubes', iki.ArrayField(base_field=iki.UUIDField(), null=True)),
                ('needed_by', iki.DateField(null=True)),
                ('instructions', iki.TextField(null=True)),
                ('standing', iki.TextField(null=True)),
                ('status', iki.TextField(null=True)),
                ('priority', iki.TextField(null=True)),
                ('scheduled_on', iki.DateField(null=True)),
                ('analyst', iki.UUIDField(db_column='analyst_id', null=True)),
                ('service_notes', iki.TextField(null=True)),
            ],
            table='iki__requests_request__history',
        ),
        iki.AppendOnly(
            model_name='Request_History',
        ),
        iki.CreateModel(
            name='ResearchDeliverable_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('request', iki.UUIDField(db_column='request_id', null=True)),
                ('review', iki.TextField(null=True)),
            ],
            table='iki__requests_researchdeliverable__history',
        ),
        iki.AppendOnly(
            model_name='ResearchDeliverable_History',
        ),
        iki.CreateModel(
            name='ResearchRequest_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
            ],
            table='iki__requests_researchrequest__history',
        ),
        iki.AppendOnly(
            model_name='ResearchRequest_History',
        ),
        iki.CreateModel(
            name='WorkItem_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('request', iki.UUIDField(db_column='request_id', null=True)),
                ('priority', iki.TextField(null=True)),
                ('scheduled_on', iki.DateField(null=True)),
                ('notes', iki.TextField(null=True)),
                ('status', iki.TextField(null=True)),
            ],
            table='iki__requests_workitem__history',
        ),
        iki.AppendOnly(
            model_name='WorkItem_History',
        ),
        iki.CreateModel(
            name='PlainDeliverable',
            fields=[
                ('deliverable_ptr', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to='iki__requests.deliverable')),
                ('status', iki.StateField(db_comment='A state field (ADR 0125): one of recorded, delivered.', db_default='recorded', locking=['delivered'], states=['recorded', 'delivered'])),
            ],
            table='iki__requests_plaindeliverable',
            bases=('iki__requests.deliverable',),
        ),
        iki.CreateModel(
            name='QCDeliverable',
            fields=[
                ('deliverable_ptr', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to='iki__requests.deliverable')),
                ('notes', iki.TextField(null=True)),
                ('review', iki.StateField(db_comment='A state field (ADR 0125): one of recorded, submitted, reviewed, released.', db_default='recorded', locking=['submitted', 'reviewed', 'released'], states=['recorded', 'submitted', 'reviewed', 'released'])),
            ],
            table='iki__requests_qcdeliverable',
            bases=('iki__requests.deliverable',),
        ),
        iki.CreateModel(
            name='ResearchDeliverable',
            fields=[
                ('deliverable_ptr', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to='iki__requests.deliverable')),
                ('review', iki.StateField(db_comment='A state field (ADR 0125): one of recorded, signed_off.', db_default='recorded', locking=['signed_off'], states=['recorded', 'signed_off'])),
            ],
            table='iki__requests_researchdeliverable',
            bases=('iki__requests.deliverable',),
        ),
        iki.CreateModel(
            name='PlainRequest',
            fields=[
                ('request_ptr', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to='iki__requests.request')),
            ],
            table='iki__requests_plainrequest',
            bases=('iki__requests.request',),
        ),
        iki.CreateModel(
            name='QCRequest',
            fields=[
                ('request_ptr', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to='iki__requests.request')),
            ],
            table='iki__requests_qcrequest',
            bases=('iki__requests.request',),
        ),
        iki.CreateModel(
            name='Request_materials',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity', 'position')),
                ('entity', iki.ForeignKey(db_column='entity_id', on_delete=iki.CASCADE, related_name='+', to='iki__requests.request')),
                ('position', iki.PositiveIntegerField()),
            ],
            table='iki__requests_request__materials',
        ),
        iki.CreateModel(
            name='Request_records',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity', 'position')),
                ('entity', iki.ForeignKey(db_column='entity_id', on_delete=iki.CASCADE, related_name='+', to='iki__requests.request')),
                ('position', iki.PositiveIntegerField()),
            ],
            table='iki__requests_request__records',
        ),
        iki.CreateModel(
            name='Request_tubes',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity', 'position')),
                ('entity', iki.ForeignKey(db_column='entity_id', on_delete=iki.CASCADE, related_name='+', to='iki__requests.request')),
                ('position', iki.PositiveIntegerField()),
            ],
            table='iki__requests_request__tubes',
        ),
        iki.CreateModel(
            name='ResearchRequest',
            fields=[
                ('request_ptr', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to='iki__requests.request')),
            ],
            table='iki__requests_researchrequest',
            bases=('iki__requests.request',),
        ),
        iki.CreateModel(
            name='WorkItem',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('priority', iki.TextField(db_default='routine', null=True)),
                ('scheduled_on', iki.DateField(null=True)),
                ('notes', iki.TextField(null=True)),
                ('status', iki.StateField(db_comment='A state field (ADR 0125): one of queued, scheduled, running, done, cancelled.', db_default='queued', states=['queued', 'scheduled', 'running', 'done', 'cancelled'])),
                ('request', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to=iki.ENTITY)),
            ],
            table='iki__requests_workitem',
            bases=(iki.ENTITY,),
        ),
        iki.AddConstraint(
            model_name='deliverable',
            constraint=iki.CheckConstraint(condition=iki.Q(('outcome__in', ['fulfilled', 'unsuccessful'])), name='iki__requests_deliverable__outcome__values'),
        ),
        iki.AddField(
            model_name='plaindeliverable',
            name='request',
            field=iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__requests.plainrequest'),
        ),
        iki.AddField(
            model_name='qcdeliverable',
            name='request',
            field=iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__requests.qcrequest'),
        ),
        iki.AddField(
            model_name='request_materials',
            name='item',
            field=iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to=iki.ENTITY),
        ),
        iki.AddField(
            model_name='request_records',
            name='item',
            field=iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to=iki.ENTITY),
        ),
        iki.AddField(
            model_name='request_tubes',
            name='item',
            field=iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__inventory.tube'),
        ),
        iki.AddField(
            model_name='researchdeliverable',
            name='request',
            field=iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__requests.researchrequest'),
        ),
        iki.AddIndex(
            model_name='request',
            index=iki.GinIndex(iki.SearchVector(iki.Left('instructions', 100000), config='simple'), name='instructio_fts_a15e5ff8ef9db6d'),
        ),
        iki.AddIndex(
            model_name='request',
            index=iki.GinIndex(iki.SearchVector(iki.Left('service_notes', 100000), config='simple'), name='service_no_fts_6abcdd0be33b2c7'),
        ),
        iki.AddConstraint(
            model_name='request',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('title'), 1), name='iki__requests_request__title__not_empty'),
        ),
        iki.AddConstraint(
            model_name='request',
            constraint=iki.CheckConstraint(condition=iki.Q(('title__regex', '[\\r\\n]'), negated=True), name='iki__requests_request__title__one_line'),
        ),
        iki.AddConstraint(
            model_name='request',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('instructions'), 1), name='iki__requests_request__instructions__not_empty'),
        ),
        iki.AddConstraint(
            model_name='request',
            constraint=iki.CheckConstraint(condition=iki.Q(('standing__in', ['open', 'withdrawn'])), name='iki__requests_request__standing__known'),
        ),
        iki.AddConstraint(
            model_name='request',
            constraint=iki.CheckConstraint(condition=iki.Q(('status__in', ['submitted', 'accepted', 'in_progress', 'delivered', 'declined'])), name='iki__requests_request__status__known'),
        ),
        iki.AddConstraint(
            model_name='request',
            constraint=iki.CheckConstraint(condition=iki.Q(('priority__in', ['routine', 'high', 'urgent'])), name='iki__requests_request__priority__values'),
        ),
        iki.AddConstraint(
            model_name='request',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('service_notes'), 1), name='iki__requests_request__service_notes__not_empty'),
        ),
        iki.AddIndex(
            model_name='workitem',
            index=iki.GinIndex(iki.SearchVector(iki.Left('notes', 100000), config='simple'), name='notes_fts_1e3c1b1f8d4eebd8200c'),
        ),
        iki.AddConstraint(
            model_name='workitem',
            constraint=iki.CheckConstraint(condition=iki.Q(('priority__in', ['routine', 'high', 'urgent'])), name='iki__requests_workitem__priority__values'),
        ),
        iki.AddConstraint(
            model_name='workitem',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('notes'), 1), name='iki__requests_workitem__notes__not_empty'),
        ),
        iki.AddConstraint(
            model_name='workitem',
            constraint=iki.CheckConstraint(condition=iki.LessThanOrEqual(iki.Length('notes'), 10000), name='iki__requests_workitem__notes__max_length'),
        ),
        iki.AddConstraint(
            model_name='workitem',
            constraint=iki.CheckConstraint(condition=iki.Q(('status__in', ['queued', 'scheduled', 'running', 'done', 'cancelled'])), name='iki__requests_workitem__status__known'),
        ),
        iki.AddConstraint(
            model_name='plaindeliverable',
            constraint=iki.CheckConstraint(condition=iki.Q(('status__in', ['recorded', 'delivered'])), name='iki__requests_plaindeliverable__status__known'),
        ),
        iki.AddIndex(
            model_name='qcdeliverable',
            index=iki.GinIndex(iki.SearchVector(iki.Left('notes', 100000), config='simple'), name='notes_fts_df3d35fc1f7622198a56'),
        ),
        iki.AddConstraint(
            model_name='qcdeliverable',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('notes'), 1), name='iki__requests_qcdeliverable__notes__not_empty'),
        ),
        iki.AddConstraint(
            model_name='qcdeliverable',
            constraint=iki.CheckConstraint(condition=iki.Q(('review__in', ['recorded', 'submitted', 'reviewed', 'released'])), name='iki__requests_qcdeliverable__review__known'),
        ),
        iki.AddConstraint(
            model_name='request_materials',
            constraint=iki.CheckConstraint(condition=iki.Q(('position__lt', 50)), name='iki__requests_request__materials__position__max_items'),
        ),
        iki.AddConstraint(
            model_name='request_records',
            constraint=iki.CheckConstraint(condition=iki.Q(('position__lt', 50)), name='iki__requests_request__records__position__max_items'),
        ),
        iki.AddConstraint(
            model_name='request_tubes',
            constraint=iki.CheckConstraint(condition=iki.Q(('position__lt', 96)), name='iki__requests_request__tubes__position__max_items'),
        ),
        iki.AddConstraint(
            model_name='researchdeliverable',
            constraint=iki.CheckConstraint(condition=iki.Q(('review__in', ['recorded', 'signed_off'])), name='iki__requests_researchdeliverable__review__known'),
        ),
    ]
