# 0125. A state is a field, and each state says who may set it

- Status: Accepted
- Amended by [0301](0301-a-review-procedure-is-declared-once-and-a-type-says-what-it-needs.md): a review procedure declares its states, slots and independence once, compiled into guards
- Date: 2026-11-18
- Supersedes: [0118](0118-a-record-moves-through-its-types-states.md),
  [0119](0119-a-transition-can-keep-its-maker-apart-from-earlier-ones.md)
- Amends: [0120](0120-a-state-can-lock-a-record-and-the-database-holds-the-lock.md),
  [0121](0121-a-plugin-cant-strand-records-in-a-state-it-removes.md),
  [0122](0122-records-name-people-and-each-person-has-their-work.md)
- Refines: [0007](0007-typed-table-per-type-entity-storage.md),
  [0035](0035-entity-revisions-provenance-and-edit-rules.md),
  [0080](0080-triggers-run-actions-when-entities-change.md),
  [0116](0116-lab-roles-are-declared-by-plugins-and-given-per-project.md)

Updated by [ADR 0211](0211-lots-inventory-samples-and-requests.md). The shipping sample workflow now runs from a pending material draw through recorded results. Instrument and Booking are maintained by `iki/equipment`.

## Context

ADR 0118 gave each type one state and a graph of named transitions
between its states, each limited to roles, and ADR 0119 let a transition
keep its maker apart from the makers of earlier ones (`not_by`, four
eyes). Both put a lab's workflow policy in core, and both were the wrong
shape for it.

- **One state isn't enough.** A lot has a production status and, apart
  from it, a QA disposition. An instrument is in or out of service, and
  separately its calibration is due, done or failed. A sample's place in
  testing isn't its custody. Folding these into one state multiplies
  states and transitions, and hides the dimensions labs think in.
- **Transition graphs and separation of duties are policy, and policy
  varies.** Which state may follow which, who mustn't approve their own
  work, how many reviewers sign: each lab, and each regulator's reading,
  answers differently. A graph in core answers once for everyone, and
  every exception becomes a new core feature.
- **Other systems split it this way.** Salesforce keeps a picklist field
  and field-level permission in the platform, and puts "which values may
  follow which" in validation rules. Jira's workflow statuses take
  conditions and validators that apps supply. GitHub's branch protection
  is a primitive (who may push) with required status checks, supplied by
  anyone, layered on top.

## Decision

**A state is a field.** A type declares any number of fields of `type:
state`, each with its own `initial` state and its `states`. Each state has
a `label`, a `description`, `set_by` (lab roles, ADR 0116), `locks`,
`reason` (`required` or `optional`), and, when retired, `removed` and
`moved_to`. `signatures` is reserved for the electronic signatures a
state will require (step 55). A state field's value is a record's value
like any other: it's in the type's table, its history, search, the grid,
exports and bundles.

**Permission is only `set_by`.** Whoever holds one of a state's lab roles
in the record's project may set the field to that state, from any other
state. There's no transition graph, no `not_by` and no allowed moves in
core. A state field is set on its own, never with other values and never
by an action's results: `POST /entities/{ref}/states/{field}` with
`{state, reason}` and `If-Match`. Each change is its own revision, of kind
`state_changed`, naming the field, the state it left and the one it's in,
who set it (an agent and the person it acts for, ADR 0035), when, and the
reason. It's an `entity.state_changed` event, with the field and both
states, for webhooks and the org's audit log, and a trigger fires on it
for a field and, optionally, some of its states. `can.states` lists, per
field, each state the reader could set it to, whether they may, and why
not.

**Locking stays core** (amending ADR 0120). A state that `locks` freezes
the record's other fields and its archive, restore and registry moves, on
every path, and the database's trigger refuses any write to its values
while it's locked. A record is locked while any of its state fields is in
a locking state: `iki_entity.locked_by` lists those fields, and it's
locked while that isn't empty. State fields stay settable while locked, which is
how a record is unlocked. A refusal names the fields that lock it, and
the states, with their roles, that would unlock it: "SAM-000012 is locked
by its Status (Approved): it can’t be changed until its Status is set to
Registered (Coordinator), … or In review (Analyst)".

A locking state may also name an unsigned destination with `on_edit`. Its
role and guards apply to the return, which has its own state revision within
the transaction that corrects the value. [ADR 0179](0179-a-corrected-result-goes-back-for-review.md)

**Upgrades can't strand values** (amending ADR 0121, per state field). A
state that records may be in is kept, `removed: true`, with `moved_to:`
another of its field's states. A state field that's dropped is kept,
`removed: true`, with `moved_to: {field, states}` mapping its states into
another of the type's own state fields; without one, its plugin's
migration is refused while the type has records. A field can't become or
stop being a state field. Each record a migration moves gets a revision
by no one, via `system`, saying why, and its locks follow its new states.

**Work follows a state field** (amending ADR 0122). An assignment field
that's someone's work only in some states names the state field and its
states: `assignment: { field: status, states: [in_review] }`. A sample's
reviewer has it while its Status is In review, whatever its Custody. "It's
ready for you" is told when someone sets that field into one of those
states from one that isn't. My work filters by `field.state`
(`status.in_review`): any of a field's states, and every field named. A
locked record's people are locked with it, as its other values are, list
tables included.

**Workflow policy belongs to plugins, through guards** (step 58). Which
states may follow which, four eyes, and checks before a state are a
plugin's guards. So that a guard can decide them, Iki keeps what one
needs: the actor and the person it acts for, the record's values, every
state field's state, and each field's history of changes. Later, a state
can require checks the way GitHub's required status checks do.

## Consequences

- A type models each dimension of a record's process as its own field,
  with its own roles and locks, and labs see each in the grid, filters and
  history.
- Core's rule is small enough to check exhaustively: a role per state, a
  lock per state, a reason per state.
- Until guards come, any holder of a state's role can set it from any
  state, including undoing another's: example_lims's Sample can go from
  In review to Approved by its reviewer, but nothing in core stops the
  analyst who tested it holding the Reviewer role too. Labs that need four
  eyes give the roles to different people until a guard enforces it.
- A plugin's manifest keeps removed states and state fields for good, as
  it keeps removed fields.

## Alternatives considered

- **Keep transitions, add more state fields.** Each field would carry its
  own graph, and every lab's exceptions would still be core features.
- **Separation of duties in core (`not_by`).** It covers one policy of
  many (two people; but not "a QA reviewer other than the analyst's
  supervisor", nor "two of three signers"). Guards cover it and the rest.
- **States as a plain enum with field permissions.** It would lose the
  per-state roles, locks and reasons that make a state more than a value.
