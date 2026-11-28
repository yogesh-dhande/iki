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
agree. A rearrangement the report calls non-productive (`productive` F: in the
AIRR schema, its V(D)J isn't predicted to be productive, being out of frame or
having a stop codon) can't encode a chain, so linking one is refused unless the
curator gives why it does anyway, such as a resequenced clone that corrects the
call; the reason is kept on the link. One the report leaves `productive` empty
(AIRR's null) is linked with a warning to check it. A reason for a productive
one is refused, as there's nothing to overrule. The **Antibody rearrangement** it creates keeps the row's productive flag,
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
`tests/data/airr-standards-productive.tsv` is three rows of the AIRR Community's
own test rearrangements
([`good_rearrangement.tsv`](https://github.com/airr-community/airr-standards/blob/6f0ac9b19ff3ad450bb00e55d58a76b77b7c7f90/lang/python/tests/data/good_rearrangement.tsv),
CC BY 4.0), with their annotation columns: a productive one, one called
non-productive, whose junction has a stop codon, and one whose `productive` is
left empty here, as AIRR writes null, to test that case.
