# 0256. Each sequencing report format has its own import

- Status: Accepted
- Date: 2026-11-26
- Refines: [0197](0197-a-sequencing-result-keeps-the-report-and-the-selected-sample.md)

## Context

One import action read VCFs, RNA-seq count matrices and AIRR rearrangement TSVs,
chosen by a format parameter. Its form showed every format's fields at once:
a VCF's sample column beside an AIRR repertoire ID and a list of antibody
associations. A VCF had to name a registered strain or cell line, so a VCF from a
patient specimen, a donor or an organism with no such record couldn't be
imported. To offer those choices, the plugin depended on the strain, cell line and
antibody plugins, so a lab importing only RNA-seq counts installed three
registries it didn't use.

An AIRR row that a scientist associated with a registered antibody was stored
inside the import result. The association is a curator's claim, made after
the import, by a different person, often much later. It shared the import's record,
history and review.

## Decision

Each format has its own action: **Import VCF sample**, **Import RNA-seq counts**
and **Import AIRR rearrangements**. Each asks for what its format needs: the
reference assembly and a sample column for a VCF, the build and annotation and a
sample column for counts, and the germline reference set and a repertoire ID for
AIRR. All three create the same result type, with its format, and keep the report,
its SHA-256, the pinned input revisions and the selected rows.

Each result states what its selection keeps, in its summary and on its tab: the
sites where the VCF sample's genotype calls an alternate allele, with the sites
left out for lack of a call counted, a count column's raw integers, or every
rearrangement of a repertoire. The tab says the first twelve rows are a preview
and that the run's `selected.tsv` holds the rest.

The material a result's sample represents is optional and may be any registered
record. The plugin depends only on `iki/sequencing`.

Linking an AIRR rearrangement to a registered antibody moves to a separate
first-party plugin, `iki/antibody_rearrangements`. Its action checks that the
row exists in the imported repertoire, that the report still has its recorded
SHA-256 and that the row has an immunoglobulin locus. It creates one attributed
record per link with the row's calls, the pinned revisions and the curator.

## Consequences

A genomics or transcriptomics lab installs sequencing and sequencing analysis
alone. A VCF from any specimen can be imported. A discovery lab adds the link
plugin with the antibody registry. Each link is corrected or archived on its own
without touching the import's evidence.

A link records that the curator's row exists in the repertoire, not that its
sequence matches the antibody's chains. Comparing them is a separate check that a
lab can add.
