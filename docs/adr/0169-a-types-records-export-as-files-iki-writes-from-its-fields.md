# 0169. A type's records export as files Iki writes from its fields

- Status: Accepted
- Date: 2026-11-20
- Refines: [0085](0085-types-configure-ikis-own-viewers.md),
  [0052](0052-imports-file-params-and-origins.md),
  [0163](0163-a-plasmid-is-a-record-about-a-sequence-not-a-kind-of-sequence.md),
  [0168](0168-a-plasmids-topology-is-its-entrys-and-a-linear-molecules-ends-are-ends.md)

## Context

A lab's sequences don't stay in one tool. A cloning plan is drawn in SnapGene,
a primer is checked in Primer-BLAST, a construct goes to a vendor or a
sequencing core, a submission goes to NCBI. Each of them reads GenBank, and
most read FASTA. Benchling and Geneious export both from a sequence's page, and
SnapGene exports GenBank as well as its own `.dna`. The DNA plugin already
imported GenBank and FASTA; nothing wrote them back out.

The files also arrive in SnapGene's own format. Many labs keep their plasmid
maps as `.dna` files, the format SnapGene saves in, and send them as that.

A file a record is downloaded as is a copy of it that leaves Iki. In regulated
work, that copy has to be the record as it was at a known revision, made for
someone who may read it, and on the record of who took it. It also has to be
honest: if the format can't hold something the record says, the file has to
say so, not drop it.

Iki already had two ways out for data:

- **An action that writes a file as an artifact**, as `export_sdf` writes a
  compound library. That needs a Runner, a worker, and a wait; it reads only a
  record's latest revision; its artifact expires; and downloading an artifact
  isn't in the audit log.
- **A bundle** (ADR 0096), which carries records with their history between
  deployments. Only an Admin makes one, and it's Iki's own format, not one
  other tools read.

Neither is "download this sequence as GenBank".

## Decision

**A type says which files its records export by naming its fields**, as it
names those Iki's viewers show (ADR 0085). Iki writes the file; a plugin
writes no code for it. One export kind exists, `sequence`:

```yaml
exports:
  - export: sequence
    sequence: bases # a DNA or RNA sequence field
    features: features # optional: a json field of features, as the viewer reads them
    description: description # optional: string or text
    accession: accession # optional: string
    organism: organism # optional: a ref to iki/core.Organism
```

A `sequence` export writes GenBank and FASTA. Validation checks each field is
there and is the kind its role writes. A subtype exports what its nearest
ancestor does, unless it says otherwise.

`sequence` may also name a reference to a type that exports a sequence. That
is how a plasmid, a record about a sequence (ADR 0163), exports. Its file is
its sequence's bases, topology and features, under the plasmid's name and
description, with the plasmid's own `features` after the sequence's. The
referenced record is the one in the plasmid's own project, the way a run
reads a reference (ADR 0105). It is read as it was while the plasmid's
revision was the plasmid's latest, the way a record reads its registry entry
(ADR 0115). So a plasmid's earlier revision exports with the sequence as it
was then.

A feature the plasmid's annotation found where the sequence's own file
already has it, with the same type, name, place and strand, is written once,
keeping the file's qualifiers.

**Iki writes the file on request, from the record's values at a revision.**
`GET /entities/{ref}/export?format=genbank|fasta&revision=N` returns the file
as an attachment, named for the record (`pUC19.gb`, or
`pUC19 (revision 2).gb` for an earlier one).

- Anyone who can read the record may download it, and a token needs only the
  read scope, the same as reading it.
- A revision is always the same file. Even the LOCUS line's date is the
  revision's own.
- Each download goes in the audit log as `entity.exported`, with the record,
  its project, the format, the revision, whether it was the latest, the
  file's name, size and SHA-256, and the notes on what the file couldn't
  hold. An auditor can therefore tell which file someone took.
- The record page has an Export button in its header, and each earlier
  revision has one in its history. Each opens a dialog that names the formats
  and what each holds.

**GenBank is written as INSDC sets it out:**

- The LOCUS line gives the name (cut to 16 characters, with the whole name in
  COMMENT), the length, the molecule (`DNA`, or `ss-DNA` for a single strand),
  the topology and the revision's date. The division is left blank, since
  Iki doesn't assign one.
- ACCESSION and VERSION are written only for a record that has an accession.
  Iki doesn't make one up.
- COMMENT gives the record's Iki ID and revision.
- The source feature comes from the organism: `/organism` and
  `/db_xref="taxon:N"`.
- Each feature keeps its type, its parts in order, their partial ends
  (`<`, `>`), `order()`, sites between bases (`100^101`) and its qualifiers.
  A name no qualifier already gives is written as `/label`, as SnapGene and
  Benchling write it.
- A feature that crosses the origin of a circle is written
  `join(x..L,1..y)`.

What GenBank can't hold is left out and listed in the file's COMMENT, and the
page says how many notes there are:

- a feature across a linear molecule's ends, which only a circle has
  (ADR 0168);
- a feature beyond the sequence's ends;
- a qualifier name INSDC can't hold.

A feature type that can't be a feature table key is written as a
`misc_feature`, with its type in a `/note`, and COMMENT says so too.

A feature with no strand is written as forward, since GenBank has no way to
say otherwise, and COMMENT says so.

**FASTA gives the ID, name and description in its header, with the topology
as NCBI's source modifier**, so the file reads back as the same molecule:

```
>PLS-000007 pUC19 Cloning vector pUC19 [topology=circular]
```

The DNA plugin's import reads `[topology=…]` back. FASTA holds no features,
and the page says so when a record has some.

**Iki reads SnapGene's `.dna` files, but doesn't write them.** The DNA
plugin's import reads:

- a `.dna` file's bases and topology;
- its features, with their segments, strands and qualifiers, and the
  features that cross the origin;
- its primers, as `Primer` records and their binding sites as features;
- its description and accession.

The import reports anything a sequence record can't hold as a run message,
and keeps the whole file as the record's origin (ADR 0052). Examples are the
host's methylation, SnapGene's history of how the molecule was made, and the
reads aligned to it.

Iki doesn't write `.dna`, for these reasons:

- **SnapGene doesn't publish the format.** Every reader outside SnapGene,
  Biopython's included, was worked out from SnapGene's own files. A reader
  can be checked against files SnapGene wrote. A writer has nothing to be
  checked against without SnapGene itself, which Iki's tests can't run, so a
  file it wrote would be one SnapGene might read differently or refuse.
- **A record holds what `.dna` can't**: partial ends, `order()`, sites
  between bases, and parts on different strands. Writing those would lose
  them silently, with no COMMENT for the file to say so in.
- **SnapGene opens GenBank** and keeps its features and qualifiers, so a
  record exported as GenBank opens in SnapGene with everything a `.dna` file
  would carry.

## Consequences

- A record downloads in a second, on its page, for anyone who can read it, at
  any revision. No action, worker or role is needed.
- Every download is in the audit log with its file's hash, which an artifact
  download isn't. It is an access record, like `bundle.downloaded`, not a
  change to the record.
- The writers are Iki's, so every type that names its fields exports alike,
  and a fix reaches all of them. A plugin can't add a format of its own this
  way. A file only a plugin can write, such as an SDF of a compound library,
  stays an action that writes an artifact.
- A `.dna` file comes into Iki, but goes out as GenBank. A lab that needs
  `.dna` back opens the GenBank file in SnapGene and saves it. If SnapGene
  publishes its format, or a reference writer can be tested against it, a
  `snapgene` format can be added to the `sequence` export.
- Further formats, such as FASTA for a protein or EMBL, are a new export kind
  or format, added here.

## Alternatives considered

- **An export action per format, writing an artifact.** This would reuse the
  `export_sdf` pattern. Rejected for the reasons above: it needs a Runner,
  reads only the latest revision, isn't audited and expires.
- **Writing the file in the browser.** The page has the values, but a
  download made there isn't audited, can't be fetched with a token, and would
  repeat the writer in TypeScript.
- **Writing `.dna` from what other readers know of it.** Rejected for the
  reasons above. A lossy writer that can't be checked against SnapGene is
  worse than none when the same file opens in SnapGene as GenBank.
