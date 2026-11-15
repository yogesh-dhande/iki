# 0089. A param can take several files

- Status: Accepted
- Date: 2026-11-15
- Refines: [0052](0052-imports-file-params-and-origins.md)

## Context

ADR 0052 gave params one file each, and left lists of files until an
action needed them. One does now: checking a construct means aligning
its Sanger reads to the plasmid it should be, and a sequencing run gives
a trace file per read, two to eight for a plasmid, 96 for a plate. Making
someone launch a run per read, or zip them first, is not how scientists
work. Benchling's alignment takes several traces at once, and so do
SnapGene and Geneious.

## Decision

**A list param can hold files:** `type: list` with `items: { type: file }`,
the items' `media_types` and `max_size`, and the list's `min_items` and
`max_items`, as a list field of files has. Like a file param, it has no
default.

At launch, each file is checked as a file param's is: one the person can
read, in the run's project, of a type and size the items take. A file can
be chosen once. The run keeps their IDs in the order chosen, which the
command gets in `input.json`'s `params`, each file described in its
`files` and downloaded before the command starts. An imported entity's
`origin.file` may name any of them.

Reuse ([ADR 0057](0057-reuse-reproduce-exactly-and-seeds.md)) counts the list by its files' contents, in order: the same
reads uploaded again reuse the results, and another read runs again.

**On the New run page,** a list of files can be filled one at a time,
choosing an upload or a file in the project, or with several files chosen
at once, each uploading in its own row. Each is checked against the
items' types and sizes before it's sent, and a list never takes more than
its `max_items`. The run's page lists them by name, each downloadable by
whoever can read it.

## Consequences

- The dna plugin's alignment of reads to a reference takes a run's traces
  in one go.
- Lists of files in entity forms check each item against the items' types
  and sizes, which they didn't before, and can be filled several at once.
