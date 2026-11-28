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
            name='Protein_Entry_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('sequence', iki.TextField(null=True)),
                ('length', iki.QuantityField(null=True, unit='{aa}')),
                ('molecular_weight', iki.QuantityField(null=True, unit='u')),
                ('isoelectric_point', iki.FloatField(null=True)),
                ('extinction_coefficient_oxidized', iki.QuantityField(null=True, unit='L/(mol.cm)')),
                ('extinction_coefficient_reduced', iki.QuantityField(null=True, unit='L/(mol.cm)')),
                ('gravy', iki.FloatField(null=True)),
                ('hydropathy', iki.JSONField(null=True)),
                ('instability_index', iki.FloatField(null=True)),
            ],
            table='iki__protein_protein__entry__history',
        ),
        iki.AppendOnly(
            model_name='Protein_Entry_History',
        ),
        iki.CreateModel(
            name='Protein_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('uniprot_accession', iki.TextField(null=True)),
                ('gene', iki.TextField(null=True)),
                ('encoded_by', iki.UUIDField(db_column='encoded_by_id', null=True)),
            ],
            table='iki__protein_protein__history',
        ),
        iki.AppendOnly(
            model_name='Protein_History',
        ),
        iki.CreateModel(
            name='ProteinStructure_Entry',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('pdb_id', iki.CharField(db_index=True, max_length=12, null=True)),
                ('method', iki.TextField(db_index=True, null=True)),
                ('resolution', iki.QuantityField(db_index=True, null=True, unit='Ao')),
                ('chain_ids', iki.ArrayField(base_field=iki.CharField(max_length=4), null=True)),
                ('released', iki.DateField(null=True)),
                ('secondary_structure', iki.ArrayField(base_field=iki.TextField(), null=True)),
                ('helix_fraction', iki.QuantityField(db_index=True, null=True, unit='%')),
                ('strand_fraction', iki.QuantityField(db_index=True, null=True, unit='%')),
                ('accessible_surface_area', iki.QuantityField(null=True, unit='Ao2')),
                ('dssp', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to=iki.FILE)),
                ('structure', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to=iki.FILE)),
            ],
            table='iki__protein_proteinstructure__entry',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='ProteinStructure_Entry_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('structure', iki.UUIDField(db_column='structure_id', null=True)),
                ('pdb_id', iki.TextField(null=True)),
                ('method', iki.TextField(null=True)),
                ('resolution', iki.QuantityField(null=True, unit='Ao')),
                ('resolutions', iki.JSONField(null=True)),
                ('chains', iki.ArrayField(base_field=iki.UUIDField(), null=True)),
                ('chain_ids', iki.ArrayField(base_field=iki.TextField(), null=True)),
                ('released', iki.DateField(null=True)),
                ('secondary_structure', iki.ArrayField(base_field=iki.TextField(), null=True)),
                ('helix_fraction', iki.QuantityField(null=True, unit='%')),
                ('strand_fraction', iki.QuantityField(null=True, unit='%')),
                ('accessible_surface_area', iki.QuantityField(null=True, unit='Ao2')),
                ('dssp', iki.UUIDField(db_column='dssp_id', null=True)),
            ],
            table='iki__protein_proteinstructure__entry__history',
        ),
        iki.AppendOnly(
            model_name='ProteinStructure_Entry_History',
        ),
        iki.CreateModel(
            name='ProteinStructure_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('name', iki.TextField(null=True)),
                ('description', iki.TextField(null=True)),
            ],
            table='iki__protein_proteinstructure__history',
        ),
        iki.AppendOnly(
            model_name='ProteinStructure_History',
        ),
        iki.CreateModel(
            name='Protein_Entry',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('sequence', iki.TextField()),
                ('length', iki.QuantityField(db_index=True, null=True, unit='{aa}')),
                ('molecular_weight', iki.QuantityField(db_index=True, null=True, unit='u')),
                ('isoelectric_point', iki.FloatField(db_index=True, null=True)),
                ('extinction_coefficient_oxidized', iki.QuantityField(null=True, unit='L/(mol.cm)')),
                ('extinction_coefficient_reduced', iki.QuantityField(null=True, unit='L/(mol.cm)')),
                ('gravy', iki.FloatField(null=True)),
                ('hydropathy', iki.JSONField(null=True)),
                ('instability_index', iki.FloatField(null=True)),
            ],
            table='iki__protein_protein__entry',
            constraints=[
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('sequence'), 1), name='iki__protein_protein__entry__sequence__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('sequence__regex', '[\\r\\n]'), negated=True), name='iki__protein_protein__entry__sequence__one_line'),
                iki.CheckConstraint(condition=iki.Q(('sequence__regex', '^[ACDEFGHIKLMNPQRSTVWYBZXUOJacdefghiklmnpqrstvwybzxuoj*]*$')), name='iki__protein_protein__entry__sequence__format'),
                iki.CheckConstraint(condition=iki.Q(('length__gt', float('-inf')), ('length__lt', float('inf'))), name='iki__protein_protein__entry__length__finite'),
                iki.CheckConstraint(condition=iki.Q(('molecular_weight__gt', float('-inf')), ('molecular_weight__lt', float('inf'))), name='iki__protein_protein__entry__molecular_weight__finite'),
                iki.CheckConstraint(condition=iki.Q(('molecular_weight__gte', 0)), name='iki__protein_protein__entry__molecular_weight__min'),
                iki.CheckConstraint(condition=iki.Q(('isoelectric_point__gt', float('-inf')), ('isoelectric_point__lt', float('inf'))), name='iki__protein_protein__entry__isoelectric_point__finite'),
                iki.CheckConstraint(condition=iki.Q(('isoelectric_point__gte', 0)), name='iki__protein_protein__entry__isoelectric_point__min'),
                iki.CheckConstraint(condition=iki.Q(('isoelectric_point__lte', 14)), name='iki__protein_protein__entry__isoelectric_point__max'),
                iki.CheckConstraint(condition=iki.Q(('extinction_coefficient_oxidized__gt', float('-inf')), ('extinction_coefficient_oxidized__lt', float('inf'))), name='iki__protein_protein__entry__extinction_coefficient__2c4e7af8ea'),
                iki.CheckConstraint(condition=iki.Q(('extinction_coefficient_oxidized__gte', 0)), name='iki__protein_protein__entry__extinction_coefficient__9663c38afa'),
                iki.CheckConstraint(condition=iki.Q(('extinction_coefficient_reduced__gt', float('-inf')), ('extinction_coefficient_reduced__lt', float('inf'))), name='iki__protein_protein__entry__extinction_coefficient__a8e430bc75'),
                iki.CheckConstraint(condition=iki.Q(('extinction_coefficient_reduced__gte', 0)), name='iki__protein_protein__entry__extinction_coefficient__21d175990f'),
                iki.CheckConstraint(condition=iki.Q(('gravy__gt', float('-inf')), ('gravy__lt', float('inf'))), name='iki__protein_protein__entry__gravy__finite'),
                iki.CheckConstraint(condition=iki.Q(('instability_index__gt', float('-inf')), ('instability_index__lt', float('inf'))), name='iki__protein_protein__entry__instability_index__finite'),
            ],
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='ProteinStructure',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('name', iki.CharField(db_index=True, max_length=255, null=True)),
                ('description', iki.TextField(null=True)),
            ],
            table='iki__protein_proteinstructure',
            indexes=[
                iki.GinIndex(fields=['name'], name='name_trgm_3d582283ae93059c3384', opclasses=['gin_trgm_ops']),
                iki.GinIndex(iki.SearchVector(iki.Left('description', 100000), config='simple'), name='descriptio_fts_7029d30765ec97f'),
            ],
            constraints=[
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('name'), 1), name='iki__protein_proteinstructure__name__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('name__regex', '[\\r\\n]'), negated=True), name='iki__protein_proteinstructure__name__one_line'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('description'), 1), name='iki__protein_proteinstructure__description__not_empty'),
                iki.CheckConstraint(condition=iki.LessThanOrEqual(iki.Length('description'), 10000), name='iki__protein_proteinstructure__description__max_length'),
            ],
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='Protein',
            fields=[
                ('sequence_ptr', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to='iki__core.sequence')),
                ('uniprot_accession', iki.CharField(db_index=True, max_length=20, null=True)),
                ('gene', iki.CharField(db_index=True, max_length=100, null=True)),
                ('encoded_by', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to='iki__dna.dnasequence_entry')),
            ],
            table='iki__protein_protein',
            indexes=[
                iki.GinIndex(fields=['uniprot_accession'], name='uniprot_ac_trgm_9b3b4ba5fa9062', opclasses=['gin_trgm_ops']),
                iki.GinIndex(fields=['gene'], name='gene_trgm_8bd0b30d217af9b5cd3e', opclasses=['gin_trgm_ops']),
            ],
            constraints=[
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('uniprot_accession'), 1), name='iki__protein_protein__uniprot_accession__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('uniprot_accession__regex', '[\\r\\n]'), negated=True), name='iki__protein_protein__uniprot_accession__one_line'),
                iki.CheckConstraint(condition=iki.Q(('uniprot_accession__regex', '^(?:^([OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9]([A-Z][A-Z0-9]{2}[0-9]){1,2})(-[0-9]+)?$)$')), name='iki__protein_protein__uniprot_accession__pattern'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('gene'), 1), name='iki__protein_protein__gene__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('gene__regex', '[\\r\\n]'), negated=True), name='iki__protein_protein__gene__one_line'),
            ],
            bases=('iki__core.sequence',),
        ),
        iki.CreateModel(
            name='ProteinStructure_Entry_chains',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity', 'position')),
                ('entity', iki.ForeignKey(db_column='entity_id', on_delete=iki.CASCADE, related_name='+', to='iki__protein.proteinstructure_entry')),
                ('position', iki.PositiveIntegerField()),
                ('item', iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__protein.protein_entry')),
            ],
            table='iki__protein_proteinstructure__entry__chains',
        ),
        iki.CreateModel(
            name='ProteinStructure_Entry_resolutions',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity', 'position')),
                ('entity', iki.ForeignKey(db_column='entity_id', on_delete=iki.CASCADE, related_name='+', to='iki__protein.proteinstructure_entry')),
                ('position', iki.PositiveIntegerField()),
                ('method', iki.TextField()),
                ('resolution', iki.QuantityField(null=True, unit='Ao')),
            ],
            table='iki__protein_proteinstructure__entry__resolutions',
            constraints=[
                iki.CheckConstraint(condition=iki.Q(('method__in', ['xray', 'nmr', 'cryo_em', 'electron_crystallography', 'neutron', 'predicted', 'other'])), name='iki__protein_proteinstructure__entry__resolutions____97a8864fde'),
                iki.CheckConstraint(condition=iki.Q(('resolution__gt', float('-inf')), ('resolution__lt', float('inf'))), name='iki__protein_proteinstructure__entry__resolutions____8a09c77485'),
                iki.CheckConstraint(condition=iki.Q(('resolution__gte', 0)), name='iki__protein_proteinstructure__entry__resolutions____f247cda886'),
                iki.CheckConstraint(condition=iki.Q(('position__lt', 20)), name='iki__protein_proteinstructure__entry__resolutions____d1167100e1'),
            ],
        ),
        iki.AddIndex(
            model_name='proteinstructure_entry',
            index=iki.GinIndex(fields=['pdb_id'], name='pdb_id_trgm_18dad746af5f4ba2f6', opclasses=['gin_trgm_ops']),
        ),
        iki.AddConstraint(
            model_name='proteinstructure_entry',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('pdb_id'), 1), name='iki__protein_proteinstructure__entry__pdb_id__not_empty'),
        ),
        iki.AddConstraint(
            model_name='proteinstructure_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('pdb_id__regex', '[\\r\\n]'), negated=True), name='iki__protein_proteinstructure__entry__pdb_id__one_line'),
        ),
        iki.AddConstraint(
            model_name='proteinstructure_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('pdb_id__regex', '^(?:^([0-9][A-Z0-9]{3}|PDB_[0-9A-Z]{8})$)$')), name='iki__protein_proteinstructure__entry__pdb_id__pattern'),
        ),
        iki.AddConstraint(
            model_name='proteinstructure_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('method__in', ['xray', 'nmr', 'cryo_em', 'electron_crystallography', 'neutron', 'predicted', 'other'])), name='iki__protein_proteinstructure__entry__method__values'),
        ),
        iki.AddConstraint(
            model_name='proteinstructure_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('resolution__gt', float('-inf')), ('resolution__lt', float('inf'))), name='iki__protein_proteinstructure__entry__resolution__finite'),
        ),
        iki.AddConstraint(
            model_name='proteinstructure_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('resolution__gte', 0)), name='iki__protein_proteinstructure__entry__resolution__min'),
        ),
        iki.AddConstraint(
            model_name='proteinstructure_entry',
            constraint=iki.CheckConstraint(condition=iki.IsPlainList('chain_ids'), name='iki__protein_proteinstructure__entry__chain_ids__plain_list'),
        ),
        iki.AddConstraint(
            model_name='proteinstructure_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('chain_ids__len__lte', 10000)), name='iki__protein_proteinstructure__entry__chain_ids__max_items'),
        ),
        iki.AddConstraint(
            model_name='proteinstructure_entry',
            constraint=iki.CheckConstraint(condition=iki.AllLengthsBetween('chain_ids', 1, None), name='iki__protein_proteinstructure__entry__chain_ids__item_length'),
        ),
        iki.AddConstraint(
            model_name='proteinstructure_entry',
            constraint=iki.CheckConstraint(condition=iki.NoneMatch('chain_ids', '[\\r\\n]'), name='iki__protein_proteinstructure__entry__chain_ids__item_one_line'),
        ),
        iki.AddConstraint(
            model_name='proteinstructure_entry',
            constraint=iki.CheckConstraint(condition=iki.IsPlainList('secondary_structure'), name='iki__protein_proteinstructure__entry__secondary_str__493dbd2a33'),
        ),
        iki.AddConstraint(
            model_name='proteinstructure_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('secondary_structure__len__lte', 10000)), name='iki__protein_proteinstructure__entry__secondary_str__755a7dc87d'),
        ),
        iki.AddConstraint(
            model_name='proteinstructure_entry',
            constraint=iki.CheckConstraint(condition=iki.AllLengthsBetween('secondary_structure', 1, None), name='iki__protein_proteinstructure__entry__secondary_str__e81c0a3dfd'),
        ),
        iki.AddConstraint(
            model_name='proteinstructure_entry',
            constraint=iki.CheckConstraint(condition=iki.NoneMatch('secondary_structure', '[\\r\\n]'), name='iki__protein_proteinstructure__entry__secondary_str__ceccd1f1e9'),
        ),
        iki.AddConstraint(
            model_name='proteinstructure_entry',
            constraint=iki.CheckConstraint(condition=iki.AllMatch('secondary_structure', '^(?:^[HGIPEBTS?-]*$)$'), name='iki__protein_proteinstructure__entry__secondary_str__41d1e11a92'),
        ),
        iki.AddConstraint(
            model_name='proteinstructure_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('helix_fraction__gt', float('-inf')), ('helix_fraction__lt', float('inf'))), name='iki__protein_proteinstructure__entry__helix_fraction__finite'),
        ),
        iki.AddConstraint(
            model_name='proteinstructure_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('helix_fraction__gte', 0)), name='iki__protein_proteinstructure__entry__helix_fraction__min'),
        ),
        iki.AddConstraint(
            model_name='proteinstructure_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('helix_fraction__lte', 100)), name='iki__protein_proteinstructure__entry__helix_fraction__max'),
        ),
        iki.AddConstraint(
            model_name='proteinstructure_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('strand_fraction__gt', float('-inf')), ('strand_fraction__lt', float('inf'))), name='iki__protein_proteinstructure__entry__strand_fraction__finite'),
        ),
        iki.AddConstraint(
            model_name='proteinstructure_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('strand_fraction__gte', 0)), name='iki__protein_proteinstructure__entry__strand_fraction__min'),
        ),
        iki.AddConstraint(
            model_name='proteinstructure_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('strand_fraction__lte', 100)), name='iki__protein_proteinstructure__entry__strand_fraction__max'),
        ),
        iki.AddConstraint(
            model_name='proteinstructure_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('accessible_surface_area__gt', float('-inf')), ('accessible_surface_area__lt', float('inf'))), name='iki__protein_proteinstructure__entry__accessible_su__fef8656687'),
        ),
        iki.AddConstraint(
            model_name='proteinstructure_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('accessible_surface_area__gte', 0)), name='iki__protein_proteinstructure__entry__accessible_su__9bfeb3f329'),
        ),
        iki.AddConstraint(
            model_name='proteinstructure_entry_chains',
            constraint=iki.CheckConstraint(condition=iki.Q(('position__lt', 10000)), name='iki__protein_proteinstructure__entry__chains__posit__e9e4894a27'),
        ),
    ]
