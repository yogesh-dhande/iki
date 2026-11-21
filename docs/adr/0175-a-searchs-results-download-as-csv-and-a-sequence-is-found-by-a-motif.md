# 0175. A search's results download as CSV, and a sequence is found by a motif

- Status: Accepted
- Date: 2026-11-21
- Refines: [0037](0037-entity-search.md),
  [0138](0138-an-identity-reads-sequences-and-sets-of-parts-as-what-they-are.md)

## Context

A sequence was searched as any string: `contains` and `starts_with` found its
bases as text, on the strand and from the origin they were written with. A
scientist asking for every plasmid with an EcoRI site, or every construct a
primer binds, means something else. They write the site in IUPAC codes
(`GAATTY` is GAATT, then C or T). A double-stranded molecule has the site on
either strand. A circle has it across its origin too. Iki's identity already
reads a sequence as the molecule it is (ADR 0138); its search didn't.
Benchling and SnapGene find a sequence by its bases in these ways.

## Decision

**A string with a sequence format takes `motif`.** The formats are
`dna_sequence`, `rna_sequence` and `protein_sequence`, so DNA, RNA and
protein fields all take it.

```json
{ "field": "bases", "op": "motif", "value": "GAATTY" }
```

The table of operators in `iki_spec.conditions` gives each format's extra
operators beside each kind's. So the server, the validator of components' and
notifications' searches, and the web app's filters offer it on the same fields.
Each field says its `format` in the API.

**The value is the alphabet's IUPAC codes.** These are the IUPAC-IUB codes
for nucleotides (Cornish-Bowden, 1985). For proteins they're the 20 amino
acids, U and O, with B, Z, J and X. A motif is read in either case, without
the spaces and line breaks a pasted sequence has, up to 200 codes. A code that
isn't its alphabet's is refused, and the error says why: U in DNA, for
instance. `iki_spec.motifs` holds the tables. Its tests check them against
Biopython's, and check that its patterns find on pUC19 exactly what
Biopython's `nt_search` finds.

**A code in the motif matches a code in the record that it certainly is.**
Each code stands for a set of residues. A record's code matches when every
residue it might be is one the motif's code allows. So Y matches C, T and Y.
But GAATTC doesn't match a record that reads GAANTC, whose N might not be a
T. A site that might not be there isn't reported as one. Searching for N (or
for X in a protein) finds the unknown residues.

**What the record says about its molecule widens the match**, read from the
fields its sequence's `strands` and `topology` name, as its identity reads
them:

- A record whose strands field says `double` also matches the motif's reverse
  complement. That is the other strand, read 5′ to 3′, so the M13 reverse
  primer is found on pUC19, whose given strand doesn't have it. A single
  strand, or a sequence that doesn't say, matches only as written, as ADR
  0138 reads it.
- A record whose topology field says `circular` also matches across its
  origin. That reading is limited to the circle's length, so a motif longer
  than the circle doesn't match by going round it twice.

**It compiles to indexed SQL where it can.**

- A motif without ambiguity is found as text, with `ILIKE`.
- One with ambiguity is found as a regular expression, with `~*` and a
  character class for each ambiguous code.
- A trigram index on the field serves both, as pg_trgm reads regular
  expressions too. Tests check the plans.
- The other strand is one more such condition, on the same column.
- The origin is read in a window: the circle's last `k − 1` codes, then its
  first `k − 1`, for a motif of `k`. No index holds that window, so circular
  records are read one by one. That stays within a search's time limit.

No new index or stored column is needed. Most long sequences (a DNA
sequence's bases) aren't indexed and are scanned under the time limit, as
`contains` on them already was.

## Consequences

- One condition finds a site in IUPAC codes, on either strand of a duplex and
  across a plasmid's origin. It works in saved searches, collections,
  components' searches and notifications alike.
- A motif can't say "anything but" or "between 2 and 5 of": it's a sequence
  of codes, not a pattern language such as PROSITE's. A motif that needs
  alternatives is an `any` of several.
- Searching circular records for a motif reads each of their ends. At millions
  of plasmids, that's a scan the time limit stops. Then the search has to be
  narrowed, by project or another indexed field.
- Similarity search (BLAST) is a different question, which a maintained index
  of the registry's sequences would answer. That isn't this.

## Alternatives considered

- **A record's N matching anything.** This would report sites that may not be
  there, which isn't conservative in regulated work. It's the reading a
  sequence-reading tool might want, but not a registry's.
- **Every DNA field matching both strands.** A primer, or a single strand of
  RNA, isn't its complement, as its identity says (ADR 0138).
- **Storing each circle's ends, or its doubled sequence, to index the
  origin.** That's another column for every sequence type to keep current,
  for a case the window answers within the time limit.
