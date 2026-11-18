# antibody

Iki's antibody plugin: antibodies, assembled from the proteins that are their
chains, what their variable domains say about them, and their predicted
structures.

- **`iki/antibody.Antibody`** (`ABY-000001`): an antibody, by its chains.
  - A whole IgG (`igg`), a Fab (`fab`) or an Fv (`fv`, the two variable
    domains alone) has a heavy chain and a light chain. A single domain
    (`vhh`, a camelid's or a nanobody) or both variable domains on one chain
    (`scfv`) has one chain, its `heavy_chain`.
  - Its identity is its heavy chain and its light chain, if it has one: each
    antibody is registered once in a registry (ADR 0115), with one ID, and
    assembling the same chains again, in any project, finds that entry. A VHH
    and an antibody with the same heavy chain and a light chain are different
    antibodies
    ([ADR 0104](../../docs/adr/0104-an-identity-can-name-an-optional-field.md)).
  - Its format and isotype are what its chains are, so they don't need to be
    in its identity: an IgG1 and an IgG4 of the same variable domains have
    different heavy chains (their constant regions differ), and an IgG and a
    Fab differ in how much of the heavy chain there is. Two variable domains
    alone are an Fv, which has no isotype. `assemble` checks the format and
    isotype against the chains.
  - It keeps its format (`registry: derived`), its isotype (a registry fact:
    given once, then changed only by a registrar, so a second assembly that
    claims another is refused) and what it binds (`target`).

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
| `number_chains`                        | Numbers proteins' variable domains with ANARCI: CDRs, species and humanness. | container |
| `find_liabilities`                     | Finds the sequence liabilities a developability screen reports in a chain.   | backend   |
| `predict_structure_with_immunebuilder` | Predicts an antibody's structure with ImmuneBuilder, and how sure it is.     | container |

`number_chains` and `predict_structure_with_immunebuilder` list the software
they run, with the versions their images install, so what they make is recorded
as made with it
([ADR 0106](../../docs/adr/0106-how-an-entity-was-made-recorded-by-iki-or-stated-by-its-source.md)):
ANARCI 2026.2.13.2 and HMMER 3.4, and for the structure ImmuneBuilder 1.2,
OpenMM 8.6.1 and PDBFixer 1.12.0 as well. `assemble` and `find_liabilities` are
the plugin's own code, in pure Python, so they run none.

`assemble` checks that the format and the chains agree, from where their
variable domains end, so the chains must have been numbered first
(`number_chains`):

| Format  | Chains                                                                 | Isotype          |
| ------- | ---------------------------------------------------------------------- | ---------------- |
| `igg`   | A heavy chain with CH1, hinge, CH2 and CH3; a light chain with its CL  | `igg1` to `igg4` |
| `fab`   | A heavy chain with CH1 but no Fc; a light chain with its CL            | Any class        |
| `fv`    | A heavy and a light variable domain, with no constant domain           | None             |
| `scfv`  | One chain with a heavy and a light variable domain, no constant domain | None             |
| `vhh`   | One chain with one heavy variable domain, no constant domain           | None             |
| `other` | Not checked                                                            | Any, given a CH1 |

An immunoglobulin constant domain is about 100 residues (human IGKC's is 107,
IGHG1's CH1 98) and an IgG heavy chain's constant region 326 to 377 (IGHG2's to
IGHG3's), so fewer than 90 residues after a variable domain count as no
constant domain, and 300 or more as an IgG's whole constant region. Which class
a constant region is isn't read from its sequence: the isotype given is
checked against the format and that there's a constant region. These checks
are `assemble`'s: an antibody registered directly, through the API, isn't
checked. It names the antibody after its chains unless it's given a name.

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
L26-L32, L50-L52, L91-L96), which isn't implemented. A CDR keeps its endpoints'
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
included), whether it falls in a CDR (`region`: `cdr_h1` … `cdr_l3`) or in
`framework`, and its `severity`.

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
  reported too. LAP's `xCys` and `mCys`. Sequence alone can't say whether such
  a cysteine is free: it may bond another, as a camelid VHH's CDR1 and CDR3
  cysteines often do (PDB 1MEL's Cys33 and Cys109, IMGT 38 and 111F). It's a
  site to check, not an unpaired cysteine.
- **Oxidation**: methionine and tryptophan in a CDR. LAP's `MetOx` and `TrpOx`.
- **Fragmentation**: `DP` in a CDR. LAP's `FragH`.
- **Pyroglutamate**: a glutamine or glutamate at the chain's first residue
  cyclises, which makes charge variants rather than changing what the antibody
  does, so it's low ([Liu et al.](https://doi.org/10.1074/jbc.M110.185041),
  _J Biol Chem_ 286(13):11211-11217, 2011). It isn't in LAP's table.

The [Therapeutic Antibody Profiler](https://doi.org/10.1073/pnas.1810576116)
(Raybould et al., _PNAS_ 116(10):4025-4030, 2019) reports such motifs alongside
its five developability guidelines; those guidelines are structural, and this
action doesn't compute them. LAP's own `Hydro` (`NP`), `FragM` (`TS`), low
`[STK]N` deamidation and integrin-binding motifs aren't implemented.

A liability is a site to look at, not a defect: what a screen reports needs
measuring before it's believed. Which CDR definition a chain's numbering used
decides where its CDRs are, and which scheme numbered it how a liability is
numbered, so screening the same chain after renumbering it can report different
regions and numbers for the same residues.

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
