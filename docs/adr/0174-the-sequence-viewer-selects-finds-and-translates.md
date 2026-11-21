# 0174. The sequence viewer selects, finds and translates

- Status: Accepted
- Date: 2026-11-21
- Refines: [0085](0085-types-configure-ikis-own-viewers.md),
  [0087](0087-tracks-run-along-the-sequence-viewer.md)

## Context

ADR 0085's sequence viewer draws a sequence's residues in rows, its features
as bars under them, and ADR 0087's tracks along them. A scientist doing what
they do in Benchling's or SnapGene's sequence view needed three more things
from it:

- **Both strands, and what a stretch of them is.** Benchling's editor shows
  the complementary strand, and selecting a region says its length, its GC
  content and its melting temperature. Iki's viewer drew one strand and
  couldn't select anything.
- **Where a site is.** A record search for `GAATTC` says which records hold
  it as an exact string on one strand, and not where.
- **What it codes for.** A coding sequence's protein was a separate record
  (the protein plugin's `translate`), the right record, but nobody could read
  the protein along its DNA.

Each is read out of the bases, in the page, as the viewer already reads the
features. Each also has a number someone may compare with another tool's, so
how it's computed has to be pinned down.

## Decision

### The rows take a caret and a selection, as a text field does

**A caret between residues, and a selection from where it started to where
it's been extended.** The arrows move it (up and down by a row); Home and
End go to a row's start and end, with Control to the sequence's; Page Up and
Page Down go a window's worth of rows; Control with the left and right arrows
(Option on a Mac) goes a group of ten; Shift with any of them extends the
selection; Control+A selects it all, Escape clears it and Control+C copies
it. A pointer places the caret at the boundary nearest it, Shift extends to
it, a drag selects, and a feature's bar selects the whole feature. A place at
a row's end is also the next row's start, so, as a wrapped text field does,
the caret shows there unless End or a pointer put it at the end of its own.

**The rows are one widget,** `role="application"` with
`aria-roledescription="sequence"`, as the alignment viewer's are:
its keys aren't a document's, so a screen reader passes them on. What it
does is said in its description, and **a live readout above the rows** says
where the caret is ("Caret before base 397: G, paired with C") or what's
selected ("Bases 396–401 selected: 6 bp · GC 33.33% · Tm −20.4 °C"), so
nothing the viewer shows is only drawn. The readout, the find box and the copy
buttons stay at the top of the window as the rows scroll under them, and a row
brought into view is brought below them.

**A selection's numbers are the DNA plugin's, computed the same way,** so the
viewer and the plugin never disagree about the same bases:

- GC content counts G, C and S among the bases whose pairing is known (A, C,
  G, T, U, S and W), as `compute_composition` does, to two places.
- Tm is SantaLucia's unified nearest-neighbour model (PNAS 95, 1460; 1998)
  with his salt correction, at 50 mM Na⁺ alone and 50 nM of strands in all,
  as the primer type states and computes it. It's given for a stretch of 2 to
  200 bases of A, C, G and T: 200 is the longest primer the plugin takes, and
  a nearest-neighbour Tm is a two-state model of an oligo melting from its
  duplex, which a longer stretch doesn't do. Past 200, or with an ambiguous
  base, the readout says there's no Tm and why. A short stretch's Tm is what
  the model says, below zero for EcoRI's site, which is what a 6-mer primer
  would be recorded with too.

**A duplex shows its other strand under each row**, 3′ to 5′, when the field
its viewer names says it's `double` (ADR 0138's strands), with a switch to
hide it. Each IUPAC code pairs with the code for its bases' complements (R
with Y, B with V; S, W and N with themselves), and RNA's A with U. The other
strand's line is hidden from screen readers; the readout says each base's
pair.

**Copying** gives the selection's bases, or their reverse complement, and
says which it copied and how many.

### A motif is found on both strands, and round a circle's origin

**A find box over the rows** takes bases in IUPAC codes, or a protein's
residues with X for any and B, Z and J for their pairs; spaces and numbers,
as a pasted GenBank or FASTA has, are dropped. **A motif's code matches each
residue all of whose readings it allows:** its N matches an R, its R an A, a
G or an R, and its A doesn't match an N, which only might be an A. In plain
bases that's what Biopython's `nt_search` finds.

**Both strands, and across the origin.** The reverse strand's matches are
where the motif's reverse complement is on the forward strand; a match that
is both (EcoRI's palindromic `GAATTC`) is one match, on both strands. On a
circle a match may read across the origin; on a line it never reads across
the ends (ADR 0168). The viewer knows which from **the topology field its
sequence field names**, which the type API now gives with the strands field.
Each can be switched off.

**Every match is marked; Previous and Next step through them,** round past
either end as find on a page does, with Enter and Shift+Enter in the box, and
N and Shift+N in the rows. Going to a match selects it, so the readout says
what it is, and a status says which match it is of how many, on which
strand. Overlapping matches each count. At most 10,000 are marked, as more
say too little to step through, and a motif is at most 200 long, a primer's
length: a longer search is an alignment's job.

### A coding sequence is translated as GenBank translates it

**Each CDS feature gets a lane** of one-letter amino acids, each over its
codon's three bases (codons shaded in turn, starts and stops marked), read as
INSDC's feature table says:

- by the NCBI genetic code its `/transl_table` names, or the standard code;
  the tables are NCBI's own, as Biopython holds them and the plugins
  translate by, shipped with the page;
- its parts in the order kept, a reverse-strand part's bases complemented and
  read from its end, so a join reads across its introns, a codon split by one
  drawn over its middle base;
- from the base its `/codon_start` names; a 5′ partial end (`<`) starts no
  protein, so its first codon isn't taken as methionine, and otherwise a
  first codon that is one of the table's start codons is, as an initiator
  tRNA reads it (bacterial genes' GTG and TTG);
- with its `/transl_except`, such as a mitochondrial stop completed by
  polyadenylation (`(pos:4261..4262,aa:TERM)`) or a selenocysteine;
- in tables 27, 28 and 31, a codon that's a stop or an amino acid by where it
  is reads as a stop only as a complete coding sequence's last codon;
- a codon with ambiguous bases reads as what every codon it could be reads
  as (TTY is F), or as B, Z or J where those are all it could be, or X, as the
  alignment viewer compares residues;
- a last codon of one or two bases is left out unless a `/transl_except` says
  what it is, or it's at a 3′ partial end and its bases settle it whatever
  follows (CT is L), which is where NCBI's `/translation` gives it too.

A lane's protein is what the record's own `/translation` says: the tests
check every coding sequence of twelve GenBank records, with joins, partial
ends, `/codon_start` 2 and 3, the reverse strand, ambiguous bases, and
tables 1, 2, 5 and 11. A CDS naming a table NCBI doesn't have isn't
translated, and the viewer says so.

**Any of the six reading frames** can be shown too, by a genetic code chosen
(the coding sequences' own if they agree on one; not 27, 28 or 31, whose stops
the bases alone don't settle, as ADR 0172's ORF finder refuses them). A frame
takes no start codon as methionine, since nothing says a protein starts there,
and reads along the sequence as it's numbered, not round a circle's origin:
that's an ORF's job (ADR 0172). The lanes are hidden from screen readers; the
caret's readout says what its base reads as in each lane ("in INS, codon 1,
ATG: M").

## Consequences

- What a scientist checks in a construct — a site, its strand and place, a
  primer's GC and Tm, that an insert is in frame — is read in Iki without a
  desktop tool, by keyboard as well as pointer.
- The viewer computes GC content, Tm and translations in the page, so its
  numbers follow the plugins' only as long as both keep to the methods here.
  A change to the primer type's Tm conditions, such as a buffer's Mg²⁺,
  changes this ADR and the viewer with it.
- A protein record is still made by the protein plugin's `translate`; the
  lane only shows what it would read.
- A selection isn't kept in a saved view (ADR 0086), which keeps the feature
  shown; a selection is what someone is reading now, not what a view is of.
