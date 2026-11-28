# 0306. A type's flow is drawn from its procedures

- Status: Accepted
- Date: 2026-11-27
- Amends: [0199](0199-a-types-flow-is-drawn-where-its-records-are.md)

## Context

ADR 0199 draws each state field from `GET /entity-types/{type}/flow`, which reads the
type's states and guards. With a reviewed type's states coming from its pinned
procedure ([ADR 0301](0301-a-review-procedure-is-declared-once-and-a-type-says-what-it-needs.md)),
the type alone no longer says how its records move, and the guards are compiled from
declarations the diagram can read directly.

## Decision

**A reviewed type's flow lists its procedures.** For its review field, the flow
endpoint returns the type's needs and, for each procedure that fits it and that an
installed starter offers, each offered flow: its route, its states with their slots,
the roles they're bound to in the reader's project, signatures, locks and moves, and
who each state's signer must not be, in words read from `maker` and `independent_of`.
It also says which flow the project has chosen, which the deployment defaults to, and
which the plugin suggests (ADR 0302). Other state fields are drawn as before.

**A record's page draws its own flow.** The state panel shows the pinned procedure and
flow by name, draws that route only, and marks "Now" and "You can" as before.

**The type page says how it's reviewed.** "How records move" gains, for the review
field, the procedure and flow this project uses, and each other flow that could be
chosen, drawn the same way. The starter page draws its flows from the same response.
The web app still never works a flow out itself.

## Consequences

- Who must be different people is shown from the same declaration the server enforces,
  on the type page, the starter page and the record.
- `flow-fixtures.json` and its server test cover the procedures as well as the types.
