# 0105. An input can include what its references name

- Status: Accepted
- Date: 2026-11-16
- Refines: [0042](0042-action-runs-and-jobs.md), [0066](0066-a-run-reads-only-whats-in-its-project.md)

## Context

A run's input carries each input entity's values, with references as IDs. A
command can't follow them: it has no token to read the API with, and it
shouldn't, since what it reads would then be whatever the entities are when
it asks, not what they were when the run was launched.

Some actions need what a reference names. Predicting an antibody's structure
needs its chains' sequences, and an antibody is its heavy chain and its light
chain, two references to proteins (ADR 0104). The workarounds are worse: an
input per chain makes the person pick the chains the antibody already names,
and copying the sequences onto the antibody keeps two of each.

## Decision

**An action's input can `include` references of its type**, its own or a
parent's, each a reference or a list of them:

```yaml
inputs:
  antibody: { type: Antibody, include: [heavy_chain, light_chain] }
```

When the run is launched, Iki pins what each input entity's included
references name, as it pins the inputs: the entity, at the revision it has
then, in a table of its own (`iki_run_inclusion`, append-only). The run's
input carries them under each entity's `included`, by reference, in the same
form as the inputs, with any files their values hold:

```json
{
  "id": "…",
  "hrid": "ABY-000001",
  "values": { "heavy_chain": "…", "light_chain": "…" },
  "included": {
    "heavy_chain": [{ "hrid": "PRT-000105", "revision": 1, "values": { "sequence": "EVQ…" } }],
    "light_chain": [{ "hrid": "PRT-000106", "revision": 1, "values": { "sequence": "DIQ…" } }]
  }
}
```

A reference that's empty is an empty list, and a list of references is its
entities in order.
A registered record in the input carries its `entry_id` and `entry_revision`
alongside its project record `id` and `revision`. The entry pair identifies
the registry facts the run read even if the project subsequently moves its
record to a replacement entry (ADR 0115).

They're read as the inputs are:

- **Who can read them.** Whoever launches the run must be able to read them,
  or the launch is refused, naming the input entity whose reference it is.
- **Where.** They must be in the run's project (ADR 0066). Ones that aren't
  are listed with the inputs that aren't, and the launch can add them to it
  as it does those. A workflow checks what its steps include of its own
  inputs when it starts; what a step takes from an earlier one is checked
  when the step is launched.
- **Reuse.** They count in a run's reuse key ([0057](0057-reuse-reproduce-exactly-and-seeds.md)), at their revisions,
  so a run on an antibody whose chain has changed since runs again. Keys of
  runs that include nothing are as they were.
- **Reproducing.** A reproduction reads what the run read, at those
  revisions, and is refused if the person can't read them now.
- **Bundles.** A run's inclusions go with it (`included` on its line), and
  ones that don't come are kept on its record, as its inputs are.

Each attempt, including a retry, receives the pinned direct and included
records while they remain readable in the run's project. The same check
covers every reference identifier in their values that the project could
read at launch. The input keeps its `unreadable` positions across retries
and reproductions, including after import. These positions count in the
reuse key. A record that has moved to a
new registry entry still supplies the old entry identity and facts at its
pin. Reproduction applies these checks to the original revisions.

Only the input's own references are included, not what those name in turn:
one level is what the actions that need this need, and following references
further would make a run read an unbounded graph.

## Consequences

- The antibody plugin's `predict_structure_with_immunebuilder` reads an antibody's chains
  through its input.
- The run's page lists what it read through references beside its inputs.
- The worker passes `included` through to `input.json` unchanged.
