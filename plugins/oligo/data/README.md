# The monomer library

`monomers.json` holds the 28 monomers the oligo plugin seeds its registry from
(`register_monomer_library`), and resolves HELM's symbols by when a run isn't
given a monomer. `build_monomers.py` builds it, with RDKit, from the Pistoia
Alliance's HELM monomer sets,
[HELMMonomerSets](https://github.com/PistoiaHELM/HELMMonomerSets) at commit
`fecc972f4707bc4e0cc34b7de3713cdba559e177` (9 December 2021), and the
corrections below. Each entry says where it's from in `source`, and a
modifier where its ε260 is from in `extinction_source`.

Every structure is written as HELM's libraries write them: SMILES with each
attachment point's cap atom mapped, `[H:1]` for a hydrogen and `[OH:3]` for a
hydroxyl. A sugar's R1 is its 5′-O, R2 its 3′-O and R3 its anomeric carbon's
hydroxyl, where its base joins; a base's R1 is the N-H that joins its sugar;
a linker's R1 and R2 are its two hydroxyls; a modifier joins through its R1.

## Where each is from

- **monomerLib2.0**, as it gives them: ribose (`R`), 2′-deoxyribose (`dR`),
  2′-O-methylribose (`mR`), 2′-fluoro-2′-deoxyribose (`fR`),
  2′-O-methoxyethylribose (`MOE`), LNA (`LR`), ENA (`eR`), phosphate (`P`),
  phosphorothioate with its stereochemistry undefined (`sP`), the five natural
  bases, and 5-methylcytosine (`5meC`).
- **HELMCoreLibrary**, rebuilt from its molfiles, whose stereochemistry its
  SMILES lose:
  - `Rsp` and `Ssp`, phosphorothioate made Rp and Sp: its SMILES give both the
    same `[P@]`. From the molfiles, RDKit's CIP labeller labels the phosphorus
    R and S once it's joined between two nucleosides (Iki's tests check it).
  - `cet`, (S)-constrained ethyl: its SMILES makes the 6′ carbon R; its
    molfile, and its name, S.
- **The Ionis library**, with these corrections:
  - `BHQ2`: its nitro group is written `[N+](=O)O`, charged and protonated;
    corrected to `[N+](=O)[O-]`. It then adds 556.5 g/mol through a 3′
    phosphate, as IDT lists 3′ BHQ-2.
  - `5Cy5`: its R1 cap is `[OH:1]` on an oxygen already there (`CCCO[OH:1]`, a
    peroxide); corrected to `CCCO[H:1]`. As an inner salt it adds 532.6
    g/mol, as IDT lists 5′ Cy5.
  - `5BioTEG`: its biotin has no stereochemistry; given D-biotin's (3aS, 4S,
    6aR), checked against D-biotin's standard InChIKey,
    YBJHBAHKTGYVGT-ZKWXMUAHSA-N. It adds 481.5 g/mol, as IDT lists 5′
    biotin-TEG.
  - `cholTEG`: its cholesterol has no stereochemistry; given cholesterol's,
    checked against its standard InChIKey, HVYWMOMLDIMFJA-DPAQBDIFSA-N.
  - `3GN3C12hp`, triantennary GalNAc as in L96: as the library gives it, its
    constitution without stereochemistry, which formula and weight don't
    depend on. Inclisiran's sense strand with it is the formula EMA's
    assessment report gives (`tests/`).
  - `5FAM` (6-FAM on a C6 amino linker), `BHQ1`, `nC6o` (amino C6) and `SpC3`
    as it gives them: 6-FAM adds 537.5 g/mol through a 5′ phosphate and BHQ-1
    554.5 through a 3′ one, amino C6 179.2, as IDT lists them.
- **Iki's own**: `5Cy3`, which the Ionis library has only on another linker:
  its `5Cy5` with one CH=CH fewer in the polymethine chain, as IDT's Cy3 is
  its Cy5's. It adds 506.6 g/mol, as IDT lists 5′ Cy3.

A modifier's ε260 is its supplier's (IDT's: 6-FAM 20,960, BHQ-1 and BHQ-2
8,000, Cy3 4,900, Cy5 10,000 L/(mol·cm)); biotin, amino linkers, spacers,
cholesterol and GalNAc have no chromophore absorbing at 260 nm, so 0.

Left out, since their structures couldn't be checked against what their
suppliers sell: IDT's standard 5′ biotin (on a C6 linker) and 3′ biotin, and
3′ 6-FAM. A lab registers its own, with the structure its supplier gives.

## Licence

The HELM monomer sets are under the MIT licence:

```
MIT License

Copyright (c) 2021 Pistoia Alliance HELM Project

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
