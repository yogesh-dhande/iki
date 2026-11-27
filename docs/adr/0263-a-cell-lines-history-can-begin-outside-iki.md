# 0263. A cell line's history can begin outside Iki

- Status: Accepted
- Date: 2026-11-26
- Refines: [0186](0186-cell-banks-and-the-checks-that-applied-to-a-culture.md)
- Builds on: [0261](0261-a-bank-check-follows-the-procedure-its-project-grants.md)

## Context

`iki/cell_lines` described one fully observed history: a Cellosaurus line, a
master bank from supplier material, working banks from the master, and every
thaw and passage since, each with a known passage number. Many labs' records
aren't like that. Cellosaurus doesn't list most primary lines a lab
establishes. A research lab keeps one tier of frozen stock rather than the
two-tier master and working system of ICH Q5D. Material arrives from a
collaborator without a catalogue number or a passage history, and a culture
already growing when the lab began recording has no thaw to start from.
Engineered derivatives are made by transduction, immortalization, adaptation
or selection as well as by editing and transfection.

A lab that can't record these must invent a parent, a passage or a supplier,
which is worse than leaving them out.

## Decision

**A parental line is registered in Cellosaurus or locally.** Its identity is
where it's registered with its Cellosaurus accession or the lab's own
accession, exactly one of them. The Cellosaurus import registers the line
alone when no supplier is given, and keeps its strict supplier and catalogue
check when one is.

**A bank's structure is the lab's choice.** `CellBank.kind` adds `stock`, a
single-tier bank that can name its catalogue source or another bank of the same
line it was made from. Master and working banks keep the two-tier rules: a
master has no source bank, a working bank's source is a master bank of its
line, and a bank follows its source in date and known passage.

**An unknown passage is explicit.** A bank and a culture each have
`passage_unknown`; a record has a passage number or is marked unknown, never
neither. A thaw from a bank of unknown passage, and a passage from a culture of
unknown passage, stay unknown.

**A culture can start outside Iki.** `CellCulture.event` adds `start`, which
has no vial and no parent culture. It names its bank when it came from one in
the project, on or after the banking date, or its line otherwise. Passages
follow a started culture as they follow a thaw. A culture from no bank cites no
bank check, because a check belongs to a bank.

**Engineered changes** add transduction, immortalization, adaptation,
selection and other, each with its required description.

## Consequences

A fully observed history is checked exactly as before. A partial one is
recorded as partial, and a reader sees where it begins and which passages
weren't recorded.

A banked vial still names its bank twice, as `bank` and as its tube
`contents`, and a rule keeps them equal. Deriving one from the other needs a
way for a subtype to narrow an inherited reference, which the platform doesn't
have yet.
