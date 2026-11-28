# Made by `iki plugin makemigrations`. Review it, and commit it with the manifests it
# migrates: once a deployment has applied it, it never changes.

from iki_server.plugins import operations as iki


class Migration(iki.Migration):

    initial = True

    dependencies = [
        ('iki__dna', '0001_initial'),
        ('iki_anchors', '0001_iki_0_1'),
    ]

    operations = [
        iki.CreateModel(
            name='Assembly',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('name', iki.CharField(db_index=True, max_length=255, null=True)),
                ('description', iki.TextField(null=True)),
                ('method', iki.TextField(db_index=True)),
                ('enzymes', iki.ArrayField(base_field=iki.CharField(max_length=20), null=True)),
                ('simulated_length', iki.QuantityField(null=True, unit='{bp}')),
                ('simulated_topology', iki.TextField(db_index=True, null=True)),
                ('verification_note', iki.TextField(null=True)),
                ('matches_product', iki.BooleanField(null=True)),
                ('junctions', iki.JSONField(null=True)),
                ('backbone', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__dna.dnasequence_entry')),
                ('product', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to='iki__dna.dnasequence_entry')),
            ],
            table='iki__plasmid_assembly',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='Assembly_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('name', iki.TextField(null=True)),
                ('description', iki.TextField(null=True)),
                ('method', iki.TextField(null=True)),
                ('enzymes', iki.ArrayField(base_field=iki.TextField(), null=True)),
                ('backbone', iki.UUIDField(db_column='backbone_id', null=True)),
                ('inserts', iki.ArrayField(base_field=iki.UUIDField(), null=True)),
                ('product', iki.UUIDField(db_column='product_id', null=True)),
                ('simulated_length', iki.QuantityField(null=True, unit='{bp}')),
                ('simulated_topology', iki.TextField(null=True)),
                ('verification_note', iki.TextField(null=True)),
                ('matches_product', iki.BooleanField(null=True)),
                ('junctions', iki.JSONField(null=True)),
                ('fragments', iki.JSONField(null=True)),
            ],
            table='iki__plasmid_assembly__history',
        ),
        iki.AppendOnly(
            model_name='Assembly_History',
        ),
        iki.CreateModel(
            name='Pcr_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('name', iki.TextField(null=True)),
                ('description', iki.TextField(null=True)),
                ('template', iki.UUIDField(db_column='template_id', null=True)),
                ('forward_primer', iki.UUIDField(db_column='forward_primer_id', null=True)),
                ('reverse_primer', iki.UUIDField(db_column='reverse_primer_id', null=True)),
                ('product_length', iki.QuantityField(null=True, unit='{bp}')),
                ('forward_melting_temperature', iki.QuantityField(null=True, unit='Cel')),
                ('reverse_melting_temperature', iki.QuantityField(null=True, unit='Cel')),
                ('primer_dimer_free_energy', iki.QuantityField(null=True, unit='kcal/mol')),
                ('tm_method', iki.TextField(null=True)),
                ('products_found', iki.BigIntegerField(null=True)),
            ],
            table='iki__plasmid_pcr__history',
        ),
        iki.AppendOnly(
            model_name='Pcr_History',
        ),
        iki.CreateModel(
            name='Plasmid_Entry',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('length', iki.QuantityField(db_index=True, null=True, unit='{bp}')),
                ('topology', iki.TextField(db_index=True, null=True)),
                ('copy_number', iki.TextField(db_index=True, null=True)),
                ('biosafety_level', iki.TextField(db_index=True, null=True)),
                ('sequence', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__dna.dnasequence_entry')),
            ],
            table='iki__plasmid_plasmid__entry',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='Plasmid_Entry_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('sequence', iki.UUIDField(db_column='sequence_id', null=True)),
                ('length', iki.QuantityField(null=True, unit='{bp}')),
                ('topology', iki.TextField(null=True)),
                ('vector_type', iki.UUIDField(db_column='vector_type_id', null=True)),
                ('hosts', iki.ArrayField(base_field=iki.UUIDField(), null=True)),
                ('copy_number', iki.TextField(null=True)),
                ('biosafety_level', iki.TextField(null=True)),
            ],
            table='iki__plasmid_plasmid__entry__history',
        ),
        iki.AppendOnly(
            model_name='Plasmid_Entry_History',
        ),
        iki.CreateModel(
            name='Plasmid_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('name', iki.TextField(null=True)),
                ('description', iki.TextField(null=True)),
                ('replication_origins', iki.ArrayField(base_field=iki.TextField(), null=True)),
                ('bacterial_resistance', iki.ArrayField(base_field=iki.TextField(), null=True)),
                ('selection_markers', iki.ArrayField(base_field=iki.TextField(), null=True)),
                ('promoters', iki.ArrayField(base_field=iki.TextField(), null=True)),
                ('annotated_features', iki.JSONField(null=True)),
                ('orfs', iki.JSONField(null=True)),
                ('orfs_found_with', iki.TextField(null=True)),
                ('primer_binding_sites', iki.JSONField(null=True)),
                ('primer_binding_sites_found_with', iki.TextField(null=True)),
                ('annotated_with', iki.TextField(null=True)),
                ('source', iki.TextField(null=True)),
                ('source_assembly', iki.UUIDField(db_column='source_assembly_id', null=True)),
                ('addgene_id', iki.TextField(null=True)),
                ('vendor', iki.TextField(null=True)),
                ('catalog_number', iki.TextField(null=True)),
                ('depositor', iki.TextField(null=True)),
                ('received_on', iki.DateField(null=True)),
                ('growth_strain', iki.TextField(null=True)),
                ('growth_temperature', iki.QuantityField(null=True, unit='Cel')),
                ('mta_required', iki.BooleanField(null=True)),
                ('mta_reference', iki.TextField(null=True)),
                ('mta_provider', iki.TextField(null=True)),
                ('mta_signed_on', iki.DateField(null=True)),
                ('mta_restrictions', iki.TextField(null=True)),
                ('biosafety_approval', iki.TextField(null=True)),
            ],
            table='iki__plasmid_plasmid__history',
        ),
        iki.AppendOnly(
            model_name='Plasmid_History',
        ),
        iki.CreateModel(
            name='PlasmidHost',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
            ],
            table='iki__plasmid_plasmidhost',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='PlasmidHost_Entry_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('code', iki.TextField(null=True)),
                ('label', iki.TextField(null=True)),
                ('description', iki.TextField(null=True)),
                ('position', iki.BigIntegerField(null=True)),
            ],
            table='iki__plasmid_plasmidhost__entry__history',
        ),
        iki.AppendOnly(
            model_name='PlasmidHost_Entry_History',
        ),
        iki.CreateModel(
            name='PlasmidHost_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
            ],
            table='iki__plasmid_plasmidhost__history',
        ),
        iki.AppendOnly(
            model_name='PlasmidHost_History',
        ),
        iki.CreateModel(
            name='VectorType',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
            ],
            table='iki__plasmid_vectortype',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='VectorType_Entry_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('code', iki.TextField(null=True)),
                ('label', iki.TextField(null=True)),
                ('description', iki.TextField(null=True)),
                ('position', iki.BigIntegerField(null=True)),
            ],
            table='iki__plasmid_vectortype__entry__history',
        ),
        iki.AppendOnly(
            model_name='VectorType_Entry_History',
        ),
        iki.CreateModel(
            name='VectorType_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
            ],
            table='iki__plasmid_vectortype__history',
        ),
        iki.AppendOnly(
            model_name='VectorType_History',
        ),
        iki.CreateModel(
            name='Pcr',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('name', iki.CharField(db_index=True, max_length=255, null=True)),
                ('description', iki.TextField(null=True)),
                ('product_length', iki.QuantityField(null=True, unit='{bp}')),
                ('forward_melting_temperature', iki.QuantityField(null=True, unit='Cel')),
                ('reverse_melting_temperature', iki.QuantityField(null=True, unit='Cel')),
                ('primer_dimer_free_energy', iki.QuantityField(db_index=True, null=True, unit='kcal/mol')),
                ('tm_method', iki.CharField(max_length=300, null=True)),
                ('products_found', iki.BigIntegerField(null=True)),
                ('forward_primer', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__dna.primer_entry')),
                ('reverse_primer', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__dna.primer_entry')),
                ('template', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__dna.dnasequence_entry')),
            ],
            table='iki__plasmid_pcr',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='Plasmid',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('name', iki.CharField(db_index=True, max_length=255, null=True)),
                ('description', iki.TextField(null=True)),
                ('replication_origins', iki.ArrayField(base_field=iki.CharField(max_length=60), null=True)),
                ('bacterial_resistance', iki.ArrayField(base_field=iki.TextField(), null=True)),
                ('selection_markers', iki.ArrayField(base_field=iki.TextField(), null=True)),
                ('promoters', iki.ArrayField(base_field=iki.CharField(max_length=60), null=True)),
                ('annotated_features', iki.JSONField(null=True)),
                ('orfs', iki.JSONField(null=True)),
                ('orfs_found_with', iki.CharField(max_length=200, null=True)),
                ('primer_binding_sites', iki.JSONField(null=True)),
                ('primer_binding_sites_found_with', iki.CharField(max_length=300, null=True)),
                ('annotated_with', iki.CharField(max_length=200, null=True)),
                ('source', iki.TextField(db_index=True, null=True)),
                ('addgene_id', iki.CharField(db_index=True, max_length=7, null=True)),
                ('vendor', iki.CharField(max_length=200, null=True)),
                ('catalog_number', iki.CharField(db_index=True, max_length=100, null=True)),
                ('depositor', iki.CharField(max_length=200, null=True)),
                ('received_on', iki.DateField(null=True)),
                ('growth_strain', iki.CharField(max_length=100, null=True)),
                ('growth_temperature', iki.QuantityField(null=True, unit='Cel')),
                ('mta_required', iki.BooleanField(db_index=True, null=True)),
                ('mta_reference', iki.CharField(db_index=True, max_length=100, null=True)),
                ('mta_provider', iki.CharField(max_length=200, null=True)),
                ('mta_signed_on', iki.DateField(null=True)),
                ('mta_restrictions', iki.TextField(null=True)),
                ('biosafety_approval', iki.CharField(max_length=100, null=True)),
                ('source_assembly', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to='iki__plasmid.assembly')),
            ],
            table='iki__plasmid_plasmid',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='Plasmid_Entry_hosts',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity', 'position')),
                ('entity', iki.ForeignKey(db_column='entity_id', on_delete=iki.CASCADE, related_name='+', to='iki__plasmid.plasmid_entry')),
                ('position', iki.PositiveIntegerField()),
            ],
            table='iki__plasmid_plasmid__entry__hosts',
        ),
        iki.CreateModel(
            name='PlasmidHost_Entry',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('code', iki.CharField(db_index=True, max_length=64)),
                ('label', iki.CharField(db_index=True, max_length=200)),
                ('description', iki.TextField(null=True)),
                ('position', iki.BigIntegerField(null=True)),
            ],
            table='iki__plasmid_plasmidhost__entry',
            indexes=[
                iki.GinIndex(fields=['code'], name='code_trgm_c80e0c311d58003cec53', opclasses=['gin_trgm_ops']),
                iki.GinIndex(fields=['label'], name='label_trgm_f8190fd5457a22ab0ed', opclasses=['gin_trgm_ops']),
                iki.GinIndex(iki.SearchVector(iki.Left('description', 100000), config='simple'), name='descriptio_fts_495bf9755b724e7'),
            ],
            constraints=[
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('code'), 1), name='iki__plasmid_plasmidhost__entry__code__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('code__regex', '[\\r\\n]'), negated=True), name='iki__plasmid_plasmidhost__entry__code__one_line'),
                iki.CheckConstraint(condition=iki.Q(('code__regex', '^(?:^[A-Za-z0-9][A-Za-z0-9_.+-]*$)$')), name='iki__plasmid_plasmidhost__entry__code__pattern'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('label'), 1), name='iki__plasmid_plasmidhost__entry__label__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('label__regex', '[\\r\\n]'), negated=True), name='iki__plasmid_plasmidhost__entry__label__one_line'),
                iki.CheckConstraint(condition=iki.Q(('label__regex', '^(?:^\\S(?:.*\\S)?$)$')), name='iki__plasmid_plasmidhost__entry__label__pattern'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('description'), 1), name='iki__plasmid_plasmidhost__entry__description__not_empty'),
                iki.CheckConstraint(condition=iki.LessThanOrEqual(iki.Length('description'), 2000), name='iki__plasmid_plasmidhost__entry__description__max_length'),
                iki.CheckConstraint(condition=iki.Q(('position__gte', 1)), name='iki__plasmid_plasmidhost__entry__position__min'),
                iki.CheckConstraint(condition=iki.Q(('position__lte', 10000)), name='iki__plasmid_plasmidhost__entry__position__max'),
            ],
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='VectorType_Entry',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('code', iki.CharField(db_index=True, max_length=64)),
                ('label', iki.CharField(db_index=True, max_length=200)),
                ('description', iki.TextField(null=True)),
                ('position', iki.BigIntegerField(null=True)),
            ],
            table='iki__plasmid_vectortype__entry',
            indexes=[
                iki.GinIndex(fields=['code'], name='code_trgm_81bf7ea9fdb33614a081', opclasses=['gin_trgm_ops']),
                iki.GinIndex(fields=['label'], name='label_trgm_5c932dc72418226ba44', opclasses=['gin_trgm_ops']),
                iki.GinIndex(iki.SearchVector(iki.Left('description', 100000), config='simple'), name='descriptio_fts_f0eb7ad6156d4db'),
            ],
            constraints=[
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('code'), 1), name='iki__plasmid_vectortype__entry__code__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('code__regex', '[\\r\\n]'), negated=True), name='iki__plasmid_vectortype__entry__code__one_line'),
                iki.CheckConstraint(condition=iki.Q(('code__regex', '^(?:^[A-Za-z0-9][A-Za-z0-9_.+-]*$)$')), name='iki__plasmid_vectortype__entry__code__pattern'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('label'), 1), name='iki__plasmid_vectortype__entry__label__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('label__regex', '[\\r\\n]'), negated=True), name='iki__plasmid_vectortype__entry__label__one_line'),
                iki.CheckConstraint(condition=iki.Q(('label__regex', '^(?:^\\S(?:.*\\S)?$)$')), name='iki__plasmid_vectortype__entry__label__pattern'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('description'), 1), name='iki__plasmid_vectortype__entry__description__not_empty'),
                iki.CheckConstraint(condition=iki.LessThanOrEqual(iki.Length('description'), 2000), name='iki__plasmid_vectortype__entry__description__max_length'),
                iki.CheckConstraint(condition=iki.Q(('position__gte', 1)), name='iki__plasmid_vectortype__entry__position__min'),
                iki.CheckConstraint(condition=iki.Q(('position__lte', 10000)), name='iki__plasmid_vectortype__entry__position__max'),
            ],
            bases=(iki.ENTITY,),
        ),
        iki.AddField(
            model_name='plasmid_entry',
            name='vector_type',
            field=iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to='iki__plasmid.vectortype_entry'),
        ),
        iki.CreateModel(
            name='Assembly_fragments',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity', 'position')),
                ('entity', iki.ForeignKey(db_column='entity_id', on_delete=iki.CASCADE, related_name='+', to='iki__plasmid.assembly')),
                ('position', iki.PositiveIntegerField()),
                ('part', iki.CharField(max_length=255)),
                ('length', iki.BigIntegerField()),
                ('start', iki.BigIntegerField()),
                ('end', iki.BigIntegerField()),
                ('left', iki.CharField(max_length=200, null=True)),
                ('right', iki.CharField(max_length=200, null=True)),
            ],
            table='iki__plasmid_assembly__fragments',
            constraints=[
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('part'), 1), name='iki__plasmid_assembly__fragments__part__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('part__regex', '[\\r\\n]'), negated=True), name='iki__plasmid_assembly__fragments__part__one_line'),
                iki.CheckConstraint(condition=iki.Q(('length__gte', 1)), name='iki__plasmid_assembly__fragments__length__min'),
                iki.CheckConstraint(condition=iki.Q(('start__gte', 1)), name='iki__plasmid_assembly__fragments__start__min'),
                iki.CheckConstraint(condition=iki.Q(('end__gte', 1)), name='iki__plasmid_assembly__fragments__end__min'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('left'), 1), name='iki__plasmid_assembly__fragments__left__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('left__regex', '[\\r\\n]'), negated=True), name='iki__plasmid_assembly__fragments__left__one_line'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('right'), 1), name='iki__plasmid_assembly__fragments__right__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('right__regex', '[\\r\\n]'), negated=True), name='iki__plasmid_assembly__fragments__right__one_line'),
                iki.CheckConstraint(condition=iki.Q(('position__lt', 500)), name='iki__plasmid_assembly__fragments__position__max_items'),
            ],
        ),
        iki.CreateModel(
            name='Assembly_inserts',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity', 'position')),
                ('entity', iki.ForeignKey(db_column='entity_id', on_delete=iki.CASCADE, related_name='+', to='iki__plasmid.assembly')),
                ('position', iki.PositiveIntegerField()),
                ('item', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__dna.dnasequence_entry')),
            ],
            table='iki__plasmid_assembly__inserts',
        ),
        iki.AddIndex(
            model_name='assembly',
            index=iki.GinIndex(fields=['name'], name='name_trgm_fd4ee3026c04bc56cb6f', opclasses=['gin_trgm_ops']),
        ),
        iki.AddIndex(
            model_name='assembly',
            index=iki.GinIndex(iki.SearchVector(iki.Left('description', 100000), config='simple'), name='descriptio_fts_0c824d08c04a999'),
        ),
        iki.AddIndex(
            model_name='assembly',
            index=iki.GinIndex(iki.SearchVector(iki.Left('verification_note', 100000), config='simple'), name='verificati_fts_b75bc03955994b3'),
        ),
        iki.AddConstraint(
            model_name='assembly',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('name'), 1), name='iki__plasmid_assembly__name__not_empty'),
        ),
        iki.AddConstraint(
            model_name='assembly',
            constraint=iki.CheckConstraint(condition=iki.Q(('name__regex', '[\\r\\n]'), negated=True), name='iki__plasmid_assembly__name__one_line'),
        ),
        iki.AddConstraint(
            model_name='assembly',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('description'), 1), name='iki__plasmid_assembly__description__not_empty'),
        ),
        iki.AddConstraint(
            model_name='assembly',
            constraint=iki.CheckConstraint(condition=iki.LessThanOrEqual(iki.Length('description'), 10000), name='iki__plasmid_assembly__description__max_length'),
        ),
        iki.AddConstraint(
            model_name='assembly',
            constraint=iki.CheckConstraint(condition=iki.Q(('method__in', ['digest', 'restriction_ligation', 'golden_gate', 'gibson', 'gateway_lr', 'gateway_bp', 'topo'])), name='iki__plasmid_assembly__method__values'),
        ),
        iki.AddConstraint(
            model_name='assembly',
            constraint=iki.CheckConstraint(condition=iki.IsPlainList('enzymes'), name='iki__plasmid_assembly__enzymes__plain_list'),
        ),
        iki.AddConstraint(
            model_name='assembly',
            constraint=iki.CheckConstraint(condition=iki.Q(('enzymes__len__lte', 6)), name='iki__plasmid_assembly__enzymes__max_items'),
        ),
        iki.AddConstraint(
            model_name='assembly',
            constraint=iki.CheckConstraint(condition=iki.AllLengthsBetween('enzymes', 1, None), name='iki__plasmid_assembly__enzymes__item_length'),
        ),
        iki.AddConstraint(
            model_name='assembly',
            constraint=iki.CheckConstraint(condition=iki.NoneMatch('enzymes', '[\\r\\n]'), name='iki__plasmid_assembly__enzymes__item_one_line'),
        ),
        iki.AddConstraint(
            model_name='assembly',
            constraint=iki.CheckConstraint(condition=iki.AllMatch('enzymes', '^(?:^[A-Za-z][A-Za-z0-9.-]{1,19}$)$'), name='iki__plasmid_assembly__enzymes__item_pattern'),
        ),
        iki.AddConstraint(
            model_name='assembly',
            constraint=iki.CheckConstraint(condition=iki.Q(('simulated_length__gt', float('-inf')), ('simulated_length__lt', float('inf'))), name='iki__plasmid_assembly__simulated_length__finite'),
        ),
        iki.AddConstraint(
            model_name='assembly',
            constraint=iki.CheckConstraint(condition=iki.Q(('simulated_length__gte', 0)), name='iki__plasmid_assembly__simulated_length__min'),
        ),
        iki.AddConstraint(
            model_name='assembly',
            constraint=iki.CheckConstraint(condition=iki.Q(('simulated_topology__in', ['circular', 'linear'])), name='iki__plasmid_assembly__simulated_topology__values'),
        ),
        iki.AddConstraint(
            model_name='assembly',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('verification_note'), 1), name='iki__plasmid_assembly__verification_note__not_empty'),
        ),
        iki.AddConstraint(
            model_name='assembly',
            constraint=iki.CheckConstraint(condition=iki.LessThanOrEqual(iki.Length('verification_note'), 10000), name='iki__plasmid_assembly__verification_note__max_length'),
        ),
        iki.AddIndex(
            model_name='pcr',
            index=iki.GinIndex(fields=['name'], name='name_trgm_573973cfdbb190175389', opclasses=['gin_trgm_ops']),
        ),
        iki.AddIndex(
            model_name='pcr',
            index=iki.GinIndex(iki.SearchVector(iki.Left('description', 100000), config='simple'), name='descriptio_fts_b1fecb8a2fbe7b9'),
        ),
        iki.AddConstraint(
            model_name='pcr',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('name'), 1), name='iki__plasmid_pcr__name__not_empty'),
        ),
        iki.AddConstraint(
            model_name='pcr',
            constraint=iki.CheckConstraint(condition=iki.Q(('name__regex', '[\\r\\n]'), negated=True), name='iki__plasmid_pcr__name__one_line'),
        ),
        iki.AddConstraint(
            model_name='pcr',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('description'), 1), name='iki__plasmid_pcr__description__not_empty'),
        ),
        iki.AddConstraint(
            model_name='pcr',
            constraint=iki.CheckConstraint(condition=iki.LessThanOrEqual(iki.Length('description'), 10000), name='iki__plasmid_pcr__description__max_length'),
        ),
        iki.AddConstraint(
            model_name='pcr',
            constraint=iki.CheckConstraint(condition=iki.Q(('product_length__gt', float('-inf')), ('product_length__lt', float('inf'))), name='iki__plasmid_pcr__product_length__finite'),
        ),
        iki.AddConstraint(
            model_name='pcr',
            constraint=iki.CheckConstraint(condition=iki.Q(('product_length__gte', 0)), name='iki__plasmid_pcr__product_length__min'),
        ),
        iki.AddConstraint(
            model_name='pcr',
            constraint=iki.CheckConstraint(condition=iki.Q(('forward_melting_temperature__gt', float('-inf')), ('forward_melting_temperature__lt', float('inf'))), name='iki__plasmid_pcr__forward_melting_temperature__finite'),
        ),
        iki.AddConstraint(
            model_name='pcr',
            constraint=iki.CheckConstraint(condition=iki.Q(('reverse_melting_temperature__gt', float('-inf')), ('reverse_melting_temperature__lt', float('inf'))), name='iki__plasmid_pcr__reverse_melting_temperature__finite'),
        ),
        iki.AddConstraint(
            model_name='pcr',
            constraint=iki.CheckConstraint(condition=iki.Q(('primer_dimer_free_energy__gt', float('-inf')), ('primer_dimer_free_energy__lt', float('inf'))), name='iki__plasmid_pcr__primer_dimer_free_energy__finite'),
        ),
        iki.AddConstraint(
            model_name='pcr',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('tm_method'), 1), name='iki__plasmid_pcr__tm_method__not_empty'),
        ),
        iki.AddConstraint(
            model_name='pcr',
            constraint=iki.CheckConstraint(condition=iki.Q(('tm_method__regex', '[\\r\\n]'), negated=True), name='iki__plasmid_pcr__tm_method__one_line'),
        ),
        iki.AddConstraint(
            model_name='pcr',
            constraint=iki.CheckConstraint(condition=iki.Q(('products_found__gte', 0)), name='iki__plasmid_pcr__products_found__min'),
        ),
        iki.AddIndex(
            model_name='plasmid',
            index=iki.GinIndex(fields=['name'], name='name_trgm_97bd27f7581f90e029ea', opclasses=['gin_trgm_ops']),
        ),
        iki.AddIndex(
            model_name='plasmid',
            index=iki.GinIndex(iki.SearchVector(iki.Left('description', 100000), config='simple'), name='descriptio_fts_56ab66de191bf01'),
        ),
        iki.AddIndex(
            model_name='plasmid',
            index=iki.GinIndex(fields=['addgene_id'], name='addgene_id_trgm_50ba19ff5df619', opclasses=['gin_trgm_ops']),
        ),
        iki.AddIndex(
            model_name='plasmid',
            index=iki.GinIndex(fields=['catalog_number'], name='catalog_nu_trgm_541f14ea6d435b', opclasses=['gin_trgm_ops']),
        ),
        iki.AddIndex(
            model_name='plasmid',
            index=iki.GinIndex(fields=['mta_reference'], name='mta_refere_trgm_552acddd07b19c', opclasses=['gin_trgm_ops']),
        ),
        iki.AddIndex(
            model_name='plasmid',
            index=iki.GinIndex(iki.SearchVector(iki.Left('mta_restrictions', 100000), config='simple'), name='mta_restri_fts_f86d8aa2a0adf96'),
        ),
        iki.AddConstraint(
            model_name='plasmid',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('name'), 1), name='iki__plasmid_plasmid__name__not_empty'),
        ),
        iki.AddConstraint(
            model_name='plasmid',
            constraint=iki.CheckConstraint(condition=iki.Q(('name__regex', '[\\r\\n]'), negated=True), name='iki__plasmid_plasmid__name__one_line'),
        ),
        iki.AddConstraint(
            model_name='plasmid',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('description'), 1), name='iki__plasmid_plasmid__description__not_empty'),
        ),
        iki.AddConstraint(
            model_name='plasmid',
            constraint=iki.CheckConstraint(condition=iki.LessThanOrEqual(iki.Length('description'), 10000), name='iki__plasmid_plasmid__description__max_length'),
        ),
        iki.AddConstraint(
            model_name='plasmid',
            constraint=iki.CheckConstraint(condition=iki.IsPlainList('replication_origins'), name='iki__plasmid_plasmid__replication_origins__plain_list'),
        ),
        iki.AddConstraint(
            model_name='plasmid',
            constraint=iki.CheckConstraint(condition=iki.Q(('replication_origins__len__lte', 10)), name='iki__plasmid_plasmid__replication_origins__max_items'),
        ),
        iki.AddConstraint(
            model_name='plasmid',
            constraint=iki.CheckConstraint(condition=iki.AllLengthsBetween('replication_origins', 1, None), name='iki__plasmid_plasmid__replication_origins__item_length'),
        ),
        iki.AddConstraint(
            model_name='plasmid',
            constraint=iki.CheckConstraint(condition=iki.NoneMatch('replication_origins', '[\\r\\n]'), name='iki__plasmid_plasmid__replication_origins__item_one_line'),
        ),
        iki.AddConstraint(
            model_name='plasmid',
            constraint=iki.CheckConstraint(condition=iki.AllMatch('replication_origins', "^(?:^[A-Za-z0-9][A-Za-z0-9 ()./+'-]{0,59}$)$"), name='iki__plasmid_plasmid__replication_origins__item_pattern'),
        ),
        iki.AddConstraint(
            model_name='plasmid',
            constraint=iki.CheckConstraint(condition=iki.IsPlainList('bacterial_resistance'), name='iki__plasmid_plasmid__bacterial_resistance__plain_list'),
        ),
        iki.AddConstraint(
            model_name='plasmid',
            constraint=iki.CheckConstraint(condition=iki.Q(('bacterial_resistance__len__lte', 8)), name='iki__plasmid_plasmid__bacterial_resistance__max_items'),
        ),
        iki.AddConstraint(
            model_name='plasmid',
            constraint=iki.CheckConstraint(condition=iki.Q(('bacterial_resistance__contained_by', ['ampicillin', 'kanamycin', 'chloramphenicol', 'tetracycline', 'spectinomycin', 'streptomycin', 'gentamicin', 'zeocin'])), name='iki__plasmid_plasmid__bacterial_resistance__item_values'),
        ),
        iki.AddConstraint(
            model_name='plasmid',
            constraint=iki.CheckConstraint(condition=iki.IsPlainList('selection_markers'), name='iki__plasmid_plasmid__selection_markers__plain_list'),
        ),
        iki.AddConstraint(
            model_name='plasmid',
            constraint=iki.CheckConstraint(condition=iki.Q(('selection_markers__len__lte', 8)), name='iki__plasmid_plasmid__selection_markers__max_items'),
        ),
        iki.AddConstraint(
            model_name='plasmid',
            constraint=iki.CheckConstraint(condition=iki.Q(('selection_markers__contained_by', ['neomycin_g418', 'puromycin', 'hygromycin', 'blasticidin', 'zeocin', 'ura3', 'leu2', 'his3', 'trp1', 'lys2', 'ade2', 'dhfr', 'gs'])), name='iki__plasmid_plasmid__selection_markers__item_values'),
        ),
        iki.AddConstraint(
            model_name='plasmid',
            constraint=iki.CheckConstraint(condition=iki.IsPlainList('promoters'), name='iki__plasmid_plasmid__promoters__plain_list'),
        ),
        iki.AddConstraint(
            model_name='plasmid',
            constraint=iki.CheckConstraint(condition=iki.Q(('promoters__len__lte', 12)), name='iki__plasmid_plasmid__promoters__max_items'),
        ),
        iki.AddConstraint(
            model_name='plasmid',
            constraint=iki.CheckConstraint(condition=iki.AllLengthsBetween('promoters', 1, None), name='iki__plasmid_plasmid__promoters__item_length'),
        ),
        iki.AddConstraint(
            model_name='plasmid',
            constraint=iki.CheckConstraint(condition=iki.NoneMatch('promoters', '[\\r\\n]'), name='iki__plasmid_plasmid__promoters__item_one_line'),
        ),
        iki.AddConstraint(
            model_name='plasmid',
            constraint=iki.CheckConstraint(condition=iki.AllMatch('promoters', "^(?:^[A-Za-z0-9][A-Za-z0-9 ()./+'-]{0,59}$)$"), name='iki__plasmid_plasmid__promoters__item_pattern'),
        ),
        iki.AddConstraint(
            model_name='plasmid',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('orfs_found_with'), 1), name='iki__plasmid_plasmid__orfs_found_with__not_empty'),
        ),
        iki.AddConstraint(
            model_name='plasmid',
            constraint=iki.CheckConstraint(condition=iki.Q(('orfs_found_with__regex', '[\\r\\n]'), negated=True), name='iki__plasmid_plasmid__orfs_found_with__one_line'),
        ),
        iki.AddConstraint(
            model_name='plasmid',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('primer_binding_sites_found_with'), 1), name='iki__plasmid_plasmid__primer_binding_sites_found_wi__b2260627c7'),
        ),
        iki.AddConstraint(
            model_name='plasmid',
            constraint=iki.CheckConstraint(condition=iki.Q(('primer_binding_sites_found_with__regex', '[\\r\\n]'), negated=True), name='iki__plasmid_plasmid__primer_binding_sites_found_with__one_line'),
        ),
        iki.AddConstraint(
            model_name='plasmid',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('annotated_with'), 1), name='iki__plasmid_plasmid__annotated_with__not_empty'),
        ),
        iki.AddConstraint(
            model_name='plasmid',
            constraint=iki.CheckConstraint(condition=iki.Q(('annotated_with__regex', '[\\r\\n]'), negated=True), name='iki__plasmid_plasmid__annotated_with__one_line'),
        ),
        iki.AddConstraint(
            model_name='plasmid',
            constraint=iki.CheckConstraint(condition=iki.Q(('source__in', ['in_house', 'addgene', 'vendor', 'collaborator', 'other'])), name='iki__plasmid_plasmid__source__values'),
        ),
        iki.AddConstraint(
            model_name='plasmid',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('addgene_id'), 1), name='iki__plasmid_plasmid__addgene_id__not_empty'),
        ),
        iki.AddConstraint(
            model_name='plasmid',
            constraint=iki.CheckConstraint(condition=iki.Q(('addgene_id__regex', '[\\r\\n]'), negated=True), name='iki__plasmid_plasmid__addgene_id__one_line'),
        ),
        iki.AddConstraint(
            model_name='plasmid',
            constraint=iki.CheckConstraint(condition=iki.Q(('addgene_id__regex', '^(?:^[1-9][0-9]{0,6}$)$')), name='iki__plasmid_plasmid__addgene_id__pattern'),
        ),
        iki.AddConstraint(
            model_name='plasmid',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('vendor'), 1), name='iki__plasmid_plasmid__vendor__not_empty'),
        ),
        iki.AddConstraint(
            model_name='plasmid',
            constraint=iki.CheckConstraint(condition=iki.Q(('vendor__regex', '[\\r\\n]'), negated=True), name='iki__plasmid_plasmid__vendor__one_line'),
        ),
        iki.AddConstraint(
            model_name='plasmid',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('catalog_number'), 1), name='iki__plasmid_plasmid__catalog_number__not_empty'),
        ),
        iki.AddConstraint(
            model_name='plasmid',
            constraint=iki.CheckConstraint(condition=iki.Q(('catalog_number__regex', '[\\r\\n]'), negated=True), name='iki__plasmid_plasmid__catalog_number__one_line'),
        ),
        iki.AddConstraint(
            model_name='plasmid',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('depositor'), 1), name='iki__plasmid_plasmid__depositor__not_empty'),
        ),
        iki.AddConstraint(
            model_name='plasmid',
            constraint=iki.CheckConstraint(condition=iki.Q(('depositor__regex', '[\\r\\n]'), negated=True), name='iki__plasmid_plasmid__depositor__one_line'),
        ),
        iki.AddConstraint(
            model_name='plasmid',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('growth_strain'), 1), name='iki__plasmid_plasmid__growth_strain__not_empty'),
        ),
        iki.AddConstraint(
            model_name='plasmid',
            constraint=iki.CheckConstraint(condition=iki.Q(('growth_strain__regex', '[\\r\\n]'), negated=True), name='iki__plasmid_plasmid__growth_strain__one_line'),
        ),
        iki.AddConstraint(
            model_name='plasmid',
            constraint=iki.CheckConstraint(condition=iki.Q(('growth_temperature__gt', float('-inf')), ('growth_temperature__lt', float('inf'))), name='iki__plasmid_plasmid__growth_temperature__finite'),
        ),
        iki.AddConstraint(
            model_name='plasmid',
            constraint=iki.CheckConstraint(condition=iki.Q(('growth_temperature__gte', 0)), name='iki__plasmid_plasmid__growth_temperature__min'),
        ),
        iki.AddConstraint(
            model_name='plasmid',
            constraint=iki.CheckConstraint(condition=iki.Q(('growth_temperature__lte', 50)), name='iki__plasmid_plasmid__growth_temperature__max'),
        ),
        iki.AddConstraint(
            model_name='plasmid',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('mta_reference'), 1), name='iki__plasmid_plasmid__mta_reference__not_empty'),
        ),
        iki.AddConstraint(
            model_name='plasmid',
            constraint=iki.CheckConstraint(condition=iki.Q(('mta_reference__regex', '[\\r\\n]'), negated=True), name='iki__plasmid_plasmid__mta_reference__one_line'),
        ),
        iki.AddConstraint(
            model_name='plasmid',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('mta_provider'), 1), name='iki__plasmid_plasmid__mta_provider__not_empty'),
        ),
        iki.AddConstraint(
            model_name='plasmid',
            constraint=iki.CheckConstraint(condition=iki.Q(('mta_provider__regex', '[\\r\\n]'), negated=True), name='iki__plasmid_plasmid__mta_provider__one_line'),
        ),
        iki.AddConstraint(
            model_name='plasmid',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('mta_restrictions'), 1), name='iki__plasmid_plasmid__mta_restrictions__not_empty'),
        ),
        iki.AddConstraint(
            model_name='plasmid',
            constraint=iki.CheckConstraint(condition=iki.LessThanOrEqual(iki.Length('mta_restrictions'), 5000), name='iki__plasmid_plasmid__mta_restrictions__max_length'),
        ),
        iki.AddConstraint(
            model_name='plasmid',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('biosafety_approval'), 1), name='iki__plasmid_plasmid__biosafety_approval__not_empty'),
        ),
        iki.AddConstraint(
            model_name='plasmid',
            constraint=iki.CheckConstraint(condition=iki.Q(('biosafety_approval__regex', '[\\r\\n]'), negated=True), name='iki__plasmid_plasmid__biosafety_approval__one_line'),
        ),
        iki.AddField(
            model_name='plasmid_entry_hosts',
            name='item',
            field=iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__plasmid.plasmidhost_entry'),
        ),
        iki.AddConstraint(
            model_name='plasmid_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('length__gt', float('-inf')), ('length__lt', float('inf'))), name='iki__plasmid_plasmid__entry__length__finite'),
        ),
        iki.AddConstraint(
            model_name='plasmid_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('length__gte', 0)), name='iki__plasmid_plasmid__entry__length__min'),
        ),
        iki.AddConstraint(
            model_name='plasmid_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('topology__in', ['circular', 'linear'])), name='iki__plasmid_plasmid__entry__topology__values'),
        ),
        iki.AddConstraint(
            model_name='plasmid_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('copy_number__in', ['high', 'low', 'single'])), name='iki__plasmid_plasmid__entry__copy_number__values'),
        ),
        iki.AddConstraint(
            model_name='plasmid_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('biosafety_level__in', ['bsl1', 'bsl2', 'bsl3', 'bsl4'])), name='iki__plasmid_plasmid__entry__biosafety_level__values'),
        ),
        iki.AddConstraint(
            model_name='assembly_inserts',
            constraint=iki.CheckConstraint(condition=iki.Q(('position__lt', 5)), name='iki__plasmid_assembly__inserts__position__max_items'),
        ),
        iki.AddConstraint(
            model_name='plasmid_entry_hosts',
            constraint=iki.CheckConstraint(condition=iki.Q(('position__lt', 20)), name='iki__plasmid_plasmid__entry__hosts__position__max_items'),
        ),
    ]
