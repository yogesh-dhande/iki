# Antibody structure prediction

Iki's antibody structure prediction plugin: an optional provider for the
[antibody plugin](../antibody). It predicts an antibody's variable-domain
structure with [ImmuneBuilder](https://github.com/oxpig/ImmuneBuilder)
(BSD-3-Clause), and records which model made it and how far that model's
ensemble disagrees. Install it beside `iki/antibody` when the lab predicts
structures; a lab that only registers, numbers and screens antibodies leaves
it out, and pulls no model image or weights
([ADR 0257](../../docs/adr/0257-an-antibodys-methods-are-separate-choices.md)).

| Action                                 | What it does                                                             | Tier      |
| -------------------------------------- | ------------------------------------------------------------------------ | --------- |
| `predict_structure_with_immunebuilder` | Predicts an antibody's structure with ImmuneBuilder, and how sure it is. | container |

It lists the software it runs, so what it makes is recorded as made with it
([ADR 0106](../../docs/adr/0106-how-an-entity-was-made-recorded-by-iki-or-stated-by-its-source.md)):
ImmuneBuilder 1.2, OpenMM 8.6.1, PDBFixer 1.12.0, ANARCI 2026.2.13.2 and HMMER 3.4.

## What a prediction records

`predict_structure_with_immunebuilder` runs in the plugin's `structure-tools`
image (`images/structure-tools`), with ImmuneBuilder's trained weights
([Zenodo 7258553](https://zenodo.org/records/7258553), CC BY 4.0: cite its paper
when you publish its structures). Its input includes the antibody's chains
(ADR 0105), so it reads their sequences as they were when it was launched.
ABodyBuilder2 models a heavy and a light chain's variable domains, from an IgG's
or a Fab's two chains or an scFv's one; NanoBodyBuilder2 a VHH's single domain.
Each models one binding head, the first domain of each type it finds, so a chain
numbering found more in (a tandem scFv's, a DVD-Ig's) is refused, saying which
it has, rather than modelled in part as if whole: each binding head, registered
as an antibody of its own, is modelled.

Each model runs four networks and keeps the prediction closest to the others,
refined with OpenMM. The action makes an `iki/protein.ProteinStructure` of the
antibody's chains (chain IDs H and L), derived from the antibody, with the
fields this plugin adds to protein structures:

- `immunebuilder_model`: `abodybuilder2` or `nanobodybuilder2`;
- `immunebuilder_version`: the ImmuneBuilder release, `1.2`;
- `predicted_error`: how far the four networks disagree, the root mean square
  deviation of each residue's alpha carbon, averaged over its residues, in Å;
- `cdr_h3_predicted_error`: the same over CDR-H3 (IMGT 105 to 117).

Each residue's own error is its B-factor in the file. The error is that model's
ensemble's, not an experimental error, so compare it only with structures that
name the same model and release; another predictor's confidence is on its own
scale and belongs in that provider's fields. Refining isn't exactly repeatable,
so the action isn't deterministic: running it again makes a new structure, with
the same estimated errors. An antibody takes about a minute on one CPU, and
about 15 seconds on four.

## The image

The image keeps the name it had in the antibody plugin,
`iki-antibody-structure-tools`, so its published digests stay valid. Its
[README](images/structure-tools/README.md) says how the weights are published,
verified against their [SHA256SUMS](images/structure-tools/SHA256SUMS) and
attributed ([notice](images/structure-tools/WEIGHTS-NOTICE.txt)). The image also
carries OpenMM's licences, which its wheel doesn't: OpenMM is MIT, and the
OpenCL platform its wheel carries, which these runs don't use, LGPL-3.0.

## Tests

`server/tests/test_antibody_structure.py` runs the action's script with a
stand-in ImmuneBuilder, checks that the release a structure records is the one
the action and the image pin, and that the antibody plugin installs without this
one. The image's `check.py` models trastuzumab's Fv and the GFP enhancer
nanobody with the real one, and the dev cluster's end-to-end check predicts
trastuzumab's structure in a pod of its own.
