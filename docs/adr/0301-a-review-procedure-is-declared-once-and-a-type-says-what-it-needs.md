# 0301. A review procedure is declared once, and a type says what it needs of one

- Status: Accepted
- Date: 2026-11-27
- Amends: [0125](0125-a-state-is-a-field-each-state-says-who-sets-it.md),
  [0128](0128-guards-say-when-a-state-may-be-set.md)
- Reverses in part: [0247](0247-a-project-chooses-how-its-experiments-are-signed.md)
  (one state field with a route per flow, chosen by guards),
  [0240](0240-a-project-chooses-its-release-stability-and-verification-review.md)
  (a starter names each flow's states)
- Refines: [0126](0126-an-electronic-signature-is-bound-to-the-revision-it-signs.md),
  [0179](0179-a-corrected-result-goes-back-for-review.md),
  [0266](0266-a-project-chooses-its-omics-results-review.md)

Who chooses a record's procedure, and when it's pinned, is
[ADR 0302](0302-a-project-chooses-each-types-review-procedure-and-the-server-pins-it.md).
Starters are [ADR 0303](0303-a-plugin-declares-its-starters.md), role slots
[ADR 0304](0304-lab-roles-are-shared-and-a-procedures-slots-are-bound-at-setup.md), the
types this removes [ADR 0305](0305-a-type-is-a-kind-of-work-not-a-review-procedure.md),
and the flow diagram [ADR 0306](0306-a-types-flow-is-drawn-from-its-procedures.md).

## Context

Research, Regulated QC and Record only are what labs call their review procedures. In
Iki they existed only as copies. Fourteen types in twelve plugins hand-wrote the same
state machine: the 66-line `review` field of proteomics, mass spectrometry and
sequencing analysis results was byte-identical. Thirty guards repeated "not the
creator" and twenty-seven "not an editor of the recorded cycle". Sixteen state machines
wrote independence by hand. A fix to four eyes had to be made in each, and a lab
writing its own type copied whichever one it found. A type chose its flow five ways:
a subtype per flow, a subtype plus an enum its guards re-checked, one type with a
pinned enum and a route per flow, a re-pinnable enum, and the roles a project granted.

The starters' server recipes said separately which roles must be different people.
Nothing compared that with the guards that enforced it at runtime.

[ADR 0125](0125-a-state-is-a-field-each-state-says-who-sets-it.md) and
[0128](0128-guards-say-when-a-state-may-be-set.md) kept separation of duties out of
core because policy varies. It does, but along a few axes that repeat, not freely:

1. what Research means: one reviewer's sign-off; a witness independent of the creator
   and editors; the performing engineer's own signature; QA alone; a reviewer and QA;
2. two shapes of Record only: stay recorded and open, or an unsigned step that locks;
3. a signature's meaning for the same step: review, verification, approval or witness;
4. the first state: `recorded`, or `scheduled` for a service appointment;
5. the edits window independence reads: the recorded state, a lot's quarantine and
   retest, a sample's testing cycle filtered to its measurements;
6. a type's own extra conditions: an assigned reviewer, a sealed case, a request still
   open, a fit's status, who performed the work;
7. where the choice is pinned: at create, or on the record that opens the work;
8. a derived document, a certificate of analysis issued from a reviewed certificate,
   which takes one signature;
9. deliberate differences in who's excluded: a lot's recorder only when it was made
   here, a sample's measurement editors but not its creator, a certificate's named
   analysts, an investigation's observer and investigator.

Labs also have SOPs of their own. A GMP site's result review might add a second
reviewer, or name its states as its SOP does. Until now that meant forking Iki's type.

Veeva Vault declares a lifecycle once and attaches it to document types. LabWare's
approval schemes are configuration that analyses refer to. Benchling configures review
on projects and templates, not on each schema. In GxP terms a record's procedure is a
controlled SOP, written once and applied to the work it governs.

## Decision

**A review procedure is a manifest of its own.** `kind: ReviewProcedure`, in a
plugin's `procedures/` directory, has a `name`, an integer `version`, a `label`, a
`description`, its role `slots`, its `states` and its `flows`. It's referred to by
qualified name and version: `iki/core.result_review@1`. Core ships
`iki/core.result_review@1`, and any plugin may declare its own, a lab's included. A
published version doesn't change: a different procedure is a new version, and a
deployment refuses a plugin whose procedure differs from the one its records pin
under the same version.

```yaml
apiVersion: iki/v1
kind: ReviewProcedure
name: result_review
version: 1
label: Result review
slots:
  analyst: { label: Analyst, description: Records the result and signs its submission. }
  reviewer: { label: Reviewer, description: Reviews the result independently. }
  qa: { label: QA, description: Releases an independently reviewed result. }
states:
  recorded:
    label: Recorded
    open: true
    by: [analyst, reviewer, qa]
    reason: required
  accepted:
    label: Accepted
    by: [analyst, reviewer]
    locks: true
  signed_off:
    label: Signed off
    by: reviewer
    locks: true
    signature: { meaning: review }
    independent_of: [creator, editors]
  submitted:
    label: Submitted
    by: analyst
    locks: true
    signature: { meaning: authorship }
    maker: true
  reviewed:
    label: Independently reviewed
    by: reviewer
    locks: true
    signature: { meaning: review }
    independent_of: [creator, editors, signers: [submitted]]
  released:
    label: QA released
    by: qa
    locks: true
    signature: { meaning: approval, label: QA release }
    independent_of: [creator, editors, signers: [submitted, reviewed]]
flows:
  record_only:
    label: Record only
    summary: The result is kept as evidence, as recorded, and no one signs it.
    route: [recorded]
  accepted:
    label: Accept without sign-off
    summary: The work is accepted as recorded and locked. No one signs it.
    route: [recorded, accepted]
  research:
    label: Research
    summary: One reviewer, who didn't make or edit the result, signs it off.
    route: [recorded, signed_off]
  regulated_qc:
    label: Regulated QC
    summary: >-
      The analyst signs the submission, an independent reviewer signs the review and QA
      signs the release: three different people.
    route: [recorded, submitted, reviewed, released]
    moves: { recorded: [submitted, reviewed] }
```

- **A state** has a `label`, a `description`, `by` (the slots whose holders may set
  it), `locks`, `reason`, at most one `signature` (meaning, label, statement, as ADR
  0126), and `on_edit` (ADR 0179). An `open` state is where the work is recorded and
  edited; it's the edits window that independence reads. Each flow starts in an open
  state.
- **A flow** is a `route` through the procedure's states, a `label` and a `summary`.
  Each state on the route may be set only from the one before it; the state after an
  open state may be set from any of the flow's open states. `moves` adds the other
  moves a flow allows, into a state from the states listed: a return for correction, a
  return to testing, a cancellation. The route's last state is the flow's **final**
  state. A record only ever has its own flow's states.
- **Who signs is declared on every signed state, one of two ways.** `maker` says the
  signer must be the work's maker: `maker: true` is the record's creator or someone who
  edited it in the current window, and `maker: {person: performed_by}` is the person a
  field names. An `authorship` signature ("I made this record") needs `maker`.
  `independent_of` says whom the signer must not be:
  - `creator`: whoever created the record;
  - `editors`: whoever edited it since it last entered one of its flow's open states,
    or, with `{editors: {fields: [measurements, notes]}}`, whoever changed those fields;
    on a state field without a procedure, the window is named (`{editors: {in:
[quarantined, retest_due]}}`);
  - `signers: [states]`: whoever last signed into those states;
  - `people: [fields]`: the people that person fields name, including a list of people
    or a person field one reference away (`test.observed_by`).

  Each item can take a `when` (CEL): a lot's recorder is excluded only when the lot was
  made here. A signed state with neither `maker` nor `independent_of` is refused by the
  validator, so every signature that attests to someone else's work declares who they
  must not be. Unsigned states, such as an acceptance that locks without a signature,
  declare neither.

**Independence is declared structure, compiled into guards.** The validator compiles
`by`, the route, `moves`, `maker` and `independent_of` into the guards ADR 0128 already
evaluates, one guard each with a generated message ("A signer independent of the
creator, the submitter and the editors must sign"), and checks them as it checks any
guard. The server enforces the compiled guards, under the record's lock and at every
signature, as before. The starter preview reads the same structure to say who must be
different people, and the flow diagram reads it to say so on each state. One source
means setup and runtime can't disagree. `independent_of` and `maker` are also available
on any state of an ordinary state field, so a lot's QA release declares its
independence the same way.

**Invariants no procedure relaxes.** A signed state needs a person: an agent can't sign
(ADR 0128). Every move is a revision with who, when and why (ADR 0125). Declared
independence is enforced by the server. A procedure has no way to turn any of them off.

**A type says what it needs of a procedure, not which one it uses.** A type that is
reviewed declares `review:`. Iki adds its state field (`review` unless `field` names
another) and two immutable fields beside it, `review_procedure` and `review_flow`,
which the server stamps at create (ADR 0302).

```yaml
review:
  pinned: create
  needs:
    open: recorded
    final: [signed_off, released]
    states: [submitted]
    locks: signed
    slots: [reviewer]
  independent_of: [people: [analysts]]
  guards:
    sealed:
      on: [submitted, signed_off]
      check: record.case.sealed == true
      message: Seal the release case before signing its certificate.
```

- `needs.open`: the state its actions write in, which every flow starts in;
- `needs.final`: the states its readers take as complete (a culture citing a check, a
  request receiving a result). Every flow's final state must be one of them, so a
  result that must be signed can't be paired with Record only by omitting it;
- `needs.states`: other states its notifications, assignments, triggers, rules and
  scripts name; the procedure must have each;
- `needs.locks`: `signed`, every signed state locks, or `final`, the final state locks;
- `needs.slots`: the slots its own fields and guards name.

The type adds what's its own. `independent_of` adds people to every state that declares
independence. `guards` add ordinary CEL conditions, each `on` named states, on
`signed` (every signed state) or on `final`. They're appended to the compiled guards
and keep ADR 0128's semantics. `derived: true` marks a record re-issued from one
already reviewed under the same flow, such as a certificate of analysis: its route is
the open state and the flow's final state, with the final state's slot and signature,
and its independence was enforced on the source.

Everything the type itself names (a notification on entering `submitted`, an assignment
in a state, a guard's states, a rule comparing `record.review` with a state) is checked
against its needs, as an enum's values are checked today.

**A procedure fits a type, or the pairing is refused.** A procedure is compatible with
a type when each of the flows offered has the type's `open` state first and one of its
`final` states last, has every state in `states`, locks as `locks` says, declares every
slot in `slots`, and has every state a type guard is `on`; and, for a derived type,
each final state is signed. The validator checks every pairing a plugin declares (ADR
0303), the deployment build checks them all, and the server checks again when a
project or deployment chooses one. A refusal names the type, the procedure and the
need it misses: "acme/qms.gmp_result_review@2 can't review ProteomicsResult: its flow
`gmp` ends in `approved`, and ProteomicsResult's readers take only `signed_off` or
`released` as complete."

So a lab writes its SOP as a procedure in its own plugin: its own states and labels,
its own number of reviewers, its signature meanings and exclusions. It applies it to
Iki's types, without forking them, wherever the type's needs are met.

## Consequences

- One correction to four eyes is made once, in core's procedure or the compiler, and
  every type using it has it.
- The nine axes above are each one declaration: Research's meaning is the procedure's
  flow; Record only is a one-state route or an unsigned locking state; the meaning is
  the state's signature; the first state is the route's; the window is the open
  states, with `fields` or `in`; extra conditions are the type's guards; the pin point
  is ADR 0302's; a derived document is `derived: true`; and exclusions are
  `independent_of` items with `when` and `people`.
- Research sign-off is now independent of the record's creator and editors in core's
  procedure, as a review signature implies. A lab whose researchers sign their own
  results uses a flow whose signature is the maker's.
- An authorship signature is the maker's. A submission by someone who neither created
  nor edited the record is refused.
- A plugin author reads one procedure, not a state machine per type. The type's page
  and the starter page show the procedures that fit it.
- A server test checks that every type with `review:` has `maker` or `independent_of`
  on every signed state of every compatible procedure, and a validator check refuses
  one without.
- A procedure is less expressive than free CEL. What a procedure can't say stays an
  ordinary guard on the type, as ADR 0128 allows.

## Alternatives considered

- **Leave the copies and test that starters agree with them.** Cheaper, but every fix
  is sixteen fixes, and labs copy the drift into their own plugins.
- **A macro that expands into the omics block, with patches.** The nine axes become
  patch soup, and independence stays opaque CEL that the preview can't read.
- **A procedure per type, named by the type.** That's today's coupling under a new
  name: a lab couldn't apply its SOP to Iki's types.
