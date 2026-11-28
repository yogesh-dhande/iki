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
            name='Duplex_Entry',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('duplex_length', iki.QuantityField(db_index=True, null=True, unit='{bp}')),
                ('mismatches', iki.BigIntegerField(null=True)),
                ('sense_overhang_5', iki.BigIntegerField(null=True)),
                ('sense_overhang_3', iki.BigIntegerField(null=True)),
                ('antisense_overhang_5', iki.BigIntegerField(null=True)),
                ('antisense_overhang_3', iki.BigIntegerField(null=True)),
                ('pairing', iki.JSONField(null=True)),
                ('molecular_formula', iki.CharField(max_length=1000, null=True)),
                ('molecular_weight', iki.QuantityField(db_index=True, null=True, unit='g/mol')),
                ('molecular_weight_sodium', iki.QuantityField(null=True, unit='g/mol')),
            ],
            table='iki__oligo_duplex__entry',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='Duplex_Entry_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('strands', iki.JSONField(null=True)),
                ('duplex_length', iki.QuantityField(null=True, unit='{bp}')),
                ('mismatches', iki.BigIntegerField(null=True)),
                ('sense_overhang_5', iki.BigIntegerField(null=True)),
                ('sense_overhang_3', iki.BigIntegerField(null=True)),
                ('antisense_overhang_5', iki.BigIntegerField(null=True)),
                ('antisense_overhang_3', iki.BigIntegerField(null=True)),
                ('pairing', iki.JSONField(null=True)),
                ('molecular_formula', iki.TextField(null=True)),
                ('molecular_weight', iki.QuantityField(null=True, unit='g/mol')),
                ('molecular_weight_sodium', iki.QuantityField(null=True, unit='g/mol')),
            ],
            table='iki__oligo_duplex__entry__history',
        ),
        iki.AppendOnly(
            model_name='Duplex_Entry_History',
        ),
        iki.CreateModel(
            name='Duplex_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('name', iki.TextField(null=True)),
                ('description', iki.TextField(null=True)),
            ],
            table='iki__oligo_duplex__history',
        ),
        iki.AppendOnly(
            model_name='Duplex_History',
        ),
        iki.CreateModel(
            name='Monomer',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('description', iki.TextField(null=True)),
            ],
            table='iki__oligo_monomer',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='Monomer_Entry',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('polymer_type', iki.TextField(db_index=True)),
                ('symbol', iki.CharField(db_index=True, max_length=100)),
                ('name', iki.CharField(db_index=True, max_length=255, null=True)),
                ('role', iki.TextField(db_index=True)),
                ('natural_analogue', iki.CharField(db_index=True, max_length=10, null=True)),
                ('smiles', iki.CharField(max_length=20000)),
                ('extinction_260', iki.QuantityField(null=True, unit='L/(mol.cm)')),
                ('extinction_source', iki.CharField(max_length=500, null=True)),
                ('source', iki.CharField(max_length=500, null=True)),
                ('structure', iki.CharField(db_index=True, max_length=64, null=True)),
                ('attachments', iki.ArrayField(base_field=iki.CharField(max_length=10), null=True)),
                ('molecular_formula', iki.CharField(max_length=200, null=True)),
                ('molecular_weight', iki.QuantityField(null=True, unit='g/mol')),
            ],
            table='iki__oligo_monomer__entry',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='Monomer_Entry_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('polymer_type', iki.TextField(null=True)),
                ('symbol', iki.TextField(null=True)),
                ('name', iki.TextField(null=True)),
                ('role', iki.TextField(null=True)),
                ('natural_analogue', iki.TextField(null=True)),
                ('smiles', iki.TextField(null=True)),
                ('extinction_260', iki.QuantityField(null=True, unit='L/(mol.cm)')),
                ('extinction_source', iki.TextField(null=True)),
                ('source', iki.TextField(null=True)),
                ('structure', iki.TextField(null=True)),
                ('attachments', iki.ArrayField(base_field=iki.TextField(), null=True)),
                ('molecular_formula', iki.TextField(null=True)),
                ('molecular_weight', iki.QuantityField(null=True, unit='g/mol')),
            ],
            table='iki__oligo_monomer__entry__history',
        ),
        iki.AppendOnly(
            model_name='Monomer_Entry_History',
        ),
        iki.CreateModel(
            name='Monomer_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('description', iki.TextField(null=True)),
            ],
            table='iki__oligo_monomer__history',
        ),
        iki.AppendOnly(
            model_name='Monomer_History',
        ),
        iki.CreateModel(
            name='Oligonucleotide',
            fields=[
                ('sequence_ptr', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to='iki__core.sequence')),
                ('purpose', iki.TextField(db_index=True, null=True)),
            ],
            table='iki__oligo_oligonucleotide',
            bases=('iki__core.sequence',),
        ),
        iki.CreateModel(
            name='Oligonucleotide_Entry',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('helm', iki.CharField(max_length=100000)),
                ('bases', iki.CharField(db_index=True, max_length=600, null=True)),
                ('length', iki.QuantityField(db_index=True, null=True, unit='{nt}')),
                ('molecular_formula', iki.CharField(db_index=True, max_length=500, null=True)),
                ('molecular_weight', iki.QuantityField(db_index=True, null=True, unit='g/mol')),
                ('molecular_weight_sodium', iki.QuantityField(null=True, unit='g/mol')),
                ('monoisotopic_mass', iki.QuantityField(null=True, unit='u')),
                ('extinction_260', iki.QuantityField(null=True, unit='L/(mol.cm)')),
                ('extinction_note', iki.TextField(null=True)),
                ('modifications', iki.ArrayField(base_field=iki.CharField(max_length=300), null=True)),
                ('positions', iki.JSONField(null=True)),
            ],
            table='iki__oligo_oligonucleotide__entry',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='Oligonucleotide_Entry_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('helm', iki.TextField(null=True)),
                ('monomers', iki.ArrayField(base_field=iki.UUIDField(), null=True)),
                ('bases', iki.TextField(null=True)),
                ('length', iki.QuantityField(null=True, unit='{nt}')),
                ('molecular_formula', iki.TextField(null=True)),
                ('molecular_weight', iki.QuantityField(null=True, unit='g/mol')),
                ('molecular_weight_sodium', iki.QuantityField(null=True, unit='g/mol')),
                ('monoisotopic_mass', iki.QuantityField(null=True, unit='u')),
                ('extinction_260', iki.QuantityField(null=True, unit='L/(mol.cm)')),
                ('extinction_note', iki.TextField(null=True)),
                ('modifications', iki.ArrayField(base_field=iki.TextField(), null=True)),
                ('positions', iki.JSONField(null=True)),
            ],
            table='iki__oligo_oligonucleotide__entry__history',
        ),
        iki.AppendOnly(
            model_name='Oligonucleotide_Entry_History',
        ),
        iki.CreateModel(
            name='Oligonucleotide_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('purpose', iki.TextField(null=True)),
            ],
            table='iki__oligo_oligonucleotide__history',
        ),
        iki.AppendOnly(
            model_name='Oligonucleotide_History',
        ),
        iki.CreateModel(
            name='Duplex',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('name', iki.CharField(db_index=True, max_length=255, null=True)),
                ('description', iki.TextField(null=True)),
            ],
            table='iki__oligo_duplex',
            indexes=[
                iki.GinIndex(fields=['name'], name='name_trgm_da74b11511895d4856fd', opclasses=['gin_trgm_ops']),
                iki.GinIndex(iki.SearchVector(iki.Left('description', 100000), config='simple'), name='descriptio_fts_f090998df213818'),
            ],
            constraints=[
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('name'), 1), name='iki__oligo_duplex__name__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('name__regex', '[\\r\\n]'), negated=True), name='iki__oligo_duplex__name__one_line'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('description'), 1), name='iki__oligo_duplex__description__not_empty'),
                iki.CheckConstraint(condition=iki.LessThanOrEqual(iki.Length('description'), 10000), name='iki__oligo_duplex__description__max_length'),
            ],
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='Duplex_Entry_strands',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity', 'position')),
                ('entity', iki.ForeignKey(db_column='entity_id', on_delete=iki.CASCADE, related_name='+', to='iki__oligo.duplex_entry')),
                ('position', iki.PositiveIntegerField()),
                ('role', iki.TextField()),
            ],
            table='iki__oligo_duplex__entry__strands',
        ),
        iki.AddConstraint(
            model_name='duplex_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('duplex_length__gt', float('-inf')), ('duplex_length__lt', float('inf'))), name='iki__oligo_duplex__entry__duplex_length__finite'),
        ),
        iki.AddConstraint(
            model_name='duplex_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('duplex_length__gte', 0)), name='iki__oligo_duplex__entry__duplex_length__min'),
        ),
        iki.AddConstraint(
            model_name='duplex_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('mismatches__gte', 0)), name='iki__oligo_duplex__entry__mismatches__min'),
        ),
        iki.AddConstraint(
            model_name='duplex_entry',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('molecular_formula'), 1), name='iki__oligo_duplex__entry__molecular_formula__not_empty'),
        ),
        iki.AddConstraint(
            model_name='duplex_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('molecular_formula__regex', '[\\r\\n]'), negated=True), name='iki__oligo_duplex__entry__molecular_formula__one_line'),
        ),
        iki.AddConstraint(
            model_name='duplex_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('molecular_weight__gt', float('-inf')), ('molecular_weight__lt', float('inf'))), name='iki__oligo_duplex__entry__molecular_weight__finite'),
        ),
        iki.AddConstraint(
            model_name='duplex_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('molecular_weight__gte', 0)), name='iki__oligo_duplex__entry__molecular_weight__min'),
        ),
        iki.AddConstraint(
            model_name='duplex_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('molecular_weight_sodium__gt', float('-inf')), ('molecular_weight_sodium__lt', float('inf'))), name='iki__oligo_duplex__entry__molecular_weight_sodium__finite'),
        ),
        iki.AddConstraint(
            model_name='duplex_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('molecular_weight_sodium__gte', 0)), name='iki__oligo_duplex__entry__molecular_weight_sodium__min'),
        ),
        iki.AddIndex(
            model_name='monomer',
            index=iki.GinIndex(iki.SearchVector(iki.Left('description', 100000), config='simple'), name='descriptio_fts_0f1ac63a2aca53f'),
        ),
        iki.AddConstraint(
            model_name='monomer',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('description'), 1), name='iki__oligo_monomer__description__not_empty'),
        ),
        iki.AddConstraint(
            model_name='monomer',
            constraint=iki.CheckConstraint(condition=iki.LessThanOrEqual(iki.Length('description'), 10000), name='iki__oligo_monomer__description__max_length'),
        ),
        iki.AddIndex(
            model_name='monomer_entry',
            index=iki.GinIndex(fields=['symbol'], name='symbol_trgm_9ae3532561dcfc525a', opclasses=['gin_trgm_ops']),
        ),
        iki.AddIndex(
            model_name='monomer_entry',
            index=iki.GinIndex(fields=['name'], name='name_trgm_0a0949c794ab6c196ec6', opclasses=['gin_trgm_ops']),
        ),
        iki.AddIndex(
            model_name='monomer_entry',
            index=iki.GinIndex(fields=['natural_analogue'], name='natural_an_trgm_d03dff9e401336', opclasses=['gin_trgm_ops']),
        ),
        iki.AddIndex(
            model_name='monomer_entry',
            index=iki.GinIndex(fields=['structure'], name='structure_trgm_c0aded94adef74a', opclasses=['gin_trgm_ops']),
        ),
        iki.AddConstraint(
            model_name='monomer_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('polymer_type__in', ['RNA', 'CHEM', 'PEPTIDE'])), name='iki__oligo_monomer__entry__polymer_type__values'),
        ),
        iki.AddConstraint(
            model_name='monomer_entry',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('symbol'), 1), name='iki__oligo_monomer__entry__symbol__not_empty'),
        ),
        iki.AddConstraint(
            model_name='monomer_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('symbol__regex', '[\\r\\n]'), negated=True), name='iki__oligo_monomer__entry__symbol__one_line'),
        ),
        iki.AddConstraint(
            model_name='monomer_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('symbol__regex', '^(?:^[^\\s$|{}.,:+"\'()\\[\\]*]+$)$')), name='iki__oligo_monomer__entry__symbol__pattern'),
        ),
        iki.AddConstraint(
            model_name='monomer_entry',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('name'), 1), name='iki__oligo_monomer__entry__name__not_empty'),
        ),
        iki.AddConstraint(
            model_name='monomer_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('name__regex', '[\\r\\n]'), negated=True), name='iki__oligo_monomer__entry__name__one_line'),
        ),
        iki.AddConstraint(
            model_name='monomer_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('name__regex', '^(?:^[A-Za-z0-9][^\\t\\r\\n]*$)$')), name='iki__oligo_monomer__entry__name__pattern'),
        ),
        iki.AddConstraint(
            model_name='monomer_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('role__in', ['sugar', 'base', 'linker', 'modifier'])), name='iki__oligo_monomer__entry__role__values'),
        ),
        iki.AddConstraint(
            model_name='monomer_entry',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('natural_analogue'), 1), name='iki__oligo_monomer__entry__natural_analogue__not_empty'),
        ),
        iki.AddConstraint(
            model_name='monomer_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('natural_analogue__regex', '[\\r\\n]'), negated=True), name='iki__oligo_monomer__entry__natural_analogue__one_line'),
        ),
        iki.AddConstraint(
            model_name='monomer_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('natural_analogue__regex', '^(?:^[A-Za-z0-9]+$)$')), name='iki__oligo_monomer__entry__natural_analogue__pattern'),
        ),
        iki.AddConstraint(
            model_name='monomer_entry',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('smiles'), 1), name='iki__oligo_monomer__entry__smiles__not_empty'),
        ),
        iki.AddConstraint(
            model_name='monomer_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('smiles__regex', '[\\r\\n]'), negated=True), name='iki__oligo_monomer__entry__smiles__one_line'),
        ),
        iki.AddConstraint(
            model_name='monomer_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('smiles__regex', '^(?:^[A-Za-z0-9@+\\-\\[\\]()=#$:.%/\\\\]+$)$')), name='iki__oligo_monomer__entry__smiles__pattern'),
        ),
        iki.AddConstraint(
            model_name='monomer_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('extinction_260__gt', float('-inf')), ('extinction_260__lt', float('inf'))), name='iki__oligo_monomer__entry__extinction_260__finite'),
        ),
        iki.AddConstraint(
            model_name='monomer_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('extinction_260__gte', 0)), name='iki__oligo_monomer__entry__extinction_260__min'),
        ),
        iki.AddConstraint(
            model_name='monomer_entry',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('extinction_source'), 1), name='iki__oligo_monomer__entry__extinction_source__not_empty'),
        ),
        iki.AddConstraint(
            model_name='monomer_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('extinction_source__regex', '[\\r\\n]'), negated=True), name='iki__oligo_monomer__entry__extinction_source__one_line'),
        ),
        iki.AddConstraint(
            model_name='monomer_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('extinction_source__regex', '^(?:^[A-Za-z0-9][^\\t\\r\\n]*$)$')), name='iki__oligo_monomer__entry__extinction_source__pattern'),
        ),
        iki.AddConstraint(
            model_name='monomer_entry',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('source'), 1), name='iki__oligo_monomer__entry__source__not_empty'),
        ),
        iki.AddConstraint(
            model_name='monomer_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('source__regex', '[\\r\\n]'), negated=True), name='iki__oligo_monomer__entry__source__one_line'),
        ),
        iki.AddConstraint(
            model_name='monomer_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('source__regex', '^(?:^[A-Za-z0-9][^\\t\\r\\n]*$)$')), name='iki__oligo_monomer__entry__source__pattern'),
        ),
        iki.AddConstraint(
            model_name='monomer_entry',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('structure'), 1), name='iki__oligo_monomer__entry__structure__not_empty'),
        ),
        iki.AddConstraint(
            model_name='monomer_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('structure__regex', '[\\r\\n]'), negated=True), name='iki__oligo_monomer__entry__structure__one_line'),
        ),
        iki.AddConstraint(
            model_name='monomer_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('structure__regex', '^(?:^[0-9a-f]{64}$)$')), name='iki__oligo_monomer__entry__structure__pattern'),
        ),
        iki.AddConstraint(
            model_name='monomer_entry',
            constraint=iki.CheckConstraint(condition=iki.IsPlainList('attachments'), name='iki__oligo_monomer__entry__attachments__plain_list'),
        ),
        iki.AddConstraint(
            model_name='monomer_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('attachments__len__lte', 10)), name='iki__oligo_monomer__entry__attachments__max_items'),
        ),
        iki.AddConstraint(
            model_name='monomer_entry',
            constraint=iki.CheckConstraint(condition=iki.AllLengthsBetween('attachments', 1, None), name='iki__oligo_monomer__entry__attachments__item_length'),
        ),
        iki.AddConstraint(
            model_name='monomer_entry',
            constraint=iki.CheckConstraint(condition=iki.NoneMatch('attachments', '[\\r\\n]'), name='iki__oligo_monomer__entry__attachments__item_one_line'),
        ),
        iki.AddConstraint(
            model_name='monomer_entry',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('molecular_formula'), 1), name='iki__oligo_monomer__entry__molecular_formula__not_empty'),
        ),
        iki.AddConstraint(
            model_name='monomer_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('molecular_formula__regex', '[\\r\\n]'), negated=True), name='iki__oligo_monomer__entry__molecular_formula__one_line'),
        ),
        iki.AddConstraint(
            model_name='monomer_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('molecular_weight__gt', float('-inf')), ('molecular_weight__lt', float('inf'))), name='iki__oligo_monomer__entry__molecular_weight__finite'),
        ),
        iki.AddConstraint(
            model_name='monomer_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('molecular_weight__gte', 0)), name='iki__oligo_monomer__entry__molecular_weight__min'),
        ),
        iki.AddConstraint(
            model_name='oligonucleotide',
            constraint=iki.CheckConstraint(condition=iki.Q(('purpose__in', ['antisense', 'siRNA-strand', 'primer', 'probe', 'aptamer', 'other'])), name='iki__oligo_oligonucleotide__purpose__values'),
        ),
        iki.CreateModel(
            name='Oligonucleotide_Entry_monomers',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity', 'position')),
                ('entity', iki.ForeignKey(db_column='entity_id', on_delete=iki.CASCADE, related_name='+', to='iki__oligo.oligonucleotide_entry')),
                ('position', iki.PositiveIntegerField()),
            ],
            table='iki__oligo_oligonucleotide__entry__monomers',
        ),
        iki.AddIndex(
            model_name='oligonucleotide_entry',
            index=iki.GinIndex(fields=['bases'], name='bases_trgm_dd899d4765568698657', opclasses=['gin_trgm_ops']),
        ),
        iki.AddIndex(
            model_name='oligonucleotide_entry',
            index=iki.GinIndex(fields=['molecular_formula'], name='molecular_trgm_c26a7f86080ab44', opclasses=['gin_trgm_ops']),
        ),
        iki.AddIndex(
            model_name='oligonucleotide_entry',
            index=iki.GinIndex(iki.SearchVector(iki.Left('extinction_note', 100000), config='simple'), name='extinction_fts_0894de60ef123fb'),
        ),
        iki.AddConstraint(
            model_name='oligonucleotide_entry',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('helm'), 1), name='iki__oligo_oligonucleotide__entry__helm__not_empty'),
        ),
        iki.AddConstraint(
            model_name='oligonucleotide_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('helm__regex', '[\\r\\n]'), negated=True), name='iki__oligo_oligonucleotide__entry__helm__one_line'),
        ),
        iki.AddConstraint(
            model_name='oligonucleotide_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('helm__regex', '^(PEPTIDE|RNA|CHEM|BLOB)[0-9]+[{].*[$].*[$].*[$].*[$](V2[.]0)?$')), name='iki__oligo_oligonucleotide__entry__helm__format'),
        ),
        iki.AddConstraint(
            model_name='oligonucleotide_entry',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('bases'), 1), name='iki__oligo_oligonucleotide__entry__bases__not_empty'),
        ),
        iki.AddConstraint(
            model_name='oligonucleotide_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('bases__regex', '[\\r\\n]'), negated=True), name='iki__oligo_oligonucleotide__entry__bases__one_line'),
        ),
        iki.AddConstraint(
            model_name='oligonucleotide_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('bases__regex', '^(?:^[ACGTUN-]*$)$')), name='iki__oligo_oligonucleotide__entry__bases__pattern'),
        ),
        iki.AddConstraint(
            model_name='oligonucleotide_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('length__gt', float('-inf')), ('length__lt', float('inf'))), name='iki__oligo_oligonucleotide__entry__length__finite'),
        ),
        iki.AddConstraint(
            model_name='oligonucleotide_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('length__gte', 0)), name='iki__oligo_oligonucleotide__entry__length__min'),
        ),
        iki.AddConstraint(
            model_name='oligonucleotide_entry',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('molecular_formula'), 1), name='iki__oligo_oligonucleotide__entry__molecular_formula__not_empty'),
        ),
        iki.AddConstraint(
            model_name='oligonucleotide_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('molecular_formula__regex', '[\\r\\n]'), negated=True), name='iki__oligo_oligonucleotide__entry__molecular_formula__one_line'),
        ),
        iki.AddConstraint(
            model_name='oligonucleotide_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('molecular_weight__gt', float('-inf')), ('molecular_weight__lt', float('inf'))), name='iki__oligo_oligonucleotide__entry__molecular_weight__finite'),
        ),
        iki.AddConstraint(
            model_name='oligonucleotide_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('molecular_weight__gte', 0)), name='iki__oligo_oligonucleotide__entry__molecular_weight__min'),
        ),
        iki.AddConstraint(
            model_name='oligonucleotide_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('molecular_weight_sodium__gt', float('-inf')), ('molecular_weight_sodium__lt', float('inf'))), name='iki__oligo_oligonucleotide__entry__molecular_weight__aca98c6326'),
        ),
        iki.AddConstraint(
            model_name='oligonucleotide_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('molecular_weight_sodium__gte', 0)), name='iki__oligo_oligonucleotide__entry__molecular_weight_sodium__min'),
        ),
        iki.AddConstraint(
            model_name='oligonucleotide_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('monoisotopic_mass__gt', float('-inf')), ('monoisotopic_mass__lt', float('inf'))), name='iki__oligo_oligonucleotide__entry__monoisotopic_mass__finite'),
        ),
        iki.AddConstraint(
            model_name='oligonucleotide_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('monoisotopic_mass__gte', 0)), name='iki__oligo_oligonucleotide__entry__monoisotopic_mass__min'),
        ),
        iki.AddConstraint(
            model_name='oligonucleotide_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('extinction_260__gt', float('-inf')), ('extinction_260__lt', float('inf'))), name='iki__oligo_oligonucleotide__entry__extinction_260__finite'),
        ),
        iki.AddConstraint(
            model_name='oligonucleotide_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('extinction_260__gte', 0)), name='iki__oligo_oligonucleotide__entry__extinction_260__min'),
        ),
        iki.AddConstraint(
            model_name='oligonucleotide_entry',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('extinction_note'), 1), name='iki__oligo_oligonucleotide__entry__extinction_note__not_empty'),
        ),
        iki.AddConstraint(
            model_name='oligonucleotide_entry',
            constraint=iki.CheckConstraint(condition=iki.LessThanOrEqual(iki.Length('extinction_note'), 2000), name='iki__oligo_oligonucleotide__entry__extinction_note__max_length'),
        ),
        iki.AddConstraint(
            model_name='oligonucleotide_entry',
            constraint=iki.CheckConstraint(condition=iki.IsPlainList('modifications'), name='iki__oligo_oligonucleotide__entry__modifications__plain_list'),
        ),
        iki.AddConstraint(
            model_name='oligonucleotide_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('modifications__len__lte', 200)), name='iki__oligo_oligonucleotide__entry__modifications__max_items'),
        ),
        iki.AddConstraint(
            model_name='oligonucleotide_entry',
            constraint=iki.CheckConstraint(condition=iki.AllLengthsBetween('modifications', 1, None), name='iki__oligo_oligonucleotide__entry__modifications__item_length'),
        ),
        iki.AddConstraint(
            model_name='oligonucleotide_entry',
            constraint=iki.CheckConstraint(condition=iki.NoneMatch('modifications', '[\\r\\n]'), name='iki__oligo_oligonucleotide__entry__modifications__item_one_line'),
        ),
        iki.AddField(
            model_name='duplex_entry_strands',
            name='strand',
            field=iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__oligo.oligonucleotide_entry'),
        ),
        iki.AddField(
            model_name='oligonucleotide_entry_monomers',
            name='item',
            field=iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__oligo.monomer_entry'),
        ),
        iki.AddConstraint(
            model_name='duplex_entry_strands',
            constraint=iki.CheckConstraint(condition=iki.Q(('role__in', ['sense', 'antisense'])), name='iki__oligo_duplex__entry__strands__role__values'),
        ),
        iki.AddConstraint(
            model_name='duplex_entry_strands',
            constraint=iki.CheckConstraint(condition=iki.Q(('position__lt', 2)), name='iki__oligo_duplex__entry__strands__position__max_items'),
        ),
        iki.AddConstraint(
            model_name='oligonucleotide_entry_monomers',
            constraint=iki.CheckConstraint(condition=iki.Q(('position__lt', 500)), name='iki__oligo_oligonucleotide__entry__monomers__positi__233baa3555'),
        ),
    ]
