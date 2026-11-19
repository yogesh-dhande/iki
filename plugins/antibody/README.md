# antibody

Iki's antibody plugin: antibodies and the molecules made like them, assembled
from the proteins that are their chains, what their variable domains say about
them, and their predicted structures.

Each is a complex
([ADR 0161](../../docs/adr/0161-a-complex-is-its-parts-and-their-copies.md)):
it extends `iki/core.Complex`, so its `components` are its chains, each a
protein with its `copies` and its `role`, and its identity is its chains and
their copies, whatever their roles or order. Searching `iki/core.Complex` finds
every one, beside other plugins' and a lab's own complexes; the graph finds
every complex a chain is part of; and the complex plugin computes each one's
formula and masses. Each type says which roles its chains take, how many and of
what ([ADR 0164](../../docs/adr/0164-a-subtype-says-which-roles-its-rows-take.md)),
and its `bonds`, a registry fact, are its disulfides, between its chains and
within each domain, each from a copy's cysteine to another's.

- **`iki/antibody.Antibody`** (`ABY-000001`): an antibody of one specificity,
  by its chains: a `heavy` chain and, unless it's a VHH or an scFv, a `light`
  chain.
  - A whole IgG (`igg`) is two copies of each; a Fab (`fab`), an Fv (`fv`, the
    two variable domains alone), a single domain (`vhh`, a camelid's or a
    nanobody) or both variable domains on one chain (`scfv`) one of each.
  - Its identity is its chains and their copies: each antibody is registered
    once in a registry (ADR 0115), with one ID, and assembling the same chains
    again, in any project, finds that entry. A VHH and an antibody with the
    same heavy chain and a light chain are different antibodies, and so are a
    whole IgG and a half-antibody of the same two chains, one copy of each.
    The same chains and copies registered as another complex type, such as a
    protein complex, are this antibody: that registration is refused, saying
    so.
  - Its format and isotype are what its chains are, so they don't need to be
    in its identity: an IgG1 and an IgG4 of the same variable domains have
    different heavy chains (their constant regions differ), and an IgG and a
    Fab differ in how much of the heavy chain there is. Two variable domains
    alone are an Fv, which has no isotype. `assemble` checks the format and
    isotype against the chains, reading the isotype off the heavy chain's
    constant region, and the type's rules keep what the format says of its
    chains, their copies and its isotype at every save.
  - It keeps its format (`registry: derived`), its isotype (a registry fact:
    given once, then changed only by a registrar, so a second assembly that
    claims another is refused) and what it binds (`target`).

- **`iki/antibody.MultispecificAntibody`** (`MSA-000001`): an antibody whose
  binding sites bind two or more targets, or epitopes: a bispecific or a
  trispecific.
  - Its chains are two heavy chains in all (two different ones, one copy of
    each, or one, twice), up to two copies of each of up to three light
    chains, and up to two Fd chains (a Fab arm's heavy chain without an Fc, as
    a 2+1 format's).
  - Its `binding_sites`, a registry fact of two to eight, are what binds what:
    each a heavy chain, which of its variable domains (1 for its first, 2 for
    a second fused after it), the light chain it pairs with and its target,
    by HGNC symbol (`F9`, `EGFR`, `CD3E`), with its epitope if two sites bind
    one target. Which chains pair isn't its identity: a mispaired assembly of
    the same chains is a product-related impurity of the same molecule
    (Schaefer et al., _PNAS_ 108:11187-11192, 2011), so a registration with
    other pairing is refused, as any differing fact is.
  - Its rules keep its sites made of its own chains, every light chain paired
    with a site, and more than one specificity.
  - Its `isotype` (a fact) and `fc_pairing` (derived: `knobs_into_holes`,
    `charge_pairs`, `fab_arm_exchange` or `none`) are its heavy chains'.

- **`iki/antibody.FcFusion`** (`FCF-000001`): a protein fused to an IgG's Fc,
  such as etanercept or aflibercept: one `fusion` chain twice, two different
  ones, or one with a plain `fc` chain (a monovalent fusion). Its isotype (a
  fact), how its Fc pairs (derived) and its target.

- **`iki/antibody.AntibodyAntigenComplex`** (`AAC-000001`): an antibody bound
  to its antigen, as a structure shows it: its parts are the `antibody` (an
  antibody or a multispecific, itself a complex) and up to four `antigen`s
  (proteins or complexes), each with its copies, and its `structure` links the
  structure that shows it, such as trastuzumab's Fab bound to HER2 (PDB 1N8Z,
  imported with the protein plugin), with its `epitope`.

- **Proteins' numbering** (an extension of `iki/protein.Protein`): what numbering a
  chain finds, in the plugin's own table.
  - Its variable domains (`variable_domains`), in order along the chain: each
    residue's number and insertion code, where each domain starts and ends,
    and where its CDRs are. An scFv's chain has a heavy and a light one
    (`heavy_and_light`).
  - The scheme it was numbered in (`numbering_scheme`: IMGT's, Kabat's,
    Chothia's, Martin's or AHo's), and its CDRs (`cdr_h1` to `cdr_l3`) by the
    definition chosen (`cdr_definition`: IMGT's, Kabat's, Chothia's, AbM's or
    Contact's).
  - The species whose germline V gene it's closest to (`species`), such as
    `alpaca` for a nanobody.
  - Its humanness: the human germline V gene each domain is closest to, and how
    much of its V region is the same (`heavy_germline_identity`,
    `light_germline_identity`). A human antibody is usually above 90%, a
    humanized one above 80%.
  - Its chain type, closest species and germlines follow from its sequence
    alone, so they're its registry entry's (`registry: derived`), the same in
    every project that holds it. Its numbering, CDRs and liabilities depend on
    the scheme and definition a run chose, so they're each project's record's
    (ADR 0115): numbering it again, in another scheme, replaces them in that
    project, and another project's are its own.

- **Proteins' sequence liabilities** (the same extension of `iki/protein.Protein`):
  the motifs a developability screen reports in a numbered chain
  (`liabilities`), each with where it is, whether it falls in a CDR or in
  framework, and how severe published screens call it; how many of each kind it
  has (`liability_counts`); and how many in all (`liability_count`, indexed, so
  chains can be sorted and filtered by it). A chain with none has
  `liability_count` 0, which is not the same as one that hasn't been screened
  (it has none of the three fields).

- **Predicted structures' errors** (an extension of `iki/protein.ProteinStructure`):
  for a structure `predict_structure_with_immunebuilder` made, how far ImmuneBuilder's models
  disagree (`predicted_error`, `cdr_h3_predicted_error`).

Its chains are [protein plugin](../protein) proteins, so their properties,
alignments and structures are the protein plugin's. The plugin depends on it,
and on the [dna plugin](../dna), which the protein plugin depends on.

## Actions

| Action                                 | What it does                                                                 | Tier      |
| -------------------------------------- | ---------------------------------------------------------------------------- | --------- |
| `assemble`                             | Registers the antibody its chains make, or finds its registry entry.         | backend   |
| `assemble_multispecific`               | Registers the multispecific its chains make: its sites, pairing and bonds.   | backend   |
| `assemble_fc_fusion`                   | Registers the Fc fusion its chains make: its isotype, pairing and bonds.     | backend   |
| `number_chains`                        | Numbers proteins' variable domains with ANARCI: CDRs, species and humanness. | container |
| `find_liabilities`                     | Finds the sequence liabilities a developability screen reports in a chain.   | backend   |
| `predict_structure_with_immunebuilder` | Predicts an antibody's structure with ImmuneBuilder, and how sure it is.     | container |

`number_chains`, `assemble` and `predict_structure_with_immunebuilder` list the
software they run, with the versions their environments install, so what they
make is recorded as made with it
([ADR 0106](../../docs/adr/0106-how-an-entity-was-made-recorded-by-iki-or-stated-by-its-source.md)):
ANARCI 2026.2.13.2 and HMMER 3.4 for the numbering; Biopython 1.88 for
`assemble`, whose pairwise aligner, with BLOSUM62, compares a heavy chain's
constant region with IMGT's human ones; and for the structure ImmuneBuilder 1.2,
OpenMM 8.6.1 and PDBFixer 1.12.0. `find_liabilities` is the plugin's own code,
in pure Python on the stored numbering, so it runs none.

`assemble` checks that the format and the chains agree, from where their
variable domains end and which constant domains follow, so the chains must have
been numbered first (`number_chains`):

| Format  | Chains                                                                                        | Isotype                      |
| ------- | --------------------------------------------------------------------------------------------- | ---------------------------- |
| `igg`   | A heavy chain with CH1, hinge, CH2 and CH3; a light chain with its CL                         | `igg1` to `igg4`             |
| `fab`   | A heavy chain with CH1, perhaps some hinge, but no Fc (CH2 or CH3); a light chain with its CL | Any class                    |
| `fv`    | A heavy and a light variable domain, with no constant domain                                  | None                         |
| `scfv`  | One chain with a heavy and a light variable domain, no constant domain                        | None                         |
| `vhh`   | One chain with one heavy variable domain, no constant domain                                  | None                         |
| `other` | Not checked                                                                                   | Any, given a constant region |

An immunoglobulin constant domain is about 100 residues (human IGKC's is 107,
IGHG1's CH1 98), so fewer than 90 residues after a variable domain count as no
constant domain. Which constant domains a heavy chain has is read off how its
residues after the variable domain align with the human reference they match:
at least 10 residues of a domain are that domain. So a heavy chain through
CH2 (IgG1's CH1, hinge and CH2 are 223 residues) isn't a Fab's, nor an IgG's
without CH3; a Fab' with the whole hinge, or a pepsin F(ab')2 with CH2's first
few residues, is a Fab's. Where no human reference matches (a mouse's), the
length says it instead: an IgG heavy chain's constant region is 326 to 377
residues (IGHG2's to IGHG3's), so 300 or more count as a whole one. For
`other`, a heavy chain without numbered variable domains, such as an Fc
fusion's (etanercept's, aflibercept's: numbering leaves a protein without
antibody domains as it was), is compared whole, so its isotype is checked or
taken from it too.

Where the heavy chain has a constant region, its sequence says the isotype.
`assemble` aligns the residues after the variable domain with each human heavy
chain constant region (`scripts/constant_regions.py`): IMGT's first allele of
each IGHC gene, secreted form, domain by domain (IGHG1\*01 to IGHG4\*01,
IGHA1\*01, IGHA2\*01, IGHM\*01, IGHE\*01 and IGHD\*01, from IMGT's
[protein display of the human IGH C-REGIONs](https://www.imgt.org/IMGTrepertoire/Proteins/protein/human/IGH/IGHC/Hu_IGHCallgenes.html),
CC BY 4.0), each checked against UniProt's secreted isoform of the gene
(P01857-1, P01859-1, P01860-1, P01861-1, P01876-1, P01877-1, P01871-1,
P01854-1, P01880-1) in `server/tests/test_antibody_isotype.py`. Each is
compared from CH1, as a whole constant region has it, and from the hinge, as an
Fc or a fusion to one has it. IgG3's hinge has two, three or four exons,
depending on the allele (Dard et al., _Eur J Hum Genet_ 9:765-772, 2001), so
IgG3 is compared with each: IGHG3\*01's four (X03604), IGHG3\*03's three
(X16110) and IGHG3\*04's two (X99549), as IMGT's alignment of the IGHG3 alleles
gives them; a short-hinge IgG3 isn't IgG1's. The one it's nearest is its
isotype when it's at least 90% identical to it and the next is at least 3
differences further:

- an isotype left out is the chain's, and the run says how sure (_"Its isotype
  is igg1, from its heavy chain: … IgG1's, 99.4% identical to IMGT's IGHG1\*01
  (J00228) over 329 residues; the next nearest, IgG2's, is 91.8%"_);
- one given that isn't the chain's is refused, saying which it is;
- an IgG whose constant region is another class's is refused: choose `other`;
- a hybrid, whose domains are different subclasses', may be given as any of
  them, with a warning, and is left out otherwise, as is one as near two
  references as each other. A domain that's at least 3 differences nearer
  another subclass's is that one's; one that's one subclass's along its first
  part and another's along the rest, with at least 3 fewer differences read so
  than as either's, is a crossover, said with where it is. Eculizumab's CH1,
  hinge and the start of CH2 are IgG2's and the rest of CH2 and CH3 IgG4's, as
  its review report says (PMDA, Soliris, 2010), and `assemble` says _"its CH1
  and hinge IgG2's; its CH3 IgG4's; its CH2 IgG2's to its residue 5 and IgG4's
  from its residue 37, the two the same between"_;
- one that matches no human reference (a mouse's IgG1 is about 64% identical
  to the nearest) keeps the isotype given, unchecked, with a warning.

Allotypes differ from these alleles by a residue or two, and engineered Fc
regions (S228P, LALA, YTE) by a few; subclasses differ by 20 or more over a
whole constant region, and by 4 or more over CH1 alone, so a Fab's CH1 says its
subclass too.

`assemble` gives an IgG and a Fab its inter-chain disulfides, where its
subclass places them in EU numbering (Edelman et al., _PNAS_ 63:78-85, 1969),
read off how its heavy chain's constant region aligns with IGHG1\*01, which is
EU numbering's residue for residue (Liu and May, _mAbs_ 4:17-23, 2012):

| Subclass | Light chain's last cysteine to | Heavy chain to heavy chain |
| -------- | ------------------------------ | -------------------------- |
| IgG1     | C220, the hinge's first        | C226 and C229              |
| IgG4     | C131, in CH1                   | C226 and C229              |

IgG2's light-chain disulfides take more than one arrangement (its A, B and A/B
isoforms), and IgG3's hinge has 11 between its heavy chains, so neither's are
given; nor are those of `fv`, `scfv`, `vhh` and `other`. Trastuzumab's are
KEGG DRUG's (D03257): H223-L214, H229-H′229 and H232-H′232.

It gives each domain's own disulfide too, between its two sheets, in each copy
of its chain, which KEGG DRUG lists with the others:

- a variable domain's, IMGT's C23 and C104 (Lefranc et al., _Dev Comp Immunol_
  27:55-77, 2003), Kabat's and Chothia's H22 and H92 or L23 and L88. A domain
  whose only cysteines are those two has them; in one with more (a VHH's
  CDR1–CDR3 disulfide, a CDR's unpaired cysteine) they're where its numbering
  puts them, and the rest are left out;
- an IgG heavy chain's CH1's, CH2's and CH3's, EU C144–C200, C261–C321 and
  C367–C425, which every subclass has, where its constant region was read as
  one human subclass's; an Fc fusion's CH2's and CH3's alone, as what's fused
  to its hinge has disulfides of its own that aren't an IgG's;
- a light chain's CL's, its two cysteines before its last.

So trastuzumab has 16 (KEGG's H22-H96, H147-H203, H264-H324, H370-H428,
L23-L88 and L134-L194 in each copy, as well as those between its chains),
emicizumab, amivantamab and mosunetuzumab 16 each, and etanercept 7.
Its cystines, counted from these, are what the complex plugin's absorbance at
280 nm adds.

What can be told from the antibody's own fields is a rule of its type
([ADR 0127](../../docs/adr/0127-plugins-rules-are-checked-at-every-save.md)),
kept at every save however it's registered: by `assemble`, through the API, a
bulk import or a bundle. An IgG, a Fab and an Fv have a light chain, a VHH and
an scFv none; an IgG has two copies of each chain, the others one; an Fv, scFv
or VHH has no isotype; an IgG's is one of its subclasses. The two chains
differ, as every complex's parts do: a part is given once, with its copies. Whether a chain has the constant region its
format says is `assemble`'s check alone: it reads where the chain's variable
domains end, its project's numbering, which a rule can't, as a reference holds
the chain's registry entry. `assemble` names the antibody after its chains
unless it's given a name.

### Multispecifics and Fc fusions

`assemble_multispecific` takes one or two heavy chains and up to two light
chains, numbered first, and the `targets` of its binding sites, in order. Its
sites are read off each heavy chain's variable domains, paired with a light
chain's by their order: a heavy chain's first VH with the light chain given
with it (`light_chain` for `heavy_chain`, `second_light_chain` for
`second_heavy_chain`, or the one both share). A light chain both arms share
is one part with two copies. Its isotype is its heavy chains' constant
regions', which must be one subclass's, and their pairing is read off their CH3
domains at EU numbers:

- **knobs into holes**: T366W in one heavy chain and T366S, L368A and Y407V in
  the other (Ridgway et al., _Protein Eng_ 9:617-621, 1996; Atwell et al.,
  _J Mol Biol_ 270:26-35, 1997), as mosunetuzumab's;
- **Fab-arm exchange**: F405L in one and K409R in the other, Genmab's DuoBody
  (Labrijn et al., _PNAS_ 110:5145-5150, 2013), as amivantamab's;
- **charge pairs**: charges swapped between them at two or more of EU 356, 357,
  370, 392, 399, 405, 409 and 439 (Gunasekaran et al., _J Biol Chem_
  285:19637-19646, 2010), such as emicizumab's E356K and K439E (Sampei et al.,
  _PLoS ONE_ 8:e57479, 2013).

Its disulfides are each heavy chain's to its light chain and the two heavy
chains' to each other, as for an IgG, and each domain's own. These match KEGG
DRUG's for each, all 16, and its targets the review reports'; between its
chains:

| Antibody             | KEGG DRUG | Sites                                       | Pairing          | Disulfides                                  |
| -------------------- | --------- | ------------------------------------------- | ---------------- | ------------------------------------------- |
| Emicizumab (IgG4)    | D10821    | F9 (448-residue chain), F10 (444)           | charge pairs     | H137-L214, h133-L′214, H229-h225, H232-h228 |
| Amivantamab (IgG1)   | D11894    | EGFR (A with C), MET (B with D)             | Fab-arm exchange | A228-C214, B222-D214, A234-B228, A237-B231  |
| Mosunetuzumab (IgG1) | D11463    | CD3E (hole A with B), MS4A1 (knob C with E) | knobs into holes | A222-B219, C225-E213, A228-C231, A231-C234  |

Emicizumab's arms are Japan's PMDA's (review report, Hemlibra, 2018): its
448-residue heavy chain binds factor IXa and its 444-residue one factor X.
Amivantamab's EGFR arm is zalutumumab's, 100% identical in its variable domains
(KEGG D10031). Mosunetuzumab's CD20 arm is its knob (US 11,958,906).

Formats it can't assemble, because their chains aren't one heavy chain's VH
with its own light chain, are registered through the form, the API or a
bundle, with their sites given, and the type's rules check them: a 2+1 format's
Fd chain, a CrossMab's crossed domains, a CODV or a trispecific with a light
chain fused to a heavy one, such as tilrekimig (KEGG D12962; IL4, IL13 and
TSLP).

`assemble_fc_fusion` takes a fusion chain and, optionally, a second: another
fusion, or a plain Fc, whose hinge starts within its first 25 residues. Its
isotype is its Fc's, compared whole. Without a light chain, an IgG1 hinge's
first cysteine (EU C220) bonds the other chain's: etanercept (KEGG D00742) has
three inter-chain disulfides, C240, C246 and C249 to their twins, as Lamanna et
al. found (_Sci Rep_ 7:3951, 2017), and aflibercept (D09574), fused after that
cysteine, two, C211 and C214, as KEGG lists them; and its Fc's CH2 and CH3
have one each, aflibercept's at KEGG's 246-306 and 352-410.

`number_chains` runs [ANARCI](https://github.com/oxpig/ANARCI) (BSD-3-Clause)
with HMMER's hmmscan, in the plugin's `numbering-tools` image
(`images/numbering-tools`), which Iki's publish workflow builds, scans and
signs. ANARCI numbers each chain twice: with every species' models and
germlines, for the numbering and the closest species, then with human
germlines alone, for humanness. Its germlines are
[IMGT's](https://www.imgt.org) (CC BY 4.0): cite IMGT when you publish what
they found. A protein without an antibody domain, or with a residue ANARCI
can't read (such as U), is left as it was, and the run says which.

### Numbering schemes and CDR definitions

`scheme` says how residues are numbered: [IMGT's](https://www.imgt.org/IMGTScientificChart/Numbering/IMGTIGVLsuperfamily.html),
Kabat's, Chothia's, Martin's (enhanced Chothia: Abhinandan and Martin, _Mol
Immunol_ 45:3832-3839, 2008) or AHo's (Honegger and Plückthun, _J Mol Biol_
309:657-670, 2001), all as ANARCI numbers them. `cdr_definition` says where
the CDRs are. Each definition's boundaries are published in one numbering, so
the CDRs are read in that numbering, which ANARCI numbers the chain in again if
it isn't the chain's: a definition finds the same residues whichever scheme
numbers the chain. Each domain keeps where its CDRs are in the chain.

| Definition | Read in   | CDR-H1   | CDR-H2  | CDR-H3   | CDR-L1  | CDR-L2  | CDR-L3  |
| ---------- | --------- | -------- | ------- | -------- | ------- | ------- | ------- |
| `imgt`     | IMGT's    | 27-38    | 56-65   | 105-117  | 27-38   | 56-65   | 105-117 |
| `kabat`    | Kabat's   | H31-H35B | H50-H65 | H95-H102 | L24-L34 | L50-L56 | L89-L97 |
| `chothia`  | Chothia's | H26-H32  | H52-H56 | H95-H102 | L24-L34 | L50-L56 | L89-L97 |
| `abm`      | Chothia's | H26-H35  | H50-H58 | H95-H102 | L24-L34 | L50-L56 | L89-L97 |
| `contact`  | Chothia's | H30-H35  | H47-H58 | H93-H101 | L30-L36 | L46-L55 | L89-L96 |

IMGT's are Lefranc et al.'s (_Dev Comp Immunol_ 27:55-77, 2003). Kabat's, AbM's
and Contact's are as A. C. R. Martin tabulates them
([bioinf.org.uk/abs](http://www.bioinf.org.uk/abs/info.html); Martin, in
_Antibody Engineering_ vol. 2, 2010, Table 3.4), each in the numbering that
table gives it in; AbM's and Kabat's CDR-H1 read in Chothia's numbering
(H26-H35, H31-H35) are the same residues as in Kabat's (H26-H35B, H31-H35B).
Chothia's are the wider set Martin's table gave until 2021, which SAbDab still
uses; the table now gives a narrower consensus (H26-H32, H52-H56, H96-H101,
L26-L32, L50-L52, L91-L96), which isn't implemented, because no primary source
of the Chothia group's defines it. It's Martin's own reading, from 28 June 2021, of the Chothia
group's papers, which his table shows disagreeing on every loop: CDR-L1 is
26-32, 26-33, 25-32 or 25-33, CDR-H2 53-55, 52a-55, 52-56 or 50-58, and CDR-H3
95-101, 96-101, 95-102 or 92-104, from Chothia et al. 1986 (_Science_
233:755-758) and Chothia and Lesk 1987 (_J Mol Biol_ 196:901-917) to
Al-Lazikani et al. 1997 (_J Mol Biol_ 273:927-948) and Morea et al. 1998
(_J Mol Biol_ 275:269-294). The consensus's own CDR-H2 is marked "possibly the
most controversial: it's very clearly H52a-H55 in some of the papers", and its
CDR-H1 in Kabat's numbering ends at H32, H33 or H34 depending on the loop's
length. Zhu, Olson and Magliery tabulate the set (_Antibodies_ 13(4):99, 2024,
Table 1, "Consensus Chothia"), citing Martin's table for it, and on
trastuzumab's light chain it gives CDR-L2 as SAS where the wider set gives
SASFLYS; but none of the Chothia papers defines it (the earliest, 1986 and
1987, differ from it at CDR-H2: 53-55), so a CDR labelled with it would be
traced to Martin's reading of them rather than to a definition of theirs. A CDR keeps its endpoints'
insertions. Kabat's numbering has room for two at H35 (H35A, H35B), where
Martin's table ends Kabat's CDR-H1; ANARCI numbers a longer CDR-H1's further
residues H35C onwards, still before the conserved W36, and these are kept in
CDR-H1, as AbNumber's Kabat borders also have it.

Without a `cdr_definition`, a scheme's CDRs are its own: IMGT's, Kabat's or
Chothia's for those numberings; Chothia's for Martin's, whose numbering is
Chothia's in the CDRs and differs only in framework; and IMGT's for AHo's,
which numbers for structural alignment and defines no CDRs of its own.

### Sequence liabilities

`find_liabilities` reads a chain's liabilities off the numbering `number_chains`
stored, so a chain has to be numbered first; one that hasn't been is left as it
was, and the run says so. It's the plugin's own code (`scripts/liabilities.py`),
pure Python on the stored numbering, so it runs on the backend tier and lists no
software. Each liability it reports gives its `kind`, the residues that make it
(`motif`), where it is (`position`, the index in the sequence from 1, and
`number`, its number in the scheme the chain was numbered in, insertion code
included, for a residue in a variable domain), whether it falls in a CDR
(`region`: `cdr_h1` … `cdr_l3`), in `framework` or `outside` the variable
domains, and its `severity`.

These are the rules it implements, and nothing else. The motifs and severities
are the ones published developability screens use, chiefly the liability table
of [LAP](https://doi.org/10.1371/journal.pcbi.1011881) (Satława et al., _PLoS
Comput Biol_ 20(3):e1011881, 2024, Table 1), whose scope per motif (a CDR, or
the whole variable fragment) this follows:

| Kind                    | Motif                                   | Where          | Severity                   |
| ----------------------- | --------------------------------------- | -------------- | -------------------------- |
| `n_glycosylation`       | `N-X-S/T`, X not proline                | A whole domain | high                       |
| `deamidation`           | `NG`, `NS`, `NT`, `NN`                  | A CDR          | high for `NG`, else medium |
| `isomerisation`         | `DG`, `DS`, `DT`, `DD`                  | A CDR          | high for `DG`, else medium |
| `fragmentation`         | `DP`                                    | A CDR          | high                       |
| `oxidation`             | `M`, `W`                                | A CDR          | medium                     |
| `noncanonical_cysteine` | `C` away from the domain's disulphide   | A whole domain | high                       |
| `pyroglutamate`         | `Q` or `E` at the chain's first residue | The N-terminus | low                        |

- **N-glycosylation**: the sequon an N-linked glycan may be added at, a
  potential site: whether it's occupied needs measuring. LAP's `Ngly`, over the
  variable fragment; here, within a variable domain ANARCI numbered, so a sequon
  that runs past a domain's end isn't counted. A sequon whose middle residue is
  unknown (`X`, which may be proline, and then it's no sequon) is reported with
  `uncertain: true`. An `X` matches no other motif, which each need a residue
  of their own.
- **Deamidation** and **isomerisation**: `NG` and `DG` are ranked above the
  others because they're the most labile, as measured across 131 clinical-stage
  antibodies: deamidation falls off in the order NG, NS, NT, and DG isomerises
  more than DS, which isomerises more than DD or DT
  ([Lu et al.](https://doi.org/10.1080/19420862.2018.1548233), _mAbs_
  11(1):45-57, 2019, whose hot spots are Kabat H54 (NG), H98 (DG) and CDR-L1).
  LAP puts `N[GS]` together as high; this follows the measured order instead and
  leaves `NS` at medium. Both sources count them in the CDRs alone, so a motif
  in framework isn't reported.
- **Noncanonical cysteines**: every cysteine in a variable domain other than the
  two that make its conserved disulphide, at IMGT 23 and 104 (Kabat's, Chothia's and Martin's
  numbering of the same residues, heavy 22 and 92, light 23 and 88; AHo's 23 and
  106). If either of those
  positions isn't a cysteine, the other has no canonical partner, so it's
  reported too; a fragment that doesn't reach one of them (a VH that starts at
  IMGT 26, say) says nothing of its partner, which isn't reported. LAP's `xCys` and `mCys`. Sequence alone can't say whether such
  a cysteine is free: it may bond another, as a camelid VHH's CDR1 and CDR3
  cysteines often do (PDB 1MEL's Cys33 and Cys109, IMGT 38 and 111F). It's a
  site to check, not an unpaired cysteine.
- **Oxidation**: methionine and tryptophan in a CDR. LAP's `MetOx` and `TrpOx`.
- **Fragmentation**: `DP` in a CDR. LAP's `FragH`.
- **Pyroglutamate**: a glutamine or glutamate at the chain's first residue
  cyclises, whether or not a variable domain starts there: one after a tag or
  a linker has no `number` and its `region` is `outside`. It makes charge
  variants rather than changing what the antibody does, so it's low ([Liu et al.](https://doi.org/10.1074/jbc.M110.185041),
  _J Biol Chem_ 286(13):11211-11217, 2011). It isn't in LAP's table.

The [Therapeutic Antibody Profiler](https://doi.org/10.1073/pnas.1810576116)
(Raybould et al., _PNAS_ 116(10):4025-4030, 2019) reports such motifs alongside
its five developability guidelines; those guidelines are structural, and this
action doesn't compute them. LAP's own `Hydro` (`NP`), `FragM` (`TS`), low
`[STK]N` deamidation and integrin-binding motifs aren't implemented.

A liability is a site to look at, not a defect: what a screen reports needs
measuring before it's believed. Which CDR definition a chain's numbering used
decides where its CDRs are, and which scheme numbered it how a liability is
numbered, so renumbering a chain changes the regions and numbers of the same
residues: `number_chains` finds a screened chain's liabilities again on the new
numbering, so they're never left on the old one's.

`liabilities` and `liability_counts` are JSON, which a chain's page shows as it
is, under its fields; `liability_count` is an indexed integer, so chains can be
filtered and sorted by how many they have, in the grid and through the API.

`predict_structure_with_immunebuilder` runs [ImmuneBuilder](https://github.com/oxpig/ImmuneBuilder)
(BSD-3-Clause) in the plugin's `structure-tools` image (`images/structure-tools`),
with its trained weights ([Zenodo 7258553](https://zenodo.org/records/7258553),
CC BY 4.0: cite its paper when you publish its structures). Its input includes the
antibody's chains (ADR 0105), so it reads their sequences as they were when it was
launched. ABodyBuilder2 models a heavy and a light chain's variable domains, from
an IgG's or a Fab's two chains or an scFv's one; NanoBodyBuilder2 a VHH's single
domain. Each runs four models and keeps the one closest to the others, refined
with OpenMM. It makes an `iki/protein.ProteinStructure` of the antibody's chains
(chain IDs H and L), derived from the antibody, with how far the four models
disagree: `predicted_error` over every residue and `cdr_h3_predicted_error` over
CDR-H3, in Å, and each residue's in the file as its B-factor. Refining isn't
exactly repeatable, so the action isn't deterministic: running it again makes a
new structure, with the same estimated errors. An antibody takes about a minute on one CPU, and about 15 seconds on four.

The image carries OpenMM's licences, which its wheel doesn't: OpenMM is MIT, and
the OpenCL platform its wheel carries, which these runs don't use, LGPL-3.0.

## Tests

```console
iki plugin test plugins/antibody --with plugins/dna --with plugins/protein
```

`tests/assemble_multispecific.yaml` assembles emicizumab, amivantamab and
mosunetuzumab from KEGG DRUG's chains, and `tests/assemble_fc_fusion.yaml`
etanercept and aflibercept, each checked against KEGG's disulfides. The dev
cluster's end-to-end check assembles emicizumab and etanercept, waits for the
complex plugin to compute their compositions, and finds both by searching
`iki/core.Complex`.

`server/tests/test_antibody_numbering.py` runs `number_chains`' script with a
stand-in ANARCI that answers what the real one said in the image
(`server/tests/data/record_anarci.py` records it). The image's `check.py`
runs the real one on trastuzumab, and the dev cluster's end-to-end check
numbers trastuzumab's chains in a pod of their own.

`tests/find_liabilities.yaml` screens made-up domains, where each rule is in one
place, and `server/tests/test_antibody_liabilities.py` screens real chains on
the numbering ANARCI gave them, through the same stand-in. The dev cluster's
end-to-end check screens trastuzumab's chains after numbering them.

`server/tests/test_antibody_structure.py` runs `predict_structure_with_immunebuilder`'s script with a
stand-in ImmuneBuilder. The structure-tools image's `check.py` models
trastuzumab's Fv and the GFP enhancer nanobody with the real one, and the
end-to-end check predicts trastuzumab's structure in a pod of its own.
