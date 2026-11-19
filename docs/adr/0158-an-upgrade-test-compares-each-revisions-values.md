# 0158. An upgrade test compares each revision's values

- Status: Accepted
- Date: 2026-11-19
- Refines: [0094](0094-a-plugins-upgrade-is-tested-from-an-earlier-commit.md),
  [0148](0148-a-signature-reads-its-record-through-what-migrations-did-since.md)

## Context

ADR 0094's upgrade test checks that each sample is still at the same
revision, with every revision of its history readable. Readable isn't the
same as intact. A history row that's gone reads as the revision before it,
and a history column that was emptied reads as nothing, without an error.
A migration that does either, while the records' own values stay as they
were, passed the test. Every sample also had a single revision, so the test
couldn't tell one revision's values from another's.

Kept history is the audit trail, and electronic signatures are checked
against it (ADR 0126). An upgrade that empties it is the one an upgrade test
is there to catch.

## Decision

**Each sample is made in two revisions where it can be.** The first holds
only what the sample needs: its required fields and its identity. The second
adds the rest. If the sample can't be saved with only those, say a rule
needs more, it's made in one revision, as before.

**What each revision holds is kept, then compared.** Before the upgrade,
the test reads every revision of every sample's history. After the upgrade,
and after the rollback, it reads them again, and each must hold what it held
before. The only differences allowed are what plugins' migrations recorded
doing to kept history since (ADR 0148): a field renamed is read under its
new name, and a quantity converted is converted as the migration converted
it. A field added since isn't compared, and neither is one marked removed.

Each difference fails the step, naming the sample, the revision, the field,
and what it held and holds: "SMP-000001's revision 2 (weighed's sample 1)
held count 3, and holds null."

## Consequences

- An upgrade that loses or blanks history fails before a deployment runs
  it, even when every revision still reads.
- A hand-written migration that changes kept history must record it as a
  `HistoryChange`, as `ConvertUnit` and the generated renames do, or the
  test fails. Signatures need the same record to verify (ADR 0148).
- Samples take two saves instead of one.
