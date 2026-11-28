# Made by `iki plugin makemigrations`. Review it, and commit it with the manifests it
# migrates: once a deployment has applied it, it never changes.

from iki_server.plugins import operations as iki


class Migration(iki.Migration):

    initial = True

    dependencies = [
        ('iki__protein', '0001_initial'),
        ('iki_anchors', '0001_iki_0_1'),
    ]

    operations = [
        iki.CreateModel(
            name='Antibody_Entry_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('format', iki.TextField(null=True)),
                ('isotype', iki.TextField(null=True)),
                ('constant_species', iki.TextField(null=True)),
            ],
            table='iki__antibody_antibody__entry__history',
        ),
        iki.AppendOnly(
            model_name='Antibody_Entry_History',
        ),
        iki.CreateModel(
            name='Antibody_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('target', iki.TextField(null=True)),
            ],
            table='iki__antibody_antibody__history',
        ),
        iki.AppendOnly(
            model_name='Antibody_History',
        ),
        iki.CreateModel(
            name='Ext_iki_protein_Protein_Entry_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('chain_type', iki.TextField(null=True)),
                ('species', iki.TextField(null=True)),
                ('germline_set', iki.TextField(null=True)),
                ('heavy_v_gene', iki.TextField(null=True)),
                ('heavy_germline_identity', iki.QuantityField(null=True, unit='%')),
                ('heavy_j_gene', iki.TextField(null=True)),
                ('light_v_gene', iki.TextField(null=True)),
                ('light_germline_identity', iki.QuantityField(null=True, unit='%')),
                ('light_j_gene', iki.TextField(null=True)),
            ],
            table='iki__antibody__iki__protein_protein__entry__history',
        ),
        iki.AppendOnly(
            model_name='Ext_iki_protein_Protein_Entry_History',
        ),
        iki.CreateModel(
            name='Ext_iki_protein_Protein_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('numbering_scheme', iki.TextField(null=True)),
                ('cdr_definition', iki.TextField(null=True)),
                ('cdr_h1', iki.TextField(null=True)),
                ('cdr_h2', iki.TextField(null=True)),
                ('cdr_h3', iki.TextField(null=True)),
                ('cdr_l1', iki.TextField(null=True)),
                ('cdr_l2', iki.TextField(null=True)),
                ('cdr_l3', iki.TextField(null=True)),
                ('variable_domains', iki.JSONField(null=True)),
                ('liability_screen', iki.JSONField(null=True)),
                ('liabilities', iki.JSONField(null=True)),
                ('liability_counts', iki.JSONField(null=True)),
                ('liability_count', iki.BigIntegerField(null=True)),
            ],
            table='iki__antibody__iki__protein_protein__history',
        ),
        iki.AppendOnly(
            model_name='Ext_iki_protein_Protein_History',
        ),
        iki.CreateModel(
            name='Antibody',
            fields=[
                ('complex_ptr', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to='iki__core.complex')),
                ('target', iki.CharField(db_index=True, max_length=255, null=True)),
            ],
            table='iki__antibody_antibody',
            indexes=[
                iki.GinIndex(fields=['target'], name='target_trgm_f7169008ed5fd7b3ff', opclasses=['gin_trgm_ops']),
            ],
            constraints=[
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('target'), 1), name='iki__antibody_antibody__target__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('target__regex', '[\\r\\n]'), negated=True), name='iki__antibody_antibody__target__one_line'),
            ],
            bases=('iki__core.complex',),
        ),
        iki.CreateModel(
            name='Antibody_Entry',
            fields=[
                ('complex_entry_ptr', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to='iki__core.complex_entry')),
                ('format', iki.TextField(db_default='igg', db_index=True)),
                ('isotype', iki.TextField(db_index=True, null=True)),
                ('constant_species', iki.TextField(db_index=True, null=True)),
            ],
            table='iki__antibody_antibody__entry',
            constraints=[
                iki.CheckConstraint(condition=iki.Q(('format__in', ['igg', 'fab', 'fv', 'scfv', 'vhh', 'other'])), name='iki__antibody_antibody__entry__format__values'),
                iki.CheckConstraint(condition=iki.Q(('isotype__in', ['igg1', 'igg2', 'igg2a', 'igg2b', 'igg2c', 'igg3', 'igg4', 'igg', 'iga1', 'iga2', 'iga', 'igm', 'ige', 'igd'])), name='iki__antibody_antibody__entry__isotype__values'),
                iki.CheckConstraint(condition=iki.Q(('constant_species__in', ['human', 'macaque', 'mouse', 'rat', 'rabbit'])), name='iki__antibody_antibody__entry__constant_species__values'),
            ],
            bases=('iki__core.complex_entry',),
        ),
        iki.CreateModel(
            name='Ext_iki_protein_Protein',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, primary_key=True, related_name='iki_antibody_Ext', serialize=False, to='iki__protein.protein')),
                ('numbering_scheme', iki.TextField(null=True)),
                ('cdr_definition', iki.TextField(null=True)),
                ('cdr_h1', iki.CharField(max_length=128, null=True)),
                ('cdr_h2', iki.CharField(max_length=128, null=True)),
                ('cdr_h3', iki.CharField(db_index=True, max_length=128, null=True)),
                ('cdr_l1', iki.CharField(max_length=128, null=True)),
                ('cdr_l2', iki.CharField(max_length=128, null=True)),
                ('cdr_l3', iki.CharField(db_index=True, max_length=128, null=True)),
                ('variable_domains', iki.JSONField(null=True)),
                ('liability_screen', iki.JSONField(null=True)),
                ('liabilities', iki.JSONField(null=True)),
                ('liability_counts', iki.JSONField(null=True)),
                ('liability_count', iki.BigIntegerField(db_index=True, null=True)),
            ],
            table='iki__antibody__iki__protein_protein',
            indexes=[
                iki.GinIndex(fields=['cdr_h3'], name='cdr_h3_trgm_73238b81aa8dc86926', opclasses=['gin_trgm_ops']),
                iki.GinIndex(fields=['cdr_l3'], name='cdr_l3_trgm_1166bae1c8d2dd7491', opclasses=['gin_trgm_ops']),
            ],
            constraints=[
                iki.CheckConstraint(condition=iki.Q(('numbering_scheme__in', ['imgt', 'kabat', 'chothia', 'martin', 'aho'])), name='iki__antibody__iki__protein_protein__numbering_scheme__values'),
                iki.CheckConstraint(condition=iki.Q(('cdr_definition__in', ['imgt', 'kabat', 'chothia', 'abm', 'contact'])), name='iki__antibody__iki__protein_protein__cdr_definition__values'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('cdr_h1'), 1), name='iki__antibody__iki__protein_protein__cdr_h1__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('cdr_h1__regex', '[\\r\\n]'), negated=True), name='iki__antibody__iki__protein_protein__cdr_h1__one_line'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('cdr_h2'), 1), name='iki__antibody__iki__protein_protein__cdr_h2__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('cdr_h2__regex', '[\\r\\n]'), negated=True), name='iki__antibody__iki__protein_protein__cdr_h2__one_line'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('cdr_h3'), 1), name='iki__antibody__iki__protein_protein__cdr_h3__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('cdr_h3__regex', '[\\r\\n]'), negated=True), name='iki__antibody__iki__protein_protein__cdr_h3__one_line'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('cdr_l1'), 1), name='iki__antibody__iki__protein_protein__cdr_l1__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('cdr_l1__regex', '[\\r\\n]'), negated=True), name='iki__antibody__iki__protein_protein__cdr_l1__one_line'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('cdr_l2'), 1), name='iki__antibody__iki__protein_protein__cdr_l2__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('cdr_l2__regex', '[\\r\\n]'), negated=True), name='iki__antibody__iki__protein_protein__cdr_l2__one_line'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('cdr_l3'), 1), name='iki__antibody__iki__protein_protein__cdr_l3__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('cdr_l3__regex', '[\\r\\n]'), negated=True), name='iki__antibody__iki__protein_protein__cdr_l3__one_line'),
                iki.CheckConstraint(condition=iki.Q(('liability_count__gte', 0)), name='iki__antibody__iki__protein_protein__liability_count__min'),
            ],
        ),
        iki.CreateModel(
            name='Ext_iki_protein_Protein_Entry',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, primary_key=True, related_name='iki_antibody_Entry', serialize=False, to='iki__protein.protein_entry')),
                ('chain_type', iki.TextField(db_index=True, null=True)),
                ('species', iki.TextField(db_index=True, null=True)),
                ('germline_set', iki.CharField(max_length=100, null=True)),
                ('heavy_v_gene', iki.CharField(db_index=True, max_length=64, null=True)),
                ('heavy_germline_identity', iki.QuantityField(db_index=True, null=True, unit='%')),
                ('heavy_j_gene', iki.CharField(max_length=64, null=True)),
                ('light_v_gene', iki.CharField(db_index=True, max_length=64, null=True)),
                ('light_germline_identity', iki.QuantityField(db_index=True, null=True, unit='%')),
                ('light_j_gene', iki.CharField(max_length=64, null=True)),
            ],
            table='iki__antibody__iki__protein_protein__entry',
            indexes=[
                iki.GinIndex(fields=['heavy_v_gene'], name='heavy_v_ge_trgm_dc05174a6e2c0a', opclasses=['gin_trgm_ops']),
                iki.GinIndex(fields=['light_v_gene'], name='light_v_ge_trgm_e16a89a6b33b64', opclasses=['gin_trgm_ops']),
            ],
            constraints=[
                iki.CheckConstraint(condition=iki.Q(('chain_type__in', ['heavy', 'kappa', 'lambda', 'heavy_and_light'])), name='iki__antibody__iki__protein_protein__entry__chain_type__values'),
                iki.CheckConstraint(condition=iki.Q(('species__in', ['human', 'mouse', 'rat', 'rabbit', 'rhesus', 'pig', 'cow', 'alpaca'])), name='iki__antibody__iki__protein_protein__entry__species__values'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('germline_set'), 1), name='iki__antibody__iki__protein_protein__entry__germlin__90d0e01236'),
                iki.CheckConstraint(condition=iki.Q(('germline_set__regex', '[\\r\\n]'), negated=True), name='iki__antibody__iki__protein_protein__entry__germlin__9b9c9183bc'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('heavy_v_gene'), 1), name='iki__antibody__iki__protein_protein__entry__heavy_v__39b6085b61'),
                iki.CheckConstraint(condition=iki.Q(('heavy_v_gene__regex', '[\\r\\n]'), negated=True), name='iki__antibody__iki__protein_protein__entry__heavy_v__c82d13309e'),
                iki.CheckConstraint(condition=iki.Q(('heavy_germline_identity__gt', float('-inf')), ('heavy_germline_identity__lt', float('inf'))), name='iki__antibody__iki__protein_protein__entry__heavy_g__f1cf540b88'),
                iki.CheckConstraint(condition=iki.Q(('heavy_germline_identity__gte', 0)), name='iki__antibody__iki__protein_protein__entry__heavy_g__c2d6f126b5'),
                iki.CheckConstraint(condition=iki.Q(('heavy_germline_identity__lte', 100)), name='iki__antibody__iki__protein_protein__entry__heavy_g__5894c87263'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('heavy_j_gene'), 1), name='iki__antibody__iki__protein_protein__entry__heavy_j__b14bde5ec1'),
                iki.CheckConstraint(condition=iki.Q(('heavy_j_gene__regex', '[\\r\\n]'), negated=True), name='iki__antibody__iki__protein_protein__entry__heavy_j__50a5402789'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('light_v_gene'), 1), name='iki__antibody__iki__protein_protein__entry__light_v__06a7c648e7'),
                iki.CheckConstraint(condition=iki.Q(('light_v_gene__regex', '[\\r\\n]'), negated=True), name='iki__antibody__iki__protein_protein__entry__light_v__edcfff0927'),
                iki.CheckConstraint(condition=iki.Q(('light_germline_identity__gt', float('-inf')), ('light_germline_identity__lt', float('inf'))), name='iki__antibody__iki__protein_protein__entry__light_g__03c2b66e4e'),
                iki.CheckConstraint(condition=iki.Q(('light_germline_identity__gte', 0)), name='iki__antibody__iki__protein_protein__entry__light_g__5e8826584f'),
                iki.CheckConstraint(condition=iki.Q(('light_germline_identity__lte', 100)), name='iki__antibody__iki__protein_protein__entry__light_g__ec32375193'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('light_j_gene'), 1), name='iki__antibody__iki__protein_protein__entry__light_j__3e69159466'),
                iki.CheckConstraint(condition=iki.Q(('light_j_gene__regex', '[\\r\\n]'), negated=True), name='iki__antibody__iki__protein_protein__entry__light_j__e6d9f827fd'),
            ],
        ),
    ]
