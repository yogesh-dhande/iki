# protein

Iki's protein plugin: proteins, translated from DNA, imported or fetched from
UniProt, with the properties of their sequences, and their structures, from
the PDB or a lab's own files, with their secondary structure.

- **`protein.Protein`** (`PRT-000001`): a protein, by its amino acid sequence.
  - Its identity is its sequence, so registering the same protein again
    reuses it.
  - It keeps its UniProt accession and gene where it has them, and the DNA
    sequence it was translated from (`encoded_by`).
  - Its length, molecular weight, isoelectric point, extinction coefficients,
    GRAVY and instability index are computed by the plugin's actions.

- **`protein.ProteinStructure`** (`PST-000001`): a three-dimensional
  structure, experimental or predicted, in mmCIF or PDB format.
  - Its identity is its file, so importing the same file again reuses it.
  - It keeps its PDB ID, title, method, resolution and first release, and the
    protein each of its chains is (`chains`, in the order of `chain_ids`).
  - DSSP gives each chain's secondary structure, residue by residue, the
    shares of its residues in helices and strands, and its solvent-accessible
    surface, and keeps its annotated mmCIF (`dssp`).

`Protein` extends `core.Sequence`, so a search for sequences finds proteins
alongside DNA. The plugin depends on the [dna plugin](../dna) for
`encoded_by`.

## Actions

Most run on backend workers, in the `protein-tools` environment (Python 3.12
with Biopython and gemmi). The structure actions ask for 2 GiB of memory.
DSSP runs on the container tier, in the `structure-tools` environment: the
plugin's own image, which Iki's publish workflow builds from
`images/structure-tools`, scans and signs
([ADR 0065](../../docs/adr/0065-first-party-plugins-images-are-built-and-signed-like-ikis.md)).

- **`compute_properties`**: a protein's properties, as ExPASy's ProtParam
  computes them, for many at once.
- **`translate`**: translates a DNA sequence in the strand, frame and NCBI
  genetic code chosen, stopping at the first stop codon or keeping each as
  `*`, and registers the protein, linked to the DNA. Many at once.
- **`import_proteins`**: registers each record of a FASTA file (up to 10
  million, and 1 GiB) as a protein. A protein already in Iki is reused. Each
  records the file and record it came from, with the record's SHA-256.
- **`fetch_proteins`**: fetches UniProtKB entries by accession (up to 1,000,
  such as `P69905`, or `P69905-2` for an isoform) and registers each as a
  protein. Each records the entry, the UniProt release and sequence version,
  its link, when it was fetched and its SHA-256. It reaches only
  `rest.uniprot.org`, through the deployment's egress proxy.
- **`fetch_structures`**: fetches PDB entries by ID (up to 100, such as
  `4HHB`) from RCSB in mmCIF, and registers each as a structure, with the
  proteins its chains are. Each records the entry, its revision, its link,
  when it was fetched and the file's SHA-256. It reaches only
  `files.rcsb.org`.
- **`import_structure`**: registers an mmCIF or PDB file (up to 1 GiB) as a
  structure, with the proteins its chains are. The structure keeps the file
  itself.
- **`assign_secondary_structure`**: runs DSSP (mkdssp 4.4) on structures'
  first models, up to 20 at once, and sets their secondary structure.

Properties are Biopython's ProtParam, which follows ExPASy's:

- **Molecular weight**: average isotopic masses of the residues, plus one
  water.
- **Isoelectric point**: Bjellqvist's pK values, with the terminal residues'
  own.
- **Extinction coefficients**: at 280 nm in water, from Pace's values for W,
  Y and cystine (1995). One assumes every pair of cysteines forms a cystine;
  the other, that none does.
- **GRAVY**: the mean Kyte–Doolittle hydropathy.
- **Instability index**: Guruprasad's. Above 40, a protein is probably
  unstable in a test tube.

They need a sequence of the 20 standard amino acids alone, and a final stop is
left out. For a sequence with U, O, B, Z, J, X or an inner stop, the run warns
which residue stopped it and sets the length alone.

Translation uses NCBI's genetic codes (1, the standard code, by default). A
codon with ambiguous bases becomes the amino acid every reading of it gives,
or B, Z, J or X. Bases past the last whole codon are left out. A DNA
sequence that gives no residues (one that starts with a stop codon, say)
fails its own run and the rest go on.

FASTA headers give a name (the first word) and a description (the rest).
UniProt's (`>sp|P69905|HBA_HUMAN Hemoglobin subunit alpha OS=Homo sapiens
OX=9606 GN=HBA1 PE=1 SV=2`) give the entry name, the protein's name, its
accession, organism and gene. NCBI's (`>NP_000549.1 hemoglobin subunit alpha
[Homo sapiens]`) give the organism in brackets. A record with a stop inside
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
predictions often do), and its name, organism, gene and UniProt accession
where the file gives them. Chains of the same protein refer to the same
`Protein`, which is reused if Iki already has its sequence. Other polymers
(DNA, RNA, D-peptides) and ligands are left out, and the run says so. A file
with no protein chains, or that isn't mmCIF or PDB, fails the run, which says
why. An entry the PDB doesn't have, or has made obsolete, fails the fetch.

DSSP reads each file as the import did, so each chain's letters line up with
its protein's sequence: H, G and I are helices (alpha, 3-10, pi), P a
polyproline II helix, E a strand in a sheet, B an isolated bridge, T a turn, S
a bend, `-` none, and `?` a residue without coordinates. The helix share
counts H, G and I, and the strand share E and B, over residues with
coordinates. The accessible surface is the sum of the residues', as DSSP
reports it, with the chains together. DSSP takes residues' chemistry from
the wwPDB's Chemical Component Dictionary in the image (the PDBj snapshot of
2026-01-01), so a structure with a residue newer than that fails its own run,
which names the residue.

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
`server/tests/test_protein_actions.py` runs the actions: `make check-deps`
checks they match, and that each is at least 3 days old.

`server/tests/test_first_party_plugins.py` checks that it's valid, that its
migrations match its manifests, and that it migrates forwards and back.

To change the structure-tools image, change `images/structure-tools` (CI
builds it and runs its `check.py`). Once `main` has published it, pin the new
digest that the publish workflow's summary gives in
`environments/structure-tools.yaml`, and in `deploy/dev/sso.py`.
