# 0148. A signature reads its record through what migrations did since

- Status: Accepted
- Date: 2026-11-18
- Refines: [0126](0126-an-electronic-signature-is-bound-to-the-revision-it-signs.md),
  [0033](0033-plugin-codegen-and-migrations.md),
  [0115](0115-registries-keep-identities-projects-keep-records.md)

## Context

Verifying an electronic signature (ADR 0126) rebuilds the record's content
at the signed revision from its history tables, and compares it with the
content kept with the signature. Three changes that Iki supports, and that
aren't tampering, changed what that rebuild gives:

- A plugin's migration renames a field (ADR 0033). The history column is
  renamed with it, so the rebuilt content names `title` where `name` was
  signed.
- A plugin's migration changes a quantity's unit. `ConvertUnit` rescales
  the history too, so 1.5 g signed is now 1500 mg. That's the same
  quantity, but a different number. The signed content didn't say which
  unit its 1.5 was in.
- A project moves its record from a superseded registry entry to the one
  that replaced it (ADR 0115). The rebuild read the entry the record names
  now, not the one it named when it was signed.

Each time, verification said the record's history "was changed outside
Iki". For QA, that reads as tampering with Part 11 evidence.

A signature attests to a record as it was. Veeva Vault, Benchling and
LabWare bind a signature to one version of a record, and keep that
version readable, as it was, however the system changes later. The checks
must still catch any change that a supported operation didn't make.

## Decision

**What was signed stays as it was signed.** Signed content is never
rewritten. Its format is now `iki/signed-content/v2`, which adds `units`:
the UCUM unit of each quantity among its values, as the values were stored.
So 1.5 is signed as 1.5 g. Format v1 was never released, so v2 replaces it.

**Migrations record what they do to kept history.** Each change goes in
`iki_history_change`, in the migration's own transaction, forwards or
back:

- `RecordRename`, which `iki plugin makemigrations` adds beside the
  `RenameField` of each history table, records a renamed field.
- `ConvertUnit` on a history table records the conversion: the units,
  `scale` and `offset`.
- `RenameEntityType` records a renamed type.

Each row names the part of a type its table holds, as the type's shape
does (`acme/lab.Sample`, `acme/lab.Sample#entry`,
`acme/lims:acme/lab.Sample`), and the plugin. Each row is signed with the
deployment's signing key (ADR 0126): Ed25519 over canonical JSON of the
row, its ID included. The migrate Job holds `IKI_ENCRYPTION_KEYS`, so it
can sign, and the database alone can't. The table is append-only for every
role, its owner included. The app role can only read it.

**A signature notes how far those changes had got.** Each signature keeps
the last change recorded when it was signed (`history_seen`). If a
migration commits while someone is signing, the signing is refused and
they sign again, so the content is never read from part-old, part-new
history.

**Verifying reads the record through what was done since.** Verification
finds the changes made after the signature that touch its record's tables,
using the tables' names at the time of each change. It then applies them to
the signed content, in order:

- a renamed field is named anew;
- a quantity is converted exactly as the migration converted it,
  `old * scale + offset`, so it's the same double;
- a renamed type is named anew.

It compares the result with the history as it's kept now. Each change it
applies must be signed by one of this deployment's own keys, and a
conversion must start from the unit the content had then. Otherwise, or if
anything differs, the record check fails. When something was applied, a passing
check says what: "revision 1 still holds what was signed, as plugins'
migrations have kept it since: `name` renamed `title`, `mass` converted
from g to mg."

**A record of a registry entry is read with the entry it named then.**
The content reads the entry the record named at the signed revision, using
the same `entry_at` that history uses. That's the superseded one if the
record has moved since. The entry's own history isn't touched by a move,
so a change to it is still caught.

**Bundles carry the changes.** Each line of `signatures.ndjson` has
`since`: the changes made since the signature that touch its record, as the
exporting deployment recorded them. `iki bundle verify` applies them before
checking the bundle's history. An import keeps them with the signature
(`migrated`), and applies any changes the importing deployment makes later
on top.

## Consequences

- A plugin can rename a signed field, change its unit, or rename its type.
  A project can move a signed record to its replacement entry. Each
  signature still verifies, and says what was done since it was signed.
- A value changed in SQL is still caught, before such a change or after
  it. A change row added outside Iki isn't signed, so it explains nothing,
  and the check names it. A row deleted or reordered leaves the names or
  units wrong. A conversion has to start from the unit that was signed, and
  the result has to be in the unit the field has now.
- A row recorded before the deployment had a signing key is unsigned. No
  signature here was made before it, so none is read through it.
- An imported signature keeps the changes its bundle brought (`migrated`)
  in a column, as it keeps the registry entries it renames (`renamed`).
  The bundle's signature vouched for both at import, but nothing signs them
  in the database after.
- The signed content now says what units its quantities were in. A reader
  without Iki can tell what was signed.
- Signing refuses, and asks to sign again, if a deploy migrates kept
  history at the same moment. That's rare and safe.
- A conversion that `ConvertUnit` records needs `part`, which
  `makemigrations` gives it. A hand-written `ConvertUnit` on a history
  table must name it too, or signatures over that field fail to verify.
- `iki_history_change` grows by one row per field a migration renames or
  converts. It stays small.

## Alternatives considered

- **Refuse migrations that touch signed fields.** Plugins could never
  correct a unit or a name once anything was signed. The refusal would come
  at deploy time, from data the plugin's author can't see. It blocks
  supported changes, rather than keeping signatures checkable across them.
- **Compare quantities in their units, within a tolerance.** Converting
  back and forth isn't exact for doubles. That would mean accepting values
  that differ slightly, and it still wouldn't explain renames.
- **Keep history in the units it was written in.** That would undo ADR
  0033's choice of one unit per column, and every reader of history would
  have to convert.
- **Store the signed values separately and never check history again.**
  The content kept with each signature is already that. Checking it against
  history is what catches a value changed in SQL (ADR 0126).
