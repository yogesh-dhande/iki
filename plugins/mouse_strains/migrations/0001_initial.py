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
            name='MouseAllele',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
            ],
            table='iki__mouse_strains_mouseallele',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='MouseAllele_Entry_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('mgi_id', iki.TextField(null=True)),
                ('symbol', iki.TextField(null=True)),
            ],
            table='iki__mouse_strains_mouseallele__entry__history',
        ),
        iki.AppendOnly(
            model_name='MouseAllele_Entry_History',
        ),
        iki.CreateModel(
            name='MouseAllele_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
            ],
            table='iki__mouse_strains_mouseallele__history',
        ),
        iki.AppendOnly(
            model_name='MouseAllele_History',
        ),
        iki.CreateModel(
            name='MouseCryobox',
            fields=[
                ('box_ptr', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to='iki__inventory.box')),
            ],
            table='iki__mouse_strains_mousecryobox',
            bases=('iki__inventory.box',),
        ),
        iki.CreateModel(
            name='MouseCryobox_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
            ],
            table='iki__mouse_strains_mousecryobox__history',
        ),
        iki.AppendOnly(
            model_name='MouseCryobox_History',
        ),
        iki.CreateModel(
            name='MouseCryostock',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('kind', iki.TextField(db_index=True)),
                ('carrier', iki.TextField(db_index=True)),
                ('banked_on', iki.DateField(db_index=True)),
                ('embryos_at_banking', iki.BigIntegerField(null=True)),
                ('barcode', iki.CharField(db_index=True, max_length=64, null=True)),
            ],
            table='iki__mouse_strains_mousecryostock',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='MouseCryostock_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('strain', iki.UUIDField(db_column='strain_id', null=True)),
                ('kind', iki.TextField(null=True)),
                ('carrier', iki.TextField(null=True)),
                ('banked_on', iki.DateField(null=True)),
                ('genotype', iki.UUIDField(db_column='genotype_id', null=True)),
                ('embryos_at_banking', iki.BigIntegerField(null=True)),
                ('barcode', iki.TextField(null=True)),
                ('files', iki.ArrayField(base_field=iki.UUIDField(), null=True)),
            ],
            table='iki__mouse_strains_mousecryostock__history',
        ),
        iki.AppendOnly(
            model_name='MouseCryostock_History',
        ),
        iki.CreateModel(
            name='MouseGenotype',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('subject', iki.CharField(max_length=200)),
                ('notation', iki.CharField(max_length=2000)),
                ('report', iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to=iki.FILE)),
            ],
            table='iki__mouse_strains_mousegenotype',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='MouseGenotype_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('strain', iki.UUIDField(db_column='strain_id', null=True)),
                ('subject', iki.TextField(null=True)),
                ('notation', iki.TextField(null=True)),
                ('alleles', iki.ArrayField(base_field=iki.UUIDField(), null=True)),
                ('report', iki.UUIDField(db_column='report_id', null=True)),
            ],
            table='iki__mouse_strains_mousegenotype__history',
        ),
        iki.AppendOnly(
            model_name='MouseGenotype_History',
        ),
        iki.CreateModel(
            name='MouseStrain',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
            ],
            table='iki__mouse_strains_mousestrain',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='MouseStrain_Entry',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('catalogue', iki.TextField(db_default='mgi', db_index=True)),
                ('mgi_id', iki.CharField(db_index=True, max_length=32, null=True)),
                ('local_id', iki.CharField(db_index=True, max_length=64, null=True)),
                ('nomenclature', iki.CharField(max_length=1000, null=True)),
                ('jax_stock', iki.CharField(db_index=True, max_length=6, null=True)),
                ('repository_stocks', iki.ArrayField(base_field=iki.CharField(max_length=100), null=True)),
            ],
            table='iki__mouse_strains_mousestrain__entry',
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='MouseStrain_Entry_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
                ('catalogue', iki.TextField(null=True)),
                ('mgi_id', iki.TextField(null=True)),
                ('local_id', iki.TextField(null=True)),
                ('nomenclature', iki.TextField(null=True)),
                ('jax_stock', iki.TextField(null=True)),
                ('repository_stocks', iki.ArrayField(base_field=iki.TextField(), null=True)),
                ('alleles', iki.ArrayField(base_field=iki.UUIDField(), null=True)),
            ],
            table='iki__mouse_strains_mousestrain__entry__history',
        ),
        iki.AppendOnly(
            model_name='MouseStrain_Entry_History',
        ),
        iki.CreateModel(
            name='MouseStrain_History',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity_id', 'revision')),
                ('entity_id', iki.UUIDField()),
                ('revision', iki.PositiveIntegerField()),
            ],
            table='iki__mouse_strains_mousestrain__history',
        ),
        iki.AppendOnly(
            model_name='MouseStrain_History',
        ),
        iki.CreateModel(
            name='MouseAllele_Entry',
            fields=[
                ('entity', iki.OneToOneField(db_column='entity_id', on_delete=iki.PROTECT, parent_link=True, primary_key=True, related_name='+', serialize=False, to=iki.ENTITY)),
                ('mgi_id', iki.CharField(db_index=True, max_length=32)),
                ('symbol', iki.CharField(db_index=True, max_length=512, null=True)),
            ],
            table='iki__mouse_strains_mouseallele__entry',
            indexes=[
                iki.GinIndex(fields=['mgi_id'], name='mgi_id_trgm_cda1af60ad0b67add6', opclasses=['gin_trgm_ops']),
                iki.GinIndex(fields=['symbol'], name='symbol_trgm_ea14e48fffd86969a3', opclasses=['gin_trgm_ops']),
            ],
            constraints=[
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('mgi_id'), 1), name='iki__mouse_strains_mouseallele__entry__mgi_id__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('mgi_id__regex', '[\\r\\n]'), negated=True), name='iki__mouse_strains_mouseallele__entry__mgi_id__one_line'),
                iki.CheckConstraint(condition=iki.Q(('mgi_id__regex', '^(?:^MGI:[1-9][0-9]*$)$')), name='iki__mouse_strains_mouseallele__entry__mgi_id__pattern'),
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('symbol'), 1), name='iki__mouse_strains_mouseallele__entry__symbol__not_empty'),
                iki.CheckConstraint(condition=iki.Q(('symbol__regex', '[\\r\\n]'), negated=True), name='iki__mouse_strains_mouseallele__entry__symbol__one_line'),
                iki.CheckConstraint(condition=iki.Q(('symbol__regex', '^(?:^[^\\t\\r\\n]+$)$')), name='iki__mouse_strains_mouseallele__entry__symbol__pattern'),
            ],
            bases=(iki.ENTITY,),
        ),
        iki.CreateModel(
            name='MouseCryostock_files',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity', 'position')),
                ('entity', iki.ForeignKey(db_column='entity_id', on_delete=iki.CASCADE, related_name='+', to='iki__mouse_strains.mousecryostock')),
                ('position', iki.PositiveIntegerField()),
            ],
            table='iki__mouse_strains_mousecryostock__files',
        ),
        iki.CreateModel(
            name='MouseGenotype_alleles',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity', 'position')),
                ('entity', iki.ForeignKey(db_column='entity_id', on_delete=iki.CASCADE, related_name='+', to='iki__mouse_strains.mousegenotype')),
                ('position', iki.PositiveIntegerField()),
            ],
            table='iki__mouse_strains_mousegenotype__alleles',
        ),
        iki.AddField(
            model_name='mousecryostock',
            name='genotype',
            field=iki.ForeignKey(null=True, on_delete=iki.PROTECT, related_name='+', to='iki__mouse_strains.mousegenotype'),
        ),
        iki.CreateModel(
            name='MouseStrain_Entry_alleles',
            fields=[
                ('pk', iki.CompositePrimaryKey('entity', 'position')),
                ('entity', iki.ForeignKey(db_column='entity_id', on_delete=iki.CASCADE, related_name='+', to='iki__mouse_strains.mousestrain_entry')),
                ('position', iki.PositiveIntegerField()),
            ],
            table='iki__mouse_strains_mousestrain__entry__alleles',
        ),
        iki.AddIndex(
            model_name='mousestrain_entry',
            index=iki.GinIndex(fields=['mgi_id'], name='mgi_id_trgm_f6d73fd0d04feaf6f0', opclasses=['gin_trgm_ops']),
        ),
        iki.AddIndex(
            model_name='mousestrain_entry',
            index=iki.GinIndex(fields=['local_id'], name='local_id_trgm_564e9cbfd252c6b1', opclasses=['gin_trgm_ops']),
        ),
        iki.AddIndex(
            model_name='mousestrain_entry',
            index=iki.GinIndex(fields=['jax_stock'], name='jax_stock_trgm_8b4b85f9aeabdd2', opclasses=['gin_trgm_ops']),
        ),
        iki.AddConstraint(
            model_name='mousestrain_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('catalogue__in', ['mgi', 'local'])), name='iki__mouse_strains_mousestrain__entry__catalogue__values'),
        ),
        iki.AddConstraint(
            model_name='mousestrain_entry',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('mgi_id'), 1), name='iki__mouse_strains_mousestrain__entry__mgi_id__not_empty'),
        ),
        iki.AddConstraint(
            model_name='mousestrain_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('mgi_id__regex', '[\\r\\n]'), negated=True), name='iki__mouse_strains_mousestrain__entry__mgi_id__one_line'),
        ),
        iki.AddConstraint(
            model_name='mousestrain_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('mgi_id__regex', '^(?:^MGI:[1-9][0-9]*$)$')), name='iki__mouse_strains_mousestrain__entry__mgi_id__pattern'),
        ),
        iki.AddConstraint(
            model_name='mousestrain_entry',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('local_id'), 1), name='iki__mouse_strains_mousestrain__entry__local_id__not_empty'),
        ),
        iki.AddConstraint(
            model_name='mousestrain_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('local_id__regex', '[\\r\\n]'), negated=True), name='iki__mouse_strains_mousestrain__entry__local_id__one_line'),
        ),
        iki.AddConstraint(
            model_name='mousestrain_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('local_id__regex', '^(?:^[^\\s]+$)$')), name='iki__mouse_strains_mousestrain__entry__local_id__pattern'),
        ),
        iki.AddConstraint(
            model_name='mousestrain_entry',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('nomenclature'), 1), name='iki__mouse_strains_mousestrain__entry__nomenclature__not_empty'),
        ),
        iki.AddConstraint(
            model_name='mousestrain_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('nomenclature__regex', '[\\r\\n]'), negated=True), name='iki__mouse_strains_mousestrain__entry__nomenclature__one_line'),
        ),
        iki.AddConstraint(
            model_name='mousestrain_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('nomenclature__regex', '^(?:^[^\\t\\r\\n]+$)$')), name='iki__mouse_strains_mousestrain__entry__nomenclature__pattern'),
        ),
        iki.AddConstraint(
            model_name='mousestrain_entry',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('jax_stock'), 1), name='iki__mouse_strains_mousestrain__entry__jax_stock__not_empty'),
        ),
        iki.AddConstraint(
            model_name='mousestrain_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('jax_stock__regex', '[\\r\\n]'), negated=True), name='iki__mouse_strains_mousestrain__entry__jax_stock__one_line'),
        ),
        iki.AddConstraint(
            model_name='mousestrain_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('jax_stock__regex', '^(?:^[0-9]{6}$)$')), name='iki__mouse_strains_mousestrain__entry__jax_stock__pattern'),
        ),
        iki.AddConstraint(
            model_name='mousestrain_entry',
            constraint=iki.CheckConstraint(condition=iki.IsPlainList('repository_stocks'), name='iki__mouse_strains_mousestrain__entry__repository_s__4f512650b2'),
        ),
        iki.AddConstraint(
            model_name='mousestrain_entry',
            constraint=iki.CheckConstraint(condition=iki.Q(('repository_stocks__len__lte', 20)), name='iki__mouse_strains_mousestrain__entry__repository_s__3ed47dfbcb'),
        ),
        iki.AddConstraint(
            model_name='mousestrain_entry',
            constraint=iki.CheckConstraint(condition=iki.AllLengthsBetween('repository_stocks', 1, None), name='iki__mouse_strains_mousestrain__entry__repository_s__d020f8d8b9'),
        ),
        iki.AddConstraint(
            model_name='mousestrain_entry',
            constraint=iki.CheckConstraint(condition=iki.NoneMatch('repository_stocks', '[\\r\\n]'), name='iki__mouse_strains_mousestrain__entry__repository_s__b5bc33d05a'),
        ),
        iki.AddConstraint(
            model_name='mousestrain_entry',
            constraint=iki.CheckConstraint(condition=iki.AllMatch('repository_stocks', '^(?:^[A-Z][A-Za-z0-9]*:[^\\s]+$)$'), name='iki__mouse_strains_mousestrain__entry__repository_s__e05378ca1e'),
        ),
        iki.AddField(
            model_name='mousegenotype',
            name='strain',
            field=iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__mouse_strains.mousestrain_entry'),
        ),
        iki.AddField(
            model_name='mousecryostock',
            name='strain',
            field=iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__mouse_strains.mousestrain_entry'),
        ),
        iki.AddField(
            model_name='mousecryostock_files',
            name='item',
            field=iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to=iki.FILE),
        ),
        iki.AddField(
            model_name='mousegenotype_alleles',
            name='item',
            field=iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__mouse_strains.mouseallele_entry'),
        ),
        iki.AddField(
            model_name='mousestrain_entry_alleles',
            name='item',
            field=iki.ForeignKey(on_delete=iki.PROTECT, related_name='+', to='iki__mouse_strains.mouseallele_entry'),
        ),
        iki.AddConstraint(
            model_name='mousegenotype',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('subject'), 1), name='iki__mouse_strains_mousegenotype__subject__not_empty'),
        ),
        iki.AddConstraint(
            model_name='mousegenotype',
            constraint=iki.CheckConstraint(condition=iki.Q(('subject__regex', '[\\r\\n]'), negated=True), name='iki__mouse_strains_mousegenotype__subject__one_line'),
        ),
        iki.AddConstraint(
            model_name='mousegenotype',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('notation'), 1), name='iki__mouse_strains_mousegenotype__notation__not_empty'),
        ),
        iki.AddConstraint(
            model_name='mousegenotype',
            constraint=iki.CheckConstraint(condition=iki.Q(('notation__regex', '[\\r\\n]'), negated=True), name='iki__mouse_strains_mousegenotype__notation__one_line'),
        ),
        iki.AddIndex(
            model_name='mousecryostock',
            index=iki.GinIndex(fields=['barcode'], name='barcode_trgm_cf560cc5641a8edb6', opclasses=['gin_trgm_ops']),
        ),
        iki.AddConstraint(
            model_name='mousecryostock',
            constraint=iki.CheckConstraint(condition=iki.Q(('kind__in', ['embryos', 'sperm'])), name='iki__mouse_strains_mousecryostock__kind__values'),
        ),
        iki.AddConstraint(
            model_name='mousecryostock',
            constraint=iki.CheckConstraint(condition=iki.Q(('carrier__in', ['straw', 'cryovial'])), name='iki__mouse_strains_mousecryostock__carrier__values'),
        ),
        iki.AddConstraint(
            model_name='mousecryostock',
            constraint=iki.CheckConstraint(condition=iki.Q(('embryos_at_banking__gte', 1)), name='iki__mouse_strains_mousecryostock__embryos_at_banking__min'),
        ),
        iki.AddConstraint(
            model_name='mousecryostock',
            constraint=iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('barcode'), 1), name='iki__mouse_strains_mousecryostock__barcode__not_empty'),
        ),
        iki.AddConstraint(
            model_name='mousecryostock',
            constraint=iki.CheckConstraint(condition=iki.Q(('barcode__regex', '[\\r\\n]'), negated=True), name='iki__mouse_strains_mousecryostock__barcode__one_line'),
        ),
        iki.AddConstraint(
            model_name='mousecryostock_files',
            constraint=iki.CheckConstraint(condition=iki.Q(('position__lt', 50)), name='iki__mouse_strains_mousecryostock__files__position__max_items'),
        ),
        iki.AddConstraint(
            model_name='mousegenotype_alleles',
            constraint=iki.CheckConstraint(condition=iki.Q(('position__lt', 200)), name='iki__mouse_strains_mousegenotype__alleles__position__max_items'),
        ),
        iki.AddConstraint(
            model_name='mousestrain_entry_alleles',
            constraint=iki.CheckConstraint(condition=iki.Q(('position__lt', 200)), name='iki__mouse_strains_mousestrain__entry__alleles__pos__b0db9744e0'),
        ),
    ]
