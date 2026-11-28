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
            name='CertificateOfAnalysis_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('coa', iki.JSONField(null=True)),
                ('digest', iki.TextField(null=True)),
                ('review_flow', iki.TextField(null=True)),
                ('review', iki.TextField(null=True)),
            ],
            table='iki__qc_release_certificateofanalysis__history',
        ),
        iki.AppendOnly(
            model_name='CertificateOfAnalysis_History',
        ),
        iki.CreateModel(
            name='OOSInvestigation_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('case', iki.UUIDField(db_column='case_id', null=True)),
                ('test', iki.UUIDField(db_column='test_id', null=True)),
                ('resolution', iki.TextField(null=True)),
                ('plan', iki.TextField(null=True)),
                ('findings', iki.TextField(null=True)),
                ('evidence', iki.UUIDField(db_column='evidence_id', null=True)),
            ],
            table='iki__qc_release_oosinvestigation__history',
        ),
        iki.AppendOnly(
            model_name='OOSInvestigation_History',
        ),
        iki.CreateModel(
            name='QCCertificate_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('review', iki.TextField(null=True)),
            ],
            table='iki__qc_release_qccertificate__history',
        ),
        iki.AppendOnly(
            model_name='QCCertificate_History',
        ),
        iki.CreateModel(
            name='QCOOSInvestigation_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('investigator', iki.UUIDField(db_column='investigator_id', null=True)),
                ('review', iki.TextField(null=True)),
            ],
            table='iki__qc_release_qcoosinvestigation__history',
        ),
        iki.AppendOnly(
            model_name='QCOOSInvestigation_History',
        ),
        iki.CreateModel(
            name='ReleaseCase',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('lot_revision', iki.BigIntegerField(null=True)),
                ('lot_number', iki.CharField(max_length=100, null=True)),
                ('schedule_revision', iki.BigIntegerField(null=True)),
                ('context', iki.CharField(max_length=120, null=True)),
                ('grade', iki.CharField(max_length=120, null=True)),
                ('cycle', iki.BigIntegerField(null=True)),
                ('reason', iki.TextField(null=True)),
                ('superseded', iki.BooleanField(null=True)),
                ('specification_revision', iki.BigIntegerField(null=True)),
                ('version', iki.CharField(max_length=100, null=True)),
                ('testing_on', iki.DateField(null=True)),
                ('review_flow', iki.TextField(null=True)),
                ('attempts', iki.JSONField(null=True)),
                ('sealed', iki.BooleanField(null=True)),
                ('lot', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to=iki.ENTITY)),
                ('material', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to=iki.ENTITY)),
                ('previous', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to='iki__qc_release.releasecase')),
            ],
            table='iki__qc_release_releasecase',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='ReleaseCase_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('lot', iki.UUIDField(db_column='lot_id', null=True)),
                ('lot_revision', iki.BigIntegerField(null=True)),
                ('lot_number', iki.TextField(null=True)),
                ('material', iki.UUIDField(db_column='material_id', null=True)),
                ('schedule', iki.UUIDField(db_column='schedule_id', null=True)),
                ('schedule_revision', iki.BigIntegerField(null=True)),
                ('context', iki.TextField(null=True)),
                ('grade', iki.TextField(null=True)),
                ('cycle', iki.BigIntegerField(null=True)),
                ('previous', iki.UUIDField(db_column='previous_id', null=True)),
                ('reason', iki.TextField(null=True)),
                ('superseded', iki.BooleanField(null=True)),
                ('specification', iki.UUIDField(db_column='specification_id', null=True)),
                ('specification_revision', iki.BigIntegerField(null=True)),
                ('version', iki.TextField(null=True)),
                ('testing_on', iki.DateField(null=True)),
                ('review_flow', iki.TextField(null=True)),
                ('attempts', iki.JSONField(null=True)),
                ('sealed', iki.BooleanField(null=True)),
            ],
            table='iki__qc_release_releasecase__history',
        ),
        iki.AppendOnly(
            model_name='ReleaseCase_History',
        ),
        iki.CreateModel(
            name='ReleaseCertificate_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('case', iki.UUIDField(db_column='case_id', null=True)),
                ('lot', iki.UUIDField(db_column='lot_id', null=True)),
                ('specification', iki.UUIDField(db_column='specification_id', null=True)),
                ('specification_revision', iki.BigIntegerField(null=True)),
                ('version', iki.TextField(null=True)),
                ('testing_on', iki.DateField(null=True)),
                ('review_flow', iki.TextField(null=True)),
                ('report', iki.JSONField(null=True)),
                ('coa', iki.JSONField(null=True)),
                ('coa_digest', iki.TextField(null=True)),
                ('analysts', iki.ArrayField(base_field=iki.UUIDField(), null=True)),
                ('notes', iki.TextField(null=True)),
            ],
            table='iki__qc_release_releasecertificate__history',
        ),
        iki.AppendOnly(
            model_name='ReleaseCertificate_History',
        ),
        iki.CreateModel(
            name='ReleaseTest_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('case', iki.UUIDField(db_column='case_id', null=True)),
                ('lot', iki.UUIDField(db_column='lot_id', null=True)),
                ('sequence', iki.BigIntegerField(null=True)),
                ('attribute', iki.TextField(null=True)),
                ('method', iki.TextField(null=True)),
                ('kind', iki.TextField(null=True)),
                ('unit', iki.TextField(null=True)),
                ('value', iki.FloatField(null=True)),
                ('reported', iki.TextField(null=True)),
                ('rounding', iki.TextField(null=True)),
                ('lower', iki.TextField(null=True)),
                ('upper', iki.TextField(null=True)),
                ('acceptance', iki.TextField(null=True)),
                ('result', iki.TextField(null=True)),
                ('observation', iki.TextField(null=True)),
                ('outcome', iki.TextField(null=True)),
                ('observed_by', iki.UUIDField(db_column='observed_by_id', null=True)),
                ('repeat_of', iki.UUIDField(db_column='repeat_of_id', null=True)),
                ('purpose', iki.TextField(null=True)),
                ('investigation', iki.UUIDField(db_column='investigation_id', null=True)),
                ('investigation_revision', iki.BigIntegerField(null=True)),
                ('evidence', iki.UUIDField(db_column='evidence_id', null=True)),
                ('evidence_revision', iki.BigIntegerField(null=True)),
                ('lab_report', iki.UUIDField(db_column='lab_report_id', null=True)),
            ],
            table='iki__qc_release_releasetest__history',
        ),
        iki.AppendOnly(
            model_name='ReleaseTest_History',
        ),
        iki.CreateModel(
            name='ResearchCertificate_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('review', iki.TextField(null=True)),
            ],
            table='iki__qc_release_researchcertificate__history',
        ),
        iki.AppendOnly(
            model_name='ResearchCertificate_History',
        ),
        iki.CreateModel(
            name='ResearchOOSInvestigation_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('investigator', iki.UUIDField(db_column='investigator_id', null=True)),
                ('review', iki.TextField(null=True)),
            ],
            table='iki__qc_release_researchoosinvestigation__history',
        ),
        iki.AppendOnly(
            model_name='ResearchOOSInvestigation_History',
        ),
        iki.CreateModel(
            name='SpecificationSchedule',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('context', iki.CharField(db_default='Release', max_length=120)),
                ('grade', iki.CharField(max_length=120, null=True)),
                ('tested_through', iki.DateField(null=True)),
                ('material', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to=iki.ENTITY)),
            ],
            table='iki__qc_release_specificationschedule',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='SpecificationSchedule_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('material', iki.UUIDField(db_column='material_id', null=True)),
                ('context', iki.TextField(null=True)),
                ('grade', iki.TextField(null=True)),
                ('versions', iki.ArrayField(base_field=iki.UUIDField(), null=True)),
                ('tested_through', iki.DateField(null=True)),
            ],
            table='iki__qc_release_specificationschedule__history',
        ),
        iki.AppendOnly(
            model_name='SpecificationSchedule_History',
        ),
        iki.CreateModel(
            name='SpecificationVersion',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('version', iki.CharField(max_length=100)),
                ('effective_from', iki.DateField()),
                ('source', iki.CharField(max_length=1000)),
                ('approval', iki.StateField(db_comment='A state field (ADR 0125): one of draft, approved.', db_default='draft', locking=['approved'], states=['draft', 'approved'])),
                ('schedule', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__qc_release.specificationschedule')),
            ],
            table='iki__qc_release_specificationversion',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='SpecificationVersion_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('schedule', iki.UUIDField(db_column='schedule_id', null=True)),
                ('version', iki.TextField(null=True)),
                ('effective_from', iki.DateField(null=True)),
                ('source', iki.TextField(null=True)),
                ('criteria', iki.JSONField(null=True)),
                ('approval', iki.TextField(null=True)),
            ],
            table='iki__qc_release_specificationversion__history',
        ),
        iki.AppendOnly(
            model_name='SpecificationVersion_History',
        ),
        iki.CreateModel(
            name='CertificateOfAnalysis',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('coa', iki.JSONField(null=True)),
                ('digest', iki.CharField(db_index=True, max_length=64, null=True)),
                ('review_flow', iki.TextField(null=True)),
                ('review', iki.StateField(db_comment='A state field (ADR 0125): one of recorded, issued, released.', db_default='recorded', locking=['issued', 'released'], states=['recorded', 'issued', 'released'])),
            ],
            table='iki__qc_release_certificateofanalysis',
            indexes=[
                iki.GinIndex(fields=['digest'], name='digest_trgm_a1bc6551339f564939', opclasses=['gin_trgm_ops']),
            ],
            constraints=[
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('digest'), 1), name='iki__qc_release_certificateofanalysis__digest__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('digest__regex', '[\\r\\n]'), negated=True), name='iki__qc_release_certificateofanalysis__digest__one_line'),
                iki.CheckConstraint(condition=iki.Q(('review_flow__in', ['research', 'regulated_qc'])), name='iki__qc_release_certificateofanalysis__review_flow__values'),
                iki.CheckConstraint(condition=iki.Q(('review__in', ['recorded', 'issued', 'released'])), name='iki__qc_release_certificateofanalysis__review__known'),
            ],
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='ReleaseTest',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('sequence', iki.BigIntegerField(null=True)),
                ('attribute', iki.CharField(db_index=True, max_length=120, null=True)),
                ('method', iki.CharField(max_length=200, null=True)),
                ('kind', iki.TextField(null=True)),
                ('unit', iki.CharField(max_length=50, null=True)),
                ('value', iki.FloatField(null=True)),
                ('reported', iki.CharField(max_length=400, null=True)),
                ('rounding', iki.TextField(null=True)),
                ('lower', iki.CharField(max_length=17, null=True)),
                ('upper', iki.CharField(max_length=17, null=True)),
                ('acceptance', iki.CharField(max_length=500, null=True)),
                ('result', iki.TextField(null=True)),
                ('observation', iki.TextField(null=True)),
                ('outcome', iki.TextField(db_index=True, null=True)),
                ('purpose', iki.TextField(null=True)),
                ('investigation_revision', iki.BigIntegerField(null=True)),
                ('evidence_revision', iki.BigIntegerField(null=True)),
                ('case', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to='iki__qc_release.releasecase')),
                ('evidence', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to=iki.ENTITY)),
                ('investigation', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to=iki.ENTITY)),
                ('lab_report', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to=iki.FILE)),
                ('lot', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to=iki.ENTITY)),
                ('observed_by', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to=iki.USER)),
                ('repeat_of', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to='iki__qc_release.releasetest')),
            ],
            table='iki__qc_release_releasetest',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='OOSInvestigation',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('resolution', iki.TextField(null=True)),
                ('plan', iki.TextField(null=True)),
                ('findings', iki.TextField(null=True)),
                ('evidence', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to=iki.FILE)),
                ('case', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to='iki__qc_release.releasecase')),
                ('test', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to='iki__qc_release.releasetest')),
            ],
            table='iki__qc_release_oosinvestigation',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='SpecificationSchedule_versions',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity', 'position')),
                ('entity', iki.ForeignKey(db_column='entity_id', on_delete=iki.CASCADE, related_name='+', to='iki__qc_release.specificationschedule')),
                ('position', iki.PositiveIntegerField()),
            ],
            table='iki__qc_release_specificationschedule__versions',
        ),
        iki.AddField(
            model_name='releasecase',
            name='schedule',
            field=iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to='iki__qc_release.specificationschedule'),
        ),
        iki.CreateModel(
            name='SpecificationVersion_criteria',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity', 'position')),
                ('entity', iki.ForeignKey(db_column='entity_id', on_delete=iki.CASCADE, related_name='+', to='iki__qc_release.specificationversion')),
                ('position', iki.PositiveIntegerField()),
                ('attribute', iki.CharField(max_length=120)),
                ('procedure', iki.CharField(max_length=200, null=True)),
                ('procedure_revision', iki.CharField(max_length=80, null=True)),
                ('method', iki.CharField(max_length=200)),
                ('kind', iki.TextField(db_default='numeric')),
                ('unit', iki.CharField(max_length=50, null=True)),
                ('lower', iki.CharField(max_length=17, null=True)),
                ('upper', iki.CharField(max_length=17, null=True)),
                ('rounding', iki.TextField(db_default='limit_places', null=True)),
                ('acceptance', iki.CharField(max_length=500, null=True)),
                ('source', iki.CharField(max_length=1000, null=True)),
            ],
            table='iki__qc_release_specificationversion__criteria',
        ),
        iki.CreateModel(
            name='ReleaseCertificate',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('specification_revision', iki.BigIntegerField(null=True)),
                ('version', iki.CharField(max_length=100, null=True)),
                ('testing_on', iki.DateField(null=True)),
                ('review_flow', iki.TextField(null=True)),
                ('report', iki.JSONField(null=True)),
                ('coa', iki.JSONField(null=True)),
                ('coa_digest', iki.CharField(db_index=True, max_length=64, null=True)),
                ('notes', iki.TextField(null=True)),
                ('lot', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to=iki.ENTITY)),
                ('case', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to='iki__qc_release.releasecase')),
                ('specification', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to='iki__qc_release.specificationversion')),
            ],
            table='iki__qc_release_releasecertificate',
            bases=(iki.ENTITY,),
        ),
        iki.AddField(
            model_name='releasecase',
            name='specification',
            field=iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to='iki__qc_release.specificationversion'),
        ),
        iki.CreateModel(
            name='QCOOSInvestigation',
            fields=[
                ('oosinvestigation_ptr', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to='iki__qc_release.oosinvestigation')),
                ('review', iki.StateField(db_comment='A state field (ADR 0125): one of recorded, submitted, reviewed, released.', db_default='recorded', locking=['submitted', 'reviewed', 'released'], states=['recorded', 'submitted', 'reviewed', 'released'])),
                ('investigator', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to=iki.USER)),
            ],
            table='iki__qc_release_qcoosinvestigation',
            bases=('iki__qc_release.oosinvestigation',),
        ),
        iki.CreateModel(
            name='ResearchOOSInvestigation',
            fields=[
                ('oosinvestigation_ptr', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to='iki__qc_release.oosinvestigation')),
                ('review', iki.StateField(db_comment='A state field (ADR 0125): one of recorded, signed_off.', db_default='recorded', locking=['signed_off'], states=['recorded', 'signed_off'])),
                ('investigator', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to=iki.USER)),
            ],
            table='iki__qc_release_researchoosinvestigation',
            bases=('iki__qc_release.oosinvestigation',),
        ),
        iki.CreateModel(
            name='QCCertificate',
            fields=[
                ('releasecertificate_ptr', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to='iki__qc_release.releasecertificate')),
                ('review', iki.StateField(db_comment='A state field (ADR 0125): one of recorded, submitted, reviewed, released.', db_default='recorded', locking=['submitted', 'reviewed', 'released'], states=['recorded', 'submitted', 'reviewed', 'released'])),
            ],
            table='iki__qc_release_qccertificate',
            constraints=[
                iki.CheckConstraint(condition=iki.Q(('review__in', ['recorded', 'submitted', 'reviewed', 'released'])), name='iki__qc_release_qccertificate__review__known'),
            ],
            bases=('iki__qc_release.releasecertificate',),
        ),
        iki.CreateModel(
            name='ReleaseCertificate_analysts',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity', 'position')),
                ('entity', iki.ForeignKey(db_column='entity_id', on_delete=iki.CASCADE, related_name='+', to='iki__qc_release.releasecertificate')),
                ('position', iki.PositiveIntegerField()),
                ('item', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to=iki.USER)),
            ],
            table='iki__qc_release_releasecertificate__analysts',
        ),
        iki.CreateModel(
            name='ResearchCertificate',
            fields=[
                ('releasecertificate_ptr', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to='iki__qc_release.releasecertificate')),
                ('review', iki.StateField(db_comment='A state field (ADR 0125): one of recorded, signed_off.', db_default='recorded', locking=['signed_off'], states=['recorded', 'signed_off'])),
            ],
            table='iki__qc_release_researchcertificate',
            constraints=[
                iki.CheckConstraint(condition=iki.Q(('review__in', ['recorded', 'signed_off'])), name='iki__qc_release_researchcertificate__review__known'),
            ],
            bases=('iki__qc_release.releasecertificate',),
        ),
        iki.AddIndex(
            model_name='releasetest',
            index=iki.GinIndex(fields=['attribute'], name='attribute_trgm_c645b8f240a9234', opclasses=['gin_trgm_ops']),
        ),
        iki.AddIndex(
            model_name='releasetest',
            index=iki.GinIndex(iki.SearchVector(iki.Left('observation', 100000), config='simple'), name='observatio_fts_180411d5ceb23ac'),
        ),
        iki.AddConstraint(
            model_name='releasetest',
            constraint=iki.CheckConstraint(condition=iki.Q(('sequence__gte', 1)), name='iki__qc_release_releasetest__sequence__min'),
        ),
        iki.AddConstraint(
            model_name='releasetest',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('attribute'), 1), name='iki__qc_release_releasetest__attribute__not_empty'),
        ),
        iki.AddConstraint(
            model_name='releasetest',
            constraint=iki.CheckConstraint(condition=iki.Q(('attribute__regex', '[\\r\\n]'), negated=True), name='iki__qc_release_releasetest__attribute__one_line'),
        ),
        iki.AddConstraint(
            model_name='releasetest',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('method'), 1), name='iki__qc_release_releasetest__method__not_empty'),
        ),
        iki.AddConstraint(
            model_name='releasetest',
            constraint=iki.CheckConstraint(condition=iki.Q(('method__regex', '[\\r\\n]'), negated=True), name='iki__qc_release_releasetest__method__one_line'),
        ),
        iki.AddConstraint(
            model_name='releasetest',
            constraint=iki.CheckConstraint(condition=iki.Q(('kind__in', ['numeric', 'conforms'])), name='iki__qc_release_releasetest__kind__values'),
        ),
        iki.AddConstraint(
            model_name='releasetest',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('unit'), 1), name='iki__qc_release_releasetest__unit__not_empty'),
        ),
        iki.AddConstraint(
            model_name='releasetest',
            constraint=iki.CheckConstraint(condition=iki.Q(('unit__regex', '[\\r\\n]'), negated=True), name='iki__qc_release_releasetest__unit__one_line'),
        ),
        iki.AddConstraint(
            model_name='releasetest',
            constraint=iki.CheckConstraint(condition=iki.Q(('value__gt', float('-inf')), ('value__lt', float('inf'))), name='iki__qc_release_releasetest__value__finite'),
        ),
        iki.AddConstraint(
            model_name='releasetest',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('reported'), 1), name='iki__qc_release_releasetest__reported__not_empty'),
        ),
        iki.AddConstraint(
            model_name='releasetest',
            constraint=iki.CheckConstraint(condition=iki.Q(('reported__regex', '[\\r\\n]'), negated=True), name='iki__qc_release_releasetest__reported__one_line'),
        ),
        iki.AddConstraint(
            model_name='releasetest',
            constraint=iki.CheckConstraint(condition=iki.Q(('rounding__in', ['limit_places', 'unrounded'])), name='iki__qc_release_releasetest__rounding__values'),
        ),
        iki.AddConstraint(
            model_name='releasetest',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('lower'), 1), name='iki__qc_release_releasetest__lower__not_empty'),
        ),
        iki.AddConstraint(
            model_name='releasetest',
            constraint=iki.CheckConstraint(condition=iki.Q(('lower__regex', '[\\r\\n]'), negated=True), name='iki__qc_release_releasetest__lower__one_line'),
        ),
        iki.AddConstraint(
            model_name='releasetest',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('upper'), 1), name='iki__qc_release_releasetest__upper__not_empty'),
        ),
        iki.AddConstraint(
            model_name='releasetest',
            constraint=iki.CheckConstraint(condition=iki.Q(('upper__regex', '[\\r\\n]'), negated=True), name='iki__qc_release_releasetest__upper__one_line'),
        ),
        iki.AddConstraint(
            model_name='releasetest',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('acceptance'), 1), name='iki__qc_release_releasetest__acceptance__not_empty'),
        ),
        iki.AddConstraint(
            model_name='releasetest',
            constraint=iki.CheckConstraint(condition=iki.Q(('acceptance__regex', '[\\r\\n]'), negated=True), name='iki__qc_release_releasetest__acceptance__one_line'),
        ),
        iki.AddConstraint(
            model_name='releasetest',
            constraint=iki.CheckConstraint(condition=iki.Q(('result__in', ['conforms', 'does_not_conform'])), name='iki__qc_release_releasetest__result__values'),
        ),
        iki.AddConstraint(
            model_name='releasetest',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('observation'), 1), name='iki__qc_release_releasetest__observation__not_empty'),
        ),
        iki.AddConstraint(
            model_name='releasetest',
            constraint=iki.CheckConstraint(condition=iki.LessThanOrEqual(iki.Length('observation'), 2000), name='iki__qc_release_releasetest__observation__max_length'),
        ),
        iki.AddConstraint(
            model_name='releasetest',
            constraint=iki.CheckConstraint(condition=iki.Q(('outcome__in', ['pass', 'oos'])), name='iki__qc_release_releasetest__outcome__values'),
        ),
        iki.AddConstraint(
            model_name='releasetest',
            constraint=iki.CheckConstraint(condition=iki.Q(('purpose__in', ['reportable', 'investigational'])), name='iki__qc_release_releasetest__purpose__values'),
        ),
        iki.AddConstraint(
            model_name='releasetest',
            constraint=iki.CheckConstraint(condition=iki.Q(('investigation_revision__gte', 1)), name='iki__qc_release_releasetest__investigation_revision__min'),
        ),
        iki.AddConstraint(
            model_name='releasetest',
            constraint=iki.CheckConstraint(condition=iki.Q(('evidence_revision__gte', 1)), name='iki__qc_release_releasetest__evidence_revision__min'),
        ),
        iki.AddIndex(
            model_name='oosinvestigation',
            index=iki.GinIndex(iki.SearchVector(iki.Left('plan', 100000), config='simple'), name='plan_fts_ddba769a7ae5cd305e3db'),
        ),
        iki.AddIndex(
            model_name='oosinvestigation',
            index=iki.GinIndex(iki.SearchVector(iki.Left('findings', 100000), config='simple'), name='findings_fts_c1bc28b7822819115'),
        ),
        iki.AddConstraint(
            model_name='oosinvestigation',
            constraint=iki.CheckConstraint(condition=iki.Q(('resolution__in', ['confirmed', 'invalidated', 'inconclusive'])), name='iki__qc_release_oosinvestigation__resolution__values'),
        ),
        iki.AddConstraint(
            model_name='oosinvestigation',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('plan'), 1), name='iki__qc_release_oosinvestigation__plan__not_empty'),
        ),
        iki.AddConstraint(
            model_name='oosinvestigation',
            constraint=iki.CheckConstraint(condition=iki.LessThanOrEqual(iki.Length('plan'), 4000), name='iki__qc_release_oosinvestigation__plan__max_length'),
        ),
        iki.AddConstraint(
            model_name='oosinvestigation',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('findings'), 1), name='iki__qc_release_oosinvestigation__findings__not_empty'),
        ),
        iki.AddConstraint(
            model_name='oosinvestigation',
            constraint=iki.CheckConstraint(condition=iki.LessThanOrEqual(iki.Length('findings'), 12000), name='iki__qc_release_oosinvestigation__findings__max_length'),
        ),
        iki.AddField(
            model_name='specificationschedule_versions',
            name='item',
            field=iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__qc_release.specificationversion'),
        ),
        iki.AddConstraint(
            model_name='specificationschedule',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('context'), 1), name='iki__qc_release_specificationschedule__context__not_empty'),
        ),
        iki.AddConstraint(
            model_name='specificationschedule',
            constraint=iki.CheckConstraint(condition=iki.Q(('context__regex', '[\\r\\n]'), negated=True), name='iki__qc_release_specificationschedule__context__one_line'),
        ),
        iki.AddConstraint(
            model_name='specificationschedule',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('grade'), 1), name='iki__qc_release_specificationschedule__grade__not_empty'),
        ),
        iki.AddConstraint(
            model_name='specificationschedule',
            constraint=iki.CheckConstraint(condition=iki.Q(('grade__regex', '[\\r\\n]'), negated=True), name='iki__qc_release_specificationschedule__grade__one_line'),
        ),
        iki.AddConstraint(
            model_name='specificationversion_criteria',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('attribute'), 1), name='iki__qc_release_specificationversion__criteria__att__bbe427a513'),
        ),
        iki.AddConstraint(
            model_name='specificationversion_criteria',
            constraint=iki.CheckConstraint(condition=iki.Q(('attribute__regex', '[\\r\\n]'), negated=True), name='iki__qc_release_specificationversion__criteria__att__3294b3a9a3'),
        ),
        iki.AddConstraint(
            model_name='specificationversion_criteria',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('procedure'), 1), name='iki__qc_release_specificationversion__criteria__pro__18a3aaa710'),
        ),
        iki.AddConstraint(
            model_name='specificationversion_criteria',
            constraint=iki.CheckConstraint(condition=iki.Q(('procedure__regex', '[\\r\\n]'), negated=True), name='iki__qc_release_specificationversion__criteria__pro__fe7c8872d3'),
        ),
        iki.AddConstraint(
            model_name='specificationversion_criteria',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('procedure_revision'), 1), name='iki__qc_release_specificationversion__criteria__pro__4482f09dab'),
        ),
        iki.AddConstraint(
            model_name='specificationversion_criteria',
            constraint=iki.CheckConstraint(condition=iki.Q(('procedure_revision__regex', '[\\r\\n]'), negated=True), name='iki__qc_release_specificationversion__criteria__pro__e801e65ea8'),
        ),
        iki.AddConstraint(
            model_name='specificationversion_criteria',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('method'), 1), name='iki__qc_release_specificationversion__criteria__met__534c035a18'),
        ),
        iki.AddConstraint(
            model_name='specificationversion_criteria',
            constraint=iki.CheckConstraint(condition=iki.Q(('method__regex', '[\\r\\n]'), negated=True), name='iki__qc_release_specificationversion__criteria__met__2a55ff2ce9'),
        ),
        iki.AddConstraint(
            model_name='specificationversion_criteria',
            constraint=iki.CheckConstraint(condition=iki.Q(('kind__in', ['numeric', 'conforms'])), name='iki__qc_release_specificationversion__criteria__kind__values'),
        ),
        iki.AddConstraint(
            model_name='specificationversion_criteria',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('unit'), 1), name='iki__qc_release_specificationversion__criteria__unit__not_empty'),
        ),
        iki.AddConstraint(
            model_name='specificationversion_criteria',
            constraint=iki.CheckConstraint(condition=iki.Q(('unit__regex', '[\\r\\n]'), negated=True), name='iki__qc_release_specificationversion__criteria__unit__one_line'),
        ),
        iki.AddConstraint(
            model_name='specificationversion_criteria',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('lower'), 1), name='iki__qc_release_specificationversion__criteria__low__f9c90d63b5'),
        ),
        iki.AddConstraint(
            model_name='specificationversion_criteria',
            constraint=iki.CheckConstraint(condition=iki.Q(('lower__regex', '[\\r\\n]'), negated=True), name='iki__qc_release_specificationversion__criteria__lower__one_line'),
        ),
        iki.AddConstraint(
            model_name='specificationversion_criteria',
            constraint=iki.CheckConstraint(condition=iki.Q(('lower__regex', '^(?:^-?(0|[1-9][0-9]{0,8})([.][0-9]{1,6})?$)$')), name='iki__qc_release_specificationversion__criteria__lower__pattern'),
        ),
        iki.AddConstraint(
            model_name='specificationversion_criteria',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('upper'), 1), name='iki__qc_release_specificationversion__criteria__upp__578a579fba'),
        ),
        iki.AddConstraint(
            model_name='specificationversion_criteria',
            constraint=iki.CheckConstraint(condition=iki.Q(('upper__regex', '[\\r\\n]'), negated=True), name='iki__qc_release_specificationversion__criteria__upper__one_line'),
        ),
        iki.AddConstraint(
            model_name='specificationversion_criteria',
            constraint=iki.CheckConstraint(condition=iki.Q(('upper__regex', '^(?:^-?(0|[1-9][0-9]{0,8})([.][0-9]{1,6})?$)$')), name='iki__qc_release_specificationversion__criteria__upper__pattern'),
        ),
        iki.AddConstraint(
            model_name='specificationversion_criteria',
            constraint=iki.CheckConstraint(condition=iki.Q(('rounding__in', ['limit_places', 'unrounded'])), name='iki__qc_release_specificationversion__criteria__rou__bd375fe09b'),
        ),
        iki.AddConstraint(
            model_name='specificationversion_criteria',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('acceptance'), 1), name='iki__qc_release_specificationversion__criteria__acc__1f39a203c3'),
        ),
        iki.AddConstraint(
            model_name='specificationversion_criteria',
            constraint=iki.CheckConstraint(condition=iki.Q(('acceptance__regex', '[\\r\\n]'), negated=True), name='iki__qc_release_specificationversion__criteria__acc__f591102369'),
        ),
        iki.AddConstraint(
            model_name='specificationversion_criteria',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('source'), 1), name='iki__qc_release_specificationversion__criteria__sou__c94bb31012'),
        ),
        iki.AddConstraint(
            model_name='specificationversion_criteria',
            constraint=iki.CheckConstraint(condition=iki.Q(('source__regex', '[\\r\\n]'), negated=True), name='iki__qc_release_specificationversion__criteria__sou__05cf17bee3'),
        ),
        iki.AddConstraint(
            model_name='specificationversion_criteria',
            constraint=iki.CheckConstraint(condition=iki.Q(('position__lt', 50)), name='iki__qc_release_specificationversion__criteria__pos__73c7af505a'),
        ),
        iki.AddConstraint(
            model_name='specificationversion',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('version'), 1), name='iki__qc_release_specificationversion__version__not_empty'),
        ),
        iki.AddConstraint(
            model_name='specificationversion',
            constraint=iki.CheckConstraint(condition=iki.Q(('version__regex', '[\\r\\n]'), negated=True), name='iki__qc_release_specificationversion__version__one_line'),
        ),
        iki.AddConstraint(
            model_name='specificationversion',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('source'), 1), name='iki__qc_release_specificationversion__source__not_empty'),
        ),
        iki.AddConstraint(
            model_name='specificationversion',
            constraint=iki.CheckConstraint(condition=iki.Q(('source__regex', '[\\r\\n]'), negated=True), name='iki__qc_release_specificationversion__source__one_line'),
        ),
        iki.AddConstraint(
            model_name='specificationversion',
            constraint=iki.CheckConstraint(condition=iki.Q(('approval__in', ['draft', 'approved'])), name='iki__qc_release_specificationversion__approval__known'),
        ),
        iki.AddIndex(
            model_name='releasecertificate',
            index=iki.GinIndex(fields=['coa_digest'], name='coa_digest_trgm_69218a15baffb0', opclasses=['gin_trgm_ops']),
        ),
        iki.AddIndex(
            model_name='releasecertificate',
            index=iki.GinIndex(iki.SearchVector(iki.Left('notes', 100000), config='simple'), name='notes_fts_3d925196352d8ad4e496'),
        ),
        iki.AddConstraint(
            model_name='releasecertificate',
            constraint=iki.CheckConstraint(condition=iki.Q(('specification_revision__gte', 1)), name='iki__qc_release_releasecertificate__specification_revision__min'),
        ),
        iki.AddConstraint(
            model_name='releasecertificate',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('version'), 1), name='iki__qc_release_releasecertificate__version__not_empty'),
        ),
        iki.AddConstraint(
            model_name='releasecertificate',
            constraint=iki.CheckConstraint(condition=iki.Q(('version__regex', '[\\r\\n]'), negated=True), name='iki__qc_release_releasecertificate__version__one_line'),
        ),
        iki.AddConstraint(
            model_name='releasecertificate',
            constraint=iki.CheckConstraint(condition=iki.Q(('review_flow__in', ['research', 'regulated_qc'])), name='iki__qc_release_releasecertificate__review_flow__values'),
        ),
        iki.AddConstraint(
            model_name='releasecertificate',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('coa_digest'), 1), name='iki__qc_release_releasecertificate__coa_digest__not_empty'),
        ),
        iki.AddConstraint(
            model_name='releasecertificate',
            constraint=iki.CheckConstraint(condition=iki.Q(('coa_digest__regex', '[\\r\\n]'), negated=True), name='iki__qc_release_releasecertificate__coa_digest__one_line'),
        ),
        iki.AddConstraint(
            model_name='releasecertificate',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('notes'), 1), name='iki__qc_release_releasecertificate__notes__not_empty'),
        ),
        iki.AddConstraint(
            model_name='releasecertificate',
            constraint=iki.CheckConstraint(condition=iki.LessThanOrEqual(iki.Length('notes'), 10000), name='iki__qc_release_releasecertificate__notes__max_length'),
        ),
        iki.AddIndex(
            model_name='releasecase',
            index=iki.GinIndex(iki.SearchVector(iki.Left('reason', 100000), config='simple'), name='reason_fts_6e24d43bc1ab3b12d99'),
        ),
        iki.AddConstraint(
            model_name='releasecase',
            constraint=iki.CheckConstraint(condition=iki.Q(('lot_revision__gte', 1)), name='iki__qc_release_releasecase__lot_revision__min'),
        ),
        iki.AddConstraint(
            model_name='releasecase',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('lot_number'), 1), name='iki__qc_release_releasecase__lot_number__not_empty'),
        ),
        iki.AddConstraint(
            model_name='releasecase',
            constraint=iki.CheckConstraint(condition=iki.Q(('lot_number__regex', '[\\r\\n]'), negated=True), name='iki__qc_release_releasecase__lot_number__one_line'),
        ),
        iki.AddConstraint(
            model_name='releasecase',
            constraint=iki.CheckConstraint(condition=iki.Q(('schedule_revision__gte', 1)), name='iki__qc_release_releasecase__schedule_revision__min'),
        ),
        iki.AddConstraint(
            model_name='releasecase',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('context'), 1), name='iki__qc_release_releasecase__context__not_empty'),
        ),
        iki.AddConstraint(
            model_name='releasecase',
            constraint=iki.CheckConstraint(condition=iki.Q(('context__regex', '[\\r\\n]'), negated=True), name='iki__qc_release_releasecase__context__one_line'),
        ),
        iki.AddConstraint(
            model_name='releasecase',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('grade'), 1), name='iki__qc_release_releasecase__grade__not_empty'),
        ),
        iki.AddConstraint(
            model_name='releasecase',
            constraint=iki.CheckConstraint(condition=iki.Q(('grade__regex', '[\\r\\n]'), negated=True), name='iki__qc_release_releasecase__grade__one_line'),
        ),
        iki.AddConstraint(
            model_name='releasecase',
            constraint=iki.CheckConstraint(condition=iki.Q(('cycle__gte', 1)), name='iki__qc_release_releasecase__cycle__min'),
        ),
        iki.AddConstraint(
            model_name='releasecase',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('reason'), 1), name='iki__qc_release_releasecase__reason__not_empty'),
        ),
        iki.AddConstraint(
            model_name='releasecase',
            constraint=iki.CheckConstraint(condition=iki.LessThanOrEqual(iki.Length('reason'), 2000), name='iki__qc_release_releasecase__reason__max_length'),
        ),
        iki.AddConstraint(
            model_name='releasecase',
            constraint=iki.CheckConstraint(condition=iki.Q(('specification_revision__gte', 1)), name='iki__qc_release_releasecase__specification_revision__min'),
        ),
        iki.AddConstraint(
            model_name='releasecase',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('version'), 1), name='iki__qc_release_releasecase__version__not_empty'),
        ),
        iki.AddConstraint(
            model_name='releasecase',
            constraint=iki.CheckConstraint(condition=iki.Q(('version__regex', '[\\r\\n]'), negated=True), name='iki__qc_release_releasecase__version__one_line'),
        ),
        iki.AddConstraint(
            model_name='releasecase',
            constraint=iki.CheckConstraint(condition=iki.Q(('review_flow__in', ['research', 'regulated_qc'])), name='iki__qc_release_releasecase__review_flow__values'),
        ),
        iki.AddConstraint(
            model_name='qcoosinvestigation',
            constraint=iki.CheckConstraint(condition=iki.Q(('review__in', ['recorded', 'submitted', 'reviewed', 'released'])), name='iki__qc_release_qcoosinvestigation__review__known'),
        ),
        iki.AddConstraint(
            model_name='researchoosinvestigation',
            constraint=iki.CheckConstraint(condition=iki.Q(('review__in', ['recorded', 'signed_off'])), name='iki__qc_release_researchoosinvestigation__review__known'),
        ),
        iki.AddConstraint(
            model_name='releasecertificate_analysts',
            constraint=iki.CheckConstraint(condition=iki.Q(('position__lt', 250)), name='iki__qc_release_releasecertificate__analysts__posit__3515a7a778'),
        ),
        iki.AddConstraint(
            model_name='specificationschedule_versions',
            constraint=iki.CheckConstraint(condition=iki.Q(('position__lt', 100)), name='iki__qc_release_specificationschedule__versions__po__5ff9f76baa'),
        ),
    ]
