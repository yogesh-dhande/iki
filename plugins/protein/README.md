# protein

Iki's protein plugin: proteins, translated from DNA, imported or fetched from
UniProt, with the properties of their sequences, and their structures, from
the PDB, AlphaFold DB or a lab's own files, with their secondary structure, and alignments
of proteins, two or many.

- **`iki/protein.Protein`** (`PRT-000001`): a protein, by its amino acid sequence.
  - Its identity is its sequence: each protein is registered once in a
    registry (ADR 0115), with one ID, and registering it again, in any
    project, finds that entry. A project that hadn't got it gets its own
    record of it; one that had finds its record.
  - Each project's record keeps the UniProt accession and gene it came from,
    where it has them
    ([ADR 0154](../../docs/adr/0154-an-accession-is-the-records-not-the-entrys.md)):
    one sequence is several genes' and species' UniProtKB entries, as
    calmodulin is CALM1's P0DP23 and CALM2's P0DP24, and each import or fetch
    keeps the entry it read as its origin. It keeps the DNA sequence it was
    translated from (`encoded_by`) too.
  - Its length, molecular weight, isoelectric point, extinction coefficients,
    GRAVY, instability index and hydropathy along its sequence are computed by
    the plugin's actions. Its Sequence tab draws the hydropathy as a track.

- **`iki/protein.ProteinStructure`** (`PST-000001`): a three-dimensional
  structure, experimental or predicted, in mmCIF or PDB format.
  - Its identity is its file, registered as a protein's is. Its method,
    resolution and chains are its entry's, and so is its PDB ID, a registry
    fact only its registrars change once it's set.
  - It keeps its PDB ID, title, method, resolution (and, for one determined
    by several methods, each one's, in `resolutions`), for a PDB entry its
    first release, and the protein each of its chains is (`chains`, in the
    order of `chain_ids`).
  - DSSP gives each chain's secondary structure, residue by residue, the
    shares of its residues in helices and strands, and its solvent-accessible
    surface, and keeps its annotated mmCIF (`dssp`).

`Protein` extends `iki/core.Sequence`, so a search for sequences finds proteins
alongside DNA. The plugin depends on the [dna plugin](../dna) for
`encoded_by`.

A project Admin can enable **Compute each new protein’s properties** to run
the calculation when a new sequence is registered. For a lab that calculates
properties after translation, the [translation workflow example](../../examples/plugins/protein_translation)
shows how to chain the two operations in a lab-owned plugin.

## Actions

Most run on backend workers, in the `protein-tools` environment (Python 3.12
with Biopython and gemmi). The structure actions ask for 2 GiB of memory.
DSSP and MAFFT run on the container tier, in the plugin's own images, which
Iki's publish workflow builds, scans and signs
([ADR 0065](../../docs/adr/0065-first-party-plugins-images-are-built-and-signed-like-ikis.md)):
`structure-tools`, from `images/structure-tools`, and `alignment-tools`, from
`images/alignment-tools`, which asks for at least 1 CPU and 2 GiB
(MAFFT uses as many CPUs as it's given).

Each action that computes lists its software, by name and the version its
environment installs, so what it makes is recorded as made with it
([ADR 0106](../../docs/adr/0106-how-an-entity-was-made-recorded-by-iki-or-stated-by-its-source.md)):
Biopython 1.88 for `compute_properties`, `translate`, `align_pair` and
`align_reads`, MAFFT 7.505 for `align_multiple`, and DSSP 4.4.10 for
`assign_secondary_structure`. The imports and fetches compute nothing, so
they list none.

- **`compute_properties`**: a protein's properties, as ExPASy's ProtParam
  computes them, for many at once.
- **`translate`**: translates a DNA sequence in the strand, frame and NCBI
  genetic code chosen, stopping at the first stop codon or keeping each as
  `*`, or as a complete coding sequence, and registers the protein, linked to
  the DNA. Many at once.
- **`import_proteins`**: registers each record of a FASTA file (up to 10
  million, and 1 GiB) as a protein. A protein already registered is found,
  and the project gets its record of it, or keeps the one it has. Each
  records the file and record it came from, with the record's SHA-256.
- **`fetch_proteins`**: fetches UniProtKB entries by accession (up to 1,000,
  such as `P69905`, or `P69905-2` for an isoform) and registers each as a
  protein. Each records the entry, the UniProt release and sequence version,
  its link, when it was fetched and its SHA-256. It reaches only
  `rest.uniprot.org`, through the deployment's egress proxy.
- **`fetch_structures`**: fetches PDB entries by ID (up to 100, such as
  `4HHB`, or wwPDB's extended form, `pdb_00004hhb`, or an entry that has
  only that, `pdb_1001ba3c`) from RCSB in mmCIF, and registers each as a structure, with the
  proteins its chains are. Each records the entry, its revision, its link,
  when it was fetched and the file's SHA-256. It reaches only
  `files.rcsb.org`.
- **`fetch_structures_from_alphafold_db`**: fetches AlphaFold DB's predicted
  structures of UniProt entries by accession (up to 100, such as `P69905`;
  AlphaFold DB predicts canonical sequences, so not an isoform's), in mmCIF,
  and registers each as a structure (method `predicted`), with the protein it
  predicts. It asks AlphaFold DB's API
  (`/api/prediction/<accession>`) for each entry's models and takes every one
  it lists at its latest version. Each records the model (`AF-P69905-F1`), its
  version, its entry's link, when it was fetched and the file's SHA-256, and
  says what made it as the file does: AlphaFold and its version (or, for a
  model another provider gave AlphaFold DB, its tool, such as ColabFold), and
  the model's mean pLDDT. It reaches only `alphafold.ebi.ac.uk`.
- **`import_structure`**: registers an mmCIF or PDB file (up to 1 GiB) as a
  structure, with the proteins its chains are. The structure keeps the file
  itself.
- **`assign_secondary_structure`**: runs DSSP (mkdssp 4.4.10) on structures'
  first models, up to 20 at once, and sets their secondary structure.
- **`align_pair`**: aligns two proteins, globally as EMBOSS needle does or
  locally as water does, with needle's default scoring unless the run says
  otherwise: BLOSUM62, a gap opening penalty of 10 and an extension penalty
  of 0.5, and, globally, gaps at the ends free. It records the score, the
  alignment's length, and its identity, similarity and gaps as percentages of
  it, unrounded, so a workflow's threshold compares the true share (899 of 999
  is under 90%). Similar columns are those the matrix scores above zero, as
  EMBOSS counts them, so X with X is identical but not similar. U, O and J,
  which the matrices lack, are aligned (and scored) as X, but counted by their
  own letters: U with U, O with O or J with J is identical and also similar,
  as a residue is to itself, so WWUWW with itself is 100% similar, where
  WWXWW, with the same score, is 80%.
  A local alignment with nothing scoring above zero is none: the run says so
  and creates no alignment. It keeps the alignment as two artifacts
  ([ADR 0068](../../docs/adr/0068-runs-keep-artifacts-for-a-while.md)):
  `alignment.txt` in EMBOSS's pair format, and `alignment.fasta`, where a row
  that's part of its protein, as a local alignment's are, is headed
  `HRID/start-end`, as Jalview writes it, and numbered so. The FASTA
  is also an `iki/core.Alignment` of the two, the target its reference, which
  Iki's alignment viewer shows
  ([ADR 0088](../../docs/adr/0088-alignments-are-entities-of-sequence-entities.md)).
  Up to 100 million cells (10,000 residues by 10,000).
- **`align_multiple`**: aligns up to 2,000 proteins with MAFFT 7.505 (from
  Debian, without its GPL-licensed extensions) into an `iki/core.Alignment` of them,
  each row headed by its protein's HRID, in the order given. A reference, if
  chosen, is the first row. MAFFT's strategy is the run's: Auto (the default)
  lets MAFFT choose by how many proteins there are and how long, and the
  alignment's method records the one it used; L-INS-i, G-INS-i and E-INS-i
  take up to 200 proteins, FFT-NS-2 any number. A protein with a letter MAFFT
  doesn't score (U, O or J) is aligned with `--anysymbol`, as the method says.
  The run fails if MAFFT changes any row's residues rather than only adding
  gaps. It keeps `alignment.fasta` as an artifact, which is also the
  alignment's file. Proteins up to 40,000 residues, and 5 million in all.
  MAFFT's alignment can depend on how many threads it runs on, so the method
  says how many it was given.
- **`consensus`**: registers an alignment's consensus as a protein, derived
  from the alignment, as the dna plugin's `consensus` does for DNA (the same
  script, which reads that README's rule). Where no residue reaches the
  threshold, D and N together are B, E and Q are Z, I and L are J, and any
  other set is X; without ambiguity codes, X. A read's X that `align_reads`
  narrows in its header (`possible=42:KN`, an AAN codon) counts as what it
  could be: half a K and half an N, not a twentieth of every residue. A protein
  already in Iki is found ([ADR 0173](../../docs/adr/0173-dna-is-aligned-as-proteins-are-and-a-consensus-is-a-sequence.md)).
- **`align_reads`**: aligns sequencing reads to a protein, such as Sanger
  reads of an expression construct to the protein it should make, into a
  `iki/core.Alignment` the alignment viewer shows, the protein its reference. It
  takes the same files as the dna plugin's `align_reads`: up to 96, AB1
  traces, FASTQ or FASTA files, up to 1,000 reads in all
  ([ADR 0089](../../docs/adr/0089-a-param-can-take-several-files.md)). It
  asks for 1 GiB of memory, and keeps `alignment.fasta` as an artifact, which
  is also the alignment's file: in a read's row, `-` is a deletion and `.` a
  place it has no observation of, beyond its ends. Other tools reading aligned
  FASTA take that `.` for a residue, so it keeps `alignment-standard.fasta` too,
  with `-` for both, and each read's header, in both, says where it lies on the
  protein (`; on PRT-000001 1–200`). A codon that could be some residues but not
  others, such as AAN (K or N), is `X` in its row, and the row's header ends
  `possible=21:KN`, each such column (from 1) with what it could be, so the
  viewer counts a K-or-N where the protein has A as the difference the run
  reports, and an `NNN` as saying nothing.

Aligning reads to a protein: reads are read and trimmed as the dna plugin's
`align_reads` does (the protein plugin has its own copy of that code, and a
test keeps the two the same). Each read is translated in its six frames by
the standard genetic code and aligned locally to the protein as tblastn
does: BLOSUM62, a gap opening penalty of 11 and an extension penalty of 1.
The best frame is kept if the alignment's expected value, by BLASTP's
statistics for those scores (λ 0.267, K 0.041), is under 0.001, so a read
that's partly vector aligns where it holds the protein's code, and one that
doesn't code for it is left out, saying so (a warning for each reason, naming
the reads, and each read, with why, in the run's artifact `left_out.tsv`). What's left of the read either
side is aligned in its strand's other frames: a part that aligns there means
the frame shifted, and the run names where (`p.(K120fs)`). The run lists each
difference in the protein's numbering, as HGVS names a protein change predicted
from DNA, in parentheses, in one-letter codes: substitutions (`p.(T315I)`),
stops (`p.(Q45*)`), deletions (`p.(K12del)`, `p.(K12_L14del)`), insertions
(`p.(K12_L13insG)`), duplications (`p.(K12dup)`), deletion-insertions and
frameshifts, a deletion or insertion in repeated residues named at its
C-terminal-most place, with the reads that show it and their quality (the
least of its codon's bases). Changes a read makes to consecutive residues are
one change, a deletion-insertion, as HGVS names changes on one allele: R21S and
C22W beside each other are `p.(R21_C22delinsSW)`, and are one of the run's
differences. Each codon is compared as the residues it could
code for, by every codon its bases could be: it differs if it can be none of
what the protein's residue stands for (B is D or N, Z is E or Q, J is I or L,
X anything), so RAY (Asp or Asn) at an Ala is `p.(A42B)`. A codon that could be
either, such as NNN, says nothing: it covers nothing, and the run lists
where. UGA is selenocysteine where the protein has U, and UAG pyrrolysine
where it has O; any other stop there (`p.(U42*)`) is a stop.

Properties follow ExPASy's ProtParam, and all but the molecular weight are
Biopython's:

- **Length**: its amino acids; a stop (`*`) is none.
- **Molecular weight**: its residues' formulas, plus one water, weighed by
  CIAAW's standard atomic weights 2024, the table every Iki plugin weighs by
  ([ADR 0272](../../docs/adr/0272-one-table-of-atomic-weights-weighs-every-molecule.md)). It gives haemoglobin α 15,257.59 Da, where ExPASy gives 15,257.55
  and Biopython's ProtParam, by IUPAC's atomic weights of 2005, 15,257.36.
- **Isoelectric point**: the pH at which Bjellqvist's charge model (his pK
  values, with the terminal residues' own) gives no net charge, found by
  bisection to 0.0001. Biopython's own search keeps between pH 4.05 and 12,
  so Iki brackets the zero itself: ten aspartates are 3.08, not 4.05.
- **Extinction coefficients**: at 280 nm in water, from Pace's values for W,
  Y and cystine (1995). One assumes every pair of cysteines forms a cystine;
  the other, that none does.
- **GRAVY**: the mean Kyte–Doolittle hydropathy.
- **Hydropathy**: ProtScale's Kyte–Doolittle profile with its defaults, a
  window of nine residues centred on each, equally weighted. The first and
  last four residues have none.
- **Instability index**: Guruprasad's. Above 40, a protein is probably
  unstable in a test tube.

Each needs residues it has values for, and the stops at its end are left out
(`M**` is M, as it is to its identity). All take the 20 standard amino acids. Molecular weight also takes selenocysteine
(U), pyrrolysine (O) and J (I or L, which weigh the same); the isoelectric
point and extinction coefficients also take J, as neither I nor L is charged
or absorbs at 280 nm. For B, Z, X, an inner stop, or a residue a property has
no value for, the run warns which residue left which properties out. An
isoelectric point past pH 0 to 14, which the model gives only for an extreme
composition, charged residues nearly all of one sign (120 arginines: 14.08;
102: 14.004; (RAKR)×60: 14.08), isn't saved, checked before it's rounded, and the
run says why:
past pH 14 is over 1 M hydroxide, where the model's pK values don't hold. The
other properties are.

Translation uses NCBI's genetic codes (1, the standard code, by default). A
codon with ambiguous bases becomes the amino acid every reading of it gives,
or B, Z, J or X. A frame is read codon by codon, bases past the last whole
codon left out. A complete coding sequence is read as NCBI's tables say of
one, as Biopython's `cds=True` does: its first codon must be one of the
code's start codons, and gives methionine (so a bacterial GTG start is M, not
V), its last a stop, with no other stop and no bases left over. In codes 27,
28 and 31, a stop codon can also code for an amino acid, so only its place
says which: those codes can't stop at the first stop codon, and a frame keeps
such codons as amino acids, saying so of a last one that could have ended it,
an ambiguous one too (TAR in code 28 is TAA or TAG, so Q or a stop; TRA is
TAA or TGA, so Q, W or a stop, and is kept as X). It says to translate a
complete coding sequence to end there only when every codon the last could be
is a stop: TAN in code 28 could also be TAC or TAT, Tyr and never a stop.
A DNA sequence that gives no residues (no whole codon, only stops, or a stop
first), or isn't the complete coding sequence asked for, fails its own run
and the rest go on.

FASTA headers give a name (the first word) and a description (the rest).
UniProt's (`>sp|P69905|HBA_HUMAN Hemoglobin subunit alpha OS=Homo sapiens
OX=9606 GN=HBA1 PE=1 SV=2`) give the entry name, the protein's name, its
accession, organism (by its NCBI Taxonomy ID, `OX=`) and gene, and each record's
origin is its UniProtKB entry, by accession and sequence version (`SV=`), with
the file it was read from. Each organism is
an `iki/core.Organism`, made the first time an import meets it
([ADR 0140](../../docs/adr/0140-an-organism-is-a-taxon-by-its-ncbi-taxonomy-id.md)).
NCBI's (`>NP_000549.1 hemoglobin subunit alpha [Homo sapiens]`) name the
organism in brackets without its ID, which a name can't be told by, so it's left
out and the run says so. An organism keeps the name its source gives, such as
UniProt's `Escherichia coli (strain K12)` for NCBI's `Escherichia coli K-12`,
until a registrar corrects it; NCBI's brackets for a genus it hasn't placed
(`[Clostridium] scindens`) and quotes for an informal name (`'Nostoc
azollae' 0708`) are kept. A name it can't keep (one that runs over lines)
leaves the organism its NCBI Taxonomy ID, and the run says so. A record with a stop inside
it, a gap, or a letter that isn't an amino acid's code fails the whole import,
and the run says which record and why, so nothing is half imported.

An accession UniProt has no entry for (merged into another, or made obsolete)
fails the fetch, which names it. When UniProt says to slow down or fails on
its side, it waits and asks again.

A structure and the proteins its chains are come from the same run, all or
nothing, the structure referring to the proteins made with it
([ADR 0064](../../docs/adr/0064-a-runs-new-entities-can-refer-to-one-another.md)).
Each protein (an mmCIF entity of type `polypeptide(L)`) takes its full
sequence (SEQRES, or the residues it has where a file gives none, as
predictions often do), and its name, organism (by its NCBI Taxonomy ID),
gene and UniProt accession where the file gives them. Chains of the same
protein refer to the same `Protein`, the registry's entry for its sequence if it has one already. A
chimeric chain, whose segments wwPDB maps to different UniProt entries or
organisms (2RH1's β2-adrenergic receptor with T4 lysozyme in its third loop:
P07550 at residues 8–237 and 398–500, P00720 at 238–397), is none of them: its
protein gets no accession, organism or gene, its description lists each with
its residues, and the run says so. A PDB ID is kept as its four characters
(`4HHB`), the same entry's extended ID (`pdb_00004hhb`) included; an entry with
only an extended ID, `pdb_` and eight letters or digits as the PDBx/mmCIF
dictionary gives it, keeps that (`PDB_1001BA3C`). A structure's release is its
PDB entry's first revision; a file that isn't a PDB entry's, such as an
AlphaFold DB model, has none, as its revisions are the model's. A resolution
is read as CIF writes numbers, with any standard uncertainty in brackets left
off (`2.10(5)` is 2.10 Å). A structure determined by several methods, such as
3KCO, refined jointly against neutron (1.8 Å) and X-ray (1.53 Å) data, keeps
each method's resolution from its own refinement (`_refine.pdbx_refine_id`)
in `resolutions`; its method and resolution are the first method's, as the
file lists them, and the run says so. Other polymers
(DNA, RNA, D-peptides) and ligands are left out, and the run says so. A file
with no protein chains, or that isn't mmCIF or PDB, fails the run, which says
why. An entry the PDB doesn't have, or has made obsolete, fails the fetch.

A structure's origin also says how its file says it was made
([ADR 0106](../../docs/adr/0106-how-an-entity-was-made-recorded-by-iki-or-stated-by-its-source.md)),
as its source's word, never Iki's: an experiment, by mmCIF's `_exptl.method`
(EXPDTA in a PDB file), such as `X-RAY DIFFRACTION`, or a computed model, by
`THEORETICAL MODEL` or, in a ModelCIF file without `_exptl`, its model's type.
With it, the software the file names (`_software`; in a ModelCIF file, only its
software groups', which made the model, not those that annotated it), its
version without a leading `v`, and a model's global quality metrics
(`_ma_qa_metric_global`), a pLDDT as `mean_plddt`. A file that doesn't say,
or names a method that isn't one of wwPDB's, says nothing. Its proteins'
origins don't say: their sequences weren't made by the experiment or the
model. A ModelCIF file's proteins take their organism, gene and UniProt
accession from its target (`_ma_target_ref_db_details`), each from one
reference, never mixed: the one marked primary (`is_primary`), or else the
UniProt reference's accession and gene, and the organism its references agree
on. References that disagree, such as ModelArchive's ma-bak-cepc-0001, whose
UniProt entry is of strain YJM789 and its OrthoDB entry of YJM1573, leave the
protein without an organism, and the run says so. An organism given by its
NCBI Taxonomy ID without a name is still that organism. A file without
chain IDs in `_entity_poly` takes them from its residues.

A sequence record doesn't say how its sequence was made, so proteins imported
from FASTA or fetched from UniProt say nothing of it.

DSSP reads each file as the import did, so each chain's letters line up with
its protein's sequence; a PDB file reaches it as the mmCIF gemmi makes of it,
with each residue's place in its sequence (`label_seq_id`), so a chain
numbered from 101, with insertion codes (52A) or with residues missing gets
its letters on its own residues. The image's check compares them with what
mkdssp gives reading the PDB file itself: H, G and I are helices (alpha, 3-10, pi), P a
polyproline II helix, E a strand in a sheet, B an isolated bridge, T a turn, S
a bend, `-` none, and `?` a residue DSSP didn't assign: one without
coordinates, or one DSSP leaves out though it has them, such as green
fluorescent protein's chromophore (CRO 66 in PDB 1EMA). The helix share counts
H, G and I, and the strand share E and B, over the residues DSSP assigned (in
1EMA's chain A, 117 strand residues of 225 assigned, 52.0%, where 226 have
coordinates). The accessible surface is the sum of the residues', as DSSP
reports it, with the chains together. A residue DSSP gives no accessibility
(`?`) has no measured area, so the surface is then left out, and the run says
how many had none; mkdssp 4.4.10 writes every residue's or none. DSSP takes residues' chemistry from
the wwPDB's Chemical Component Dictionary in the image (the PDBj snapshot of
2026-01-01), so a structure with a residue newer than that fails its own run,
which names the residue.

AlphaFold DB's models are
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), as each file's
`_pdbx_data_usage` says: people may share and adapt them, crediting AlphaFold
DB. When you publish what you found with them, cite
AlphaFold (Jumper et al., Nature 596, 583–589, 2021,
doi:10.1038/s41586-021-03819-2) and AlphaFold DB (Bertoni et al., Nucleic
Acids Research, 2026, doi:10.1093/nar/gkaf1226), or, for a model from another
provider, the work its file's `_citation` names. The predictions are
theoretical models, as each file says: check them against experiment.

Structure prediction (ESMFold) and embeddings come with shared volumes, for
their weights.

## Developing it

From the repository root:

```sh
uv run iki plugin validate plugins/protein --with plugins/dna
cd plugins && uv run iki plugin makemigrations protein --with dna   # after changing a manifest
```

Its environment's packages are pinned, with their hashes, in
`requirements.txt`, compiled from `requirements.in` as it says. Pin the same
releases in the repository's dev dependencies, where
`server/tests/test_protein_actions.py` runs the actions (and
`server/tests/test_protein_reads.py`, `align_reads`): `make check-deps`
checks they match, and that each is at least 3 days old. `scripts/reads.py`
reads and trims reads with a copy of the dna plugin's code, since each
plugin ships its own scripts; change both together, as that test checks.

`server/tests/test_first_party_plugins.py` checks that it's valid, that its
migrations match its manifests, and that it migrates forwards and back.

To change an image, change `images/structure-tools` or
`images/alignment-tools` (CI builds it and runs its `check.py`). Once `main`
has published it, pin the new digest that the publish workflow's summary
gives in its environment (`environments/structure-tools.yaml` or
`environments/alignment-tools.yaml`), and in `deploy/dev/sso.py`.
