# 0140. An organism is a taxon, by its NCBI Taxonomy ID

- Status: Accepted
- Date: 2026-11-18
- Refines: [0115](0115-registries-keep-identities-projects-keep-records.md),
  [0056](0056-bulk-imports.md)

## Context

`iki/core.Sequence.organism` was a string: whatever the file said. A
GenBank record says `Homo sapiens`, a PDB entry `HOMO SAPIENS` or a common
name, an old file a name NCBI has since changed (_Lactobacillus casei_ is
_Lacticaseibacillus casei_ now, the same taxon, 1582). So "every human
protein" is a search for each spelling, and a strain, a cell line or a
donor's sample can't say it's from the same organism as a sequence. The
molecules step 62 adds (antibodies, complexes, biologics, cells) need a
species they can refer to.

Every source names the taxon by its NCBI Taxonomy ID too: a GenBank
record's source feature (`/db_xref="taxon:9606"`), UniProt's `OX=`,
mmCIF's `pdbx_gene_src_ncbi_taxonomy_id`, ModelCIF's
`ncbi_taxonomy_id`. That ID is what INSDC, UniProt and the PDB agree on,
and it never changes when the name does.

## Decision

**An organism is a core entity type with an identity: its NCBI Taxonomy
ID.** `iki/core.Organism` has `taxon_id` (an integer, its identity),
`scientific_name`, `rank` and `parent` (another organism), each a registry
fact (ADR 0115), so registrars correct a name NCBI changes. Its ID is
`TAX-…`, and its title its scientific name. One entry per taxon in each
registry, held by each project that uses it.

**`Sequence.organism` refers to an organism.** So do DNA sequences,
proteins and what extends them. A search for a protein's organism is by its
entry, and the grid and the graph show and follow it as any reference.

**Imports make an organism the first time they meet it.** The DNA
plugin's GenBank and NCBI imports, and the protein plugin's FASTA,
UniProt, PDB, AlphaFold DB and file imports, give each taxon they read as a
create of `iki/core.Organism` with its ID and name and a key
(`taxon:9606`). The sequences from it refer to that key (`$create`). An
organism already registered is the one they get. A source that names an
organism without its ID (NCBI's FASTA headers, `[Homo sapiens]`) leaves it
out, with a warning: a name alone can't say which taxon it is.

**A name only fills an organism that has none.** A run's create can name
fields in `fills`: where the entry has a value already, it keeps it and the
create goes ahead. Elsewhere a fact that differs is refused (ADR 0115). The
imports fill `scientific_name`, so an old file with a taxon's old name
still imports, and the registrar's name stays.

**A creates file's lines can refer to `creates`' keyed creates.** A bulk
import of a million sequences (ADR 0056) needs only a few organisms. They
go in `creates`, written first, and the lines refer to them by key. A line
still can't refer to another line, or to a create in its identity: a line's
identity is checked against the registry before anything is written.

**No bundled taxonomy.** Iki doesn't ship NCBI's taxonomy dump or load it
at install. An organism is created when it's first used, from what the
source said. Why:

- **Size.** The dump holds over 2.7 million taxa, nearly all of them never
  used by any one lab. Loading them into every registry would make its
  organisms list and its search mostly noise.
- **Staleness.** NCBI changes names and merges taxa every day. A bundled
  copy is out of date the day it ships, and an air-gapped deployment
  (ADR 0114) couldn't refresh it. A registrar corrects the few that
  matter.
- **Licensing and provenance.** Each organism says where it came from: the
  import that made it. A bundled copy would be a second source with its
  own terms and history.
- **What's needed.** A record names a taxon by its ID. Its lineage
  (`parent`, `rank`) is optional: a plugin or a registrar fills it when a
  workflow needs it, such as a search for every primate's sequences.

## Consequences

- One search finds every sequence from a taxon, however its files spelled
  it. A strain, a donor's sample or an antibody (step 62) can refer to the
  same organism.
- `Sequence.organism` changed from a string to a reference, before M1, so
  its migrations were regenerated. A plugin that set it to a string now
  refers to an organism: it creates it (as the imports do) or finds it.
- A record whose source gave no taxon ID has no organism. The run's
  warning says which, and a person sets it.
- A taxon's lineage isn't filled in by the imports. A follow-up can add a
  `fetch_organisms` action that reads it from NCBI's E-utilities, for
  deployments that may reach them.
- `fills` is only for `creates`, not a creates file's lines, and lines
  can't refer to a create in their identity fields. Both are follow-ups if
  a plugin needs them.
