# 0088. Alignments are entities of sequence entities

- Status: Accepted
- Date: 2026-11-15
- Refines: [0085](0085-types-configure-ikis-own-viewers.md)

## Context

Scientists align sequences all the time: a family of proteins they've
registered, to see what's conserved; Sanger reads against the plasmid a
construct should be; a variant against its reference protein. An
alignment is a result, like a structure: someone made it, by some method,
from sequences that are mostly in Iki already, and others will want to see
it, pick sequences from it and act on them. Some of its rows aren't
sequences in Iki at all, such as the reads.

Viewers that draw a file belong to Iki (ADR 0085). Alignments run to
thousands of rows and columns, so drawing one as DOM nodes isn't an option,
and reading the file needs a parser that's quick on tens of megabytes.

## Decision

**`core.Alignment` is a built-in type,** with an optional `reference` (the
sequence the others were aligned to), `members` (the sequences in Iki it
aligns, up to 10,000), `aligned` (the alignment, as aligned FASTA) and the
`method` used. A row is a member when its FASTA header's first word is that
member's HRID. Rows that aren't, such as reads, are shown under their own
names and aren't linked: a header that merely looks like an HRID isn't
taken for one. Members and the file are immutable, as a run's result is.

**The alignment viewer** names the file field, the list of members and the
reference. It pins the reference at the top and can show the others only
where they differ from it. It draws only what's in view on a canvas, with a
ruler, the consensus, Clustal's conservation marks and a bar of how much
each column agrees. With a reference, its summary says how much of the
reference the other rows cover and at how many columns they differ from it,
since reads covering part of it leave no column without a gap; without one,
it counts identical columns as Clustal does. Residues are coloured in Clustal X's hues by kind,
where their column agrees with its consensus, or everywhere, or not at all.
Row labels link to the members, which can be picked to run an action on.
A cursor moves with the arrow keys and its place is read out, so the
alignment can be explored without a mouse or sight; a saved view (ADR 0086) keeps where it was.

**The page reads the file with iki-wasm,** Iki's own Rust parsers compiled
to WebAssembly, in a Web Worker, after checking the bytes against the
SHA-256 Iki stored. The planning that chose WebAssembly expected a native
desktop app later (Tauri), using the same Rust crates; parsing in Rust now
means that app needs no second parser. The worker's script carries its own
policy: scripts and connections from Iki only, WebAssembly but no `eval`.
The page creates the worker through a Trusted Types policy that only lets
Iki's own worker scripts run. Files over 64 MB, or over a million columns,
are left to desktop viewers such as Jalview.

## Consequences

- Actions that align should create a `core.Alignment`, its members their
  inputs. The protein plugin's `align_pair` and a new `align_multiple`
  (MAFFT) are next (step 41f).
- Lineage shows which sequences an alignment came from, and an alignment's
  rows lead back to them.
- Editing an alignment by hand, moving gaps where an algorithm misplaced
  them, comes after M1 (see the roadmap). The file is immutable for now; an
  edited alignment will be a new revision with its own history.
- The build needs the Rust toolchain with the `wasm32-unknown-unknown`
  target for the web app, as it already does for the worker.
