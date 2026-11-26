# Antibody rearrangements

Record which registered antibody an AIRR rearrangement encodes. Import the
repertoire first with **Import AIRR rearrangements** from
[`iki/sequencing_analysis`](../sequencing_analysis/README.md). Then run **Link a
rearrangement to an antibody** with that result, the antibody and the
rearrangement's exact `sequence_id`. Link the heavy and the light chain's
rearrangements separately.

The action checks the result is an AIRR import, that the staged report still has
the SHA-256 the result recorded, and that the `sequence_id` is exactly one row of
the imported repertoire with an immunoglobulin locus (IGH, IGK or IGL). When the
report leaves `locus` empty, the V and J calls give it if both are present and
agree. The **Antibody rearrangement** it creates keeps the row's productive flag,
gene calls, duplicate count and junction, the revisions of the result and the
antibody, and the person who linked them as its curator. Add the reason in its
notes, such as a cloned and confirmed sequence.

A link is the curator's statement: the action checks the row exists, not that
its sequence matches the antibody's chains. The import keeps the report as
evidence; each link is a separate, attributed record, so a correction archives one
link without touching the import.

This plugin needs `iki/antibody` and `iki/sequencing_analysis`. A lab that
imports VCFs or RNA-seq counts needs neither it nor the antibody registry.

## Test data

`tests/data/pbmc-b-airr.tsv` is the first four contigs' annotation columns of
the human PBMC B-cell rearrangements that
[nf-core/airrflow](https://raw.githubusercontent.com/nf-core/test-datasets/d05f0f2a13d529e673068a61e9ff976d20d0f9ae/testdata-reveal/sc5p_v2_hs_PBMC_1k_b_airr_rearrangement.tsv)
tests with, the same excerpt the sequencing analysis plugin imports.
