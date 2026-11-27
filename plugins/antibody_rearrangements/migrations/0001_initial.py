# Made by `iki plugin makemigrations`. Review it, and commit it with the manifests it
# migrates: once a deployment has applied it, it never changes.

from iki_server.plugins import operations as iki


class Migration(iki.Migration):

    initial = True

    dependencies = [
        ('iki__antibody', '0001_initial'),
        ('iki__sequencing_analysis', '0001_initial'),
        ('iki_anchors', '0001_iki_0_1'),
    ]

    operations = [
        iki.CreateModel(
            name='AntibodyRearrangement_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('antibody', iki.UUIDField(db_column='antibody_id', null=True)),
                ('result', iki.UUIDField(db_column='result_id', null=True)),
                ('sequence_id', iki.TextField(null=True)),
                ('repertoire_id', iki.TextField(null=True)),
                ('locus', iki.TextField(null=True)),
                ('rearrangement', iki.JSONField(null=True)),
                ('curator', iki.UUIDField(db_column='curator_id', null=True)),
                ('inputs', iki.JSONField(null=True)),
                ('notes', iki.TextField(null=True)),
            ],
            table='iki__antibody_rearrangements_antibodyrearrangement__history',
        ),
        iki.AppendOnly(
            model_name='AntibodyRearrangement_History',
        ),
        iki.CreateModel(
            name='AntibodyRearrangement',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('sequence_id', iki.CharField(max_length=250)),
                ('repertoire_id', iki.CharField(max_length=250, null=True)),
                ('locus', iki.TextField(null=True)),
                ('rearrangement', iki.JSONField(null=True)),
                ('inputs', iki.JSONField(null=True)),
                ('notes', iki.TextField(null=True)),
                ('antibody', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__antibody.antibody_entry')),
                ('curator', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to=iki.USER)),
                ('result', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__sequencing_analysis.sequencinganalysisresult')),
            ],
            table='iki__antibody_rearrangements_antibodyrearrangement',
            indexes=[
                iki.GinIndex(iki.SearchVector(iki.Left('notes', 100000), config='simple'), name='notes_fts_acf4abeccc34ad2deff4'),
            ],
            constraints=[
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('sequence_id'), 1), name='iki__antibody_rearrangements_antibodyrearrangement___1eb246b11e'),
                iki.CheckConstraint(condition=iki.Q(('sequence_id__regex', '[\\r\\n]'), negated=True), name='iki__antibody_rearrangements_antibodyrearrangement___b45467caeb'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('repertoire_id'), 1), name='iki__antibody_rearrangements_antibodyrearrangement___16d1eca727'),
                iki.CheckConstraint(condition=iki.Q(('repertoire_id__regex', '[\\r\\n]'), negated=True), name='iki__antibody_rearrangements_antibodyrearrangement___3576212bdc'),
                iki.CheckConstraint(condition=iki.Q(('locus__in', ['IGH', 'IGK', 'IGL'])), name='iki__antibody_rearrangements_antibodyrearrangement___b3fdf2ee78'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('notes'), 1), name='iki__antibody_rearrangements_antibodyrearrangement___e1a72a1b7e'),
                iki.CheckConstraint(condition=iki.LessThanOrEqual(iki.Length('notes'), 3000), name='iki__antibody_rearrangements_antibodyrearrangement___5d5e716a52'),
            ],
            bases=(iki.ENTITY,),
        ),
    ]
