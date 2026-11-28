# Made by `iki plugin makemigrations`. Review it, and commit it with the manifests it
# migrates: once a deployment has applied it, it never changes.

from iki_server.plugins import operations as iki


class Migration(iki.Migration):

    initial = True

    dependencies = [
        ('iki__sequencing', '0001_initial'),
        ('iki_anchors', '0001_iki_0_1'),
    ]

    operations = [
        iki.CreateModel(
            name='SequencingAnalysisResult_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('library', iki.UUIDField(db_column='library_id', null=True)),
                ('sample', iki.UUIDField(db_column='sample_id', null=True)),
                ('run', iki.UUIDField(db_column='run_id', null=True)),
                ('material', iki.UUIDField(db_column='material_id', null=True)),
                ('report', iki.UUIDField(db_column='report_id', null=True)),
                ('report_sha256', iki.TextField(null=True)),
                ('pipeline_context', iki.JSONField(null=True)),
                ('format', iki.TextField(null=True)),
                ('summary', iki.JSONField(null=True)),
                ('review_flow', iki.TextField(null=True)),
                ('notes', iki.TextField(null=True)),
                ('review', iki.TextField(null=True)),
            ],
            table='iki__sequencing_analysis_sequencinganalysisresult__history',
        ),
        iki.AppendOnly(
            model_name='SequencingAnalysisResult_History',
        ),
        iki.CreateModel(
            name='SequencingAnalysisResult',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('report_sha256', iki.CharField(max_length=64, null=True)),
                ('pipeline_context', iki.JSONField(null=True)),
                ('format', iki.TextField(null=True)),
                ('summary', iki.JSONField(null=True)),
                ('review_flow', iki.TextField(null=True)),
                ('notes', iki.TextField(null=True)),
                ('review', iki.StateField(db_comment='A state field (ADR 0125): one of recorded, signed_off, submitted, reviewed, released.', db_default='recorded', locking=['signed_off', 'submitted', 'reviewed', 'released'], states=['recorded', 'signed_off', 'submitted', 'reviewed', 'released'])),
                ('library', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__sequencing.sequencinglibrary')),
                ('material', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to=iki.ENTITY)),
                ('report', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to=iki.FILE)),
                ('run', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to='iki__sequencing.sequencingrun')),
                ('sample', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to=iki.ENTITY)),
            ],
            table='iki__sequencing_analysis_sequencinganalysisresult',
            indexes=[
                iki.GinIndex(iki.SearchVector(iki.Left('notes', 100000), config='simple'), name='notes_fts_fc6a61279a7e153d9f95'),
            ],
            constraints=[
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('report_sha256'), 1), name='iki__sequencing_analysis_sequencinganalysisresult____aac99f093a'),
                iki.CheckConstraint(condition=iki.Q(('report_sha256__regex', '[\\r\\n]'), negated=True), name='iki__sequencing_analysis_sequencinganalysisresult____8ba4918643'),
                iki.CheckConstraint(condition=iki.Q(('format__in', ['vcf', 'counts', 'airr'])), name='iki__sequencing_analysis_sequencinganalysisresult____bc7195bf18'),
                iki.CheckConstraint(condition=iki.Q(('review_flow__in', ['research', 'regulated_qc', 'record_only'])), name='iki__sequencing_analysis_sequencinganalysisresult____084b31c085'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('notes'), 1), name='iki__sequencing_analysis_sequencinganalysisresult____daa3279823'),
                iki.CheckConstraint(condition=iki.LessThanOrEqual(iki.Length('notes'), 3000), name='iki__sequencing_analysis_sequencinganalysisresult____f70b1b53af'),
                iki.CheckConstraint(condition=iki.Q(('review__in', ['recorded', 'signed_off', 'submitted', 'reviewed', 'released'])), name='iki__sequencing_analysis_sequencinganalysisresult____4f900cce64'),
            ],
            bases=(iki.ENTITY,),
        ),
    ]
