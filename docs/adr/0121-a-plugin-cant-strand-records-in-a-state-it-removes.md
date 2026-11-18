# 0121. A plugin can't strand records in a state it removes

- Status: Accepted
- Date: 2026-11-17
- Refines: [0033](0033-plugin-codegen-and-migrations.md),
  [0118](0118-a-record-moves-through-its-types-states.md),
  [0120](0120-a-state-can-lock-a-record-and-the-database-holds-the-lock.md)

## Context

A lab's process changes, and with it a plugin's states: a step is added,
merged or dropped. Records may be in a state the new version drops. If a
plugin could simply delete it, those records would be in a state nothing
knows, with no transitions out, and the database's check of the state
column would refuse the migration halfway. Iki keeps every record's
history (ADR 0035), so the move has to be recorded too.

## Decision

**A type's states are part of its migrations.** The state column records
the states, which lock, and where removed ones went, so
`iki plugin makemigrations` sees each change and writes it as a
migration, with a `SyncStates` operation for the type.

**A state with records can't just disappear.** If a state is gone from
the manifest, makemigrations refuses and says how to keep it: marked
`removed: true` with `moved_to:` a state that remains.

```yaml
states:
  on_hold: { label: On hold, removed: true, moved_to: received }
```

A removed state takes no transitions and no new records. The migration
moves the records in it to `moved_to`, each with a revision of kind
`transitioned`, by no one, via `system`, naming `iki.moved` and saying
why. Rolling the migration back moves back each record whose latest
revision is that move.

A type can't lose its states altogether, as it can't lose a field's
history. States added to a type that has records start each of them in
the initial state.

**Which states lock follows the migration.** A state that locks now locks
the records already in it, and one that stops locking unlocks them; the
reverse migration puts them back as they were.

## Consequences

- An upgrade that changes a lab's process says where each record goes,
  and each record's history says it was moved, and when.
- A plugin keeps a removed state's name in its manifest for good, as it
  keeps a removed field's.
- A changed lock applies to existing records at once, which an Org Admin
  sees in the plugin's migration before deploying it.
