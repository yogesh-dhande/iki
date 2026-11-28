# 0302. A project chooses each type's review procedure, and the server pins it

- Status: Accepted
- Date: 2026-11-27
- Reverses: [0236](0236-a-lab-chooses-each-samples-testing-procedure.md) (the creator
  chooses a sample's procedure by its type),
  [0247](0247-a-project-chooses-how-its-experiments-are-signed.md) in part (an
  experiment re-pins its flow at each capture),
  [0261](0261-a-bank-check-follows-the-procedure-its-project-grants.md) in part (a bank
  check's procedure is whatever the granted roles allow)
- Refines: [0240](0240-a-project-chooses-its-release-stability-and-verification-review.md),
  [0266](0266-a-project-chooses-its-omics-results-review.md),
  [0198](0198-a-project-applies-a-versioned-assay-starter.md)

## Context

[ADR 0301](0301-a-review-procedure-is-declared-once-and-a-type-says-what-it-needs.md)
declares procedures once and lets a type pair with any that fits. Who picks the pairing
for a record, and when, was decided three different ways:

- the person creating a sample chose its procedure by choosing its type (0236), and a
  request's intake chose by request type (0195, 0235);
- a project's Admin chose, and the record that opened the work pinned it (0240, 0247,
  0266);
- a bank check followed whichever roles the project had granted (0261).

Nothing in the server decided. A record's flow was whatever the worker that created it
stamped, from a plugin setting each manifest defaulted to `research`, and each project's
choice overrode that setting at run launch. An analyst could create a plain sample for
a QC request, and nothing said the lab's procedure wasn't followed. An experiment
re-pinned its flow at every capture, so a project that changed its choice could close a
record under a lighter procedure than the one it was recorded under.

In a regulated lab a record's procedure comes from the SOP that governs the work. Its
owner (QA, or the project's lead) chooses it. The analyst doesn't, at the moment they
create the record, and it doesn't change under a record already in progress.

## Decision

**The choice is made per type, in three layers.**

1. **A plugin suggests.** Each starter (ADR 0303) a plugin ships names the procedure,
   the flows it offers and the one it suggests. Setup shows the suggestion
   preselected. It's never applied by itself.
2. **The deployment has a default per type.** An Org Admin may set, for a type, a
   default for every project that hasn't chosen: a starter, its flow, and its slots'
   roles (ADR 0304). Setting, changing or clearing a default needs a reason, and each
   is in the audit log with it.
3. **The project chooses per type.** A project's Admin, its procedure owner, applies a
   starter for a type, starting from the deployment's default if there is one and
   otherwise from the plugin's suggestion. Any compatible procedure may be chosen
   (ADR 0301). Applying is audited with the choice, the grants and the plan that was
   previewed (ADRs 0198, 0207).

A choice for a type applies to its subtypes, unless a subtype has one of its own. A
service's own request type, which adds what it asks for, follows the choice for
Request.

**The server pins the procedure when the record is created.** For a type whose
`review` is `pinned: create`, creating a record stamps `review_procedure` (such as
`iki/core.result_review@1`) and `review_flow` (such as `regulated_qc`) from the
project's choice, or, if the project hasn't chosen, from the deployment's default, and
sets the field to the flow's first state. For `pinned: {by: case}`, both come from the
record the reference names, which must already have them: a release case's
investigations and certificate follow the case, a stability study's measurements follow
the study. A type that only opens work for others declares `review: {opens: true}`: it
is pinned the same way and has no review field of its own.

The stamped values are immutable, for every writer. No action, import, bundle or
correction sets them, and workers no longer read a review setting or stamp a flow. A
create whose procedure can't be determined is refused, 409 `procedure_not_chosen`:
"ProteomicsResult has no review procedure in Discovery proteomics. A project Admin
chooses one in Project settings → Starters." The refusal is the same for an action's
outputs, an import, a spreadsheet and the API: the run is refused whole, as any refused
output is (ADR 0044). An opening record without a pin refuses its followers in the
same way.

**A pin is for the record's life.** A project or deployment that changes its choice
changes what new records are pinned to. Existing records keep their procedure, flow,
signatures and history, and finish as they began. An experiment is pinned when it's
created, and capturing evidence again doesn't re-pin it. A bank check is pinned like
any record, and its project's grants say only who may act.

**The pin is stored and enforced as data.** The deployment keeps each installed
procedure version's states, flows and which states lock, synced from its plugins. The
pinned columns and the review field reference them, so the database refuses a state
that isn't the record's procedure's. A procedure version stays installed while any
record pins it: upgrading its plugin can add versions, not change or remove one in use.
`review_procedure` and `review_flow` are searchable, in the grid and exports, and shown
on the record.

**Plugin settings no longer choose a procedure.** The `review_flow` and
`verification_flow` settings are removed, with their `default: research`, and with
them the run-launch override and the scripts' fallbacks. A deployment that wants one
procedure everywhere sets each type's default.

## Consequences

- A QC project's records can't be made under Research by choosing a type or launching
  an action: the server pins what its owner chose.
- A new deployment with no defaults and a project that hasn't chosen can't create
  reviewed records until someone with the authority chooses. That is deliberate. The
  refusal says who and where, and the starters page is one step away.
- Changing a choice never rewrites a record or invalidates a signature, as before.
  An Org Admin's default changes are attributable, with a reason, as a controlled
  procedure change should be.
- ADR 0236's concern, that nothing on the record says the lab's procedure was
  followed, is met by the stamp: the project set it, not the analyst, and it's on the
  record, its history and its exports.
- Tests and the dev profile apply starters before creating reviewed records, as labs
  will.

## Alternatives considered

- **Keep each manifest's default as a last resort.** A default the plugin author chose
  for every lab is a procedure nobody owns. Failing closed asks the owner once.
- **The creator chooses, by type or by a field.** The analyst would pick the
  procedure their own work is checked under.
- **Re-pin open records when the choice changes.** It lets a record recorded under
  Regulated QC be closed under Research, which is how an experiment's review could be
  downgraded.
- **A deployment minimum.** Considered and left out for now: a minimum needs an
  ordering between procedures, including labs' own, and the default plus each
  project's audited choice covers what labs asked for.
