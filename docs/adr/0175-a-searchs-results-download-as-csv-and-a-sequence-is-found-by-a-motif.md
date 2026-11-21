# 0175. A search's results download as CSV, and a sequence is found by a motif

- Status: Accepted
- Date: 2026-11-21
- Refines: [0037](0037-entity-search.md),
  [0138](0138-an-identity-reads-sequences-and-sets-of-parts-as-what-they-are.md),
  [0171](0171-a-types-records-export-as-files-iki-writes-from-its-fields.md)

## Context

Two things a scientist does with a registry's search, which Benchling's does,
Iki's didn't.

**Taking the results away.** The grid showed what a search found, and the only
way out was the API. "Export" in a project's settings means a signed bundle
for another Iki deployment (ADR 0096), and a record downloads as GenBank one at
a time (ADR 0171). A list of a project's constructs for a spreadsheet, a
sequencing core or a report had to be copied by hand. A table that leaves Iki
is a copy of records. In regulated work, that copy has to be what the reader
may read, and on the record of who took it, as a record's file is (ADR 0171).
A spreadsheet also runs a cell that starts with `=`, `+`, `-` or `@` as a
formula. So a name someone typed into Iki could run in the spreadsheet of
whoever downloads it: OWASP's CSV injection.

**Finding a sequence by its bases.** A sequence was searched as any string: `contains` and `starts_with` found its
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

**A search's results download as CSV.**
`GET /entities/search.csv` takes what `POST /entities/search` takes, as
parameters, with `where` as JSON. It finds the same entities, as the same
reader, in the same order.

- **Its columns are the grid's.** These are the ID; the type, where the type
  has subtypes; a request's sides; its states, then its other fields; or
  without a type, each one's type and title. The owning project is added where
  the search isn't held to one. `columns` names others, in order. Every row
  ends with when the record last changed, its revision and its UUID. An HRID
  can name a registry entry's record in several projects, so the UUID and
  revision say exactly which record, and as of when, each row shows.
- **Its values are as the grid shows them, as the reader may read them.** A
  reference is its HRID, or "An entity you can’t read". A person is their name.
  A state is its label, and a list is its items. Where a spreadsheet needs data
  rather than display, the file says it as data. Numbers are in full
  (`11.981`, not `11.98`). A quantity's unit is in its column's heading, in the
  unit the grid shows it in: `Molecular weight (kDa)`. Dates and times are ISO
  8601, times in UTC.
- **It streams.** It reads a keyset page of 500 at a time, each under a
  search's time limit, and sends each page as it's read. Under ASGI, each page
  is read in the request's thread rather than after Django has gathered the
  lot, so a million rows start at once and are never held whole. A page that
  runs out of time ends the file with a row saying it stopped, rather than
  ending it as if it were whole.
- **No cell is a formula.** This follows OWASP's guidance. A cell starting
  with `=`, `+`, `-`, `@`, a tab, a carriage return or a line feed, or their
  full-width forms, starts with a `'`, which spreadsheets show as text. Every
  cell is quoted, so a separator inside one can't start another in a locale
  that splits on `;`. A number Iki writes (`-20`) stays a number. The lineage
  and cost downloads use the same writer.
- **Each download is in the audit log** as `search.exported`. The event has
  the reader, the project, the search as given, the columns and the file's
  name. It's written before the first row is sent, so a download cut off
  partway is still on the record. It has no hash of the file, unlike ADR 0171's.
  A hash is known only once the last row is written, which is after the rows
  have left, or else after holding the whole file. The search, the time and
  each row's revision say what each row was.
- **The grid has a Download CSV link** beside its count. It is a link, so
  the keyboard reaches it and the browser saves it with its own progress, and
  it's described by the count. It downloads everything the search finds, not
  only the rows loaded. A saved search is a grid's search, so it downloads the
  same way.

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
- A search's results leave Iki as a table anyone who can read them can take,
  each time on the record, with what they are and as of when.
- A download's file isn't one fixed file the way a record's revision is. The
  same search run later finds the records as they are then.
- Similarity search (BLAST) is a different question, which a maintained index
  of the registry's sequences would answer. That isn't this.

## Alternatives considered

- **Writing the CSV in the browser from the rows it has loaded.** That's only
  the rows scrolled to, unaudited, and a second writer of values beside the
  server's.
- **Holding a transaction open for the whole download**, so every row is as
  of one moment. A slow reader would hold back the database's vacuum for as
  long as the download took. Each row's revision says what it was instead.
- **Rows as the API's JSON, with machine names for headings.** The API and
  the SDK's DataFrames already give that. The file is for the spreadsheet a
  scientist opens.

- **A record's N matching anything.** This would report sites that may not be
  there, which isn't conservative in regulated work. It's the reading a
  sequence-reading tool might want, but not a registry's.
- **Every DNA field matching both strands.** A primer, or a single strand of
  RNA, isn't its complement, as its identity says (ADR 0138).
- **Storing each circle's ends, or its doubled sequence, to index the
  origin.** That's another column for every sequence type to keep current,
  for a case the window answers within the time limit.
