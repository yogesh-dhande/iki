# 0205. A guide design says where a nuclease can cut, and how particular each place is

- Status: Accepted
- Date: 2026-11-23
- Refines: [0172](0172-what-the-bases-say-orfs-primer-sites-and-the-product-of-a-pcr.md),
  [0163](0163-a-plasmid-is-a-record-about-a-sequence-not-a-kind-of-sequence.md),
  [0138](0138-an-identity-reads-sequences-and-sets-of-parts-as-what-they-are.md)

## Context

A CRISPR experiment begins with picking a guide, and picking one is two
questions: where can this nuclease cut, and will it cut anywhere else.
Benchling, SnapGene and Geneious answer both; so do CHOPCHOP and CRISPOR,
which labs use alongside them. Iki answered neither. A lab designed its
guides in one of those tools, ordered the oligos, and registered the edited
cell line afterwards with nothing connecting it to the guide that made it.

The hard part is not finding the sites. It is **what Iki is willing to
assert**. The field's scores come with very different provenance: some are
published formulas anybody can check, and some are fitted models whose
weights were never published at all. A platform that prints them all as
numbers of equal standing is telling a lab something it cannot support.

## Decision

### A design is a record; the guides are a field of it

`iki/dna.GuideDesign` names the target, the stretch of it the cut must fall
in, the nuclease, and the sequences to search for other places the guides
could cut. `design_guides` reads both strands and records every site.

The design has no identity, as a codon optimization and a mutagenesis have
none: the same gene is targeted differently for different nucleases and
against different genomes, and which design a lab used is the lab's record.

**The guides are not entities.** A guide is a twenty-base stretch of a
sequence Iki already has, and nothing is ordered until a lab picks one, so
registering every site as an oligo would register thousands of molecules
nobody will ever make. The design keeps them in a field; the oligos a lab
goes on to order are registered where oligos are, and derive from what the
lab actually ordered.

### Each nuclease is placed on the motif its own paper reports

A guide takes a nuclease to one place by base pairing, but only where the DNA
also carries that nuclease's protospacer adjacent motif, which the guide
cannot change. So the motif, the spacer length and the cut geometry are per
nuclease, and each is cited:

- **SpCas9** reads `NGG` 3' of a 20-base protospacer and cuts both strands
  bluntly between the third and fourth base 5' of the PAM (Jinek et al.,
  _Science_ 337:816, 2012; Gasiunas et al., _PNAS_ 109:E2579, 2012).
- **SaCas9** reads `NNGRRT` 3' of a 21- to 23-base protospacer (Ran et al.,
  _Nature_ 520:186, 2015).
- **AsCas12a and LbCas12a** read `TTTV` **5'** of the protospacer — the other
  end from Cas9 — and cut staggered, after the 18th base of the protospacer
  strand and the 23rd of the other, leaving a five-base 5' overhang (Zetsche
  et al., _Cell_ 163:759, 2015, for the cut; Kim HK et al., _Nat Methods_
  14:153, 2017, for `TTTV`).
- **FnCas12a** reads the looser `TTN` that its own paper reports (Zetsche et
  al., 2015).

`TTTV` is often attributed to Kleinstiver et al. (2016), which reports `TTTN`
for the variants it engineered; the `TTTV` that design tools use for
AsCas12a and LbCas12a is Kim HK et al. (2017)'s, and that is what Iki cites.

**A design places a guide only on the motif a paper reports for design, and
the off-target search allows every motif the same paper reports the nuclease
cleaving.** SpCas9 also cuts, less well, at `NAG` (Hsu et al., _Nat
Biotechnol_ 31:827, 2013); SaCas9 cleaves at `NNGRR` without the T (Ran et
al., 2015). So a guide is never put on the worse motif, and a search never
pretends the worse motif isn't cut. **A site with an ambiguous base in it is
not offered**, since a guide has to be ordered as bases.

A circular target has no end, so the search reads past its last base into its
first: a motif straddling the origin is a motif, and a search reading the
plasmid as a line would miss every guide there.

### What is known to make a guide work less well is flagged, not scored

Each flag names the work it comes from, and none is rolled into a number:

- **GC outside 40–80%** — the band CHOPCHOP keeps to (Labun et al., _Nucleic
  Acids Res_ 44:W272, 2016). It is that tool's rule, not a result from the
  efficiency papers, and the record says so.
- **`TTTT` in the spacer** — RNA polymerase III reads a run of T as a
  terminator, so the guide is transcribed short (Bogenhagen and Brown, _Cell_
  24:261, 1981; Doench et al., _Nat Biotechnol_ 32:1262, 2014).
- **No G at the 5' end** — the U6 promoter starts transcription at a purine,
  so a G is usually added or the first base changed (Mali et al., _Science_
  339:823, 2013; Ran et al., _Nat Protoc_ 8:2281, 2013).
- **Self-complementarity** — a stretch of the spacer that pairs with another
  stretch of itself competes with the DNA the guide is meant to find.

**Iki does not compute Doench's Rule Set 2 score, and this is deliberate.**
The weights behind it (Doench et al., _Nat Biotechnol_ 34:184, 2016) were
never published: they exist only as a pickled scikit-learn model shipped with
the authors' code. A number called "the Doench score" here would be a number
nobody could check against anything, and reproducing it would mean vendoring
a binary model file whose contents Iki cannot read, review or cite. The
flags above are what Iki can state and support.

**Iki does not compute the CFD score either.** Its mismatch table _is_
published, as Supplementary Table 19 of the same paper, but the supplement
carries no licence; the redistributable copies exist because Bioconductor's
crisprScore relicensed its own transcription under MIT. Taking a table from a
paper's supplement and shipping it as plugin data is a licensing question,
not a scientific one, and it is not one to answer by assuming.

### Specificity is the CRISPR Design tool's published formula, cited as that

Every place in the searched sequences matching a guide within the stated
number of mismatches, beside a motif the nuclease cleaves, is listed with
**which bases differ**. For SpCas9 each is scored, and the guide given an
aggregate specificity, by the formulas the CRISPR Design tool published in
its own "Scores of single hits" documentation at `crispr.mit.edu/about`
(read from the Internet Archive; identical in every snapshot from 2013 to
2018):

```
S_hit  = Π over mismatched e of (1 − W[e])  ×  1 / ( ((19 − d̄)/19) × 4 + 1 )  ×  1 / n²
S_guide = 100 / (100 + Σ S_hit)
```

Three things about this are easy to get wrong, and each is recorded in the
code and pinned by a test:

- **The weight vector is the website's, not the paper's.** The page
  attributes the measurements to Hsu et al. (2013), and the paper describes
  the tool, but the twenty numbers appear nowhere in the paper's text. Iki
  cites the archived page as their source and Hsu et al. for the experiments
  behind them.
- **Index 1 is the PAM-distal end.** The website's vector is ordered that
  way; the paper numbers positions "1–19 directly 5' of the requisite NGG
  PAM", which is the opposite. Reading the vector the paper's way round puts
  every weight on the wrong base — and since the weights are near zero at one
  end and large at the other, the result looks plausible and is wrong.
- **`d̄` is the mean of the gaps between consecutive mismatches**, not the
  mean over every pair. For mismatches at 1, 2 and 11 those are 5 and 6⅔,
  and they give different scores.

A guide's own site is not one of its off-targets. Any _other_ perfect match
is, and scores the full 100, which halves the guide's specificity — which is
what makes the shared ampicillin resistance gene of pUC19 and pBR322 a real
test case rather than a constructed one.

**No specificity is reported where the formula doesn't apply:** where the
design named nothing to search, and for any nuclease but SpCas9, whose
20-base protospacer the weights were measured on. The record says which, in
words, because **an absent number must not read as a good one** — a
specificity of nought would say the guide cuts everywhere.

**The implementation is Iki's own, from the published formula.** CRISPOR is
the one surviving implementation, and its source has not been open since
version 4: it is free for academic use only, and commercial and non-profit
users must obtain a licence from UCSC. So neither its code nor the expected
values embedded in it are vendored, copied or adapted here. Iki's tests check
the formula against values worked by hand from the published page, and
against two anchors it fixes exactly: a place matching a guide perfectly
scores 100, and a single mismatch at the PAM-proximal end scores
100 × (1 − 0.583) = 41.7.

**CRISPOR's extra heuristics are not Iki's.** It multiplies a hit's score by
0.2 where the off-target's motif is not `GG`, which its own source calls a
heuristic and the published formula does not contain. Iki reports the motif
beside the score and leaves the weighing to the reader, rather than applying
an adjustment it would have to attribute to a tool rather than a paper.

### It runs on the server, not in `iki-wasm`

`iki-wasm` holds the science the sequence viewer computes while a person
interacts with it -- a melting temperature following a selection, annotation
matching as a sequence is drawn -- work that has to happen between frames. A
guide design is not that: a region and a nuclease are stated, a search is
asked for, and the person reads the guides that come back. Nothing in the
viewer needs a specificity recomputed per frame, so it runs server-side and
there is no second implementation, and no digest to keep in step with one. If
guides are ever to be drawn live as someone scrolls a long sequence, that is
the moment to move the scoring into Rust, and this paragraph will say what
changed.

## Consequences

- A lab finds its guides, sees what is against each and where else each could
  cut, inside Iki, with every number traceable to something a reader can
  open.
- **Iki's guide list is shorter on numbers than some tools'.** There is no
  single "efficiency score" and no CFD. The flags say what is known; the
  reasons for the two omissions are above, and both are reversible — Rule Set
  2 if its weights are ever published, CFD if its table's licence is ever
  settled.
- **Specificity is only ever as good as what was searched.** Against one
  plasmid it means little; against the genome the edit is to be made in it
  means what the tools mean by it. The record names the sequences and their
  lengths so the number can be read as what it is, and warns when nothing was
  given.
- **The search is exact within a mismatch budget, over the sequences named.**
  It is not an index over a whole genome: searching a mammalian genome this
  way is slow, and a lab doing that today uses a tool built around an
  aligner. A design that names a 3 Gb target will take a long time, which the
  action's timeout bounds rather than hides.
- **Nothing is simulated.** Iki says where a nuclease can cut and what else
  it matches; whether the cut happened, whether the repair went the way the
  lab wanted, and what the sequencing read back are the bench's, and the
  record of them is the lab's.
- **No guide is registered as an oligo.** A lab that orders one registers it
  where oligos are registered; the design is what it was picked from.
